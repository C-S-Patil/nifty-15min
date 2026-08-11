import json
import os
import requests
from kiteconnect import KiteConnect

# Load Environment / Secrets
KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram_alert(message: str):
    """Sends immediate Markdown alerts to your Telegram Channel/Bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram tokens missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=5,
        )
    except Exception as e:
        print(f"Telegram Alert Failed: {e}")


def execute_paper_order(
    symbol: str, action: str, price: float, num_lots: int, reason: str = ""
):
    """Logs simulated paper trade execution and triggers Telegram alert."""
    qty = num_lots * 75
    alert_msg = (
        f"📄 *PAPER TRADE {action} ALERT*\n\n"
        f"📌 *Asset:* {symbol}\n"
        f"📈 *Price:* ₹{price:.2f}\n"
        f"📦 *Quantity:* {qty} ({num_lots} Lot{'s' if num_lots > 1 else ''})\n"
        f"💡 *Trigger Reason:* {reason}"
    )
    send_telegram_alert(alert_msg)
    return {"status": "SUCCESS", "mode": "PAPER", "price": price, "qty": qty}


def execute_live_kite_order(
    symbol: str, action: str, price: float, num_lots: int
):
    """Places live Market Order via Zerodha Kite Connect API."""
    if not KITE_API_KEY or not KITE_ACCESS_TOKEN:
        err_msg = "⚠️ *LIVE KITE ORDER FAILED*: API Key or Access Token missing in Secrets."
        send_telegram_alert(err_msg)
        return {"status": "FAILED", "reason": "Missing Kite Credentials"}

    try:
        kite = KiteConnect(api_key=KITE_API_KEY)
        kite.set_access_token(KITE_ACCESS_TOKEN)

        transaction_type = (
            kite.TRANSACTION_TYPE_BUY
            if action == "BUY"
            else kite.TRANSACTION_TYPE_SELL
        )
        trading_symbol = "NIFTY24AUGFUT"  # Maps to active futures contract

        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=trading_symbol,
            transaction_type=transaction_type,
            quantity=num_lots * 75,
            product=kite.PRODUCT_MIS,  # Intraday MIS
            order_type=kite.ORDER_TYPE_MARKET,
        )

        alert_msg = (
            f"🚀 *LIVE KITE ORDER EXECUTED* 🟢\n\n"
            f"🆔 *Order ID:* `{order_id}`\n"
            f"📌 *Symbol:* {trading_symbol}\n"
            f"⚡ *Action:* {action}\n"
            f"📦 *Qty:* {num_lots * 75} ({num_lots} Lots)\n"
            f"💰 *Execution Price:* ~₹{price:.2f}"
        )
        send_telegram_alert(alert_msg)
        return {"status": "SUCCESS", "order_id": order_id}

    except Exception as e:
        err_msg = f"❌ *LIVE KITE ORDER ERROR*: {str(e)}"
        send_telegram_alert(err_msg)
        return {"status": "FAILED", "reason": str(e)}
        
