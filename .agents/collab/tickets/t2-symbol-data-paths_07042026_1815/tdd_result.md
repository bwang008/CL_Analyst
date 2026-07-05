# TDD Result — t2-symbol-data-paths_07042026_1815

**Outcome: GREEN + PARITY PASS — ticket complete. HUMAN AUTHORIZED (multi-component) 2026-07-04.**

- Red: 71 tests / 88 nodes in `tests/test_symbol_data_paths.py` + `tests/test_build_future_contract.py`, clean ImportErrors; baseline 924 (manager-verified).
- Green: **1023 passed, 0 failed** full fast suite (manager-verified; includes the parallel session's macro-pctile work landed at c1c78fc/dae4be4).
- C7 blocking parity gate: **PARITY: PASS**, exit 0 — 15=15 trades, 15/15 exact-cent, $0.00 delta ($1,695.01 both engines). Data-path spine + contract routing changes leave the CL trade path bit-identical.

## Files changed
- `src/live_execution/ibkr_client.py` — registry-driven `build_future_contract`; CL/MCL builders → wrappers; fetch methods REQUIRE keyword-only symbol; front-month exchange from registry.
- `src/live_execution/adapters/ibkr_data_feed.py` — required `instrument_context`; brain-symbol fetch delegation; CL fallback dead; index branches preserved.
- `src/live_execution/interfaces/data_feed_interface.py` — front-month CL default dropped (C6); SimulatedDataFeed untouched.
- `src/live_execution/data_manager.py` — `DataPaths` + `derive_data_paths(symbol)` single naming authority (3 CL legacy exceptions); required symbol; per-instance roll-metadata path (module global dead).
- `src/live_execution/live_trader.py` — DataManager paths via brain symbol; `{SYM}_raw_1h.parquet` default; C2 cross-talk comment; `_brain_symbol` property (structural derivation for legacy `__new__` test seams — no silent default).
- `src/live_execution/cli.py` — derived defaults; instrument_context to factory; cid-cache merge gated to brain CL (C8 — fleet-critical fix).
- `src/live_execution/adapters/ibkr_execution.py` — resolve_contract exchange from registry.
- Tests: +`test_symbol_data_paths.py` (44), +`test_build_future_contract.py` (27, committed earlier in dae4be4 by parallel session); mechanical churn in `test_data_manager.py` (15 × symbol="CL"), `test_rollover.py` (8 global patches → instance attr).

## Notes for downstream tickets
- T3 owns: tick-size order pricing incl. `close_cl_position*` NYMEX injection.
- T5 owns: roll-metadata front_month_id normalization (C2 cross-talk), _EXPIRY_BUFFER_DAYS source, session hours/watchdog.
- T6 owns: deletion of the CL/MCL wrapper builders, per-symbol backup filenames, smoke-test cadence regex, live_config seed_path_5m/cache_path keys.
