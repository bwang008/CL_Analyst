# Run Per-Side Strategy Optimization (Local)

## Objective
Run `batch_post_optimizer.py` locally against the completed batch `batch_20260518_2321` to generate two optimization reports:
- `batch_summary_optimized_sharpe.md`
- `batch_summary_optimized_sortino.md`

Each report contains independently optimized Long and Short models across all 8 experiments × 2 ML metrics (logloss, average_precision).

## Command

```powershell
cd C:\Users\bwang\Documents\GitHub\CL_Analyst_Development

python agent/batch_post_optimizer.py `
    --batch-dir reports/batch_runs/batch_20260518_2321 `
    --n-trials 500 `
    --holdout-months 4 `
    --workers 8 `
    --objective both `
    --no-filter
```

## Parameters Explained
- `--n-trials 500` — 500 Optuna trials per side (reduced from 1500; search space is halved to 9 params per side)
- `--holdout-months 4` — Last 4 months reserved as unseen holdout for out-of-sample validation
- `--workers 8` — 8 parallel workers (this machine has 24 cores; 8 workers × ~3 cores each from data loading is a safe ceiling)
- `--objective both` — Runs Sharpe optimization for all 32 tasks, then Sortino optimization for all 32 tasks sequentially (64 total)
- `--no-filter` — Keep best trial even if unprofitable (we want to see the full landscape)

## Expected Behavior
1. The script loads `batch_progress.json` from the batch directory
2. For each of the 8 completed experiments × 2 metrics:
   - Creates merged prediction CSVs if they don't already exist
   - Runs **LONG** side optimization (opposing SHORT side disabled via `min_prob=1.0`)
   - Runs **SHORT** side optimization (opposing LONG side disabled via `min_prob=1.0`)
3. First pass uses **Sharpe** objective → generates `batch_summary_optimized_sharpe.md` + `optimization_results_sharpe.json`
4. Second pass uses **Sortino** objective → generates `batch_summary_optimized_sortino.md` + `optimization_results_sortino.json`

## Expected Output Files
All written to `reports/batch_runs/batch_20260518_2321/`:
- `batch_summary_optimized_sharpe.md` — Long/Short tables with all Opt columns populated
- `batch_summary_optimized_sortino.md` — Same structure, Sortino-optimized parameters
- `optimization_results_sharpe.json` — Raw per-side results for Sharpe
- `optimization_results_sortino.json` — Raw per-side results for Sortino

## Task Count
- 8 experiments × 2 metrics × 2 sides = **32 tasks per objective**
- 2 objectives (sharpe + sortino) = **64 total optimization runs**
- Each run: 500 trials × 9 parameters = fast convergence (~30-60s per task)

## Estimated Wall Time
~30-60 minutes total with 8 workers.

## Monitoring
The script sends Telegram notifications at 25%/50%/75%/100% milestones and every 30 minutes. Console output shows trial-by-trial progress.

## After Completion
Verify both reports exist and contain populated Opt columns:
```powershell
Get-Content "reports/batch_runs/batch_20260518_2321/batch_summary_optimized_sharpe.md" -TotalCount 30
Get-Content "reports/batch_runs/batch_20260518_2321/batch_summary_optimized_sortino.md" -TotalCount 30
```
