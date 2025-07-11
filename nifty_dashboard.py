
import yfinance as yf
import pandas as pd
import mplfinance as mpf
import streamlit as st

st.set_page_config(page_title="Nifty VWAP Dashboard", layout="wide")
st.title("📊 Nifty 15-Min Candle Dashboard with VWAP & Entry/Exit Signals")

# Download NIFTYBEES data (as NIFTY proxy)
ticker = "NIFTYBEES.NS"
data = yf.download(ticker, interval="15m", period="1d")

# VWAP Calculation
data['TypicalPrice'] = (data['High'] + data['Low']) / 2
data['VWAP'] = (
    data['TypicalPrice'].astype(float) * data['Volume'].astype(float)
).cumsum() / data['Volume'].astype(float).cumsum()

# Initial Balance (9:15–10:15)
ib = data.between_time("09:15", "10:15")
ib_high, ib_low = ib['High'].max(), ib['Low'].min()
st.subheader(f"📌 Initial Balance: High = {ib_high:.2f}, Low = {ib_low:.2f}")

# Entry/Exit Signals
def get_signal(row):
    if row['Close'] > row['VWAP'] and row['Close'] > ib_high:
        return "Buy"
    elif row['Close'] < row['VWAP'] and row['Close'] < ib_low:
        return "Sell"
    return ""

data['Signal'] = data.apply(get_signal, axis=1)
st.dataframe(data[['Open', 'High', 'Low', 'Close', 'VWAP', 'Signal']].tail(5), use_container_width=True)

# VWAP chart (basic)
st.subheader("📈 Candlestick Chart with VWAP")
st.write("Note: Full interactive chart is under development.")
