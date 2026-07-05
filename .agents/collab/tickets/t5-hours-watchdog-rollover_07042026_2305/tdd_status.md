# TDD Status — t5-hours-watchdog-rollover_07042026_2305

## [2026-07-05T00:08 PDT] PHASE: Red — TDD-TESTER

**Test file:** `tests/test_session_watchdog_rollover.py` (Strict-Lock, FINALIZED — 72 tests, 7 classes)

**Red proof:** `conda run -n trader python -m pytest tests/test_session_watchdog_rollover.py -v --tb=short --continue-on-collection-errors`
```
tests\test_session_watchdog_rollover.py:96: in <module>
    from src.live_execution.session_calendar import (  # noqa: E402
E   ModuleNotFoundError: No module named 'src.live_execution.session_calendar'
=========================== short test summary info ===========================
ERROR tests/test_session_watchdog_rollover.py
========================= 1 warning, 1 error in 3.01s =========================
```
Missing-implementation failure only (ghost import per TDD protocol; T2 precedent —
the whole file fails at collection until `session_calendar.py` exists).

**Neighbors green:** `tests/test_build_future_contract.py tests/test_symbol_data_paths.py
tests/test_data_manager.py tests/test_rollover.py tests/test_instrument_master_live_fields.py`
→ **200 passed** (19.78s).

**Self-check (scratchpad, throwaway fake calendar injected — NOT in repo):**
23 HEAD-behavior pin tests pass at HEAD 7a861bb (frozen-clock seam, watchdog/warm-start
stubs, CL byte pins, Q1 false-positive pins, CL tolerance skip/apply, CL 280-day trim,
CL 4320 message, CL-only Step-0 restore); the other 49 fail for the RIGHT reasons
(ghost helpers, missing registry field, missing execution_symbol kwarg, grains calendar,
ZC watchdog storm, ZC 4320 filename, ZC over-trim). No test-plumbing failures.

**Test classes / coverage:**
- `TestSessionCalendarGlobex` — CL pinned instants byte-identical (impact_review V1 set,
  Jan+Jul); minute-by-minute equivalence sweep vs frozen HEAD reference over plain
  EST/EDT weeks + both 2026 DST transitions (03-08, 11-01); CL/MCL/ES/GC/SI/NQ tuple
  dispatch; ES maintenance break modeled; GLOBEX `session_open_anchor` always None;
  Q1 reopen surface pinned (Mon 18:03 ET OPEN + no anchor).
- `TestSessionCalendarGrains` — ZC halts (morning 07:45-08:30, afternoon 13:20-19:00)
  CLOSED; OPEN pins byte-exact incl. Tue 10:00 CT, 08:30 boundary, overnight ≥19:00;
  weekend incl. Fri ≥13:20; ZS dispatch; anchor vectors (19:02→19:00, 09:00→08:30,
  Mon 03:00→Sun 19:00, cross-midnight); watchdog reopen grace at 08:35 CT with 19h-old
  bar → False, grace expiry 08:50 → True; unknown session shape → ValueError.
- `TestFrontMonthSelection` — CL all-12 short-circuit (contractMonth access raises via
  sentinel property), legacy buffer-6 byte parity incl. string ">=" boundary + nearest
  fallback; GC serial filtering + FND-buffer skip; C3 2027 Memorial-Day OUTCOME pin
  (GCM7 ineligible 2027-05-27/28, still eligible 05-20 — remedy-agnostic);
  `_first_notice_proxy` vectors; ES HMUZ buffer-8 + serial decoy; no-active-month
  RuntimeError; blank contractMonth ValueError; `_EXPIRY_BUFFER_DAYS` gone.
- `TestRollToleranceRegistry` — all 15 entries carry `roll_ratio_tolerance`
  (CL/MCL 0.01 pin, others 0.001, sane band); DataManager instance attr from registry;
  end-to-end initialize(): ES 1.004 APPLIED (history+ledger+ratios), CL 1.004 SKIPPED
  (pin), CL 1.02 APPLIED (pin).
- `TestRollMetadataNamespace` — execution_symbol default/explicit; CL-only save keeps
  legacy keys byte-semantics + gains by_symbol; two-symbol no ping-pong; C1 roll_history
  "from" from own namespace under interleaved writes (+merge not replace); C2 Step-0
  restore ownership filter (CL-only file unchanged pin); legacy-file migration quiet.
