import io
from datetime import time
import numpy as np
import pandas as pd
import pytz
import requests
import ta
import yfinance as yf

# Map symbols to Yahoo Finance Tickers and default F&O Lot Sizes
SYMBOL_MAP = {
    "Nifty 50": {"ticker": "^NSEI", "lot_size": 75},
    "Bank Nifty": {"ticker": "^NSEBANK", "lot_size": 15},
    "TCS": {"ticker": "TCS.NS", "lot_size": 175},
    "Infosys (INFY)": {"ticker": "INFY.NS", "lot_size": 400},
    "State Bank of India (SBIN)": {"ticker": "SBIN.NS", "lot_size": 750},
    "HDFC Bank": {"ticker": "HDFCBANK.NS", "lot_size": 550},
    "Reliance Industries": {"ticker": "RELIANCE.NS", "lot_size": 250},
    "ICICI Bank": {"ticker": "ICICIBANK.NS", "lot_size": 700},
}


def export_trades_to_excel(trades_df: pd.DataFrame) -> bytes:
    """Converts the trades DataFrame into an Excel file bytes stream for Streamlit download."""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        trades_df.to_excel(writer, index=False, sheet_name="Executed Trades")
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
    
