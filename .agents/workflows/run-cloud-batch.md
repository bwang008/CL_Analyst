# Run Cloud Batch Experiment Workflow

// turbo-all

## Overview

Runs N canary experiments sequentially-or-in-parallel on GCP (up to 2 VMs concurrently within the 100-vCPU quota cap). Each experiment gets a fresh VM, a timestamped output directory (no overwrites), and Telegram notifications. Results are aggregated into a single comparison report at the end.

**Key files:**
- `configs/canary_batch_manifest.json` — declare what to run (edit this)
- `gcp/run_canary_batch.ps1` — orchestrator (do not edit between runs)
- `gcp/gcp_monitor.ps1` — VM monitor with Telegram support (called automatically)
- `gcp/collect_batch_results.ps1` — post-run report aggregator
- `reports/batch_runs/<batch_id>/` — all outputs land here

---

## Prerequisites

- Google Cloud SDK authenticated: `gcloud auth login`
- Project set: `gcloud config set project cltrainer`
- Data uploaded to GCS (verify): `gcloud storage ls gs://cltrainer-optuna-results/data/`
- Telegram configured in `.env` (optional but recommended):
  ```
  TELEGRAM_BOT_TOKEN=<your_token>
  TELEGRAM_CHAT_ID=<your_chat_id>
  ```
- Working directory: `c:\Users\bwang\Documents\GitHub\CL_Analyst_Development`

---

## Step 1 — Define the Experiment Manifest

Edit `configs/canary_batch_manifest.json`. Each entry in `experiments` defines one canary run:

```json
{
  "defaults": {
    "machine_type": "n2-highcpu-48",
    "provisioning_model": "SPOT",
    "gcs_data_path": "gs://cltrainer-optuna-results/data/cl-1h_bk_HourSet_04.parquet",
    "strategy_config": "ensemble4.json",
    "metrics": "logloss,average_precision",
    "timeout_minutes": 90,
    "max_concurrent_vcpus": 100,
    "vcpus_per_vm": 48
  },
  "experiments": [
    {
      "label": "Canary 3H",
      "target_long":  "TARGET_TRIPLE_2x1_3H_LONG",
      "target_short": "TARGET_TRIPLE_2x1_3H_SHORT",
      "gcs_prefix": "canary_3h"
    },
    {
      "label": "Canary 6H",
      "target_long":  "TARGET_TRIPLE_2x1_6H_LONG",
      "target_short": "TARGET_TRIPLE_2x1_6H_SHORT",
      "gcs_prefix": "canary_6h"
    }
  ]
}
```

**Per-experiment overrides** (add to any entry to override defaults):
- `machine_type`, `provisioning_model`, `gcs_data_path`, `strategy_config`
- `metrics`, `timeout_minutes`

**GCS prefix rules:**
- Use lowercase letters, numbers, and underscores only
- The orchestrator appends a timestamp automatically: `canary_3h` → `canary_3h_20260424_0954`
- This ensures no two runs ever share a GCS path or local directory

For a different batch, create a new manifest file and pass it with `-ManifestPath`:
```powershell
# Example: create configs\hourset05_batch.json for the next set of targets
```

---

## Step 2 — Verify Data on GCS (Critical)

> [!CAUTION]
> If the `gcs_data_path` specified in your manifest does not exist in Google Cloud Storage, the batch VM instances will crash immediately upon startup.

Before launching, explicitly verify that your data has been uploaded to the bucket path declared in the manifest.

```powershell
# Example: Ensure the parquet file actually exists
gcloud storage ls gs://cltrainer-optuna-results/data/cl-1h_bk_HourSet_08.parquet
```

If it fails to find the file, you must run the data processor and upload it manually before proceeding.

---

## Step 3 — Dry Run (validate before spending money)

```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\run_canary_batch.ps1 `
    -ManifestPath configs\canary_batch_manifest.json `
    -Zone "us-central1-a, us-central1-b, us-central1-c, us-central1-f" `
    -DryRun
```

This will:
- Print every experiment that would be deployed (VM name, GCS prefix, machine type, targets)
- Send a Telegram test message (verifies credentials are working)
- **Create zero VMs**

Review the output. If anything looks wrong, fix the manifest before continuing.

---

## Step 4 — Launch the Batch

> [!TIP]
> Telegram notifications are sent by default. If you wish to run the batch silently, pass the `-DisableTelegram` flag.

```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\run_canary_batch.ps1 `
    -ManifestPath configs\canary_batch_manifest.json `
    -Zone "us-central1-a, us-central1-b, us-central1-c, us-central1-f"
```

The orchestrator will:
1. Check current vCPU usage against the 100-vCPU quota cap
2. Fire a new VM when a slot is available (max 2 concurrent)
3. For each experiment:
   - Delete any pre-existing VM with that name (clean slate)
   - Deploy a fresh `n2-highcpu-48` VM via `gcp_deploy_canary.ps1`
   - Start `gcp_monitor.ps1` as a background job (5-min poll interval)
   - Send 🚀 Telegram on first heartbeat
   - Send ⚠️ Telegram on stale heartbeat (>10 min unchanged)
   - Send 💀/✅/❌ Telegram on completion
   - **Verify artifacts before deleting VM** (pipeline_summary.json + logs/ + *.pkl)
   - Delete VM to free quota for next experiment
4. Track progress in `reports/batch_runs/<batch_id>/batch_progress.json`
5. Send a final 🏁 Telegram with completion summary

**Leave this terminal window open** or run in a session that won't be interrupted. The PS background jobs are tied to this process.

**Expected timing (12 experiments, 2 concurrent):**
- ~6 rounds × ~30 min/round = ~3 hours wall time
- VM setup overhead: ~3–4 min per experiment (startup script)
- Total elapsed: ~3.5–4 hours

---

## Step 5 — Monitor Progress

### Telegram
All key events are sent automatically (unless `-DisableTelegram` is passed).

### Console
The orchestrator prints status every 30 seconds showing active job states and queue depth.

### Manual check (while batch is running)
```powershell
# See what's in batch_progress.json so far
Get-Content .\reports\batch_runs\<batch_id>\batch_progress.json | ConvertFrom-Json | Select completed,failed,total

