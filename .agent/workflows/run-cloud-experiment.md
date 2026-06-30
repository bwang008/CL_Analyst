---
description: How to run an Optuna hyperparameter search on GCP using the canary deploy + monitor wrapper
---

# Run Cloud Experiment Workflow

// turbo-all

## Prerequisites
- Google Cloud SDK installed and authenticated (`gcloud auth login`)
- Project set: `gcloud config set project cltrainer`
- Data uploaded to GCS: `gs://cltrainer-optuna-results/data/<dataset>.parquet`
- Working directory: `c:\Users\bwang\Documents\GitHub\CL_Analyst_Development`

---

## Batch Sweep Run (Recommended — Fully Automated)

The batch orchestrator manages N experiments end-to-end: deploy → monitor → collect → post-optimize.
Use the **v2 manifests** (`configs/batch_manifest_v2_*.json`, `baseline`/`overrides` schema validated
by `BatchSweepConfig`). The legacy `defaults`/`target_long` format is deprecated.

| Tier | Example Manifest (v2) | Experiments | Sweep n_trials | Post-Opt Trials | Use Case |
| ---------- | --------------------------------------------- | ----------- | ----------- | --------------- | ------------------------------------------- |
| **Canary** | `batch_manifest_v2_hourset14a_canary.json` | 2 | 3 | 3 | Pipeline validation / parity (~20-30 min) |
| **Scout** | `batch_manifest_v2_hourset14a_scout.json` | 4 | 200 | 200 | Moderate exploration, ballpark performance |
| **Production** | (generate via `scripts/generate_v2_manifest.py`) | 8 | 500 | 1500 | Deep optimization, final model selection |

### opt_mode — the post-optimizer chain (manifest is the source of truth)

`baseline.execution_workflow.opt_mode` selects how the optimizer VM runs. It is **required** and read
authoritatively from the manifest (never a CLI flag). Two values:

| `opt_mode` | Passes | Selection | Top-N | Produces | Use |
| ---------- | ------ | --------- | ----- | -------- | --- |
| **`individual`** | 2 (individual → ensemble) | `unified_pair_optimizer.py` | **Top 4** (`top_pairs.json`) | per-side `batch_summary_optimized_<obj>.md` **and** `batch_summary_optimized_ensembles_<obj>.md` + working `<obj>_ensemble_backtests.md` | **Default. Reproduces CANARY_V1.** Pass 1 optimizes each side, top individuals are paired, pass 2 re-optimizes the pairs as ensembles — all in one VM call. |
| **`ensemble`** | 1 (brute-force) | `select_top_ensembles.py` | Top 8 (`top_8_ensembles.json`) | `batch_ensemble_pre_opt.md` + ensemble reports only (no per-side reports) | Alternative brute-force sweep of all long/short combos. Diverges from CANARY_V1; skips individual optimization. |

### 1. Verify no VMs are running (avoid quota conflicts):
```powershell
gcloud compute instances list
```

### 2. Dry run to validate manifest:
The dry run runs `BatchSweepConfig` schema validation **and** the manifest sanity gate
(train_cutoff defined, no holdout leak, post_optimizer_holdout_months > 0,
slippage_per_side ∈ [0, 0.5], opt_mode valid). Any failure aborts before a single VM is created.
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\batch_manifest_v2_hourset14a_canary.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -DryRun
```

### 3. Launch the batch (replace manifest for your tier):
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\batch_manifest_v2_hourset14a_canary.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f"
```

