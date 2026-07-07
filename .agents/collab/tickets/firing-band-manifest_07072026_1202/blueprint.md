# Ticket Resolution Blueprint — firing-band-manifest_07072026_1202
**Ticket Directory:** `.agents/collab/tickets/firing-band-manifest_07072026_1202/`
**Branch:** `training-update` (do NOT touch `stable-fleet`)
**Depends on:** `dynamic-entry-threshold_07072026_1011` (commit `ddaff90`) — already merged on this branch.

## Change Summary
Ticket 1a made the post-optimizer derive per-model entry-threshold bounds from a
signal-firing band, defaulting to module constants `FIRING_FRAC_MIN=0.05` /
`FIRING_FRAC_MAX=0.45` (already active in cloud batches via `run_optimization`'s defaults).
This ticket makes that band **manifest-tunable** so it becomes part of the source-of-truth
config, without changing behavior for any existing manifest.

**Key safety property:** because 1a set correct module-constant defaults, this ticket is a
*best-effort override* — if the manifest omits the band (all 36 existing manifests do), the
optimizer keeps using `[0.05, 0.45]`. So there is no blast radius and no shell edits.

## Design decisions (follow exactly)
- Fields are **OPTIONAL with explicit non-None defaults `0.05` / `0.45`** (NOT required) —
  a required field would break all 36 existing `configs/batch_manifest_v2_*.json`. This does
  NOT violate the no-silent-null rule: the default is explicit, documented, non-None, and
  logged (it is not a hidden `None` that silently changes PnL).
- **Pure-Python threading, no shell edits.** `agent/batch_post_optimizer.py` already opens
  `{batch_dir}/manifest.json`; read the band there and thread it down. Do NOT modify
  `gcp/vm_post_optimize.sh` or any `.ps1`.

## Target Files
- `src/config/schemas.py` — `ExecutionWorkflowConfig`
- `agent/batch_post_optimizer.py` — read band from manifest + thread to `run_optimization`
- `configs/batch_manifest_v2_hourset14a_scout.json` and `..._hourset14b_scout.json` — add the
  fields as a documented example (source-of-truth manifests only; do NOT touch the other 34)
- `tests/` — new test module

## Required Changes
1. **`src/config/schemas.py` → `ExecutionWorkflowConfig`:** add
   `firing_frac_min: float = 0.05` and `firing_frac_max: float = 0.45`. Add a
   `@field_validator` (or model validator) asserting `0.0 < firing_frac_min < firing_frac_max <= 1.0`
   (reject inverted/out-of-range bands loudly). Keep the existing `slippage_per_side` /
   `execution_data_path` validators intact.
2. **`agent/batch_post_optimizer.py`:**
   - In `main()`, load the batch manifest (`{args.batch_dir}/manifest.json`, already the
     pattern used by `find_ohlcv_path`) and read
     `baseline.execution_workflow.firing_frac_min` / `firing_frac_max`. If absent, fall back to
     `strategy_optimizer.FIRING_FRAC_MIN` / `FIRING_FRAC_MAX` (import them). Store on args and
     print one line showing the effective band + source (manifest vs default).
   - Add `firing_frac_min` / `firing_frac_max` params to `run_single_optimization(...)` and
     pass them into the `run_optimization(...)` call (line ~349). `run_optimization` already
     accepts these kwargs (added in 1a).
   - Thread the band into BOTH `run_single_optimization` call sites in `main()` (the single-side
     path ~line 900 and the pair/ensemble path ~line 935).
3. **Example manifests (14a_scout, 14b_scout only):** add
   `"firing_frac_min": 0.05, "firing_frac_max": 0.45` to `baseline.execution_workflow` so the
   fields are documented in the canonical templates. Leave the other 34 manifests untouched
   (they inherit the defaults).

## Test Requirements (TDD-tester writes FIRST; red before code)
- Schema: valid band parses; `firing_frac_min >= firing_frac_max`, negatives, and `> 1.0`
  each raise; **omitting both fields yields the 0.05 / 0.45 defaults** and an existing manifest
  (load a real `configs/batch_manifest_v2_*.json`) still validates unchanged.
- `batch_post_optimizer`: given a manifest dict WITH a custom band, the value read equals the
  manifest's; given one WITHOUT, it equals the `strategy_optimizer` module constants.
- Threading: `run_single_optimization` forwards `firing_frac_min/max` into `run_optimization`
  (assert via monkeypatch/mock capturing the kwargs — do not run a full optimization).
- Regression guard: full fast suite stays green except the known pre-existing ES01B sentinels
  (`pytest tests/ -m "not slow"` → the same 10 failures as before, nothing new).

## Validation (post-green)
- Load `configs/batch_manifest_v2_hourset14b_scout.json` through `BatchSweepConfig` and confirm
  it validates with the new example fields.
- Confirm a manifest without the fields still validates and yields `(0.05, 0.45)`.

## Out of scope
No shell/PowerShell changes; no changes to the other 34 manifests; the step-divisibility
polish (float `step=range/10` can drop Optuna's top step on some distributions) is a tiny
separate follow-up and NOT part of this ticket.
