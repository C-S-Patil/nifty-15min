import os
import requests
from strategy_engine import fetch_and_prepare_data

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(
        url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5
    )


def run_scan():
    data = fetch_and_prepare_data(ticker="^NSEI", period="10d")
    if data.empty:
        return

    latest = data.iloc[-1]
    if (
        latest["Close"] > latest["Daily_EMA50"]
        and latest["Close"] < latest["VWAP_Lower"]
        and latest["RSI"] < 38
    ):
        send_telegram(
            f"🚨 NIFTY BUY ALERT @ ₹{latest['Close']:.2f} | RSI: {latest['RSI']:.1f}"
        )
    elif (
        latest["Close"] < latest["Daily_EMA50"]
        and latest["Close"] > latest["VWAP_Upper"]
        and latest["RSI"] > 62
    ):
        send_telegram(
            f"🚨 NIFTY SELL ALERT @ ₹{latest['Close']:.2f} | RSI: {latest['RSI']:.1f}"
        )


if __name__ == "__main__":
    run_scan()