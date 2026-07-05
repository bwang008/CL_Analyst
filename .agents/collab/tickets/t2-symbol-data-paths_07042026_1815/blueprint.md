# Ticket Resolution Blueprint — t2-symbol-data-paths_07042026_1815
**Ticket Directory:** `.agents/collab/tickets/t2-symbol-data-paths_07042026_1815/`

## Requirement Summary
T2 of the multi-symbol live-gaps program (parent:
`.agents/collab/tickets/multi-symbol-live-gaps_07042026_1520/blueprint.md`).
Kills silent misdata for non-CL symbols: brain-stream subscriptions fall back to CL
for any non-MCL symbol, historical backfill builds CL contracts unconditionally, and
all warm-start caches/ledgers/roll-metadata share CL-named files. Consumes T1's
InstrumentContext/registry. HUMAN AUTHORIZED 2026-07-04 after Impact-Reviewer
technical PASS; conditions C1–C8 binding. Full design detail: `audit.md` §4–§6
(decisions D1–D6, naming table, 30-item test list); verification + conditions:
`impact_review.md`. This document governs where they conflict.

## Manager rulings (given)
- MCL brain=CL semantics ACKed (matches Two-Stream docstring + T1 brain_symbol).
- `ibkr_execution.py:73` resolve_contract NYMEX→registry exchange 1-liner INCLUDED.
- Mechanical test-fixture churn approved (census-corrected: ~15 in test_data_manager.py,
  8 `_ROLL_METADATA_PATH` patches in test_rollover.py → new param, 0 in
  test_data_manager_ratio.py).
- Niceties deferred to T6 (per-symbol backup names, smoke-test regex, live_config
  seed_path_5m/cache_path keys).

## Target Files
- `src/live_execution/ibkr_client.py` — new `build_future_contract(symbol, *, continuous,
  contract_month, exchange=None→registry, currency)` (D6; exact legacy error message for
  continuous=False without month); `build_cl_contract`/`build_mcl_contract` become thin
  wrappers (byte-identical fields incl. includeExpired=True on ContFuture branch ONLY —
  C3 pins both branches); `fetch_historical_bars`, `fetch_historical_bars_by_duration`
  (+async) gain REQUIRED keyword-only `symbol`; `get_front_month_contract(+async)`
  exchange from registry.
- `src/live_execution/adapters/ibkr_data_feed.py` — `__init__` gains required
  `instrument_context: InstrumentContext`; fetch delegations pass
  `symbol=ctx.brain_symbol`; continuous subscription routing uses the REQUESTED symbol
  via build_future_contract (kill not-MCL→CL fallback); front-month Future exchange from
  registry; VIX/OVX/DX index branches preserved verbatim.
- `src/live_execution/interfaces/data_feed_interface.py` — symbol default dropped from
  the abstract fetch signature (C6: honest diff — interface DOES change; SimulatedDataFeed
  stays untouched).
- `src/live_execution/data_manager.py` — required keyword-only `symbol: str` (validated
  via get_instrument); new pure `derive_data_paths(symbol) -> DataPaths` as the single
  naming authority (C1: preserve the existing path-expression asymmetry — 5m seed via
  get_data_path(), the other 6 composed from get_data_root()); module-global
  `_ROLL_METADATA_PATH` replaced by instance attribute (constructor override for tests);
  backfill/roll/ledger fetches pass symbol; dead `build_cl_contract`+`build_mcl_contract`
  imports removed (C5: BOTH names at data_manager.py:855-858).
- `src/live_execution/live_trader.py` — DataManager construction paths via
  derive_data_paths(ctx.brain_symbol) (D3); CL byte-identical (3 exceptions:
  warm_start_cache.parquet, warm_start_cache_1h.parquet, .roll_metadata.json);
  1h seed default pattern `{SYM}_raw_1h.parquet`; `live_config.seed_path_1h` override
  preserved; dead imports at :2373 removed (C5); C2: comment documenting the
  CL+MCL concurrent roll-metadata cross-talk (noise, not misdata; T5 normalizes).
- `src/live_execution/cli.py` — seed/cache defaults via derive_data_paths; pass
  instrument_context to DataFeedFactory (kwargs pass-through, no factory change);
  `_merge_legacy_cid_caches` gated on `ctx.brain_symbol == "CL"` (C8).
- `src/live_execution/adapters/ibkr_execution.py:73` — resolve_contract exchange from
  registry (1-liner).
- Tests: new `tests/test_symbol_data_paths.py` (or tester's naming) implementing the
  audit §6 30-item list with C3/C8 additions; mechanical fixture updates ONLY in
  test_data_manager.py / test_rollover.py.

## Hard Constraints
1. CL configs: byte-identical paths, filenames, contracts, and IBKR requests (test 16
   is THE regression pin; C1 expression fidelity).
2. No silent defaults anywhere new (manager fetch methods: required symbol; DataManager:
   required symbol; unknown symbol raises via get_instrument).
3. MCL: brain=CL continuous subscription; shares CL data files (D3); cid-merge still
   applies to MCL (C8 test).
4. Missing non-CL seed → existing hard FileNotFoundError with derived path in message.
5. Scope guards: NO tick/order pricing (T3 — close_cl_position NYMEX stays), NO macro/vol
   (T4), NO watchdog/rollover timing or _EXPIRY_BUFFER_DAYS source (T5), NO fleet_runner.
6. Ignore the unrelated untracked macro-pctile files (other workstream).

## Verification
- Full fast suite green (baseline 924 + new tests; census-corrected fixture churn only).
- C7 (BLOCKING): HS14B ledger parity gate re-run (`setup --disable-trailing`, 2200/336)
  → PARITY: PASS required before commit.
