# Ticket Resolution Blueprint — autorename-stale-config-paths_07112026_1003
**Ticket Directory:** `.agents/collab/tickets/autorename-stale-config-paths_07112026_1003/`

## Bug Summary
The batch AUTO-STAMP/auto-rename step in `gcp/run_sweep_batch.ps1` (block ~lines 1240-1295, introduced by ticket `block-sharpe-objective-ab_07092026_1031`, commit `96cd1ef`) renames a completed batch output dir `batch_<ts>` → `batch_<ts>_<SYMBOL>_<TIER>[_OBJAB]`. The ensemble configs written by the VM bake ABSOLUTE `predictions_path` strings pointing at `reports/batch_runs/batch_<ts>/predictions/...`; after the rename those paths are stale and the downstream config-validation gate (build-symbol-pipeline Phase 6) fails with "predictions_path not found".

**The core fix already landed at HEAD** (commit `b7d2c63`, BOM fix `715c52f`): a rewrite loop at `gcp/run_sweep_batch.ps1:1272-1288` replaces `batch_runs/$BatchId/` → `batch_runs/$stampName/` inside the stamped configs, mirroring the manual NG repair, and it deliberately leaves `model_path` untouched (model_path is sweep-rooted at `reports/sweep_*`, not under `batch_runs/<id>/`, so the string replace never matches it). Normal auto-renamed runs are therefore already correct. The NG batch `batch_20260711_061128_NG_SCOUT` broke only because it went through a manual recovery path that bypassed this rewrite, and it has been hand-repaired (read-only, do not touch).

**Residual defect (this ticket):** the existing rewrite loop SILENTLY NO-OPS in two branches, violating the project's crash-loudly / no-silent-defaults rule:
1. `if (Test-Path $stampedCfgDir)` at ~line 1273 has no `else` — if the stamped `configs/` dir is missing, the rewrite is silently skipped and the operator only learns later via an opaque Phase-6 "predictions_path not found".
2. `if ($cfgPatched -gt 0)` at ~line 1285 prints a success line only when >0 — when 0 configs matched, nothing is printed, hiding that no path was rewritten.

## Root Cause (file:line)
- `predictions_path` stamped absolute in `agent/generate_ensemble_artifacts.py` (~:460 / :475 / :479) using `args.batch_dir = batch_runs/batch_<ts>`.
- `Rename-Item` at `gcp/run_sweep_batch.ps1:1265` moves the dir; the rewrite at `:1272-1288` corrects the embedded paths.
- `model_path` at `generate_ensemble_artifacts.py` (~:474 / :478) is sweep-rooted → not matched by the `batch_runs/<id>/` replace → safe and must remain untouched.
- Residual: the rewrite's two `else`-less branches (`:1273`, `:1285`) swallow the "nothing rewritten" case instead of surfacing it.

## Severity & Routing
- Severity: **LOW** (isolated, additive observability). Because it is a **recent regression** it was NOT fast-tracked; it went through the Impact-Reviewer, which **APPROVED** (no Interface / Base Class / Refactor rule triggered; no human authorization required; no veto loop).

## Target Files
- `gcp/run_sweep_batch.ps1` (the two additive `else` warning branches — REQUIRED)
- A new pytest test file under the project's tests dir (e.g. `tests/`) — OPTIONAL logic-guard (see caveat below)

## Required Changes

### 1. `gcp/run_sweep_batch.ps1` — loud warnings on the two silent no-op branches (REQUIRED)
Both changes are purely ADDITIVE `else` branches emitting `Write-Host` WARNINGS (Yellow). They are **WARNINGS, not throws** — the entire auto-stamp block is wrapped in a try/catch (~:1246 / :1293) that is contractually designed to NEVER fail the batch run; the hard-fail responsibility belongs to the downstream Phase-6 config-validation gate. The warnings exist to make the silent no-op VISIBLE in the batch log.

(a) Add an `else` to `if (Test-Path $stampedCfgDir)` (~line 1273). The `else` must emit a loud Yellow `Write-Host` WARNING stating that the stamped configs dir was NOT found and that the embedded `predictions_path` values were NOT rewritten — including the offending `$stampedCfgDir` path in the message so it is actionable.

(b) Add an `else` to `if ($cfgPatched -gt 0)` (~line 1285). The `else` must emit a loud Yellow `Write-Host` WARNING stating that ZERO config paths matched / were rewritten (i.e. no `batch_runs/$BatchId/` segment was found in any config) — surfacing that the expected repoint did not happen.

CONSTRAINTS:
- Do NOT modify the `.Replace("batch_runs/$BatchId/", "batch_runs/$stampName/")` at ~:1277 — `model_path` MUST stay untouched.
- Do NOT change the try/catch contract; the warnings must run only when their `if` condition was false, and must not themselves throw or mask a prior exception.
- Do NOT change any function signature or shared utility (none exist here — this is a leaf script).

### 2. Optional logic-guard pytest (OPTIONAL — additive)
Add a pytest that mirrors the string-replace logic against a fixture: an ensemble config JSON containing an OLD-batch-id `predictions_path` (e.g. `reports/batch_runs/batch_<ts>/predictions/foo.csv`) PLUS a sweep-rooted `model_path` (e.g. `reports/sweep_<...>/model.pkl`). Apply the same `batch_runs/<old_id>/` → `batch_runs/<new_stamped_id>/` replacement and assert:
- the `predictions_path` was repointed to the new stamped dir, AND
- the `model_path` was left UNCHANGED.

**CAVEAT (record explicitly):** the real rewrite is PowerShell. This pytest can only mirror the string-replace LOGIC in Python against a fixture — it is a **LOGIC-GUARD, NOT end-to-end coverage** of the PowerShell block. Label the test/docstring as such so it is not mistaken for an e2e test of the PS auto-stamp step.

## Reference behavior to codify
The manual NG repair (`batch_20260711_061128_NG_SCOUT`) repointed all 8 `predictions_path` refs to the `_NG_SCOUT` dir and left `model_path` alone — that is exactly what the `:1272-1288` rewrite produces and what the logic-guard test should assert.