- `TestSeedLookbackMath` — formula 24→280 EXACT / 23→292 / 16→406, nonpositive raises
  ValueError; REQUIRED_1H_BARS==4320; instance seed_lookback_days; CL 280-day trim pin,
  ZC 406-day trim, ZC 350-day seed untrimmed/no-raise; 4320 message CL byte-identical,
  ZC names warm_start_cache_ZC_1h.parquet.
- `TestLiveTraderSessionWiring` — CL `_get_market_status` byte-match via calendar
  (delegation asserted); DM bars_per_day CL 288/24 pin, ES 276/23, ZC 200/16;
  execution_symbol wiring (CL/MCL/ZC); threshold pin 15; ZC halt → watchdog False
  despite 105-min stale clock; CL Mon/Sun reopen false-positives PINNED True (Q1);
  16-min True / 10-min False / no-bars False.

**Design ambiguities resolved (governing doc cited):**
1. Spawn-prompt "ZC 19:30 CT overnight halt → CLOSED" conflicts with audit §4a
   (evening halt is [13:20, 19:00); "else OPEN incl. Mon-Thu ≥19:00") and the registry
   `_GRAINS_SESSION` 19:00 reopen — followed audit: 19:30 CT pinned OPEN; the CLOSED
   halt boundary is pinned at 18:30/18:59 CT (Friday 19:30 CT IS pinned CLOSED-weekend).
2. Grains CLOSED strings shape-pinned (startswith "CLOSED" + halt/weekend marker), not
   byte-pinned — blueprint hard constraint 1 freezes only CL strings; "OPEN" is pinned
   byte-exact everywhere (watchdog gate compares == "OPEN").
3. C3 pinned on OUTCOME only (GCQ7 selected on 2027-05-27/28) so both blueprint-allowed
   remedies (holiday-aware proxy OR bumped GC/MGC buffer) satisfy it; the two
   `_first_notice_proxy` vectors pinned are non-holiday, remedy-agnostic.
4. `derive_seed_lookback_days(<=0)` pinned to ValueError (audit says "raises" without a
   type; house loud-failure convention).
5. No freezegun in repo — frozen clock via `patch.object(live_trader, "datetime", <subclass>)`
   (the module-name seam the code reads); calendar functions take injected utc_now.

**Coder notes:** do NOT modify this test file (Strict-Lock). The single authorized
existing-pin update is `tests/test_build_future_contract.py:390` (Coder-owned).
`test_expiry_buffer_days_gone` asserts the attr absent on BOTH the class and the module.
`test_dm_execution_symbol_wiring` requires live_trader to pass
`execution_symbol=ctx.execution_symbol` to BOTH DataManagers.

NEXT: Coder (Green) → full fast suite → HS14B ledger parity gate (BLOCKING, per C5/blueprint).

## [2026-07-05T00:35 PDT] PHASE: Green — TDD-CODER

**Result: GREEN on first full run.** All 72 nodes of the Strict-Lock suite pass;
neighbors green; full fast suite 1284 passed (= 1212 baseline + 72 new), zero
failures. No test file modified except the single authorized pin update.

**Pytest summaries:**
1. `tests/test_session_watchdog_rollover.py -v` → **72 passed** (2.73s)
2. Neighbors (`test_build_future_contract test_symbol_data_paths
   test_data_manager test_rollover test_instrument_master_live_fields
   test_live_macro_refresh test_macro_vol_parameterization -q`)
   → **286 passed** (18.85s)
3. Full: `tests/ -q -m "not slow"` → **1284 passed** (147.71s)

**Files modified:**
- NEW `src/live_execution/session_calendar.py` — GLOBEX body moved VERBATIM
  (local `import pytz` and comments included); grains calendar per audit §4a
  ([13:20,19:00) CT evening halt, [07:45,08:30) morning halt, Fri>=13:20
  weekend, Sun<19:00 weekend); `session_open_anchor` (GLOBEX→None,
  grains→most-recent 08:30/19:00 CT open as tz-naive UTC); dispatch on
  registry `session_hours_ct` tuple equality; unknown shape ValueError naming
  the symbol; C4 doc block (ES/NQ 15:15-15:30 CT halt NOT modeled — REQUIRED
  precondition for ES launch, T7); injected `utc_now` everywhere.
