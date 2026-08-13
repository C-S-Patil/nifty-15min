from datetime import time
import io
import json
import os
from urllib.parse import quote
import numpy as np
import pandas as pd
import pytz
import requests
import ta
import yfinance as yf
import logging
import time as time_module


SYMBOL_MAP = {
    "Nifty 50": {"ticker": "^NSEI", "proxy": "NIFTYBEES.NS", "lot_size": 65},
    "Bank Nifty": {
        "ticker": "^NSEBANK",
        "proxy": "BANKBEES.NS",
        "lot_size": 15,
    },
    "TCS": {"ticker": "TCS.NS", "proxy": "TCS.NS", "lot_size": 175},
    "Infosys (INFY)": {
        "ticker": "INFY.NS",
        "proxy": "INFY.NS",
        "lot_size": 400,
    },
    "State Bank of India (SBIN)": {
        "ticker": "SBIN.NS",
        "proxy": "SBIN.NS",
        "lot_size": 750,
    },
    "HDFC Bank": {
        "ticker": "HDFCBANK.NS",
        "proxy": "HDFCBANK.NS",
        "lot_size": 550,
    },
    "Reliance Industries": {
        "ticker": "RELIANCE.NS",
        "proxy": "RELIANCE.NS",
        "lot_size": 250,
    },
    "ICICI Bank": {
        "ticker": "ICICIBANK.NS",
        "proxy": "ICICIBANK.NS",
        "lot_size": 700,
    },
}


LOGGER = logging.getLogger("quant_engine")

IST = pytz.timezone("Asia/Kolkata")

YAHOO_TIMEOUT = 10
YAHOO_CACHE_TTL_SECONDS = 60
YAHOO_COOLDOWN_SECONDS = 300

# Process-local cache.
# This dramatically reduces Streamlit rerun traffic.
_DATA_CACHE = {}
_YAHOO_RATE_LIMIT_UNTIL = 0.0


def _cache_key(ticker: str, period: str, interval: str) -> tuple:
    return ticker, period, interval


def _get_cached_data(ticker: str, period: str, interval: str):
    key = _cache_key(ticker, period, interval)
    item = _DATA_CACHE.get(key)

    if not item:
        return None

    timestamp, df = item

    if time_module.time() - timestamp > YAHOO_CACHE_TTL_SECONDS:
        _DATA_CACHE.pop(key, None)
        return None

    return df.copy()


def _set_cached_data(ticker: str, period: str, interval: str, df: pd.DataFrame):
    _DATA_CACHE[_cache_key(ticker, period, interval)] = (
        time_module.time(),
        df.copy(),
    )


def _is_yahoo_rate_limited() -> bool:
    return time_module.time() < _YAHOO_RATE_LIMIT_UNTIL


def _mark_yahoo_rate_limited():
    global _YAHOO_RATE_LIMIT_UNTIL
    _YAHOO_RATE_LIMIT_UNTIL = (
        time_module.time() + YAHOO_COOLDOWN_SECONDS
    )


def _normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    required = ["Open", "High", "Low", "Close"]

    if not all(col in df.columns for col in required):
        return pd.DataFrame()

    if "Volume" not in df.columns:
        df["Volume"] = 0

    df = df[["Open", "High", "Low", "Close", "Volume"]]

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])

    return df


def _fetch_yfinance(
    ticker: str,
    period: str,
    interval: str,
) -> pd.DataFrame:

    if _is_yahoo_rate_limited():
        LOGGER.warning(
            "[DATA] Yahoo cooldown active; skipping yfinance for %s",
            ticker,
        )
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
            LOGGER.info(
                "[DATA] yfinance success: %s rows=%d",
                ticker,
                len(df),
            )
            return df

        LOGGER.warning(
            "[DATA] yfinance returned empty data: %s",
            ticker,
        )

    except Exception as exc:
        error_name = type(exc).__name__
        error_text = str(exc)

        if (
            "RateLimit" in error_name
            or "Too Many Requests" in error_text
            or "429" in error_text
        ):
            _mark_yahoo_rate_limited()

            LOGGER.error(
                "[DATA] Yahoo RATE LIMITED: %s. "
                "Yahoo requests disabled for %ss.",
                ticker,
                YAHOO_COOLDOWN_SECONDS,
            )
        else:
            LOGGER.error(
                "[DATA] yfinance failure for %s: %s: %s",
                ticker,
                error_name,
                error_text,
            )

    return pd.DataFrame()


