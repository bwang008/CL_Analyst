# AGENT_LOG

Historical progress and completed track summaries (reverse-chronological; newest first).

## 2026-02-23 — Track 2.1: Short Sniper (panic-selling)

- **Target**: `TARGET_TRIPLE_2x1_24H_SHORT` (binary short label)
- **Experiment**: `EXP-020` (`S_Ultimate_Short`)
- **Dataset**: `data/processed/CL_set_06_shortfix.parquet`
- **Key point**: the short edge was **high-confidence only**; max-F1 overtraded and lost money under friction.

### Threshold sweep + friction results (selected)

- F1-optimal threshold: `0.45`
  - Friction backtest PF: `0.87` (unprofitable; too many trades)
- Selected trade threshold: `0.60`
  - **Win rate**: `70.0%`
  - **Profit factor**: `2.39`
  - **Net PnL**: `+$1,071,745.37` (CL multiplier 1000; commission 2.50/side; slippage 0.03/side)

### Archived bundle

- Registry entry created:
  - `models/registry/EXP-020_S_Ultimate_Short/`
- Catalog updated:
  - `models/registry/README.md`

## 2026-02-23 — Track 1: Reality Check (market friction + OOS stress)

### Task 1.1 — Market friction backtest

- Updated `agent/backtester.py` to support:
  - direction-aware slippage (Buy fills worse; Sell fills worse)
  - flat commission per side
  - CL contract multiplier (1000)
- Friction-aware vault run (S_Ultimate):
  - Profit factor: `7.76`
  - Win rate: `84.4%`
  - Net PnL: `+$1,277,168.30`

### Task 1.2 — OOS regime testing

- Added regime runner `agent/oos_regime_test.py` to train pre-regime and test on extreme windows.
- Findings:
  - 2009 window was skipped due to insufficient pre-window training data in processed set.
  - COVID crash window remained profitable (PF > 1), but materially degraded vs vault-era performance.

## 2026-02-23 — Model Registry system

- Added `models/registry/` system and an archiving CLI:
  - `agent/archive_model.py`
- Archived `EXP-017` bundle:
  - `models/registry/EXP-017_S_Ultimate/`

