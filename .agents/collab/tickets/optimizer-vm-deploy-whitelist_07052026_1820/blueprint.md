# Ticket Resolution Blueprint — optimizer-vm-deploy-whitelist_07052026_1820
**Ticket Directory:** `.agents/collab/tickets/optimizer-vm-deploy-whitelist_07052026_1820/`

## Bug Summary
`gcp/gcp_deploy_optimizer.ps1` ships a hand-curated 29-file whitelist to the post-optimizer
VM. `agent/generate_ensemble_artifacts.py:27` (T6, commit 03218af, same-day) imports
`src.live_execution.instrument_context`, which is not whitelisted →
ModuleNotFoundError on the VM → pair selection emits 0 pairs → batch fatal + VM self-delete.
Four batches killed today by this class (0840/0842 on src.core — patched incompletely in
c2c2adf — then 1606 GC and 1715 NG on instrument_context). Structural cause: a manually
maintained mirror of a living import graph. Secondary finding: `python ... 2>&1 | tee` in
`vm_post_optimize.sh` masks non-zero python exits under `set -e` (tee returns 0), so the
script limps past the true failure to a misleading "0 pairs" FATAL.

Auditor RCA + Reviewer approval (5 mandatory conditions, pipefail in-scope) in
`ticket_audit_log.md`. Severity MEDIUM; same-day regression — full review chain used, no
fast-track.

## Target Files
- `gcp/gcp_deploy_optimizer.ps1`
- `gcp/vm_post_optimize.sh`

## Required Changes
1. Replace steps [3/7] (per-file mkdir tree) + [4/7] ($codeFiles whitelist scp loop) in
   `gcp_deploy_optimizer.ps1` with the whole-tree zip mechanism transplanted from
   `gcp_deploy_sweep.ps1` (~lines 161-217): per-VM `deploy_$VmName.zip` built with
   `tar -a -c -C $ProjectDir --exclude=__pycache__ --exclude=*.parquet --exclude=*.csv`
   over existence-checked items `src, agent, gcp, requirements.txt, .env` → single scp →
   `unzip -q -o` on the VM → verify + retry.
   MANDATORY CONDITIONS (reviewer):
   a. Keep `mkdir -p $RemoteProject/configs/strategies` (zip omits configs/; the
      strategy-config scp and ensemble mode depend on it).
   b. Verification sentinel = `gcp/vm_post_optimize.sh` (NOT the sweep's vm_sweep_run.sh).
   c. Retain the existing [4b/7] SHA256 stale-disk check unchanged.
   d. CRLF-fix + `chmod +x gcp/*.sh` block stays AFTER unzip (current ordering).
   e. Per-VM zip name + local `Remove-Item` cleanup retained.
   Keep the configs uploads (global_risk_filters.json, configs/strategies/*.json) as-is.
2. `vm_post_optimize.sh`: add `set -o pipefail` beside `set -e` so piped python/gsutil
   failures become fatal at the true failure point (existing `|| { ... }` soft-failure
   guards on uploads/config-gen are preserved semantics under pipefail).
