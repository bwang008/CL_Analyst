# Audit — T2 Data-path + contract symbol propagation
**Ticket:** `t2-symbol-data-paths_07042026_1815`
**Auditor:** Ticket-Auditor | **Date:** 2026-07-04 | **Branch:** `development` @ `fe1ce5e` (T1 instrument metadata merged)
**Source blueprint:** `.agents/collab/tickets/multi-symbol-live-gaps_07042026_1520/blueprint.md` (items B1, B2, B3; arch items 4, 5, 9; T2 sketch)
**Consumes:** T1 deliverables (`src/live_execution/instrument_context.py` — `InstrumentContext`/`resolve_instrument_context`; extended `INSTRUMENT_REGISTRY` with `exchange`, `active_months`, `roll_buffer_days`, `micro_of`, `bars_per_day_*`)

---

## 1. Severity & regression status

- **Severity: MEDIUM/HIGH** (ticket-auditor scale — multi-file structural change: 1 new pure function + signature changes in `ibkr_client.py`, adapter constructor change, DataManager constructor change, path derivation rewiring in `live_trader.py`/`cli.py`, mechanical test churn in 3 test files). Fixes blueprint **B1** (brain subscription CL-fallback), **B2** (historical fetch builds CL unconditionally), **B3** (shared CL-named data artifacts incl. module-global roll metadata).
- **Regression: NO.** All three gaps are original design shape ("the live engine is CL-shaped at every layer below the symbol string"), not recent breakage. Verified: the fleet-runner merge (`eb6e9d8`) added only `fleet_runner.py` (launches `cli --config` subprocesses, no data-path logic); T1 (`fe1ce5e`) honored its scope guards — `cli.py:167-172` seed/cache CL defaults, `ibkr_data_feed.py` routing, and `data_manager.py` are byte-unchanged by T1. `git log -n 5` on each target file shows no recent commit touching the affected logic.
- **New hazard found beyond the blueprint (B3-adjacent, fleet-certain):** `cli.py:256` calls `_merge_legacy_cid_caches(resolved_cache_path)` for ANY instance with `client_id != 1`. The legacy per-cid caches (`warm_start_cache_cid*.parquet`) are CL bars by definition; for a non-CL instance this merges **CL bars into the new symbol's cache** — exactly the silent misdata T2 exists to kill. The fleet runner requires unique client_ids, so every fleet instance takes this branch. Must be gated to CL (see §5.6).

## 2. Ticket/blueprint claims verified against HEAD `fe1ce5e` (line shifts from T1 noted)