def _fetch_direct_yahoo_chart(
    ticker: str,
    range_str: str = "1mo",
    interval: str = "15m",
) -> pd.DataFrame:

    if _is_yahoo_rate_limited():
        LOGGER.warning(
            "[DATA] Yahoo cooldown active; skipping direct API: %s",
            ticker,
        )
        return pd.DataFrame()

    encoded_ticker = quote(ticker, safe="")

    hosts = [
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Connection": "close",
    }

    for host in hosts:
        url = (
            f"https://{host}/v8/finance/chart/"
            f"{encoded_ticker}"
            f"?range={range_str}&interval={interval}"
            f"&events=history"
        )

        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=YAHOO_TIMEOUT,
            )

            if response.status_code == 429:
                _mark_yahoo_rate_limited()

                LOGGER.error(
                    "[DATA] Yahoo HTTP 429 for %s via %s",
                    ticker,
                    host,
                )

                return pd.DataFrame()

            if response.status_code != 200:
                LOGGER.warning(
                    "[DATA] Yahoo HTTP %s for %s via %s",
                    response.status_code,
                    ticker,
                    host,
                )
                continue

            payload = response.json()
            result = payload.get("chart", {}).get("result")

            if not result:
                LOGGER.warning(
                    "[DATA] Yahoo returned no chart result for %s",
                    ticker,
                )
                continue

            result = result[0]

            timestamps = result.get("timestamp", [])
            quote_data = (
                result.get("indicators", {})
                .get("quote", [{}])[0]
            )

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
                index=pd.to_datetime(
                    timestamps,
                    unit="s",
                    utc=True,
                ),
            )

            df = _normalise_ohlcv(df)

            if not df.empty:
                LOGGER.info(
                    "[DATA] Direct Yahoo success: %s rows=%d",
                    ticker,
                    len(df),
                )
                return df

        except requests.RequestException as exc:
            LOGGER.warning(
                "[DATA] Direct Yahoo network failure "
                "%s via %s: %s",
                ticker,
                host,
                exc,
            )

        except (ValueError, KeyError, TypeError) as exc:
            LOGGER.warning(
                "[DATA] Invalid Yahoo response "
                "%s via %s: %s",
                ticker,
                host,
                exc,
            )

    return pd.DataFrame()


def _fetch_proxy(
    ticker: str,
    period: str,
    interval: str,
) -> pd.DataFrame:

    config = next(
        (
            item
            for item in SYMBOL_MAP.values()
            if item["ticker"] == ticker
        ),
        None,
    )

    if not config:
        return pd.DataFrame()

    proxy = config.get("proxy")

    if not proxy or proxy == ticker:
        return pd.DataFrame()

    LOGGER.warning(
        "[DATA] Attempting ETF proxy %s for %s",
        proxy,
        ticker,
    )

    # Only make the proxy request when the normal ticker
    # failed. This prevents unnecessary Yahoo traffic.
    df = _fetch_yfinance(
        proxy,
        period,
        interval,
    )

    if df.empty:
        return df

    # NIFTYBEES is approximately 1/100th of NIFTY.
    if ticker == "^NSEI" and proxy == "NIFTYBEES.NS":
        for col in ["Open", "High", "Low", "Close"]:
            df[col] *= 100.0

    return df


