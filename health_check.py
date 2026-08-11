from datetime import datetime
import os
import pytz
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram_alert(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials not configured. Skipping ping.")
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
            timeout=5,
        )
        return res.status_code == 200
    except Exception as e:
        print(f"❌ Telegram send error: {e}")
        return False


def run_health_check():
    # Convert UTC time to IST
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)

    # 1. Skip Weekends (5 = Saturday, 6 = Sunday)
    if now_ist.weekday() >= 5:
        print(
            f"ℹ️ Today is {now_ist.strftime('%A')} (Market Closed). Skipping health check."
        )
        return

    current_time_str = now_ist.strftime("%H:%M")
    current_hour = now_ist.hour
    current_minute = now_ist.minute

    # 2. Check if current IST time matches 09:00 AM or 12:30 PM (with a 15-min cron window tolerance)
    is_9am_slot = (current_hour == 9) and (0 <= current_minute < 15)
    is_1230pm_slot = (current_hour == 12) and (30 <= current_minute < 45)

    if is_9am_slot or is_1230pm_slot:
        slot_label = (
            "09:00 AM IST (Pre-Market Check)"
            if is_9am_slot
            else "12:30 PM IST (Mid-Day Check)"
        )

        message = (
            f"🟢 *QUANT ENGINE HEALTH CHECK*\n\n"
            f"📅 *Date:* {now_ist.strftime('%Y-%m-%d')}\n"
            f"⏰ *Slot:* {slot_label}\n"
            f"📡 *Status:* System active & ready for signals."
        )
        send_telegram_alert(message)
        print(f"✅ Sent Telegram health check for {slot_label}")
    else:
        print(
            f"ℹ️ Current time {current_time_str} IST is outside scheduled health check slots (09:00 AM & 12:30 PM). Skipping."
        )


if __name__ == "__main__":
    run_health_check()
    
