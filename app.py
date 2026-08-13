
from datetime import datetime
import math
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
    MIN_BACKTEST_TRADES_FOR_STATS,
    RELIABLE_BACKTEST_TRADES,
    calculate_strategy_statistics,
    evaluate_signal,
    export_trades_to_excel,
    fetch_and_prepare_data,
    prepare_research_data,
    run_institutional_backtest,
)

try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except ImportError:
    KiteConnect = None
    KITE_AVAILABLE = False

IST = pytz.timezone("Asia/Kolkata")
SIGNAL_MAX_AGE_MINUTES = 20


# ---------------------------------------------------------------------
# App helpers
# ---------------------------------------------------------------------

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
            st.error("Telegram is not configured.")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        payload = response.json()
        ok = response.status_code == 200 and bool(payload.get("ok"))
        if show_ui:
            if ok:
                st.success("Telegram alert delivered.")
            else:
                st.error(
                    f"Telegram failed: HTTP {response.status_code} — "
                    f"{payload.get('description', response.text[:300])}"
                )
        return ok
    except Exception as exc:
        if show_ui:
            st.error(f"Telegram connection failed: {exc}")
        return False


def test_telegram():
    token = secret("TELEGRAM_BOT_TOKEN")
    if not token or not secret("TELEGRAM_CHAT_ID"):
        st.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10,
        )
        payload = response.json()
        if response.status_code != 200 or not payload.get("ok"):
            st.error("Telegram Bot API authentication failed.")
            return
        bot_name = payload["result"].get("username", "bot")
        message = (
            "🟢 *Quant Engine Telegram Test*\n\n"
            f"Bot: `@{bot_name}`\n"
            f"Time: `{datetime.now(IST):%Y-%m-%d %H:%M:%S IST}`\n"
            "Status: *Connection successful*"
        )
        send_telegram_alert(message)
    except Exception as exc:
        st.error(f"Telegram test failed: {exc}")


# ---------------------------------------------------------------------
# Zerodha safety
# ---------------------------------------------------------------------

def build_kite():
    if not KITE_AVAILABLE:
        return None
    api_key = secret("KITE_API_KEY")
    access_token = secret("KITE_ACCESS_TOKEN")
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
    positions = kite.positions().get("net", [])
    for position in positions:
        if (
            position.get("exchange") == "NFO"
            and position.get("tradingsymbol") == trading_symbol
        ):
            return int(position.get("quantity", 0))
    return 0


def execute_live_order(symbol_name, action, num_lots):
    kite = build_kite()
    if kite is None:
        return {
            "status": "BLOCKED",
            "reason": "Kite credentials/package unavailable",
        }
    if not truthy("LIVE_TRADING_ENABLED"):
        return {
            "status": "BLOCKED",
            "reason": "LIVE_TRADING_ENABLED is not true",
        }

    cfg = SYMBOL_MAP[symbol_name]
    try:
        instrument = resolve_nearest_future(
            kite,
            cfg["future_underlying"],
        )
        trading_symbol = instrument["tradingsymbol"]
        broker_lot_size = int(instrument["lot_size"])
        quantity = int(num_lots) * broker_lot_size

        if quantity <= 0:
            raise RuntimeError("Invalid broker quantity.")

        open_qty = current_open_quantity(
            kite,
            trading_symbol,
        )
        if open_qty != 0:
            raise RuntimeError(
                f"Existing NFO position detected for "
                f"{trading_symbol}: {open_qty}. "
                "New entry blocked."
            )

        transaction_type = (
            kite.TRANSACTION_TYPE_BUY
            if action == "BUY"
            else kite.TRANSACTION_TYPE_SELL
        )

        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=trading_symbol,
            transaction_type=transaction_type,
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
        }

    except Exception as exc:
        return {
            "status": "FAILED",
            "reason": str(exc),
        }


