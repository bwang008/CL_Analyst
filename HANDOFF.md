# HANDOFF

## Current Branch
- `main`

## Last Completed Task
- **Task 4.2/4.3 — Live Execution Engine**: Built `src/live_execution/live_trader.py`, `telemetry.py`, and updated `ibkr_client.py` with position management, live bar subscriptions, and bracket order execution. System connects to IBKR Paper Trading, runs S_Ultimate inference on live 5-min CL bars, and logs all activity to SQLite.

## Current Known Bugs / Issues
- **Evaluator naming**: `reports/vault_metrics.json` uses class names `{1: "Buy", 2: "Sell"}` even for binary short targets. For `TARGET_TRIPLE_2x1_24H_SHORT`, the "Buy" slot corresponds to the positive short label.
- **Binary probabilities**: With focal loss custom objective, LightGBM `predict()` may emit logits (not 0–1). The live trader applies sigmoid transform; use `agent/threshold_sweep_binary.py` (sigmoid-aware) for binary sweeps.
- **Data coverage**: processed datasets start at `2009-01-15…` in `set_06`; true 2008-era OOS requires earlier data coverage in processed parquet.
- **Cold start limitation**: Live trader fetches 5 days of history on startup. Some features with 35-day windows (10,080 bars) will have warm-up NaNs until enough live bars accumulate. These are forward-filled for inference.

## Immediate Next Steps
- Run live trader in `--dry-run` mode against paper trading to validate end-to-end connectivity.
- Monitor telemetry DB for feature quality and signal frequency during market hours.
- Extend the evaluator/reporting to support "Short" naming for binary short targets.
- Consider adding fill tracking (subscribe to `ib.orderStatusEvent` for real-time fill updates in telemetry).
