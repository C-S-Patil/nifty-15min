import datetime
import pandas as pd
import requests
import streamlit as st
from strategy_engine import fetch_and_prepare_data, run_institutional_backtest

st.set_page_config(
    page_title="Nifty Institutional Quant Engine",
    page_icon="⚡",
    layout="wide",
)

st.title("⚡ Nifty 15-Min Quant Strategy Engine")

# Fetch Telegram Credentials from Streamlit Secrets or Environment
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")

# Fetch Data
data = fetch_and_prepare_data(ticker="^NSEI", period="10d")

if not data.empty:
    # Use the last completed bar (iloc[-2]) for stable signal calculation
    closed_bar = data.iloc[-2] if len(data) > 1 else data.iloc[-1]
    latest_bar = data.iloc[-1]

    trend_state = (
        "BULLISH 🟢"
        if closed_bar["Close"] > closed_bar["Daily_EMA50"]
        else "BEARISH 🔴"
    )

    # Evaluate Trade Conditions
    signal = "HOLD"
    if (
        closed_bar["Close"] > closed_bar["Daily_EMA50"]
        and closed_bar["Close"] < closed_bar["VWAP_Lower"]
        and closed_bar["RSI"] < 42  # Slightly relaxed for live testing
    ):
        signal = "BUY"
    elif (
        closed_bar["Close"] < closed_bar["Daily_EMA50"]
        and closed_bar["Close"] > closed_bar["VWAP_Upper"]
        and closed_bar["RSI"] > 58
    ):
        signal = "SELL"

    # Display Metrics
    st.subheader("📍 Live Market Monitor (Nifty 50)")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Live Price", f"₹{latest_bar['Close']:.2f}")
    c2.metric("VWAP", f"₹{latest_bar['VWAP']:.2f}")
    c3.metric("RSI (14)", f"{latest_bar['RSI']:.1f}")
    c4.metric("Daily Trend", trend_state)
    c5.metric("Current Signal", signal)

    # Trigger Telegram Alert on Signal
    if signal in ["BUY", "SELL"]:
        msg = f"🚨 *NIFTY {signal} ALERT*\nPrice: ₹{latest_bar['Close']:.2f}\nVWAP: ₹{latest_bar['VWAP']:.2f}\nRSI: {latest_bar['RSI']:.1f}"
        st.success(msg)

        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": msg,
                    "parse_mode": "Markdown",
                },
            )

    st.line_chart(
        data[["Close", "VWAP", "VWAP_Upper", "VWAP_Lower"]].tail(100)
    )
else:
    st.error("Failed to load live data.")
    
