from datetime import datetime, time, timedelta
import io
import json
import logging
import time as time_module
import os
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import pytz
import requests
import ta
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
LOGGER = logging.getLogger("quant_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

SYMBOL_MAP = {
    "Nifty 50": {"ticker": "^NSEI", "proxy": "NIFTYBEES.NS", "lot_size": 65, "future_underlying": "NIFTY"},
    "Bank Nifty": {"ticker": "^NSEBANK", "proxy": "BANKBEES.NS", "lot_size": 15, "future_underlying": "BANKNIFTY"},
    "TCS": {"ticker": "TCS.NS", "proxy": None, "lot_size": 175, "future_underlying": "TCS"},
    "Infosys (INFY)": {"ticker": "INFY.NS", "proxy": None, "lot_size": 400, "future_underlying": "INFY"},
    "State Bank of India (SBIN)": {"ticker": "SBIN.NS", "proxy": None, "lot_size": 750, "future_underlying": "SBIN"},
    "HDFC Bank": {"ticker": "HDFCBANK.NS", "proxy": None, "lot_size": 550, "future_underlying": "HDFCBANK"},
    "Reliance Industries": {"ticker": "RELIANCE.NS", "proxy": None, "lot_size": 250, "future_underlying": "RELIANCE"},
    "ICICI Bank": {"ticker": "ICICIBANK.NS", "proxy": None, "lot_size": 700, "future_underlying": "ICICIBANK"},
}

RSI_OVERSOLD_DEFAULT = 38
RSI_OVERBOUGHT_DEFAULT = 62
ADX_MAX_DEFAULT = 32
VWAP_STD_MULTIPLIER = 1.5
ATR_SL_MULTIPLIER = 2.5
ATR_TARGET_MULTIPLIER = 3.5
ENTRY_CUTOFF = time(14, 45)
EOD_SQUAREOFF = time(15, 15)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

YAHOO_TIMEOUT = 10
YAHOO_COOLDOWN_SECONDS = 300
INTRADAY_CACHE_TTL = 60
DAILY_CACHE_TTL = 6 * 60 * 60

_DATA_CACHE = {}
_DAILY_CACHE = {}
_YAHOO_RATE_LIMIT_UNTIL = 0.0

def _cache_key(ticker, period, interval):
    return ticker, period, interval

def _get_cache(cache, key, ttl):
    item = cache.get(key)
    if not item:
        return None
    ts, df = item
    if time_module.time() - ts > ttl:
        cache.pop(key, None)
        return None
    return df.copy()

def _set_cache(cache, key, df):
    cache[key] = (time_module.time(), df.copy())

def _is_yahoo_rate_limited():
    return time_module.time() < _YAHOO_RATE_LIMIT_UNTIL

def _mark_yahoo_rate_limited():
    global _YAHOO_RATE_LIMIT_UNTIL
    _YAHOO_RATE_LIMIT_UNTIL = time_module.time() + YAHOO_COOLDOWN_SECONDS

def _normalise_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close"]
    if not all(c in df.columns for c in required):
        return pd.DataFrame()
    if "Volume" not in df.columns:
        df["Volume"] = 0
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Open", "High", "Low", "Close"])

def _fetch_yfinance(ticker, period, interval):
    if _is_yahoo_rate_limited():
        LOGGER.warning("[DATA] Yahoo cooldown active; skip yfinance %s", ticker)
        return pd.DataFrame(), "YAHOO_COOLDOWN"
    try:
        df = yf.download(
            tickers=ticker, period=period, interval=interval,
            progress=False, auto_adjust=False, threads=False,
        )
        df = _normalise_ohlcv(df)
        if not df.empty:
            return df, "YFINANCE"
    except Exception as exc:
        name, text = type(exc).__name__, str(exc)
        if "RateLimit" in name or "Too Many Requests" in text or "429" in text:
            _mark_yahoo_rate_limited()
            LOGGER.error("[DATA] Yahoo rate limited for %s; cooldown=%ss", ticker, YAHOO_COOLDOWN_SECONDS)
            return pd.DataFrame(), "YAHOO_RATE_LIMIT"
        LOGGER.error("[DATA] yfinance failure %s: %s", ticker, exc)
    return pd.DataFrame(), "YFINANCE_EMPTY"

def _fetch_direct_yahoo_chart(ticker, range_str="1mo", interval="15m"):
    if _is_yahoo_rate_limited():
        return pd.DataFrame(), "YAHOO_COOLDOWN"
    encoded = quote(ticker, safe="")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
    }
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{encoded}?range={range_str}&interval={interval}&events=history"
        try:
            r = requests.get(url, headers=headers, timeout=YAHOO_TIMEOUT)
            if r.status_code == 429:
                _mark_yahoo_rate_limited()
                LOGGER.error("[DATA] Yahoo HTTP 429 for %s", ticker)
                return pd.DataFrame(), "YAHOO_RATE_LIMIT"
            if r.status_code != 200:
                continue
            result = (r.json().get("chart") or {}).get("result")
            if not result:
                continue
            result = result[0]
            ts = result.get("timestamp", [])
            q = (result.get("indicators") or {}).get("quote", [{}])[0]
            if not ts:
                continue
            df = pd.DataFrame({
                "Open": q.get("open"), "High": q.get("high"),
                "Low": q.get("low"), "Close": q.get("close"),
                "Volume": q.get("volume"),
            }, index=pd.to_datetime(ts, unit="s", utc=True))
            df = _normalise_ohlcv(df)
            if not df.empty:
                return df, "YAHOO_DIRECT"
        except requests.RequestException as exc:
            LOGGER.warning("[DATA] Yahoo direct network error %s: %s", ticker, exc)
        except (ValueError, KeyError, TypeError) as exc:
            LOGGER.warning("[DATA] Yahoo direct parse error %s: %s", ticker, exc)
    return pd.DataFrame(), "YAHOO_DIRECT_EMPTY"

