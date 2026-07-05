# TDD Status — t7-es-ops-runway_07052026_0214

## PHASE: Red
**Updated:** 2026-07-05 03:34 PT | **HEAD:** 03218af (branch development, no worktree)
**Tester:** TDD-Tester | **Test file:** `tests/test_hourly_only_equity_session.py` (NEW, Strict-Lock, FINALIZED)
**Evolved (sanctioned):** `tests/test_session_watchdog_rollover.py` — exactly the 3+1 blueprint-sanctioned pin evolutions, nothing else touched.

## New file — 46 tests, 9 classes
| Class | Coverage | Red state |
|---|---|---|
| `TestEquityRegistryShape` | `(("17:00","15:15"),("15:30","16:00"))` on EXACTLY ES/MES/NQ/MNQ; micros==parents; GLOBEX/grains tuples unchanged (pin) | 2 fail / 2 pin-pass |
| `TestEquityCalendar` | Jan CST + Jul CDT parametrized: Tue 15:20 CLOSED-halt, Tue 15:35 byte-"OPEN", Tue 16:30 CLOSED-maintenance, Sun 16:00 / Fri 16:30 / Sat CLOSED-weekend, Sun 17:05 + Fri 15:35 OPEN; ES/MES/NQ/MNQ dispatch; unknown shape still raises (pin) | 6 fail (halt/maintenance/dispatch discriminators) / 5 pin-pass (weekend+OPEN instants coincide with GLOBEX at HEAD) |
| `TestEquityAnchor` | most-recent Mon-Fri 15:30 / Sun-Thu 17:00 CT open vectors (8 per week, incl. Sat→Fri-15:30 pinning Fri-17:00-not-an-open), tz-naive; GLOBEX (CL/MCL/GC) still None (pin) | 2 fail / 2 pin-pass |
| `TestEnable5mStreamFlag` | default True + CL byte-identical construction (T5 288/24 pins reused); live_config-present-key-absent → True; false+`bar_size:"5m"` → ValueError at `__init__`; false → only 1h manager; loud "HOURLY-ONLY"+"enable_5m_stream" caplog banner | 5 fail |
| `TestHourlyOnlyBoot` | REAL construction, ES-style 1h config, flag false, tmp-path 5m seed/cache (absent) + tmp 1h seed via existing `live_config.seed_path_1h`: constructs, `data_manager_5m is None`; `_warm_start` skips 5m (no FileNotFoundError, 1h window up, `rolling_df_5m`/`_last_bar_time_5m` stay None); `_subscribe` issues NO continuous-5m call while 1h AND front-month hands subs both happen; `_shutdown` safe + 1h save | 4 fail (Red `_warm_start` dies with the tmp-path No-Silent-Bootstrap FileNotFoundError — the exact defect) |
| `TestHourlyOnlyCacheSaveGuard` | C2 FUNCTIONAL: `__new__` stub with `data_manager_5m=None` → `_shutdown` must still call `data_manager_1h.save_cache()` and must NOT emit the shared-except "Failed to save warm-start cache" warning | 1 fail (at HEAD the swallowed AttributeError skips the 1h save — save_cache called 0 times) |
| `TestTrailingFrameSelection` | C4: ES hourly-only in-position stub (5m frame None) reads 1h extremes → activation + `modify_order` via the S6 seam; below-trigger 1h extremes accumulate; CL pin: divergent frames, 5m drives (positive + negative control). All stubs deliberately OMIT `_enable_5m_stream` → frame-presence selection is structurally forced (no flag read) | 2 fail (`'NoneType' object has no attribute 'iloc'` — the :1100 blocker) / 2 pin-pass |
| `TestHourlyOnlyWatchdog` | `_STALE_BAR_THRESHOLD_MINUTES_1H == 135`; hourly-only anchors `_last_bar_time_1h`: True at 140 min / False at 120 min (frozen clock, OPEN, Jan+Jul); equity-halt CLOSED → False; no-1h-bar-yet → False; 5m-enabled pins: 16→True / 10→False / fresh-5m+stale-1h→False | 3 fail / 7 pin-pass |
| `TestES01BFlagPatch` | shipped ES01B carries `live_config.enable_5m_stream == false` (Coder patches); still resolves ES/ES/CME tick 0.25; T6 sentinels (client_id 1010, marketable_limit x2, thresholds .53/.56, experiment_ids, holdout 6, conflict_resolution, no brain_symbol) unchanged | 1 fail / 2 pin-pass |

