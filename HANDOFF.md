# Handoff Summary

## Current project state
- **Primary dataset default:** `set_03` (master squeeze targets).
- **Data pipeline:** `DataProcessor` -> `AlphaFactory` -> targets -> cleanup -> save.
- **Targets now available:**
  - `TARGET_DIR_8PCT_*` (multi/long/short)
  - `TARGET_DIR_4PCT_*` (multi/long/short)
  - `TARGET_SQZ_8PCT_*` (multi/long/short)
  - `TARGET_SQZ_4PCT_*` (multi/long/short)
  - `TARGET_SQUEEZE*` still present in `set_01`/`set_02` for backward compatibility.
- **Training CLI updates:** `--target`, `--targets`, `--balance_mode`, and `--config` supported; sequential runs logged to `reports/train_runs.log` and `reports/batch_results.csv`.
- **Performance optimizations:** Numba JIT for Hurst, entropy, Corwin-Schultz, and rolling slope/R2.
- **Progress logging:** `data_processor.py` prints 25/50/75/100% checkpoints and AlphaFactory window timings.

## How to run
- Generate default dataset:
  - `python -u src/data_processor.py` (now defaults to `set_03`)
- Process via CLI:
  - `python main.py process`
- Train single target:
  - `python main.py train --target TARGET_SQZ_4PCT_LONG`
- Train multiple targets:
  - `python main.py train --targets TARGET_SQZ_8PCT_LONG,TARGET_SQZ_4PCT_LONG`
- Train with downsampling:
  - `python main.py train --balance_mode downsample --target TARGET_SQZ_4PCT_LONG`
- Run batch config:
  - `python main.py --config experiments.json`

## Known issues / watch-outs
- **Long runtime** still dominated by the 35-day window (10080 bars) but now faster with Numba.
- **Targets at tail** contain NaNs; cleanup now preserves them using nullable `Int64`.
- **DATASETS.json** has been normalized and should remain valid JSON.

## Next steps
1. Run multi-target training and review `reports/train_runs.log`.
2. Compare macro vs squeeze targets in feature importance.
3. Decide whether to keep `TARGET_SQUEEZE` in set_01/02 or deprecate.
4. Optional: add per-class precision/recall to reports or fold summaries.
