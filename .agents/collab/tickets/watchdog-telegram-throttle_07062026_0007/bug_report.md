# Bug Report — watchdog-telegram-throttle_07062026_0007

## User complaint (2026-07-05 ~23:40-23:56 PDT, live 4-model fleet)
During the thin post-holiday Sunday Globex session, MES and MGC (brains ES/GC) print no
trade bars for long stretches. The stale-bar watchdog + reconnect + escalation machinery
works correctly (recovers real data holes via backfill), but it SPAMS Telegram: the user
received ~10+ messages within minutes, repeating every ~30 min per quiet symbol, e.g.:

```
23:40:32 MGC STALE BAR WATCHDOG - No bars received for 21m during market hours. Forcing reconnect...
23:40:33 MGC RECONNECT - Connection lost, attempting recovery (max 15 attempts)...
23:40:41 MGC RECONNECTED - Recovery successful on attempt 1/15
23:43:43 MES STALE BAR WATCHDOG - No bars received for 24m ...
23:43:44 MES RECONNECT - Connection lost, attempting recovery ...
23:43:53 MES RECONNECTED - Recovery successful on attempt 1/15
23:45:43 MGC STALE BAR WATCHDOG - No bars received for 26m ...
23:45:43 MGC RECONNECT - ...
23:45:52 MGC RECONNECTED - ...
23:48:54 MES WATCHDOG ESCALATION - 3 consecutive reconnects produced no bars (29m stale). Terminating process...
23:49:41 MES LiveTrader Online ... (startup banner after fleet_runner restart)
23:50:53 MGC WATCHDOG ESCALATION - 3 consecutive reconnects produced no bars (31m stale). Terminating process...
```

## USER DIRECTIVE (explicit, 2026-07-06)
1. Raise the stale-bar reconnect threshold from 15 to **30 minutes**.
2. Watchdog-family Telegram messages must not arrive **more than once per hour** per
   instance ("some type of cooldown, or consolidation of messages — I don't need 10
   messages for a single process").
3. Log lines are NOT to be throttled — full fidelity stays in the log files. Only the
   Telegram sends are rate-limited.

## Code facts (verified 2026-07-06 by Ticket-Manager)
- `src/live_execution/live_trader.py:149` — `_STALE_BAR_THRESHOLD_MINUTES = 15` (5m-stream
  instances; all 4 fleet models use this). `_STALE_BAR_THRESHOLD_MINUTES_1H = 135` (:154,
  hourly-only instances — user directive does NOT change this one).
- `_MAX_FRUITLESS_RECONNECTS = 3` (:161) — unchanged by directive.
- Watchdog logic: `_check_stale_bars()` at live_trader.py:4087-4191; called from the event
  loop (~:3912). Any new brain-stream bar resets the fruitless counter (:2959, :3017).
- Telegram sends in the spam family (all wrapped in try/except so failures never block):
  - :4181 `*STALE BAR WATCHDOG* - No bars received for Xm...` (every watchdog fire)
  - :4162 `*WATCHDOG ESCALATION* - 3 consecutive reconnects...` (before SystemExit)
  - :3739 `*RECONNECT* - Connection lost, attempting recovery (max 15 attempts)...`
    (first attempt inside `_reconnect()`)
  - :3807 `*RECONNECT* - Attempt N/15: ...` (subsequent attempts)
  - :3829 `*RECONNECTED* - Recovery successful on attempt N/15`
  - NOT in scope (rare, genuinely alarming, keep unthrottled): :3877 / :3925 / :3946
    `*RECONNECT FAILED* - All 15 attempts exhausted...` and startup banners
    (:854 Front Month, :886 LiveTrader Online) and 1-hour heartbeats.
- Escalation path raises SystemExit → fleet_runner restarts the child → **in-process
  cooldown state dies with the process**. In the observed loop the process restarts every
  ~30-45 min, so a naive in-memory cooldown still yields ~1.5-2 watchdog messages/hour.
  Cross-restart persistence (e.g., tiny JSON keyed by client_id/category under the data
  root, best-effort read/write) is likely needed to honor "once per hour" literally.
- Test pins that MUST be evolved in the same change (repo pin rule):
  - `tests/test_session_watchdog_rollover.py:1325` — `assert lt_module._STALE_BAR_THRESHOLD_MINUTES == 15`
  - `tests/test_hourly_only_equity_session.py:867` pins the 135 constant (UNCHANGED — do not touch).
  - `tests/test_reconnect_recovery_fixes.py:747` pins `_MAX_FRUITLESS_RECONNECTS == 3`
    (UNCHANGED). Its R4 tests drive `_check_stale_bars()` through fruitless firings and
    may assert on telegram/log behavior — the throttle must not break them, or they must
    be evolved same-change with justification.
  - Neither test file carries a Strict-Locked header (verified by grep).
- Session-calendar watchdog arithmetic (`_session_open_anchor`, reopen grace) is
  byte-pinned by tests/test_session_watchdog_rollover.py — the threshold constant change
  must not alter that arithmetic, only the constant.
- The live fleet is RUNNING from this repo; children pick up code on their next restart.
  Tree must never be left in a broken state; tests must pass before done.

## Severity / classification inputs
- Not a regression: behavior is original design (fleet-reconnect-recovery-fixes 675afd2,
  R4 escalation) working as intended; this is a user-directed tuning + alert-throttling
  enhancement on live-money code.
- Risk surface: `_STALE_BAR_THRESHOLD_MINUTES` 15→30 doubles blind time before recovery
  begins during a REAL silent subscription death (bracket TP/SL rest server-side on IBKR,
  but trailing-stop tightening and max-hold exits stall while blind). User accepted this
  trade-off explicitly.