def _fetch_proxy(ticker, period, interval):
    cfg = next((v for v in SYMBOL_MAP.values() if v["ticker"] == ticker), None)
    proxy = cfg.get("proxy") if cfg else None
    if not proxy or proxy == ticker or _is_yahoo_rate_limited():
        return pd.DataFrame(), "PROXY_SKIPPED"
    df, source = _fetch_yfinance(proxy, period, interval)
    if df.empty:
        return df, source
    if ticker == "^NSEI" and proxy == "NIFTYBEES.NS":
        for c in ["Open", "High", "Low", "Close"]:
            df[c] *= 100.0
    elif ticker == "^NSEBANK" and proxy == "BANKBEES.NS":
        # BANKBEES is an ETF proxy; do not fabricate an exact index scaling factor.
        # It is intentionally disabled for signal-grade index data.
        return pd.DataFrame(), "BANK_PROXY_DISABLED"
    return df, f"PROXY:{proxy}"

def _prepare_index(df):
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(IST)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[(df.index.time >= MARKET_OPEN) & (df.index.time <= MARKET_CLOSE)].copy()

def _fetch_daily_ema50(ticker):
    key = ticker
    cached = _get_cache(_DAILY_CACHE, key, DAILY_CACHE_TTL)
    if cached is not None:
        return cached
    if _is_yahoo_rate_limited():
        return pd.Series(dtype=float)
    try:
        daily = yf.download(tickers=ticker, period="1y", interval="1d", progress=False, auto_adjust=False, threads=False)
        daily = _normalise_ohlcv(daily)
        if daily.empty:
            return pd.Series(dtype=float)
        close = daily["Close"].dropna()
        ema = close.ewm(span=50, adjust=False, min_periods=50).mean()
        _set_cache(_DAILY_CACHE, key, ema.to_frame("EMA"))
        return ema
    except Exception as exc:
        if "RateLimit" in type(exc).__name__ or "429" in str(exc) or "Too Many Requests" in str(exc):
            _mark_yahoo_rate_limited()
        LOGGER.warning("[DATA] Daily EMA fetch failed %s: %s", ticker, exc)
        return pd.Series(dtype=float)

