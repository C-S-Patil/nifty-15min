from datetime import time
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

st.set_page_config(
    page_title="Nifty & Stocks Quant Engine", page_icon="⚡", layout="wide"
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

# Load Historical Data for Selected Asset
data = fetch_and_prepare_data(ticker=ticker, period="1y")

if data.empty:
    st.error(f"❌ Failed to fetch market data for {selected_symbol_name}.")
    st.stop()

one_month_data = data.tail(22 * 25)

# ------------------------------------------------------------------
# SECTION 1: ACTIVE POSITION & CHART
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
st.plotly_chart(fig, use_container_width=True)

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
        use_container_width=True,
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

trades_12m = run_institutional_backtest(
    data,
    rsi_oversold=rsi_oversold,
    rsi_overbought=rsi_overbought,
    num_lots=num_lots,
    lot_size=default_lot_size,
)

if not trades_12m.empty:
    monthly_table = generate_monthly_breakdown(trades_12m, capital)
    st.dataframe(monthly_table, use_container_width=True, hide_index=True)
    
