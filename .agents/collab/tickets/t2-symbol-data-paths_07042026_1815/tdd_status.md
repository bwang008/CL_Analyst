# TDD Status — t2-symbol-data-paths_07042026_1815

PHASE: Red
DATE: 2026-07-04 19:09
AGENT: TDD-TESTER

## Test files (Strict-Lock: TRUE — implementation agents may NOT modify)
- `tests/test_symbol_data_paths.py` — 821 lines, 44 tests (49 parametrized nodes).
  Ghost import: `DataPaths` / `derive_data_paths` from `src.live_execution.data_manager`.
- `tests/test_build_future_contract.py` — 376 lines, 27 tests (39 parametrized nodes).
  Ghost import: `build_future_contract` from `src.live_execution.ibkr_client`.

## Red proof
`conda run -n trader python -m pytest tests/test_symbol_data_paths.py tests/test_build_future_contract.py -v --tb=short --continue-on-collection-errors`
-> 2 collection errors, both missing-implementation ImportError (no syntax errors):
- `ImportError: cannot import name 'DataPaths' from 'src.live_execution.data_manager'`
- `ImportError: cannot import name 'build_future_contract' from 'src.live_execution.ibkr_client'`

Both files fully parse (the ImportError fires at module-exec, after compilation).
All mock seams (LiveTrader DataManager-patch + Path.exists, adapter
IBKRConnectionManager patch, cli patch set, DataManager backfill/ledger,
IBKRConnectionManager fetch/front-month, exec resolve_contract) were
smoke-verified against HEAD via a scratchpad script — 10/10 OK, so Green-phase
failures will be attributable to implementation only.

## T1 suites undisturbed
`conda run -n trader python -m pytest tests/test_instrument_context.py tests/test_instrument_master_live_fields.py -q` -> 110 passed.

## Coverage notes for the Coder (audit section 6 items 1-30 + C1/C3/C6/C8)
- Item 26 implemented as pinned in the audit: reconnect backfill and the
  DataManager-facing fetch calls carry NO symbol kwarg (symbol is
  ADAPTER-BOUND per D1); the symbol is asserted at the IBKRConnectionManager
  seam for DataManager backfill/ledger fetches, and SimulatedDataFeed
  compatibility is pinned directly (C6/C7 parity-harness guard).
- DataPaths field names pinned: seed_5m, cache_5m, ledger_5m, seed_1h,
  cache_1h, ledger_1h, roll_metadata.
- derive_data_paths must consume data_manager's module-level
  get_data_path/get_data_root imports (expression-fidelity test patches
  `src.live_execution.data_manager.get_data_path/get_data_root`).
- bars_per_day pinned ONLY for CL (288/24) — ES provisioning is T5 scope.
- Mechanical fixture churn in tests/test_data_manager.py / tests/test_rollover.py
  is the Coder's responsibility (C4 census: 15 constructions + 8
  _ROLL_METADATA_PATH patch migrations); those files were NOT touched here.

---

# TDD Status — t2-symbol-data-paths_07042026_1815

PHASE: Green
DATE: 2026-07-04 20:05
AGENT: TDD-CODER

## Implementation (audit.md sections 4-5, conditions C1-C8; Strict-Lock test files untouched)
- `src/live_execution/ibkr_client.py` — NEW `build_future_contract` (D6 signature;
  registry exchange when exchange=None; symbol validated via get_instrument even
  with explicit exchange; verbatim legacy month-required message; C3
  includeExpired=True on ContFuture branch ONLY); `build_cl_contract`/
  `build_mcl_contract` -> delegating wrappers (D5, byte-identical);
  `fetch_historical_bars` / `fetch_historical_bars_by_duration`(+async) gained
  REQUIRED keyword-only `symbol`; `get_front_month_contract`(+async) search
  Future exchange from registry (`_EXPIRY_BUFFER_DAYS` untouched at 6); module
  convenience `fetch_historical_bars` stays CL (passes symbol="CL" explicitly).
- `src/live_execution/adapters/ibkr_data_feed.py` — required keyword-only
  `instrument_context` (D1); 3 fetch delegations forward
  `symbol=ctx.brain_symbol`; continuous routing via
  `build_future_contract(symbol, continuous=True)` (not-MCL->CL fallback DEAD);
  front-month Future exchange from registry; VIX/OVX/DX branches verbatim;
  adapter `get_front_month_contract` "CL" default dropped.
