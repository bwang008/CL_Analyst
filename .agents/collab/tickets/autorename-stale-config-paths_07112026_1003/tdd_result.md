# TDD Result — autorename-stale-config-paths_07112026_1003
**Ticket Directory:** `.agents/collab/tickets/autorename-stale-config-paths_07112026_1003/`
**Outcome:** GREEN — all approved changes implemented and verified via TDD.

## What was fixed
The batch auto-stamp config-path rewrite loop in `gcp/run_sweep_batch.ps1` (~:1272-1288) silently no-opped in two branches, violating the crash-loudly / no-silent-defaults rule: (1) when the stamped `configs/` dir was missing, and (2) when zero configs were patched. In both cases the operator got no signal — the only symptom surfaced later as an opaque Phase-6 "predictions_path not found". Two additive Yellow `Write-Host` WARNING `else` branches were added so the silent no-op is now loud in the batch log.

The core path-rewrite (predictions_path repointed to the stamped dir; model_path left untouched because it is sweep-rooted) already landed at HEAD (commit b7d2c63); this ticket only adds the missing observability, per the approved blueprint.

## Files changed
- `gcp/run_sweep_batch.ps1` — added two additive `else` warning branches (implementation). Surgical Edit; `.Replace("batch_runs/$BatchId/", ...)`, the BOM-less WriteAllText, Rename-Item, the never-fail try/catch, and the Green success lines are all UNTOUCHED. model_path untouched. No BOM/line-ending change.

### Exact diff
```diff
                 }
                 if ($cfgPatched -gt 0) {
                     Write-Host "  Rewrote embedded batch-dir paths in $cfgPatched config(s) to the stamped name." -ForegroundColor Green
+                } else {
+                    Write-Host "  WARNING: 0 config(s) matched -- no 'batch_runs/$BatchId/' segment was found in any config, so ZERO config paths were rewritten in $stampedCfgDir. The embedded predictions_path values may be stale and the Phase-6 config-validation gate may fail." -ForegroundColor Yellow
                 }
+            } else {
+                Write-Host "  WARNING: stamped configs dir NOT found: $stampedCfgDir -- the embedded predictions_path values were NOT rewritten, so the Phase-6 config-validation gate will fail." -ForegroundColor Yellow
             }
```

## Test added
- `tests/test_autostamp_config_rewrite.py` — NEW, 12 tests, 4 classes. Strict-Lock: TRUE. Targets the `gcp/run_sweep_batch.ps1` auto-stamp config-path rewrite block.
  - Strategy A (primary): real `powershell.exe` (skipif-guarded off-Windows) drives the EXACT extracted rewrite expression + BOM-less write against `tmp_path` fixtures — asserts predictions_path repointed, model_path byte-for-byte unchanged, no UTF-8 BOM, and the two `else`-branch WARNINGS are emitted on the missing-configs-dir / zero-patched paths.
  - Plus a Python logic-guard mirror (labeled explicitly NOT e2e) and text-scan tests pinning that the two `else` branches exist in the shipped script.
  - It is a LOGIC-GUARD around the extracted PS expression, not an end-to-end run of the full 1240-1295 block (per Impact-Reviewer caveat).

## Test runs (TDD-Manager, independent)
- RED (before fix, new file): 4 failed / 8 passed — the 4 failures were exactly the two missing `else` branches (real-PS warning tests + text-scan tests).
- RED baseline (full fast suite): 4 failed / 2083 passed — only the 4 fix-pinning tests failed; NO unrelated breakage.
- GREEN (after fix, full fast suite): `conda run -n trader python -m pytest tests/ -m "not slow"` -> **2087 passed, 0 failed** (0:05:53). All 4 previously-RED tests now pass; no regressions.

## Constraints honored
- No commit. No VM start/stop. Live SI batch (batch_20260711_094042) and hand-repaired NG batch (batch_20260711_061128_NG_SCOUT) untouched (only `tmp_path` fixtures used). Change confined to pipeline script + new test.
