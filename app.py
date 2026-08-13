from datetime import datetime, time
import os

import pandas as pd
import plotly.graph_objects as go
import pytz
import requests
import streamlit as st

import strategy_engine as se
from strategy_engine import (
    SYMBOL_MAP,
    ADX_MAX_DEFAULT,
    ATR_SL_MULTIPLIER,
    ATR_TARGET_MULTIPLIER,
    ENTRY_CUTOFF,
    EOD_SQUAREOFF,
    export_trades_to_excel,
    fetch_and_prepare_data,
    generate_monthly_breakdown,
    get_combined_12m_trades,
    evaluate_signal,
    run_institutional_backtest,
)

try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except ImportError:
    KiteConnect = None
    KITE_AVAILABLE = False

IST = pytz.timezone("Asia/Kolkata")

st.set_page_config(
    page_title="Quant Strategy & Execution Engine",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

def secret(name, default=""):
    try:
        value = st.secrets.get(name)
    except Exception:
        value = None
    return str(value if value is not None else os.getenv(name, default))

def truthy(name):
    return secret(name, "false").strip().lower() in {"1", "true", "yes", "on"}

def telegram_configured():
    return bool(secret("TELEGRAM_BOT_TOKEN") and secret("TELEGRAM_CHAT_ID"))

def send_telegram_alert(message, show_ui=True):
    token, chat_id = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        if show_ui:
            st.error("Telegram is not configured. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        ok = r.status_code == 200 and bool(r.json().get("ok"))
        if show_ui:
            (st.success if ok else st.error)(
                "Telegram alert delivered." if ok else f"Telegram failed: HTTP {r.status_code}"
            )
        return ok
    except Exception as exc:
        if show_ui:
            st.error(f"Telegram connection failed: {exc}")
        return False

def test_telegram():
    token, chat_id = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        st.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        if r.status_code != 200 or not r.json().get("ok"):
            st.error("Telegram Bot API authentication failed.")
            return
        bot_name = r.json()["result"].get("username", "bot")
        message = (
            "🟢 *Quant Engine Telegram Test*\n\n"
            f"Bot: `@{bot_name}`\n"
            f"Time: `{datetime.now(IST):%Y-%m-%d %H:%M:%S IST}`\n"
            "Status: *Connection successful*"
        )
        send_telegram_alert(message)
    except Exception as exc:
        st.error(f"Telegram test failed: {exc}")

def build_kite():
    if not KITE_AVAILABLE:
        return None
    api_key, access_token = secret("KITE_API_KEY"), secret("KITE_ACCESS_TOKEN")
    if not api_key or not access_token:
        return None
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite

def resolve_nearest_future(kite, underlying):
    instruments = kite.instruments("NFO")
    today = datetime.now(IST).date()
    candidates = []
    for inst in instruments:
        if inst.get("instrument_type") != "FUT":
            continue
        if inst.get("name") != underlying:
            continue
        expiry = inst.get("expiry")
        if not expiry:
            continue
        if hasattr(expiry, "date"):
            expiry = expiry.date()
        if expiry >= today:
            candidates.append(inst)
    if not candidates:
        raise RuntimeError(f"No live NFO future found for {underlying}.")
    candidates.sort(key=lambda x: x["expiry"])
    return candidates[0]

def current_open_quantity(kite, trading_symbol):
    try:
        positions = kite.positions().get("net", [])
        for p in positions:
            if p.get("exchange") == "NFO" and p.get("tradingsymbol") == trading_symbol:
                return int(p.get("quantity", 0))
    except Exception:
        pass
    return 0

def execute_live_order(symbol_name, action, price, num_lots, configured_lot_size, reason):
    kite = build_kite()
    if kite is None:
        return {"status": "PAPER_MODE", "reason": "Kite credentials/package unavailable"}

    if not truthy("LIVE_TRADING_ENABLED"):
        return {"status": "BLOCKED", "reason": "LIVE_TRADING_ENABLED is not true"}

    cfg = SYMBOL_MAP[symbol_name]
    underlying = cfg["future_underlying"]

    try:
        inst = resolve_nearest_future(kite, underlying)
        trading_symbol = inst["tradingsymbol"]
        broker_lot_size = int(inst["lot_size"])
        quantity = num_lots * broker_lot_size

        if quantity <= 0:
            raise RuntimeError("Invalid quantity.")

        open_qty = current_open_quantity(kite, trading_symbol)
        if open_qty != 0:
            raise RuntimeError(
                f"Existing NFO position detected for {trading_symbol}: {open_qty}. "
                "New signal blocked to prevent accidental stacking."
            )

        tx = kite.TRANSACTION_TYPE_BUY if action == "BUY" else kite.TRANSACTION_TYPE_SELL
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=trading_symbol,
            transaction_type=tx,
            quantity=quantity,
            product=kite.PRODUCT_MIS,
            order_type=kite.ORDER_TYPE_MARKET,
            validity=kite.VALIDITY_DAY,
            tag="QUANT15M",
        )

        return {
            "status": "LIVE_ORDER_PLACED",
            "order_id": order_id,
            "trading_symbol": trading_symbol,
            "quantity": quantity,
            "lot_size": broker_lot_size,
            "reason": reason,
        }
    except Exception as exc:
        return {"status": "FAILED", "reason": str(exc)}

def dispatch_trade(symbol_name, action, price, num_lots, lot_size, reason):
    qty = num_lots * lot_size
    signal_message = (
        f"🚨 *{symbol_name} {action} SIGNAL*\n\n"
        f"Price: `₹{price:.2f}`\n"
        f"Configured Qty: `{qty}`\n"
        f"Reason: {reason}"
    )
    telegram_ok = send_telegram_alert(signal_message)

    if not truthy("LIVE_TRADING_ENABLED"):
        st.info("📝 Paper Mode: no live order was placed.")
        return {"status": "PAPER_MODE", "telegram": telegram_ok}

    if not st.session_state.get("live_confirmed", False):
        st.error("Live order blocked: confirm the live-trading checkbox first.")
        return {"status": "BLOCKED", "reason": "LIVE_CONFIRMATION_REQUIRED"}

    result = execute_live_order(symbol_name, action, price, num_lots, lot_size, reason)
    if result["status"] == "LIVE_ORDER_PLACED":
        st.success(
            f"🚀 Order submitted: `{result['trading_symbol']}` "
            f"{action} × {result['quantity']} | Order ID `{result['order_id']}`"
        )
        send_telegram_alert(
            f"🚀 *LIVE ORDER SUBMITTED*\n"
            f"Symbol: `{result['trading_symbol']}`\n"
            f"Action: `{action}`\n"
            f"Quantity: `{result['quantity']}`\n"
            f"Order ID: `{result['order_id']}`",
            show_ui=False,
        )
    else:
        st.error(f"Live execution blocked/failed: {result.get('reason', 'unknown')}")
    return result

# ---------------- Sidebar ----------------
st.sidebar.title("⚙️ Engine Controls")
selected_name = st.sidebar.selectbox("Asset", list(SYMBOL_MAP.keys()))
cfg = SYMBOL_MAP[selected_name]
ticker = cfg["ticker"]
lot_size = cfg["lot_size"]

capital = st.sidebar.number_input("Capital (₹)", min_value=10000.0, value=250000.0, step=25000.0)
num_lots = st.sidebar.number_input("Lots", min_value=1, max_value=20, value=1, step=1)

st.sidebar.markdown("### Strategy")
rsi_oversold = st.sidebar.slider("RSI Oversold", 25, 45, 38)
rsi_overbought = st.sidebar.slider("RSI Overbought", 55, 75, 62)
st.sidebar.caption(f"ADX must be < {ADX_MAX_DEFAULT}")
st.sidebar.caption(f"Entry cutoff: {ENTRY_CUTOFF.strftime('%H:%M IST')}")
st.sidebar.caption(f"EOD square-off: {EOD_SQUAREOFF.strftime('%H:%M IST')}")

st.sidebar.markdown("---")
st.sidebar.markdown("### 🔔 Notifications")
telegram_ok = telegram_configured()
st.sidebar.write("Telegram:", "🟢 Configured" if telegram_ok else "🔴 Not configured")
if st.sidebar.button("🔔 Test Telegram Alert"):
    test_telegram()

st.sidebar.markdown("---")
st.sidebar.markdown("### 🛡️ Execution Safety")
live_enabled = truthy("LIVE_TRADING_ENABLED")
st.sidebar.write("Live trading:", "🔴 ENABLED" if live_enabled else "🟢 Paper Mode")
if live_enabled:
    st.session_state.live_confirmed = st.sidebar.checkbox(
        "I understand this can place real NFO orders.",
        value=False,
    )
else:
    st.session_state.live_confirmed = False

# ---------------- Header ----------------
st.title("⚡ Quant Strategy & Execution Engine")
st.caption("15-minute VWAP mean-reversion | RSI + ADX confirmation | ATR risk management")

data = fetch_and_prepare_data(ticker=ticker, period="1mo", interval="15m")
if data.empty:
    st.error(f"🛑 Reliable market data is unavailable for {selected_name}.")
    st.warning("No signal or live order is generated when market data cannot be validated.")
    st.stop()

closed = se.get_last_closed_candles(data)
if len(closed) < 2:
    st.warning("Waiting for a fully closed 15-minute candle.")
    st.stop()

latest = closed.iloc[-1]
previous = closed.iloc[-2]
result = evaluate_signal(data, rsi_oversold, rsi_overbought, ADX_MAX_DEFAULT, require_closed=True)
signal = result["signal"]

# ---------------- Status cards ----------------
st.subheader(f"📌 {selected_name} — Live Market Monitor")
c1, c2, c3 = st.columns(3)
c1.metric("Last Closed Price", f"₹{latest['Close']:.2f}")
c2.metric("VWAP", f"₹{latest['VWAP']:.2f}")
c3.metric("Daily Trend", latest["Daily_Trend"])

c4, c5, c6 = st.columns(3)
c4.metric("RSI (14)", f"{latest['RSI']:.1f}")
c5.metric("ADX (14)", f"{latest['ADX']:.1f}")
c6.metric("ATR (14)", f"₹{latest['ATR']:.2f}")

data_source = data.attrs.get("data_source", "UNKNOWN")
data_degraded = bool(data.attrs.get("degraded", False))
if data_degraded:
    st.warning("⚠️ Proxy/degraded market data is being displayed. Signal generation and execution are disabled until primary market data is restored.")
st.caption(
    f"Data source: `{data_source}` • "
    f"Closed candle: `{latest.name:%d-%b-%Y %H:%M IST}` • "
    f"Next decision window: `{ENTRY_CUTOFF:%H:%M IST}`"
)

if data_degraded:
    signal = "HOLD"
    result = dict(result)
    result["reason"] = "DEGRADED_PROXY_DATA"

if signal == "BUY":
    st.success(f"🟢 **BUY SIGNAL** — ₹{latest['Close']:.2f}\n\n{result['reason']}")
elif signal == "SELL":
    st.error(f"🔴 **SELL SIGNAL** — ₹{latest['Close']:.2f}\n\n{result['reason']}")
else:
    if result["reason"] == "ENTRY_WINDOW_CLOSED":
        st.warning("⏱️ Entry window closed. No new trades are permitted.")
    else:
        st.info("⚪ **HOLD** — all entry conditions are not simultaneously satisfied.")

# ---------------- Diagnostics ----------------
with st.expander("🔍 Signal Diagnostics", expanded=False):
    diagnostics = pd.DataFrame([
        ["Previous close below VWAP Lower", bool(previous["Close"] < previous["VWAP_Lower"])],
        ["Previous close above VWAP Upper", bool(previous["Close"] > previous["VWAP_Upper"])],
        ["Current candle bullish", bool(latest["Close"] > latest["Open"])],
        ["Current candle bearish", bool(latest["Close"] < latest["Open"])],
        [f"RSI < {rsi_oversold}", bool(latest["RSI"] < rsi_oversold)],
        [f"RSI > {rsi_overbought}", bool(latest["RSI"] > rsi_overbought)],
        [f"ADX < {ADX_MAX_DEFAULT}", bool(latest["ADX"] < ADX_MAX_DEFAULT)],
        ["Entry window open", bool((latest.name + pd.Timedelta(minutes=15)).time() <= ENTRY_CUTOFF)],
    ], columns=["Condition", "Pass"])
    st.dataframe(diagnostics, width="stretch", hide_index=True)

# ---------------- Chart ----------------
chart = data.tail(120)
fig = go.Figure()
fig.add_trace(go.Candlestick(
    x=chart.index, open=chart["Open"], high=chart["High"],
    low=chart["Low"], close=chart["Close"], name="15m",
))
fig.add_trace(go.Scatter(x=chart.index, y=chart["VWAP"], name="VWAP", mode="lines"))
fig.add_trace(go.Scatter(x=chart.index, y=chart["VWAP_Upper"], name="VWAP Upper", mode="lines", line=dict(dash="dash")))
fig.add_trace(go.Scatter(x=chart.index, y=chart["VWAP_Lower"], name="VWAP Lower", mode="lines", line=dict(dash="dash")))
fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[15.5, 9.25], pattern="hour")])
fig.update_layout(height=500, xaxis_title="Time (IST)", yaxis_title="Price (₹)", template="plotly_dark", margin=dict(l=10, r=10, t=45, b=10))
st.plotly_chart(fig, width="stretch")

