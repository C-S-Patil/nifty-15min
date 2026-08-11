import os
import requests
import streamlit as st
from kiteconnect import KiteConnect

# Retrieve credentials from Streamlit secrets or OS environment
TELEGRAM_BOT_TOKEN = st.secrets.get(
    "TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", "")
)
TELEGRAM_CHAT_ID = st.secrets.get(
    "TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", "")
)
KITE_API_KEY = st.secrets.get("KITE_API_KEY", os.getenv("KITE_API_KEY", ""))
KITE_ACCESS_TOKEN = st.secrets.get(
    "KITE_ACCESS_TOKEN", os.getenv("KITE_ACCESS_TOKEN", "")
)


def send_telegram_alert(message: str) -> bool:
    """Sends Markdown alerts to Telegram. Logs to UI if credentials missing."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        st.warning(
            "⚠️ [LOG] Telegram Bot Token or Chat ID not configured in secrets. Skipping Telegram alert."
        )
        print("Telegram credentials missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=5,
        )
        if response.status_code == 200:
            st.info("📨 [LOG] Telegram alert dispatched successfully.")
            return True
        else:
            st.error(
                f"❌ [LOG] Telegram API Error: {response.status_code} - {response.text}"
            )
            return False
    except Exception as e:
        st.error(f"❌ [LOG] Telegram connection exception: {e}")
        return False


def execute_auto_trade(
    symbol: str, action: str, price: float, num_lots: int, reason: str = ""
):
    """Unified Order Execution Pipeline: Dispatches Telegram alert and attempts Kite Live Order.

    Defaults to Paper Trade Log if Kite secrets are unconfigured.
    """
    total_qty = num_lots * 75

    # 1. Dispatch Telegram Alert
    alert_msg = (
        f"🚨 *NIFTY {action} SIGNAL DETECTED* ⚡\n\n"
        f"📌 *Asset:* {symbol}\n"
        f"📈 *Signal Price:* ₹{price:.2f}\n"
        f"📦 *Order Quantity:* {total_qty} ({num_lots} Lot{'s' if num_lots > 1 else ''})\n"
        f"💡 *Trigger Reason:* {reason}"
    )
    send_telegram_alert(alert_msg)

    # 2. Attempt Live Kite Order Execution
    if KITE_API_KEY and KITE_ACCESS_TOKEN:
        try:
            kite = KiteConnect(api_key=KITE_API_KEY)
            kite.set_access_token(KITE_ACCESS_TOKEN)

            transaction_type = (
                kite.TRANSACTION_TYPE_BUY
                if action == "BUY"
                else kite.TRANSACTION_TYPE_SELL
            )
            trading_symbol = "NIFTY24AUGFUT"

            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=trading_symbol,
                transaction_type=transaction_type,
                quantity=total_qty,
                product=kite.PRODUCT_MIS,
                order_type=kite.ORDER_TYPE_MARKET,
            )

            success_msg = f"🚀 *LIVE KITE ORDER PLACED*\nOrder ID: `{order_id}` | Qty: {total_qty}"
            send_telegram_alert(success_msg)
            st.success(f"🚀 Live Kite Order Placed! Order ID: {order_id}")
            return {"status": "LIVE_SUCCESS", "order_id": order_id}

        except Exception as e:
            err_msg = f"❌ *Kite Order Failed*: {str(e)} | Logged in Paper Mode."
            send_telegram_alert(err_msg)
            st.error(f"❌ Kite Order Error: {e}")
            return {"status": "PAPER_FALLBACK", "reason": str(e)}

    else:
        st.warning(
            "ℹ️ [LOG] Zerodha Kite credentials not found in Streamlit Secrets. Defaulting execution to Paper Trade Log."
        )
        return {
            "status": "PAPER_LOGGED",
            "price": price,
            "qty": total_qty,
            "reason": reason,
        }
        
