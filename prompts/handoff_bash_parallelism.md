# Agent Handoff: Process-Level Parallelism for Optuna Search

## Problem Statement

Our Optuna hyperparameter search ([agent/optuna_lgbm_search_v2.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/optuna_lgbm_search_v2.py)) crashes with **exit 139 (SIGSEGV)** when run with `n_jobs > 1`. Root cause: **LightGBM's C++ `Booster.__boost()` is not thread-safe** when multiple Optuna worker threads call `lgb.train()` concurrently in the same process. The `faulthandler` traceback confirmed the crash site — two threads simultaneously in `lightgbm/basic.py:4214 __boost`.

**The fix**: Run Optuna workers as **separate OS processes** instead of threads. Each process gets its own C heap — zero race conditions. JournalFileStorage already supports multi-process concurrent access, so no Python code changes are needed to the search logic itself.

## What to Implement

### 1. Default VM Machine Type → `n2-highcpu-48`

**File**: [gcp/gcp_deploy_canary.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_deploy_canary.ps1) (line 19)

Change the default `MachineType` from `n2-standard-48` (192 GB RAM) to `n2-highcpu-48` (48 GB RAM). Memory usage peaks at ~3 GB per process — 48 GB is more than enough for 4 parallel processes. This saves ~32% on hourly VM cost.

Also update [gcp/gcp_deploy_run.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_deploy_run.ps1) if it has a similar default.

### 2. Bash-Level Process Parallelism in Canary Script

**File**: [gcp/vm_canary_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_canary_run.sh)

Replace the current single-process invocation on each search:
```bash
python agent/optuna_lgbm_search_v2.py --n-jobs $N_JOBS --num-threads $NUM_THREADS ...
```

With 4 parallel background processes sharing the same study via JournalFileStorage:
```bash
N_WORKERS=4           # Number of parallel OS processes
THREADS_PER_WORKER=12 # LGB threads per worker (N_WORKERS × THREADS_PER_WORKER = 48 cores)
TRIALS_PER_WORKER=$((N_TRIALS / N_WORKERS))  # e.g., 20/4 = 5 each

WORKER_PIDS=()
for WORKER_ID in $(seq 1 $N_WORKERS); do
    python agent/optuna_lgbm_search_v2.py \
        --n-jobs 1 \
        --num-threads $THREADS_PER_WORKER \
        --n-trials $TRIALS_PER_WORKER \
        --study-name "$STUDY" \
        --db-dir "$DB_DIR" \
        --worker-id $WORKER_ID \
        ... other args ... \
        2>&1 &
    WORKER_PIDS+=($!)
done

# Wait for all workers and capture exit codes
WORKER_FAILURES=0
for i in "${!WORKER_PIDS[@]}"; do
    wait ${WORKER_PIDS[$i]}
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        echo "  Worker $((i+1)) FAILED (exit $EXIT_CODE)"
        WORKER_FAILURES=$((WORKER_FAILURES + 1))
    fi
done
```

> [!IMPORTANT]
> The CPU validation check at the top of the script needs updating. Currently it requires `N_JOBS × NUM_THREADS == nproc`. The new validation should be `N_WORKERS × THREADS_PER_WORKER == nproc`.

Do the same for [gcp/vm_production_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_production_run.sh).

### 3. Worker-Tagged Logging

**File**: [agent/optuna_lgbm_search_v2.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/optuna_lgbm_search_v2.py)

Add a `--worker-id` CLI argument. Tag ALL log output (print statements AND `[MEM]` lines) with the worker ID so logs from 4 processes writing to the same file are distinguishable:

```
[W1] [1/4] Loading data...
[W1] [MEM] after_data_load RSS=0.72GB peak=0.72GB
[W2] [1/4] Loading data...
[W1] [MEM] trial_start RSS=0.72GB trial=0
[W3] [1/4] Loading data...
...
```

