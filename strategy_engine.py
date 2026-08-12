import io
from datetime import time
import numpy as np
import pandas as pd
import pytz
import requests
import ta
import yfinance as yf
import json
import os

SYMBOL_MAP = {
    "Nifty 50": {"ticker": "^NSEI", "lot_size": 65},  # Updated to 65
    "Bank Nifty": {"ticker": "^NSEBANK", "lot_size": 15},
    "TCS": {"ticker": "TCS.NS", "lot_size": 175},
    "Infosys (INFY)": {"ticker": "INFY.NS", "lot_size": 400},
    "State Bank of India (SBIN)": {"ticker": "SBIN.NS", "lot_size": 750},
    "HDFC Bank": {"ticker": "HDFCBANK.NS", "lot_size": 550},
    "Reliance Industries": {"ticker": "RELIANCE.NS", "lot_size": 250},
    "ICICI Bank": {"ticker": "ICICIBANK.NS", "lot_size": 700},
}


def load_historical_trades(
    file_path: str = "data/historical_trades.json",
) -> pd.DataFrame:
    """Loads historical trade logs saved in the local repository."""
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


def save_trade_to_history(
    trade_dict: dict, file_path: str = "data/historical_trades.json"
):
    """Appends a newly executed trade to the local repository JSON file."""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    existing_trades = []

    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                existing_trades = json.load(f)
        except Exception:
            existing_trades = []

    # Ensure timestamps are string formatted for JSON serialization
    trade_copy = trade_dict.copy()
    if isinstance(trade_copy.get("EntryTime"), pd.Timestamp):
        trade_copy["EntryTime"] = trade_copy["EntryTime"].strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    if isinstance(trade_copy.get("ExitTime"), pd.Timestamp):
        trade_copy["ExitTime"] = trade_copy["ExitTime"].strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    existing_trades.append(trade_copy)

    with open(file_path, "w") as f:
        json.dump(existing_trades, f, indent=2)


def get_combined_12m_trades(
    live_60d_trades: pd.DataFrame,
    file_path: str = "data/historical_trades.json",
) -> pd.DataFrame:
    """Merges saved repo trades with recent 60-day live backtest trades, removing duplicates."""
    saved_df = load_historical_trades(file_path)

    if saved_df.empty:
        return live_60d_trades

    if live_60d_trades.empty:
        return saved_df

    # Combine both DataFrames
    combined = pd.concat([saved_df, live_60d_trades], ignore_index=True)

    # Remove duplicate trades based on EntryTime and Type
    combined["EntryStr"] = combined["EntryTime"].astype(str)
    combined = combined.drop_duplicates(
        subset=["EntryStr", "Type"]
    ).drop(columns=["EntryStr"])

    # Sort chronologically
    combined = combined.sort_values(by="ExitTime").reset_index(drop=True)
    return combined
    

def run_institutional_backtest(
    df: pd.DataFrame,
    rsi_oversold: int = 38,
    rsi_overbought: int = 62,
    sl_atr_mult: float = 2.5,
    tgt_atr_mult: float = 3.5,
    num_lots: int = 1,
    lot_size: int = 65,  # Updated default lot size
    charges_per_trade: float = 60.0,
) -> pd.DataFrame:
    # Existing backtest calculation logic...
    pass


def export_trades_to_excel(trades_df: pd.DataFrame) -> bytes:
    """Converts the trades DataFrame into an Excel file bytes stream, removing timezone information to prevent Excel ValueError."""
    if trades_df.empty:
        return b""

    df_export = trades_df.copy()

    # Convert or strip timezones from datetime columns
    for col in df_export.select_dtypes(
        include=["datetime64[ns, Asia/Kolkata]", "datetime64[ns, UTC]", "datetimetz"]
    ).columns:
        df_export[col] = df_export[col].dt.tz_localize(None)

    # Also handle string timestamps if they contain timezone offsets
    if "EntryTime" in df_export.columns:
        df_export["EntryTime"] = (
            pd.to_datetime(df_export["EntryTime"])
            .dt.tz_localize(None)
            .dt.strftime("%Y-%m-%d %H:%M:%S")
        )

    if "ExitTime" in df_export.columns:
        df_export["ExitTime"] = (
            pd.to_datetime(df_export["ExitTime"])
            .dt.tz_localize(None)
            .dt.strftime("%Y-%m-%d %H:%M:%S")
        )

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df_export.to_excel(writer, index=False, sheet_name="Executed Trades")

    output.seek(0)
    return output.getvalue()
    


