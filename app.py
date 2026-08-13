from datetime import time
import os

import pandas as pd
import plotly.graph_objects as go
import pytz
import requests
import streamlit as st

# ============================================================
# CORE STRATEGY ENGINE
# ============================================================

import strategy_engine as se

from strategy_engine import (
    SYMBOL_MAP,
    export_trades_to_excel,
    fetch_and_prepare_data,
    generate_monthly_breakdown,
    run_institutional_backtest,
)


# Safe fallback for get_combined_12m_trades if module cache is stale
if hasattr(se, "get_combined_12m_trades"):
    get_combined_12m_trades = se.get_combined_12m_trades
else:

    def get_combined_12m_trades(trades_df, file_path=None):
        return trades_df


# ============================================================
# ZERODHA / KITE CONNECT
# ============================================================

try:
    from kiteconnect import KiteConnect

    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False


# ============================================================
# APP CONFIGURATION
# ============================================================

IST = pytz.timezone("Asia/Kolkata")

RSI_DEFAULT_OVERSOLD = 38
RSI_DEFAULT_OVERBOUGHT = 62
ADX_MAX = 32

# No new entry after this time.
# Existing positions are handled separately by the execution
# / EOD-square-off mechanism.
ENTRY_CUTOFF = time(14, 45)


# ============================================================
# TELEGRAM
# ============================================================

def _get_secret(name: str, default: str = "") -> str:
    """
    Safely retrieve a Streamlit secret, falling back to
    environment variables.

    This avoids exceptions when a secret is not configured.
    """
    try:
        value = st.secrets.get(name, None)
    except Exception:
        value = None

    if value is None:
        value = os.getenv(name, default)

    return str(value or default)


def send_telegram_alert(message: str) -> bool:
    """
    Send a Telegram Markdown alert.

    Returns:
        True  -> successful delivery
        False -> delivery failed / credentials unavailable
    """

    bot_token = _get_secret("TELEGRAM_BOT_TOKEN")
    chat_id = _get_secret("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        st.warning(
            "⚠️ Telegram Bot Token or Chat ID is not configured "
            "in Streamlit Secrets / environment variables."
        )
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{bot_token}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )

        if response.status_code == 200:
            st.info("📨 Telegram alert sent successfully.")
            return True

        st.error(
            f"❌ Telegram Error: "
            f"{response.status_code} - "
            f"{response.text[:500]}"
        )
        return False

    except requests.RequestException as exc:
        st.error(
            f"❌ Telegram network error: {exc}"
        )
        return False

    except Exception as exc:
        st.error(
            f"❌ Telegram Exception: {exc}"
        )
        return False


# ============================================================
# EXECUTION
# ============================================================