def fetch_and_prepare_data(
    ticker: str = "^NSEI",
    period: str = "1mo",
    interval: str = "15m",
) -> pd.DataFrame:

    """
    Production market-data provider.

    Provider order:

        1. Process-local cache
        2. yfinance
        3. Direct Yahoo Chart API
        4. ETF proxy for indices

    IMPORTANT:
    We intentionally do NOT retry the same Yahoo request repeatedly.
    A Yahoo 429 activates a cooldown to prevent request storms.
    """

    cached = _get_cached_data(
        ticker,
        period,
        interval,
    )

    if cached is not None and not cached.empty:
        LOGGER.info(
            "[DATA] Cache hit: %s rows=%d",
            ticker,
            len(cached),
        )
        return cached

    LOGGER.info(
        "[DATA] Fetching %s period=%s interval=%s",
        ticker,
        period,
        interval,
    )

    df = _fetch_yfinance(
        ticker,
        period,
        interval,
    )

    if df.empty:
        df = _fetch_direct_yahoo_chart(
            ticker,
            range_str=period,
            interval=interval,
        )

    if df.empty and ticker in ("^NSEI", "^NSEBANK"):
        df = _fetch_proxy(
            ticker,
            period,
            interval,
        )

    if df.empty:
        LOGGER.error(
            "[DATA] MARKET DATA UNAVAILABLE: %s",
            ticker,
        )
        return pd.DataFrame()

    # Timezone -> IST
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    df.index = df.index.tz_convert(IST)

    # Remove duplicate timestamps.
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()

    df["Date"] = df.index.date

    # VWAP
    df["Typical_Price"] = (
        df["High"] + df["Low"] + df["Close"]
    ) / 3

    df["VP"] = (
        df["Typical_Price"] * df["Volume"]
    )

    df["Cum_VP"] = (
        df.groupby("Date")["VP"]
        .cumsum()
    )

    df["Cum_Vol"] = (
        df.groupby("Date")["Volume"]
        .cumsum()
    )

    df["VWAP"] = np.where(
        df["Cum_Vol"] > 0,
        df["Cum_VP"] / df["Cum_Vol"],
        df["Typical_Price"],
    )

    df["VWAP_Std"] = (
        df.groupby("Date")["Typical_Price"]
        .transform("std")
        .fillna(0)
    )

    df["VWAP_Upper"] = (
        df["VWAP"] + 1.5 * df["VWAP_Std"]
    )

    df["VWAP_Lower"] = (
        df["VWAP"] - 1.5 * df["VWAP_Std"]
    )

    df["RSI"] = ta.momentum.RSIIndicator(
        close=df["Close"],
        window=14,
    ).rsi()

    df["ATR"] = ta.volatility.AverageTrueRange(
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        window=14,
    ).average_true_range()

    df["Daily_EMA50"] = ta.trend.EMAIndicator(
        close=df["Close"],
        window=50,
    ).ema_indicator()

    df["ADX"] = ta.trend.ADXIndicator(
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        window=14,
    ).adx()

    df = df.dropna()

    if df.empty:
        return pd.DataFrame()

    _set_cached_data(
        ticker,
        period,
        interval,
        df,
    )

    return df.copy()


def run_institutional_backtest(
    df: pd.DataFrame,
    rsi_oversold: int = 38,
    rsi_overbought: int = 62,
    sl_atr_mult: float = 2.5,
    tgt_atr_mult: float = 3.5,
    num_lots: int = 1,
    lot_size: int = 65,
    charges_per_trade: float = 60.0,
) -> pd.DataFrame:
    trades = []
    in_position = False
    pos_type = None
    entry_price = 0.0
    trailing_sl = 0.0
    tgt_price = 0.0
    entry_time = None
    daily_trade_count = {}

    total_qty = num_lots * lot_size
    total_charges = charges_per_trade * num_lots

    for i in range(2, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]
        current_date = row["Date"]
        current_time = row.name.time()

        trades_today = daily_trade_count.get(current_date, 0)

        if in_position:
            high, low, close = row["High"], row["Low"], row["Close"]
            current_atr = row["ATR"] if not np.isnan(row["ATR"]) else 15.0

            exit_triggered = False
            exit_price = 0.0
            exit_reason = ""

            if current_time >= time(15, 15):
                exit_triggered = True
                exit_price = close
                exit_reason = "EOD Squareoff"

            elif pos_type == "BUY":
                if high >= entry_price + (current_atr * 1.5):
                    new_sl = high - (current_atr * 1.5)
                    trailing_sl = max(trailing_sl, new_sl)

                if low <= trailing_sl:
                    exit_triggered = True
                    exit_price = trailing_sl
                    exit_reason = "Trailing SL Hit"
                elif high >= tgt_price:
                    exit_triggered = True
                    exit_price = tgt_price
                    exit_reason = "Target Hit"

            elif pos_type == "SELL":
                if low <= entry_price - (current_atr * 1.5):
                    new_sl = low + (current_atr * 1.5)
                    trailing_sl = min(trailing_sl, new_sl)

                if high >= trailing_sl:
                    exit_triggered = True
                    exit_price = trailing_sl
                    exit_reason = "Trailing SL Hit"
                elif low <= tgt_price:
                    exit_triggered = True
                    exit_price = tgt_price
                    exit_reason = "Target Hit"

            if exit_triggered:
                gross_pnl = (
                    (exit_price - entry_price) * total_qty
                    if pos_type == "BUY"
                    else (entry_price - exit_price) * total_qty
                )
                net_pnl = gross_pnl - total_charges

                trades.append(
                    {
                        "Type": pos_type,
                        "EntryTime": entry_time,
                        "ExitTime": row.name,
                        "EntryPrice": round(entry_price, 2),
                        "ExitPrice": round(exit_price, 2),
                        "GrossPnL": round(gross_pnl, 2),
                        "Charges": round(total_charges, 2),
                        "NetPnL": round(net_pnl, 2),
                        "Reason": exit_reason,
                    }
                )
                in_position = False

        if (
            not in_position
            and current_time < time(14, 45)
            and trades_today < 2
            and row["ATR"] >= 10.0
        ):
            close = row["Close"]
            adx = row["ADX"] if "ADX" in row else 20.0

            if (
                prev_row["Close"] < prev_row["VWAP_Lower"]
                and close > row["Open"]
                and row["RSI"] < rsi_oversold
                and adx < 32
            ):
                in_position = True
                pos_type = "BUY"
                entry_price = close
                entry_time = row.name
                trailing_sl = entry_price - (row["ATR"] * sl_atr_mult)
                tgt_price = entry_price + (row["ATR"] * tgt_atr_mult)
                daily_trade_count[current_date] = trades_today + 1

            elif (
                prev_row["Close"] > prev_row["VWAP_Upper"]
                and close < row["Open"]
                and row["RSI"] > rsi_overbought
                and adx < 32
            ):
                in_position = True
                pos_type = "SELL"
                entry_price = close
                entry_time = row.name
                trailing_sl = entry_price + (row["ATR"] * sl_atr_mult)
                tgt_price = entry_price - (row["ATR"] * tgt_atr_mult)
                daily_trade_count[current_date] = trades_today + 1

    return pd.DataFrame(trades)


