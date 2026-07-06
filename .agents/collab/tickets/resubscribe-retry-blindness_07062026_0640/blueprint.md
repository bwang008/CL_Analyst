# Ticket Resolution Blueprint — resubscribe-retry-blindness_07062026_0640
**Ticket Directory:** `.agents/collab/tickets/resubscribe-retry-blindness_07062026_0640/`

## Bug Summary
2026-07-06 ~06:00 PT incident: an IBKR website login invalidated the Gateway
data session. All children received farm-lost (2103/2105) then farm-OK (2106)
while the session conflict still held, so `_deferred_resubscribe` failed with
error-162-shaped RuntimeErrors on every child. The except path logs "will
retry on next reconnect" but the ONLY trigger for another attempt is a new
farm-OK event (live_trader.py:2649) — which never comes, because the farm
already reported OK. Children sat alive-but-blind (`subs_lost=True`) with NO
error-queue visibility: the queue only captures process crashes
(fleet_error_events.py is invoked by fleet_runner on child death), and the
stale-bar watchdog's non-fatal firings leave only log/Telegram traces.

Root causes:
1. `_deferred_resubscribe` has no retry timer — a failed resubscription waits
   for an external event that may never fire.
2. Alive-but-degraded states (watchdog firing, resubscribe retries exhausted)
   emit NO error-queue event, so the hourly monitor cannot see them.

## Target Files
- `src/live_execution/live_trader.py`
- `src/live_execution/fleet_error_events.py`

## Required Changes

### R1 — retry timer with exponential backoff (live_trader.py)
- New module constants next to `_STALE_BAR_THRESHOLD_MINUTES`:
  `_RESUBSCRIBE_RETRY_BASE_SECONDS = 60`,
  `_RESUBSCRIBE_RETRY_CAP_SECONDS = 300`,
  `_MAX_RESUBSCRIBE_RETRIES = 5`.
- New small seam method `LiveTrader._schedule_resubscribe_retry(delay_seconds)`
  that does `asyncio.get_event_loop().call_later(delay_seconds,
  lambda: asyncio.ensure_future(self._deferred_resubscribe()))`.
- `_deferred_resubscribe` except-path: increment a stub-safe retry counter
  (`getattr(self, "_resubscribe_retry_count", 0) + 1`). If count <=
  `_MAX_RESUBSCRIBE_RETRIES`: compute
  `delay = min(BASE * 2**(count-1), CAP)` (60,120,240,300,300), log with the
  attempt number, call `_schedule_resubscribe_retry(delay)`, and keep
  `_resubscribe_pending = True` after the coroutine exits (so a racing
  farm-OK event cannot double-schedule). If count > max: log exhaustion,
  emit a health event (kind `resubscribe-retries-exhausted`, R3 below), and
  clear `_resubscribe_pending` (farm-OK path re-arms; watchdog is backstop).
- Success path: reset the retry counter to 0. `_resubscribe_pending` must
  end False (current `finally` semantics preserved for success).

### R2 — watchdog firings become queue events (live_trader.py)
- New stub-safe helper `LiveTrader._emit_health_event(kind, detail)` that
  calls `fleet_error_events.emit_child_health_event(model_name=<strategy/
  instance name>, client_id=<client id>, kind=kind, detail=detail)` inside
  try/except (emission failure must never affect trading).
- `_check_stale_bars` non-fatal firing branch (after the STALE BAR WATCHDOG
  warning, before returning True) calls
  `self._emit_health_event("stale-bars-watchdog", <detail incl. minutes
  stale + subs_lost flag>)`. The SystemExit escalation branch is NOT changed
  (the resulting crash already reaches the queue via fleet_runner).

### R3 — child-side health events (fleet_error_events.py)
- New module function `emit_child_health_event(model_name, client_id, kind,
  detail, queue_dir=DEFAULT_QUEUE_DIR,
  patterns_path=DEFAULT_INFRA_PATTERNS_PATH)`:
  - Schema-compatible with crash events (same required keys the watcher
    reads: event_id, model_name, classification, matched_infra_pattern,
    occurrences, gave_up, traceback, ...) plus `event_kind: "health"` and
    `health_kind: <kind>`; `traceback` carries the detail text; exit_code /
    restart_count are None; gave_up False.
  - Classifies `detail` against infra_patterns.json (so farm-flavored
    stale-bar events auto-file as infrastructure by the watcher).
  - Dedup identical to crash events: hash over model+kind+normalized detail;
    pending -> update occurrences/last_seen in place; processing -> skip;
    done-only -> new event.
  - NEVER raises (mirror emit_crash_event's contract); returns the pending
    path or None.

## Test Contract (Strict-Locked)
`tests/test_resubscribe_retry.py` — seams as specified above; the retry
counter attribute name `_resubscribe_retry_count` and the seam method name
`_schedule_resubscribe_retry` are part of the contract.