## T5 pin evolutions (each carries a T7 EVOLUTION note citing this ticket)
1. `test_globex_family_dispatches_to_same_calendar` — loop now CL/MCL/GC/SI; NEW assertion: ES/NQ no longer carry `(("17:00","16:00"),)`. FAILS at HEAD.
2. `test_es_maintenance_break_modeled_closed` — re-pinned from `_HALT_STR` byte-equality to equity shape (`!= "OPEN"`, `startswith("CLOSED")`, contains `"maintenance"`) at both ET instants. FAILS at HEAD.
3. `test_session_open_anchor_none_for_globex` — `_ES` dropped from the None loop (CL/MCL/GC keep the pin); NEW assertion: ES anchor is NOT None at all 5 instants. FAILS at HEAD.
4. `test_stale_threshold_pin_15` — C7 clarification: docstring re-scoped to 5m-ENABLED instances (`_last_bar_time_5m` / 15 min); assertion byte-unchanged, still PASSES.

## Red proof (HEAD 03218af)
`conda run -n trader python -m pytest tests/test_hourly_only_equity_session.py tests/test_session_watchdog_rollover.py -v --tb=short --continue-on-collection-errors`
```
29 failed, 89 passed, 1 warning in 3.72s
```
- New file: 26 failed / 20 pin-passed (all failures on missing implementation: GLOBEX strings where equity expected, anchor None, missing `_enable_5m_stream` / `_STALE_BAR_THRESHOLD_MINUTES_1H`, unconditional 5m DataManager/subscribe, tmp-seed FileNotFoundError in `_warm_start`, C2 skipped 1h save, `None.iloc` at :1100, ES01B key absent).
- T5 file: EXACTLY the 3 evolved pins fail; all other pins (CL byte sweep incl. DST windows, grains, Q1 reopen pins, seed math, roll namespace, front-month selection, bars_per_day wiring) stay green — 69 passed.
- Neighbors: `tests/test_instrument_master_live_fields.py tests/test_config_generator_symbols.py tests/test_instrument_context.py -q` → **161 passed**.

## Notes for the Coder
- `data_manager_5m` hourly-only sentinel is **None** (audit_hourly_only §7 item 1) — pinned.
- Trailing frame selection must key off FRAME PRESENCE, not the flag: the trailing stubs here AND in the Strict-Lock `test_modify_order_transmit.py` / `test_tick_order_pricing.py` do not set `_enable_5m_stream`.
- `_check_stale_bars` must tolerate stubs without `_enable_5m_stream` (T5 `_watchdog_stub` sets only `_last_bar_time_5m`) — the in-function getattr seam the amendment flagged.
- Do NOT patch ES01B beyond the one field; do NOT touch the livetest 5m mirror, the :2527/:2253 inconsistency, or generator emission (C6).
- Post-green: full fast suite + BLOCKING HS14B ledger parity gate (`setup --disable-trailing`, 2200/336) → PARITY: PASS before commit.

---

## PHASE: Green
**Updated:** 2026-07-05 03:54 PT | **HEAD:** 03218af (branch development, no worktree, uncommitted)
**Coder:** TDD-Coder | Ticket: t7-es-ops-runway_07052026_0214

