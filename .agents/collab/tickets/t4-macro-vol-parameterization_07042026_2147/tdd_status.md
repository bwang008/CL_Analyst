# TDD Status — t4-macro-vol-parameterization_07042026_2147

## PHASE: Green
**Actor:** TDD-Coder | **Date:** 2026-07-04 22:56 | **HEAD:** `55a6a8f` (branch `development`, no worktree, uncommitted — Manager commits after the parity gate)

### Implementation delivered (audit §3.1-§3.4, Manager rulings honored)
1. `src/features/macro_features.py` — new module helpers `vol_label_for`,
   `is_external_macro_feature`, `has_external_macro_features` (instrument-
   independent, single-arg per audit §3.1), `external_macro_feature_names`,
   `validate_external_macro_features` (raise message per audit §4 wording,
   vol-label stems deduped via dict.fromkeys — reviewer condition 4);
   `_build_fred_features` vol label now instrument-driven (D4 — dead
   comment/`pass` block and file-sniffing deleted; column loop deduped via
   dict.fromkeys so VIX-proxy frames emit no skip-warning); Q1 hard-raise
   when the FRED file lacks the instrument's vol column (names column,
   symbol, volatility_index + `--symbol <SYM> --fred-only` regenerate hint);
   constructor `instrument=None -> CL` kept as the DOCUMENTED training-only
   shim (Q2, class docstring); `_load_fred`/`_load_cot` FileNotFoundError
   hints now include `--symbol {sym}` (paths were already symbol-aware).
2. `src/live_execution/feature_pipeline.py` — `build_live_features` gains
   keyword-only `instrument=None`; external classification via
   `has_external_macro_features` (D1); external-needed + instrument None ->
   ValueError raised BEFORE `MacroFeatureEngine` construction (message names
   the first offending feature); engine constructed with
   `instrument=instrument` when provided; internal-only/non-macro callers
   byte-compatible.
3. `src/live_execution/live_trader.py` — `_needs_macro` via the helper
   (computed pre-context, line ~217); `validate_external_macro_features(
   feature_names, ctx.brain_instrument)` in `__init__` immediately after
   context resolution, gated on `_needs_macro`, BEFORE connect()/any network
   side-effect (D3, reviewer condition 3); new `_brain_instrument` property
   beside `_brain_symbol` (context-first, structural fallback via
   `_brain_symbol`/`get_instrument`, unknown raises ValueError, missing seam
   raises AttributeError naming `_execution_symbol`); startup fetch list =
   ordered `["VIX"] + ([vol] if vol != "VIX")` keyed by label (CL byte-order
   `["VIX","OVX"]` — D2; keys match `_build_fred_features` live_overrides
   column labels); the 4 engine sites (start Step 7 ×2, hourly heartbeat ×2)
   pass `instrument=self._brain_instrument`; the 2 `build_live_features`
   sites pass `instrument=self._brain_instrument if self._needs_macro else
   None` (instrument seam only resolved when the feature list needs it —
   keeps object.__new__ non-macro stubs working); mute blocks byte-untouched.
4. `src/live_execution/adapters/ibkr_data_feed.py` — module-level
   `_INDEX_CONTRACT_SPECS = {VIX/OVX/GVZ: ("CBOE","USD"), DX: ("NYBOT","USD")}`
   (shape per the locked test); `fetch_daily_close_async` routes through it,
   unknown symbol raises listing `sorted(_INDEX_CONTRACT_SPECS)`;
   `subscribe_live_bars[_async]` index branches + qualification-exemption
   sets consume the same map (dormant DX branch kept, `what_to_show="TRADES"`
   forced only for the CBOE vol indices exactly as before).
5. Mechanical churn (census-exact): `tests/test_fred_live_override_index.py`
   + `tests/test_macro_pctile_fast_rank.py` MagicMock fixtures gain
   `instrument.volatility_index = "OVXCLS"`; `tests/smoke_test_pipeline.py`,
   `scripts/feature_parity_compare.py`, `scripts/feature_parity_multi_ts.py`
   (×2 call sites) pass `instrument=get_instrument("CL")`.

### Deviations from the spawn prompt (none from blueprint/audit)
- `has_external_macro_features(feature_names)` is single-arg (audit §3.1 +
  locked test signature), not the two-arg form the spawn prompt sketched.
