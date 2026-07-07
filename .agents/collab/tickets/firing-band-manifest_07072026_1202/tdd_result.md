# TDD Result — firing-band-manifest_07072026_1202

**Branch:** `training-update` (not switched; not committed — tree left uncommitted for review)
**Env:** `trader` conda env
**Outcome:** GREEN. 14 new tests pass; full fast suite = **1692 passed / 10 failed**, where the
10 failures are exactly the known pre-existing ES01B sentinels (nothing new broke).

## Requirement
Make the post-optimizer's signal-firing band (introduced in ticket 1a, ddaff90) **manifest-tunable**
via OPTIONAL `ExecutionWorkflowConfig` fields with explicit non-None defaults `0.05` / `0.45`.
Best-effort override: manifests that omit the band (all 36 existing) keep `[0.05, 0.45]`, so there is
no blast radius and no shell edits.

## Files changed
1. `src/config/schemas.py` — `ExecutionWorkflowConfig`:
   - Added `firing_frac_min: float = 0.05` and `firing_frac_max: float = 0.45` (OPTIONAL, explicit
     non-None defaults).
   - Added a `@model_validator(mode="after")` `validate_firing_band` asserting
     `0.0 < firing_frac_min < firing_frac_max <= 1.0` (rejects inverted / zero-width / negative / >1.0).
   - Existing `slippage_per_side` / `execution_data_path` validators left intact.
2. `agent/batch_post_optimizer.py`:
   - Imported `FIRING_FRAC_MIN` / `FIRING_FRAC_MAX` from `strategy_optimizer`.
   - Added `resolve_firing_band(manifest) -> (float, float)` helper reading
     `baseline.execution_workflow.firing_frac_min/max`, falling back to the module constants when absent
     (also handles a missing `execution_workflow`).
   - Added `firing_frac_min` / `firing_frac_max` params (default = module constants) to
     `run_single_optimization(...)` and passed them into the `run_optimization(...)` call
     (`run_optimization` already accepts these kwargs from 1a).
   - In `main()`: resolve the band from the batch `manifest.json` after economics resolution, store on
     `args`, and print one line showing the effective band + source (manifest vs default).
   - Threaded `args.firing_frac_min/max` into BOTH `run_single_optimization` call sites in
     `_run_all_objectives_concurrent` (workers>1 pool-submit path and workers==1 sequential path).
3. `configs/batch_manifest_v2_hourset14a_scout.json` and
   `configs/batch_manifest_v2_hourset14b_scout.json` — added
   `"firing_frac_min": 0.05, "firing_frac_max": 0.45` to `baseline.execution_workflow` as documented
   example. The other 34 manifests were NOT touched.
4. `tests/test_firing_band_manifest.py` — NEW test module (14 tests).

**Not touched (per scope):** `gcp/vm_post_optimize.sh`, any `.ps1`, the other 34 manifests.

## Tests (all new, all passing)
Schema: defaults-when-omitted (0.05/0.45), custom valid parses, inverted raises, equal raises,
negative/zero raises, >1.0 raises, max==1.0 allowed, real unedited on-disk manifest (14A canary) still
validates & defaults, both edited example manifests carry & validate the band.
batch_post_optimizer: `resolve_firing_band` reads custom band, falls back to module constants when band
absent, and when `execution_workflow` absent.
Threading: `run_single_optimization` forwards `firing_frac_min/max` into `run_optimization` (custom and
default-to-module-constants), asserted via monkeypatch capturing kwargs (no full optimization run).

## Red → Green evidence
- RED: all 14 new tests failed for the right reasons (missing attributes / missing helper / unexpected
  kwarg), not tautologies.
- GREEN: `tests/test_firing_band_manifest.py` → 14 passed; full fast suite
  `pytest tests/ -m "not slow"` → 1692 passed, 10 failed (only the known ES01B sentinels in
  test_config_generator_symbols, test_hourly_only_equity_session, test_instrument_context,
  test_shallow_5m_bootstrap).

## Validation (post-green)
- `configs/batch_manifest_v2_hourset14b_scout.json` loads through `BatchSweepConfig` with the new example
  fields (test_example_manifest_with_band_validates).
- A manifest without the fields still validates and yields `(0.05, 0.45)`
  (test_existing_manifest_without_band_still_validates).

## Deviations
None. Scope implemented exactly as blueprinted. The validator uses a model-validator (allowed by the
blueprint's "@field_validator or model validator") because the invariant spans two fields.
