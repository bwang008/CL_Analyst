# Blueprint — screen-cloud-orchestration_07072026_1248  (S4, HUMAN-APPLIED)
**Ticket Directory:** `.agents/collab/tickets/screen-cloud-orchestration_07072026_1248/`
**Branch:** `training-update`
**Status:** ⚠️ **NOT auto-implemented.** This edits cloud-critical PowerShell/bash that cannot
be validated without a billable GCP dry-run. Apply it yourself (or via a ticket) and validate
with a canary before trusting it. The Stage-1 screen already works via the CLI
([/cloud-target-batch](../../workflows/cloud-target-batch.md)); this ticket only adds the cloud
FAN-OUT so many symbols×targets screen in parallel.

## Goal
Make `run_sweep_batch.ps1` support a screen batch: when the manifest's
`baseline.training_workflow.mode == "screen"`, the sweep VMs run the fixed-param AUC screen
(skip Optuna) and there is **no post-optimizer stage**; the orchestrator collects each
experiment's `AUC_Model_Report.md`. Behavior for `mode == "optimize"` (default) must be
**byte-identical** to today.

## How `mode` flows (already in place)
`training_workflow.mode` was added to the schema in ticket `cloud-target-screen-core_07072026_1223`
(commit `3c91445`) and `vm_e2e_pipeline.py --mode screen` already works end-to-end. This ticket
only teaches the orchestration layer to select it. Read `mode` from
`$mf.baseline.training_workflow.mode` (PowerShell) and `master_config.training_workflow.mode`
(orchestrator.py); default `"optimize"` when absent.

## Target Files & Required Changes

### 1. `gcp/orchestrator.py` (VM-side; ~195 lines) — the clean fork
- Read `mode` from the loaded master config (default `"optimize"`).
- **If `mode == "screen"`:** SKIP the Optuna search loop (currently ~lines 134–150, the
  per-direction×metric `optuna_lgbm_search_v2.py` Popen loop) AND replace the optimize E2E call
  (~lines 175–188) with a single `vm_e2e_pipeline.py --master-config <cfg> --mode screen
  --output-dir <out> --random-seed <seed>` subprocess. Upload the resulting
  `AUC_Model_Report.md` (and per-target rows if emitted) to GCS like other artifacts.
- **Else:** current path unchanged.

### 2. `gcp/run_sweep_batch.ps1` (~1141 lines) — guard every optimize-only assumption
- **Dry-run validation gate (~lines 420–472):** the checks that require
  `post_optimizer_holdout_months > 0` and `opt_mode ∈ {individual, ensemble}` must be **skipped
  when `mode == "screen"`** (screen has no post-optimizer). Add a mode read near the manifest
  parse and wrap those asserts in `if ($mode -ne 'screen')`. Keep `train_cutoff_date` and
  dataset checks for both modes.
- **Post-optimizer deploy (~line 1012, `gcp_deploy_optimizer.ps1`):** wrap in
  `if ($mode -ne 'screen')` — screen batches deploy NO optimizer VM.
- **Artifact-verification gate (`Test-ArtifactsDownloaded`, ~line 229):** for screen mode,
  success = each experiment produced `AUC_Model_Report.md` (instead of `*.pkl` + summary).
  Branch on `$mode`.
- **Final reports:** for screen mode, consolidate the per-experiment `AUC_Model_Report.md` into
  one batch-level ranked report (optional; per-experiment reports are enough to start).

### 3. `gcp/vm_sweep_run.sh`
- Likely NO change — it just calls `orchestrator.py`. Confirm it forwards `--master-config`
  unchanged (it does today).

## Manifest shape for a screen batch
A v2 batch manifest (`BatchSweepConfig`) with:
- `baseline.training_workflow.mode = "screen"`, `target_columns` = the screen grid (or split
  across `experiments[]` so each VM screens a slice — fan-out unit = per experiment).
- `baseline.execution_workflow` can be omitted (screen needs no backtest). If the dry-run still
  demands it, either relax that in step 2 or include a benign stub.
- Note: the dataset must already exist in GCS (screen does not regenerate data).

## Validation (MANUAL — you launch; billable)
1. **No-regression:** run an existing `mode=optimize` canary (e.g.
   `batch_manifest_v2_hourset14a_canary.json`) end-to-end and confirm identical behavior
   (post-optimizer still runs, same artifacts). This proves the guards didn't break the default path.
2. **Screen canary:** a small screen manifest (2–4 targets, one symbol) →
   `& .\gcp\run_sweep_batch.ps1 -ManifestPath <screen_manifest> -DryRun` passes, then a live run;
   confirm: NO `opt-post-*` VM is created, each VM emits `AUC_Model_Report.md`, and the batch
   completes + tears down cleanly (check the orphaned-VM rules in run-cloud-batch.md).
3. Only after both pass, commit on `training-update`.

## Risk callouts
- The dry-run gate edit is the highest risk — a wrong guard could let a broken optimize manifest
  through, or block a valid one. Keep the change strictly additive (`if screen: skip; else:
  exactly-as-before`).
- Do NOT weaken the orphaned-VM / `--max-run-duration` TTL protections.
- Screen VMs are short (fixed-param train, seconds/target) — keep the existing TTL; they'll
  finish well within it.
