# Ticket Resolution Blueprint — t6-config-generator-fix_07052026_0043
**Ticket Directory:** `.agents/collab/tickets/t6-config-generator-fix_07052026_0043/`

## Bug Summary
The ensemble config generator (a) derives model registry prefixes from the data
basename while vm_e2e_pipeline strips the `{symbol}_` prefix (regression-by-divergence
since a239197/7ce89b8 2026-06-29 — bites CL too: v2 CL batches emit model_paths at
NONEXISTENT `E2E_CL_*` dirs) and (b) deep-copies the CL base config so
`execution_symbol:"CL"` leaks into non-CL configs (day-one latent gap; produced the
broken shipped `ES01B_Sharpe_E03_07042026.json`). Reviewer verdict: APPROVE
(conditions C1-C4; no human authorization). Full design: `audit.md` (§2 the 10-field
ES01B patch table, §5 design, §6 14-test list); verification: `impact_review.md`.
This document governs on conflict.

## Manager rulings (given)
- D1: corrected v2-CL output ACCEPTED (byte-pinning dead paths = pinning a bug);
  legacy `bk_`-input CL outputs stay byte-identical.
- D2: unconditional `models.<side>.symbol` emission ACCEPTED (+1 key on CL configs;
  the only surviving cross-check post-stripping).
- Spin-offs confirmed (not in this diff): m2 `cl_*` rename; warmup `entry_crossed`
  fix; C1 residual (strategy_optimizer `_opt_/_hybrid_` writes into configs/strategies
  from the raw CL base — route to T8's generated-artifact validation gate).
- vm_e2e `ensemble_cfg` one-liner: IN.
- Wrappers (build_cl_contract/build_mcl_contract): KEEP (live caller
  scripts/download_ibkr_history.py; delegate to registry builder; deletion churns two
  Strict-Lock files for zero gain). live_config seed_path_5m/cache_path keys: WON'T-DO.

## Target Files
- NEW `src/core/dataset_tag.py` — `derive_dataset_tag(basename, symbol)`: byte-for-byte
  the vm_e2e_pipeline.py:655-661 logic (268-case differential verified). Stdlib leaf.
- `agent/generate_ensemble_artifacts.py` — consume the helper with
  `manifest.baseline.symbol` (RAISE when missing; `get_instrument(symbol)` fail-fast);
  set `execution_symbol` and `models.<side>.symbol` from baseline.symbol on every
  emitted strategy config; post-emission self-check: RAISE if the emitted config fails
  `resolve_instrument_context` (model_path existence = WARN only).
- `gcp/vm_e2e_pipeline.py` — both duplicated tag blocks (:652-661, :733-740) call the
  helper (identity-pin test required: both import the SAME function); the ensemble_cfg
  emission one-liner (execution_symbol/models.symbol from run symbol).
- `configs/strategies/ES01B_Sharpe_E03_07042026.json` — surgical patch per audit §2
  10-field table: execution_symbol ES; models.long/short.symbol ES; model_paths →
  `reports/sweep_es01b_2x1_6h_scout_20260704-0701/registry/production_output/registry/
  E2E_HourSet_01B_{long,short}_logloss/final_model.pkl`; predictions_path →
  `reports/batch_runs/batch_20260704_0701_ES_01B_SCOUT/...` (verified on disk;
  round-trip resolves ES/ES/CME — re-executed by reviewer).
- `tests/test_instrument_context.py` — THE ONE INTENDED STRICT-LOCK EVOLUTION (exactly
  two pins, both self-documented "until T6"): :273 intended-failure test → asserts the
  patched config RESOLVES as ES (execution ES, brain ES, exchange CME);
  :305 fleet glob → empty intended_failures list.
- Small CL-identical cosmetics (parity-gated): per-symbol backup filenames in
  `_backup_cache_to_repo`; smoke_test_pipeline cadence regex for per-symbol cache
  names; m1 display division; narrow m3 string sweep (audit-enumerated only).
- Ticket bookkeeping: C1 census amendment + C2 flag (generator :272 silently defaults
  strategy_config to the CL base when the manifest lacks a `defaults` block —
  pre-existing, symbol-benign post-T6, flag in ticket_status for T8) + C3 batch name
  cited as `batch_20260630_2232_SCOUT_14B_FAIL`.

## Hard Constraints
1. CL generator outputs: byte-identical for legacy `bk_` inputs; for v2 `{sym}_` inputs
   the ONLY changes are corrected model_path prefixes (D1) + the new models.*.symbol
   key (D2). Fixture round-trip pins both.
2. No silent defaults in NEW code paths (baseline.symbol required; resolver self-check
   raises). The pre-existing :272 default is flagged, not fixed here (C2).
3. Scope guards: NO training pipeline logic beyond the tag/emission alignment; NO
   batch_post_optimizer changes (verified: emits no strategy configs); NO live engine
   changes; NO fleet_runner; spin-offs stay out.

## Test requirements (audit §6 14-item list; highlights)
- Tag helper: 268-case-style differential vs a frozen transcription of the vm_e2e
  logic; identity-pin that generator + vm_e2e import the SAME function.
- Generator fixtures: CL bk_ manifest → byte-identical output (except D2 key);
  CL v2 manifest → corrected prefixes; ES manifest → execution_symbol ES,
  models.*.symbol ES, resolver round-trip passes; baseline.symbol missing → raises;
  emitted-config-fails-resolver → raises (tamper fixture).
- ES01B: patched file resolves ES/ES/CME; all referenced paths exist; SHA-drift guard
  on the two flipped pins only.
- Cosmetics: CL-identical pins where touched.

## Verification
- Full fast suite green (baseline 1284 + new; exactly the two T1 pin flips).
- C4 (BLOCKING): HS14B ledger parity gate (`setup --disable-trailing`, 2200/336) →
  PARITY: PASS before commit.
