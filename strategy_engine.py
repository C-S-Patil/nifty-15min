
import numpy as np
import pandas as pd
import pytz
import requests
import ta
import yfinance as yf


def fetch_and_prepare_data(
    ticker: str = "^NSEI", interval: str = "15m", period: str = "1mo"
) -> pd.DataFrame:
    """Fetches intraday data and converts timestamps strictly to Asia/Kolkata (IST)."""
    try:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )

        # 1. Fetch 15m Intraday Data
        dat = yf.Ticker(ticker, session=session)
        df_15m = dat.history(interval=interval, period=period)

        if df_15m.empty:
            df_15m = yf.download(
                ticker,
                interval=interval,
                period="1mo",
                progress=False,
                session=session,
            )

        if df_15m.empty:
            return pd.DataFrame()

        # Handle MultiIndex columns
        if isinstance(df_15m.columns, pd.MultiIndex):
            df_15m.columns = df_15m.columns.get_level_values(0)

        # 2. Timezone Normalization to IST (Asia/Kolkata)
        ist = pytz.timezone("Asia/Kolkata")
        if df_15m.index.tzinfo is None:
            df_15m.index = df_15m.index.tz_localize("UTC").tz_convert(ist)
        else:
            df_15m.index = df_15m.index.tz_convert(ist)

        df_15m.dropna(subset=["Close", "Volume"], inplace=True)

        # 3. Fetch Daily Data for 50 EMA HTF Filter
        df_daily = dat.history(interval="1d", period="1y")
        if df_daily.empty:
            df_daily = yf.download(
                ticker,
                interval="1d",
                period="1y",
                progress=False,
                session=session,
            )

        if isinstance(df_daily.columns, pd.MultiIndex):
            df_daily.columns = df_daily.columns.get_level_values(0)

        if df_daily.index.tzinfo is None:
            df_daily.index = df_daily.index.tz_localize("UTC").tz_convert(ist)
        else:
            df_daily.index = df_daily.index.tz_convert(ist)

        # Calculate Daily 50 EMA
        df_daily["Daily_EMA50"] = ta.trend.EMAIndicator(
            df_daily["Close"], window=50
        ).ema_indicator()
        df_daily["Date"] = df_daily.index.date

        # 4. Process Intraday Indicators & Daily Resetted VWAP
        df_15m["Date"] = df_15m.index.date
        df_15m["Time"] = df_15m.index.time
        df_15m["TypicalPrice"] = (
            df_15m["High"] + df_15m["Low"] + df_15m["Close"]
        ) / 3
        df_15m["TP_Vol"] = df_15m["TypicalPrice"] * df_15m["Volume"]

        # Cumulative VWAP grouped by IST Date
        df_15m["Cum_TP_Vol"] = df_15m.groupby("Date")["TP_Vol"].cumsum()
        df_15m["Cum_Vol"] = df_15m.groupby("Date")["Volume"].cumsum()
        df_15m["VWAP"] = df_15m["Cum_TP_Vol"] / df_15m["Cum_Vol"]

        # Technical Indicators
        df_15m["RSI"] = ta.momentum.RSIIndicator(
            df_15m["Close"], window=14
        ).rsi()
        df_15m["ATR"] = ta.volatility.AverageTrueRange(
            df_15m["High"], df_15m["Low"], df_15m["Close"], window=14
        ).average_true_range()

        df_15m["VWAP_Upper"] = df_15m["VWAP"] + (df_15m["ATR"] * 1.0)
        df_15m["VWAP_Lower"] = df_15m["VWAP"] - (df_15m["ATR"] * 1.0)

        # Map Daily EMA to 15m Candles
        daily_ema_map = df_daily.set_index("Date")["Daily_EMA50"].to_dict()
        df_15m["Daily_EMA50"] = df_15m["Date"].map(daily_ema_map)
        df_15m["Daily_EMA50"] = df_15m["Daily_EMA50"].ffill()

        return df_15m.dropna()

    except Exception as e:
        print(f"Data Processing Error: {e}")
        return pd.DataFrame()