### 4. The orchestrator automatically:
   - Deploys VMs across fallback zones (quota-aware concurrency gating by vCPUs and VM count)
   - Monitors via background PS jobs polling every 90s
   - Sends Telegram notifications at key milestones
   - Runs artifact verification gate before VM deletion
   - Captures crash diagnostics on failure
   - Generates consolidated batch summary
   - Deploys a post-optimizer VM (reads `opt_mode` from the manifest), waits for completion, downloads results
   - **opt_mode=individual (default):** pass 1 per-side optimization → `unified_pair_optimizer.py` selects
     **Top 4** (`top_pairs.json`) → pass 2 ensemble optimization on those pairs
   - **opt_mode=ensemble:** brute-force sweep of all combos → `select_top_ensembles.py` Top 8 (`top_8_ensembles.json`)
   - Generates ensemble backtest verification reports (`sharpe_ensemble_backtests.md`, `sortino_ensemble_backtests.md`)
   - Produces `batch_summary_optimized_<obj>.md`, `batch_summary_optimized_ensembles_<obj>.md`, and `wall_clock_summary.md`

### 5. Review results (opt_mode=individual / parity layout):
```
reports/batch_runs/batch_<timestamp>/
├── batch_progress.json                        ← live progress tracker
├── batch_summary.md                           ← unoptimized results
├── batch_summary_optimized_{sharpe,sortino}.md          ← per-side individual optimization (MAIN)
├── optimization_results_{sharpe,sortino}.json           ← raw individual optimization data
├── top_pairs.json                             ← Top 4 ensemble pairs (unified_pair_optimizer)
├── batch_summary_optimized_ensembles_{sharpe,sortino}.md ← Top-4 ensemble optimization
├── optimization_results_ensembles_{sharpe,sortino}.json
├── {sharpe,sortino}_ensemble_backtests.md     ← full backtest dumps per ensemble
├── wall_clock_summary.md                      ← auto-generated timing report
├── configs/                                   ← backtest-ready config JSONs per ensemble
├── predictions/                               ← merged prediction CSVs per ensemble
└── manifest.json                              ← frozen config
```
(opt_mode=ensemble instead emits `batch_ensemble_pre_opt.md` + `top_8_ensembles.json`.)

### 6. Validate parity against a reference run:
After a canary/parity run, confirm it structurally matches the golden reference
(`batch_20260626_0521_CANARY_V1`) and introduced no new crashes:
```powershell
conda activate trader
python scripts/compare_parity.py --run reports\batch_runs\batch_<timestamp>
# exit 0 = PARITY PASS; checks artifact set, Top-4, no FileNotFound/new tracebacks, slippage 0.01, sane PnL
```
```

### Manifest Format (v2 — `BatchSweepConfig`)
The manifest is the **single source of truth**: every operational parameter is required and validated;
there are no silent code-side defaults. Generate a fresh one with `scripts/generate_v2_manifest.py`.
```json
{
  "infrastructure": {
    "machine_type": "c2-standard-16",
    "provisioning_model": "STANDARD",
    "timeout_minutes": 240,
    "max_concurrent_vcpus": 288,
    "vcpus_per_vm": 16,
    "max_concurrent_vms": 12
  },
  "baseline": {
    "symbol": "CL",
    "data_workflow": { "dataset_version": "HourSet_14A", "resolution": "1h", "features": {}, "targets": {} },
    "training_workflow": {
      "train_cutoff_date": "2022-01-01",
      "holdout_cutoff_date": "2026-01-01",
      "target_columns": [],
      "gcs_base_dir": "gs://cltrainer-optuna-results/canary",
      "optuna": { "n_trials": 3, "post_optimizer_trials": 3, "post_optimizer_holdout_months": 6, "...": "..." }
    },
    "execution_workflow": {
      "slippage_per_side": 0.01,
      "opt_mode": "individual",
      "strategy_config_path": "configs/strategies/hourly_ensemble_010.json"
    }
  },
  "experiments": [
    {
      "label": "HS14A 2x1 6H",
      "gcs_prefix": "sweep_hs14a_2x1_6h_canary",
      "overrides": { "training_workflow": { "target_columns": ["TARGET_TRIPLE_2x1_6H_LONG", "TARGET_TRIPLE_2x1_6H_SHORT"] } }
    }
  ]
}
```
> **slippage_per_side is ABSOLUTE price units** (passed straight to the backtest engine, no tick-size
> conversion). `0.01` = $0.01/side. A value like `1.0` means ~$2,000/trade and produces the −$2.5M-PnL
> blowup class — the dry-run gate rejects anything > 0.5.

---

## Single Experiment Run (Manual — Legacy)

### 1. Delete any existing canary VM:
```powershell
gcloud compute instances delete optuna-runner-canary --zone=us-central1-a --quiet
```

### 2. Deploy:
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_deploy_sweep.ps1 `
    -TargetLong TARGET_TRIPLE_3x1_12H_LONG `
    -TargetShort TARGET_TRIPLE_3x1_12H_SHORT `
    -ProvisioningModel STANDARD
