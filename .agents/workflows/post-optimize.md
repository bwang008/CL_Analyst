---
description: Run strategy optimization on all models in a completed batch directory
---

# Post-Optimize Workflow

// turbo-all

## Prerequisites
- A completed batch directory must exist under `reports/batch_runs/` (e.g., `reports/batch_runs/batch_20260511_2116`)

## Instructions

1. Identify the most recent batch directory:
```powershell
Get-ChildItem reports\batch_runs -Directory | Sort-Object CreationTime -Descending | Select-Object -First 1
```

2. Run the batch post-optimizer on the target directory (replace `[BATCH_DIR_NAME]` with the actual name):
```powershell
python agent/batch_post_optimizer.py --batch-dir reports/batch_runs/[BATCH_DIR_NAME] --n-trials 100 --min-trades 10
```
> [!NOTE]
> This process takes approximately 4-5 minutes per model optimization (e.g., a batch of 4 experiments × 2 metrics × 3 modes = ~1.5 hours total runtime).

3. Once complete, review the optimized results:
   - File: `reports/batch_runs/[BATCH_DIR_NAME]/batch_summary_optimized.md`
   - File: `reports/batch_runs/[BATCH_DIR_NAME]/optimization_results.json`

4. Compare the baseline `batch_summary.md` against `batch_summary_optimized.md` to identify the improvement in Profit Factor (PF) and Net PnL.
