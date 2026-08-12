from datetime import time
import io
import os
import pandas as pd
import plotly.graph_objects as go
import pytz
import requests
import streamlit as st
from strategy_engine import (
    SYMBOL_MAP,
    export_trades_to_excel,
    fetch_and_prepare_data,
    generate_monthly_breakdown,
    run_institutional_backtest,
)

# Optional import for Zerodha KiteConnect with fallback
try:
    from kiteconnect import KiteConnect

    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False


# ------------------------------------------------------------------
# EXECUTION ENGINE FUNCTIONS (Telegram & Zerodha Kite)
# ------------------------------------------------------------------
def send_telegram_alert(message: str) -> bool:
    """Dispatches Markdown alerts to Telegram channel/bot."""
    bot_token = st.secrets.get(
        "TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    chat_id = st.secrets.get(
        "TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", "")
    )

    if not bot_token or not chat_id:
        st.warning(
            "⚠️ [LOG] Telegram Bot Token or Chat ID not configured in Streamlit Secrets."
        )
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        res = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=5,
        )
        if res.status_code == 200:
            st.info("📨 [LOG] Telegram alert sent successfully.")
            return True
        else:
            st.error(f"❌ [LOG] Telegram Error: {res.status_code} - {res.text}")
            return False
    except Exception as e:
        st.error(f"❌ [LOG] Telegram Exception: {e}")
        return False


def execute_auto_trade(
    symbol: str,
    action: str,
    price: float,
    num_lots: int,
    lot_size: int = 65,
    reason: str = "",
):
    """Unified Order Execution: Dispatches Telegram alert and attempts Kite Live Order."""
    total_qty = num_lots * lot_size

    # 1. Dispatch Telegram Alert
    alert_msg = (
        f"🚨 *{symbol} {action} SIGNAL DETECTED* ⚡\n\n"
        f"📌 *Asset:* {symbol}\n"
        f"📈 *Signal Price:* ₹{price:.2f}\n"
        f"📦 *Quantity:* {total_qty} ({num_lots} Lot{'s' if num_lots > 1 else ''} @ {lot_size}/lot)\n"
        f"💡 *Trigger Reason:* {reason}"
    )
    send_telegram_alert(alert_msg)

    # 2. Kite Live Order Execution (with Paper Fallback)
    api_key = st.secrets.get("KITE_API_KEY", os.getenv("KITE_API_KEY", ""))
    access_token = st.secrets.get(
        "KITE_ACCESS_TOKEN", os.getenv("KITE_ACCESS_TOKEN", "")
    )

    if KITE_AVAILABLE and api_key and access_token:
        try:
            kite = KiteConnect(api_key=api_key)
            kite.set_access_token(access_token)

            tx_type = (
                kite.TRANSACTION_TYPE_BUY
                if action == "BUY"
                else kite.TRANSACTION_TYPE_SELL
            )
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol="NIFTY24AUGFUT",
                transaction_type=tx_type,
                quantity=total_qty,
                product=kite.PRODUCT_MIS,
                order_type=kite.ORDER_TYPE_MARKET,
            )
            st.success(f"🚀 Live Kite Order Placed! Order ID: `{order_id}`")
            send_telegram_alert(
                f"🚀 *LIVE ORDER EXECUTED*\nOrder ID: `{order_id}` | Qty: {total_qty}"
            )
            return {"status": "LIVE_SUCCESS", "order_id": order_id}
        except Exception as e:
            st.error(f"❌ Kite Order Error: {e}")
            return {"status": "FAILED", "reason": str(e)}
    else:
        st.warning(
            "ℹ️ [LOG] Zerodha Kite credentials or package missing. Execution logged in Paper Mode."
        )
        return {"status": "PAPER_LOGGED"}


# ------------------------------------------------------------------
# STREAMLIT UI DASHBOARD
# ------------------------------------------------------------------
st.set_page_config(
    page_title="Quant Strategy & Execution Engine", page_icon="⚡", layout="wide"
)

st.title("⚡ Quant Strategy & Execution Engine (Indices & Stocks)")

# Sidebar Symbol & Capital Selection
st.sidebar.header("🎯 Asset & Capital Configuration")
selected_symbol_name = st.sidebar.selectbox(
    "Select Asset / Stock", list(SYMBOL_MAP.keys())
)
selected_asset_info = SYMBOL_MAP[selected_symbol_name]
ticker = selected_asset_info["ticker"]
default_lot_size = selected_asset_info["lot_size"]