def execute_auto_trade(
    symbol: str,
    action: str,
    price: float,
    num_lots: int,
    lot_size: int = 65,
    reason: str = "",
):
    """
    Dispatch a signal to Telegram and, when explicitly configured,
    place a Zerodha Kite order.

    IMPORTANT:
    We deliberately do NOT hard-code a futures contract.

    The old hard-coded:
        NIFTY24AUGFUT

    is unsafe because futures contracts expire.

    Live execution therefore requires:
        KITE_TRADING_SYMBOL

    in Streamlit Secrets / environment variables.
    """

    total_qty = num_lots * lot_size

    alert_msg = (
        f"🚨 *{symbol} {action} SIGNAL DETECTED* ⚡\n\n"
        f"📌 *Asset:* {symbol}\n"
        f"📈 *Signal Price:* ₹{price:.2f}\n"
        f"📦 *Quantity:* {total_qty} "
        f"({num_lots} Lot"
        f"{'s' if num_lots > 1 else ''} "
        f"@ {lot_size}/lot)\n"
        f"💡 *Trigger Reason:* {reason}"
    )

    send_telegram_alert(alert_msg)

    api_key = _get_secret("KITE_API_KEY")
    access_token = _get_secret("KITE_ACCESS_TOKEN")

    # --------------------------------------------------------
    # PAPER MODE
    # --------------------------------------------------------

    if not KITE_AVAILABLE or not api_key or not access_token:

        st.warning(
            "ℹ️ Zerodha Kite credentials/package are not fully "
            "configured. Execution recorded in Paper Mode."
        )

        return {
            "status": "PAPER_LOGGED",
            "symbol": symbol,
            "action": action,
            "quantity": total_qty,
            "price": price,
        }

    # --------------------------------------------------------
    # LIVE TRADING SYMBOL
    # --------------------------------------------------------

    trading_symbol = _get_secret("KITE_TRADING_SYMBOL")

    if not trading_symbol:

        st.error(
            "🛑 LIVE EXECUTION BLOCKED.\n\n"
            "KITE_TRADING_SYMBOL is not configured.\n\n"
            "The application will never use an obsolete or "
            "hard-coded futures contract."
        )

        send_telegram_alert(
            f"🛑 *LIVE ORDER BLOCKED*\n\n"
            f"Asset: {symbol}\n"
            f"Signal: {action}\n"
            f"Quantity: {total_qty}\n"
            f"Reason: KITE_TRADING_SYMBOL is not configured."
        )

        return {
            "status": "BLOCKED",
            "reason": "KITE_TRADING_SYMBOL_NOT_CONFIGURED",
        }

    # --------------------------------------------------------
    # LIVE KITE ORDER
    # --------------------------------------------------------

    try:

        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        if action == "BUY":
            transaction_type = kite.TRANSACTION_TYPE_BUY
        elif action == "SELL":
            transaction_type = kite.TRANSACTION_TYPE_SELL
        else:
            raise ValueError(
                f"Invalid trading action: {action}"
            )

        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=trading_symbol,
            transaction_type=transaction_type,
            quantity=total_qty,
            product=kite.PRODUCT_MIS,
            order_type=kite.ORDER_TYPE_MARKET,
        )

        st.success(
            f"🚀 Live Kite Order Placed!\n\n"
            f"Symbol: `{trading_symbol}`\n"
            f"Action: `{action}`\n"
            f"Quantity: `{total_qty}`\n"
            f"Order ID: `{order_id}`"
        )

        send_telegram_alert(
            f"🚀 *LIVE ORDER EXECUTED*\n\n"
            f"Symbol: `{trading_symbol}`\n"
            f"Action: `{action}`\n"
            f"Order ID: `{order_id}`\n"
            f"Qty: `{total_qty}`"
        )

        return {
            "status": "LIVE_SUCCESS",
            "order_id": order_id,
            "trading_symbol": trading_symbol,
            "quantity": total_qty,
        }

    except Exception as exc:

        st.error(
            f"❌ Kite Order Error: {exc}"
        )

        send_telegram_alert(
            f"❌ *LIVE ORDER FAILED*\n\n"
            f"Symbol: `{trading_symbol}`\n"
            f"Action: `{action}`\n"
            f"Qty: `{total_qty}`\n"
            f"Error: `{str(exc)[:500]}`"
        )

        return {
            "status": "FAILED",
            "reason": str(exc),
        }


# ============================================================
# STREAMLIT PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Quant Strategy & Execution Engine",
    page_icon="⚡",
    layout="wide",
)


st.title(
    "⚡ Quant Strategy & Execution Engine "
    "(Indices & Stocks)"
)


# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.header(
    "🎯 Asset & Capital Configuration"
)

selected_symbol_name = st.sidebar.selectbox(
    "Select Asset / Stock",
    list(SYMBOL_MAP.keys()),
)

selected_asset_info = SYMBOL_MAP[
    selected_symbol_name
]

ticker = selected_asset_info["ticker"]

default_lot_size = selected_asset_info["lot_size"]

capital = st.sidebar.number_input(
    "Trading Capital (₹)",
    value=250000.0,
    step=25000.0,
    format="%.2f",
)

num_lots = st.sidebar.slider(
    "Number of Lots",
    min_value=1,
    max_value=20,
    value=1,
)

st.sidebar.caption(
    f"Current Lot Size for {selected_symbol_name}: "
    f"**{default_lot_size} shares**"
)

st.sidebar.markdown("---")

st.sidebar.header(
    "⚙️ Strategy Parameters"
)

rsi_oversold = st.sidebar.slider(
    "RSI Oversold Filter",
    25,
    45,
    RSI_DEFAULT_OVERSOLD,
)

rsi_overbought = st.sidebar.slider(
    "RSI Overbought Filter",
    55,
    75,
    RSI_DEFAULT_OVERBOUGHT,
)

st.sidebar.caption(
    f"ADX maximum: **{ADX_MAX}**"
)

st.sidebar.caption(
    "Entry cutoff: **14:45 IST**"
)


# ============================================================
# MARKET DATA
# ============================================================

data = fetch_and_prepare_data(
    ticker=ticker,
    period="1mo",
    interval="15m",
)


# ------------------------------------------------------------
# FAIL CLOSED ON MARKET DATA FAILURE
# ------------------------------------------------------------

if data.empty:

    st.error(
        f"🛑 Market data unavailable for "
        f"**{selected_symbol_name}**."
    )

    st.warning(
        "No trading signal or order will be generated. "
        "The strategy requires reliable market data."
    )

    st.info(
        "The market-data provider may currently be "
        "rate-limited or temporarily unavailable. "
        "Please retry after the provider cooldown."
    )

    st.stop()


# ------------------------------------------------------------
# BASIC DATA VALIDATION
# ------------------------------------------------------------