- `_INDEX_CONTRACT_SPECS` values are `(exchange, currency)` 2-tuples per the
  locked test's map-content pins, not the 1-tuples in the spawn prompt.

### Green proof
```
conda run -n trader python -m pytest tests/test_macro_vol_parameterization.py -v --tb=short
======================== 83 passed, 1 warning in 7.66s ========================

conda run -n trader python -m pytest tests/test_stale_data_detection.py tests/test_live_macro_refresh.py \
  tests/test_instrument_context.py tests/test_tick_order_pricing.py tests/test_modify_order_transmit.py \
  tests/test_fred_live_override_index.py tests/test_macro_pctile_fast_rank.py -q
======================= 175 passed, 1 warning in 4.54s ========================

conda run -n trader python -m pytest tests/ -q --tb=short -m "not slow"
=============== 1212 passed, 259 warnings in 163.65s (0:02:43) ================
```
1212 = baseline 1129 + 83 new; zero failures. Locked test file untouched.

### Remaining before commit (Manager-owned)
- BLOCKING: HS14B ledger parity gate (`setup --disable-trailing`, 2200/336)
  -> PARITY: PASS.

---

## PHASE: Red (superseded)
**Actor:** TDD-Tester | **Date:** 2026-07-04 22:34 | **HEAD:** `55a6a8f` (branch `development`, no worktree)

## Deliverable
`tests/test_macro_vol_parameterization.py` — NEW, FINALIZED, Strict-Lock TRUE
(implementation agents may not modify it). 46 test functions (83 parametrized
instances) in 7 classes. No existing
test file was touched (Coder owns the enumerated mechanical churn: the 2
MagicMock fixtures in test_fred_live_override_index.py /
test_macro_pctile_fast_rank.py, smoke_test_pipeline.py, and the 2 parity
scripts).

## Ghost imports (the Red trigger — Coder resolves, per audit §3.1/§3.4)
- `src.features.macro_features`: `vol_label_for`, `has_external_macro_features`
  (instrument-independent signature per audit §3.1 — computed before context
  resolution in LiveTrader.__init__), `external_macro_feature_names`,
  `validate_external_macro_features`
- `src.live_execution.adapters.ibkr_data_feed`: `_INDEX_CONTRACT_SPECS`

## Test census by class
- **TestVolLabelAndNaming** (10): vol_label_for registry table (15 symbols,
  CL/MCL/NG→OVX, GC/MGC→GVZ, rest→VIX) + lockstep with live_vol_index;
  external_macro_feature_names exact ground truth (CL 39+13=52, ES 29+13=42,
  GC 39+13=52 incl. 9 MACRO_GVZ_* + MACRO_VIX_GVZ_RATIO; no OVX in ES set);
  MCL set == CL set; internal MACRO_WIDTH_*/MACRO_POS_* never external (D1);
  enumerator lockstep vs actually-built FRED+COT columns (CL/ES/GC synthetic
  frames); CL value pins bit-equal to independent pandas reference (D4
  output-identity); ES frame VIX-only, no dup columns, no skip-warning.
- **TestNeedsMacroClassification** (2, 11 parametrized cases):
  has_external_macro_features truth table (item 1) + extensional identity
  with the legacy 6-prefix rule over CL/ES-shaped lists (hard constraint 1).
- **TestValidateExternalMacro** (7): ES+MACRO_OVX_CHG_1D actionable
  ValueError (feature + instrument + buildable stems); MACRO_VIX_GVZ_RATIO on
  ES raises (exact-name enumeration, item 10); GC GVZ set no-raise; ZC/ZS/SI
  raise message dedupes vol stems (reviewer condition 4: `MACRO_VIX*` appears
  exactly once, no duplicated stems); shipped-style 233-name CL list cannot
  raise (item 5); MCL accepts CL macro names.
- **TestEngineInstrumentFiles** (7): ES/GC engines resolve _es/_gc CSVs
  (monkeypatched get_data_path — no real data root); instrument=None legacy
  shim == instrument=CL byte-identical _cl.csv paths (Q2); Q1 hard-raise
  (GC engine on ES-shaped file → ValueError naming GVZ/GC/regenerate hint);
  missing FRED/COT file symbol-aware FileNotFoundError (item 17); ES engine
  stale-DXY still raises StaleDataException (item 19 engine half).