def dispatch_trade(symbol_name, action, row, num_lots, reason):
    now = datetime.now(IST)
    decision_time = row.name + pd.Timedelta(minutes=15)
    age_minutes = (pd.Timestamp(now) - decision_time).total_seconds() / 60.0

    if age_minutes > SIGNAL_MAX_AGE_MINUTES:
        st.error(
            "🛑 Signal is stale. Live/manual dispatch is blocked. "
            f"The signal candle closed {age_minutes:.0f} minutes ago."
        )
        return

    live_enabled = truthy("LIVE_TRADING_ENABLED")

    if live_enabled and not st.session_state.get("live_confirmed", False):
        st.error(
            "Live order blocked. Confirm the live-trading safety checkbox first."
        )
        return

    message = (
        f"🚨 *{symbol_name} {action} SIGNAL*\n\n"
        f"Closed candle: `{row.name:%Y-%m-%d %H:%M IST}`\n"
        f"Price: `₹{row['Close']:.2f}`\n"
        f"RSI: `{row['RSI']:.1f}`\n"
        f"ADX: `{row['ADX']:.1f}`\n"
        f"ATR: `₹{row['ATR']:.2f}`\n"
        f"Reason: {reason}"
    )

    telegram_ok = send_telegram_alert(message)

    if not live_enabled:
        st.info(
            "📝 Paper Mode: Telegram sent; no live order was placed."
            if telegram_ok
            else "📝 Paper Mode: no live order was placed."
        )
        return

    result = execute_live_order(
        symbol_name,
        action,
        num_lots,
    )

    if result["status"] == "LIVE_ORDER_PLACED":
        st.success(
            f"🚀 Order submitted: "
            f"`{result['trading_symbol']}` "
            f"{action} × {result['quantity']} | "
            f"Order ID `{result['order_id']}`"
        )
        send_telegram_alert(
            "🚀 *LIVE ORDER SUBMITTED*\n"
            f"Symbol: `{result['trading_symbol']}`\n"
            f"Action: `{action}`\n"
            f"Quantity: `{result['quantity']}`\n"
            f"Order ID: `{result['order_id']}`",
            show_ui=False,
        )
    else:
        st.error(
            f"Live execution blocked/failed: "
            f"{result.get('reason', 'unknown')}"
        )


# ---------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------

st.sidebar.title("⚙️ Engine Controls")

selected_name = st.sidebar.selectbox(
    "Asset",
    list(SYMBOL_MAP.keys()),
)
cfg = SYMBOL_MAP[selected_name]
ticker = cfg["ticker"]
configured_lot_size = cfg["lot_size"]

capital = st.sidebar.number_input(
    "Research Capital (₹)",
    min_value=10_000.0,
    value=250_000.0,
    step=25_000.0,
)

num_lots = st.sidebar.number_input(
    "Lots",
    min_value=1,
    max_value=20,
    value=1,
    step=1,
)

st.sidebar.markdown("### Strategy")
rsi_oversold = st.sidebar.slider(
    "RSI Oversold",
    25,
    45,
    38,
)
rsi_overbought = st.sidebar.slider(
    "RSI Overbought",
    55,
    75,
    62,
)
use_daily_trend_filter = st.sidebar.checkbox(
    "Use Daily EMA50 trend filter",
    value=True,
    help=(
        "Uses the actual 50-day EMA from completed daily candles. "
        "This is NOT the same as the 15-minute EMA50."
    ),
)

st.sidebar.caption(
    f"ADX < {ADX_MAX_DEFAULT} • "
    f"SL {ATR_SL_MULTIPLIER}× ATR • "
    f"Target {ATR_TARGET_MULTIPLIER}× ATR"
)
st.sidebar.caption(
    f"Last signal candle must end by {ENTRY_CUTOFF:%H:%M IST}"
)
st.sidebar.caption(
    f"EOD square-off: {EOD_SQUAREOFF:%H:%M IST}"
)

st.sidebar.markdown("---")
st.sidebar.markdown("### 🔔 Notifications")

if telegram_configured():
    st.sidebar.success("Telegram: Configured")
