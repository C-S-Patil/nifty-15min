import datetime
import pandas as pd
import requests
import streamlit as st
from strategy_engine import fetch_and_prepare_data, run_institutional_backtest

# Page Config
st.set_page_config(
    page_title="Nifty Institutional Quant Engine",
    page_icon="⚡",
    layout="wide",
)

# Secrets / Telegram Setup
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=5
        )
    except Exception as e:
        st.error(f"Telegram Alert Failed: {e}")


# Header
st.title("⚡ Nifty 15-Min Multi-Timeframe Strategy Engine")
st.markdown(
    "**Strategy Mechanics:** Daily 50 EMA HTF Trend Filter + Dynamic Intraday VWAP Envelopes + RSI Extremes + ATR Trailing SL."
)

# Sidebar Options
st.sidebar.header("🕹️ Strategy Parameters")
symbol_map = {
    "Nifty 50": "^NSEI",
    "Bank Nifty": "^NSEBANK",
    "Reliance": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "Infosys": "INFY.NS",
}

selected_symbol = st.sidebar.selectbox(
    "Select Asset", list(symbol_map.keys())
)
ticker = symbol_map[selected_symbol]
period = st.sidebar.selectbox("Historical Window", ["30d", "60d"], index=1)

rsi_oversold = st.sidebar.slider("RSI Buy Level (Oversold)", 25, 45, 38)
rsi_overbought = st.sidebar.slider("RSI Sell Level (Overbought)", 55, 75, 62)

sl_atr_mult = st.sidebar.slider(
    "Trailing SL (ATR Multiplier)", 0.5, 3.0, 1.5, step=0.1
)
tgt_atr_mult = st.sidebar.slider(
    "Target (ATR Multiplier)", 1.5, 5.0, 2.5, step=0.1
)

st.sidebar.header("💸 Risk & Execution Friction")
initial_capital = st.sidebar.number_input(
    "Capital (₹)", value=100000, step=10000
)
lot_size = st.sidebar.number_input("Lot Size (Qty)", value=75, step=25)
slippage_pct = (
    st.sidebar.slider("Slippage (%)", 0.0, 0.2, 0.05, step=0.01) / 100
)

# Fetch Data
data = fetch_and_prepare_data(ticker=ticker, period=period)

if data.empty:
    st.error("Failed to fetch market data. Please verify connectivity.")
    st.stop()

# Live Signals Analysis
latest = data.iloc[-1]
trend_state = (
    "BULLISH 🟢" if latest["Close"] > latest["Daily_EMA50"] else "BEARISH 🔴"
)

latest_signal = "HOLD"
if (
    latest["Close"] > latest["Daily_EMA50"]
    and latest["Close"] < latest["VWAP_Lower"]
    and latest["RSI"] < rsi_oversold
):
    latest_signal = "BUY"
elif (
    latest["Close"] < latest["Daily_EMA50"]
    and latest["Close"] > latest["VWAP_Upper"]
    and latest["RSI"] > rsi_overbought
):
    latest_signal = "SELL"

# Display Live Dashboard
st.subheader(f"📍 Live Market Monitor: {selected_symbol}")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Close Price", f"₹{latest['Close']:.2f}")
c2.metric("VWAP", f"₹{latest['VWAP']:.2f}")
c3.metric("RSI (14)", f"{latest['RSI']:.1f}")
c4.metric("Daily HTF Trend", trend_state)
c5.metric("Signal", latest_signal)

if latest_signal in ["BUY", "SELL"]:
    alert_msg = (
        f"🚨 {selected_symbol} SIGNAL ALERT ({datetime.datetime.now().strftime('%H:%M')}):\n"
        f"Action: {latest_signal} @ ₹{latest['Close']:.2f}\n"
        f"HTF Trend: {trend_state} | VWAP: {latest['VWAP']:.2f} | RSI: {latest['RSI']:.1f}"
    )
    st.success(alert_msg)
    send_telegram_alert(alert_msg)

# Chart Visualization
st.subheader("Price vs Dynamic Intraday VWAP Bands")
st.line_chart(
    data[["Close", "VWAP", "VWAP_Upper", "VWAP_Lower"]].tail(120)
)

# Run Backtest
trades_df = run_institutional_backtest(
    data,
    rsi_oversold=rsi_oversold,
    rsi_overbought=rsi_overbought,
    sl_atr_mult=sl_atr_mult,
    tgt_atr_mult=tgt_atr_mult,
    lot_size=lot_size,
    capital=initial_capital,
    slippage_pct=slippage_pct,
)

# Analytics Summary
st.markdown("---")
st.subheader("📊 Backtest Analytics Report")

if not trades_df.empty:
    total_trades = len(trades_df)
    winning_trades = len(trades_df[trades_df["NetPnL"] > 0])
    win_rate = (winning_trades / total_trades) * 100
    net_pnl = trades_df["NetPnL"].sum()
    total_charges = trades_df["Charges"].sum()

    gross_profit = trades_df[trades_df["NetPnL"] > 0]["NetPnL"].sum()
    gross_loss = abs(trades_df[trades_df["NetPnL"] < 0]["NetPnL"].sum())
    profit_factor = (
        gross_profit / gross_loss if gross_loss > 0 else gross_profit
    )

    trades_df["CumulativePnL"] = trades_df["NetPnL"].cumsum()
    trades_df["Equity"] = initial_capital + trades_df["CumulativePnL"]
    peak = trades_df["Equity"].cummax()
    max_dd = ((trades_df["Equity"] - peak) / peak).min() * 100

    b1, b2, b3, b4, b5 = st.columns(5)
    b1.metric("Total Trades", total_trades)
    b2.metric("Win Rate %", f"{win_rate:.1f}%")
    b3.metric(
        "Net Return",
        f"₹{net_pnl:,.2f}",
        delta=f"{((net_pnl/initial_capital)*100):.2f}%",
    )
    b4.metric("Profit Factor", f"{profit_factor:.2f}")
    b5.metric("Max Drawdown", f"{max_dd:.2f}%")

    st.subheader("Equity Growth Curve")
    st.line_chart(trades_df.set_index("ExitTime")["Equity"])

    st.subheader("Detailed Trade Logs")
    st.dataframe(trades_df, use_container_width=True)
else:
    st.info(
        "No trades triggered within the selected parameter range. Adjust parameters in the sidebar to run backtest."
    )
