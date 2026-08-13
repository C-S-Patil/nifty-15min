
from __future__ import annotations

import io
import json
import logging
import math
import os
import time as time_module
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import pytz
import requests
import ta
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")

# --------------------------- Strategy constants ---------------------------

RSI_OVERSOLD_DEFAULT = 38
RSI_OVERBOUGHT_DEFAULT = 62
ADX_MAX_DEFAULT = 32
ENTRY_CUTOFF = time(14, 45)          # last signal candle must end by 14:45
EOD_SQUAREOFF = time(15, 15)         # forced exit decision time
ATR_SL_MULTIPLIER = 2.5
ATR_TARGET_MULTIPLIER = 3.5
ATR_TRAIL_MULTIPLIER = 1.5
VWAP_BAND_STD_MULTIPLIER = 1.5
MAX_TRADES_PER_DAY = 2
SLIPPAGE_BPS = 2.0                   # 2 bps per side in research/backtest
MIN_BACKTEST_TRADES_FOR_STATS = 30
RELIABLE_BACKTEST_TRADES = 100

SYMBOL_MAP = {
    "Nifty 50": {
        "ticker": "^NSEI",
        "proxy": "NIFTYBEES.NS",
        "lot_size": 65,
        "future_underlying": "NIFTY",
    },
    "Bank Nifty": {
        "ticker": "^NSEBANK",
        "proxy": "BANKBEES.NS",
        "lot_size": 15,
        "future_underlying": "BANKNIFTY",
    },
}
LOGGER = logging.getLogger("quant_engine")
YAHOO_TIMEOUT = 10
YAHOO_INTRADAY_CACHE_TTL = 60
YAHOO_DAILY_CACHE_TTL = 3600
YAHOO_COOLDOWN_SECONDS = 300
_DATA_CACHE = {}
_DAILY_CACHE = {}
_YAHOO_RATE_LIMIT_UNTIL = 0.0


def _cache_key(ticker, period, interval):
    return ticker, period, interval


def _get_cached(cache, key, ttl):
    item = cache.get(key)
    if not item:
        return None
    ts, df = item
    if time_module.time() - ts > ttl:
        cache.pop(key, None)
        return None
    return df.copy()


def _set_cached(cache, key, df):
    cache[key] = (time_module.time(), df.copy())


def _is_rate_limited():
    return time_module.time() < _YAHOO_RATE_LIMIT_UNTIL


def _mark_rate_limited():
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
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df


def _fetch_yfinance(ticker, period, interval):
    if _is_rate_limited():
        LOGGER.warning("[DATA] Yahoo cooldown active; skipping yfinance %s", ticker)
        return pd.DataFrame()
    try:
        df = yf.download(
            tickers=ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        df = _normalise_ohlcv(df)
        if not df.empty:
            return df
    except Exception as exc:
        text = str(exc)
        if "RateLimit" in type(exc).__name__ or "Too Many Requests" in text or "429" in text:
            _mark_rate_limited()
            LOGGER.error("[DATA] Yahoo rate limited; cooldown=%ss", YAHOO_COOLDOWN_SECONDS)
        else:
            LOGGER.warning("[DATA] yfinance %s failed: %s", ticker, exc)
    return pd.DataFrame()


def _fetch_direct_yahoo_chart(ticker, range_str="1mo", interval="15m"):
    if _is_rate_limited():
        return pd.DataFrame()
    encoded = quote(ticker, safe="")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Connection": "close",
    }
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{encoded}?range={range_str}&interval={interval}&events=history"
        try:
            r = requests.get(url, headers=headers, timeout=YAHOO_TIMEOUT)
            if r.status_code == 429:
                _mark_rate_limited()
                LOGGER.error("[DATA] Yahoo HTTP 429")
                return pd.DataFrame()
            if r.status_code != 200:
                continue
            result = (r.json().get("chart") or {}).get("result")
            if not result:
                continue
            result = result[0]
            timestamps = result.get("timestamp", [])
            quote_data = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            if not timestamps:
                continue
            df = pd.DataFrame(
                {
                    "Open": quote_data.get("open"),
                    "High": quote_data.get("high"),
                    "Low": quote_data.get("low"),
                    "Close": quote_data.get("close"),
                    "Volume": quote_data.get("volume"),
                },
                index=pd.to_datetime(timestamps, unit="s", utc=True),
            )
            df = _normalise_ohlcv(df)
            if not df.empty:
                return df
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            LOGGER.warning("[DATA] direct Yahoo failure %s: %s", host, exc)
    return pd.DataFrame()