required_columns = [
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "VWAP",
    "VWAP_Upper",
    "VWAP_Lower",
    "RSI",
    "ATR",
    "ADX",
    "Daily_EMA50",
]

missing_columns = [
    column
    for column in required_columns
    if column not in data.columns
]

if missing_columns:

    st.error(
        "🛑 Strategy data is incomplete.\n\n"
        f"Missing columns: {missing_columns}"
    )

    st.stop()


if len(data) < 2:

    st.error(
        "🛑 Insufficient candles to evaluate "
        "the entry strategy."
    )

    st.stop()


# ============================================================
# DATASETS
# ============================================================

one_month_data = data.tail(22 * 25)


# ============================================================
# SECTION 1 — OPEN TRADES & LIVE CHART
# ============================================================

st.subheader(
    f"📌 Active Market Monitor — "
    f"{selected_symbol_name}"
)

latest = data.iloc[-1]
previous = data.iloc[-2]

last_time_ist = latest.name.strftime(
    "%Y-%m-%d %H:%M IST"
)


# ============================================================
# TREND
# ============================================================

trend_state = (
    "BULLISH 🟢"
    if latest["Close"] > latest["Daily_EMA50"]
    else "BEARISH 🔴"
)


# ============================================================
# EXACT ENTRY LOGIC
# ============================================================

current_candle_time = latest.name.time()


buy_signal = (
    previous["Close"] < previous["VWAP_Lower"]
    and latest["Close"] > latest["Open"]
    and latest["RSI"] < rsi_oversold
    and latest["ADX"] < ADX_MAX
    and current_candle_time < ENTRY_CUTOFF
)


sell_signal = (
    previous["Close"] > previous["VWAP_Upper"]
    and latest["Close"] < latest["Open"]
    and latest["RSI"] > rsi_overbought
    and latest["ADX"] < ADX_MAX
    and current_candle_time < ENTRY_CUTOFF
)


latest_signal = "HOLD"

if buy_signal:
    latest_signal = "BUY"

elif sell_signal:
    latest_signal = "SELL"


# ============================================================
# LIVE METRICS
# ============================================================

c1, c2, c3, c4, c5 = st.columns(5)

c1.metric(
    "Close Price",
    f"₹{latest['Close']:.2f}",
)

c2.metric(
    "VWAP",
    f"₹{latest['VWAP']:.2f}",
)

c3.metric(
    "RSI (14)",
    f"{latest['RSI']:.1f}",
)

c4.metric(
    "ADX (14)",
    f"{latest['ADX']:.1f}",
)

c5.metric(
    "Daily Trend",
    trend_state,
)


# ============================================================
# SIGNAL STATUS
# ============================================================

if latest_signal == "BUY":

    st.success(
        f"🟢 **BUY SIGNAL ACTIVE** at "
        f"₹{latest['Close']:.2f}\n\n"
        f"Previous Close: "
        f"₹{previous['Close']:.2f}\n\n"
        f"VWAP Lower: "
        f"₹{previous['VWAP_Lower']:.2f}\n\n"
        f"RSI: {latest['RSI']:.1f} | "
        f"ADX: {latest['ADX']:.1f}"
    )

    if st.button(
        "Dispatch Auto-Trade (Telegram + Kite)",
        type="primary",
    ):

        execute_auto_trade(
            selected_symbol_name,
            "BUY",
            float(latest["Close"]),
            num_lots,
            default_lot_size,
            (
                "Previous candle below VWAP Lower + "
                "bullish reversal candle + "
                f"RSI < {rsi_oversold} + "
                f"ADX < {ADX_MAX}"
            ),
        )


elif latest_signal == "SELL":

    st.error(
        f"🔴 **SELL SIGNAL ACTIVE** at "
        f"₹{latest['Close']:.2f}\n\n"
        f"Previous Close: "
        f"₹{previous['Close']:.2f}\n\n"
        f"VWAP Upper: "
        f"₹{previous['VWAP_Upper']:.2f}\n\n"
        f"RSI: {latest['RSI']:.1f} | "
        f"ADX: {latest['ADX']:.1f}"
    )

    if st.button(
        "Dispatch Auto-Trade (Telegram + Kite)",
        type="primary",
    ):

        execute_auto_trade(
            selected_symbol_name,
            "SELL",
            float(latest["Close"]),
            num_lots,
            default_lot_size,
            (
                "Previous candle above VWAP Upper + "
                "bearish reversal candle + "
                f"RSI > {rsi_overbought} + "
                f"ADX < {ADX_MAX}"
            ),
        )


