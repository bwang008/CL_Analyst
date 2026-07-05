# Impact Review — t4-macro-vol-parameterization_07042026_2147

**Reviewer:** Ticket-Impact-Reviewer | **Date:** 2026-07-04 | **HEAD reviewed:** `55a6a8f` (verified; src/tests/scripts clean at review time)
**Proposal reviewed:** `audit.md` (Ticket-Auditor, same ticket) | **Manager rulings honored:** Q1 ACKed (hard-raise, blast radius verified below), Q2 accepted as scoped (constructor CL shim documented, live boundary enforced), Q3 deferred to T7.

## VERDICT: **APPROVE** (with 3 non-blocking documentation conditions + the blocking parity-gate condition the audit already carries)

---

## 1. Independent verification results

### 1.1 Site map (claim: 8 CL-hardcoded sites; startup-only index fetch) — VERIFIED
Greps for `MacroFeatureEngine(`, `fetch_daily_close`, `_needs_macro`, `_macro_daily_closes`, `MACRO_` across `src/` at HEAD reproduce the audit's map exactly:
- `live_trader.py:217-221` prefix-list `_needs_macro`; `:637-645` literal `[("VIX","VIX"),("OVX","OVX")]` startup fetch; `:696/:700/:3749/:3755` bare engines; `:2018/:2861` instrument-less `build_live_features`; `feature_pipeline.py:250-258` duplicate prefix list + bare engine; `macro_features.py:120-131` CL-default constructor; `:453-468` file-sniffing vol label + dead comment block (confirmed verbatim); `ibkr_data_feed.py:103-107/:148-152/:182-197` VIX/OVX-only branches with `:125/:169` qualification exemptions.
- **Startup-only claim TRUE:** the only `fetch_daily_close` caller in live_trader is `:641`. The hourly path (`_log_heartbeat` `:3743-3802`) calls only `refresh_if_stale()` + trial `_build_fred_features` and reuses cached `_macro_daily_closes` — no index re-fetch exists at HEAD. D5's documentation of this is accurate.
- **Dead-code claim TRUE:** `subscribe_live_bars` in live_trader is called only at `:2141/:2152` (brain futures) and `:2168` (front-month execution future) — no index symbol ever reaches the feed's index branches.
- **No missed macro-relevant site.** Extra grep hits, all correctly out of scope: `macro_features.py:17` (module docstring, not code); `feature_buckets.py:63` (`"macro_tech": ["MACRO_"]` — training-side bucket definition consumed only by `alpha_factory.py`, not a live fetch/naming site); `data_processor.py:2616/:2974` (`MACRO_POS_` drop lists, training-side, untouched); `feature_pipeline.py:203` `_has_internal_macro` — which already excludes-by-construction the same `MACRO_POS_/MACRO_WIDTH_` prefixes, independently corroborating D1.

### 1.2 D1 exclusion list vs parquet ground truth — VERIFIED (bitwise)
Read via pyarrow from `data/processed/`:
- `CL_HourSet_14B.parquet`: **39 external** MACRO + 5 internal (`MACRO_WIDTH_{1W,2W,1M,3M,6M}`) + **13 COT**.
- `ES_HourSet_01B.parquet`: **29 external** (VIX only — zero OVX/ratio) + 5 `MACRO_WIDTH_*` + 13 COT (identical names).
- `GC_HourSet_01A.parquet`: **39 external** (incl. 9 `MACRO_GVZ_*` + `MACRO_VIX_GVZ_RATIO`) + 10 internal (`WIDTH`+`POS` ×5) + 13 COT (identical names).
- Extensional-equality check (old 6-prefix rule vs proposed external-shaped rule) over each file's MACRO∪COT columns: **IDENTICAL on CL and ES; on GC the new rule adds exactly the 9 `MACRO_GVZ_*` names** — precisely the intended M2 fix, nothing else. The blueprint's naive `startswith(("MACRO_","COT_"))` would have misclassified the WIDTH/POS internals in all three files; D1's correction is necessary and sufficient.

