# Blueprint — reconnect-recovery-fixes_07052026_2022

## Context (root cause, confirmed from live fleet logs 2026-07-05)

First parallel `fleet_runner` session: the ES01B instance (brain=ES continuous, execution=MES,
clientIds 2000/2001, --dry-run) received ZERO bars for 2+ hours during open market while logging
"Reconnected successfully on attempt 1" every ~5 minutes. The CL instance on the same gateway was
healthy throughout. Log-confirmed failure chain:

1. keepUpToDate subscriptions silently died at 00:05 UTC (known IBKR behavior; watchdog caught it correctly).
2. Every reconnect cycle, the `60 S` snapshot for the ES/MES streams got IBKR **error 162
   "HMDS query returned no data"** → TWS terminates that request server-side → the returned
   BarDataList is EMPTY and live updates NEVER arrive → the code logged "Subscribed" anyway.
   (The `2 D` 1-hour requests in the same cycles returned data — the short `60 S` window is the
   fragile request shape.)
3. Two resubscription paths raced each other every cycle (log shows every subscribe sequence twice,
   plus "API historical data query cancelled" — one path cancelling the other's in-flight request).
4. Reconnect backfill logged "gap < 10 min — no backfill needed" in the same second the watchdog
   logged "no bars for 131 min" (timezone bug — gap computes negative).

## Requirements (4 fixes, all in src/live_execution/)

### R1 — Backfill gap must be computed in UTC
`LiveTrader._backfill_reconnect_gap_async` (src/live_execution/live_trader.py:2712) uses
`pd.Timestamp.now()` = LOCAL wall clock (host is UTC-7), but `_last_bar_time_5m` / `_last_bar_time_1h`
are **tz-naive UTC** (normalized via `tz_convert("UTC").tz_localize(None)`, see line 2893-2895).
Gap therefore computes ≈ real_gap − 7h → always negative → backfill NEVER triggers.

**Required behavior:** the gap reference "now" must be tz-naive UTC, matching the stale-bar watchdog
(line 4071: `datetime.now(timezone.utc).replace(tzinfo=None)`). With `_last_bar_time_5m` 90 minutes
(UTC) in the past, the 5M backfill MUST issue a historical fetch; same for 1H with gap > 70 min.
The result must remain tz-naive (trader env is pandas 1.5.3 — `pd.Timestamp.utcnow()` is tz-AWARE
and would raise on comparison with tz-naive index values; must strip tz).

### R2 — `_resubscribe_pending` guard must be set BEFORE `data_client.connect()`
In `LiveTrader._reconnect` (src/live_execution/live_trader.py:3740-3752) the guard
`self._resubscribe_pending = True` is set only AFTER `data_client.connect()` +
`exec_client.connect()` + callback re-registration. IBKR delivers status code 2104 ("market data
farm OK") DURING the connect handshake; with `_subscriptions_lost=True` this fires
`_on_ib_error` → schedules `_deferred_resubscribe()` → races the sync
`_resubscribe_and_backfill()` at line 3795. Both paths subscribe all streams, cancel each other's
in-flight requests, and clobber `self._live_bars_*` references.

**Required behavior:** the guard must already be `True` when `data_client.connect()` is called, so a
2104 arriving during (or immediately after) connect cannot schedule `_deferred_resubscribe`.
The existing behavior of clearing flags afterward (`_resubscribe_and_backfill` sets
`_resubscribe_pending = False` at the end) must be preserved. On a failed attempt (exception or
data-farm-broken `continue`), the guard must not leak permanently `True` in a way that blocks the
NEXT legitimate deferred resubscription after `_reconnect` gives up (attempt loop exhausted →
returns False) — verify and pin whatever reset semantics keep the 2104 path usable later.

### R3 — Empty snapshot (dead keepUpToDate stream) must not be treated as a successful subscription
`IBKRClient.subscribe_live_bars` / `subscribe_live_bars_async`
(src/live_execution/ibkr_client.py:1020-1085) call `reqHistoricalData(..., keepUpToDate=True)` with
default `duration_str="60 S"`. When IBKR answers error 162 "HMDS query returned no data", ib_insync
returns an EMPTY BarDataList without raising and the live stream is dead on arrival. The callers in
live_trader.py (`_subscribe` line 2352, `_subscribe_front_month` line 2390, `_deferred_resubscribe`
line 2613) log "Subscribed ..." unconditionally.

**Required behavior (per subscription — 5m brain, 1h brain, front-month; BOTH sync and async paths):**
- If the returned bars list is empty, retry the subscription ONCE with a longer fallback duration.
  Fallback must be at least `"1 D"` so weekend/holiday startups (where a 60 S window is legitimately
  empty but the prior session's bars exist) still succeed.
- If the retry ALSO returns an empty list, RAISE (clear, descriptive exception naming the stream) —
  project rule: fail loudly, no silent degraded state. In `_reconnect` this propagates into the
  existing `except` at line 3804 → the attempt is counted as FAILED and retried with backoff, and
  "Reconnected successfully" is NOT logged. In `_deferred_resubscribe` the existing
  `except`/`log.exception` path handles it (and `_subscriptions_lost` must remain True so recovery
  re-triggers).
- When the first snapshot returns data, behavior must be byte-identical to today (no extra requests).

### R4 — Escalate after N fruitless watchdog reconnects (process restart via fleet_runner)
Today the stale-bar watchdog (`_check_stale_bars`, live_trader.py:~4050) fires every ~5 min, each
reconnect "succeeds", the attempt counter resets, and the instance churns forever. fleet_runner only
restarts CRASHED children, so a child stuck in this loop looks healthy.

**Required behavior:**
- Count consecutive stale-bar-watchdog firings that occur WITHOUT any new live bar arriving between
  them (any new bar on the 5m/1h brain streams resets the counter to 0).
- When the counter reaches a module-level constant (`_MAX_FRUITLESS_RECONNECTS = 3`), log CRITICAL,
  attempt a Telegram notification (failure to send must never block), and terminate the process with
  a non-zero exit (`SystemExit`) so fleet_runner restarts the child fresh (the startup subscription
  path demonstrably works).
- The counter/threshold must NOT trigger for watchdog fires that DO recover data in between.

## Constraints
- Conda env `trader` (pandas 1.5.3). Full fast suite must pass:
  `conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"`.
- Existing conventions: reconnection tests live in tests/test_reconnection.py; LiveTrader test stubs
  are built with `object.__new__(LiveTrader)` + minimal seams (see that file and
  tests/test_session_watchdog_rollover.py). Mock ALL IBKR/network/filesystem I/O.
- Project rule: no silent null defaults — missing/invalid state raises, never defaults.
- Do NOT touch fleet_runner.py (its crash-restart behavior is the escalation target, unchanged).
- Do NOT modify pre-existing tests; new tests go in new file(s) or append to test_reconnection.py
  only if additive.

## Acceptance
1. New tests fail on current code for R1-R4 (Red), pass after implementation (Green).
2. Entire fast suite green in trader env.
