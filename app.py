import datetime
import pandas as pd
import requests
import streamlit as st

from execution_engine import execute_live_kite_order, execute_paper_order
from strategy_engine import fetch_and_prepare_data, run_institutional_backtest

st.set_page_config(
    page_title="Nifty Institutional Quant Engine",
    page_icon="⚡",
    layout="wide",
)

st.title("⚡ Nifty 15-Min Quant Strategy & Execution Engine")

# Sidebar Setup
st.sidebar.header("⚙️ Strategy & Execution")
symbol_map = {"Nifty 50": "^NSEI", "Bank Nifty": "^NSEBANK"}
selected_symbol = st.sidebar.selectbox(
    "Select Asset", list(symbol_map.keys())
)
ticker = symbol_map[selected_symbol]

mode = st.sidebar.radio(
    "Mode", ["Backtest & Dashboard 📊", "Paper Trade 📄", "Live Execution 🚀"]
)

rsi_oversold = st.sidebar.slider("RSI Oversold", 25, 45, 38)
rsi_overbought = st.sidebar.slider("RSI Overbought", 55, 75, 62)
sl_atr_mult = st.sidebar.slider("SL ATR Multiplier", 0.5, 3.0, 1.5)
tgt_atr_mult = st.sidebar.slider("TGT ATR Multiplier", 1.5, 5.0, 2.5)

# Fetch Data
data = fetch_and_prepare_data(ticker=ticker, period="60d")

if not data.empty:
    latest = data.iloc[-1]
    trend_state = (
        "BULLISH 🟢"
        if latest["Close"] > latest["Daily_EMA50"]
        else "BEARISH 🔴"
    )

    st.subheader(f"Current Market Status ({selected_symbol})")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Close Price", f"₹{latest['Close']:.2f}")
    c2.metric("VWAP", f"₹{latest['VWAP']:.2f}")
    c3.metric("RSI", f"{latest['RSI']:.1f}")
    c4.metric("Daily Trend", trend_state)

    st.line_chart(
        data[["Close", "VWAP", "VWAP_Upper", "VWAP_Lower"]].tail(100)
    )

    # Backtest Metrics
    trades_df = run_institutional_backtest(
        data,
        rsi_oversold=rsi_oversold,
        rsi_overbought=rsi_overbought,
        sl_atr_mult=sl_atr_mult,
        tgt_atr_mult=tgt_atr_mult,
    )

    st.markdown("---")
    st.subheader("📊 Performance Analytics")
    if not trades_df.empty:
        total_trades = len(trades_df)
        win_rate = (
            len(trades_df[trades_df["NetPnL"] > 0]) / total_trades
        ) * 100
        net_pnl = trades_df["NetPnL"].sum()

        b1, b2, b3 = st.columns(3)
        b1.metric("Total Trades", total_trades)
        b2.metric("Win Rate %", f"{win_rate:.1f}%")
        b3.metric("Net Profit/Loss", f"₹{net_pnl:,.2f}")

        st.dataframe(trades_df, use_container_width=True)