### 1.3 CL byte-identity — VERIFIED with model-artifact evidence
- Loaded both HS14B parity boosters (`reports/sweep_hs14b_2x1_6h_canary_20260624_2007/registry/canary_output/registry/E2E_CL_HourSet_14B_{long_logloss,short_average_precision}/final_model.pkl`, 233 features each, conda `trader` env): old-rule and new-rule `_needs_macro` both **True** (equal); every external macro/COT feature name is a member of the CL-buildable set (`{VIX,OVX,DXY,YIELD_CURVE}×(raw+5 CHG+3 PCTILE) + RATIO + SIGN + FED_FUNDS + 13 COT = 52`) → the new `__init__` validation **cannot raise for the shipped parity ensemble**.
- Fetch order: D2's `["VIX"] + [vol if != VIX]` with CL registry `live_vol_index="OVX"` yields `["VIX","OVX"]` — today's literal order; alias==symbol preserved.
- Same files: constructor path resolution is `get_data_path(f"raw/macro/fred_macro_data_{sym.lower()}.csv")` for both `instrument=None→get_instrument("CL")` and explicit `instrument=CL` — byte-identical paths (`_cl.csv` pair). `_load_fred/_load_cot` already `FileNotFoundError` naming the per-symbol path (`macro_features.py:247-251/:263-267`) — no-silent-default constraint holds.
- D4 (vol label OVX via registry instead of sniffing): for `fred_macro_data_cl.csv` (header `Date,VIX,DXY,YIELD_CURVE,FED_FUNDS,OVX`) sniffing returns `OVX`; `vol_label_for(CL)` = `"OVXCLS".replace("CLS","")` = `OVX` — identical.