else:
    st.sidebar.warning("Telegram: Not configured")

if st.sidebar.button("🔔 Test Telegram Alert"):
    test_telegram()

st.sidebar.markdown("---")
st.sidebar.markdown("### 🛡️ Execution Safety")

live_enabled = truthy("LIVE_TRADING_ENABLED")

if live_enabled:
    st.sidebar.error("LIVE TRADING: ENABLED")
    st.session_state.live_confirmed = st.sidebar.checkbox(
        "I understand this can place real NFO orders.",
        value=False,
    )
else:
    st.sidebar.success("PAPER MODE: Active")
    st.session_state.live_confirmed = False


# ---------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------

st.title("⚡ Quant Strategy & Execution Engine")
st.caption(
    "15-minute VWAP mean-reversion • RSI + ADX confirmation • "
    "Daily trend regime • ATR risk management"
)

data = fetch_and_prepare_data(
    ticker=ticker,
    period="1mo",
    interval="15m",
)

if data.empty:
    st.error(
        f"🛑 Reliable market data is unavailable for {selected_name}."
    )
    st.warning(
        "No signal, backtest, or live order is generated when "
        "market data cannot be validated."
    )
    st.stop()

closed = se.get_last_closed_candles(data)

if len(closed) < 2:
    st.warning("Waiting for a fully closed 15-minute candle.")
    st.stop()

latest = closed.iloc[-1]
previous = closed.iloc[-2]

signal_result = evaluate_signal(
    data,
    rsi_oversold=rsi_oversold,
    rsi_overbought=rsi_overbought,
    adx_max=ADX_MAX_DEFAULT,
    require_closed=True,
    use_daily_trend_filter=use_daily_trend_filter,
)

signal = signal_result["signal"]
data_source = data.attrs.get("data_source", "UNKNOWN")
degraded = bool(data.attrs.get("degraded", False))

if degraded:
    signal = "HOLD"
    signal_result = dict(signal_result)
    signal_result["reason"] = "DEGRADED_PROXY_DATA"


# ---------------------------------------------------------------------
# Market monitor
# ---------------------------------------------------------------------

st.subheader(f"📌 {selected_name} — Live Market Monitor")

c1, c2, c3 = st.columns(3)
c1.metric(
    "Last Closed Price",
    f"₹{latest['Close']:.2f}",
)
c2.metric(
    "VWAP",
    f"₹{latest['VWAP']:.2f}",
)
c3.metric(
    "Daily Trend",
    latest["Daily_Trend"],
)

c4, c5, c6 = st.columns(3)
c4.metric(
    "RSI (14)",
    f"{latest['RSI']:.1f}",
)
c5.metric(
    "ADX (14)",
    f"{latest['ADX']:.1f}",
)
c6.metric(
    "ATR (14)",
    f"₹{latest['ATR']:.2f}",
)

st.caption(
    f"Data source: `{data_source}` • "
    f"Closed candle: `{latest.name:%d-%b-%Y %H:%M IST}` • "
    f"15m EMA50: `₹{latest['EMA50_15m']:.2f}` • "
    f"Daily EMA50: "
    f"`₹{latest['Daily_EMA50']:.2f}`"
    if pd.notna(latest["Daily_EMA50"])
    else
    f"Data source: `{data_source}` • "
    f"Closed candle: `{latest.name:%d-%b-%Y %H:%M IST}` • "
    f"15m EMA50: `₹{latest['EMA50_15m']:.2f}` • "
    "Daily EMA50: `UNAVAILABLE`"
)

if degraded:
    st.warning(
        "⚠️ Proxy/degraded data is being displayed. "
        "Signal generation and execution are disabled."
    )

if signal == "BUY":
    st.success(
        f"🟢 **BUY SIGNAL** — ₹{latest['Close']:.2f}\n\n"
        f"{signal_result['reason']}"
    )
elif signal == "SELL":
    st.error(
        f"🔴 **SELL SIGNAL** — ₹{latest['Close']:.2f}\n\n"
        f"{signal_result['reason']}"
    )
