# Ticket Resolution Blueprint — live-backtest-reconciliation_07022026_2228
**Ticket Directory:** `.agents/collab/tickets/live-backtest-reconciliation_07022026_2228/`

## Bug Summary
The live-to-backtest reconciliation pipeline (`run_reconciliation_audit.py` + `trade_reconciler.py`) is hardcoded to an obsolete `HourSet_08` configuration and target dates (May 13-15). It also fatally incorrectly resamples OHLCV from sparse `market_bars` in the live telemetry DB, creating a 72-day gap that inflates ATR from 0.51 to 3.19, resulting in 0 backtest trades. Finally, the telemetry exporter fails to compute necessary PnL fields for reconciliation.

## Target Files
- `scripts/run_reconciliation_audit.py`
- `src/live_execution/telemetry.py`
- `.agents/workflows/validate-parity.md`

## Required Changes

**1. `scripts/run_reconciliation_audit.py`**
- Refactor script to use `argparse` for accepting `strategy_config`, `start_date`, `end_date`, and prediction sources as arguments instead of hardcoded values.
- In `step1_resample_ohlcv()`, replace the sparse DB query with logic to load the full contiguous historical parquet data directly (e.g. `14A/14B`) to prevent ATR distortion.

**2. `src/live_execution/telemetry.py`**
- In `export_trade_ledger()`, dynamically compute `gross_pnl_dollars`, `commission_dollars`, and `net_pnl_dollars` for each row. The calculation should be `(exit_price - entry_price) * side_mult * quantity * multiplier`.
- Ensure `exit_price` is correctly populated for all closed trade variants.

**3. `.agents/workflows/validate-parity.md`**
- Add a new explicit step in the workflow detailing how to run the newly parameterized `run_reconciliation_audit.py` (e.g., Step 6: Live Trade Reconciliation).
