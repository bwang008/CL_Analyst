# Impact Review — Seedless 5m Live Stream (shallow IBKR bootstrap)
**Ticket:** `seedless-5m-live-stream_07052026_0546` | **Reviewer:** Ticket-Impact-Reviewer | 2026-07-05 PT
**Repo HEAD at review:** `f165b9d` (branch `development`)

## VERDICT: APPROVE (with binding conditions in §4)

The manager has already ruled on all five audit §6 open questions (ES01B key removal, same-commit
sequencing, ledger-skip, "5 D" duration, 6-pin in-ticket repair), so no further human authorization
is requested. Refactor Veto NOT triggered (§3).

---

## 0. HEAD discrepancy (immaterial, recorded)
The audit stamps HEAD `336d29f`; actual HEAD is `f165b9d` — one commit ahead. Verified by
`git show --stat f165b9d`: docs (`.agents/collab/tickets/*`, `READBEN.me`) + `data/predictions/*.csv`
only. Zero source/config/test drift — every audit line-number claim re-verified below holds at
`f165b9d`. The Coder should cite `f165b9d` (or later) as base.

## 1. Independent verification results (7 tasks)

### 1.1 The 6-failing-pins claim — CONFIRMED BY RUN
Ran `pytest tests/test_hourly_only_equity_session.py tests/test_instrument_context.py
tests/test_config_generator_symbols.py` at HEAD (conda `trader`, 131 collected):
**6 failed, 125 passed** — exactly the audit §3.3 enumeration, no more, no fewer:
- `test_hourly_only_equity_session.py::TestES01BFlagPatch::test_es01b_still_resolves_es_es_cme` — `'MES' == 'ES'` (:983)
- `test_hourly_only_equity_session.py::TestES01BFlagPatch::test_es01b_t6_sentinel_fields_unchanged` — `'MES' == 'ES'` (:995)
- `test_instrument_context.py::TestShippedConfigs::test_es01b_shipped_config_resolves_as_es` — `'MES' == 'ES'` (:295)
- `test_config_generator_symbols.py::TestES01BPatchedConfig::test_patched_fields_match_audit_table` — `'MES' == 'ES'` (:606)
- `test_config_generator_symbols.py::TestES01BPatchedConfig::test_resolves_as_es_es_cme` — `'MES' == 'ES'` (:624)
- `test_config_generator_symbols.py::TestES01BPatchedConfig::test_untouched_fields_pinned` — `2000 == 1010` (:650)

Root cause confirmed via `git show 336d29f`: that commit changed ONLY
`execution_symbol: "ES"→"MES"` and `client_id: 1010→2000` in the ES01B config (plus workflow doc +
fleet manifest) with zero test evolutions — the failures trace to 336d29f's config flip, nothing
else. `test_es01b_carries_enable_5m_stream_false` is among the 125 passing (must evolve, per plan).
The pre-existing-regression framing and the in-ticket repair sanction are sound.

### 1.2 bar_size gate — CONFIRMED
Full grep census of `rolling_df_5m|data_manager_5m|_last_bar_time_5m|_live_bars_5m` in
`live_trader.py` matches the audit map exactly. The ONLY deep-5m-history consumers are:
- `_on_new_bar(bar_time, self.rolling_df_5m, "5m")` at `:2922-2924` — gated `bar_size == "5m"`;
- `_warmup_inference_state` `elif self.rolling_df_5m` at `:2120-2121` — unreachable for 1h/2h/4h
  models (the `data_manager_1h.get_ratio_adjusted_df()` branch at `:2118-2119` takes precedence).