- `src/live_execution/interfaces/data_feed_interface.py` — abstract
  `get_front_month_contract` "CL" default dropped (C6). SimulatedDataFeed:
  ZERO changes.
- `src/live_execution/data_manager.py` — NEW frozen `DataPaths` + pure
  `derive_data_paths(symbol)` (C1 asymmetry: 5m seed via get_data_path, other 6
  from get_data_root; CL exceptions ONLY warm_start_cache.parquet /
  warm_start_cache_1h.parquet / .roll_metadata.json); DataManager required
  keyword-only `symbol` (validated), path params -> Optional[None]->derived;
  `roll_metadata_path` per-instance (constructor override); module globals
  `_DEFAULT_*`/`_ROLL_METADATA_PATH` deleted; seed-missing messages
  symbol-generic (types/triggers verbatim); dead builder import removed (C5).
- `src/live_execution/live_trader.py` — paths via
  `derive_data_paths(ctx.brain_symbol)` (D3); both DataManagers get symbol +
  ledger + shared roll_metadata_path; 1h seed default `{SYM}_raw_1h.parquet`;
  `live_config.seed_path_1h` override preserved exactly; 1h FileNotFoundError
  symbol-aware (cache/seed/CL_DATA_ROOT lines kept); brain subscriptions
  (sync + async twin) use brain symbol, Hands stays execution symbol; module
  constants `_DEFAULT_SEED_PATH`/`_DEFAULT_CACHE_PATH` deleted; dead import at
  reconnect backfill removed (C5); C2 cross-talk comment added at the
  roll-metadata wiring.
- `src/live_execution/cli.py` — `--seed-path`/`--cache-path` default None ->
  derived post-ctx from `derive_data_paths(ctx.brain_symbol)`;
  `DataFeedFactory.create(..., instrument_context=ctx)`;
  `_merge_legacy_cid_caches` gated on `ctx.brain_symbol == "CL"` (C8);
  shared-cache comment now "shared per brain symbol".
- `src/live_execution/adapters/ibkr_execution.py` — `resolve_contract` exchange
  from registry (manager-approved 1-liner); dead `build_cl_contract` import
  removed (C5).
- C4 churn (ONLY approved test edits): `tests/test_data_manager.py` 15
  constructions gained `symbol="CL"`; `tests/test_rollover.py` fixture gained
  `symbol="CL"` + tmp `roll_metadata_path`, all 8 `_ROLL_METADATA_PATH`
  module-global patches migrated to the instance attribute.

## Deviation (reported)
- `tests/test_cooldown.py::TestTimezoneResubscribe` (3 tests, NOT in the C4
  census, NOT editable under my contract) builds LiveTrader via
  `object.__new__` with only `_execution_symbol` set — `_subscribe` reading
  `self._instrument_context.brain_symbol` raised AttributeError. Accommodated
  in the implementation with a `LiveTrader._brain_symbol` property: prefers
  the resolved InstrumentContext (always set by `__init__`), else the SAME
  structural derivation the resolver uses (`micro_of or execution_symbol`)
  via get_instrument — raises on unknown symbols, no silent CL default.

## Green proof
1. `conda run -n trader python -m pytest tests/test_symbol_data_paths.py tests/test_build_future_contract.py -v --tb=short`
   -> **88 passed** (49 + 39 nodes).
2. `conda run -n trader python -m pytest tests/test_data_manager.py tests/test_rollover.py tests/test_data_manager_ratio.py tests/test_instrument_context.py tests/test_instrument_master_live_fields.py -q`
   -> **155 passed**.
3. `conda run -n trader python -m pytest tests/ -q --tb=short -m "not slow" --ignore=tests/test_macro_pctile_fast_rank.py`
   -> **1012 passed, 0 failed** (= 924 baseline + 88 new).

NOT committed (manager commits after the C7 HS14B parity gate). Scope guards
honored: no tick/pricing (T3), no macro/vol (T4), no watchdog/rollover-timing/
_EXPIRY_BUFFER_DAYS source (T5), no fleet_runner, no SimulatedDataFeed edits.