elif signal_result["reason"] == "ENTRY_WINDOW_CLOSED":
    st.warning(
        "⏱️ Entry window closed. No new trades are permitted."
    )
else:
    st.info(
        "⚪ **HOLD** — all entry conditions are not simultaneously satisfied."
    )


# ---------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------

with st.expander("🔍 Signal Diagnostics", expanded=False):
    decision_end = latest.name + pd.Timedelta(minutes=15)

    diagnostics = pd.DataFrame(
        [
            [
                "Previous close below VWAP Lower",
                bool(previous["Close"] < previous["VWAP_Lower"]),
            ],
            [
                "Previous close above VWAP Upper",
                bool(previous["Close"] > previous["VWAP_Upper"]),
            ],
            [
                "Current candle bullish",
                bool(latest["Close"] > latest["Open"]),
            ],
            [
                "Current candle bearish",
                bool(latest["Close"] < latest["Open"]),
            ],
            [
                f"RSI < {rsi_oversold}",
                bool(latest["RSI"] < rsi_oversold),
            ],
            [
                f"RSI > {rsi_overbought}",
                bool(latest["RSI"] > rsi_overbought),
            ],
            [
                f"ADX < {ADX_MAX_DEFAULT}",
                bool(latest["ADX"] < ADX_MAX_DEFAULT),
            ],
            [
                "Daily trend filter",
                (
                    latest["Daily_Trend"]
                    if use_daily_trend_filter
                    else "DISABLED"
                ),
            ],
            [
                "Entry candle ends by 14:45",
                bool(decision_end.time() <= ENTRY_CUTOFF),
            ],
            [
                "Primary market data",
                "YES" if not degraded else "NO",
            ],
        ],
        columns=["Condition", "Status"],
    )

    st.dataframe(
        diagnostics,
        width="stretch",
        hide_index=True,
    )


# ---------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------

chart = data.tail(120)

fig = go.Figure()

fig.add_trace(
    go.Candlestick(
        x=chart.index,
        open=chart["Open"],
        high=chart["High"],
        low=chart["Low"],
        close=chart["Close"],
        name="15m",
    )
)

fig.add_trace(
    go.Scatter(
        x=chart.index,
        y=chart["VWAP"],
        name="VWAP",
        mode="lines",
    )
)

fig.add_trace(
    go.Scatter(
        x=chart.index,
        y=chart["VWAP_Upper"],
        name="VWAP Upper",
        mode="lines",
        line=dict(dash="dash"),
    )
)

fig.add_trace(
    go.Scatter(
        x=chart.index,
        y=chart["VWAP_Lower"],
        name="VWAP Lower",
        mode="lines",
        line=dict(dash="dash"),
    )
)

fig.add_trace(
    go.Scatter(
        x=chart.index,
        y=chart["EMA50_15m"],
        name="15m EMA50",
        mode="lines",
    )
)

fig.update_xaxes(
    rangebreaks=[
        dict(bounds=["sat", "mon"]),
        dict(bounds=[15.5, 9.25], pattern="hour"),
    ]
)

fig.update_layout(
    height=500,
    xaxis_title="Time (IST)",
    yaxis_title="Price (₹)",
    template="plotly_dark",
    margin=dict(l=10, r=10, t=45, b=10),
)

st.plotly_chart(
    fig,
    width="stretch",
)


# ---------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------

if signal in {"BUY", "SELL"} and not degraded:
    st.markdown("---")
    st.subheader("🚦 Trade Dispatch")

    estimated_qty = int(num_lots) * int(configured_lot_size)

    if live_enabled:
        st.warning(
            "Live trading is enabled. The current nearest NFO "
            "futures contract and broker lot size will be resolved dynamically."
        )
    else:
        st.info(
            f"Paper Mode is active. Dispatch sends Telegram only. "
            f"Configured research quantity: {estimated_qty}"
        )

    if st.button(
        f"Dispatch {signal} — Telegram + Execution",
        type="primary",
    ):
        dispatch_trade(
            selected_name,
            signal,
            latest,
            int(num_lots),
            signal_result["reason"],
        )


