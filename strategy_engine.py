import datetime
import numpy as np
import pandas as pd
import pytz
import requests
import ta
import yfinance as yf


def filter_active_market_hours(df: pd.DataFrame) -> pd.DataFrame:
    """Filters data to retain only active exchange trading sessions (09:15 - 15:30 IST)

    and special evening trading windows (e.g. Samvat / Muhurat Trading).
    """
    if df.empty:
        return df

    # Convert index times to time objects for fast filtering
    times = df.index.time
    standard_start = datetime.time(9, 15)
    standard_end = datetime.time(15, 30)

    # Special session window (e.g., Muhurat Trading usually 18:00 - 19:30 IST)
    muhurat_start = datetime.time(18, 0)
    muhurat_end = datetime.time(19, 30)

    # Filter mask for Standard Market Hours OR Special Evening Session
    standard_mask = (times >= standard_start) & (times <= standard_end)
    muhurat_mask = (times >= muhurat_start) & (times <= muhurat_end)

    active_df = df[standard_mask | muhurat_mask].copy()
    return active_df


def fetch_and_prepare_data(
    ticker: str = "^NSEI", interval: str = "15m", period: str = "1mo"
) -> pd.DataFrame:
    """Fetches intraday data and strips non-trading hours."""
    try:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )

        dat = yf.Ticker(ticker, session=session)
        df_15m = dat.history(interval=interval, period=period)

        if df_15m.empty:
            df_15m = yf.download(
                ticker,
                interval=interval,
                period="5d",
                progress=False,
                session=session,
            )

        if df_15m.empty:
            return pd.DataFrame()

        if isinstance(df_15m.columns, pd.MultiIndex):
            df_15m.columns = df_15m.columns.get_level_values(0)

        # 1. Timezone Normalization to IST
        ist = pytz.timezone("Asia/Kolkata")
        if df_15m.index.tzinfo is None:
            df_15m.index = df_15m.index.tz_localize("UTC").tz_convert(ist)
        else:
            df_15m.index = df_15m.index.tz_convert(ist)

        df_15m.dropna(subset=["Close"], inplace=True)

        # 2. Filter Active Market Hours ONLY (09:15 to 15:30 IST + Samvat)
        df_15m = filter_active_market_hours(df_15m)

        if df_15m.empty:
            return pd.DataFrame()

        # Fix missing Volume for Index tickers
        if df_15m["Volume"].sum() == 0 or df_15m["Volume"].isna().all():
            df_15m["Volume"] = (df_15m["High"] - df_15m["Low"]).replace(
                0, 0.01
            )

        # 3. Daily HTF Data for 50 EMA Filter
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

        df_daily["Daily_EMA50"] = ta.trend.EMAIndicator(
            df_daily["Close"], window=50
        ).ema_indicator()
        df_daily["Date"] = df_daily.index.date

        # 4. Calculate Intraday Resetted VWAP
        df_15m["Date"] = df_15m.index.date
        df_15m["Time"] = df_15m.index.time
        df_15m["TypicalPrice"] = (
            df_15m["High"] + df_15m["Low"] + df_15m["Close"]
        ) / 3
        df_15m["TP_Vol"] = df_15m["TypicalPrice"] * df_15m["Volume"]

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

        daily_ema_map = df_daily.set_index("Date")["Daily_EMA50"].to_dict()
        df_15m["Daily_EMA50"] = df_15m["Date"].map(daily_ema_map)

        df_15m.ffill(inplace=True)
        df_15m.bfill(inplace=True)

        return df_15m

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

            if current_time >= pd.to_datetime("15:15").time():
                exit_triggered = True
                raw_exit_price = close
                exit_reason = "EOD Squareoff"

            elif pos_type == "BUY":
                new_sl = high - (current_atr * sl_atr_mult)
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
                new_sl = low + (current_atr * sl_atr_mult)
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
                trailing_sl = entry_price - (row["ATR"] * sl_atr_mult)
                tgt_price = entry_price + (row["ATR"] * tgt_atr_mult)

            elif (
                close < daily_ema
                and close > row["VWAP_Upper"]
                and row["RSI"] > rsi_overbought
            ):
                in_position = True
                pos_type = "SELL"
                entry_price = close * (1 - slippage_pct)
                entry_time = row.name
                trailing_sl = entry_price + (row["ATR"] * sl_atr_mult)
                tgt_price = entry_price - (row["ATR"] * tgt_atr_mult)

    return pd.DataFrame(trades)
    
