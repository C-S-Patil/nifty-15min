import datetime
import json
import os
from token_manager import get_valid_kite_client

PAPER_TRADES_FILE = "paper_positions.json"


def execute_paper_order(
    symbol, transaction_type, quantity, price, sl_price, tgt_price
):
    positions = []
    if os.path.exists(PAPER_TRADES_FILE):
        with open(PAPER_TRADES_FILE, "r") as f:
            try:
                positions = json.load(f)
            except json.JSONDecodeError:
                positions = []

    order_entry = {
        "timestamp": str(datetime.datetime.now()),
        "symbol": symbol,
        "type": transaction_type,
        "quantity": quantity,
        "entry_price": price,
        "sl_price": sl_price,
        "tgt_price": tgt_price,
        "status": "OPEN",
    }

    positions.append(order_entry)
    with open(PAPER_TRADES_FILE, "w") as f:
        json.dump(positions, f, indent=4)

    return order_entry


def execute_live_kite_order(trading_symbol, transaction_type, quantity):
    try:
        kite = get_valid_kite_client()
        tx_type = (
            kite.TRANSACTION_TYPE_BUY
            if transaction_type == "BUY"
            else kite.TRANSACTION_TYPE_SELL
        )

        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=trading_symbol,
            transaction_type=tx_type,
            quantity=quantity,
            product=kite.PRODUCT_MIS,
            order_type=kite.ORDER_TYPE_MARKET,
            tag="NIFTY_15MIN_BOT",
        )
        return order_id
    except Exception as e:
        print(f"Order Placement Failed: {e}")
        return None