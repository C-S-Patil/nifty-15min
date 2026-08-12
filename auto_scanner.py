import os
import requests
from strategy_engine import SYMBOL_MAP, fetch_and_prepare_data

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram_alert(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        res = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        return res.status_code == 200
    except Exception as e:
        print(f"❌ Telegram Exception: {e}")
        return False


def run_scanner():
    ticker = SYMBOL_MAP["Nifty 50"]["ticker"]
    lot_size = SYMBOL_MAP["Nifty 50"]["lot_size"]

    # Fetch latest 60d 15m data
    df = fetch_and_prepare_data(ticker=ticker, period="60d", interval="15m")
    if df.empty:
        print("❌ Could not fetch market data.")
        return

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    # Evaluate Entry Conditions
    rsi_oversold = 38
    rsi_overbought = 62
    signal = None

    if (
        prev["Close"] < prev["VWAP_Lower"]
        and latest["Close"] > latest["Open"]
        and latest["RSI"] < rsi_oversold
    ):
        signal = "BUY"
    elif (
        prev["Close"] > prev["VWAP_Upper"]
        and latest["Close"] < latest["Open"]
        and latest["RSI"] > rsi_overbought
    ):
        signal = "SELL"

    if signal:
        total_qty = 1 * lot_size
        alert_msg = (
            f"🚨 *AUTOMATED NIFTY {signal} SIGNAL DETECTED* ⚡\n\n"
            f"📌 *Asset:* Nifty 50\n"
            f"📈 *Signal Price:* ₹{latest['Close']:.2f}\n"
            f"📦 *Quantity:* {total_qty} (1 Lot @ {lot_size}/lot)\n"
            f"💡 *Trigger Reason:* RSI + VWAP Reversal Candle Close"
        )
        send_telegram_alert(alert_msg)
        print(f"✅ Automated alert dispatched for {signal}!")
    else:
        print("⚪ No active signal detected on current candle.")


if __name__ == "__main__":
    run_scanner()

