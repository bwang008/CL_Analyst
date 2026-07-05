# TDD Result — t4-macro-vol-parameterization_07042026_2147

**Outcome: GREEN + PARITY PASS — ticket complete. Reviewer verdict: APPROVE.**

- Red: 46 tests / 83 nodes, ghost-import collection error; seams pre-validated at HEAD
  (9/9). Baseline 1129 (manager-verified).
- Green: **1212 passed, 0 failed**, first run (manager-verified independently).
- Blocking parity gate: **PARITY: PASS**, exit 0 — $0.00 delta ($1,695.01 both engines).
  Note: this gate run exercised the T4 code directly (parity scripts now pass
  instrument=get_instrument("CL")), doubly confirming CL bit-stability.

## Files changed
- `src/features/macro_features.py` — vol_label_for / is_external_macro_feature /
  has_external_macro_features / external_macro_feature_names /
  validate_external_macro_features; per-instrument FRED/COT file resolution
  (constructor instrument=None→CL documented as training-only shim — Q2);
  registry-driven vol label in _build_fred_features (D4, bit-identical CL pin);
  Q1 hard-raise on FRED file missing the instrument's vol column.
- `src/live_execution/feature_pipeline.py` — build_live_features(instrument=);
  external-needed + None → raise BEFORE engine construction; D1 internal
  MACRO_WIDTH_/MACRO_POS_ exclusion.
- `src/live_execution/live_trader.py` — _brain_instrument seam property; helper-based
  _needs_macro; startup feature-contract validation in __init__ before connect
  (ES+MACRO_OVX_* → ValueError); ordered per-instrument fetch list (CL ["VIX","OVX"]
  byte-order; GC ["VIX","GVZ"]; ES/ZC/ZS/SI ["VIX"]); 4 engine sites + 2
  build_live_features sites pass the instrument.
- `src/live_execution/adapters/ibkr_data_feed.py` — _INDEX_CONTRACT_SPECS
  (VIX/OVX/GVZ→CBOE, DX→NYBOT); unknown index raises listing supported.
- `tests/test_macro_vol_parameterization.py` — NEW, 46 tests (Strict-Lock).
- Mechanical churn: 2 test fixtures (+volatility_index), smoke tool + 2 parity scripts
  (+instrument=get_instrument("CL")).

## Notes
- GVZ IBKR market-data entitlement remains unverified offline → T7 canary checks it.
- T8 sweeps the 10 legacy training MacroFeatureEngine call sites to explicit instrument.
- Silent-misdata gap class now fully closed across T2 (prices/paths) + T4 (macro):
  a non-CL config can no longer receive CL data on any live input path.
