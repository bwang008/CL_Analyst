# Audit — t4-macro-vol-parameterization_07042026_2147

**Auditor:** Ticket-Auditor | **Date:** 2026-07-04 | **HEAD audited:** `55a6a8f` (branch `development`; T1/T2/T3 + modify_order fix merged)
**Scope source:** T4 section + Gap Table rows B6/M2/M3 of `.agents/collab/tickets/multi-symbol-live-gaps_07042026_1520/blueprint.md`; T1/T2/T3 tdd_result.md consulted.
**Severity: MEDIUM/HIGH** (multi-file structural change → Manager must route through Impact-Reviewer).
**Regression: NO** — this is gap/enabler work. The live macro path has been CL-shaped since inception (`02a553f`); nothing that previously worked is broken. No CL-affecting defect found at HEAD.

---

## 1. Live macro data flow at HEAD — verified line map (T2/T3 shifted the pre-T2 line numbers)

End-to-end flow: **startup index fetch → startup FRED/COT refresh + trial build (mute gate) → per-bar `build_live_features` → `merge_all` (mute gate) → hourly refresh (mute clear/activate)**.

| # | Site (current HEAD) | Behavior | CL-hardcoded? |
|---|---|---|---|
| 1 | `src/live_execution/live_trader.py:217-221` (`__init__`) | `_needs_macro = any(f.startswith(("MACRO_VIX","MACRO_OVX","MACRO_DXY","MACRO_YIELD_CURVE","MACRO_FED_FUNDS","COT_")))` (pre-T2 ~:217-223) | **YES** — VIX/OVX literal stems; GC's `MACRO_GVZ_*` not detected (M2) |
| 2 | `live_trader.py:637-645` (`start()` Step 3b) | Startup daily-close fetch loop over hardcoded `[("VIX","VIX"),("OVX","OVX")]` into `self._macro_daily_closes`; failures swallowed with `log.warning` (pre-T2 ~:613-621) | **YES** — fetch list literal |
| 3 | `live_trader.py:693-721` (`start()` Step 7) | `MacroFeatureEngine().refresh_if_stale()` (:696) + trial `MacroFeatureEngine()._build_fred_features(live_overrides=…)` (:700); `StaleDataException` → Safety Mute activation (:704-720) | **YES** — bare `MacroFeatureEngine()` → constructor default `get_instrument("CL")` → `fred_macro_data_cl.csv` + `cftc_cot_cl.csv` |
| 4 | `live_trader.py:2016-2025` (`_warmup_inference_state`) | `build_live_features(..., macro_overrides={})` — no instrument | **YES** (transitively, via #7) |
| 5 | `live_trader.py:2855-2892` (`_on_new_bar`) | overrides copy gated by `_needs_macro` (:2857); `build_live_features(...)` (:2861); `StaleDataException` → mute activation (:2868-2892) | **YES** (transitively, via #7) |
| 6 | `live_trader.py:3743-3802` (`_log_heartbeat`, hourly) | Rate-limited (3600 s) `MacroFeatureEngine().refresh_if_stale()` (:3749) + trial build (:3755) → mute clear/re-activate. NOTE: **no index daily-close re-fetch happens here at HEAD** — closes are fetched once at startup only (pre-T2 ~:3640-3699) | **YES** — bare engines ×2 |
| 7 | `src/live_execution/feature_pipeline.py:250-254, 258` (`build_live_features`) | `_has_external_macro` — same hardcoded prefix list as #1; then `MacroFeatureEngine().merge_all(work, live_overrides, live_time)` — bare engine, CL default | **YES** — both the list and the engine |
| 8 | `src/features/macro_features.py:120-131` (`MacroFeatureEngine.__init__`) | `instrument=None → get_instrument("CL")`; paths already per-symbol: `fred_macro_data_{sym}.csv` / `cftc_cot_{sym}.csv` via `get_data_path` | **Default is CL** — parameterization exists, live never uses it |
| 9 | `macro_features.py:140-236` (`refresh_if_stale`) | Downloads via `download_fred_data(api_key, instrument=self.instrument)` / `download_cot_data(self.instrument)` — fully instrument-parameterized already | No (inherits engine's instrument) |
| 10 | `macro_features.py:453-468` (`_build_fred_features`) | vol label chosen by **file-content sniffing**: `vol_cols = [c not in {VIX,DXY,YIELD_CURVE,FED_FUNDS}]; vol_label = vol_cols[0] if vol_cols else "OVX"`; lines 456-465 are a dead `pass` block of exploratory comments | **Soft** — works for all real files but fallback is `"OVX"` and selection is file-order-dependent, not instrument-driven |
| 11 | `macro_features.py:437-451` (live-override injection) | `if key in new_row` — only overwrites existing columns (stray "OVX" key on an ES frame is a silent no-op) | No (safe by construction) |
| 12 | `src/live_execution/adapters/ibkr_data_feed.py:182-197` (`fetch_daily_close[_async]`) | `if symbol in ("VIX","OVX"): Index(symbol,"CBOE","USD") else raise ValueError` | **YES** — no GVZ; this is the ONLY live index path (indices are never `subscribe_live_bars`-subscribed; live_trader:2141/2152/2168 subscribe only brain/front-month futures) |
| 13 | `ibkr_data_feed.py:103-107/148-152` (`subscribe_live_bars[_async]` index branches) + `:125/:169` qualification-exemption sets | VIX/OVX→CBOE, DX→NYBOT. **Dead code in the live path** (no caller passes an index symbol) — DX/DXY is FRED-only; no daily DX fetch exists and none is needed by `_needs_macro` features | YES but dormant |
| 14 | `src/live_execution/ibkr_client.py:883-914` (`fetch_daily_close[_async]`) | Contract-generic (takes qualified `Contract`) | **No — already generic; no change** |
| 15 | `src/live_execution/adapters/simulated_data_feed.py` | Has **no** `fetch_daily_close` — startup fetch's per-symbol try/except (:640-643) swallows the AttributeError in sim/parity runs | No change |
| 16 | Mute internals: `macro_features.py:52-64` (`_STALE_THRESHOLDS={"DXY":10}`, `_FEATURE_STALE_THRESHOLDS={"MACRO_DXY_CHG_1D":11}`) | Staleness monitors only DXY, which every per-symbol FRED file contains → **mute semantics are already symbol-independent**; they parameterize for free once the engine gets the right instrument | No change to semantics |

`cli.py` needs no T4 change: instrument context already resolved (:227-229) and passed to the data feed factory (:310); no macro logic lives there.

## 2. Training-side conventions the live path must consume (verified, not invent)

- **FRED** (`scripts/download_macro_data.py:87-98`): base series `VIXCLS→VIX, DTWEXBGS→DXY, T10Y2Y→YIELD_CURVE, FEDFUNDS→FED_FUNDS`; plus `instrument.volatility_index` when ≠ VIXCLS, labeled `volatility_index.replace("CLS","")` → CL adds `OVX`, GC adds `GVZ`, ES/ZC/ZS/SI/NQ add nothing. Saved as `raw/macro/fred_macro_data_{sym}.csv`.
  **Verified on disk** (`C:\CL_Analyst_Data\data\raw\macro\`): `fred_macro_data_cl.csv` header `Date,VIX,DXY,YIELD_CURVE,FED_FUNDS,OVX`; `_es/_zc/_zs/_si` = base 4 only; `_gc` has `GVZ`. Per-symbol COT files exist for cl/es/gc/ng/nq/si/zc/zs.
- **COT** (`download_macro_data.py:363-453`): `DisaggregatedAdapter` (CL/NG/HG/GC/PA/ZC/ZS/SI) vs `TffAdapter` (ES/NQ, approved MM←LevFunds/Prod←AssetMgr/Spec←Dealer mapping), both emitting the canonical `Date,OI,MM_*,Prod_*,Spec_*,*_Net` schema → **identical `COT_*` feature names for every symbol** (13 names; confirmed in CL_HourSet_14B, ES_HourSet_01B, GC_HourSet_01A parquets). Unmapped symbol raises (`get_cot_adapter`).
- **Parquet ground truth** (read via pyarrow):
  - `CL_HourSet_14B`: external MACRO stems VIX/OVX/DXY/YIELD_CURVE/FED_FUNDS + `MACRO_VIX_OVX_RATIO` + `MACRO_YIELD_CURVE_SIGN` (39 external) + internal `MACRO_WIDTH_*` ×5.
  - `ES_HourSet_01B`: **VIX only — no OVX anywhere, no ratio** (29 external) + `MACRO_WIDTH_*` ×5.
  - `GC_HourSet_01A`: VIX + **GVZ** + `MACRO_VIX_GVZ_RATIO` (39 external) + `MACRO_POS_*`/`MACRO_WIDTH_*` ×10.
  - Exactly matches the enumerable output of `_build_fred_features` (9 features/col × cols + ratio + sign + fed_funds) + `_build_cot_features` (13) — the naming is deterministic from the instrument. **ZC/ZS/SI (VIXCLS proxy per the standup): training features are `MACRO_VIX_*` — live must fetch VIX for them; consistent.**
- **Internal (non-file) MACRO features**: only `alpha_factory.py:479-480` produces `MACRO_WIDTH_{label}`/`MACRO_POS_{label}` — the complete internal set (verified by repo-wide grep). This matters: see deviation D1.
- **Registry (T1)**: `instrument_master.py` `live_vol_index` (CL/MCL→OVX, ES/MES/NQ/MNQ/HG/PA/ZC/ZS/SI/SIL→VIX, GC/MGC→GVZ) and `volatility_index` FRED series. Invariant `live_vol_index == volatility_index.replace("CLS","")` holds for all 15 entries (pin it — the IB index symbol doubles as the FRED column label / override key).

## 3. Design (localized; no refactor)

**Principle:** the brain instrument (T2's D3 — macro features feed the MODEL, so MCL uses CL's macro set automatically) drives (a) the index fetch list, (b) the engine's per-symbol CSVs, (c) the buildable `MACRO_*` name set; the model's `feature_names` stays the ultimate contract, enforced by a hard raise at construction.

### 3.1 `src/features/macro_features.py` — single source of truth for naming (owns `CHANGE_WINDOWS`/`PCTILE_WINDOWS`)
New module-level API (append; leaf-safe for existing importers):
```python
_INTERNAL_MACRO_PREFIXES = ("MACRO_POS_", "MACRO_WIDTH_")   # alpha_factory-computed, NOT file-backed
_FRED_BASE_COLS = ("VIX", "DXY", "YIELD_CURVE", "FED_FUNDS")

def vol_label_for(instrument) -> str:
    """FRED column label for the instrument's vol index (lockstep with
    download_macro_data.py:96 label derivation)."""
    return instrument.volatility_index.replace("CLS", "")

def is_external_macro_feature(name: str) -> bool:
    return name.startswith(("MACRO_", "COT_")) and not name.startswith(_INTERNAL_MACRO_PREFIXES)

def has_external_macro_features(feature_names) -> bool:
    return any(is_external_macro_feature(f) for f in feature_names)

def external_macro_feature_names(instrument) -> frozenset[str]:
    """Exact FRED+COT feature names buildable for this instrument —
    mirrors _build_fred_features/_build_cot_features name construction."""

def validate_external_macro_features(feature_names, instrument) -> None:
    """Raise ValueError listing every external-macro-shaped feature not
    buildable for `instrument`."""
```
Exact raise message (validate):
```
Model requires external macro/COT features unavailable for instrument '{sym}'
(FRED vol column '{vol_label}' from {volatility_index}): {sorted(bad)}.
Buildable stems for {sym}: MACRO_VIX*, MACRO_{vol_label}*, MACRO_DXY*,
MACRO_YIELD_CURVE*, MACRO_FED_FUNDS, COT_*. This model was trained on a
different instrument's macro set — refusing to start.
```
`_build_fred_features` (:453-468): delete the dead comment/`pass` block; replace file-sniffing with `vol_label = vol_label_for(self.instrument)`; iterate `dict.fromkeys(["VIX", vol_label, "DXY", "YIELD_CURVE"])` (dedup for VIX-proxy symbols). **Behavior-identical for every real file** (CL→OVX same, GC→GVZ same, ES/ZC/ZS/SI→VIX dedup, previously fallback-"OVX"-then-skip → same output columns). If the instrument-required vol column is absent from the file, RAISE (see Open Question Q1):
```
FRED macro file {self.fred_path} is missing required column '{vol_label}'
for instrument '{sym}' (volatility_index={volatility_index}).
Regenerate: python scripts/download_macro_data.py --symbol {sym} --fred-only
```
Constructor `instrument=None → get_instrument("CL")` **kept** (Open Question Q2): `data_processor.py` has 10 bare training call sites (:1701-:3072, legacy CL sets) + the parameterized :3199; churning training is out of T4 scope. Live-boundary enforcement instead (3.2/3.3).

### 3.2 `src/live_execution/feature_pipeline.py`
- `build_live_features(df, feature_names, lean=True, bar_size="5m", macro_overrides=None, return_last_n=1, *, instrument=None)`.
- `:250-254` → `_has_external_macro = has_external_macro_features(feature_names)`.
- Inside the external-macro branch: `if instrument is None: raise ValueError(...)` (message: `"build_live_features: model requires external macro/COT features (e.g. '{first}') but no instrument was passed — no silent CL default. Pass instrument=get_instrument('<SYM>')."`), then `MacroFeatureEngine(instrument=instrument).merge_all(...)`. Non-macro callers unaffected (parity tests with ICHIMOKU/TS/DIST feature lists keep working unchanged — verified `test_feature_parity.py`/`test_pipeline_parity_hourly.py` use no external macro names).

### 3.3 `src/live_execution/live_trader.py`
- `:217-221` → `self._needs_macro = has_external_macro_features(self.feature_names)` (import from macro_features; computed before context resolution — helper is instrument-independent, no reorder needed).
- After `:278` context resolution: `validate_external_macro_features(self.feature_names, self._instrument_context.brain_instrument)` when `_needs_macro` — **the ES-config-with-OVX-model raise at startup, before any IBKR object**.
- New `_brain_instrument` property beside `_brain_symbol`/`_tick_size` (:2097-2131), same T3 seam-fallback: prefer `_instrument_context.brain_instrument`, fall back to `get_instrument(self._brain_symbol)` for `object.__new__` test stubs — structural derivation, raises on unknown.
- `:637-645` startup fetch: `vol = self._brain_instrument.live_vol_index; index_syms = ["VIX"] + ([vol] if vol != "VIX" else [])` — CL yields `["VIX","OVX"]` **in today's exact order**; ES/ZC/ZS/SI `["VIX"]`; GC `["VIX","GVZ"]`. Alias = symbol (registry invariant makes IB symbol ≡ FRED column ≡ override key). Log line reflects the actual list.
- Engine sites `:696, :700, :3749, :3755` → `MacroFeatureEngine(instrument=self._brain_instrument)` (the `:3747` local import stays).
- `build_live_features` calls `:2018` and `:2861` → add `instrument=self._brain_instrument`.
- Mute blocks (`:704-720, :2868-2892, :3752-3800`) **byte-untouched** — staleness still keys off DXY, present in every per-symbol file.

### 3.4 `src/live_execution/adapters/ibkr_data_feed.py`
```python
_INDEX_CONTRACT_SPECS = {"VIX": ("CBOE","USD"), "OVX": ("CBOE","USD"),
                         "GVZ": ("CBOE","USD"), "DX": ("NYBOT","USD")}
```
- `fetch_daily_close_async` (:182-187): build `Index(symbol, *spec)` from the map; unknown → `ValueError(f"fetch_daily_close_async only supports index symbols {sorted(_INDEX_CONTRACT_SPECS)}. Got: {symbol}")`.
- `subscribe_live_bars[_async]` index branches (:103-107/:148-152) and qualification-exemption sets (:125/:169) consume the same map (keeps the dormant DX branch; adds GVZ for consistency). `ibkr_client.fetch_daily_close*` is already contract-generic — no change. SimulatedDataFeed — no change (startup try/except preserved).

### 3.5 Explicitly NOT in scope (guards)
No session-hours/watchdog/`_get_market_status` (T5); no generator/`batch_post_optimizer` (T6); no fleet_runner; no backtest engine/`data_processor` behavior change (macro_features edits are provably output-identical for training inputs, see 3.1); no hourly re-fetch of index closes (doesn't exist at HEAD; adding one would be new behavior — out of scope); no `_STALE_THRESHOLDS` retuning.

### 3.6 Mechanical test-fixture churn (enumerated)
- `tests/test_fred_live_override_index.py` `_make_engine_with_mock_fred` (:54-60) and `tests/test_macro_pctile_fast_rank.py` `_make_engine` (:136-142): add `engine.instrument.volatility_index = "OVXCLS"` (MagicMock instruments; neither file asserts OVX columns — T1 precedent for cross-ticket mechanical fixture updates).
- `tests/smoke_test_pipeline.py:400` (manual `__main__` tool, not pytest-collected) and `scripts/feature_parity_compare.py:203`, `scripts/feature_parity_multi_ts.py:56,169`: pass `instrument=get_instrument("CL")` explicitly (their feature lists include MACRO/COT).
- `tests/test_nan_guard.py`, `test_bad_data.py`, `test_live_features.py`, `test_feature_parity.py`, `test_pipeline_parity_hourly.py`: **zero churn** (no external macro names in their feature lists).

## 4. Hard-constraint compliance (CL byte-identity argument)

1. **Same fetches:** CL fetch list `["VIX","OVX"]`, same order, same aliases, same swallow-and-warn per symbol.
2. **Same files:** `MacroFeatureEngine(instrument=CL)` resolves the identical `fred_macro_data_cl.csv`/`cftc_cot_cl.csv` paths as today's bare constructor.
3. **Same features:** `_needs_macro`/`_has_external_macro` — for any feature drawn from CL parquets the old prefix list and the new external-shaped rule are extensionally equal (CL external stems = exactly the old list; internal WIDTH/POS excluded by both); `_build_fred_features` vol label OVX either way; validation passes for every CL feature set (⊆ CL buildable set).
4. **MCL:** brain instrument = CL → CL files/fetches/features, per T2's brain-keyed convention.
5. **No silent defaults:** missing per-symbol CSV already hard-raises (`_load_fred`/`_load_cot` FileNotFoundError naming the per-symbol path; startup Step 7 exceptions propagate to the `start()` fatal handler at :767-776 and re-raise); `build_live_features` raises when macro is needed and instrument is absent; the one retained default (engine constructor, training-only) is documented + gated behind the live-boundary raises (Q2).
6. **Blocking parity gate:** HS14B ledger parity re-run required before merge (T1-T3 convention) — the touched code sits directly on the parity path (livetest_engine drives the real LiveTrader → `_on_new_bar` → `build_live_features` → `merge_all`).

## 5. TDD test list (new file `tests/test_macro_vol_parameterization.py` unless noted)

**CL regression pins**
1. `_needs_macro` truth table: `[MACRO_VIX]`→T, `[COT_MM_NET]`→T, `[MACRO_OVX_CHG_1D]`→T, `[MACRO_WIDTH_1W]`→**F**, `[MACRO_POS_1M]`→**F**, `[MACD, ATR_14]`→F (extends `test_live_macro_refresh.py` pins).
2. Startup fetch pin (CL config, mocked data_client): `fetch_daily_close` called exactly `["VIX","OVX"]` in order; `_macro_daily_closes` keys `{"VIX","OVX"}`.
3. `MacroFeatureEngine(instrument=get_instrument("CL"))` paths == bare `MacroFeatureEngine()` paths (fred `_cl.csv` / cot `_cl.csv`).
4. `_build_fred_features` on a CL-shaped fixture frame: exact column set (incl. `MACRO_VIX_OVX_RATIO`) and bitwise-equal values pre/post change.
5. `validate_external_macro_features(<full 39-name CL external set + COT 13>, CL)` does not raise; `LiveTrader(CL config with HS14B-style features)` constructs.
6. Blocking: HS14B ledger parity gate re-run → PARITY: PASS.

**ES (VIX-only, no OVX)**
7. ES config trader: startup fetch list == `["VIX"]` — assert `fetch_daily_close` **never** called with "OVX".
8. `MacroFeatureEngine(instrument=ES)` resolves `fred_macro_data_es.csv`/`cftc_cot_es.csv`.
9. `LiveTrader` with `execution_symbol: ES` + feature `MACRO_OVX_CHG_1D` → ValueError naming the feature, 'ES', and buildable stems (raise happens in `__init__`, before connect).
10. `MACRO_VIX_GVZ_RATIO` on ES → ValueError (exact-name enumeration; prefix matching would miss it).
11. ES-shaped FRED frame (base 4 cols): builds `MACRO_VIX_*` only, no duplicate columns, no OVX skip-warning.

**GC (GVZ)**
12. GC config: fetch list `["VIX","GVZ"]`; `_needs_macro` True for `[MACRO_GVZ_CHG_1D]` (M2 pin).
13. `fetch_daily_close_async("GVZ")` builds `Index("GVZ","CBOE","USD")` (mocked manager); unknown symbol still raises ValueError.
14. GC-shaped frame → `MACRO_GVZ_*` + `MACRO_VIX_GVZ_RATIO` built; enumerator ⊇ check.

**Contract/enumerator lockstep**
15. `external_macro_feature_names(instr)` == actual built FRED+COT columns on synthetic frames, for CL, ES, GC (39/29/39 external + 13 COT).
16. Registry invariant: ∀ entries `live_vol_index == volatility_index.replace("CLS","")` and `live_vol_index ∈ _INDEX_CONTRACT_SPECS`.

**Failure modes / mute**
17. `MacroFeatureEngine(instrument=ES)` with missing file → FileNotFoundError message contains `fred_macro_data_es.csv` (resp. `cftc_cot_es.csv`).
18. `build_live_features(external-macro features, instrument=None)` → ValueError; same call with `instrument=CL` and mocked engine → merges; non-macro features + `instrument=None` → works (parity-script pin).
19. Mute preserved: ES engine with stale-DXY fixture raises StaleDataException from `_build_fred_features`; LiveTrader startup mute activation and hourly clear behave as today (parameterized over CL/ES).
20. MCL config: brain=CL → CL files + `["VIX","OVX"]` fetch (brain-driven pin).
21. FRED file missing instrument vol column (GC instrument, ES-shaped file) → ValueError with regenerate hint (if Q1 approved; else pin the warning+skip).

## 6. Deviations from the blueprint T4 sketch (with justification)

- **D1 (correctness):** Blueprint §7 proposed `_needs_macro`/`_has_external_macro` := `startswith(("MACRO_","COT_"))`. That is **wrong as written**: AlphaFactory's internal `MACRO_WIDTH_*`/`MACRO_POS_*` (present in ES/GC training parquets; ES has WIDTH-only, GC both) would misclassify internal-macro-only models as needing FRED/COT — spurious file loads, index fetches, and Safety-Mute exposure. Deviation: exclude `_INTERNAL_MACRO_PREFIXES`.
- **D2 (determinism):** Blueprint's `{"VIX"} ∪ {live_vol_index}` set → replaced by ordered `["VIX"] + [vol if != VIX]` so the CL fetch order is byte-identical to today.
- **D3 (addition):** Startup model-vs-instrument validation is not in blueprint §7, but the ticket's design goal explicitly requires the ES+OVX raise; implemented as exact-name enumeration (not prefixes) to close the `MACRO_VIX_GVZ_RATIO`-on-ES hole.
- **D4 (addition):** instrument-driven `vol_label` in `_build_fred_features` (replacing file-sniffing + deleting the dead :456-465 block) — required by design goal (c); proven output-identical for every real file shape (§2).
- **D5 (line drift, informational):** pre-T2 refs → HEAD: :613-621→:637-645; :217-223→:217-221; :3640-3699→:3743-3802; feed :86-88/:128-130/:159-174→:103-107/:148-152/:182-197. Also: the "periodic hourly macro refresh" does **not** fetch index closes at HEAD — only engine refresh — so item (a) of the design goal applies to startup only (documented, not silently extended).

## 7. Open questions requiring human authorization

- **Q1:** Should `_build_fred_features` hard-raise when the FRED file lacks the instrument's vol column (file/registry misprovisioning)? Recommended YES (no-silent-defaults; today it warn-skips → downstream missing-column → `build_live_features` returns None → silent no-trade). Shared with training via `data_processor.py:3199`; every existing file passes, so no current pipeline breaks — but it is a shared-engine behavior change.
- **Q2:** `MacroFeatureEngine.__init__` keeps `instrument=None → CL` for the 10 legacy training call sites; enforcement moved to the live boundary (`build_live_features` raise + live_trader always passing). Confirm this satisfies the no-silent-defaults rule (as recorded it targets config fields), or authorize the wider `data_processor` churn to make the constructor arg required.
- **Q3 (ops, T7):** GVZ market-data entitlement on the IBKR data account cannot be verified offline (code side is one map entry; `Index("GVZ","CBOE","USD")` is structurally valid ib_insync). T1 deferred this to T4/T7 — recommend T7 canary checks it.

## 8. Files to change (summary)

| File | Change |
|---|---|
| `src/features/macro_features.py` | +5 module helpers; instrument-driven vol label; dead block deletion; (Q1) missing-vol-column raise |
| `src/live_execution/feature_pipeline.py` | `instrument` kwarg; helper-based detection; engine instantiation with instrument; instrument-missing raise |
| `src/live_execution/live_trader.py` | helper-based `_needs_macro`; startup validation call; `_brain_instrument` property; instrument-derived fetch list; 4 engine sites + 2 `build_live_features` sites |
| `src/live_execution/adapters/ibkr_data_feed.py` | `_INDEX_CONTRACT_SPECS` map; GVZ support; map-driven branches/messages |
| `tests/test_macro_vol_parameterization.py` | NEW (§5) |
| `tests/test_fred_live_override_index.py`, `tests/test_macro_pctile_fast_rank.py` | mechanical fixture line each (`volatility_index="OVXCLS"`) |
| `tests/smoke_test_pipeline.py`, `scripts/feature_parity_compare.py`, `scripts/feature_parity_multi_ts.py` | mechanical `instrument=get_instrument("CL")` |

No changes to: `ibkr_client.py`, `cli.py`, `instrument_context.py`, `instrument_master.py` (registry already complete; invariant test may live in the new test file), `data_manager.py`, simulated adapters, `download_macro_data.py`, `data_processor.py`.
