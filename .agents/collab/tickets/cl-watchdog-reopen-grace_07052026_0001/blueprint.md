# Ticket Resolution Blueprint — cl-watchdog-reopen-grace_07052026_0001
**Ticket Directory:** `.agents/collab/tickets/cl-watchdog-reopen-grace_07052026_0001/`
**Authorization:** operator green-lit 2026-07-07 ~15:30 PT (overturning the
07-05 Q1 pin) after the false-positive fired on schedule two consecutive days.
**Routing:** FAST TRACK (workflow Step 2.3): LOW severity (daily operational
noise, self-healing), NOT a recent regression (designed-in pin from T5,
07-05); Reviewer skipped; TDD executed inline by the manager (single-subsystem
calendar function; the prior two tickets' subagent cycles were for
multi-component live-path changes).

## Bug Summary
`session_open_anchor` returns `None` for the GLOBEX calendar (CL/MCL/NG/GC/
MGC/SI), so `_check_stale_bars`' staleness clock spans the daily 16:00-17:00
CT halt and the weekend. At every 17:00 CT reopen the last bar is ~65-70 min
old, the 30-min threshold trips, and the watchdog needlessly force-reconnects
every 5m-stream GLOBEX child and queues stale-bars-watchdog health events
(observed 2026-07-06 AND 07-07 at 15:0x PT: 3-4 events + ~12 Error-366 lines
daily). Grains (M4) and equity (T7) calendars already have reopen anchors —
GLOBEX was deliberately pinned as-is with this ticket as the follow-up.

## Target Files
- `src/live_execution/session_calendar.py` — new `_globex_session_open_anchor`
  (most recent Sun-Thu 17:00 CT open, tz-naive UTC, grains-anchor loop style);
  GLOBEX dispatch branch returns it instead of None; module + function
  docstrings updated (the "pinned as-is" language retires).
- `src/live_execution/live_trader.py` — comment-only: the `_check_stale_bars`
  T5 note documenting the pinned false-positive updates to reference the fix.
- `tests/test_globex_reopen_grace.py` (NEW) — anchor correctness (mid-session,
  daily-halt, reopen+ε, Friday→Thu, Sunday reopen, DST winter/summer, tz-naive
  UTC contract) + watchdog integration (last bar pre-halt: graced at
  reopen+5min, still fires at anchor+threshold with no bars — the grace must
  not mask real post-reopen staleness).
- `tests/test_session_watchdog_rollover.py` — PIN UPDATE with justification:
  the GLOBEX-anchor-is-None assertions (~:304-342) flip to assert the 17:00 CT
  anchor; their own comments cite this ticket as the planned release. Market-
  status byte-identity sweep pins are UNTOUCHED (the fix does not touch
  `_globex_market_status`).

## Required Changes
Implementation exactly mirrors `_grains_session_open_anchor`: walk back ≤9
days; candidate open = 17:00 CT on Sun-Thu days; return the latest ≤ now as
tz-naive UTC; RuntimeError if none found (calendar bug, no silent None).
No behavior change to market status; `_check_stale_bars` consumes the anchor
through its existing `reference = max(last_bar_time, anchor)` arithmetic.