def _add_indicators(df, ticker):
    df = df.copy()
    df["Date"] = df.index.date
    df["Typical_Price"] = (df["High"] + df["Low"] + df["Close"]) / 3.0
    df["VP"] = df["Typical_Price"] * df["Volume"]
    df["Cum_VP"] = df.groupby("Date")["VP"].cumsum()
    df["Cum_Vol"] = df.groupby("Date")["Volume"].cumsum()
    df["VWAP"] = np.where(df["Cum_Vol"] > 0, df["Cum_VP"] / df["Cum_Vol"], df["Typical_Price"])
    df["VWAP_Std"] = df.groupby("Date")["Typical_Price"].transform("std").fillna(0)
    df["VWAP_Upper"] = df["VWAP"] + VWAP_STD_MULTIPLIER * df["VWAP_Std"]
    df["VWAP_Lower"] = df["VWAP"] - VWAP_STD_MULTIPLIER * df["VWAP_Std"]
    df["RSI"] = ta.momentum.RSIIndicator(df["Close"], window=14).rsi()
    df["ATR"] = ta.volatility.AverageTrueRange(df["High"], df["Low"], df["Close"], window=14).average_true_range()
    df["ADX"] = ta.trend.ADXIndicator(df["High"], df["Low"], df["Close"], window=14).adx()

    daily_ema = _fetch_daily_ema50(ticker)
    df["Daily_EMA50"] = np.nan
    if not daily_ema.empty:
        daily_ema.index = pd.to_datetime(daily_ema.index)
        if daily_ema.index.tz is not None:
            daily_ema.index = daily_ema.index.tz_convert(IST).tz_localize(None)
        mapping = daily_ema.dropna().to_dict()
        for idx in df.index:
            key = idx.tz_localize(None).normalize()
            prior = [d for d in mapping if d <= key]
            if prior:
                df.at[idx, "Daily_EMA50"] = mapping[max(prior)]
    df["Daily_Trend"] = np.where(
        df["Daily_EMA50"].notna(),
        np.where(df["Close"] > df["Daily_EMA50"], "BULLISH", "BEARISH"),
        "UNAVAILABLE",
    )
    return df.dropna(subset=["RSI", "ATR", "ADX"]).copy()

def fetch_and_prepare_data(ticker="^NSEI", period="1mo", interval="15m"):
    key = _cache_key(ticker, period, interval)
    cached = _get_cache(_DATA_CACHE, key, INTRADAY_CACHE_TTL)
    if cached is not None and not cached.empty:
        return cached

    df, source = _fetch_yfinance(ticker, period, interval)
    if df.empty and source not in ("YAHOO_RATE_LIMIT", "YAHOO_COOLDOWN"):
        df, source = _fetch_direct_yahoo_chart(ticker, period, interval)
    if df.empty and source not in ("YAHOO_RATE_LIMIT", "YAHOO_COOLDOWN") and ticker in ("^NSEI", "^NSEBANK"):
        df, source = _fetch_proxy(ticker, period, interval)
    if df.empty:
        LOGGER.error("[DATA] MARKET DATA UNAVAILABLE: %s (%s)", ticker, source)
        return pd.DataFrame()

    df = _prepare_index(df)
    df = _add_indicators(df, ticker)
    if df.empty:
        return pd.DataFrame()
    df.attrs["data_source"] = source
    df.attrs["degraded"] = str(source).startswith("PROXY:")
    df.attrs["fetched_at_ist"] = datetime.now(IST).isoformat()
    _set_cache(_DATA_CACHE, key, df)
    return df.copy()

def get_last_closed_candles(df, interval_minutes=15, now=None):
    if df.empty:
        return pd.DataFrame()
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = IST.localize(now)
    close_times = df.index + timedelta(minutes=interval_minutes)
    mask = close_times <= now
    return df.loc[mask].copy()

