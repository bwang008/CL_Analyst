# Ticket Resolution Blueprint — fleet-seed-preflight-gap_07052026_2215
**Ticket Directory:** `.agents/collab/tickets/fleet-seed-preflight-gap_07052026_2215/`

## Bug Summary
`FleetRunner.validate()` checks config existence, capacity, and client_ids — but none of
the per-symbol data prerequisites that `LiveTrader.__init__` hard-requires. On the
2026-07-05 4-model launch, NG and GC crash-looped through their full 5-restart budgets
(~5 min each) on a deterministic `FileNotFoundError` (missing `<SYM>_raw_1h.parquet`
seeds) and ended in "Manual intervention required" while CL/MES ran fine. The
prerequisite was documented in two workflows (build-symbol-pipeline Phase 1.7;
add-remove-fleet-model ADD step 2 + failure-signatures table) but the workflow's
verification step (gate c) was prose, not an executable command, and the runner never
enforced it. Auditor classification: HIGH severity, day-one integration gap (fleet_runner
born 07-04 in commit 5f3e4cd; the T2/T5/T7 seed requirements evolved in live_trader/
data_manager in parallel and validate() never learned them). NOT fast-tracked; full
Auditor → Impact-Reviewer chain ran; Reviewer APPROVED with conditions below.

Immediate remediation (already applied, NOT part of this blueprint): NG/GC 1h seeds
staged from `<SYM>_raw.parquet` and verified ≥4,320 in-window bars; macro CSVs verified.

## Target Files
- `src/live_execution/fleet_runner.py` (new method + main() wiring; `validate()` itself untouched)
- `src/live_execution/data_manager.py` (additive helper `required_live_data_artifacts()`)
- `tests/test_fleet_preflight.py` (NEW file — the existing `tests/test_fleet_runner.py` is
  Strict-Locked and must stay untouched and green)
- `.agents/workflows/add-remove-fleet-model.md` (gate (c): prose → executable; step-2
  staging prose STAYS — it is the remedy the new error messages point at)

## Required Changes
1. **NEW METHOD `FleetRunner.validate_data_prerequisites()`** — called by `main()` between
   `validate()` and `launch_all()` (BLOCKING design condition: do NOT put the preflight
   inside `validate()`; the Strict-Locked test file exercises `validate()` on minimal
   fixtures with no symbols/models and would break). Raises before any child spawns.
   Per ENABLED instance, with function-local imports (keep fleet_runner stdlib-only at
   module level; import `src.data_paths` first so `.env`/CL_DATA_ROOT/FRED_API_KEY resolve
   identically to children):
   a. `cfg = load_strategy_config(...)`; `ctx = resolve_instrument_context(cfg)`;
      `paths = derive_data_paths(ctx.brain_symbol)` — CALL the authorities, never
      re-implement path strings.
   b. `bar_size = cfg.get("bar_size", "5m").lower()` — MUST reproduce this default
      (reviewer Nuance A).
   c. Hourly (1h/2h/4h): REQUIRE `cache_1h` exists OR `seed_1h` exists, honoring the
      `live_config.seed_path_1h` override (relative resolved against `get_data_root()`).
   d. 5m: REQUIRE `seed_5m` OR `cache_5m` (shallow bootstrap is False for 5m models).
   e. Hourly configs need NO 5m artifacts; `enable_5m_stream=false` with non-hourly
      bar_size is a config error (mirror live_trader:350).
   f. `--seed-path`/`--cache-path` in the instance's `extra_args` (reviewer Nuance B):
      honor them OR loud-raise on their presence — never silently check the wrong path.
   g. Macro models (feature_names read by loading the model pkls the same way the child
      does — this also verifies model-file existence): `validate_external_macro_features`
      must pass; REQUIRE `FRED_API_KEY` set; REQUIRE `fred_macro_data_<sym>.csv` AND
      `cftc_cot_<sym>.csv` exist. (Two DELIBERATE strictifications vs runtime, accepted
      by the reviewer and confirmed by the user — fail-closed for live money.)
   h. Error messages must be operator-actionable: instance name, exact missing path, and
      the staging command (`Copy-Item <SYM>_raw.parquet <SYM>_raw_1h.parquet` /
      `python scripts/download_macro_data.py --symbol <SYM>`).
   i. No learner retention (memory released after check); per-instance local scope.