### Files modified (blueprint Target Files, nothing else)
1. `src/core/instrument_master.py` — new `_EQUITY_SESSION = (("17:00","15:15"),("15:30","16:00"))` constant; `session_hours_ct` flipped to it on EXACTLY ES/MES/NQ/MNQ (grep-verified 4 carriers); all other entries byte-untouched.
2. `src/live_execution/session_calendar.py` — new `_equity_market_status` (America/Chicago: Sat/Sun<17:00/Fri≥16:00 → CLOSED weekend; Mon-Thu 16:xx → CLOSED "maintenance"; Mon-Fri 15:15-15:29 → CLOSED "halt"; else byte-"OPEN") + `_equity_session_open_anchor` (grains-pattern walk-back over Mon-Fri 15:30 / Sun-Thu 17:00 CT opens, tz-naive UTC); two new tuple-equality dispatch arms in `market_status`/`session_open_anchor`; `_unsupported_session_shape` message extended with the EQUITY tuple; module docstring C4 block rewritten (equity shape now modeled). GLOBEX/grains function bodies byte-untouched.
3. `src/live_execution/live_trader.py` —
   (a) `_enable_5m_stream` read in `__init__` from `live_config.enable_5m_stream` (optional, default True); ValueError when false + non-hourly bar_size (message names the flag); loud WARNING banner containing "HOURLY-ONLY" + "enable_5m_stream" when false; Telegram startup payload stamped `Mode: HOURLY-ONLY (enable_5m_stream=false)` (blueprint manager ruling);
   (b) when false: `data_manager_5m = None` (construction skipped; "DATA PATHS: 5m seed=" line gated into the enabled arm per the amended canary expectations); `_warm_start` 5m block flag-gated (no 5m seed requirement, `rolling_df_5m`/`_last_bar_time_5m` stay None); `_subscribe` + `_deferred_resubscribe` 5m brain-subscription blocks flag-gated (1h + front-month hands subscriptions untouched);
   (c) None-guards at the Step-6 (:726-area) and rollover (:2407-area) `front_month_id` writes; `_shutdown` cache-save restructured per C2 — each manager save now has its OWN None-guard + try/except, so a 5m-side failure can never swallow-and-skip the 1h save;
   (d) `_check_trailing_stop` extremes frame selected ONCE by PRESENCE (`rolling_df_5m if not None else rolling_df_1h`) — no flag read; monotonic max/min semantics unchanged;
   (e) new module constant `_STALE_BAR_THRESHOLD_MINUTES_1H = 135`; `_check_stale_bars` selects stream+threshold via `getattr(self, "_enable_5m_stream", True)` (the sanctioned in-function seam): 5m-enabled → `_last_bar_time_5m`/15 byte-identical; hourly-only → `_last_bar_time_1h`/135.
4. `configs/strategies/ES01B_Sharpe_E03_07042026.json` — exactly one field added: `live_config.enable_5m_stream: false` (T6 sentinels untouched).

### Green proof (all at HEAD 03218af + these changes)
1. `conda run -n trader python -m pytest tests/test_hourly_only_equity_session.py tests/test_session_watchdog_rollover.py -v --tb=short` → **118 passed** (46 new + 72 T5 incl. the 3 re-greened evolved pins), 1 warning, 2.86s.
2. Neighbors (`test_instrument_master_live_fields test_config_generator_symbols test_instrument_context test_macro_vol_parameterization test_tick_order_pricing test_modify_order_transmit -q`) → **350 passed**, 7.13s.
3. Full fast suite (`tests/ -q -m "not slow"`) → **1381 passed, 0 failed** (= manager baseline 1335 incl. the 3 sanctioned evolutions + 46 new), 144.90s.

### Deviations / notes
- Heartbeat `last_bar=` re-point for hourly-only instances (audit_hourly_only §7 item 7) NOT implemented — it is outside the blueprint Target Files / manager contract (a)-(e) and untested; hourly-only heartbeats will report "no bars received yet". C1 canary evidence remains valid via `NEW 1H BAR` + telemetry `raw_front_month_bars` rows. Flagged for manager judgment (micro-follow-up if wanted).
- `_recover_inherited_position` / `_check_naked_position` primary-frame reads (amendment items 5/9) NOT changed — same reason (not in the contract, already `is not None`-guarded, no locked test forces them).
- Scope guards honored: :2527 vs :2253 "1h" inconsistency untouched (C6); no generator/fleet_runner/backtest/livetest-harness edits; GLOBEX/grains calendar bodies and Q1 reopen behavior byte-untouched.
- NEXT (manager): BLOCKING HS14B ledger parity gate (`setup --disable-trailing`, 2200/336) → PARITY: PASS before commit.