### 1.4 Q1 blast radius (shared-engine hard-raise) — VERIFIED SAFE, one audit omission found (non-fatal)
- Callers of the `_build_fred_features` FRED-column path (repo-wide grep, `merge_all(` + direct): `data_processor.py` ×11 (10 bare-CL sites `:1701-:3072` + parameterized `:3199-3200`), `feature_pipeline.py:258`, `live_trader.py:700/:3755`, and 2 test files with fixture-injected frames. No other production caller exists (no engine use in `scripts/`, generator, or batch tooling).
- On-disk headers (`C:\CL_Analyst_Data\data\raw\macro\`): `_cl`→has OVX ✓, `_gc`→has GVZ ✓, **`_ng`→has OVX ✓** (registry NG `volatility_index="OVXCLS"` "Energy proxy", `live_vol_index="OVX"`), `_es/_nq/_si/_zc/_zs`→base-4 incl. VIX ✓. **Every per-symbol file contains its instrument's vol column → the Q1 raise fires for zero current files.** The 10 bare-CL training sites resolve `_cl.csv` (has OVX) ✓. Registry invariant `live_vol_index == volatility_index.replace("CLS","")` holds for **all 15 entries** (line-verified in `instrument_master.py`).
- **Audit omission (correctness of the doc, not the design):** audit §2 lists FRED files as "`_es/_zc/_zs/_si` = base 4 only" and its registry recap omits **NG→OVX**. `fred_macro_data_ng.csv` carries OVX and NG's registry entry is consistent, so nothing breaks — but the §2 enumeration and the §5 test-16 narrative should mention NG (its live fetch list will be `["VIX","OVX"]`, already covered by `_INDEX_CONTRACT_SPECS`).
- Legacy `fred_macro_data.csv` (no suffix, base-4) exists on disk but is unreferenced by the engine (per-symbol template always) — inert.

### 1.5 Validation placement (__init__-before-connect) — VERIFIED, one wording nit
- **cli path:** `resolve_instrument_context` at `cli.py:229`; `LiveTrader(...)` constructed at `cli.py:313` (validation hook fires here, after `live_trader.py:278` context resolution); `trader.start()` at `cli.py:329`; `data_client.connect()` only inside `start()` at `live_trader.py:627`. Raise precedes any connection. **Nit:** the audit's phrase "before any IBKR object" is loose — `DataFeedFactory.create` at `cli.py:310` constructs the adapter *object* before `__init__`; the true (and sufficient) property is **before connect/any network side-effect**.
- **livetest/parity path:** `scripts/livetest_engine.py:711` constructs `LiveTrader` with simulated adapters and **never calls `start()`** (`_bootstrap_trader` `:173-261` replicates warm-start only). The parity config (`reports/_ledger_parity/parity_config.json`) is `execution_symbol: "CL"` + the two HS14B models verified in §1.3 → context resolves, validation passes, harness constructs. The startup index fetch is not exercised in livetest at all; `SimulatedDataFeed` confirmed to have no `fetch_daily_close` (and `:640-643`'s per-symbol try/except would swallow it in a hypothetical sim `start()`), unchanged by the proposal. **Parity gate stays runnable.**
- `_brain_instrument` seam: pattern-matches the existing `_brain_symbol`/`_tick_size` properties at `:2097-2131` (context-first, structural registry fallback that raises on unknown) — consistent with the T3 convention, no silent default.

### 1.6 Mechanical churn census — VERIFIED COMPLETE (audit's list is exact)
Greps of `build_live_features(` and `MacroFeatureEngine(` over `tests/` + `scripts/`:
- **Needs churn (all in audit §3.6/§8):** `tests/test_fred_live_override_index.py:54-60` and `tests/test_macro_pctile_fast_rank.py:136-142` — both inject `MagicMock()` instruments with only `.symbol` set; under `vol_label_for`, `MagicMock.volatility_index.replace(...)` returns a MagicMock and breaks column matching (with Q1, raises), so the one-line `volatility_index="OVXCLS"` fixture addition is **necessary**, not just cosmetic. `tests/smoke_test_pipeline.py:400` (feature list from a live model incl. `MACRO_VIX/MACRO_DXY` targets at `:164`), `scripts/feature_parity_compare.py:203`, `scripts/feature_parity_multi_ts.py:56,169` (both scripts pull `feature_names` from model pkls whose lists include external MACRO/COT — verified §1.3) — all need `instrument=get_instrument("CL")`.
- **Correctly zero-churn:** `test_stale_data_detection.py` (14 bare engines — call only `_check_value_staleness`/`_check_feature_staleness`, both byte-untouched); `test_live_macro_refresh.py` (`:53` engine uses only `refresh_if_stale`; its `LiveTrader` fixtures are `execution_symbol: "CL"` + `MACRO_VIX` — CL-buildable, new validation passes); `test_live_features.py`/`test_nan_guard.py` (only internal `MACRO_WIDTH_/POS_` names → `_has_external_macro` stays False under both rules); `test_bad_data.py`, `test_feature_parity.py`, `test_pipeline_parity_hourly.py` (no macro names). No file outside the audit's list requires changes.

## 2. Constraint evaluation

- **Interface Rule — triggered, justification accepted.** `build_live_features` gains a keyword-only `instrument` parameter (additive; non-macro callers unaffected — proven by §1.6) and the live engine-construction contract changes. Justification is strong and specific: the user's recorded no-silent-null-defaults rule forbids the localized alternative (defaulting the new parameter to CL), all five affected external callers are enumerated with exact fixes, and CL byte-identity is proven at the model-artifact level.
- **Base Class Rule — triggered (MacroFeatureEngine is shared with training), justification accepted.** The 11 `data_processor` call sites are provably output-identical: 10 bare sites keep the documented CL shim (Q2, Manager-accepted); the parameterized `:3199` site is safe for every symbol because every per-symbol FRED file on disk contains its instrument's vol column (§1.4) — the Q1 hard-raise (Manager-ACKed) can only fire on future misprovisioning, which is its purpose.
- **Refactor Veto — NOT triggered.** Four src files are modified but none is rewritten: the change is parameter threading + additive leaf helpers + a literal-to-map substitution, mirroring the already-executed T1/T2/T3 pattern on this branch. Mute semantics, staleness thresholds, `ibkr_client`, `cli`, adapters-sim, and all training behavior are untouched. This is enabler work under the human-authorized multi-symbol blueprint with explicit Manager rulings on the open questions — no additional human authorization is required.

## 3. Conditions attached to approval

1. **BLOCKING (already in the audit, held):** HS14B ledger parity gate re-run must report PARITY: PASS before merge (T1-T3 convention; the diff sits directly on the parity path).
2. **Doc correction (non-blocking):** add NG to audit §2's FRED-file/registry enumeration (`fred_macro_data_ng.csv` has OVX; registry NG→OVXCLS/OVX) so test 16's invariant narrative and the per-symbol file table are complete for all 8 per-symbol file sets.
3. **Wording (non-blocking):** state the validation guarantee as "raises before `connect()` / any network side-effect", not "before any IBKR object" (cli constructs adapter objects at `cli.py:310-311` before `LiveTrader.__init__`).
4. **Cosmetic (non-blocking):** the `validate_external_macro_features` raise message lists `MACRO_VIX*, MACRO_{vol_label}*` — for VIX-proxy instruments these duplicate; dedupe in the message the same way the build loop dedupes (`dict.fromkeys`).

## 4. Summary for the Manager

All six riskiest claims verified independently at HEAD `55a6a8f`, several with hard artifact evidence (pyarrow parquet schemas, lightgbm booster feature lists, on-disk CSV headers, registry line map). One documentation omission found (NG's OVX file/registry entry — consistent, nothing breaks). The churn census is exact; the parity harness provably still constructs; CL behavior is byte-identical by extensional equality on real artifacts, not just reasoning. **APPROVE** with the blocking parity re-run and three documentation-level conditions above.