# Check a specific VM's live logs
gcloud compute ssh optuna-canary-3h --zone=us-central1-a --command="tmux attach -t canary"
# Ctrl+B then D to detach without stopping

# Check GCS for a specific prefix
gcloud storage ls gs://cltrainer-optuna-results/canary_3h_<timestamp>/
```

---

## Step 6 — Collect Results

After the batch completes (or at any point to see partial results):

```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\collect_batch_results.ps1
```

Or target a specific batch:
```powershell
powershell -ExecutionPolicy Bypass -File .\gcp\collect_batch_results.ps1 `
    -BatchId batch_20260424_0954
```

This generates:
- `reports/batch_runs/<batch_id>/batch_summary.md` — full comparison table
- Console output of ensemble PF/PnL per experiment
- Telegram message with condensed results table

---

## Artifact Layout

Each completed experiment produces:
```
reports/
  batch_runs/
    batch_20260424_0954/
      batch_progress.json       ← orchestrator progress tracker
      batch_summary.md          ← generated by collect_batch_results.ps1
  canary_3h_20260424_0954/
    pipeline_summary.json       ← backtest metrics (all models + ensembles)
    run_report.md               ← monitor-generated summary
    logs/
      canary_run_*.log          ← full VM stdout log
    reports/
      ensemble_backtest_*.txt   ← detailed ensemble backtest reports
      backtest_report_*.txt     ← per-model backtest reports
    registry/
      E2E_HourSet_04_long_*/    ← trained model bundles
        final_model.pkl
        oos_predictions.csv
        backtest_report.txt
        feature_importance.csv
```

---

## Troubleshooting

### VM deploy fails with quota error
- Check: `gcloud compute regions describe us-central1 --format="table(quotas)"`
- The batch sends a 🚫 Telegram on quota failures and skips to the next experiment
- Remaining queued experiments will retry once a slot frees

### Heartbeat goes stale (⚠️ Telegram alert)
- The existing monitor auto-checks CPU load and optuna process count
- If load < 0.1 and no optuna processes → auto-stops VM after 15 min stale
- Check serial console: `gcloud compute instances get-serial-port-output optuna-canary-<prefix> --zone=us-central1-a`

### VM terminated with no log (💀 Telegram alert)
- Likely SPOT preemption or OOM before output was written
- OOM is auto-detected from serial console and included in the Telegram message
- The artifact gate will attempt 3 re-downloads; on failure, the VM is still deleted to free quota
- Partial results may be in GCS: `gcloud storage ls gs://cltrainer-optuna-results/<gcs_prefix>/`

### Artifact gate fails
- Manual recovery: `gsutil -m cp -r gs://cltrainer-optuna-results/<gcs_prefix>/ reports/<local_dir>/`
- Re-run collect_batch_results.ps1 to pick up any manually recovered artifacts

### Timeout (⏱️ Telegram alert)
- Default is 90 minutes per experiment. Increase `timeout_minutes` in manifest defaults
- A timed-out VM is stopped (not deleted) — check if results are in GCS before it's cleaned up

### SPOT preemption mid-run
- The monitor detects INTERRUPTED termination and flags it in the Telegram message
- Switch to STANDARD pricing for critical runs: set `"provisioning_model": "STANDARD"` in manifest defaults

---

## Running a Single Experiment (not a batch)

To run the original single-experiment workflow with Telegram support (which is now default):
```powershell
# Deploy
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_deploy_canary.ps1 `
    -VmName optuna-runner-canary `
    -GcsPrefix canary_3h `
    -TargetLong TARGET_TRIPLE_2x1_3H_LONG `
    -TargetShort TARGET_TRIPLE_2x1_3H_SHORT

# Monitor with Telegram (default)
powershell -ExecutionPolicy Bypass -File .\gcp\gcp_monitor.ps1 `
    -VmName optuna-runner-canary `
    -GcsPrefix canary_3h `
    -ExperimentLabel "Canary 3H" `
    -PollIntervalSeconds 120
```

---

## Important Notes

- **Never reuse the same GCS prefix** for two different experiments — results will be overwritten. The batch orchestrator handles this automatically via timestamps. For manual runs, always use a unique prefix.
- **CPU validation is strict**: `vm_canary_run.sh` will FATAL if `N_WORKERS × THREADS_PER_WORKER ≠ system CPUs`. The default `n2-highcpu-48` requires `N_WORKERS=4, THREADS_PER_WORKER=12`.
- **SPOT VMs can be preempted**. Use `"provisioning_model": "STANDARD"` in the manifest for runs that must complete uninterrupted.
- **GCS costs accumulate**: old experiment prefixes in GCS are not auto-deleted. Periodically run `gcloud storage rm -r gs://cltrainer-optuna-results/<old_prefix>/` to clean up.
