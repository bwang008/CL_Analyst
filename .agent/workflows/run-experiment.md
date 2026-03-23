---
description: Run a model improvement experiment locally (training + evaluation)
---
// turbo-all

## Context Loading

1. Read the experiment tracker and agent context for baseline:
   - File: `AGENT_CONTEXT.md` (project state)
   - File: `experiment_tracker.json` (prior results, current_best)
   - File: `research_backlog.json` (if this experiment addresses a backlog item)

2. Review the current experiment registry:
   ```powershell
   dir /B models\registry
   ```

## Dataset Preparation

3. If the dataset does not already exist, process it through the data pipeline:
   ```powershell
   conda run -n base python -c "from src.data_processor import DataProcessor; dp = DataProcessor(input_path=r'C:\CL_Analyst_Data\data\raw\CL.csv', dataset_version='SET_VERSION_HERE'); dp.process()"
   ```

4. Verify the dataset exists and is valid:
   ```powershell
   conda run -n base python -c "import pandas as pd; df=pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\CL_SET_VERSION_HERE.parquet'); print(f'Shape: {df.shape}'); print(f'NaN features: {df[[c for c in df.columns if not c.startswith(chr(84)+chr(65)+chr(82)) and not c.startswith(chr(82)+chr(65)+chr(87))]].isna().sum().sum()}')"
   ```

## Local Training (Optuna Walk-Forward Search)

> **Local machine: 24 cores, 64 GB RAM.**
> Architecture: 4 parallel search processes × 4 LightGBM threads = 16 cores (8 reserved for OS).
> Each search runs `--n-jobs 1` (sequential Bayesian optimization within each process).
> This mirrors the cloud canary architecture but with fewer threads per worker.

5. Launch all 4 searches in parallel (2 metrics × 2 directions) using PowerShell background jobs:
   ```powershell
   $dataset = "C:\CL_Analyst_Data\data\processed\CL_SET_VERSION_HERE.parquet"
   $cutoff = "2022-01-01"
   $trials = 50
   $threads = 4

   $searches = @(
       @{target="TARGET_TRIPLE_2x1_24H_LONG";  metric="logloss"; study="local_long_logloss"},
       @{target="TARGET_TRIPLE_2x1_24H_SHORT"; metric="logloss"; study="local_short_logloss"},
       @{target="TARGET_TRIPLE_2x1_24H_LONG";  metric="f0.5";    study="local_long_f0.5"},
       @{target="TARGET_TRIPLE_2x1_24H_SHORT"; metric="f0.5";    study="local_short_f0.5"}
   )

   foreach ($s in $searches) {
       Start-Process -NoNewWindow -FilePath "conda" -ArgumentList @(
           "run", "-n", "base", "python", "agent/optuna_lgbm_search_v2.py",
           "--target", $s.target,
           "--data", $dataset,
           "--ml-metric", $s.metric,
           "--n-trials", $trials,
           "--n-jobs", "1",
           "--num-threads", $threads,
           "--study-name", $s.study,
           "--train-cutoff-date", $cutoff
       )
       Write-Host "Started: $($s.study)"
   }
   ```

   **Parameter guide:**
   - `$trials = 50` → reasonable for local (cloud uses 20 canary / 200 prod)
   - `$threads = 4` → LightGBM threads per search (cloud canary uses 12)
   - `$cutoff = "2022-01-01"` → everything before this is gym, after is vault (untouched)
   - Estimated time: ~1-2 hours for 50 trials on set_11

6. Monitor progress (check journal files for activity):
   ```powershell
   Get-ChildItem models\optuna_studies\*.journal | Sort-Object LastWriteTime | Format-Table Name, Length, LastWriteTime
   ```

7. Check for completed results:
   ```powershell
   Get-ChildItem reports\optuna_best_params_*.json | Format-Table Name, LastWriteTime
   ```

## Evaluation

8. Run walk-forward validation to evaluate the model:
   ```powershell
   conda run -n base python -m pytest tests/ -v --tb=short -m "not slow"
   ```

9. Compare results against the baseline and summarize findings with metrics:
   - Review the Optuna output (best trial score, F1, precision)
   - Check reports: `reports/optuna_best_params_*.json`
   - Compare with `current_best` in `experiment_tracker.json`

## Logging & Commit

10. Log the experiment results to the tracker:
    - Read `experiment_tracker.json`
    - Append a new entry with: id (use `next_experiment_id` field), name, status, date, dataset,
      data_integrity (set to "clean" for set_10+, "leaked" for older), direction, target,
      metrics (pnl, profit_factor, win_rate, trades, max_drawdown), notes, and tags
    - Increment `next_experiment_id`
    - Update `current_best` if this experiment beats the baseline AND uses a clean dataset
    - Write updated `experiment_tracker.json`

11. Update `research_backlog.json`:
    - If this experiment came from a backlog item, mark it as "completed" and fill in `outcome`
    - If results suggest new ideas, add them as new backlog entries

12. If the experiment improves on the baseline, commit all changes including tracker updates:
    ```powershell
    git add -A; git commit -m "EXP-NNN: <experiment description>"
    ```