def _find_symbol_config(ticker):
    for name, cfg in SYMBOL_MAP.items():
        if cfg["ticker"] == ticker:
            return name, cfg
    return None, None


def _fetch_proxy(ticker, period, interval):
    name, cfg = _find_symbol_config(ticker)
    if not cfg:
        return pd.DataFrame()
    proxy = cfg.get("proxy")
    if not proxy or proxy == ticker:
        return pd.DataFrame()
    df = _fetch_yfinance(proxy, period, interval)
    if df.empty:
        df = _fetch_direct_yahoo_chart(proxy, period, interval)
    if df.empty:
        return df
    if ticker == "^NSEI" and proxy == "NIFTYBEES.NS":
        for col in ["Open", "High", "Low", "Close"]:
            df[col] *= 100.0
    # BANKBEES is not a guaranteed fixed ratio; never fabricate index prices.
    # Return proxy only as monitoring data and mark it degraded.
    return df


def _prepare_index(df):
    if df.empty:
        return df
    df = df.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(IST)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _fetch_daily_closes(ticker):
    key = (ticker, "1y", "1d")
    cached = _get_cached(_DAILY_CACHE, key, YAHOO_DAILY_CACHE_TTL)
    if cached is not None:
        return cached
    df = _fetch_yfinance(ticker, "1y", "1d")
    if df.empty:
        df = _fetch_direct_yahoo_chart(ticker, "1y", "1d")
    if df.empty:
        return pd.DataFrame()
    df = _prepare_index(df)
    _set_cached(_DAILY_CACHE, key, df)
    return df


def _attach_daily_trend(df, ticker):
    daily = _fetch_daily_closes(ticker)
    if daily.empty or len(daily) < 55:
        df["Daily_EMA50"] = np.nan
        df["Daily_Trend"] = "UNKNOWN"
        return df
    # Use only completed daily candles. If today's daily bar exists, exclude it.
    now_ist = datetime.now(IST)
    daily = daily[daily.index < pd.Timestamp(now_ist.date(), tz=IST)]
    if len(daily) < 50:
        df["Daily_EMA50"] = np.nan
        df["Daily_Trend"] = "UNKNOWN"
        return df
    daily_close = daily["Close"].astype(float)
    ema = daily_close.ewm(span=50, adjust=False, min_periods=50).mean().dropna()
    trend = pd.Series(index=ema.index, data=np.where(daily_close.loc[ema.index] > ema, "BULLISH", "BEARISH"))
    # Map the latest completed daily value backward to intraday bars of the same/later date.
    ema_frame = pd.DataFrame({"Daily_EMA50": ema, "Daily_Trend": trend})
    df["Daily_EMA50"] = np.nan
    df["Daily_Trend"] = "UNKNOWN"
    for ts, values in ema_frame.iterrows():
        mask = df.index.normalize() >= ts.normalize()
        df.loc[mask, "Daily_EMA50"] = values["Daily_EMA50"]
        df.loc[mask, "Daily_Trend"] = values["Daily_Trend"]
    return df


def _weighted_vwap_bands(df):
    df = df.copy()
    df["Date"] = df.index.date
    df["Typical_Price"] = (df["High"] + df["Low"] + df["Close"]) / 3.0
    volume = df["Volume"].fillna(0).clip(lower=0)
    price_volume = df["Typical_Price"] * volume
    cum_pv = price_volume.groupby(df["Date"]).cumsum()
    cum_vol = volume.groupby(df["Date"]).cumsum()
    df["VWAP"] = np.where(cum_vol > 0, cum_pv / cum_vol, df["Typical_Price"])
    # True volume-weighted dispersion around the running VWAP.
    diff2 = (df["Typical_Price"] - df["VWAP"]) ** 2
    cum_var = (diff2 * volume).groupby(df["Date"]).cumsum()
    df["VWAP_Std"] = np.sqrt(np.where(cum_vol > 0, cum_var / cum_vol, 0.0))
    df["VWAP_Upper"] = df["VWAP"] + VWAP_BAND_STD_MULTIPLIER * df["VWAP_Std"]
    df["VWAP_Lower"] = df["VWAP"] - VWAP_BAND_STD_MULTIPLIER * df["VWAP_Std"]
    return df


