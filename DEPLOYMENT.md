# Quant Strategy & Execution Engine — V2 deployment notes

## Replace

Copy these files/folders into the repository:

- `app.py`
- `strategy_engine.py`
- `auto_scanner.py`
- `.github/workflows/auto_scanner.yml`
- `data/historical_trades.json`
- `data/scanner_state.json`
- `requirements.txt`

The historical trade file is intentionally reset to `[]`; the two seeded trades are no longer used.

## Strategy changes

- Actual completed-daily 50 EMA is now `Daily_EMA50`.
- The 15-minute 50 EMA is separately named `EMA50_15m`.
- Signal/backtest entry is modeled as signal on a closed 15m candle and execution at the next 15m candle open.
- Slippage is included in research backtests.
- Same-candle stop/target ambiguity is handled conservatively.
- EOD square-off has priority at/after 15:15.
- Absolute `ATR >= 10` filter has been removed.
- VWAP dispersion is volume-weighted.
- Proxy/degraded data cannot create signals or trades.
- Maximum two trades/day is enforced in the backtest and scanner.
- Strategy statistics include win rate, profit factor, expectancy, drawdown, losing streak and monthly context.
- Multi-year research can be performed by uploading a 15-minute OHLCV CSV in the Streamlit Research Lab.

## Secrets

Streamlit / deployment environment:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `KITE_API_KEY`
- `KITE_ACCESS_TOKEN`
- `LIVE_TRADING_ENABLED=false`

GitHub Actions repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional GitHub Actions repository variable:

- `SCAN_SYMBOLS` — defaults to `Nifty 50,Bank Nifty`; only `Nifty 50` and `Bank Nifty` are supported in this release.

## Live trading

Keep `LIVE_TRADING_ENABLED=false` while validating the new engine.

The app resolves the nearest non-expired NFO futures contract dynamically and uses the broker-provided lot size. No expired hard-coded futures symbol is used.

The GitHub Actions scanner sends signal-only Telegram alerts; it does not place live orders.


## Supported instruments

This release intentionally supports **Nifty 50 and Bank Nifty only**. Stock symbols have been removed from the strategy engine, Streamlit selector, and automated scanner. Stock support will be added as a separate, explicitly validated phase later.

The research and live-execution architecture remains instrument-aware, so adding stocks later will not require redesigning the core engine.