Every other touchpoint is last-bar or forward-only at HEAD: trailing extremes `:1155-1164`
(last bar, presence-selected); recovery `:1528-1569` (last-bar timestamp + last-bar extremes);
reconnect stitch `:2694-2710` (bounded, `index > _last_bar_time_5m`); 5m callback
append/telemetry/trailing `:2896-2924`; naked-position flatten `:4171-4172` (last close);
heartbeat `:3889-3892`; watchdog `:4037-4042` (last-bar age); front-month wiring `:760-763`,
`:2486-2487`; shutdown save `:932-936` (None-guarded). The 1h hard seed requirement (`:444-454`)
and the 4320-bar floor (`:2091-2108`) are untouched. The gate set `("1h","2h","4h")` matches the
T7 flag-validation set at `:343-348`; unknown bar_size → False → today's raise. CORRECT
discriminator.

### 1.3 Shallow bootstrap path — CONFIRMED
- The FileNotFoundError raises exactly at `data_manager.py:303-308`, the Step-1 else-branch of
  `initialize()` (`:292-308`) — the only Step-1 raise the design conditionally replaces.
- Post-T2 symbol correctness: `ibkr_data_feed.py:69-86` injects
  `symbol=self._instrument_context.brain_symbol` into
  `fetch_historical_bars_by_duration`; `ibkr_client.py:876-929` builds/qualifies the continuous
  contract and returns `ib_bars_to_dataframe` (`:1438-1464`) — DateTime as BOTH index and column,
  the exact shape `save_cache`/`_load_cache`/`append_bar` handle. Identical call signature to the
  existing `_backfill` fetch (`data_manager.py:634-640`) — zero adapter/client changes needed.
- Pacing: a single "5 D" request; `_request_historical_data` (`:492-544`) already carries
  retry/backoff/pacing detection; fleet `stagger_seconds: 60` separates instances. Trivially safe.
- `_drop_incomplete_bar` (`:572-591`): `pd.Timedelta("5 mins".replace("mins","min"))` is valid —
  applicable as proposed.
- Run-2 warm start: `initialize()` checks `cache_path.exists()` FIRST (`:272`) — a cache written by
  `save_cache()` on run 1 makes run 2 byte-identical to today's CL cache branch (then gap
  `_backfill`). Confirmed self-healing. `front_month_id` is set at start() Step 6 (`:758-767`)
  before `_warm_start` Step 8 (`:805`), so Step-5 `_save_roll_metadata` records the front month on
  the bootstrap run; `_detect_rollover` first run returns False (`:823-828`); `_compute_roll_ratio`
  needs only a "3 D" overlap (`:858-864`) — the 5 D window covers it from day one.

### 1.4 Ledger-skip — CONFIRMED (audit's catch is real and necessary)
- `initialize()` Step 4 calls `_update_training_ledger()` on EVERY run with a data client
  (`:337-338`); the ledger-absent branch (`:1031-1034`) calls `_load_full_seed()` which raises its
  own FileNotFoundError (`:1080-1089`). Without the gate, a Step-1-only fix would crash on run 1
  AND every cache-warm-started run 2+ (ledger still absent, seed still absent). The gate is
  required, not optional.
- Non-CL 5m ledger consumers: repo-wide grep for `continuous_master` finds ONLY CL literals —
  `worktree_startup.py:59`, `check_vol.py:5`, `scripts/generate_mock_shadow_log.py:56-63`,
  `scripts/backfill_shadow_log.py:77-79` (+ path-name-only assertions in
  `tests/test_symbol_data_paths.py:141,144,460,463` and the derivation itself). No consumer of
  `es_continuous_master.parquet` exists.
- CL untouched: CL's ledger exists → `:1018` branch (gate never evaluated); even ledger-absent,
  CL's seed exists → predicate `allow AND not seed.exists()` is False → `_load_full_seed()` as
  today. Ledger-present/seed-absent append path (`:1060-1073`) reads only `ledger.index.max()` +
  IBKR — unchanged. 5m models keep the raise (allow=False).