def prepare_research_data(raw_df):
    """Prepare a user-supplied 15-minute OHLCV dataset for research/backtesting.

    The dataset should contain at least Open, High, Low, Close and preferably Volume,
    with a timezone-aware or IST-localizable DatetimeIndex. This path never calls Yahoo.
    """
    df = _normalise_ohlcv(raw_df)
    if df.empty:
        return pd.DataFrame()
    df = _prepare_index(df)
    df = _weighted_vwap_bands(df)
    df["RSI"] = ta.momentum.RSIIndicator(df["Close"], window=14).rsi()
    df["ATR"] = ta.volatility.AverageTrueRange(
        df["High"], df["Low"], df["Close"], window=14
    ).average_true_range()
    df["ADX"] = ta.trend.ADXIndicator(
        df["High"], df["Low"], df["Close"], window=14
    ).adx()
    df["EMA50_15m"] = ta.trend.EMAIndicator(df["Close"], window=50).ema_indicator()

    daily = df["Close"].resample("1D").last().dropna()
    ema = daily.ewm(span=50, adjust=False, min_periods=50).mean()
    # Shift one completed daily value so an intraday bar never sees the current day's close.
    daily_ema = ema.shift(1).dropna()
    trend = pd.Series(
        np.where(daily.loc[daily_ema.index] > daily_ema, "BULLISH", "BEARISH"),
        index=daily_ema.index,
    )
    df["Daily_EMA50"] = np.nan
    df["Daily_Trend"] = "UNKNOWN"
    for day, value in daily_ema.items():
        mask = df.index.normalize() == day.normalize()
        df.loc[mask, "Daily_EMA50"] = float(value)
        df.loc[mask, "Daily_Trend"] = trend.loc[day]
    df = df.dropna(subset=["VWAP", "VWAP_Upper", "VWAP_Lower", "RSI", "ATR", "ADX", "EMA50_15m"])
    df.attrs["data_source"] = "USER_RESEARCH_FILE"
    df.attrs["degraded"] = False
    df.attrs["primary_data"] = True
    return df

def research_dataset_diagnostics(raw_df, prepared_df):
    """Return transparent diagnostics for an uploaded research dataset."""
    raw_rows = int(len(raw_df)) if raw_df is not None else 0
    prepared_rows = int(len(prepared_df)) if prepared_df is not None else 0
    if prepared_df is None or prepared_df.empty:
        return {
            "raw_rows": raw_rows,
            "usable_rows": 0,
            "start": None,
            "end": None,
            "trading_days": 0,
            "median_bars_per_day": 0.0,
            "daily_ema_ready": False,
        }
    idx = prepared_df.index
    days = pd.Series(idx.normalize()).drop_duplicates()
    counts = pd.Series(1, index=idx).groupby(idx.normalize()).sum()
    return {
        "raw_rows": raw_rows,
        "usable_rows": prepared_rows,
        "start": idx.min(),
        "end": idx.max(),
        "trading_days": int(len(days)),
        "median_bars_per_day": float(counts.median()) if not counts.empty else 0.0,
        "daily_ema_ready": bool(prepared_df.get("Daily_EMA50", pd.Series(dtype=float)).notna().any())
            if prepared_df is not None else False,
    }


