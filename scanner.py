import datetime
import json
import os
import pytz
import requests
from strategy_engine import fetch_and_prepare_data

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "active_position.json"


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram tokens missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        print(f"Failed to send alert: {e}")


def load_position():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try:
                return json.load(f)
            except Exception:
                return None
    return None


def save_position(pos_dict):
    with open(STATE_FILE, "w") as f:
        json.dump(pos_dict, f, indent=4)


def clear_position():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


def run_scan():
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.datetime.now(ist)
    current_time_str = now.strftime("%H:%M:%S")

    # Fetch latest 15m candle data for Nifty
    data = fetch_and_prepare_data(ticker="^NSEI", period="5d")
    if data.empty:
        print("No data fetched.")
        return

    latest = data.iloc[-1]
    close = float(latest["Close"])
    high = float(latest["High"])
    low = float(latest["Low"])
    vwap_upper = float(latest["VWAP_Upper"])
    vwap_lower = float(latest["VWAP_Lower"])
    rsi = float(latest["RSI"])
    atr = float(latest["ATR"])
    daily_ema = float(latest["Daily_EMA50"])

    active_pos = load_position()

    # -------------------------------------------------------------
    # 1. CHECK EXIT CONDITIONS ON ACTIVE POSITION
    # -------------------------------------------------------------
    if active_pos:
        pos_type = active_pos["type"]
        entry_price = active_pos["entry_price"]
        trailing_sl = active_pos["sl_price"]
        target_price = active_pos["tgt_price"]

        exit_triggered = False
        exit_price = 0.0
        exit_reason = ""

        # EOD Square-off at 15:15 IST
        if now.time() >= datetime.time(15, 15):
            exit_triggered = True
            exit_price = close
            exit_reason = "⏰ EOD Square-off (3:15 PM)"

        elif pos_type == "BUY":
            # Ratchet Trailing SL
            new_sl = high - (atr * 1.5)
            if new_sl > trailing_sl:
                active_pos["sl_price"] = new_sl
                save_position(active_pos)

            if low <= trailing_sl:
                exit_triggered = True
                exit_price = trailing_sl
                exit_reason = "🔴 Trailing Stop-Loss Hit"
            elif high >= target_price:
                exit_triggered = True
                exit_price = target_price
                exit_reason = "🟢 Target Price Reached"

        elif pos_type == "SELL":
            # Ratchet Trailing SL
            new_sl = low + (atr * 1.5)
            if new_sl < trailing_sl:
                active_pos["sl_price"] = new_sl
                save_position(active_pos)

            if high >= trailing_sl:
                exit_triggered = True
                exit_price = trailing_sl
                exit_reason = "🔴 Trailing Stop-Loss Hit"
            elif low <= target_price:
                exit_triggered = True
                exit_price = target_price
                exit_reason = "🟢 Target Price Reached"

        if exit_triggered:
            pnl_points = (
                (exit_price - entry_price)
                if pos_type == "BUY"
                else (entry_price - exit_price)
            )
            pnl_amount = pnl_points * 75  # 1 Lot Nifty (75 Qty)

            pnl_emoji = "✅" if pnl_amount >= 0 else "❌"

            msg = (
                f"🚨 *NIFTY EXIT ALERT* {pnl_emoji}\n\n"
                f"📌 *Type:* {pos_type} Position Closed\n"
                f"💡 *Reason:* {exit_reason}\n"
                f"📈 *Entry Price:* ₹{entry_price:.2f}\n"
                f"📉 *Exit Price:* ₹{exit_price:.2f}\n"
                f"💰 *Estimated Net PnL:* ₹{pnl_amount:,.2f} ({pnl_points:+.2f} pts)\n"
                f"⏰ *Time:* {current_time_str} IST"
            )

            send_telegram(msg)
            clear_position()
            return

    # -------------------------------------------------------------
    # 2. CHECK ENTRY CONDITIONS (IF NO POSITION ACTIVE)
    # -------------------------------------------------------------
    if not active_pos and now.time() < datetime.time(14, 45):
        # BUY ENTRY: Price below VWAP Lower + RSI < 38 + Above Daily EMA
        if close > daily_ema and close < vwap_lower and rsi < 38:
            sl_price = close - (atr * 1.5)
            tgt_price = close + (atr * 2.5)

            new_pos = {
                "type": "BUY",
                "entry_price": close,
                "sl_price": sl_price,
                "tgt_price": tgt_price,
                "entry_time": current_time_str,
            }
            save_position(new_pos)

            msg = (
                f"🚨 *NIFTY BUY ENTRY ALERT* 🟢\n\n"
                f"📈 *Entry Price:* ₹{close:.2f}\n"
                f"🛡️ *Initial SL:* ₹{sl_price:.2f}\n"
                f"🎯 *Target:* ₹{tgt_price:.2f}\n"
                f"📊 *VWAP Lower:* ₹{vwap_lower:.2f} | *RSI:* {rsi:.1f}\n"
                f"⏰ *Time:* {current_time_str} IST"
            )
            send_telegram(msg)

        # SELL ENTRY: Price above VWAP Upper + RSI > 62 + Below Daily EMA
        elif close < daily_ema and close > vwap_upper and rsi > 62:
            sl_price = close + (atr * 1.5)
            tgt_price = close - (atr * 2.5)

            new_pos = {
                "type": "SELL",
                "entry_price": close,
                "sl_price": sl_price,
                "tgt_price": tgt_price,
                "entry_time": current_time_str,
            }
            save_position(new_pos)

            msg = (
                f"🚨 *NIFTY SELL ENTRY ALERT* 🔴\n\n"
                f"📉 *Entry Price:* ₹{close:.2f}\n"
                f"🛡️ *Initial SL:* ₹{sl_price:.2f}\n"
                f"🎯 *Target:* ₹{tgt_price:.2f}\n"
                f"📊 *VWAP Upper:* ₹{vwap_upper:.2f} | *RSI:* {rsi:.1f}\n"
                f"⏰ *Time:* {current_time_str} IST"
            )
            send_telegram(msg)


if __name__ == "__main__":
    run_scan()