- **TestBuildLiveFeaturesInstrument** (4): external-needed + instrument=None
  → ValueError BEFORE engine construction (MockEngine.assert_not_called);
  internal-only + None backward compatible; non-macro + None works
  (parity-script pin); instrument passed → MacroFeatureEngine constructed
  with it and merge_all invoked (mock seam).
- **TestLiveTraderMacroWiring** (12, 5 parametrized fetch cases): startup
  fetch order via the start() interception seam (_print_account_summary
  sentinel — narrowest seam the current structure allows; fetch is inline in
  start() Step 3): CL ["VIX","OVX"] byte-order (D2), MCL brain-driven
  ["VIX","OVX"] (item 20), ES ["VIX"] + never-OVX (item 7), GC ["VIX","GVZ"]
  (item 12), ZC ["VIX"]; _macro_daily_closes keys/values pinned; no-macro and
  internal-only configs never fetch; GC _needs_macro True for MACRO_GVZ_*
  (M2); ES config + MACRO_OVX_CHG_1D → ValueError at __init__ with
  connect() never called (D3, reviewer condition 3); HS14B-style CL config
  constructs; _brain_instrument seam (context path incl. full-init ZC,
  structural fallback MCL→CL / GC→GC, unknown raises ValueError, nothing-set
  raises AttributeError naming _execution_symbol).
- **TestIndexContractSpecs** (4, 4 parametrized routing cases):
  fetch_daily_close_async routing VIX/OVX/GVZ→(CBOE,USD), DX→(NYBOT,USD)
  via mocked manager + patched ib_insync.Index (asyncio.run, AsyncMock);
  unknown symbol raises listing the supported set; map content pinned;
  registry invariant live_vol_index == volatility_index.replace("CLS","") ∧
  live_vol_index ∈ _INDEX_CONTRACT_SPECS for all 15 entries incl. NG
  (item 16 + impact_review condition 2).

## Not unit-tested here (by design)
- Item 6: BLOCKING HS14B ledger parity gate (`setup --disable-trailing`,
  2200/336 → PARITY: PASS) — harness run before commit, Manager-owned.
- Item 19 LiveTrader mute activation/clear blocks — byte-untouched per audit
  §3.3; already pinned by tests/test_live_macro_refresh.py +
  tests/test_stale_data_detection.py (verified green below).

## Red proof (missing-implementation failure only)
```
conda run -n trader python -m pytest tests/test_macro_vol_parameterization.py -v --tb=short --continue-on-collection-errors
tests\test_macro_vol_parameterization.py:130: in <module>
    from src.features.macro_features import (  # noqa: E402
E   ImportError: cannot import name 'external_macro_feature_names' from 'src.features.macro_features' (C:\Users\bwang\Documents\GitHub\CL_Analyst_Development\src\features\macro_features.py)
ERROR tests/test_macro_vol_parameterization.py
========================= 1 warning, 1 error in 2.06s =========================
```

## Neighbors green (pre-existing behavior untouched)
```
conda run -n trader python -m pytest tests/test_stale_data_detection.py tests/test_live_macro_refresh.py tests/test_instrument_context.py -q
======================== 54 passed, 1 warning in 1.93s ========================
```

## Seam validation (de-risking Green)
All mock-seam mechanics were validated against HEAD via a scratchpad harness
(outside the repo): CL start()-seam fetch order, no-macro no-fetch, GC/ZC
construct+start, CL engine value pins + 39 cols, feed async seam VIX routing
+ unknown-raise, build_live_features backward-compat + mocked-engine seam,
ES stale-DXY, CL/GC lockstep 39/13 column counts — 9/9 PASS. Every Red
failure the Coder will see is therefore implementation-driven, not test
plumbing.

## Handoff to Coder
- Implement audit §3.1-§3.4 exactly (Manager rulings: Q1 hard-raise ACKed,
  Q2 constructor None→CL documented training-only shim, Q3 deferred to T7).
- Mechanical churn owned by Coder (audit §3.6): 2 MagicMock fixtures gain
  `volatility_index = "OVXCLS"`; smoke tool + 2 parity scripts pass
  `instrument=get_instrument("CL")`.
- Verification: full fast suite green (baseline 1129 + these 83), then the
  BLOCKING HS14B ledger parity gate before commit.