def fetch_and_prepare_data(ticker="^NSEI", period="1mo", interval="15m"):
    key = _cache_key(ticker, period, interval)
    cached = _get_cached(_DATA_CACHE, key, YAHOO_INTRADAY_CACHE_TTL)
    if cached is not None:
        return cached

    source = "YFINANCE"
    degraded = False
    df = _fetch_yfinance(ticker, period, interval)
    if df.empty:
        df = _fetch_direct_yahoo_chart(ticker, period, interval)
        source = "YAHOO_DIRECT"
    if df.empty and ticker in ("^NSEI", "^NSEBANK"):
        df = _fetch_proxy(ticker, period, interval)
        source = "ETF_PROXY"
        degraded = not df.empty
    if df.empty:
        LOGGER.error("[DATA] Market data unavailable for %s", ticker)
        return pd.DataFrame()

    df = _prepare_index(df)
    df = _weighted_vwap_bands(df)
    df["RSI"] = ta.momentum.RSIIndicator(df["Close"], window=14).rsi()
    df["ATR"] = ta.volatility.AverageTrueRange(
        df["High"], df["Low"], df["Close"], window=14
    ).average_true_range()
    df["ADX"] = ta.trend.ADXIndicator(
        df["High"], df["Low"], df["Close"], window=14
    ).adx()
    # This is a distinct intraday indicator. Do not call it Daily_EMA50.
    df["EMA50_15m"] = ta.trend.EMAIndicator(df["Close"], window=50).ema_indicator()
    df = _attach_daily_trend(df, ticker)
    df = df.dropna(subset=["VWAP", "VWAP_Upper", "VWAP_Lower", "RSI", "ATR", "ADX", "EMA50_15m"])
    df.attrs["data_source"] = source
    df.attrs["degraded"] = degraded
    df.attrs["primary_data"] = not degraded
    _set_cached(_DATA_CACHE, key, df)
    return df.copy()


def get_last_closed_candles(df, now=None):
    if df is None or df.empty:
        return pd.DataFrame()
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = IST.localize(now)
    out = df.copy()
    ends = out.index + pd.Timedelta(minutes=15)
    out = out[ends <= pd.Timestamp(now)]
    return out


def evaluate_signal(
    df,
    rsi_oversold=RSI_OVERSOLD_DEFAULT,
    rsi_overbought=RSI_OVERBOUGHT_DEFAULT,
    adx_max=ADX_MAX_DEFAULT,
    require_closed=True,
    use_daily_trend_filter=True,
):
    if df is None or df.empty:
        return {"signal": "HOLD", "reason": "NO_DATA", "row": None, "decision_time": None}
    closed = get_last_closed_candles(df) if require_closed else df.copy()
    if len(closed) < 2:
        return {"signal": "HOLD", "reason": "WAITING_FOR_CLOSED_CANDLE", "row": None, "decision_time": None}
    latest = closed.iloc[-1]
    previous = closed.iloc[-2]
    candle_end = latest.name + pd.Timedelta(minutes=15)
    if candle_end.time() > ENTRY_CUTOFF:
        return {"signal": "HOLD", "reason": "ENTRY_WINDOW_CLOSED", "row": latest, "decision_time": candle_end}
    if bool(df.attrs.get("degraded", False)):
        return {"signal": "HOLD", "reason": "DEGRADED_PROXY_DATA", "row": latest, "decision_time": candle_end}
    if use_daily_trend_filter and (latest["Daily_Trend"] == "UNKNOWN" or pd.isna(latest["Daily_EMA50"])):
        return {"signal": "HOLD", "reason": "DAILY_EMA_UNAVAILABLE", "row": latest, "decision_time": candle_end}

    buy = (
        previous["Close"] < previous["VWAP_Lower"]
        and latest["Close"] > latest["Open"]
        and latest["RSI"] < rsi_oversold
        and latest["ADX"] < adx_max
        and (not use_daily_trend_filter or latest["Daily_Trend"] == "BULLISH")
    )
    sell = (
        previous["Close"] > previous["VWAP_Upper"]
        and latest["Close"] < latest["Open"]
        and latest["RSI"] > rsi_overbought
        and latest["ADX"] < adx_max
        and (not use_daily_trend_filter or latest["Daily_Trend"] == "BEARISH")
    )
    if buy:
        reason = (
            f"Previous close below VWAP lower; bullish reversal; "
            f"RSI {latest['RSI']:.1f} < {rsi_oversold}; ADX {latest['ADX']:.1f} < {adx_max}"
        )
        return {"signal": "BUY", "reason": reason, "row": latest, "decision_time": candle_end}
    if sell:
        reason = (
            f"Previous close above VWAP upper; bearish reversal; "
            f"RSI {latest['RSI']:.1f} > {rsi_overbought}; ADX {latest['ADX']:.1f} < {adx_max}"
        )
        return {"signal": "SELL", "reason": reason, "row": latest, "decision_time": candle_end}
    return {"signal": "HOLD", "reason": "ENTRY_CONDITIONS_NOT_MET", "row": latest, "decision_time": candle_end}


