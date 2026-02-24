# HANDOFF

## Current Branch
- `main`

## Last Completed Task
- **Track 2.1 — Short Sniper**: trained/validated `EXP-020 (S_Ultimate_Short)`, performed probability sweep to find high-confidence short thresholds, ran friction-aware short backtest, and archived the model to the registry (`models/registry/EXP-020_S_Ultimate_Short/`).

## Current Known Bugs / Issues
- **Evaluator naming**: `reports/vault_metrics.json` uses class names `{1: "Buy", 2: "Sell"}` even for binary short targets. For `TARGET_TRIPLE_2x1_24H_SHORT`, the “Buy” slot corresponds to the positive short label.
- **Binary probabilities**: With focal loss custom objective, LightGBM `predict()` may emit logits (not 0–1). Use `agent/threshold_sweep_binary.py` (sigmoid-aware) for binary sweeps.
- **Data coverage**: processed datasets start at `2009-01-15…` in `set_06`; true 2008-era OOS requires earlier data coverage in processed parquet.

## Immediate Next Steps
- Extend the evaluator/reporting to support “Short” naming for binary short targets (cosmetic but reduces confusion).
- Add a short-side friction backtest summary report export (PF/WinRate/NetPnL) alongside the sweep output for easy comparison across thresholds.
- Decide whether to standardize on a “trade threshold” policy (e.g., minimum precision) vs max-F1 for both long and short models.
