# TDD Result — t1-instrument-metadata_07042026_1543

**Outcome: GREEN + PARITY PASS — ticket complete.**

- Red: 74 failing / 2 intentional pins passing (clean missing-implementation failures); pre-existing baseline 814 passed (manager-verified).
- Green: **924 passed, 0 failed** full fast suite (814 baseline + 110 new), manager-verified independently.
- Reviewer condition C3: HS14B ledger parity gate re-run (`setup --disable-trailing`, 2200 warmup + 336 replay) → **PARITY: PASS**, exit 0 — 15=15 trades, 15/15 exact-cent, 15/15 side+exit mapping, $0.00 total PnL delta ($1,695.01 both engines). T1's raise path does not perturb the CL trade path.

## Files changed
- `src/core/instrument_master.py` — Instrument gains 11 live fields; all entries populated (verified specs); micros MCL/MES/MNQ/MGC/SIL added (C1 parent cftc/vol inheritance); PA tick corrected 0.05→0.10/$10.
- `src/live_execution/instrument_context.py` (NEW) — resolve_instrument_context / derive_model_symbol / validate_models_against_symbol; hard-raise validation (C2 .upper()).
- `src/live_execution/live_trader.py` — silent `get("execution_symbol", "CL")` replaced with resolver; `_execution_symbol` name preserved (49 consumers).
- `src/live_execution/cli.py` — fail-fast resolution before any factory/IBKR construction.
- `configs/strategies/ensemble2_opt.json` — `"execution_symbol": "CL"` added (only config missing it).
- `tests/test_instrument_master_live_fields.py` (NEW, 76 tests) + `tests/test_instrument_context.py` (NEW, 34 tests) — Strict-Lock, untouched by Coder.
- `tests/test_live_macro_refresh.py` — mechanical fixture update only.

## Operational notes
- **ES01B_Sharpe_E03_07042026.json now refuses to start by design** (exec CL vs E2E_ES_* models) until T6 regenerates it — pinned by intended-failure test.
- GVZ IBKR permission check deferred to T4/T7; session_hours_ct provisioning-grade until T5.
- T6 should emit explicit `models.<side>.symbol`; T1's validator already hard-enforces it when present.