def _apply_slippage(price, side, bps=SLIPPAGE_BPS):
    factor = 1 + bps / 10000 if side == "BUY" else 1 - bps / 10000
    return price * factor


def _trade_stats(trades, capital):
    if trades is None or trades.empty:
        return {
            "sample_size": 0, "win_rate": np.nan, "profit_factor": np.nan,
            "expectancy": np.nan, "avg_winner": np.nan, "avg_loser": np.nan,
            "avg_r_multiple": np.nan, "net_pnl": 0.0, "return_pct": 0.0,
            "max_drawdown": 0.0, "max_drawdown_pct": 0.0, "max_losing_streak": 0,
            "avg_trades_per_month": 0.0, "best_month": np.nan, "worst_month": np.nan,
            "status": "NO_TRADES",
        }
    pnl = pd.to_numeric(trades["NetPnL"], errors="coerce").fillna(0.0)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax()
    max_dd = float(drawdown.min()) if not drawdown.empty else 0.0
    peak_capital = capital if capital > 0 else 1.0
    loss_streak = 0
    max_loss_streak = 0
    for x in pnl:
        if x < 0:
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
        else:
            loss_streak = 0
    exits = pd.to_datetime(trades["ExitTime"], errors="coerce")
    # PeriodArray cannot retain timezone metadata. Strip it explicitly here
    # instead of letting pandas emit a warning during to_period().
    if not exits.empty and getattr(exits.dt, "tz", None) is not None:
        exits = exits.dt.tz_localize(None)
    months = exits.dt.to_period("M") if not exits.empty else pd.Series(dtype="period[M]")
    monthly = trades.assign(_month=months).groupby("_month")["NetPnL"].sum() if not trades.empty else pd.Series(dtype=float)
    return {
        "sample_size": len(trades),
        "win_rate": float((pnl > 0).mean() * 100),
        "profit_factor": float(gross_profit / gross_loss) if gross_loss > 0 else np.inf,
        "expectancy": float(pnl.mean()),
        "avg_winner": float(wins.mean()) if not wins.empty else 0.0,
        "avg_loser": float(losses.mean()) if not losses.empty else 0.0,
        "avg_r_multiple": float(pd.to_numeric(trades.get("RMultiple", pd.Series(dtype=float)), errors="coerce").mean()) if "RMultiple" in trades else np.nan,
        "net_pnl": float(pnl.sum()),
        "return_pct": float(pnl.sum() / peak_capital * 100),
        "max_drawdown": max_dd,
        "max_drawdown_pct": float(max_dd / peak_capital * 100),
        "max_losing_streak": max_loss_streak,
        "avg_trades_per_month": float(len(trades) / max(len(monthly), 1)),
        "best_month": float(monthly.max()) if not monthly.empty else np.nan,
        "worst_month": float(monthly.min()) if not monthly.empty else np.nan,
        "status": "RELIABLE" if len(trades) >= RELIABLE_BACKTEST_TRADES else ("PRELIMINARY" if len(trades) >= MIN_BACKTEST_TRADES_FOR_STATS else "INSUFFICIENT_SAMPLE"),
    }


def calculate_strategy_statistics(trades, capital=250000):
    return _trade_stats(trades, capital)