```

### 3. Monitor:
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_monitor.ps1 `
    -VmName optuna-runner-canary `
    -GcsPrefix canary `
    -ExperimentLabel "Single Sweep" `
    -PollIntervalSeconds 120
```

### 4. Clean up:
```powershell
gcloud compute instances delete optuna-runner-canary --zone=us-central1-a --quiet
```

---

## Quick Status Check
```powershell
.\gcp\gcp_check_status.ps1 -VmName <vm-name>
```

## View Live Output (SSH)
```powershell
gcloud compute ssh <vm-name> --zone=<zone> --command="tmux attach -t sweep"
# Ctrl+B then D to detach without stopping
```

---

## Code Upload Reference

Both deploy scripts (`gcp_deploy_sweep.ps1` and `gcp_deploy_optimizer.ps1`) upload these `src/` files to the VM:

| File | Required By |
|------|-------------|
| `src/__init__.py` | Package init |
| `src/util.py` | `backtest_engine.py` (get_X_y) |
| `src/LGBMLearner.py` | `backtest_engine.py` (model loading) |
| `src/data_paths.py` | `backtest_engine.py` (lazy import for fallback data loading) |
| `src/data_processor.py` | `experiment_runner.py` (data processing) |
| `src/live_execution/strategies/execution_models.py` | `backtest_engine.py` (strategy factory) |
| `src/live_execution/strategies/configurable_strategy.py` | Strategy execution |
| `src/features/feature_buckets.py` | `optuna_lgbm_search_v2.py` (bucket toggling) |

> **IMPORTANT**: When adding new `from src.*` imports to any `agent/` file that runs on a VM, you MUST also add the file to the `$codeFiles` array in both `gcp_deploy_sweep.ps1` and `gcp_deploy_optimizer.ps1`. Failure to do so will cause `ModuleNotFoundError` on the VM.

## Important Notes
- **Sweep machine**: `c2-standard-16` (16 vCPUs, ~64 GB RAM) — migrated from `n2-highcpu-48` as of 2026-05-16
- **Threading**: Auto-detected via `nproc` — `N_WORKERS=4`, `THREADS_PER_WORKER=4` on C2-16 (4×4 = 16 cores)
- **CPU validation**: `vm_sweep_run.sh` will FATAL error if cores don't match the config
- **Post-optimizer**: Dynamically sized `n2-standard-{8,16,32,48}` based on experiment count. Workers auto-matched to vCPUs.
- SPOT VMs can be preempted — use STANDARD for runs that must complete
- **IP address limit**: 8 external IPs per region (pending increase to 30). Post-optimizer runs **after** all sweep VMs are deleted, so it doesn't compete for IPs. When IPs are the bottleneck, add `"max_concurrent_vms": 8` to the manifest `defaults` to cap concurrent VMs directly. GCP Console quota name: **"In-use regional external IPv4 addresses"**.
- Preferred region: **us-west1** (us-central1 can be saturated)
- Zone fallback: pass comma-separated zones to `-Zone` parameter
- **Wall clock summary**: `wall_clock_summary.md` is auto-generated at the end of every batch run
- **Old manifests**: Frozen manifests in `reports/batch_runs/*/manifest.json` may still reference `n2-highcpu-48`. Update `machine_type`, `vcpus_per_vm`, and `max_concurrent_vcpus` before re-running.
