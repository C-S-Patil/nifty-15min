def run_option_selling_backtest(
    df: pd.DataFrame,
    lot_size: int = 75,
    margin_per_spread: float = 40000.0,  # Hedged Credit Spread Margin
    avg_premium_collected: float = 45.0,  # Points collected on OTM Spread
    max_risk_points: float = 30.0,  # Defined Stop Loss in Points
) -> pd.DataFrame:
    """Simulates Intraday Credit Spread Option Selling (Bull Put / Bear Call)."""
    trades = []
    in_position = False
    pos_type = None
    entry_price = 0.0
    entry_time = None

    for i in range(1, len(df)):
        row = df.iloc[i]
        current_time = row.name.time()

        if in_position:
            close = row["Close"]
            exit_triggered = False
            pnl_points = 0.0
            exit_reason = ""

            # Force EOD Intraday Exit at 03:15 PM IST (Capture Theta)
            if current_time >= pd.to_datetime("15:15").time():
                exit_triggered = True
                pnl_points = avg_premium_collected * 0.8  # Collect 80% decay
                exit_reason = "EOD Decay Profit Captured"

            # Check Stop Loss Trigger
            elif (pos_type == "BULL_PUT_SPREAD" and close < entry_price - 80) or (
                pos_type == "BEAR_CALL_SPREAD" and close > entry_price + 80
            ):
                exit_triggered = True
                pnl_points = -max_risk_points
                exit_reason = "Max Risk SL Hit"

            if exit_triggered:
                net_pnl = pnl_points * lot_size
                trades.append(
                    {
                        "Strategy": pos_type,
                        "EntryTime": entry_time,
                        "ExitTime": row.name,
                        "PnL": net_pnl,
                        "Reason": exit_reason,
                    }
                )
                in_position = False

        # Signal Entry
        if not in_position and current_time < pd.to_datetime("14:45").time():
            if (
                row["Close"] > row["Daily_EMA50"]
                and row["Close"] < row["VWAP_Lower"]
                and row["RSI"] < 38
            ):
                in_position = True
                pos_type = "BULL_PUT_SPREAD"
                entry_price = row["Close"]
                entry_time = row.name

            elif (
                row["Close"] < row["Daily_EMA50"]
                and row["Close"] > row["VWAP_Upper"]
                and row["RSI"] > 62
            ):
                in_position = True
                pos_type = "BEAR_CALL_SPREAD"
                entry_price = row["Close"]
                entry_time = row.name

    return pd.DataFrame(trades)