def run_institutional_backtest(
    df,
    rsi_oversold=RSI_OVERSOLD_DEFAULT,
    rsi_overbought=RSI_OVERBOUGHT_DEFAULT,
    adx_max=ADX_MAX_DEFAULT,
    sl_atr_mult=ATR_SL_MULTIPLIER,
    tgt_atr_mult=ATR_TARGET_MULTIPLIER,
    num_lots=1,
    use_daily_trend_filter=True,
    lot_size=65,
    charges_per_trade=60.0,
    slippage_bps=SLIPPAGE_BPS,
):
    if df is None or df.empty or bool(df.attrs.get("degraded", False)):
        return pd.DataFrame()

    closed = get_last_closed_candles(df)
    if len(closed) < 3:
        return pd.DataFrame()

    records = []
    in_position = False
    side = None
    entry = stop = target = risk = 0.0
    entry_time = None
    trades_today = {}
    qty = int(num_lots * lot_size)
    charges = float(charges_per_trade * max(num_lots, 1))

    # Signal on candle i-1; execute at candle i open.
    for i in range(1, len(closed) - 1):
        signal_candle = closed.iloc[i - 1]
        execution_candle = closed.iloc[i]
        signal_end = signal_candle.name + pd.Timedelta(minutes=15)
        trade_date = execution_candle.name.date()

        exited_this_bar = False
        if in_position:
            high, low = float(execution_candle["High"]), float(execution_candle["Low"])
            exit_price = None
            reason = None
            # Forced square-off has priority at/after 15:15.
            # Before EOD, if both SL and target are touched in one candle,
            # assume the stop was hit first (conservative/no look-ahead).
            if execution_candle.name.time() >= EOD_SQUAREOFF:
                exit_price, reason = float(execution_candle["Close"]), "EOD Squareoff"
            elif side == "BUY":
                if low <= stop:
                    exit_price, reason = stop, "Stop Loss"
                elif high >= target:
                    exit_price, reason = target, "Target"
            else:
                if high >= stop:
                    exit_price, reason = stop, "Stop Loss"
                elif low <= target:
                    exit_price, reason = target, "Target"

            if exit_price is not None:
                exit_side = "SELL" if side == "BUY" else "BUY"
                executed_exit = _apply_slippage(float(exit_price), exit_side, slippage_bps)
                gross = (executed_exit - entry) * qty if side == "BUY" else (entry - executed_exit) * qty
                net = gross - charges
                r_mult = gross / (risk * qty) if risk > 0 else np.nan
                records.append({
                    "EntryTime": entry_time,
                    "ExitTime": execution_candle.name,
                    "Type": side,
                    "EntryPrice": entry,
                    "ExitPrice": executed_exit,
                    "Quantity": qty,
                    "GrossPnL": gross,
                    "Charges": charges,
                    "NetPnL": net,
                    "RMultiple": r_mult,
                    "ExitReason": reason,
                    "SignalCandle": signal_candle.name,
                })
                in_position = False
                side = None
                exited_this_bar = True

        # Never enter a new trade using a signal candle that occurred while
        # another position was open on this same bar.
        if exited_this_bar:
            continue
        if in_position:
            continue
        if signal_end.time() > ENTRY_CUTOFF:
            continue
        count = trades_today.get(trade_date, 0)
        if count >= MAX_TRADES_PER_DAY:
            continue

        prev_row = closed.iloc[i - 2]
        row = signal_candle
        buy = (
            prev_row["Close"] < prev_row["VWAP_Lower"]
            and row["Close"] > row["Open"]
            and row["RSI"] < rsi_oversold
            and row["ADX"] < adx_max
            and (not use_daily_trend_filter or row["Daily_Trend"] == "BULLISH")
        )
        sell = (
            prev_row["Close"] > prev_row["VWAP_Upper"]
            and row["Close"] < row["Open"]
            and row["RSI"] > rsi_overbought
            and row["ADX"] < adx_max
            and (not use_daily_trend_filter or row["Daily_Trend"] == "BEARISH")
        )
        if not (buy or sell):
            continue

        side = "BUY" if buy else "SELL"
        raw_entry = float(execution_candle["Open"])
        entry = _apply_slippage(raw_entry, side, slippage_bps)
        atr = float(row["ATR"])
        risk = atr * sl_atr_mult
        target_distance = atr * tgt_atr_mult
        if side == "BUY":
            stop = entry - risk
            target = entry + target_distance
        else:
            stop = entry + risk
            target = entry - target_distance
        entry_time = execution_candle.name
        trades_today[trade_date] = count + 1
        in_position = True

    # Force-close a still-open trade at the last available closed candle.
    if in_position and len(closed):
        last = closed.iloc[-1]
        exit_side = "SELL" if side == "BUY" else "BUY"
        executed_exit = _apply_slippage(float(last["Close"]), exit_side, slippage_bps)
        gross = (executed_exit - entry) * qty if side == "BUY" else (entry - executed_exit) * qty
        net = gross - charges
        r_mult = gross / (risk * qty) if risk > 0 else np.nan
        records.append({
            "EntryTime": entry_time,
            "ExitTime": last.name,
            "Type": side,
            "EntryPrice": entry,
            "ExitPrice": executed_exit,
            "Quantity": qty,
            "GrossPnL": gross,
            "Charges": charges,
            "NetPnL": net,
            "RMultiple": r_mult,
            "ExitReason": "End of Backtest",
            "SignalCandle": entry_time - pd.Timedelta(minutes=15),
        })

    return pd.DataFrame(records)


