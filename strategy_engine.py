from datetime import time
import io
import json
import os
import numpy as np
import pandas as pd
import pytz
import ta
import yfinance as yf

SYMBOL_MAP = {
    "Nifty 50": {"ticker": "^NSEI", "lot_size": 65},
    "Bank Nifty": {"ticker": "^NSEBANK", "lot_size": 15},
    "TCS": {"ticker": "TCS.NS", "lot_size": 175},
    "Infosys (INFY)": {"ticker": "INFY.NS", "lot_size": 400},
    "State Bank of India (SBIN)": {"ticker": "SBIN.NS", "lot_size": 750},
    "HDFC Bank": {"ticker": "HDFCBANK.NS", "lot_size": 550},
    "Reliance Industries": {"ticker": "RELIANCE.NS", "lot_size": 250},
    "ICICI Bank": {"ticker": "ICICIBANK.NS", "lot_size": 700},
}


def fetch_and_prepare_data(
    ticker: str = "^NSEI", period: str = "60d", interval: str = "15m"
) -> pd.DataFrame:
    try:
        if interval in [
            "1m",
            "2m",
            "5m",
            "15m",
            "30m",
            "60m",
        ] and period not in ["1d", "5d", "1mo", "60d"]:
            period = "60d"

        df = yf.download(
            tickers=ticker, period=period, interval=interval, progress=False
        )

        if df.empty:
            return pd.DataFrame()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
        else:
            df.index = df.index.tz_convert("Asia/Kolkata")

        df["Date"] = df.index.date

        df["Typical_Price"] = (df["High"] + df["Low"] + df["Close"]) / 3
        df["VP"] = df["Typical_Price"] * df["Volume"]

        df["Cum_VP"] = df.groupby("Date")["VP"].cumsum()
        df["Cum_Vol"] = df.groupby("Date")["Volume"].cumsum()
        df["VWAP"] = df["Cum_VP"] / df["Cum_Vol"]

        df["VWAP_Std"] = df.groupby("Date")["Typical_Price"].transform("std")
        df["VWAP_Upper"] = df["VWAP"] + (1.5 * df["VWAP_Std"])
        df["VWAP_Lower"] = df["VWAP"] - (1.5 * df["VWAP_Std"])

        df["RSI"] = ta.momentum.RSIIndicator(
            close=df["Close"], window=14
        ).rsi()
        df["ATR"] = ta.volatility.AverageTrueRange(
            high=df["High"], low=df["Low"], close=df["Close"], window=14
        ).average_true_range()
        df["Daily_EMA50"] = ta.trend.EMAIndicator(
            close=df["Close"], window=50
        ).ema_indicator()
        df["ADX"] = ta.trend.ADXIndicator(
            df["High"], df["Low"], df["Close"], window=14
        ).adx()

        return df.dropna()
    except Exception as e:
        print(f"Error fetching data: {e}")
        return pd.DataFrame()


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
    
