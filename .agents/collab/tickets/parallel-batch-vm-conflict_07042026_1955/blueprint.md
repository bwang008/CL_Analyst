# Ticket Resolution Blueprint — parallel-batch-vm-conflict_07042026_1955
**Ticket Directory:** `.agents/collab/tickets/parallel-batch-vm-conflict_07042026_1955/`

## Bug Summary
The `run_sweep_batch.ps1` (and `run_canary_batch.ps1`) orchestrators cannot safely run multiple instances in parallel because the post-optimizer phase uses a hardcoded VM name `optuna-post-optimizer` in both the deploy script and the orchestrators. When two batches reach post-optimization concurrently, the second batch connects to the first batch's VM, kills its tmux session, overwrites its code, and leaves the first batch's orchestrator hanging indefinitely.

**Root Cause:** The post-optimizer VM name (`optuna-post-optimizer`) and tmux session name (`optimizer`) are hardcoded constants instead of being derived from the batch-unique `$BatchId`/`$BatchTimestamp`.

## Target Files
- `gcp/run_sweep_batch.ps1`
- `gcp/run_canary_batch.ps1`
- `gcp/gcp_deploy_optimizer.ps1`

## Required Changes

### 1. `gcp/run_sweep_batch.ps1` (~L983, ~L1012)

**L983 — Derive VM name from batch timestamp:**
- The current line `$optVmName = "optuna-post-optimizer"` must be changed to derive a unique name from `$BatchTimestamp` (which is already available in scope and used for sweep VM naming).
- The pattern should be `"optuna-post-opt-$BatchTimestamp"` (or equivalent that stays within GCP's 63-char VM name limit and uses only lowercase letters, digits, and hyphens).
- Verify that `$BatchTimestamp` is in scope at this point in the script. If not, derive from `$BatchId` which is always available.

**L1012 — Pass VM name to deploy script:**
- Add `-VmName`, `$optVmName` to the `$optArgs` array that is passed to `gcp_deploy_optimizer.ps1`.
- This ensures the deploy script uses the batch-unique name instead of its hardcoded default.

**Downstream references (L1056, L1061, L1089):**
- These already use the `$optVmName` variable for monitoring, polling, and deletion. No changes needed — they propagate automatically once L983 is fixed.

---

### 2. `gcp/run_canary_batch.ps1` (~L647–L658, ~L665, ~L691)

**This file has the identical bug and requires the identical fix pattern:**

**~L658 — Derive VM name from batch timestamp:**
- Change `$optVmName = "optuna-post-optimizer"` to derive from the canary batch's timestamp/ID using the same pattern as Fix 1.

**~L647–L653 — Pass VM name to deploy script:**
- Add `-VmName`, `$optVmName` to the `$optArgs` array passed to `gcp_deploy_optimizer.ps1`.

**L665, L691:**
- Already use `$optVmName` — propagates automatically.

---

### 3. `gcp/gcp_deploy_optimizer.ps1` — Two sub-fixes:

**3a. Parameter default (~L16):**
- Keep the existing default `$VmName = "optuna-post-optimizer"` for backward compatibility with standalone CLI invocations.
- Add a comment: `# Callers MUST pass a batch-unique name for parallel safety`

**3b. Tmux session name (~L293, ~L301-L302):**
- The tmux session name is hardcoded to `optimizer` in both the launch command and the verification check.
- Create a variable `$tmuxSession` derived from `$BatchId` (e.g., `"opt-$BatchId"` or `"opt-$VmName"`).
- Replace all occurrences of the hardcoded `optimizer` session name with `$tmuxSession`:
  - L293: `tmux kill-session -t optimizer` → `tmux kill-session -t $tmuxSession`
  - L293: `tmux new-session -d -s optimizer` → `tmux new-session -d -s $tmuxSession`
  - L301-L302: `tmux has-session -t optimizer` → `tmux has-session -t $tmuxSession`

## Verification Plan

### Automated Tests
- Run existing test suite: `python -m pytest tests/ -x -q`
- No new unit tests are strictly required since these are infrastructure/deployment scripts, but the dry-run gate should be exercised.

### Manual Verification
1. **Dry-run validation**: Run `run_sweep_batch.ps1 -DryRun` and verify the logged VM name includes the batch timestamp.
2. **Parallel canary test**: Launch two canary batches with different manifests simultaneously and verify:
   - Each creates a uniquely-named optimizer VM
   - Each tmux session has a unique name
   - Both complete independently without interference
3. **Standalone backward compatibility**: Run `gcp_deploy_optimizer.ps1` directly (without `-VmName`) and verify it falls back to the default `optuna-post-optimizer` name.

## Non-Functional Follow-Up (Separate PR, Low Priority)
These files contain the hardcoded name in documentation/examples and should be updated for consistency:
- `docs/prompts/run_local_optimizer_prompt.md` L77–78
- `.agents/workflows/build-symbol-pipeline.md` L118