def filter_active_market_hours(df: pd.DataFrame) -> pd.DataFrame:
    """Filters data to retain only active trading sessions (09:15 - 15:30 IST + Samvat)."""
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
    ticker: str = "^NSEI", period: str = "60d", interval: str = "15m"
) -> pd.DataFrame:
    """Fetches historical market data from Yahoo Finance and calculates VWAP Envelopes, RSI, and ATR.

    Note: Intraday intervals (15m) are limited to a maximum period of 60 days by Yahoo Finance.
    """
    try:
        # Enforce max 60d period for intraday data to avoid Yahoo Finance API errors
        if interval in ["1m", "2m", "5m", "15m", "30m", "60m"] and period not in [
            "1d",
            "5d",
            "1mo",
            "60d",
        ]:
            period = "60d"

        df = yf.download(
            tickers=ticker, period=period, interval=interval, progress=False
        )

        if df.empty:
            return pd.DataFrame()

        # Handle MultiIndex columns if returned by yfinance
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Convert index to Asia/Kolkata timezone
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
        else:
            df.index = df.index.tz_convert("Asia/Kolkata")

        df["Date"] = df.index.date

        # Calculate Indicators
        # 1. VWAP
        df["Typical_Price"] = (df["High"] + df["Low"] + df["Close"]) / 3
        df["VP"] = df["Typical_Price"] * df["Volume"]

        # Cumulative sums reset per day
        df["Cum_VP"] = df.groupby("Date")["VP"].cumsum()
        df["Cum_Vol"] = df.groupby("Date")["Volume"].cumsum()
        df["VWAP"] = df["Cum_VP"] / df["Cum_Vol"]

        # Standard Deviation for VWAP Bands
        df["VWAP_Std"] = df.groupby("Date")["Typical_Price"].transform("std")
        df["VWAP_Upper"] = df["VWAP"] + (1.5 * df["VWAP_Std"])
        df["VWAP_Lower"] = df["VWAP"] - (1.5 * df["VWAP_Std"])

        # 2. RSI (14)
        df["RSI"] = ta.momentum.RSIIndicator(
            close=df["Close"], window=14
        ).rsi()

        # 3. ATR (14)
        df["ATR"] = ta.volatility.AverageTrueRange(
            high=df["High"], low=df["Low"], close=df["Close"], window=14
        ).average_true_range()

        # 4. Daily EMA 50 for Trend Filter
        df["Daily_EMA50"] = ta.trend.EMAIndicator(
            close=df["Close"], window=50
        ).ema_indicator()

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
    lot_size: int = 75,
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

    # Add ADX Filter to Strategy Data
    df["ADX"] = ta.trend.ADXIndicator(
        df["High"], df["Low"], df["Close"], window=14
    ).adx()

    for i in range(2, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]
        current_date = row["Date"]
        current_time = row.name.time()

        trades_today = daily_trade_count.get(current_date, 0)

        # -----------------------------------------------------------
        # 1. POSITION MANAGEMENT & EXITS
        # -----------------------------------------------------------
        if in_position:
            high, low, close = row["High"], row["Low"], row["Close"]
            current_atr = row["ATR"] if not np.isnan(row["ATR"]) else 15.0

            exit_triggered = False
            exit_price = 0.0
            exit_reason = ""

            # Force EOD Intraday Exit at 15:15 IST
            if current_time >= time(15, 15):
                exit_triggered = True
                exit_price = close
                exit_reason = "EOD Squareoff"

            elif pos_type == "BUY":
                # Ratchet Trailing SL only after 1.5x ATR Profit Move
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

        # -----------------------------------------------------------
        # 2. ENTRY LOGIC WITH REVERSAL CONFIRMATION (REDUCES FALSE POSITIVES)
        # -----------------------------------------------------------
        if (
            not in_position
            and current_time < time(14, 45)
            and trades_today < 2
            and row["ATR"] >= 10.0
        ):
            close = row["Close"]
            adx = row["ADX"]

            # BUY ENTRY FILTER:
            # 1. Price was below VWAP Lower on previous candle
            # 2. Current candle is a GREEN reversal bar (Close > Open)
            # 3. RSI < Oversold & ADX < 32 (Avoid trading strong downward trends)
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

            # SELL ENTRY FILTER:
            # 1. Price was above VWAP Upper on previous candle
            # 2. Current candle is a RED reversal bar (Close < Open)
            # 3. RSI > Overbought & ADX < 32
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
    """Groups historical trades by Year-Month to generate a 12-row monthly performance table."""
    if trades_df.empty:
        return pd.DataFrame()

    trades_df = trades_df.copy()
    trades_df["YearMonth"] = pd.to_datetime(trades_df["ExitTime"]).dt.to_period(
        "M"
    )

    monthly_records = []
    all_months = pd.period_range(
        end=pd.Timestamp.now().to_period("M"), periods=12, freq="M"
    )

    for ym in reversed(all_months):
        m_trades = trades_df[trades_df["YearMonth"] == ym]

        month_label = ym.strftime("%b %Y")
        total_trades = len(m_trades)

        if total_trades > 0:
            gross_pnl = m_trades["GrossPnL"].sum()
            charges = m_trades["Charges"].sum()
            net_pnl = m_trades["NetPnL"].sum()
            profit_pct = (net_pnl / capital) * 100
            win_trades = len(m_trades[m_trades["NetPnL"] > 0])
            win_rate = (win_trades / total_trades) * 100
        else:
            gross_pnl = 0.0
            charges = 0.0
            net_pnl = 0.0
            profit_pct = 0.0
            win_rate = 0.0

        monthly_records.append(
            {
                "Month": month_label,
                "Total Trades": total_trades,
                "Win Rate %": f"{win_rate:.1f}%",
                "Gross PnL (₹)": f"₹{gross_pnl:,.2f}",
                "Charges (₹)": f"₹{charges:,.2f}",
                "Net PnL (₹)": f"₹{net_pnl:,.2f}",
                "Actual Profit %": f"{profit_pct:+.2f}%",
            }
        )

    return pd.DataFrame(monthly_records)
    