- `src/live_execution/live_trader.py` — `_get_market_status` staticmethod →
  instance method delegating to the calendar via `_brain_instrument` (module
  still reads `datetime` from its own namespace — frozen-clock seam and the
  `test_live_macro_refresh.py:80` instance-mock seam both verified green);
  `_check_stale_bars` gains the anchor-grace reference (GLOBEX None →
  arithmetic bit-identical, Q1 false-positives pinned True); DataManager
  ctor feeds registry `bars_per_day_5m/1h` + `execution_symbol` (CL 288/24
  pins green); `_min_required` references `REQUIRED_1H_BARS`; 4320-raise
  message cache-name-derived (CL byte-identical, ZC names
  warm_start_cache_ZC_1h.parquet); T2-C2 comment block → "resolved in T5".
- `src/live_execution/ibkr_client.py` — module-level `_MONTH_CODES`,
  `_first_notice_proxy`, `_select_front_month` (pure, IB-free); both
  front-month methods route through the helper; `_EXPIRY_BUFFER_DAYS`
  deleted (class had it; module never did — both asserted gone); `symbol`
  defaults removed from both front-month methods; log line format unchanged
  (`buffer=%dd` now prints registry roll_buffer_days — 6 for CL).
- `src/live_execution/data_manager.py` — `REQUIRED_1H_BARS=4320` +
  `derive_seed_lookback_days` (integer-exact: 24→280, 23→292, 16→406, <=0
  raises ValueError); instance `roll_ratio_tolerance` +`seed_lookback_days`
  derived ONLY in `__init__` (ratio-method `__new__` stubs untouched);
  keyword-only `execution_symbol` (None → symbol); `_stored_front_month`
  namespaced-read helper (by_symbol → legacy w/ startswith ownership →
  first-run) used by BOTH `_detect_rollover` and C1 `old_fm`; save merges
  `last_front_month_by_symbol` and still writes the legacy key verbatim;
  C2 Step-0 restore ownership-filtered on entry `"to"`; deleted dead
  `_SEED_LOOKBACK_DAYS`/`_BARS_PER_DAY`/`_MAX_IB_REQUEST_DAYS`/
  `_ROLL_PRICE_TOLERANCE`.
- `src/core/instrument_master.py` — REQUIRED `roll_ratio_tolerance` on all
  15 entries (CL/MCL 0.01 pin; others 0.001), declared before the defaulted
  `micro_of`/`slippage_ticks` (registry is all-kwargs — safe).
- `tests/test_build_future_contract.py:387-390` — the ONE authorized pin
  update: `test_expiry_buffer_still_six_days` →
  `test_expiry_buffer_resourced_to_registry` (asserts the class attr gone +
  CL roll_buffer_days == 6), per the pin's own self-documentation.

**Deviations / judgment calls (documented in code):**
1. **C3 remedy = holiday-aware proxy** (blueprint offered proxy OR buffer
   bump): `_first_notice_proxy` steps back over US Memorial Day (the last
   Monday of May is the only US holiday that can BE a month's last weekday).
   Chosen over the GC/MGC buffer bump because it needs no registry-value
   change and passes both the C3 outcome pins (GCM7 ineligible 2027-05-27/28,
   eligible 05-20) and the non-holiday proxy vectors with buffer 3.
2. **Detail objects LACKING the `contractMonth` attribute** (as opposed to a
   blank value) fall back to the LTD month on the restricted path. Required
   to keep the Strict-Lock T2 suite green: `tests/test_build_future_contract.py::
   test_search_contract_exchange[ES-CME]` (+async twin) drives ES through the
   selection with a SimpleNamespace detail that has no contractMonth at all.
   Real ib_insync 0.9.86 ContractDetails always carries the field, so live
   behavior is exactly audit §4c (blank → ValueError, locked-test-pinned);
   the fallback is a test-double shim, documented in the helper docstring.
   LTD lies in the delivery month for every restricted registry instrument.
3. Calendar imports in live_trader are aliased (`_calendar_market_status`,
   `_session_open_anchor`) so the pre-existing local variables named
   `market_status` in `_log_heartbeat`/`_check_stale_bars` cannot shadow the
   imported callable. Zero behavior impact.
4. Step-0 restore entries with a missing/non-string `"to"` field pass the
   ownership filter (pre-T5 files were single-symbol by construction;
   filtering them out would silently drop legacy CL rolls). Documented at
   the filter site; `cumulative_ratio` stays global/mixed (informational
   only, per impact_review C2).

NEXT (Manager): HS14B ledger parity gate (`setup --disable-trailing`,
2200 warmup + 336 replay) → PARITY: PASS required before commit
(LiveTrader/DataManager `__init__` changed — C5/blueprint BLOCKING).