| Claim | Verdict |
|---|---|
| `ibkr_data_feed.py:91-99` (sync) / `:133-141` (async): continuous branch `MCL → build_mcl_contract else build_cl_contract`; front-month branch hardcodes `exchange="NYMEX"` | **CONFIRMED** — sync `:91-99` (continuous `:92-96`, front-month `:98-99`), async `:133-141`. Also `:86-90`/`:128-132` VIX/OVX (CBOE) + DX (NYBOT) index handling to preserve, and `:159-164` `fetch_daily_close_async` index-only (T4, untouched). |
| `ibkr_client.py`: `fetch_historical_bars` `:279-283`, `fetch_historical_bars_by_duration` `:741-745`, async `:789-793` all call `build_cl_contract()` unconditionally, no symbol param | **CONFIRMED** — exact lines (`build_cl_contract(` at `:279`, `:741`, `:789`). None of the three takes a symbol. Also confirmed: module-level convenience `fetch_historical_bars` `:1294-1315` (CL helper). |
| DataManager uses those for backfill | **CONFIRMED with correction** — DataManager calls only the **`_by_duration` sync** variant: `data_manager.py:518` (`_backfill`), `:695` (`_compute_roll_ratio`), `:1001` (`_fetch_ibkr_range`). The `days_back` variant (`fetch_historical_bars`) has **zero production callers** (only the adapter pass-through `:31` and the module convenience `:1309`); it is parameterized for interface consistency only. |
| Reconnect gap backfill call sites `live_trader.py:2391/:2454` (blueprint numbering) | **CONFIRMED, shifted +2** — now `:2393` (5m) and `:2456` (1h), inside `_backfill_reconnect_gap_async`. Both `continuous=True`, no symbol. |
| `get_front_month_contract` hardcodes NYMEX | **CONFIRMED** — `ibkr_client.py:625` (sync search `Future(symbol, exchange="NYMEX")`) and `:675` (async). `_EXPIRY_BUFFER_DAYS = 6` at `:601` (buffer stays 6 in T2 — `roll_buffer_days`/`active_months` consumption is T5, see scope guards). |
| `live_trader.py:329-381` DataManager construction | **CONFIRMED, shifted +2 from gap blueprint** — 5m manager `:331-338` (ledger literal `cl_continuous_master.parquet` at `:334`), 1h block `:340-383`: cache literal `warm_start_cache_1h.parquet` `:346`, `live_config.seed_path_1h` override `:348-355`, 1h seed default `CL_raw_1h.parquet` `:357`, hard FileNotFoundError (cache AND seed missing) `:363-374` (message text is CL-specific: "CL_HourSet_08.parquet"), 1h ledger literal `cl_continuous_master_1h.parquet` `:379`. Module defaults `_DEFAULT_SEED_PATH`/`_DEFAULT_CACHE_PATH` at `:139-142`; `LiveTrader.__init__` param defaults at `:198-199`. |
| `data_manager.py:52-59` CL defaults + module-global roll metadata | **CONFIRMED** — `_DEFAULT_SEED_PATH` (`raw/cl-5m_bk.csv` via `get_data_path`) `:52`, `_DEFAULT_CACHE_PATH` (`processed/warm_start_cache.parquet`) `:53`, `_DEFAULT_MASTER_LEDGER_PATH` `:54-56`, `_ROLL_METADATA_PATH` (`processed/.roll_metadata.json`) `:57-59`. The roll-metadata global is read/written by BOTH the 5m and 1h managers of one process (shared file, same `front_month_id`) — per-symbol naming must preserve that intra-symbol sharing. Seed hard-fail (keep verbatim trigger) at `:177-192`; seed-missing messages in `_seed_from_csv` `:359-368` and `_load_full_seed` `:922-931` name "CL seed CSV (cl-5m_bk.csv)" — text becomes symbol-generic. |
| `cli.py:167-172` seed/cache CL defaults; `:229-231` shared-cache comment | **CONFIRMED** — `:167-172` unshifted (T1 inserted at `:219-230`, after). The "cache is SHARED — all strategies receive the same CL continuous bars" comment + `resolved_cache_path = args.cache_path` now at `:238-243`. Legacy cid-cache merge call at `:256` (see §1 hazard). T1's `resolve_instrument_context` already runs at `:223-230`, BEFORE the factories at `:287-290` — T2 hooks in right there. |
| MCL Two-Stream constraint (3): "brain subscribes CL continuous" | **CONTRADICTS CURRENT CODE — decision required (§7 Q1).** Today `live_trader._subscribe` (`:2044-2049`, 1h `:2055-2060`) and `_deferred_resubscribe` (`:2316-2321`, `:2327-2332`) pass `symbol=self._execution_symbol` with `continuous=True`, and the adapter maps `MCL → build_mcl_contract(continuous=True)` — an MCL config's brain stream is **MCL-continuous today**, not CL-continuous (T1 audit §2 documented the same). The ticket's constraint (3) as written is therefore a deliberate behavior CHANGE for MCL configs, not a preservation. Recommended: implement constraint (3) via `ctx.brain_symbol` (see §5.5) — zero MCL configs are shipped (all 20 configs in `configs/strategies/` carry `execution_symbol: "CL"`, grep-verified), the module docstring/design intent says "Brain=CL, Hands=CL or MCL", and it makes MCL data paths coherently shared with CL (below). |

## 3. Full call graph (every futures-contract construction and data-path derivation)

### 3a. Contract construction sites
| # | Site | Contract built | Symbol source | Exchange source | T2 action |
|---|---|---|---|---|---|
| 1 | `ibkr_client.py:25-52` `build_cl_contract` | ContFuture/Future CL | hardcoded | param default NYMEX | becomes thin wrapper → `build_future_contract("CL", ...)` |
| 2 | `ibkr_client.py:55-85` `build_mcl_contract` | ContFuture/Future MCL | hardcoded | param default NYMEX | thin wrapper → `build_future_contract("MCL", ...)` |
| 3 | `ibkr_client.py:279` `fetch_historical_bars` | CL continuous/month | hardcoded via (1) | — | takes required kw `symbol`; builds via `build_future_contract` |
| 4 | `ibkr_client.py:741` `fetch_historical_bars_by_duration` | same | same | — | same |
| 5 | `ibkr_client.py:789` `fetch_historical_bars_by_duration_async` | same | same | — | same |
| 6 | `ibkr_client.py:625` / `:675` `get_front_month_contract(_async)` search `Future(symbol, exchange="NYMEX")` | generic Future search | caller param (already symbol-aware) | **hardcoded NYMEX** | `exchange = get_instrument(symbol).exchange` |
| 7 | `ibkr_data_feed.py:92-99` `subscribe_live_bars` (sync): continuous branch (MCL/CL fallback) + front-month `Future(..., exchange="NYMEX")` | ContFuture or Future | per-call `symbol` param | hardcoded | continuous → `build_future_contract(symbol, continuous=True)`; front-month exchange from registry; VIX/OVX/DX branches preserved verbatim |
| 8 | `ibkr_data_feed.py:134-141` async twin | same | same | same | same |
| 9 | `ibkr_client.py:1294-1315` module convenience `fetch_historical_bars` | CL via (3) | hardcoded doc'd CL | — | keep CL-bound; pass `symbol="CL"` explicitly |
| 10 | `scripts/download_ibkr_history.py:45,95` | ContFuture CL via (1) | CL by design (CL download script) | — | untouched (wrapper keeps it working) |
| 11 | `ibkr_execution.py:73` `resolve_contract`: `Future(symbol, localSymbol, exchange="NYMEX")` | execution front-month | per-call symbol | **hardcoded NYMEX** | recommended 1-line inclusion (§7 Q2) — else ES resolves data but can never trade (B5 remainder) |
| 12 | `ibkr_client.py:460,501` `close_cl_position(_market)` inject `pos.contract.exchange = "NYMEX"` | position contract mutation | position filter | hardcoded | **OUT of T2** (order/exit path — B5 remainder, route with T3; noted for manager) |
| 13 | Dead imports of (1): `data_manager.py:856` (inside `_update_training_ledger`, unused), `live_trader.py:2373` (inside `_backfill_reconnect_gap_async`, unused — `ib_bars_to_dataframe` also unused), `ibkr_execution.py:3` (unused) | — | — | — | remove (no behavior) |
| — | Tests importing builders | **none** (grep-verified) | | | no wrapper dependency from tests |