Implementation approach:
- Add `--worker-id` to the argparse (default: `0` for backward compatibility)
- Create a helper that prefixes all print output: `_wprint(msg)` → `print(f"[W{worker_id}] {msg}")`
- OR monkey-patch `builtins.print` with a prefix (simpler, catches all output including library prints)
- Update [_log_mem()](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/optuna_lgbm_search_v2.py#100-111) to include the worker ID in its output

### 4. Per-Worker STATUS.json for Monitoring

**File**: [gcp/vm_canary_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_canary_run.sh) (and [vm_production_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_production_run.sh))

The existing monitoring ([gcp_monitor.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_monitor.ps1)) polls `STATUS.json` from GCS. Update the status tracking to show **per-worker** progress:

```json
{
    "search": "long_logloss",
    "search_num": 1,
    "total_searches": 4,
    "workers": {
        "W1": {"status": "running", "trials_done": 3, "trials_total": 5, "pid": 12345},
        "W2": {"status": "running", "trials_done": 2, "trials_total": 5, "pid": 12346},
        "W3": {"status": "completed", "trials_done": 5, "trials_total": 5, "pid": 12347},
        "W4": {"status": "running", "trials_done": 4, "trials_total": 5, "pid": 12348}
    },
    "total_trials_done": 14,
    "total_trials_target": 20,
    "failed_workers": 0,
    "last_update": "2026-03-22T21:32:33+00:00"
}
```

To enable this, the Python script should write a per-worker status file after each completed trial:
- [agent/optuna_lgbm_search_v2.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/optuna_lgbm_search_v2.py) writes `/tmp/worker_W{id}_status.json` with `{"trials_done": N, "status": "running"}` after each trial completes
- The bash script reads all worker status files and aggregates them into the main `STATUS.json` before uploading to GCS
- When a worker finishes (exits 0), its status becomes `"completed"`; on exit ≠ 0, it becomes `"failed"`

### 5. Monitor Script Updates

**File**: [gcp/gcp_monitor.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_monitor.ps1)

Update the monitor to display the new per-worker status:

```
[14:33:43] VM=RUNNING | Search 1/4: LONG logloss
  W1: 3/5 trials ██████░░░░ 60%
  W2: 2/5 trials ████░░░░░░ 40%
  W3: 5/5 trials ██████████ DONE ✓
  W4: 4/5 trials ████████░░ 80%
  Total: 14/20 trials | elapsed=4.2m | heartbeat=22s ago
```

## Key Design Constraints

1. **`n_jobs` must be 1 in the Python script** — this is non-negotiable because `n_jobs > 1` uses Optuna's threading which triggers the LightGBM C++ segfault
2. **JournalFileStorage** is already used and supports multi-process access — no storage changes needed
3. **All workers share the same log file** via `tee -a` — the `[W{id}]` prefix distinguishes them
4. **Worker count × threads per worker must equal total vCPUs** — e.g., 4 × 12 = 48 for n2-highcpu-48
5. **The search script itself ([optuna_lgbm_search_v2.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/optuna_lgbm_search_v2.py)) should NOT be restructured** — only add `--worker-id` arg and tagged logging. The parallelism happens at the bash level.
6. **Keep `free_raw_data=False`** and `faulthandler.enable()` — defense-in-depth + crash diagnostics

## Files to Modify

| File | Changes |
|------|---------|
| [gcp/gcp_deploy_canary.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_deploy_canary.ps1) | Default `MachineType` → `n2-highcpu-48` |
| [gcp/gcp_deploy_run.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_deploy_run.ps1) | Same machine type change |
| [gcp/vm_canary_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_canary_run.sh) | Process parallelism loop, update CPU validation, worker status aggregation |
| [gcp/vm_production_run.sh](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/vm_production_run.sh) | Same parallelism changes |
| [agent/optuna_lgbm_search_v2.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/agent/optuna_lgbm_search_v2.py) | Add `--worker-id` arg, tagged logging (`[W{id}]`), per-worker status file writes |
| [gcp/gcp_monitor.ps1](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/gcp/gcp_monitor.ps1) | Display per-worker progress bars from new STATUS.json format |

## Verification Plan

1. Deploy to `n2-highcpu-48` with the parallel process approach
2. Run the canary (4 searches × 20 trials each)
3. Confirm **zero exit-139 crashes** — separate processes eliminate the C++ race
4. Verify `[MEM]` logs show each worker's RSS independently (should be ~2-3 GB each, ~10 GB total)
5. Verify monitor shows per-worker progress updating in real-time
6. Verify all workers' trials are correctly saved to the shared JournalFileStorage