# ---------------------------------------------------------------------
# Strategy statistics
# ---------------------------------------------------------------------

st.markdown("---")
st.subheader("📊 Strategy Statistics")

if degraded:
    st.info(
        "Statistics are disabled while only degraded/proxy data is available."
    )
else:
    trades = run_institutional_backtest(
        data,
        rsi_oversold=rsi_oversold,
        rsi_overbought=rsi_overbought,
        adx_max=ADX_MAX_DEFAULT,
        sl_atr_mult=ATR_SL_MULTIPLIER,
        tgt_atr_mult=ATR_TARGET_MULTIPLIER,
        num_lots=int(num_lots),
        lot_size=configured_lot_size,
        use_daily_trend_filter=use_daily_trend_filter,
    )

    if trades.empty:
        st.info(
            "No completed backtest trades in the available 1-month dataset."
        )
    else:
        stats = calculate_strategy_statistics(
            trades,
            capital=capital,
        )

        if stats["status"] == "INSUFFICIENT_SAMPLE":
            st.warning(
                f"⚠️ Only {stats['sample_size']} completed trades. "
                f"Do not treat win rate/expectancy as reliable yet. "
                f"Research target: at least {RELIABLE_BACKTEST_TRADES} trades."
            )
        elif stats["status"] == "PRELIMINARY":
            st.info(
                f"🟡 {stats['sample_size']} completed trades. "
                "Statistics are preliminary; continue accumulating data."
            )
        else:
            st.success(
                f"🟢 {stats['sample_size']} completed trades. "
                "Sample is large enough for preliminary statistical evaluation."
            )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric(
            "Completed Trades",
            stats["sample_size"],
        )
        m2.metric(
            "Win Rate",
            f"{stats['win_rate']:.1f}%",
        )
        m3.metric(
            "Profit Factor",
            (
                "∞"
                if math.isinf(stats["profit_factor"])
                else f"{stats['profit_factor']:.2f}"
            ),
        )
        m4.metric(
            "Expectancy / Trade",
            f"₹{stats['expectancy']:,.0f}",
        )

        m5, m6, m7, m8 = st.columns(4)
        m5.metric(
            "Avg Winner",
            f"₹{stats['avg_winner']:,.0f}",
        )
        m6.metric(
            "Avg Loser",
            f"₹{stats['avg_loser']:,.0f}",
        )
        m7.metric(
            "Net P&L",
            f"₹{stats['net_pnl']:,.0f}",
        )
        m8.metric(
            "Return on Capital",
            f"{stats['return_pct']:+.2f}%",
        )

        m9, m10, m11, m12 = st.columns(4)
        m9.metric(
            "Max Drawdown",
            f"₹{stats['max_drawdown']:,.0f}",
        )
        m10.metric(
            "Max DD %",
            f"{stats['max_drawdown_pct']:.2f}%",
        )
        m11.metric(
            "Max Losing Streak",
            stats["max_losing_streak"],
        )
        m12.metric(
            "Avg Trades / Month",
            f"{stats['avg_trades_per_month']:.1f}",
        )

        st.markdown("#### Monthly / risk context")

        monthly_context = pd.DataFrame(
            {
                "Metric": [
                    "Best Month",
                    "Worst Month",
                    "Average R Multiple",
                    "Sample Quality",
                ],
                "Value": [
                    (
                        f"₹{stats['best_month']:,.0f}"
                        if pd.notna(stats["best_month"])
                        else "N/A"
                    ),
                    (
                        f"₹{stats['worst_month']:,.0f}"
                        if pd.notna(stats["worst_month"])
                        else "N/A"
                    ),
                    (
                        f"{stats['avg_r_multiple']:.2f}R"
                        if pd.notna(stats["avg_r_multiple"])
                        else "N/A"
                    ),
                    stats["status"],
                ],
            }
        )
        st.dataframe(
            monthly_context,
            width="stretch",
            hide_index=True,
        )

        st.download_button(
            "📥 Download Backtest Trades",
            data=export_trades_to_excel(trades),
            file_name=f"{selected_name}_15m_backtest.xlsx",
            mime=(
                "application/"
                "vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
        )

        with st.expander("📋 Completed Backtest Trades"):
            st.dataframe(
                trades,
                width="stretch",
                hide_index=True,
            )


# ---------------------------------------------------------------------
# Research Lab
# ---------------------------------------------------------------------

st.markdown("---")
st.subheader("🧪 Research Lab — Out-of-Sample Backtest")

st.caption(
    "Yahoo Finance cannot reliably supply years of 15-minute history on demand. "
    "Upload a clean 15-minute OHLCV CSV for serious multi-year research. "
    "The uploaded file is processed locally and is not sent to Yahoo."
)

uploaded = st.file_uploader(
    "Upload 15-minute OHLCV CSV",
    type=["csv"],
    help=(
        "Required columns: Date/Datetime, Open, High, Low, Close. "
        "Volume is recommended."
    ),
)

if uploaded is not None:
    try:
        raw = pd.read_csv(uploaded)

        datetime_column = next(
            (
                col
                for col in raw.columns
                if str(col).strip().lower()
                in {
                    "datetime",
                    "date",
                    "timestamp",
                    "time",
                }
            ),
            None,
        )

        if datetime_column is None:
            st.error(
                "CSV needs a Date/Datetime/Timestamp column."
            )
        else:
            raw[datetime_column] = pd.to_datetime(
                raw[datetime_column],
                errors="coerce",
            )
            raw = raw.dropna(
                subset=[datetime_column]
            ).set_index(datetime_column)

            raw.columns = [
                str(c).strip().title()
                for c in raw.columns
            ]

            research = prepare_research_data(raw)

            if research.empty:
                st.error(
                    "Unable to prepare the research dataset. "
                    "Check the CSV columns and timestamps."
                )
            else:
                st.success(
                    f"Research dataset loaded: "
                    f"{len(research):,} usable 15-minute candles."
                )

                research_trades = run_institutional_backtest(
                    research,
                    rsi_oversold=rsi_oversold,
                    rsi_overbought=rsi_overbought,
                    adx_max=ADX_MAX_DEFAULT,
                    sl_atr_mult=ATR_SL_MULTIPLIER,
                    tgt_atr_mult=ATR_TARGET_MULTIPLIER,
                    num_lots=int(num_lots),
                    lot_size=configured_lot_size,
                    use_daily_trend_filter=use_daily_trend_filter,
                )

                if research_trades.empty:
                    st.warning(
                        "No completed trades were generated by the current rules."
                    )
                else:
                    research_stats = calculate_strategy_statistics(
                        research_trades,
                        capital=capital,
                    )

                    r1, r2, r3, r4 = st.columns(4)
                    r1.metric(
                        "Research Trades",
                        research_stats["sample_size"],
                    )
                    r2.metric(
                        "Win Rate",
                        f"{research_stats['win_rate']:.1f}%",
                    )
                    r3.metric(
                        "Profit Factor",
                        (
                            "∞"
                            if research_stats["profit_factor"] == float("inf")
                            else f"{research_stats['profit_factor']:.2f}"
                        ),
                    )
                    r4.metric(
                        "Expectancy",
                        f"₹{research_stats['expectancy']:,.0f}",
                    )

                    st.caption(
                        "Research backtest uses signal-on-candle-close → "
                        "next-candle-open execution, slippage, charges, "
                        "maximum two trades/day, and conservative same-candle "
                        "stop/target ordering."
                    )

                    st.dataframe(
                        research_trades,
                        width="stretch",
                        hide_index=True,
                    )

    except Exception as exc:
        st.error(
            f"Research file could not be processed: {exc}"
        )
