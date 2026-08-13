
from __future__ import annotations

import json
import os
from datetime import datetime, time
from pathlib import Path

import requests

from strategy_engine import (
    ADX_MAX_DEFAULT,
    ATR_SL_MULTIPLIER,
    ATR_TARGET_MULTIPLIER,
    ENTRY_CUTOFF,
    IST,
    MAX_TRADES_PER_DAY,
    RSI_OVERBOUGHT_DEFAULT,
    RSI_OVERSOLD_DEFAULT,
    SYMBOL_MAP,
    evaluate_signal,
    fetch_and_prepare_data,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path("data/scanner_state.json")


def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Telegram credentials are missing.")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        payload = response.json()
        if response.status_code != 200 or not payload.get("ok"):
            print(
                f"ERROR: Telegram failed: "
                f"HTTP {response.status_code} "
                f"{payload.get('description', response.text[:300])}"
            )
            return False
        return True
    except Exception as exc:
        print(f"ERROR: Telegram request failed: {exc}")
        return False


def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
    except Exception as exc:
        print(f"WARNING: state read failed: {exc}")
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )
    temp.replace(STATE_FILE)


def symbols_to_scan():
    requested = os.getenv(
        "SCAN_SYMBOLS",
        "Nifty 50,Bank Nifty",
    )
    names = [
        item.strip()
        for item in requested.split(",")
        if item.strip()
    ]
    valid = [
        name
        for name in names
        if name in SYMBOL_MAP
    ]
    return valid or ["Nifty 50", "Bank Nifty"]


def run_scanner():
    now = datetime.now(IST)
    print(
        f"Scanner time: "
        f"{now:%Y-%m-%d %H:%M:%S %Z}"
    )

    # GitHub Actions schedules can drift by a few seconds/minutes.
    # The data engine itself only evaluates fully closed 15m candles.
    if (
        now.weekday() >= 5
        or not (
            time(9, 15)
            <= now.time()
            <= time(15, 30)
        )
    ):
        print(
            "Outside NSE market hours. No scan."
        )
        return 0

    state = load_state()
    today_key = now.strftime("%Y-%m-%d")
    daily_counts = state.get("daily_trade_counts", {})

    # Keep state small.
    daily_counts = {
        key: value
        for key, value in daily_counts.items()
        if key.startswith(today_key + "|")
    }

    sent_any = False

    for name in symbols_to_scan():
        cfg = SYMBOL_MAP[name]

        print(
            f"Scanning {name} "
            f"({cfg['ticker']})..."
        )

        df = fetch_and_prepare_data(
            cfg["ticker"],
            period="1mo",
            interval="15m",
        )

        if df.empty:
            print(
                f"{name}: MARKET DATA UNAVAILABLE. "
                "No signal."
            )
            continue

        if bool(
            df.attrs.get("degraded", False)
        ):
            print(
                f"{name}: DEGRADED/PROXY DATA. "
                "Automated signal disabled."
            )
            continue

        result = evaluate_signal(
            df,
            rsi_oversold=RSI_OVERSOLD_DEFAULT,
            rsi_overbought=RSI_OVERBOUGHT_DEFAULT,
            adx_max=ADX_MAX_DEFAULT,
            require_closed=True,
            use_daily_trend_filter=True,
        )

        signal = result["signal"]

        if signal == "HOLD":
            print(
                f"{name}: HOLD — "
                f"{result['reason']}"
            )
            continue

        row = result["row"]
        decision_time = result["decision_time"]

        if decision_time.time() > ENTRY_CUTOFF:
            print(
                f"{name}: signal rejected after "
                "entry cutoff."
            )
            continue

        candle_key = (
            f"{name}|{signal}|"
            f"{row.name.isoformat()}"
        )

        if state.get("last_alert_key") == candle_key:
            print(
                f"{name}: duplicate signal "
                "suppressed."
            )
            continue

        count_key = f"{today_key}|{name}"
        count = int(
            daily_counts.get(count_key, 0)
        )

        if count >= MAX_TRADES_PER_DAY:
            print(
                f"{name}: daily trade limit "
                f"({MAX_TRADES_PER_DAY}) reached."
            )
            continue

        message = (
            f"🚨 *AUTOMATED {name} "
            f"{signal} SIGNAL* ⚡\n\n"
            f"🕯️ Closed candle: "
            f"`{row.name:%Y-%m-%d %H:%M IST}`\n"
            f"🧭 Decision: "
            f"`{decision_time:%H:%M IST}`\n"
            f"💰 Price: "
            f"`₹{row['Close']:.2f}`\n"
            f"📊 RSI(14): "
            f"`{row['RSI']:.2f}`\n"
            f"📉 ADX(14): "
            f"`{row['ADX']:.2f}`\n"
            f"📈 VWAP: "
            f"`₹{row['VWAP']:.2f}`\n"
            f"📏 Daily EMA50: "
            f"`₹{row['Daily_EMA50']:.2f}`\n"
            f"🛡️ Initial SL: "
            f"`{ATR_SL_MULTIPLIER} × ATR`\n"
            f"🎯 Target: "
            f"`{ATR_TARGET_MULTIPLIER} × ATR`\n\n"
            f"💡 {result['reason']}\n\n"
            "⚠️ Signal only — GitHub Actions "
            "does not place live orders."
        )

        if not send_telegram_alert(message):
            print(
                "Telegram delivery failed; "
                "state not advanced."
            )
            continue

        daily_counts[count_key] = count + 1
        state["last_alert_key"] = candle_key
        state["last_alert_at"] = now.isoformat()
        state["daily_trade_counts"] = daily_counts
        save_state(state)

        sent_any = True
        print(
            f"ALERT SENT: {candle_key}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(run_scanner())