def run_institutional_backtest(
    df: pd.DataFrame,
    rsi_oversold: int = 38,
    rsi_overbought: int = 62,
    sl_atr_mult: float = 1.5,
    tgt_atr_mult: float = 2.5,
    lot_size: int = 75,
    capital: float = 100000.0,
    slippage_pct: float = 0.0005,
    brokerage_per_order: float = 20.0,
    tax_rate: float = 0.0006,
) -> pd.DataFrame:
    """Executes backtest with HTF Trend, Dynamic Envelopes, TSL, EOD Squareoff, and Fees."""
    trades = []
    in_position = False
    pos_type = None
    entry_price = 0.0
    trailing_sl = 0.0
    tgt_price = 0.0
    entry_time = None

    for i in range(1, len(df)):
        row = df.iloc[i]
        current_time = row.name.time()

        if in_position:
            high, low, close = row["High"], row["Low"], row["Close"]
            current_atr = row["ATR"] if not np.isnan(row["ATR"]) else 10.0

            exit_triggered = False
            raw_exit_price = 0.0
            exit_reason = ""

            # Force EOD Intraday Exit at 03:15 PM IST
            if current_time >= pd.to_datetime("15:15").time():
                exit_triggered = True
                raw_exit_price = close
                exit_reason = "EOD Squareoff"

            elif pos_type == "BUY":
                new_sl = high - (current_atr * sl_mult)
                trailing_sl = max(trailing_sl, new_sl)

                if low <= trailing_sl:
                    exit_triggered = True
                    raw_exit_price = trailing_sl
                    exit_reason = "Trailing SL Hit"
                elif high >= tgt_price:
                    exit_triggered = True
                    raw_exit_price = tgt_price
                    exit_reason = "Target Hit"

            elif pos_type == "SELL":
                new_sl = low + (current_atr * sl_mult)
                trailing_sl = min(trailing_sl, new_sl)

                if high >= trailing_sl:
                    exit_triggered = True
                    raw_exit_price = trailing_sl
                    exit_reason = "Trailing SL Hit"
                elif low <= tgt_price:
                    exit_triggered = True
                    raw_exit_price = tgt_price
                    exit_reason = "Target Hit"

            if exit_triggered:
                actual_exit_price = (
                    raw_exit_price * (1 - slippage_pct)
                    if pos_type == "BUY"
                    else raw_exit_price * (1 + slippage_pct)
                )

                gross_pnl = (
                    (actual_exit_price - entry_price) * lot_size
                    if pos_type == "BUY"
                    else (entry_price - actual_exit_price) * lot_size
                )

                turnover = (entry_price + actual_exit_price) * lot_size
                brokerage = brokerage_per_order * 2
                taxes = turnover * tax_rate
                total_charges = brokerage + taxes
                net_pnl = gross_pnl - total_charges

                trades.append(
                    {
                        "Type": pos_type,
                        "EntryTime": entry_time,
                        "ExitTime": row.name,
                        "EntryPrice": round(entry_price, 2),
                        "ExitPrice": round(actual_exit_price, 2),
                        "GrossPnL": round(gross_pnl, 2),
                        "Charges": round(total_charges, 2),
                        "NetPnL": round(net_pnl, 2),
                        "Reason": exit_reason,
                    }
                )
                in_position = False

        if (
            not in_position
            and current_time < pd.to_datetime("14:45").time()
            and not np.isnan(row["ATR"])
        ):
            close = row["Close"]
            daily_ema = row["Daily_EMA50"]

            if (
                close > daily_ema
                and close < row["VWAP_Lower"]
                and row["RSI"] < rsi_oversold
            ):
                in_position = True
                pos_type = "BUY"
                entry_price = close * (1 + slippage_pct)
                entry_time = row.name
                trailing_sl = entry_price - (row["ATR"] * sl_mult)
                tgt_price = entry_price + (row["ATR"] * tgt_mult)

            elif (
                close < daily_ema
                and close > row["VWAP_Upper"]
                and row["RSI"] > rsi_overbought
            ):
                in_position = True
                pos_type = "SELL"
                entry_price = close * (1 - slippage_pct)
                entry_time = row.name
                trailing_sl = entry_price + (row["ATR"] * sl_mult)
                tgt_price = entry_price - (row["ATR"] * tgt_mult)

    return pd.DataFrame(trades)
