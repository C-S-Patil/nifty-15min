import yfinance as yf
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

st.set_page_config(page_title="Nifty VWAP Dashboard", layout="wide")
st.title("📊 Nifty 15-Min VWAP Dashboard with Entry/Exit Signals")

# Download 15-min data from yfinance
ticker = "NIFTYBEES.NS"
data = yf.download(ticker, interval="15m", period="1d", progress=False)

# Ensure columns are present and clean
# required_cols = ['(\'Open\', \'NIFTYBEES.NS\')', '(\'High\', \'NIFTYBEES.NS\')', '(\'Low\', \'NIFTYBEES.NS\')', '(\'Close\', \'NIFTYBEES.NS\')', '(\'Volume\', \'NIFTYBEES.NS\')']
# if not all(col in data.columns for col in required_cols):
#     st.error("⚠️ Missing columns in data. Cannot proceed.")
#     st.stop()

# data.dropna(subset=required_cols, inplace=True)

# ✅ Calculate VWAP using numeric Series only
typical_price = (data['High'] + data['Low']) / 2
vwap = (typical_price * data['Volume']).cumsum() / data['Volume'].cumsum()
data['VWAP'] = vwap

# Initial Balance (9:15–10:15)
ib = data.between_time("09:15", "10:15")
if not ib.empty:
    ib_high = ib['High'].max()
    ib_low = ib['Low'].min()
else:
    ib_high = ib_low = None

# Generate Entry/Exit Signal
def get_signal(row):
    if ib_high[0] and row['Close'][0] > row['VWAP'][0] and row['Close'][0] > ib_high[0]:
        return "Buy"
    elif ib_low[0] and row['Close'][0] < row['VWAP'][0] and row['Close'][0] < ib_low[0]:
        return "Sell"
    return ""

data['Signal'] = data.apply(get_signal, axis=1)

# Display Initial Balance
if ib_high[0] and ib_low[0]:
    st.markdown(f"### 📌 Initial Balance: High = **{ib_high[0]:.2f}**, Low = **{ib_low[0]:.2f}**")
else:
    st.warning("Initial Balance could not be calculated (possibly due to time cutoff).")

# Show latest data
st.subheader("📋 Latest 5 Candles with Signal")
st.dataframe(data[['Open', 'High', 'Low', 'Close', 'VWAP', 'Signal']].tail(5), use_container_width=True)

# Plot VWAP vs Close
st.subheader("📈 Close vs VWAP")
fig, ax = plt.subplots(figsize=(12, 5))
ax.plot(data.index, data['Close'], label='Close', color='blue')
ax.plot(data.index, data['VWAP'], label='VWAP', color='orange', linestyle='--')
ax.set_title("VWAP vs Close (15-min Candle)")
ax.legend()
st.pyplot(fig)