2. **Additive helper** `required_live_data_artifacts(strategy_config)` next to
   `derive_data_paths` in data_manager.py, consumed by the preflight. SCOPE CONDITION:
   this ticket wires it into the preflight ONLY — refactoring live_trader.__init__ to
   consume it is a separate follow-up ticket (zero-touch on the live path now).
3. **Tests first** (new file `tests/test_fleet_preflight.py`, DI'd popen/sleep style):
   missing 1h seed+cache raises pre-popen; seed_path_1h override honored (absolute +
   relative); 5m config missing both raises; hourly config without 5m artifacts passes;
   enable_5m_stream=false + 5m bar_size raises; macro model missing CSV or key raises;
   extra_args --seed-path handled; fully-staged tmp fixture passes; locked
   test_fleet_runner.py stays green untouched.
4. **Workflow doc**: `.agents/workflows/add-remove-fleet-model.md` gate (c) becomes an
   executable, blocking command (interim Test-Path form now; switches to invoking
   `validate_data_prerequisites()` once landed). Gate (b) one-liner gains the
   preflight call.
5. **Behavior disclosures (user-confirmed before implementation):**
   - ALL-OR-NOTHING: one unstaged symbol blocks the entire fleet launch (park it with
     `enabled:false` to launch the rest) — consistent with validate()'s existing
     philosophy.
   - Systemd/service restarts re-run the preflight: a post-launch data regression turns
     the next restart into a full-fleet block (intended fail-fast).
   - Residual gap (out of scope, disclosed): existence-not-depth — a present-but-shallow
     seed still fails post-spawn at REQUIRED_1H_BARS; optional pyarrow row-count
     enhancement deferred.
6. **Follow-up ticket (separate, not this change):** exit-code contract
   (cli.py EX_CONFIG=78 → runner treats as non-restartable) so deterministic config/data
   failures can't burn restart budgets even if they slip past preflight.

## USER-DIRECTED AMENDMENTS (2026-07-05, verified with user before implementation)
A. All-or-nothing launch semantics CONFIRMED, with the explicit bypass being
   `"enabled": false` in the manifest (error message must say so). ✓ implemented.
B. Cache-corruption + first-start scenarios MUST NOT false-positive:
   - cache absent + seed present = PASS (first start / post-deletion rebuild);
   - cache present but UNREADABLE = FAIL with an actionable "delete the corrupted
     cache and relaunch (seed rebuilds it)" message — matches the operator's
     established remediation for corrupt warm-start caches;
   - the check's purpose is exactly what the user stated: guarantee a correct seed
     exists so the warm-start cache CAN be (re)built. ✓ implemented.
C. STALENESS GATE (new requirement): IBKR backfill is a single NOW-anchored
   "{gap_days} D" request — gaps beyond its practical horizon are unfillable and
   can stitch a silent hole into the series. If the freshest bar across
   (readable cache, readable seed) is older than the horizon
   (MAX_BACKFILL_GAP_DAYS_1H=60 per user-observed ~2-month IBKR limit;
   MAX_BACKFILL_GAP_DAYS_5M=30), FAIL with a "refresh via Databento (/grab-data)
   and re-stage the seed" remedy. ✓ implemented.

## IMPLEMENTATION RESULT (same session)
- `data_manager.py`: `required_live_data_artifacts()` + MAX_BACKFILL_GAP_DAYS_* constants.
- `fleet_runner.py`: `validate_data_prerequisites()` (+ `_load_strategy`/`_newest_bar_ts`/
  `_check_requirement`), wired in main() between validate() and launch_all().
- `tests/test_fleet_preflight.py`: 17 tests (new file; Strict-Locked
  test_fleet_runner.py untouched, 21/21 green).
- Real-manifest preflight run against the live 4-model fleet: PASS.
- Incidental (pin rule): shipped ES01B config predictions_path repointed to the
  renamed PRODUCTION_ batch folder + `_ES01B_PRED` pin evolved in the same change
  (fixed the pre-existing test_instrument_context/test_config_generator failures
  the reconnect-fixes workstream had flagged).
- Full suite: 1456 passed / 0 failed.
