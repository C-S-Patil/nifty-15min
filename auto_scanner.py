import os
from datetime import time

import requests

from strategy_engine import (
    SYMBOL_MAP,
    fetch_and_prepare_data,
)


TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
)


RSI_OVERSOLD = 38
RSI_OVERBOUGHT = 62
ADX_MAX = 32


def send_telegram_alert(message: str) -> bool:

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )

        if response.status_code != 200:
            print(
                f"❌ Telegram HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
            return False

        return True

    except requests.RequestException as exc:
        print(f"❌ Telegram Exception: {exc}")
        return False


def run_scanner():

    ticker = SYMBOL_MAP["Nifty 50"]["ticker"]
    lot_size = SYMBOL_MAP["Nifty 50"]["lot_size"]

    print(
        f"📡 Scanner starting for {ticker}"
    )

    # ONE provider call.
    # fetch_and_prepare_data() now owns the fallback logic.
    df = fetch_and_prepare_data(
        ticker=ticker,
        period="1mo",
        interval="15m",
    )

    if df.empty:
        print(
            "🛑 MARKET DATA UNAVAILABLE. "
            "No signal will be generated."
        )
        return

    if len(df) < 3:
        print(
            "🛑 Insufficient market data. "
            "No signal will be generated."
        )
        return

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    required_columns = [
        "Open",
        "Close",
        "VWAP_Lower",
        "VWAP_Upper",
        "RSI",
        "ADX",
    ]

    missing = [
        col
        for col in required_columns
        if col not in df.columns
    ]

    if missing:
        print(
            f"🛑 Missing strategy columns: {missing}"
        )
        return

    current_time = latest.name.time()

    # Never generate an entry after the entry cutoff.
    if current_time >= time(14, 45):
        print(
            f"⏱️ Entry window closed: "
            f"{latest.name.strftime('%H:%M IST')}"
        )
        return

    # Exact established strategy.
    buy_signal = (
        previous["Close"]
        < previous["VWAP_Lower"]
        and latest["Close"]
        > latest["Open"]
        and latest["RSI"]
        < RSI_OVERSOLD
        and latest["ADX"]
        < ADX_MAX
    )

    sell_signal = (
        previous["Close"]
        > previous["VWAP_Upper"]
        and latest["Close"]
        < latest["Open"]
        and latest["RSI"]
        > RSI_OVERBOUGHT
        and latest["ADX"]
        < ADX_MAX
    )

    signal = None

    if buy_signal:
        signal = "BUY"

    elif sell_signal:
        signal = "SELL"

    timestamp = latest.name.strftime(
        "%Y-%m-%d %H:%M IST"
    )

    if not signal:
        print(
            f"⚪ HOLD | {timestamp} | "
            f"RSI={latest['RSI']:.2f} "
            f"ADX={latest['ADX']:.2f}"
        )
        return

    total_qty = lot_size

    alert_msg = (
        f"🚨 *AUTOMATED NIFTY "
        f"{signal} SIGNAL* ⚡\n\n"
        f"📌 *Asset:* Nifty 50\n"
        f"🕐 *Candle:* {timestamp}\n"
        f"📈 *Price:* ₹{latest['Close']:.2f}\n"
        f"📦 *Quantity:* {total_qty}\n"
        f"📊 *RSI:* {latest['RSI']:.2f}\n"
        f"📉 *ADX:* {latest['ADX']:.2f}\n"
        f"💡 *Reason:* VWAP reversal + "
        f"RSI + ADX confirmation"
    )

    if send_telegram_alert(alert_msg):
        print(
            f"✅ Automated {signal} alert dispatched."
        )
    else:
        print(
            f"⚠️ {signal} detected but Telegram "
            f"delivery failed."
        )


if __name__ == "__main__":
    run_scanner()