# ---------------- Execution ----------------
if signal in {"BUY", "SELL"} and not data_degraded:
    st.markdown("---")
    st.subheader("🚦 Trade Dispatch")
    st.write(
        f"Signal: **{signal}** | Lots: **{num_lots}** | "
        f"Configured quantity: **{num_lots * lot_size}**"
    )
    if live_enabled:
        st.warning("Live trading is enabled. The broker's nearest valid futures contract and current lot size will be resolved dynamically.")
    else:
        st.info("Paper Mode is active. Clicking dispatch will send Telegram but will not place a live order.")
    if st.button(f"Dispatch {signal} — Telegram + Execution", type="primary"):
        dispatch_trade(selected_name, signal, float(latest["Close"]), int(num_lots), lot_size, result["reason"])

# ---------------- Backtest ----------------
st.markdown("---")
st.subheader("📊 Current 1-Month Strategy Performance")
trades = pd.DataFrame() if data_degraded else run_institutional_backtest(
    data,
    rsi_oversold=rsi_oversold,
    rsi_overbought=rsi_overbought,
    adx_max=ADX_MAX_DEFAULT,
    sl_atr_mult=ATR_SL_MULTIPLIER,
    tgt_atr_mult=ATR_TARGET_MULTIPLIER,
    num_lots=int(num_lots),
    lot_size=lot_size,
)
if data_degraded:
    st.info("Backtest is disabled while only degraded proxy data is available.")
