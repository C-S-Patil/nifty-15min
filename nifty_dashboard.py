import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import ta
import datetime
import requests

# -----------------------------
# CONFIGURE YOUR TELEGRAM BOT
# -----------------------------
TELEGRAM_BOT_TOKEN = "8209156550:AAEmxEg-bWapX_7kk4bdwXk0lj-1meISdJA"
TELEGRAM_CHAT_ID = "951733992"

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        st.error(f"Failed to send Telegram message: {e}")

# -----------------------------
# UI SETUP
# -----------------------------
st.set_page_config(page_title="Market Dashboard", layout="wide")
st.title("📈 Nifty 15-Min Dashboard with Signals")

symbol_map = {
    "Nifty": "^NSEI",
    "BankNifty": "^NSEBANK",
    "Reliance": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "Infosys": "INFY.NS"
}

selected_symbol = st.selectbox("Select Instrument", list(symbol_map.keys()))
ticker = symbol_map[selected_symbol]

# -----------------------------
# FETCH DATA
# -----------------------------
@st.cache_data(ttl=300)
def load_data():
    data = yf.download(ticker, interval="15m", period="1d")
    data.dropna(inplace=True)
    data["TypicalPrice"] = (data["High"] + data["Low"] + data["Close"]) / 3
    data["VWAP"] = (data["TypicalPrice"] * data["Volume"]).cumsum() / data["Volume"].cumsum()
    data["RSI"] = ta.momentum.RSIIndicator(data["Close"], window=14).rsi()
    return data

data = load_data()

# -----------------------------
# SIGNAL LOGIC
# -----------------------------
def generate_signal(row):
    if row["Close"][0] > row["VWAP"][0] and row["RSI"][0] < 30:
        return "Buy"
    elif row["Close"][0] < row["VWAP"][0] and row["RSI"][0] > 70:
        return "Sell"
    else:
        return "Hold"

data["Signal"] = data.apply(generate_signal, axis=1)
latest = data.iloc[-1]

# -----------------------------
# DASHBOARD VIEW
# -----------------------------
st.subheader(f"Latest 15-min Candle: {selected_symbol}")
st.write(latest[["Open", "High", "Low", "Close", "VWAP", "RSI", "Signal"]])

# Signal Notification
if latest["Signal"] in ["Buy", "Sell"]:
    msg = f"{selected_symbol} Signal Alert ({datetime.datetime.now().strftime('%H:%M')}): {latest['Signal']}\nPrice: {latest['Close']:.2f}, VWAP: {latest['VWAP']:.2f}, RSI: {latest['RSI']:.2f}"
    st.success(msg)
    send_telegram_message(msg)
else:
    st.info("No signal currently. Holding position.")

# Chart
st.line_chart(data[["Close", "VWAP"]].dropna())

# -----------------------------
# END
# -----------------------------
