# TDD Result — t5-hours-watchdog-rollover_07042026_2305

**Outcome: GREEN + PARITY PASS — ticket complete. HUMAN AUTHORIZED (multi-component);
reviewer conditions C1-C5 all applied.**

- Red: 65 tests / 72 nodes; 23 HEAD-behavior pins pre-verified at HEAD via scratchpad
  smoke. Baseline 1212 (manager-verified).
- Green: **1284 passed, 0 failed** (manager-verified independently).
- Blocking parity gate (C5): **PARITY: PASS**, exit 0 — $0.00 delta ($1,695.01 both).

## Files changed
- NEW `src/live_execution/session_calendar.py` — GLOBEX calendar (CL body verbatim,
  DST-sweep byte-pinned) + GRAINS calendar ([13:20,19:00) CT halt); session_open_anchor
  (grains reopen grace; GLOBEX None = CL bit-identical); unknown shape raises; C4 doc
  block: ES/NQ 15:15-15:30 CT halt NOT modeled — REQUIRED before ES launch (T7).
- `src/live_execution/live_trader.py` — calendar delegation; session-aware watchdog
  (grains halt no longer reconnect-storms; Q1 CL reopen false-positive PINNED as-is →
  follow-up ticket cl-watchdog-reopen-grace_07052026_0001); registry bars_per_day;
  seed formula (CL 280 exact / ES 292 / ZC 406 — ZC startup crash gone); 4320-message
  cache-name-derived (CL byte-identical).
- `src/live_execution/ibkr_client.py` — `_select_front_month` + `_first_notice_proxy`
  (C3: Memorial-Day-aware proxy — GCM7 2027 ineligible past true FND, no buffer change);
  active-month filter with all-12 short-circuit (CL never reads contractMonth);
  registry roll_buffer_days (CL 6, ES 8); `_EXPIRY_BUFFER_DAYS` deleted; CL defaults
  removed from front-month methods.
- `src/live_execution/data_manager.py` — registry roll_ratio_tolerance (CL/MCL 0.01
  pinned: 1.004 skips / 1.02 applies; ES 0.001: 1.004 applies); roll-metadata
  last_front_month_by_symbol namespace (C1 old_fm namespaced read; C2 Step-0 restore
  ownership-filtered; legacy key still written; migration quiet); live bars_per_day.
- `src/core/instrument_master.py` — required roll_ratio_tolerance on all 15 entries.
- `tests/test_session_watchdog_rollover.py` — NEW, 65 tests (Strict-Lock).
- `tests/test_build_future_contract.py:387-390` — the one authorized pin update.

## Notes
- C5 reminder for future work: the parity harness BYPASSES this whole layer
  (market status/watchdog/front-month/seed math) — the T5 pins are the only fence.
- ES launch preconditions accumulated so far: equity session shape (C4), GVZ
  entitlement check (T4/Q3) — both on T7's checklist.
- Structural engine work of the multi-symbol program is COMPLETE with this ticket;
  T6-T8 are generator/config/data/process work.
