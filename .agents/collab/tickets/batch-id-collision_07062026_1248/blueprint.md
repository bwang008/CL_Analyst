# Ticket Resolution Blueprint — batch-id-collision_07062026_1248
**Ticket Directory:** `.agents/collab/tickets/batch-id-collision_07062026_1248/`

## Bug Summary
When two instances of `run_sweep_batch.ps1` (or `run_canary_batch.ps1`) are launched within the same calendar minute, they generate identical batch IDs because the timestamp format `yyyyMMdd_HHmm` only has minute-level granularity. This causes directory collisions (`reports/batch_runs/batch_<timestamp>/`), VM name collisions (`opt-post-batch-<id>`), and corrupted/mixed results across both runs.

**Root cause:** The `Get-Date -Format` call uses `HHmm` (hours+minutes) instead of `HHmmss` (hours+minutes+seconds).

All downstream consumers (Python scripts, `vm_post_optimize.sh`, `collect_batch_results.ps1`) treat the batch ID as an opaque string — no regex parsing of the timestamp suffix — so changing the format is safe.

## Target Files
- `gcp/run_sweep_batch.ps1` — line 49: batch timestamp generation
- `gcp/run_canary_batch.ps1` — line 47: batch timestamp generation

## Required Changes

### 1. `gcp/run_sweep_batch.ps1` (line 49)
Change the `Get-Date` format string from `"yyyyMMdd-HHmm"` to `"yyyyMMdd-HHmmss"` to include seconds in the batch timestamp. This single-character addition (`ss`) extends the batch ID from minute-level to second-level granularity, providing 60× collision resistance.

**Before:** `$BatchTimestamp = Get-Date -Format "yyyyMMdd-HHmm"`
**After:** `$BatchTimestamp = Get-Date -Format "yyyyMMdd-HHmmss"`

### 2. `gcp/run_canary_batch.ps1` (line 47)
Apply the identical fix: change the `Get-Date` format string from `"yyyyMMdd_HHmm"` to `"yyyyMMdd_HHmmss"`.

**Before:** `$BatchTimestamp = Get-Date -Format "yyyyMMdd_HHmm"`
**After:** `$BatchTimestamp = Get-Date -Format "yyyyMMdd_HHmmss"`

### No changes needed elsewhere
- `gcp/gcp_deploy_optimizer.ps1` derives VM names dynamically from `$BatchId` via `$BatchId.Replace('_','-')` — it will automatically incorporate the seconds.
- All other scripts treat the batch ID as an opaque string.