def evaluate_signal(df, rsi_oversold=38, rsi_overbought=62, adx_max=32,
                    require_closed=True, allow_degraded=False):
    if df is None or len(df) < 2:
        return {"signal": "HOLD", "reason": "INSUFFICIENT_DATA"}
    if bool(df.attrs.get("degraded", False)) and not allow_degraded:
        return {"signal": "HOLD", "reason": "DEGRADED_PROXY_DATA"}
    work = get_last_closed_candles(df) if require_closed else df
    if len(work) < 2:
        return {"signal": "HOLD", "reason": "NO_CLOSED_CANDLES"}
    row, prev = work.iloc[-1], work.iloc[-2]
    decision_time = row.name + timedelta(minutes=15)
    if decision_time.time() > ENTRY_CUTOFF:
        return {"signal": "HOLD", "reason": "ENTRY_WINDOW_CLOSED", "row": row, "previous": prev}
    buy = prev["Close"] < prev["VWAP_Lower"] and row["Close"] > row["Open"] and row["RSI"] < rsi_oversold and row["ADX"] < adx_max
    sell = prev["Close"] > prev["VWAP_Upper"] and row["Close"] < row["Open"] and row["RSI"] > rsi_overbought and row["ADX"] < adx_max
    if buy:
        reason = "Previous close below VWAP Lower + bullish reversal + RSI oversold + ADX below threshold"
        return {"signal": "BUY", "reason": reason, "row": row, "previous": prev, "decision_time": decision_time}
    if sell:
        reason = "Previous close above VWAP Upper + bearish reversal + RSI overbought + ADX below threshold"
        return {"signal": "SELL", "reason": reason, "row": row, "previous": prev, "decision_time": decision_time}
    return {"signal": "HOLD", "reason": "CONDITIONS_NOT_MET", "row": row, "previous": prev, "decision_time": decision_time}

def run_institutional_backtest(df, rsi_oversold=38, rsi_overbought=62, adx_max=32,
                               sl_atr_mult=2.5, tgt_atr_mult=3.5,
                               num_lots=1, lot_size=65, charges_per_trade=60.0):
    trades = []
    in_position = False
    pos_type = None
    entry_price = trailing_sl = tgt_price = 0.0
    entry_time = None
    entry_atr = 0.0
    daily_trade_count = {}
    qty = num_lots * lot_size
    charges = charges_per_trade * num_lots

    for i in range(2, len(df)):
        row, prev = df.iloc[i], df.iloc[i - 1]
        current_date = row["Date"]
        current_time = row.name.time()
        trades_today = daily_trade_count.get(current_date, 0)

        if in_position:
            exit_price = None
            reason = ""
            if current_time >= EOD_SQUAREOFF:
                exit_price, reason = row["Close"], "EOD Squareoff"
            elif pos_type == "BUY":
                if row["Low"] <= trailing_sl:
                    exit_price, reason = trailing_sl, "Trailing SL Hit"
                elif row["High"] >= tgt_price:
                    exit_price, reason = tgt_price, "Target Hit"
            else:
                if row["High"] >= trailing_sl:
                    exit_price, reason = trailing_sl, "Trailing SL Hit"
                elif row["Low"] <= tgt_price:
                    exit_price, reason = tgt_price, "Target Hit"

            if exit_price is not None:
                gross = (exit_price - entry_price) * qty if pos_type == "BUY" else (entry_price - exit_price) * qty
                trades.append({
                    "Type": pos_type, "EntryTime": entry_time, "ExitTime": row.name,
                    "EntryPrice": round(entry_price, 2), "ExitPrice": round(exit_price, 2),
                    "GrossPnL": round(gross, 2), "Charges": round(charges, 2),
                    "NetPnL": round(gross - charges, 2), "Reason": reason,
                })
                in_position = False
            else:
                # Update trailing stop only after evaluating this candle, so
                # the current candle cannot use its own high/low to create a
                # stop and then immediately hit that same stop.
                atr = float(row["ATR"])
                if pos_type == "BUY":
                    trailing_sl = max(trailing_sl, float(row["High"]) - atr * sl_atr_mult)
                else:
                    trailing_sl = min(trailing_sl, float(row["Low"]) + atr * sl_atr_mult)

        decision_close_time = (row.name + timedelta(minutes=15)).time()
        can_enter = (
            not in_position
            and decision_close_time <= ENTRY_CUTOFF
            and trades_today < 2
            and np.isfinite(row["ATR"])
            and np.isfinite(row["RSI"])
            and np.isfinite(row["ADX"])
        )
        if can_enter:
            buy = prev["Close"] < prev["VWAP_Lower"] and row["Close"] > row["Open"] and row["RSI"] < rsi_oversold and row["ADX"] < adx_max
            sell = prev["Close"] > prev["VWAP_Upper"] and row["Close"] < row["Open"] and row["RSI"] > rsi_overbought and row["ADX"] < adx_max
            if buy or sell:
                pos_type = "BUY" if buy else "SELL"
                entry_price = float(row["Close"])
                entry_time = row.name
                entry_atr = float(row["ATR"])
                tgt_price = entry_price + entry_atr * tgt_atr_mult if pos_type == "BUY" else entry_price - entry_atr * tgt_atr_mult
                trailing_sl = entry_price - entry_atr * sl_atr_mult if pos_type == "BUY" else entry_price + entry_atr * sl_atr_mult
                in_position = True
                daily_trade_count[current_date] = trades_today + 1

    return pd.DataFrame(trades)

