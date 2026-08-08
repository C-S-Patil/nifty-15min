import datetime
import os
import pytz
import requests
from strategy_engine import fetch_and_prepare_data

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=5
        )
        print("Telegram response:", response.status_code)
    except Exception as e:
        print(f"Failed to send health check: {e}")


def check_app_health():
    # Set Indian Standard Time (IST)
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.datetime.now(ist)

    current_time = now.strftime("%H:%M:%S")
    current_date = now.strftime("%Y-%m-%d")

    # Perform a live connectivity & data health test
    try:
        df = fetch_and_prepare_data(ticker="^NSEI", period="5d")
        if not df.empty:
            app_status = "ONLINE 🟢 (Data Feed Healthy)"
        else:
            app_status = "WARNING 🟡 (Data Empty)"
    except Exception as e:
        app_status = f"ERROR 🔴 (Fetch Failed: {str(e)[:30]})"

    # Construct status message
    msg = (
        f"🤖 *App Health Status*\n"
        f"📅 Date: {current_date}\n"
        f"⏰ Time: {current_time} IST\n"
        f"⚡ AppStatus: {app_status}"
    )

    send_telegram_message(msg)


if __name__ == "__main__":
    check_app_health()