capital = st.sidebar.number_input(
    "Trading Capital (₹)", value=250000.0, step=25000.0, format="%.2f"
)
num_lots = st.sidebar.slider(
    "Number of Lots", min_value=1, max_value=20, value=1
)
st.sidebar.caption(
    f"Current Lot Size for {selected_symbol_name}: **{default_lot_size} shares**"
)

st.sidebar.markdown("---")
st.sidebar.header("⚙️ Strategy Parameters")
rsi_oversold = st.sidebar.slider("RSI Oversold Filter", 25, 45, 38)
rsi_overbought = st.sidebar.slider("RSI Overbought Filter", 55, 75, 62)

# Load Historical Market Data (Limited to max 60d for 15m intraday data)
data = fetch_and_prepare_data(ticker=ticker, period="60d", interval="15m")

if data.empty:
    st.error(
        f"❌ Failed to fetch market data for {selected_symbol_name}. Data range might exceed 60-day intraday limit."
    )
    st.stop()

one_month_data = data.tail(22 * 25)

# ------------------------------------------------------------------
# SECTION 1: OPEN TRADES & LIVE CHART
# ------------------------------------------------------------------
st.subheader(f"📌 Active Market Monitor — {selected_symbol_name}")

latest = data.iloc[-1]
last_time_ist = latest.name.strftime("%Y-%m-%d %H:%M IST")
trend_state = (
    "BULLISH 🟢" if latest["Close"] > latest["Daily_EMA50"] else "BEARISH 🔴"
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Close Price", f"₹{latest['Close']:.2f}")
c2.metric("VWAP", f"₹{latest['VWAP']:.2f}")
c3.metric("RSI (14)", f"{latest['RSI']:.1f}")
c4.metric("Daily Trend", trend_state)

# Signal Evaluation
latest_signal = "HOLD"
if latest["Close"] < latest["VWAP_Lower"] and latest["RSI"] < rsi_oversold:
    latest_signal = "BUY"
elif latest["Close"] > latest["VWAP_Upper"] and latest["RSI"] > rsi_overbought:
    latest_signal = "SELL"

if latest_signal == "BUY":
    st.success(
        f"🟢 **BUY SIGNAL ACTIVE** at ₹{latest['Close']:.2f} | VWAP Lower: ₹{latest['VWAP_Lower']:.2f}"
    )
    if st.button("Dispatch Auto-Trade (Telegram + Kite)"):
        execute_auto_trade(
            selected_symbol_name,
            "BUY",
            latest["Close"],
            num_lots,
            default_lot_size,
            "RSI Oversold + VWAP Dip",
        )

elif latest_signal == "SELL":
    st.error(
        f"🔴 **SELL SIGNAL ACTIVE** at ₹{latest['Close']:.2f} | VWAP Upper: ₹{latest['VWAP_Upper']:.2f}"
    )
    if st.button("Dispatch Auto-Trade (Telegram + Kite)"):
        execute_auto_trade(
            selected_symbol_name,
            "SELL",
            latest["Close"],
            num_lots,
            default_lot_size,
            "RSI Overbought + VWAP Spike",
        )

else:
    st.info(
        "⚪ **No active entry signals on the current candle.** Strategy Status: HOLD"
    )

# Plotly Active Hours Chart
recent_chart = data.tail(120)
min_y = float(recent_chart["Low"].min()) - 10.0
max_y = float(recent_chart["High"].max()) + 10.0

fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=recent_chart.index,
        y=recent_chart["Close"],
        mode="lines",
        name="Close",
        line=dict(color="#1f77b4", width=2),
    )
)
fig.add_trace(
    go.Scatter(
        x=recent_chart.index,
        y=recent_chart["VWAP"],
        mode="lines",
        name="VWAP",
        line=dict(color="#ff7f0e", width=1.5),
    )
)
fig.add_trace(
    go.Scatter(
        x=recent_chart.index,
        y=recent_chart["VWAP_Upper"],
        mode="lines",
        name="VWAP Upper",
        line=dict(color="#d62728", width=1, dash="dash"),
    )
)
fig.add_trace(
    go.Scatter(
        x=recent_chart.index,
        y=recent_chart["VWAP_Lower"],
        mode="lines",
        name="VWAP Lower",
        line=dict(color="#2ca02c", width=1, dash="dash"),
    )
)