elif trades.empty:
    st.info("No completed backtest trades in the available period.")
else:
    total = len(trades)
    wins = int((trades["NetPnL"] > 0).sum())
    pnl = float(trades["NetPnL"].sum())
    win_rate = wins / total * 100
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Trades", total)
    m2.metric("Win Rate", f"{win_rate:.1f}%")
    m3.metric("Net P&L", f"₹{pnl:,.0f}")
    m4.metric("Return on Capital", f"{pnl / capital * 100:+.2f}%")
    st.download_button(
        "📥 Download Trades Excel",
        data=export_trades_to_excel(trades),
        file_name=f"{selected_name}_15m_trades.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    st.dataframe(trades, width="stretch", hide_index=True)

# ---------------- 12m ----------------
st.markdown("---")
st.subheader("🗓️ Historical Performance")
if "show_history" not in st.session_state:
    st.session_state.show_history = False
if st.button("📈 Load 12-Month Breakdown"):
    st.session_state.show_history = True

if st.session_state.show_history:
    combined = get_combined_12m_trades(trades)
    if combined.empty:
        st.info("No historical trade records available.")
    else:
        table = generate_monthly_breakdown(combined, capital)
        st.dataframe(table, width="stretch", hide_index=True)
        if "ExitTime" in combined.columns:
            x = pd.to_datetime(combined["ExitTime"], errors="coerce")
            if x.dt.tz is not None:
                x = x.dt.tz_localize(None)
            combined = combined.copy()
            combined["Month"] = x.dt.strftime("%b %Y")
            monthly = combined.groupby("Month", sort=False)["NetPnL"].sum()
            fig2 = go.Figure(go.Bar(x=monthly.index, y=monthly.values))
            fig2.update_layout(height=350, template="plotly_dark", yaxis_title="Net P&L (₹)")
            st.plotly_chart(fig2, width="stretch")
else:
    st.caption("Historical results are loaded only when requested.")