def generate_monthly_breakdown(trades, capital=250000):
    if trades is None or trades.empty:
        return pd.DataFrame()
    t = trades.copy()
    exits = pd.to_datetime(t["ExitTime"], errors="coerce")
    if exits.dt.tz is not None:
        exits = exits.dt.tz_localize(None)
    t["Month"] = exits.dt.strftime("%b %Y")
    grouped = t.groupby("Month", sort=False).agg(
        Trades=("NetPnL", "size"),
        NetPnL=("NetPnL", "sum"),
        WinRate=("NetPnL", lambda s: (s > 0).mean() * 100),
    )
    grouped["Return %"] = grouped["NetPnL"] / capital * 100
    return grouped.reset_index()


def load_historical_trades(path="data/historical_trades.json"):
    p = Path(path)
    if not p.exists():
        return pd.DataFrame()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not raw:
            return pd.DataFrame()
        return pd.DataFrame(raw)
    except Exception as exc:
        LOGGER.warning("Historical trades read failed: %s", exc)
        return pd.DataFrame()


def get_combined_12m_trades(backtest_trades, path="data/historical_trades.json"):
    hist = load_historical_trades(path)
    parts = [x for x in (hist, backtest_trades) if x is not None and not x.empty]
    if not parts:
        return pd.DataFrame()
    combined = pd.concat(parts, ignore_index=True, sort=False)
    if "ExitTime" in combined.columns:
        exits = pd.to_datetime(combined["ExitTime"], errors="coerce")
        if getattr(exits.dt, "tz", None) is not None:
            exits = exits.dt.tz_localize(None)
        cutoff = pd.Timestamp.now().tz_localize(None) - pd.DateOffset(months=12)
        combined = combined.assign(ExitTime=exits)
        combined = combined[combined["ExitTime"] >= cutoff]
    subset = [c for c in ["EntryTime", "ExitTime", "Type"] if c in combined.columns]
    return combined.drop_duplicates(subset=subset, keep="last").reset_index(drop=True)


def export_trades_to_excel(trades):
    """Return an Excel workbook with Excel-safe values.

    Excel does not support timezone-aware datetimes. Live/backtest timestamps
    are intentionally kept in IST elsewhere in the application, so we strip
    timezone metadata only in this export copy.
    """
    if trades is None:
        return b""

    export_df = trades.copy()

    for column in export_df.columns:
        series = export_df[column]

        # Native pandas datetime columns.
        if pd.api.types.is_datetime64_any_dtype(series):
            if getattr(series.dt, "tz", None) is not None:
                export_df[column] = series.dt.tz_localize(None)
            continue

        # Object columns can contain timezone-aware Timestamp objects when
        # records were assembled from dictionaries.
        if series.dtype == "object":
            try:
                parsed = pd.to_datetime(series, errors="raise")
                if pd.api.types.is_datetime64_any_dtype(parsed):
                    if getattr(parsed.dt, "tz", None) is not None:
                        parsed = parsed.dt.tz_localize(None)
                    export_df[column] = parsed
            except (TypeError, ValueError, OverflowError):
                # Not a datetime-like object column; leave it unchanged.
                pass

    output = io.BytesIO()
    with pd.ExcelWriter(
        output,
        engine="xlsxwriter",
        datetime_format="yyyy-mm-dd hh:mm:ss",
    ) as writer:
        export_df.to_excel(writer, index=False, sheet_name="Trades")

        worksheet = writer.sheets["Trades"]
        worksheet.freeze_panes(1, 0)
        worksheet.autofilter(
            0,
            0,
            max(len(export_df), 1),
            max(len(export_df.columns) - 1, 0),
        )

        # Sensible widths without making the workbook enormous.
        for idx, column in enumerate(export_df.columns):
            values = export_df[column].astype(str).replace("nan", "")
            width = min(max(len(str(column)), values.str.len().max() if len(values) else 0) + 2, 28)
            worksheet.set_column(idx, idx, width)

    return output.getvalue()