else:

    if current_candle_time >= ENTRY_CUTOFF:

        st.warning(
            f"⏱️ **ENTRY WINDOW CLOSED**\n\n"
            f"Current candle time: "
            f"{latest.name.strftime('%H:%M IST')}\n\n"
            f"No new entries are permitted after "
            f"{ENTRY_CUTOFF.strftime('%H:%M IST')}."
        )

    else:

        st.info(
            "⚪ **No active entry signals on the "
            "current candle.**\n\n"
            f"Strategy Status: **HOLD**\n\n"
            f"RSI: {latest['RSI']:.1f} | "
            f"ADX: {latest['ADX']:.1f}"
        )


# ============================================================
# STRATEGY CONDITION DIAGNOSTICS
# ============================================================

with st.expander("🔍 Current Strategy Diagnostics"):

    diagnostics = pd.DataFrame(
        {
            "Condition": [
                "Previous Close < VWAP Lower",
                "Previous Close > VWAP Upper",
                "Current Candle Bullish",
                "Current Candle Bearish",
                "RSI Oversold",
                "RSI Overbought",
                "ADX < Maximum",
                "Entry Window Open",
            ],
            "Value": [
                bool(
                    previous["Close"]
                    < previous["VWAP_Lower"]
                ),
                bool(
                    previous["Close"]
                    > previous["VWAP_Upper"]
                ),
                bool(
                    latest["Close"]
                    > latest["Open"]
                ),
                bool(
                    latest["Close"]
                    < latest["Open"]
                ),
                bool(
                    latest["RSI"]
                    < rsi_oversold
                ),
                bool(
                    latest["RSI"]
                    > rsi_overbought
                ),
                bool(
                    latest["ADX"]
                    < ADX_MAX
                ),
                bool(
                    current_candle_time
                    < ENTRY_CUTOFF
                ),
            ],
        }
    )

    st.dataframe(
        diagnostics,
        width="stretch",
        hide_index=True,
    )


# ============================================================
# INTRADAY CHART
# ============================================================

recent_chart = data.tail(120)

min_y = (
    float(recent_chart["Low"].min())
    - 10.0
)

max_y = (
    float(recent_chart["High"].max())
    + 10.0
)


fig = go.Figure()


fig.add_trace(
    go.Scatter(
        x=recent_chart.index,
        y=recent_chart["Close"],
        mode="lines",
        name="Close",
        line=dict(
            color="#1f77b4",
            width=2,
        ),
    )
)


fig.add_trace(
    go.Scatter(
        x=recent_chart.index,
        y=recent_chart["VWAP"],
        mode="lines",
        name="VWAP",
        line=dict(
            color="#ff7f0e",
            width=1.5,
        ),
    )
)


fig.add_trace(
    go.Scatter(
        x=recent_chart.index,
        y=recent_chart["VWAP_Upper"],
        mode="lines",
        name="VWAP Upper",
        line=dict(
            color="#d62728",
            width=1,
            dash="dash",
        ),
    )
)


fig.add_trace(
    go.Scatter(
        x=recent_chart.index,
        y=recent_chart["VWAP_Lower"],
        mode="lines",
        name="VWAP Lower",
        line=dict(
            color="#2ca02c",
            width=1,
            dash="dash",
        ),
    )
)


fig.update_xaxes(
    rangebreaks=[
        dict(bounds=["sat", "mon"]),
        dict(
            bounds=[15.5, 9.25],
            pattern="hour",
        ),
    ]
)


fig.update_layout(
    title=(
        f"{selected_symbol_name} Active Market "
        f"Sessions (09:15 - 15:30 IST) — "
        f"{last_time_ist}"
    ),
    yaxis=dict(
        range=[min_y, max_y],
        title="Price (₹)",
    ),
    xaxis=dict(
        title="Time (IST)",
    ),
    template="plotly_dark",
    height=420,
)


st.plotly_chart(
    fig,
    width="stretch",
)


# ============================================================
# SECTION 2 — 1-MONTH PERFORMANCE
# ============================================================

st.markdown("---")

st.subheader(
    "📊 Current Month Executed Trades & Performance"
)


trades_1m = run_institutional_backtest(
    one_month_data,
    rsi_oversold=rsi_oversold,
    rsi_overbought=rsi_overbought,
    num_lots=num_lots,
    lot_size=default_lot_size,
)


if not trades_1m.empty:

    total_trades_1m = len(trades_1m)

    winning_trades_1m = len(
        trades_1m[
            trades_1m["NetPnL"] > 0
        ]
    )

    win_rate_1m = (
        winning_trades_1m
        / total_trades_1m
    ) * 100

    net_pnl_1m = (
        trades_1m["NetPnL"].sum()
    )

    return_on_capital_1m = (
        net_pnl_1m / capital
    ) * 100


    m1, m2, m3, m4 = st.columns(4)


    m1.metric(
        "Total Trades (1Mo)",
        total_trades_1m,
    )


    m2.metric(
        "Win Rate %",
        f"{win_rate_1m:.1f}%",
    )


    m3.metric(
   