fig.update_xaxes(
    rangebreaks=[
        dict(bounds=["sat", "mon"]),
        dict(bounds=[15.5, 9.25], pattern="hour"),
    ]
)
fig.update_layout(
    title=f"{selected_symbol_name} Active Market Sessions (09:15 - 15:30 IST) — {last_time_ist}",
    yaxis=dict(range=[min_y, max_y], title="Price (₹)"),
    xaxis=dict(title="Time (IST)"),
    template="plotly_dark",
    height=420,
)
st.plotly_chart(fig, width="stretch")

# ------------------------------------------------------------------
# SECTION 2: 1-MONTH PERFORMANCE ANALYTICS & EXCEL DOWNLOAD
# ------------------------------------------------------------------
st.markdown("---")
st.subheader("📊 Current Month Executed Trades & Performance")

trades_1m = run_institutional_backtest(
    one_month_data,
    rsi_oversold=rsi_oversold,
    rsi_overbought=rsi_overbought,
    num_lots=num_lots,
    lot_size=default_lot_size,
)

if not trades_1m.empty:
    total_trades_1m = len(trades_1m)
    win_rate_1m = (
        len(trades_1m[trades_1m["NetPnL"] > 0]) / total_trades_1m
    ) * 100
    net_pnl_1m = trades_1m["NetPnL"].sum()
    return_on_capital_1m = (net_pnl_1m / capital) * 100

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Trades (1Mo)", total_trades_1m)
    m2.metric("Win Rate %", f"{win_rate_1m:.1f}%")
    m3.metric("Net Profit / Loss", f"₹{net_pnl_1m:,.2f}")
    m4.metric("1-Mo Return on Capital", f"{return_on_capital_1m:+.2f}%")

    display_df = trades_1m.copy()
    display_df["Type"] = display_df["Type"].apply(
        lambda x: "🟢 BUY" if x == "BUY" else "🔴 SELL"
    )

    # Excel Download Button
    excel_bytes = export_trades_to_excel(trades_1m)
    st.download_button(
        label="📥 Download Executed Trades (Excel)",
        data=excel_bytes,
        file_name=f"{selected_symbol_name}_Executed_Trades_Current_Month.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.dataframe(
        display_df,
        width="stretch",
        column_config={
            "Charges": st.column_config.NumberColumn(
                "Charges (₹)",
                help="Estimated brokerage & exchange charges",
                format="₹%.2f",
            )
        },
    )
else:
    st.info("No trades triggered during the current month period.")

# ------------------------------------------------------------------
# SECTION 3: 12-MONTH MONTHLY BREAKDOWN
# ------------------------------------------------------------------

st.markdown("---")
st.subheader("🗓️ Last 12 Months Performance Breakdown")

# 1. Run live 60-day backtest
trades_60d = run_institutional_backtest(
    data,
    rsi_oversold=rsi_oversold,
    rsi_overbought=rsi_overbought,
    num_lots=num_lots,
    lot_size=default_lot_size,
)

# 2. Merge with repo's saved historical trades
trades_12m = get_combined_12m_trades(trades_60d)

if not trades_12m.empty:
    monthly_table = generate_monthly_breakdown(trades_12m, capital)

    st.dataframe(monthly_table, width="stretch", hide_index=True)

    # Plot Monthly PnL Bar Chart
    trades_12m["YearMonth"] = pd.to_datetime(
        trades_12m["ExitTime"]
    ).dt.strftime("%b %Y")
    monthly_pnl_series = trades_12m.groupby("YearMonth", sort=False)[
        "NetPnL"
    ].sum()

    colors = [
        "#2ecc71" if val >= 0 else "#e74c3c"
        for val in monthly_pnl_series.values
    ]

    fig_bar = go.Figure(
        data=[
            go.Bar(
                x=monthly_pnl_series.index,
                y=monthly_pnl_series.values,
                marker_color=colors,
            )
        ]
    )
    fig_bar.update_layout(
        title="Monthly Net Profit / Loss Breakdown (₹)",
        xaxis_title="Month",
        yaxis_title="Net PnL (₹)",
        template="plotly_dark",
        height=380,
    )
    st.plotly_chart(fig_bar, width="stretch")
else:
    st.info("No historical trade records found.")
    