def generate_monthly_breakdown(
    trades_df: pd.DataFrame, capital: float
) -> pd.DataFrame:
    if trades_df.empty:
        return pd.DataFrame()

    df = trades_df.copy()
    exit_series = pd.to_datetime(df["ExitTime"])
    if exit_series.dt.tz is not None:
        exit_series = exit_series.dt.tz_localize(None)

    df["YearMonth"] = exit_series.dt.strftime("%b %Y")

    monthly_summary = (
        df.groupby("YearMonth", sort=False)
        .agg(
            TotalTrades=("Type", "count"),
            WinRate=(
                "NetPnL",
                lambda x: f"{(len(x[x > 0]) / len(x) * 100):.1f}%",
            ),
            GrossPnL=("GrossPnL", "sum"),
            Charges=("Charges", "sum"),
            NetPnL=("NetPnL", "sum"),
        )
        .reset_index()
    )

    monthly_summary["Actual Profit %"] = (
        monthly_summary["NetPnL"] / capital * 100
    ).apply(lambda x: f"{x:+.2f}%")
    monthly_summary["Gross PnL (₹)"] = monthly_summary["GrossPnL"].map(
        "₹{:,.2f}".format
    )
    monthly_summary["Charges (₹)"] = monthly_summary["Charges"].map(
        "₹{:,.2f}".format
    )
    monthly_summary["Net PnL (₹)"] = monthly_summary["NetPnL"].map(
        "₹{:,.2f}".format
    )

    return monthly_summary[
        [
            "YearMonth",
            "TotalTrades",
            "WinRate",
            "Gross PnL (₹)",
            "Charges (₹)",
            "Net PnL (₹)",
            "Actual Profit %",
        ]
    ].rename(columns={"YearMonth": "Month", "TotalTrades": "Total Trades"})


def export_trades_to_excel(trades_df: pd.DataFrame) -> bytes:
    if trades_df.empty:
        return b""

    df_export = trades_df.copy()
    for col in df_export.columns:
        if "time" in col.lower() or "date" in col.lower():
            try:
                df_export[col] = (
                    pd.to_datetime(df_export[col])
                    .dt.tz_localize(None)
                    .dt.strftime("%Y-%m-%d %H:%M:%S")
                )
            except Exception:
                pass

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df_export.to_excel(writer, index=False, sheet_name="Executed Trades")

    output.seek(0)
    return output.getvalue()


def load_historical_trades(
    file_path: str = "data/historical_trades.json",
) -> pd.DataFrame:
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                data = json.load(f)
            if data:
                df = pd.DataFrame(data)
                df["EntryTime"] = pd.to_datetime(df["EntryTime"])
                df["ExitTime"] = pd.to_datetime(df["ExitTime"])
                return df
        except Exception as e:
            print(f"Error loading historical trades JSON: {e}")

    return pd.DataFrame()


def get_combined_12m_trades(
    live_60d_trades: pd.DataFrame,
    file_path: str = "data/historical_trades.json",
) -> pd.DataFrame:
    saved_df = load_historical_trades(file_path)

    if saved_df.empty:
        return live_60d_trades

    if live_60d_trades.empty:
        return saved_df

    combined = pd.concat([saved_df, live_60d_trades], ignore_index=True)
    combined["EntryStr"] = combined["EntryTime"].astype(str)
    combined = combined.drop_duplicates(
        subset=["EntryStr", "Type"]
    ).drop(columns=["EntryStr"])
    combined = combined.sort_values(by="ExitTime").reset_index(drop=True)
    return combined
    
