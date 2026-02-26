# HANDOFF

## Current Branch
- `main`

## Last Completed Task
- **Task 4.4 — Smart Backfill & Dual-Ledger**: Built `src/live_execution/data_manager.py` (Three-Tier architecture: Seed CSV → Parquet cache → IBKR backfill → live append). Added Two-Stream architecture to `live_trader.py` (continuous contract for "Brain" signals, front-month contract for "Hands" execution + raw data logging). Upgraded `telemetry.py` with `raw_front_month_bars` table for training ledger. Added `get_front_month_contract()` and `fetch_historical_bars_by_duration()` to `ibkr_client.py`. Verified IBKR connectivity (IB Gateway port 4002, paper account DU1899929, front-month CLJ6). 174 tests passing.

## Current Known Bugs / Issues
- **Evaluator naming**: `reports/vault_metrics.json` uses class names `{1: "Buy", 2: "Sell"}` even for binary short targets. For `TARGET_TRIPLE_2x1_24H_SHORT`, the "Buy" slot corresponds to the positive short label.
- **Binary probabilities**: With focal loss custom objective, LightGBM `predict()` may emit logits (not 0–1). The live trader applies sigmoid transform; use `agent/threshold_sweep_binary.py` (sigmoid-aware) for binary sweeps.
- **Data coverage**: processed datasets start at `2009-01-15…` in `set_06`; true 2008-era OOS requires earlier data coverage in processed parquet.

## Immediate Next Steps
- Run live trader in `--dry-run` mode during market hours to validate end-to-end warm-start + inference pipeline.
- Monitor telemetry DB for feature quality, signal frequency, and raw front-month bar logging.
- Extend the evaluator/reporting to support "Short" naming for binary short targets.
- Consider adding fill tracking (subscribe to `ib.orderStatusEvent` for real-time fill updates in telemetry).
