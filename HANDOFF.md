# Handoff Summary

## Current project state (updated 2026-02-22)

### Model Improvement Research — Active
An agentic experiment framework is running to improve model performance. Key files:
- **`agent/experiment_log.json`** — Structured log of all experiments with metrics and verdicts. **Read this first.**
- **`agent/strategy_queue.json`** — 8 strategies ordered by priority with status tracking.
- **`agent/experiment_runner.py`** — Orchestrates experiments end-to-end (run, measure, log).
- **`.agent/workflows/run-experiment.md`** — Step-by-step workflow for running experiments.

### Experiment Results So Far (3 complete)

| ID | Target | Balance | Buy Precision | Buy Recall | Buy F1 | Verdict |
|----|--------|---------|---------------|------------|--------|---------|
| EXP-001 | DIR_2PCT_24H_LONG (set_04) | downsample | **25.6%** | **69.7%** | **37.5%** 🏆 | promising |
| EXP-002 | DIR_2PCT_24H_LONG (set_04) | weight | 30.0% | 5.0% | 8.5% | improvement |
| EXP-003 | DIR_3PCT_24H_LONG (set_04) | downsample | 12.6% | 61.1% | 20.9% | promising |

**Key finding:** Lowering threshold from 8%→2% and horizon from 48h→24h was the single biggest improvement. Downsample vastly outperforms weight mode for recall.

### Next Experiment to Run
**EXP-004:** Triple Barrier target on `set_05` — `TARGET_TRIPLE_2x1_24H_LONG` with downsample. The dataset is already processed at `data/processed/CL_set_05.parquet`.

```bash
conda activate trader
python -c "
import json
from agent.experiment_runner import run_experiment, load_experiment_log, generate_experiment_id
log = load_experiment_log()
exp_id = generate_experiment_id(log)
result = run_experiment(
    experiment_id=exp_id, strategy_id='S1b',
    hypothesis='Dynamic Triple Barrier (2xATR TP, 1xATR SL, 24h) should produce better class balance and more trainable signal',
    changes={'target': 'TARGET_TRIPLE_2x1_24H_LONG', 'tp_atr_mult': 2.0, 'sl_atr_mult': 1.0, 'max_horizon': 288},
    data_path='data/processed/CL_set_05.parquet',
    target_name='TARGET_TRIPLE_2x1_24H_LONG',
    method='walk_forward', balance_mode='downsample', threshold=0.02,
)
print(json.dumps(result, indent=2, default=str))
"
```

### Remaining Strategy Queue (after Triple Barrier)
1. **S2a**: Volatility rate-of-change features (vol compression → expansion)
2. **S2b**: Bar microstructure + MACD/ADX features
3. **S3a**: Probability threshold sweep [0.05-0.90]
4. **S3b**: Optuna hyperparameter search (constrained: num_leaves≤31, min_child_samples 50-200, avg across all WF folds)
5. **S4a**: Focal loss + class weighting
6. **S5a**: Profitability backtest (termination criterion: Sharpe>1.0, Profit Factor>1.5)

---

## Datasets Available
- `set_03` — Original (8%/4% thresholds, 48h horizon) — baseline
- `set_04` — Lower thresholds (2%/3%) with shorter horizons (12h/24h) + continuous returns
- `set_05` — Dynamic Triple Barrier targets (ATR-based barriers)

## Data pipeline
`DataProcessor` -> `AlphaFactory` -> targets -> cleanup -> save.

### Targets available
- `TARGET_DIR_8PCT_*` / `TARGET_DIR_4PCT_*` (set_03)
- `TARGET_SQZ_8PCT_*` / `TARGET_SQZ_4PCT_*` (set_03)
- `TARGET_DIR_2PCT_12H_*` / `TARGET_DIR_2PCT_24H_*` / `TARGET_DIR_3PCT_12H_*` / `TARGET_DIR_3PCT_24H_*` (set_04)
- `TARGET_RET_144` / `TARGET_RET_288` / `TARGET_RET_576` (set_04, continuous)
- `TARGET_TRIPLE_2x1_12H_*` / `TARGET_TRIPLE_2x1_24H_*` / `TARGET_TRIPLE_3x1_24H_*` (set_05)

## How to run
- Process dataset: `python -c "from src.data_processor import DataProcessor; DataProcessor(input_path='data/raw/CL.csv', dataset_version='set_04').process()"`
- Train: `python main.py train --target TARGET_DIR_2PCT_24H_LONG --balance_mode downsample`
- Run experiment: `python agent/experiment_runner.py --quick-test`
- Batch config: `python main.py --config experiments.json`

## Known issues / watch-outs
- **Long runtime** — AlphaFactory 35-day window (10080 bars) takes ~20min with Numba.
- **TARGET_RET_* columns** are floats — `cleanup()` now skips Int64 conversion for them.
- **Sell class** never predicted — all current binary targets are LONG-only. SHORT experiments pending.