Consumers of the subscription/front-month plumbing (symbol already flows, routing is what's broken):
- Brain subscribe: `live_trader.py:2044-2049` (5m), `:2055-2060` (1h) in `_subscribe`; async re-subscribe `:2316-2321`, `:2327-2332` in `_deferred_resubscribe`; the sync reconnect path `_resubscribe_and_backfill` (`:2505-2547`) reuses `_subscribe`/`_subscribe_front_month`, so fixing those two + the async twin covers ALL reconnect/resubscribe paths.
- Hands (front-month) subscribe: `_subscribe_front_month` `:2065-2078` (`continuous=False`), async `:2341-2347` — stays `execution_symbol`.
- Front-month resolution: startup `:628-632`, rollover `_check_contract_rollover` `:2111-2115` — both already pass `symbol=self._execution_symbol` (correct: hands = execution symbol); only the NYMEX inside the manager (site 6) is wrong.
- Exec contract: startup `:650`, rollover re-cache `:2199` → `exec_client.resolve_contract(execution_symbol)` (site 11).
- Rollover force-close `:2138-2167` uses `exec_client` position/cancel/close by symbol (site 12 hazard lives below it in `ibkr_client`).

### 3b. Data-path derivation sites (current CL-named artifacts)
| # | Site | Artifact | T2 action |
|---|---|---|---|
| 1 | `data_manager.py:52` `_DEFAULT_SEED_PATH` = `raw/cl-5m_bk.csv` | 5m seed | defaults become symbol-derived (constructor `symbol` req'd, §5.3) |
| 2 | `data_manager.py:53` `_DEFAULT_CACHE_PATH` = `processed/warm_start_cache.parquet` | 5m cache | same |
| 3 | `data_manager.py:54-56` `_DEFAULT_MASTER_LEDGER_PATH` = `processed/cl_continuous_master.parquet` | 5m ledger | same |
| 4 | `data_manager.py:57-59` `_ROLL_METADATA_PATH` module global (`processed/.roll_metadata.json`) — used at `:563`, `:574`, `:642`; shared 5m+1h | roll metadata | becomes instance attr from `symbol` (CL keeps legacy name), override param for tests |
| 5 | `live_trader.py:139-142` `_DEFAULT_SEED_PATH`/`_DEFAULT_CACHE_PATH` module constants; `__init__` defaults `:198-199` | 5m seed/cache | `__init__` defaults → `None` → derived from ctx.brain_symbol; constants deleted (only cli imported them) |
| 6 | `live_trader.py:334` 5m ledger literal | 5m ledger | derived |
| 7 | `live_trader.py:346` 1h cache literal; `:357` 1h seed default `CL_raw_1h.parquet`; `:348-355` `live_config.seed_path_1h` override (preserved verbatim); `:363-374` hard raise (message → symbol-aware); `:379` 1h ledger literal | 1h set | derived from ctx.brain_symbol |
| 8 | `cli.py:167-172` argparse defaults; `:243` `resolved_cache_path`; `:249-251` per-cid telemetry db (symbol-clean, untouched); `:256` legacy cid merge | CLI defaults | defaults → `None` → derived post-ctx; cid merge gated to CL |
| 9 | `data_manager.py:613-649` `_backup_cache_to_repo` → `data/cache_backups/warm_start_cache_{ts}_{reason}.parquet` + `roll_metadata_{ts}_{reason}.json` | backups | prefix backups with cache stem / symbol-qualified roll-metadata name (non-behavioral, timestamped forensic files; CL 5m backup name pattern preserved) — LOW priority, may defer |
| 10 | `scripts/livetest_engine.py:708-717` parity harness: passes explicit `seed_path` (real `cl-5m_bk.csv`) + `cache_path=warm_start_cache_sim.parquet` to `LiveTrader`; replaces `data_manager_1h` with `_MockDataManager` post-construction, but **`LiveTrader.__init__` still constructs the real 1h DataManager and runs the `:363-374` seed/cache existence check** using the `CL_raw_1h.parquet` default (HS14B config has NO `seed_path_1h` override — verified). | parity harness | needs NOTHING if CL derivation is byte-identical (constraint 1). The harness passes explicit 5m paths and its configs are CL → all derived values equal today's. Re-run of HS14B parity gate post-green is the proof (§6). |
| 11 | `tests/smoke_test_pipeline.py:249-264` cache-name→cadence regex (`warm_start_cache_(\d+)([mh])`) | ops smoke | new per-symbol names fall into existing WARN/skip branch (`:332-334`) — non-blocking; optional regex extension for `warm_start_cache_{SYM}[_1h]` |

Direct `DataManager(` constructions (repo-wide): `live_trader.py:331`, `:376` (production, the only two); tests: `tests/test_data_manager.py` ×13, `tests/test_rollover.py` fixture `:45` (+patches of `_ROLL_METADATA_PATH` global at `:62,75,86` ×3 → replaced by param), `tests/test_data_manager_ratio.py` (constructs then overrides attrs). Mechanical churn, enumerated for the TDD tester.

## 4. Key design decisions (with justification)

**D1 — How the symbol reaches the data feed: bound at adapter construction (`instrument_context`), NOT per-call params on fetch methods.**
`IBKRDataFeedClient.__init__` gains required kwarg `instrument_context: InstrumentContext`; its `fetch_historical_bars*` delegations pass `symbol=self._instrument_context.brain_symbol` to the manager. Justification: (a) the abstract `DataFeedClient` interface (`interfaces/data_feed_interface.py:19-52`) and `SimulatedDataFeed` stay **byte-unchanged** — the parity harness, all sim tests, and DataManager/live_trader fetch call sites (5 sites) need no edits; (b) all historical fetches are brain-stream continuous fetches today (verified: every caller passes `continuous=True`), so one binding is semantically exact; (c) a `symbol="CL"` per-call default would re-create the silent-CL trap the ticket kills, while a required per-call param would force churn through the interface + sim adapter + 5 call sites for zero information gain. `DataFeedFactory.create` passes `**kwargs` through (`factories.py:6-9`) — cli adds `instrument_context=ctx` with no factory change. The manager-level methods take **required keyword-only `symbol`** (no default) so nothing below the adapter can silently assume CL.

**D2 — How the symbol reaches DataManager: required keyword-only `symbol: str` (validated via `get_instrument`, raises unknown) + all path defaults derived from ONE pure function.**
New `derive_data_paths(symbol) -> DataPaths` (frozen dataclass of the 7 paths; lives in `data_manager.py`, imports only `src.data_paths` + `instrument_master`) is the **single naming authority** consumed by DataManager defaults, `live_trader.py` DataManager construction, and `cli.py` default resolution. `roll_metadata_path` becomes an instance attribute (optional constructor override for tests; `None` → structural derivation from `symbol` — same pattern T1 used for `brain_symbol`, "structural derivation, not a config default"). The module-global `_ROLL_METADATA_PATH` dies. Rationale for requiring `symbol` instead of requiring every path: only 2 production construction sites; it also fixes a latent test-isolation bug (test DataManagers currently read the real `.roll_metadata.json` global on `initialize()`).

**D3 — Which symbol keys the data paths: `ctx.brain_symbol`.**
The cached/ledgered data IS the brain-stream continuous series. Keying by brain symbol makes MCL configs share CL's legacy files — coherent with constraint (3) (brain=CL) and with today's shared-cache design. For every outright symbol brain==execution, so this is only visible for micros. (If Q1 in §7 is answered "keep MCL-continuous brain", paths must switch to execution_symbol to avoid MCL bars in CL files — the two decisions are COUPLED; do not mix.)

**D4 — Per-symbol file naming (CL legacy exception minimized).** `{sym}`=lower, `{SYM}`=upper; roots via existing `get_data_path`/`get_data_root` exactly as today (preserves CL_DATA_ROOT-primary/repo-local-fallback semantics byte-for-byte):

| Artifact | Generic pattern | CL value (byte-identical to today) | ES example |
|---|---|---|---|
| 5m seed | `data/raw/{sym}-5m_bk.csv` (via `get_data_path`) | `raw/cl-5m_bk.csv` ✓ pattern | `raw/es-5m_bk.csv` |
| 5m cache | `processed/warm_start_cache_{SYM}.parquet` | **exception:** `warm_start_cache.parquet` | `warm_start_cache_ES.parquet` |
| 5m ledger | `processed/{sym}_continuous_master.parquet` | `cl_continuous_master.parquet` ✓ pattern | `es_continuous_master.parquet` |
| 1h seed default | `processed/{SYM}_raw_1h.parquet` | `CL_raw_1h.parquet` ✓ pattern | `ES_raw_1h.parquet` |
| 1h cache | `processed/warm_start_cache_{SYM}_1h.parquet` | **exception:** `warm_start_cache_1h.parquet` | `warm_start_cache_ES_1h.parquet` |
| 1h ledger | `processed/{sym}_continuous_master_1h.parquet` | `cl_continuous_master_1h.parquet` ✓ pattern | `es_continuous_master_1h.parquet` |
| roll metadata (shared 5m+1h per symbol) | `processed/.roll_metadata_{SYM}.json` | **exception:** `.roll_metadata.json` | `.roll_metadata_ES.json` |

Only 3 of 7 artifacts need a CL exception branch; the rest follow the generic pattern already. Missing seed for any symbol → the existing `data_manager.py:177-192` hard FileNotFoundError fires with the derived path in the message (kept verbatim in trigger; text made symbol-generic). `live_config.seed_path_1h` override preserved exactly (`live_trader.py:348-355`).

**D5 — Deprecation of `build_cl_contract`/`build_mcl_contract`: keep as thin wrappers, remove dead imports, defer deletion to T6.**
Caller census (§3a): after T2 rewires sites 3-8, the only remaining users are `scripts/download_ibkr_history.py` (genuinely CL-specific) and the module convenience (site 9). Wrappers = `return build_future_contract("CL", continuous=..., contract_month=..., exchange=exchange, currency=currency)` — field-identical output, zero risk, smallest diff; dead imports at `data_manager.py:856`, `live_trader.py:2373`, `ibkr_execution.py:3` removed. Full deletion is a T6 cosmetic-sweep item.

**D6 — New builder signature (in `ibkr_client.py`):**
```python
def build_future_contract(
    symbol: str,
    *,
    continuous: bool = True,
    contract_month: Optional[str] = None,
    exchange: Optional[str] = None,   # None -> get_instrument(symbol).exchange
    currency: str = "USD",
) -> Contract
```
- unknown symbol → `ValueError: Unknown instrument symbol: {symbol}` (existing `get_instrument` raise — satisfies "unknown symbol must RAISE");
- `continuous=False` without `contract_month` → `ValueError("contract_month is required when continuous=False (format: YYYYMM).")` (verbatim legacy message);
- `continuous=True` → `ContFuture(symbol, exchange, currency, includeExpired=True)` (legacy parity).
`ibkr_client` imports `src.core.instrument_master` (pure stdlib leaf — no cycle; `instrument_master` imports nothing from `live_execution`).

## 5. Proposed implementation (localized; file-by-file)

### 5.1 `src/live_execution/ibkr_client.py`
- Add `build_future_contract` (D6). Rewrite `build_cl_contract`/`build_mcl_contract` as delegating wrappers (D5).
- `fetch_historical_bars`, `fetch_historical_bars_by_duration`, `fetch_historical_bars_by_duration_async`: add **required keyword-only `symbol: str`**; replace `build_cl_contract(...)` with `build_future_contract(symbol, continuous=continuous, contract_month=contract_month)`. Docstrings de-CL'd.
- `get_front_month_contract(_async)`: `search = Future(symbol=symbol, exchange=get_instrument(symbol).exchange, currency="USD")`. `_EXPIRY_BUFFER_DAYS` stays 6 for ALL symbols (T5 owns `roll_buffer_days`/`active_months` consumption — even though registry CL value is 6, switching sources now would silently change ES to 8; not this ticket).
- Module convenience `fetch_historical_bars` (`:1294`): pass `symbol="CL"` explicitly (stays a documented CL helper).
- NOT touched: `_CL_TICK_SIZE`/pricing (T3), `close_cl_position*` incl. NYMEX injection (§7 Q2b), `get_account_summary` keys (m2/T6).

### 5.2 `src/live_execution/adapters/ibkr_data_feed.py`
- `__init__(self, host, port, client_id, fallback_ports=None, *, instrument_context: InstrumentContext)` — required, no default (no silent CL).
- `fetch_historical_bars*` ×3: forward `symbol=self._instrument_context.brain_symbol`.
- `subscribe_live_bars(_async)`: keep VIX/OVX and DX Index branches **verbatim** (incl. qualification-failure tolerance); `elif continuous:` → `contract = build_future_contract(symbol, continuous=True)` (fallback dies; unknown symbol raises); front-month `else:` → `contract = Future(symbol=symbol, localSymbol=local_sym, exchange=get_instrument(symbol).exchange)`.
- `get_front_month_contract`: drop the `= "CL"` default (all callers pass explicitly — verified live_trader `:630`, `:2113`; adapter internal `:98/:140`); mirror in the abstract interface (`data_feed_interface.py:83`). Everything else in the interface unchanged.

### 5.3 `src/live_execution/data_manager.py`
- Add `DataPaths` frozen dataclass + `derive_data_paths(symbol: str) -> DataPaths` implementing the D4 table (validates symbol via `get_instrument`; uses `get_data_path`/`get_data_root` exactly as the current literals do).
- `DataManager.__init__`: add required keyword-only `symbol: str`; `seed_path`/`cache_path`/`master_ledger_path` defaults become `None` → filled from `derive_data_paths(symbol)`; new optional `roll_metadata_path: Optional[str] = None` → `derive_data_paths(symbol).roll_metadata` when None. Store as `self.roll_metadata_path`; `_load_roll_metadata`/`_save_roll_metadata`/`_backup_cache_to_repo` read the instance attr; delete module globals `_DEFAULT_*`/`_ROLL_METADATA_PATH`.
- Seed-missing messages (`:181-192`, `:359-368`, `:922-931`): symbol-generic text — e.g. `f"Seed file not found for {self.symbol}: {self.seed_path}\n..."` — same exception types, same trigger conditions (hard-fail rule kept verbatim).
- `_update_training_ledger` dead import (`:855-858`) removed.
- Backups (`_backup_cache_to_repo`): cache backup name gains the cache stem (`f"{self.cache_path.stem}_{ts}_{reason}.parquet"` — CL 5m stem is `warm_start_cache` → legacy name preserved; CL 1h backups gain the already-distinct `_1h` stem — forensic files, not consumed programmatically) and roll-metadata backup mirrors `self.roll_metadata_path` stem. OPTIONAL — may be deferred if the impact reviewer prefers zero delta here.

### 5.4 `src/live_execution/live_trader.py`
- `__init__` signature: `seed_path: Optional[str] = None, cache_path: Optional[str] = None`; after `resolve_instrument_context` (`:279`), `paths = derive_data_paths(self._instrument_context.brain_symbol)`; `seed_path = seed_path or str(paths.seed_5m)`, same for cache. Delete module constants `_DEFAULT_SEED_PATH`/`_DEFAULT_CACHE_PATH` (`:139-142`).
- 5m DataManager (`:331-338`): `symbol=brain_symbol`, `master_ledger_path=str(paths.ledger_5m)`, `roll_metadata_path=str(paths.roll_metadata)`.
- 1h block (`:340-383`): `cache_path_1h = str(paths.cache_1h)`; seed default `str(paths.seed_1h)`; `live_config.seed_path_1h` override logic untouched; FileNotFoundError message becomes symbol-aware (`f"Neither 1H cache nor seed file found for {brain_symbol}!..."`, keep cache/seed/CL_DATA_ROOT lines); 1h DataManager gains `symbol=brain_symbol`, `master_ledger_path=str(paths.ledger_1h)`, `roll_metadata_path=str(paths.roll_metadata)` (same file as 5m manager — preserves today's shared-metadata behavior per symbol).
- Brain subscriptions: `_subscribe` (`:2045`, `:2056`) and `_deferred_resubscribe` (`:2317`, `:2328`) pass `symbol=self._instrument_context.brain_symbol`. Front-month sites (`:630`, `:2072`, `:2113`, `:2342`) and exec sites (`:650`, `:2199`) keep `self._execution_symbol`. (`_resubscribe_and_backfill` reuses `_subscribe*` — covered.)
- NOT touched: `_needs_macro`/macro fetches `:615-699` (T4), `_check_stale_bars`/`_get_market_status` (T5), 4320-bar validation `:1911-1923` (T5/M5 — unchanged even for non-CL; a short ES seed raises the existing actionable error), rollover force-close logic (T5), pricing `:1070/:1632-1656` (T3).

### 5.5 MCL coherence (pending §7 Q1)
With D3 + 5.4: MCL config → brain streams subscribe **CL continuous**, historical fetch symbol **CL**, data files **CL legacy set** (legitimately shared with CL instances — same series), hands stream + orders **MCL front-month**. This implements ticket constraint (3) exactly; it is a deliberate change from today's MCL-continuous brain (zero shipped MCL configs; flagged for authorization).

### 5.6 `src/live_execution/cli.py`
- `--seed-path`/`--cache-path`: `default=None`, help "(default: derived from the config's execution symbol; CL keeps legacy names)". Drop the `_DEFAULT_SEED_PATH`/`_DEFAULT_CACHE_PATH` imports (`:108-109`).
- After ctx resolution (`:225`): `paths = derive_data_paths(ctx.brain_symbol)`; `resolved_seed_path = args.seed_path or str(paths.seed_5m)`; `resolved_cache_path = args.cache_path or str(paths.cache_5m)`; pass to `LiveTrader`. For CL these equal today's baked defaults byte-for-byte (same `get_data_path`/`get_data_root` calls).
- **Gate the legacy cid merge**: `if ctx.brain_symbol == "CL": _merge_legacy_cid_caches(resolved_cache_path)` — legacy `warm_start_cache_cid*.parquet` files are CL bars; merging them into a non-CL cache is silent misdata (§1).
- `DataFeedFactory.create(..., instrument_context=ctx)`. Exec factory unchanged (unless §7 Q2a approved — still no constructor change needed; `resolve_contract` reads the registry per call).
- Update the shared-cache comment (`:238-243`) to "shared per brain symbol".

### 5.7 Explicitly OUT of T2 (scope guards honored)
Tick/order pricing incl. `_CL_TICK_SIZE`, marketable-limit buffers, TP/SL rounding (T3); macro/vol fetching, `MacroFeatureEngine` instrument, `fetch_daily_close_async` index map (T4); watchdog/market-status/rollover timing, `_EXPIRY_BUFFER_DAYS`→`roll_buffer_days`, `active_months` filtering, `_SEED_LOOKBACK_DAYS`/4320-bar provisioning, `_ROLL_PRICE_TOLERANCE` (T5); `fleet_runner.py` (verified: launches `cli --config` only, no path logic); config generator (T6); `close_cl_position*` NYMEX injection + `get_account_summary` `cl_*` keys (B5 remainder/m2 — route with T3/T6, see §7 Q2b).

## 6. TDD test list

Contract building (`tests/test_build_future_contract.py` or extend an ibkr test module — pure, no IB connection):
1. `test_build_future_contract_cl_continuous_parity` — field-by-field equality with legacy `build_cl_contract(continuous=True)` output (ContFuture: symbol/exchange/currency/includeExpired). Same for `contract_month="202609"` Future.
2. `test_build_future_contract_mcl_parity` — same vs `build_mcl_contract`.
3. `test_build_future_contract_registry_exchange` — ES→CME, ZC→CBOT, GC→COMEX, NG→NYMEX (ContFuture and Future forms).
4. `test_build_future_contract_unknown_symbol_raises` — `ValueError` mentioning the symbol.
5. `test_build_future_contract_month_required` — `continuous=False` without month raises legacy message.
6. `test_legacy_builders_are_wrappers` — `build_cl_contract`/`build_mcl_contract` still importable and produce identical contracts (protects `scripts/download_ibkr_history.py`).

Manager symbol propagation (mock `self.ib`):
7. `test_fetch_historical_bars_requires_symbol` — calling without `symbol` → `TypeError` (all three methods).
8. `test_fetch_historical_bars_builds_requested_symbol` — `symbol="ES"` → `reqHistoricalData` receives ContFuture(ES, CME); `symbol="CL"` → identical contract to today's (regression pin).
9. `test_get_front_month_exchange_from_registry` — mock `reqContractDetails`; assert search contract exchange CME for ES, NYMEX for CL/MCL; buffer still 6 days (pin — T5 changes it deliberately).

Adapter routing (`tests/test_ibkr_data_feed_routing.py`, manager mocked):
10. `test_adapter_requires_instrument_context` — constructing `IBKRDataFeedClient` without it raises `TypeError`.
11. `test_continuous_subscription_uses_requested_symbol` — `subscribe_live_bars("ES", continuous=True)` → manager receives ContFuture(ES, CME); **the CL fallback is dead**: `subscribe_live_bars("ZZ", continuous=True)` raises ValueError (no CL contract built).
12. `test_cl_and_mcl_subscription_unchanged` — CL → ContFuture CL/NYMEX; MCL → ContFuture MCL/NYMEX (byte parity with legacy builders).
13. `test_index_branches_preserved` — VIX/OVX → `Index(sym,"CBOE","USD")` with `what_to_show="TRADES"`; DX → `Index("DX","NYBOT","USD")`; qualification-failure tolerance intact.
14. `test_front_month_subscription_exchange` — `continuous=False`, symbol ES → `Future(..., exchange="CME")`; CL → NYMEX.
15. `test_fetch_delegation_uses_brain_symbol` — adapter built with ES ctx: all three fetch methods forward `symbol="ES"`; CL ctx → `"CL"`; MCL ctx → `"CL"` (brain).

Path derivation (`tests/test_data_paths_per_symbol.py`):
16. `test_derive_data_paths_cl_byte_identical` — all 7 CL paths equal the legacy literals (constructed via the same `get_data_path`/`get_data_root` expressions currently in code) — THE regression pin for hard constraint (1).
17. `test_derive_data_paths_es_table` — ES paths match D4 exactly (all 7).
18. `test_derive_data_paths_unknown_raises`.
19. `test_data_manager_requires_symbol` — `DataManager()` → TypeError; `DataManager(symbol="XX", ...)` → ValueError.
20. `test_data_manager_roll_metadata_per_symbol` — CL → `.roll_metadata.json`; ES → `.roll_metadata_ES.json`; explicit `roll_metadata_path` override wins; save/load round-trip via instance attr (replaces the 3 module-global patches in `tests/test_rollover.py`).
21. `test_missing_non_cl_seed_raises_actionable` — ES DataManager, no cache, no seed → FileNotFoundError whose message contains the derived ES seed path and CL_DATA_ROOT hint.

LiveTrader integration (mocked clients, tmp data root):
22. `test_cl_config_datamanager_paths_unchanged` — HS14B-style config (1h) → both DataManagers constructed with exactly the legacy 6 paths + legacy roll metadata (hard constraint 1).
23. `test_es_config_datamanager_paths` — ES-style config → ES-derived paths everywhere; `live_config.seed_path_1h` override still wins (absolute + relative-to-data-root forms).
24. `test_es_missing_1h_seed_raises` — message names ES paths.
25. `test_brain_stream_symbol` — CL config: `_subscribe` calls `subscribe_live_bars(symbol="CL", continuous=True)` ×2 and front-month `symbol="CL", continuous=False`; MCL config: brain `"CL"`, front-month `"MCL"` (constraint 3); same assertions on `_deferred_resubscribe` (async twin).
26. `test_reconnect_backfill_no_symbol_regression` — `_backfill_reconnect_gap_async` still calls the adapter's `fetch_historical_bars_by_duration_async` (adapter-bound symbol; extend existing reconnection tests).

CLI (`tests/test_cli_paths.py`, factories mocked):
27. `test_cli_default_paths_cl_byte_identical` — CL config, no `--seed-path`/`--cache-path` → LiveTrader receives legacy strings.
28. `test_cli_explicit_paths_win` — `--seed-path X --cache-path Y` respected for any symbol.
29. `test_cli_es_derived_paths` + factory receives `instrument_context=ctx`.
30. `test_cli_cid_merge_gated_to_cl` — mock `_merge_legacy_cid_caches`: called for CL with client_id≠1, NOT called for ES with client_id≠1.

If §7 Q2a approved: 31. `test_resolve_contract_exchange_from_registry` — ES → `Future(..., exchange="CME")`; existing `tests/test_ibkr_adapters.py:86` CL assertion still green.
Post-green (manager, not coder): **HS14B ledger parity gate re-run** (`setup --disable-trailing`, 2200 warmup + 336 replay) — LiveTrader.__init__ and the data path spine changed; any non-PASS is a T2 regression. (Same convention as T1 C3.)

## 7. Open questions requiring human/manager authorization

1. **MCL brain-stream semantics (constraint 3 vs current code).** Ticket says "MCL Two-Stream preserved: brain subscribes CL continuous" — but today's code subscribes **MCL-continuous** for MCL configs (adapter `:93-96` + live_trader passing execution_symbol). Implementing the constraint = deliberate behavior change for MCL configs (none shipped; T1 intentionally did not change it). Recommended: implement as written via `ctx.brain_symbol` (D3/5.5 — also makes MCL share CL data files coherently). Need explicit ACK that this is the intended semantics, since "preserved" in the ticket text is factually inaccurate.
2. **B5 remainders adjacent to T2 scope:**
   a. `ibkr_execution.py:73` `resolve_contract` NYMEX hardcode — 1-line registry fix, same theme (contract construction), but the file is not in T2's enumerated scope. Without it a non-CL symbol fetches correct data but every order attempt fails (no-trade, loud — not silent misdata). Recommend including; request approval.
   b. `ibkr_client.py:460,501` `close_cl_position*` NYMEX injection — exit/order path; recommend routing with T3 (it's the dangerous stuck-position case from B5). NOT included in T2.
3. **DataManager constructor churn** — required `symbol` kwarg breaks ~16 test constructions across `test_data_manager.py`/`test_rollover.py`/`test_data_manager_ratio.py` (mechanical `symbol="CL"` additions; also replaces 3 module-global patches with the new param). Approve the churn (alternative — defaulting `symbol="CL"` — violates the no-silent-defaults house rule).
4. **Deferred niceties** (state if wanted in T2, else T6): per-symbol backup filenames in `_backup_cache_to_repo` (5.3, optional); `smoke_test_pipeline.py` cadence-regex extension for per-symbol cache names (currently WARN/skip — non-blocking); `live_config.seed_path_5m`/`cache_path` config overrides suggested by blueprint arch item 5 (CLI flags already cover; recommend defer).

## 8. Deviations from the gap-blueprint T2 sketch (with justification)

1. **`fetch_historical_bars*` do NOT gain a symbol param at the adapter/interface level** (blueprint B2 said "DataManager passes its instrument's symbol+exchange"). Instead the symbol binds at adapter construction (D1) and the manager methods require it. Zero interface/sim/DataManager churn, no per-call CL default anywhere, semantics identical (all fetches are brain-continuous).
2. **Exchange is resolved inside `ibkr_client` from the registry per symbol** rather than threaded as a parameter from DataManager — fewer signatures, single source of truth, unknown symbol raises at the same depth.
3. **DataManager takes `symbol` (str), not an `Instrument` object** — it needs only naming + validation; keeps the module free of instrument coupling beyond one `get_instrument` call, and keeps test fixtures trivial.
4. **CL exception surface reduced to 3 of 7 artifacts** — blueprint's `legacy_paths` mapping idea in instrument metadata is unnecessary; the generic patterns already produce CL's names for 4 of 7 artifacts, so the exceptions live in `derive_data_paths` (one place), not in the registry.
5. **`_EXPIRY_BUFFER_DAYS` and month filtering untouched** (blueprint arch item 8 mentions them under front-month) — T5 owns roll semantics; T2 changing the buffer source would silently alter ES behavior.
6. **New fix not in blueprint:** legacy cid-cache merge gated to CL (§1) — fleet-certain CL-into-ES cache corruption vector discovered during the audit.
7. **`live_config.seed_path_5m`/`cache_path` overrides (blueprint item 5) deferred** — `--seed-path`/`--cache-path` CLI flags already provide explicit override; adding config keys is additive and better sequenced with T6's generator work.