def generate_monthly_breakdown(trades_df, capital):
    if trades_df.empty:
        return pd.DataFrame()
    df = trades_df.copy()
    exit_series = pd.to_datetime(df["ExitTime"], errors="coerce")
    if exit_series.dt.tz is not None:
        exit_series = exit_series.dt.tz_localize(None)
    df["YearMonth"] = exit_series.dt.strftime("%b %Y")
    s = df.groupby("YearMonth", sort=False).agg(
        TotalTrades=("Type", "count"),
        GrossPnL=("GrossPnL", "sum"),
        Charges=("Charges", "sum"),
        NetPnL=("NetPnL", "sum"),
    ).reset_index()
    s["WinRate"] = df.groupby("YearMonth", sort=False)["NetPnL"].apply(lambda x: f"{(x.gt(0).mean()*100):.1f}%").values
    s["Actual Profit %"] = (s["NetPnL"] / capital * 100).map(lambda x: f"{x:+.2f}%")
    s["Gross PnL (₹)"] = s["GrossPnL"].map("₹{:,.2f}".format)
    s["Charges (₹)"] = s["Charges"].map("₹{:,.2f}".format)
    s["Net PnL (₹)"] = s["NetPnL"].map("₹{:,.2f}".format)
    return s[["YearMonth","TotalTrades","WinRate","Gross PnL (₹)","Charges (₹)","Net PnL (₹)","Actual Profit %"]].rename(columns={"YearMonth":"Month","TotalTrades":"Total Trades"})

def export_trades_to_excel(trades_df):
    if trades_df.empty:
        return b""
    out = io.BytesIO()
    df = trades_df.copy()
    for c in df.columns:
        if "time" in c.lower() or "date" in c.lower():
            try:
                x = pd.to_datetime(df[c], errors="coerce")
                if x.dt.tz is not None:
                    x = x.dt.tz_localize(None)
                df[c] = x.dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Executed Trades")
    out.seek(0)
    return out.getvalue()

def load_historical_trades(file_path="data/historical_trades.json"):
    path = Path(file_path)
    if not path.exists():
        return pd.DataFrame()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        for c in ("EntryTime","ExitTime"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], errors="coerce")
        return df
    except Exception as exc:
        LOGGER.warning("[HISTORY] Could not load %s: %s", file_path, exc)
        return pd.DataFrame()

def get_combined_12m_trades(live_60d_trades, file_path="data/historical_trades.json"):
    saved = load_historical_trades(file_path)
    if saved.empty:
        return live_60d_trades.copy()
    if live_60d_trades.empty:
        return saved.copy()
    combined = pd.concat([saved, live_60d_trades], ignore_index=True)
    if {"EntryTime","Type"}.issubset(combined.columns):
        combined["_dedup"] = combined["EntryTime"].astype(str) + "|" + combined["Type"].astype(str)
        combined = combined.drop_duplicates("_dedup").drop(columns="_dedup")
    if "ExitTime" in combined.columns:
        combined = combined.sort_values("ExitTime")
    return combined.reset_index(drop=True)
