import json
import os
from datetime import datetime, time
from pathlib import Path

import requests

from strategy_engine import (
    IST,
    SYMBOL_MAP,
    ADX_MAX_DEFAULT,
    RSI_OVERBOUGHT_DEFAULT,
    RSI_OVERSOLD_DEFAULT,
    fetch_and_prepare_data,
    evaluate_signal,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path("data/scanner_state.json")

def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Telegram credentials are missing.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"ERROR: Telegram HTTP {r.status_code}: {r.text[:500]}")
            return False
        return bool(r.json().get("ok", False))
    except requests.RequestException as exc:
        print(f"ERROR: Telegram request failed: {exc}")
        return False
    except ValueError:
        return False

def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: state read failed: {exc}")
    return {}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)

def run_scanner():
    now = datetime.now(IST)
    print(f"Scanner time: {now:%Y-%m-%d %H:%M:%S %Z}")

    if now.weekday() >= 5 or not (time(9, 15) <= now.time() <= time(15, 30)):
        print("Outside NSE market hours. No scan.")
        return 0

    name = "Nifty 50"
    cfg = SYMBOL_MAP[name]
    df = fetch_and_prepare_data(cfg["ticker"], period="1mo", interval="15m")

    if df.empty:
        print("MARKET DATA UNAVAILABLE. No signal and no alert.")
        return 0

    if bool(df.attrs.get("degraded", False)):
        print("DEGRADED PROXY DATA. No automated signal is permitted.")
        return 0

    result = evaluate_signal(
        df,
        rsi_oversold=RSI_OVERSOLD_DEFAULT,
        rsi_overbought=RSI_OVERBOUGHT_DEFAULT,
        adx_max=ADX_MAX_DEFAULT,
        require_closed=True,
    )
    signal = result["signal"]

    if signal == "HOLD":
        print(f"HOLD: {result['reason']}")
        return 0

    row = result["row"]
    decision_time = result["decision_time"]
    candle_key = f"{name}|{signal}|{row.name.isoformat()}"

    state = load_state()
    if state.get("last_alert_key") == candle_key:
        print(f"Duplicate signal suppressed: {candle_key}")
        return 0

    message = (
        f"🚨 *AUTOMATED {name} {signal} SIGNAL* ⚡\n\n"
        f"📌 Asset: *{name}*\n"
        f"🕯️ Closed candle: `{row.name:%Y-%m-%d %H:%M IST}`\n"
        f"🧭 Decision time: `{decision_time:%H:%M IST}`\n"
        f"💰 Price: `₹{row['Close']:.2f}`\n"
        f"📊 RSI(14): `{row['RSI']:.2f}`\n"
        f"📉 ADX(14): `{row['ADX']:.2f}`\n"
        f"📈 VWAP: `₹{row['VWAP']:.2f}`\n"
        f"🛡️ SL: `{row['ATR']*2.5:.2f} ATR`\n"
        f"🎯 Target: `{row['ATR']*3.5:.2f} ATR`\n\n"
        f"💡 {result['reason']}\n\n"
        f"⚠️ Signal only — no live order is placed by GitHub Actions."
    )

    if not send_telegram_alert(message):
        print("Telegram delivery failed; state NOT advanced so the next run can retry.")
        return 1

    state["last_alert_key"] = candle_key
    state["last_alert_at"] = now.isoformat()
    save_state(state)
    print(f"ALERT SENT: {candle_key}")
    return 0

if __name__ == "__main__":
    raise SystemExit(run_scanner())