### 1.5 Reversion claims — CONFIRMED
- Synthetic vs shipped: T7's hourly-only machinery tests all build configs via
  `_es_hourly_only_cfg(tmp_path)` with an EXPLICIT `"enable_5m_stream": False`
  (`test_hourly_only_equity_session.py:438-450`) — key removal from the shipped ES01B config
  cannot touch them; the flag remains a valid opt-out. Stay green.
- Shipped-config pins are exactly the audit's 7 (3 files): `TestES01BFlagPatch` (3),
  `TestShippedConfigs::test_es01b_shipped_config_resolves_as_es` (1),
  `TestES01BPatchedConfig` (3). Repo-wide grep for `ES01B|enable_5m_stream` in tests/ finds one
  additional file — `test_session_watchdog_rollover.py` — but its two hits are DOCSTRING-only
  (T7 C7 clarification; assertion is the constant 15). No other pin exists.
- Reversion mechanics at HEAD: watchdog flag-read → `_last_bar_time_5m`/15-min (`:4037-4042`) with
  equity session gating via `_get_market_status` (`:4026-4028`) + `_session_open_anchor`
  (`:4053-4058`); trailing selects by frame presence (`:1155-1160`, existing
  `test_5m_frame_still_drives_when_present_pin`); heartbeat/telemetry refill (`:3889-3892`,
  `:2913-2917`); reconnect resubscribes 5m (`:2598-2607`). Default-true flag (`:339-342`) means key
  removal → full 5m construction (`:398-410`) with `allow_shallow_bootstrap=True` for `bar_size
  "1h"`. All automatic; no residual T7 machinery misfires for a 5m-enabled ES.

### 1.6 CL byte-identity — CONFIRMED
- Construction census (`DataManager(` repo-wide): 2 src sites (`live_trader.py:400` 5m,
  `:459` 1h), ~30 test constructions (all keyword-based), 3 `DataManager.__new__` ratio stubs
  (bypass `__init__`; they call only ratio methods, never the gated paths),
  `scripts/livetest_engine.py:230` uses its own `_MockDataManager`. A keyword-only
  `allow_shallow_bootstrap: bool = False` leaves every existing construction byte-identical.
  No subclasses of DataManager exist (grep).
- CL runtime: seed + cache present → Step-1 else-branch unreachable; ledger gate predicate False.
  Zero CL config edits. NOTED (not blocking): the delta is presence-conditional by design — a CL
  1h instance that lost BOTH its 5m seed and cache would now shallow-bootstrap (3-surface loud)
  instead of crashing. That is precisely the user's clarified ruling (live 5m everywhere; only
  historical purchases banned), and the alternative (a CL special-case) would be a silent fork.
- `bars_per_day` never read at runtime (grep: stored `:215`, no consumer) and
  `seed_lookback_days` used only by `_seed_from_csv` — audit's no-interaction claims hold.

### 1.7 Failure semantics — CONFIRMED loud-crash, no silent window
- Gateway down: `ensure_connected()` → `connect()` raises; `qualify_contract` raises ValueError on
  qualification failure (`ibkr_client.py:420-426`); `_request_historical_data` re-raises after
  `max_retries=5` with backoff (`:538-539`). Exceptions propagate `initialize()` → `_warm_start()`
  → `start()` fatal handler (`live_trader.py:854-863`): `log.exception` + Telegram `[FATAL]` +
  **re-raise** → process exits nonzero.
- No entitlement / empty: `ib_bars_to_dataframe([])` returns an EMPTY frame (`:1449-1450`) → the
  proposed RuntimeError fires (also covers the all-bars-incomplete edge after
  `_drop_incomplete_bar`). The raise precedes any `save_cache()`, so `save_cache`'s silent
  empty-skip (`:392-394`) is unreachable and `_warm_start`'s ≥1-bar contract (`:2022-2025`) is
  never reached with an empty frame. No silent empty-window path exists.
- Fleet handling: `fleet_runner` restarts dead children with capped exponential backoff
  (`RESTART_BACKOFF_BASE_SECONDS * 2^(n-1)`, capped; default max restarts), then gives up loudly
  ("Manual intervention required") and exits nonzero when all children are dead — sane crash-loop
  behavior for a persistent gateway outage.

