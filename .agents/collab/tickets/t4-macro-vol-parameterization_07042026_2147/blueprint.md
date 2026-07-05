# Ticket Resolution Blueprint — t4-macro-vol-parameterization_07042026_2147
**Ticket Directory:** `.agents/collab/tickets/t4-macro-vol-parameterization_07042026_2147/`

## Requirement Summary
T4 of the multi-symbol live-gaps program: the live macro/vol path is CL-hardcoded at 8
sites — the four bare `MacroFeatureEngine()` constructions load CL's FRED/COT files for
ANY symbol (silent misdata), the startup daily-close fetch list is hardcoded [VIX, OVX],
`_needs_macro` uses a literal prefix list, and the data feed's index map lacks GVZ.
Brain instrument (T1 registry `live_vol_index`/`cftc_code`, T2 threading, T3 seam
pattern) now drives fetches, file selection, and feature-name validation; the model's
feature_names remain the ultimate contract (ES model wanting MACRO_OVX_* → ValueError
at startup, before connect). Reviewer verdict: APPROVE. Full design: `audit.md`
(site map §2, design §4, D1-D4 deviations, 21-test list); verification: `impact_review.md`.
This document governs on conflict.

## Manager rulings (given)
- Q1 ACKed: hard-raise when a FRED file lacks the instrument's vol column (all on-disk
  files verified passing, incl. NG→OVX which audit §2 omitted — reviewer condition 2).
- Q2: MacroFeatureEngine constructor keeps `instrument=None→CL` as a DOCUMENTED
  legacy-training shim; the LIVE boundary always passes instrument explicitly (T8
  migrates training call sites later).
- Q3: GVZ IBKR entitlement verified in T7's canary, not here.

## Target Files
- `src/features/macro_features.py` — naming source of truth: `vol_label_for(instrument)`,
  `has_external_macro_features(feature_names, instrument)`,
  `external_macro_feature_names(instrument)`, `validate_external_macro_features(
  feature_names, instrument)` (raise message dedupes vol-label stems for VIX-proxy
  symbols — reviewer condition 4); engine loads fred_macro_data_<sym>.csv /
  cftc_cot_<sym>.csv per instrument; instrument-driven vol label in
  `_build_fred_features` (D4 — replaces file-sniffing; proven output-identical);
  Q1 hard-raise on missing vol column.
- `src/live_execution/feature_pipeline.py` — `build_live_features` gains keyword
  `instrument`; raises when external macro features are needed and instrument is None;
  internal MACRO_WIDTH_*/MACRO_POS_* prefixes excluded from "external" classification
  (D1 — the naive MACRO_ prefix misclassifies; `feature_pipeline.py:203` already
  excludes them, corroborating).
- `src/live_execution/live_trader.py` — `_brain_instrument` seam property (T3 pattern:
  from InstrumentContext, structural fallback, raises when unresolvable); `_needs_macro`
  via the helper (extensionally identical for CL/ES, adds exactly MACRO_GVZ_* for GC);
  startup fetch list = ordered `["VIX"] + ([vol] if vol != "VIX")` (CL byte-order
  ["VIX","OVX"] — D2); startup exact-name validation in `__init__` BEFORE connect
  (connect happens in start(); wording per reviewer condition 3); the four
  MacroFeatureEngine constructions pass the instrument.
- `src/live_execution/adapters/ibkr_data_feed.py` — `_INDEX_CONTRACT_SPECS` map
  (VIX/CBOE, OVX/CBOE, GVZ/CBOE, DX/NYBOT) replacing the VIX/OVX-only branch in
  `fetch_daily_close_async`; unknown index symbol raises.
- Mechanical churn ONLY (per verified census): 2 MagicMock test fixtures gain the
  `volatility_index` attribute; 1 smoke tool + 2 parity scripts pass instrument where
  they call build_live_features/MacroFeatureEngine.

## Hard Constraints
1. CL byte-identical: fetch order ["VIX","OVX"]; same _cl.csv files; `_needs_macro`
   truth unchanged for every shipped CL config (HS14B boosters verified: 233 features,
   validation cannot raise) — regression pins required.
2. No silent defaults at the live boundary (instrument always explicit); engine's
   None→CL shim documented as training-only.
3. MCL uses CL's macro set (brain-driven).
4. Missing per-symbol FRED/COT file → existing hard-raise semantics preserved (the
   engine already raises on missing COT; keep messages symbol-aware).
5. Mute/StaleDataException semantics unchanged (already symbol-independent via DXY).
6. Scope guards: NO session-hours/watchdog/rollover (T5), NO generator (T6), NO
   fleet_runner, NO backtest engine, NO training call-site migration (T8).

## Test requirements (audit's 21-item list, binding; highlights)
- CL pins: fetch order, file paths, _needs_macro truth table old-vs-new on real
  HS14B feature lists, validation no-raise for shipped CL configs.
- ES: VIX-only fetch; MACRO_OVX_* in feature_names → ValueError at construction with
  actionable message; loads _es.csv files.
- GC: GVZ fetch + MACRO_GVZ_* accepted; _INDEX_CONTRACT_SPECS GVZ/CBOE.
- ZC/ZS/SI: VIX-proxy — fetch list is exactly ["VIX"]; no duplicate stems in raise
  messages (condition 4).
- Q1: FRED file missing the instrument's vol column → raise (temp CSV fixture).
- build_live_features: external-needed + instrument None → raise; internal-only
  feature list + instrument None → no raise (backward compat).
- Unknown index symbol in fetch_daily_close → raise.

## Verification
- Full fast suite green (baseline 1129 + new).
- BLOCKING: HS14B ledger parity gate (`setup --disable-trailing`, 2200/336) →
  PARITY: PASS before commit.
