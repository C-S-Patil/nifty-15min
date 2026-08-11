from datetime import time
import numpy as np
import pandas as pd
import pytz
import requests
import ta
import yfinance as yf


def filter_active_market_hours(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    times = df.index.time
    standard_start = time(9, 15)
    standard_end = time(15, 30)
    muhurat_start = time(18, 0)
    muhurat_end = time(19, 30)

    standard_mask = (times >= standard_start) & (times <= standard_end)
    muhurat_mask = (times >= muhurat_start) & (times <= muhurat_end)

    filtered_df = df[standard_mask | muhurat_mask].copy()
    return filtered_df if not filtered_df.empty else df


def fetch_and_prepare_data(
    ticker: str = "^NSEI", interval: str = "15m", period: str = "1y"
) -> pd.DataFrame:
    try:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )

        dat = yf.Ticker(ticker, session=session)
        df_15m = dat.history(interval=interval, period=period)

        if df_15m.empty:
            df_15m = yf.download(
                ticker,
                interval=interval,
                period="60d",
                progress=False,
                session=session,
            )

        if df_15m.empty:
            return pd.DataFrame()

        if isinstance(df_15m.columns, pd.MultiIndex):
            df_15m.columns = df_15m.columns.get_level_values(0)

        ist = pytz.timezone("Asia/Kolkata")
        if df_15m.index.tzinfo is None:
            df_15m.index = df_15m.index.tz_localize("UTC").tz_convert(ist)
        else:
            df_15m.index = df_15m.index.tz_convert(ist)

        df_15m.dropna(subset=["Close"], inplace=True)
        df_15m = filter_active_market_hours(df_15m)

        if df_15m["Volume"].sum() == 0 or df_15m["Volume"].isna().all():
            df_15m["Volume"] = (df_15m["High"] - df_15m["Low"]).replace(
                0, 0.01
            )

        # Daily HTF Data
        df_daily = dat.history(interval="1d", period="2y")
        if df_daily.empty:
            df_daily = yf.download(
                ticker,
                interval="1d",
                period="2y",
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

        # Intraday VWAP & Indicators
        df_15m["Date"] = df_15m.index.date
        df_15m["Time"] = df_15m.index.time
        df_15m["TypicalPrice"] = (
            df_15m["High"] + df_15m["Low"] + df_15m["Close"]
        ) / 3
        df_15m["TP_Vol"] = df_15m["TypicalPrice"] * df_15m["Volume"]

        df_15m["Cum_TP_Vol"] = df_15m.groupby("Date")["TP_Vol"].cumsum()
        df_15m["Cum_Vol"] = df_15m.groupby("Date")["Volume"].cumsum()
        df_15m["VWAP"] = df_15m["Cum_TP_Vol"] / df_15m["Cum_Vol"]

        df_15m["RSI"] = ta.momentum.RSIIndicator(
            df_15m["Close"], window=14
        ).rsi()
        df_15m["ATR"] = ta.volatility.AverageTrueRange(
            df_15m["High"], df_15m["Low"], df_15m["Close"], window=14
        ).average_true_range()

        df_15m["VWAP_Upper"] = df_15m["VWAP"] + (df_15m["ATR"] * 1.5)
        df_15m["VWAP_Lower"] = df_15m["VWAP"] - (df_15m["ATR"] * 1.5)

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
    sl_atr_mult: float = 2.5,  # Widened SL buffer to prevent premature stops
    tgt_atr_mult: float = 3.5,
    num_lots: int = 1,
    lot_size: int = 75,
    charges_per_trade: float = 60.0,  # ~ ₹60 Realistic roundtrip charges per lot
) -> pd.DataFrame:
    trades = []
    in_position = False
    pos_type = None
    entry_price = 0.0
    initial_sl = 0.0
    trailing_sl = 0.0
    tgt_price = 0.0
    entry_time = None
    daily_trade_count = {}

    total_qty = num_lots * lot_size
    total_charges = charges_per_trade * num_lots

    for i in range(1, len(df)):
        row = df.iloc[i]
        current_date = row["Date"]
        current_time = row.name.time()

        # Overtrading Control: Max 2 trades per day
        trades_today = daily_trade_count.get(current_date, 0)

        if in_position:
            high, low, close = row["High"], row["Low"], row["Close"]
            current_atr = row["ATR"] if not np.isnan(row["ATR"]) else 15.0

            exit_triggered = False
            exit_price = 0.0
            exit_reason = ""

            # Force EOD Intraday Exit
            if current_time >= time(15, 15):
                exit_triggered = True
                exit_price = close
                exit_reason = "EOD Squareoff"

            elif pos_type == "BUY":
                # Trailing SL triggers ONLY after price moves 1.5x ATR in profit
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

        # Entry Logic (Filtered for Max 2 Trades/Day & ATR > 10)
        if (
            not in_position
            and current_time < time(14, 45)
            and trades_today < 2
            and row["ATR"] >= 10.0
        ):
            close = row["Close"]

            if close < row["VWAP_Lower"] and row["RSI"] < rsi_oversold:
                in_position = True
                pos_type = "BUY"
                entry_price = close
                entry_time = row.name
                initial_sl = entry_price - (row["ATR"] * sl_atr_mult)
                trailing_sl = initial_sl
                tgt_price = entry_price + (row["ATR"] * tgt_atr_mult)
                daily_trade_count[current_date] = trades_today + 1

            elif close > row["VWAP_Upper"] and row["RSI"] > rsi_overbought:
                in_position = True
                pos_type = "SELL"
                entry_price = close
                entry_time = row.name
                initial_sl = entry_price + (row["ATR"] * sl_atr_mult)
                trailing_sl = initial_sl
                tgt_price = entry_price - (row["ATR"] * tgt_atr_mult)
                daily_trade_count[current_date] = trades_today + 1

    return pd.DataFrame(trades)


def generate_12m_performance_summary(
    trades_df: pd.DataFrame, capital: float
) -> pd.DataFrame:
    """Generates comprehensive 12-Month Performance Analytics Summary."""
    if trades_df.empty:
        return pd.DataFrame()

    total_trades = len(trades_df)
    profitable_trades = len(trades_df[trades_df["NetPnL"] > 0])
    loss_trades = len(trades_df[trades_df["NetPnL"] <= 0])
    win_rate = (
        (profitable_trades / total_trades) * 100 if total_trades > 0 else 0.0
    )

    highest_profit = trades_df["NetPnL"].max()
    highest_loss = trades_df["NetPnL"].min()

    total_gross_pnl = trades_df["GrossPnL"].sum()
    total_charges = trades_df["Charges"].sum()
    total_net_pnl = trades_df["NetPnL"].sum()

    roi_pct = (total_net_pnl / capital) * 100

    summary = {
        "Total Trades": [total_trades],
        "Profitable Trades 🟢": [profitable_trades],
        "Loss Trades 🔴": [loss_trades],
        "Win Rate %": [f"{win_rate:.1f}%"],
        "Highest Profit": [f"₹{highest_profit:,.2f}"],
        "Highest Loss": [f"₹{highest_loss:,.2f}"],
        "Total Charges": [f"₹{total_charges:,.2f}"],
        "Gross PnL": [f"₹{total_gross_pnl:,.2f}"],
        "Net PnL": [f"₹{total_net_pnl:,.2f}"],
        "ROI % on Capital": [f"{roi_pct:+.2f}%"],
    }

    return pd.DataFrame(summary)