## 2. Constraint evaluation (workflow rules)
- **Interface Rule:** triggered in the weak/additive sense — `DataManager.__init__` gains an
  optional keyword-only parameter (default False). Census-verified zero impact on all existing
  call sites; the byte-identical raise is preserved on every default path. Business justification
  is strong: the alternative (live_trader fetching + writing the cache itself) would duplicate
  DataManager's cache/dedup/atomic-write machinery and break the single-pipeline invariant the
  No-Silent-Bootstrap rule protects. APPROVED under the exception.
- **Base Class Rule:** DataManager is a core utility but has NO subclasses; changes are additive
  and default-off. APPROVED.
- **Refactor Veto:** NOT triggered — 2 coupled files in one subsystem (data_manager + its only
  consumer live_trader), an additive behavior extension, no component rewritten. Config edit is a
  1-key removal; test edits are sanctioned pin evolutions. The audit's §6 human-authorization
  items are all covered by explicit manager rulings (ES01B key removal, same-commit sequencing,
  ledger-skip, "5 D", 6-pin repair).

## 3. Blast radius summary
- `src/live_execution/data_manager.py` — additive param + gated else-branch + new private method +
  ledger-gate. Default path byte-identical (raise message byte-pinned).
- `src/live_execution/live_trader.py` — one constructor kwarg, one state flag, banner + Telegram
  stamp. CL fleet constructs identically (flag default true, allow only changes seedless behavior).
- `configs/strategies/ES01B_Sharpe_E03_07042026.json` — remove line 22 key (verified present at
  HEAD; MES/2000 at :16/:19 as audited). Fleet manifest has ES01B ENABLED (dry-run) — sequencing
  condition below is binding.
- Tests: 7 sanctioned pin evolutions (6 already red at HEAD) + new `test_shallow_5m_bootstrap.py`.
- Docs: 7 locations, guidance-only.
- NOT touched (verified no dependency): fleet_runner, backtest engine, generators, livetest
  harness, ibkr_client, adapters, cli.py, session_calendar, instrument_master.

## 4. Binding conditions of approval
1. **Same-commit sequencing (fleet-live):** the code (data_manager + live_trader) MUST land in the
   same commit as the ES01B key removal. Key removal alone crash-loops the ES child on next fleet
   restart (FileNotFoundError → backoff → cap exhausted).
2. **Byte-pins preserved:** the 5m-model/default-path FileNotFoundError message stays byte-identical
   (`data_manager.py:303-308`); `allow=True` + `data_client=None` + no seed/cache must still raise.
3. **Single request:** the bootstrap stays ONE non-chunked "5 D" fetch; empty/None → RuntimeError
   BEFORE any `save_cache()` (as designed).
4. **Test-name honesty:** `test_es01b_carries_enable_5m_stream_false` must be RENAMED as it evolves
   to the key-absent assertion (a name asserting "carries false" over an absent-key body is drift
   bait).
5. **Run-2 seedless ledger pin:** TDD item 7 must include the cache-PRESENT/seed-absent/ledger-absent
   construction explicitly (the run-2+ crash the audit caught is the highest-value regression pin
   of this ticket — pin it directly, not only via the run-1 shallow state).
6. **Post-green convention:** HS14B ledger parity gate re-run + full T5/T6/T7 suites (audit TDD 11),
   per repo convention for live_execution changes.

## 5. Discrepancies found (none blocking)
- Audit HEAD stamp `336d29f` vs actual `f165b9d` (docs/data-only delta — §0).
- Audit §1.1 says the 6 failures were "verified by pytest run 2026-07-05" — independently
  reproduced here with identical results (§1.1).
- No other material discrepancies: every line-number, grep-census, and control-flow claim I
  checked (≈40 claims) matched HEAD source.
