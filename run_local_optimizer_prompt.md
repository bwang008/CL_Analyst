# Run Per-Side Strategy Optimization (GCP Cloud)

## Objective
Deploy a GCP VM to run `batch_post_optimizer.py` against the completed batch `batch_20260518_2321` with **full parallelism** — all 32 tasks per objective running simultaneously.

Output reports:
- `batch_summary_optimized_sharpe.md`
- `batch_summary_optimized_sortino.md`

Each report contains independently optimized Long and Short models across all 8 experiments × 2 ML metrics (logloss, average_precision).

## Command

```powershell
cd C:\Users\bwang\Documents\GitHub\CL_Analyst_Development

powershell -ExecutionPolicy Bypass -File .\gcp\gcp_deploy_optimizer.ps1 `
    -BatchId batch_20260518_2321 `
    -NTrials 500 `
    -HoldoutMonths 4 `
    -Workers 32 `
    -Objective both
```

## Infrastructure
- **Machine**: `n2-standard-32` (32 vCPUs, 128 GB RAM)
- **Workers**: 32 — one per task, all 32 run simultaneously
- **Pricing**: STANDARD (non-preemptible, guaranteed completion)
- **Auto-shutdown**: Yes (VM deletes itself after completion)

## Parameters Explained
- `-NTrials 500` — 500 Optuna trials per side (9 params per side, fast convergence)
- `-HoldoutMonths 4` — Last 4 months reserved as unseen holdout for out-of-sample validation
- `-Workers 32` — 32 parallel workers = all tasks run at once (8 exp × 2 metrics × 2 sides = 32)
- `-Objective both` — Runs Sharpe optimization for all 32 tasks, then Sortino for all 32 tasks sequentially (64 total)

## Expected Behavior
1. The deploy script uploads code + batch metadata to GCS
2. VM provisions, downloads experiment artifacts from GCS
3. For each of the 8 completed experiments × 2 metrics:
   - Creates merged prediction CSVs if they don't already exist
   - Runs **LONG** side optimization (opposing SHORT side disabled via `min_prob=1.0`)
   - Runs **SHORT** side optimization (opposing LONG side disabled via `min_prob=1.0`)
4. First pass uses **Sharpe** objective → all 32 tasks run in parallel → generates `batch_summary_optimized_sharpe.md` + `optimization_results_sharpe.json`
5. Second pass uses **Sortino** objective → all 32 tasks run in parallel → generates `batch_summary_optimized_sortino.md` + `optimization_results_sortino.json`
6. `generate_batch_configs.py` runs to produce correctly-formatted strategy JSONs
7. All results uploaded to GCS, VM auto-shuts down

## Expected Output Files
All uploaded to `gs://cltrainer-optuna-results/batch_optimizer/batch_20260518_2321/`:
- `batch_summary_optimized_sharpe.md` — Long/Short tables with all Opt columns populated
- `batch_summary_optimized_sortino.md` — Same structure, Sortino-optimized parameters
- `optimization_results_sharpe.json` — Raw per-side results for Sharpe
- `optimization_results_sortino.json` — Raw per-side results for Sortino
- `batch_configs/*.json` — Production-ready strategy configs
- `logs/post_optimize_*.log` — Full run log

## Task Count
- 8 experiments × 2 metrics × 2 sides = **32 tasks per objective**
- 2 objectives (sharpe + sortino) = **64 total optimization runs**
- All 32 tasks per objective run **simultaneously** with 32 workers
- Each run: 500 trials × 9 parameters = fast convergence (~30-60s per task)

## Estimated Wall Time
- ~1-2 minutes per objective (all 32 tasks finish in one wave)
- ~5 minutes total including VM provisioning + artifact download + upload
- Conservative: ~10 minutes end-to-end

## Memory Budget
- 32 workers × ~400 MB/worker = ~13 GB (well within 128 GB)
- OHLCV + predictions: ~50 MB shared
- Python overhead: ~6 GB
- **Total: ~20 GB of 128 GB** (plenty of headroom)

## Monitoring
- Telegram notifications at 25%/50%/75%/100% milestones and every 30 minutes
- Check VM status: `.\gcp\gcp_check_status.ps1 -VmName optuna-post-optimizer`
- View live output: `gcloud compute ssh optuna-post-optimizer --zone=us-central1-a --command="tmux attach -t optimizer"`

## After Completion
Results are auto-uploaded to GCS. Download with:
```powershell
gsutil -m cp -r gs://cltrainer-optuna-results/batch_optimizer/batch_20260518_2321/ .
```
