# Ticket Resolution Blueprint — watchdog-telegram-throttle_07062026_0007
**Ticket Directory:** `.agents/collab/tickets/watchdog-telegram-throttle_07062026_0007/`

## Bug Summary
Not a defect — user-directed tuning of design-intent behavior (R4 escalation machinery,
675afd2) on live-money code. During thin holiday Globex sessions the stale-bar watchdog on
the 4-model fleet spams Telegram: per quiet symbol, every ~30-45 min cycle sends STALE BAR
WATCHDOG + RECONNECT + RECONNECTED (+ WATCHDOG ESCALATION + restart banners) — the user
received 10+ messages in minutes on 2026-07-05. The machinery itself recovers real data
holes correctly and is NOT weakened by this change.

USER DIRECTIVE (explicit, 2026-07-06): (1) `_STALE_BAR_THRESHOLD_MINUTES` 15 → 30;
(2) watchdog-family Telegram messages at most once per hour PER INSTANCE;
(3) log lines stay full-fidelity — only Telegram sends are throttled.

Severity MEDIUM (Auditor). Full chain ran: Auditor → Impact-Reviewer REJECT (2 blockers) →
Veto Loop iteration 1 → amended design → Impact-Reviewer **APPROVE** (2026-07-06). Audit
trail in `ticket_audit_log.md`.

## Target Files
- `src/live_execution/live_trader.py` — the ONLY source file the Coder modifies
- `tests/test_watchdog_telegram_throttle.py` — NEW unlocked test file (Tester authors red-first)
- Strict-Locked test files, **TDD-TESTER (lock owner) ONLY**, evolved same-change with
  per-file authorization below (precedent: test_instrument_context.py pin evolutions):
  - `tests/test_session_watchdog_rollover.py` (lock header :37)
  - `tests/test_hourly_only_equity_session.py` (lock header :37)
  - `tests/test_reconnect_recovery_fixes.py` (lock header :35)
  - `tests/test_shallow_5m_bootstrap.py` (lock header :42)
- NOT touched: `TelegramAlerter`, `fleet_runner.py`, `cli.py`, session_calendar, configs.

## Required Changes
NOTE: line numbers are content-anchored against HEAD b947ee6; locate by the quoted
content, not the number.

### 1. Threshold (directive #1)
`live_trader.py` (~:149): `_STALE_BAR_THRESHOLD_MINUTES = 15` → `30`. Update trailing
comment citing this ticket + accepted trade-off (doubled blind window before recovery
starts; bracket TP/SL rest server-side on IBKR). Single consumer is the
`stale_threshold = _STALE_BAR_THRESHOLD_MINUTES` assignment (~:4131).
`_STALE_BAR_THRESHOLD_MINUTES_1H = 135` (~:154) and `_MAX_FRUITLESS_RECONNECTS = 3`
(~:161) are UNTOUCHED. Session-anchor arithmetic (~:4145-4152) stays byte-identical.

### 2. Throttle helper (directive #2)
- New module constant near ~:161: `_WATCHDOG_TG_COOLDOWN_SECONDS = 3600` (patchable —
  the locked-file evolutions patch it to 0).
- New method `LiveTrader._send_watchdog_telegram(msg: str) -> None`, adjacent to
  `_check_stale_bars`. Semantics:
  - Clock: `datetime.now(timezone.utc)` via the module-level import (frozen-clock test
    seams then control it).
  - State: in-memory attrs (`last_send_utc`, `suppressed_count`) accessed via the
    established getattr seam pattern (cf. ~:4116/~:4151 — structural seam for
    `object.__new__` stubs); lazily hydrated ONCE from the sidecar (§3) when the state
    path attr exists and is not None.
  - SUPPRESS when `elapsed < _WATCHDOG_TG_COOLDOWN_SECONDS` (strict `<`, so patching the
    constant to 0 disables cleanly): increment `suppressed_count`, best-effort persist,
    emit `log.info("TELEGRAM SUPPRESSED (watchdog-family cooldown, %.0fm remaining, %d
    suppressed this window): %s", ...)` INCLUDING the full suppressed message text,
    return without sending.
  - SEND otherwise: if `suppressed_count > 0`, append
    `(+N watchdog-family alerts suppressed in the last hour — see log)`;
    `try: self._telegram.send(...) except Exception: pass`; record `last_send = now`,
    zero the counter, best-effort persist.
  - ATTEMPT CONSUMES BUDGET: timestamp is recorded whether or not the send succeeds
    (a Telegram outage must not become per-fire retry spam; keeps
    test_r4_telegram_failure_never_blocks_escalation_exit passing unchanged).
  - Helper NEVER raises (send and each persistence I/O in separate try/except).
  - Deliberately NOT a decorator and NOT a TelegramAlerter change (alerter is shared
    with fleet_runner's error-queue path — blast-radius containment).

### 3. Converted send sites (exactly five; each replaces its try/except send block with
one helper call — the no-raise guarantee moves into the helper; NO log line is gated):
- `*STALE BAR WATCHDOG* - No bars received for ...` (~:4193-4199)
- `*WATCHDOG ESCALATION* - ...` (~:4174-4181) — helper runs, INCLUDING its persistence
  write, BEFORE `raise SystemExit` (~:4182)
- `*RECONNECT* - Connection lost, attempting recovery ...` first attempt (~:3749-3756;
  keep the `attempt == 1` gate)
- `*RECONNECT* - Attempt N/...` farms-broken (~:3817-3824; keep the `attempt % 3` gate)
- `*RECONNECTED* - Recovery successful ...` (~:3840-3845)

Explicitly NOT converted (stay unthrottled): all three `*RECONNECT FAILED*` sites
(~:3888/~:3936/~:3957), SAFETY MUTE (~:4094), cache-validation (~:2173), startup banners
(~:867/~:899), 1-hour heartbeats (~:709/~:3722), all trade/rollover/macro sends.

### 4. Cross-restart persistence — per-client_id JSON sidecar
The escalation path SystemExits and fleet_runner restarts the child every ~30-45 min in
the spam scenario; in-memory-only state yields ~1.5-2 msgs/hour and fails the directive.
IMPORTANT (Reviewer R1): telemetry db_path is now ONE SHARED `<data_root>/fleet_telemetry.db`
(cli.py ~:252-260) — do NOT derive the sidecar from the db stem alone.
- In `__init__`, immediately after the telemetry identity block (~:387-396):
  - `client_id is not None` (fleet/CLI):
    `self._watchdog_tg_state_path = Path(db_path).with_name(f"watchdog_tg_cid{self.client_id}.json")`
    — one file per instance, sibling of the shared DB. Content:
    `{"last_send_utc": "<iso>", "suppressed_count": N}`.
  - `client_id is None` (livetest scripts/livetest_engine.py, tests, object.__new__
    stubs): state path None → helper runs in-memory-only, ZERO disk I/O.
- Single-writer guarantee: fleet_runner pre-launch validation (~:193-219) enforces
  explicit unique client_ids spaced ≥2; restarted child re-resolves the same cid → same
  file → continuity across SystemExit. (cid+1 is the exec connection inside the SAME
  process, not a second writer.)
- All reads/writes individually try/except-wrapped; corrupt/missing/unwritable degrades
  to in-memory-only; never blocks or delays the escalation path.

### 5. Strict-Locked test evolutions — TDD-TESTER (lock owner) ONLY, same change
Authorization: this ticket's user directive (threshold 15→30) is the pin-invalidating
event; Impact-Reviewer approved each evolution below. Coder MUST NOT touch these files.
- `tests/test_session_watchdog_rollover.py`:
  (a) `test_stale_threshold_pin_15` (~:1313-1325): assert `== 30`, rename `..._pin_30`,
      docstring cites the 2026-07-06 directive;
  (b) `test_cl_open_hours_16min_stale_true` (~:1366-1372): vector 16→31 min (rename) +
      NEW 29-min → False boundary companion (10-min False test unchanged);
  (c) `test_zc_grace_expires_after_threshold` (~:507-517): query instant Tue 08:50 CT →
      09:05 CT (35 min past the 08:30 anchor), docstring updated. All other
      session-anchor pins (vectors ~68 min / Fri→Sun, ZC halt gate) byte-untouched.
- `tests/test_shallow_5m_bootstrap.py`: `test_es01b_watchdog_anchors_5m_15min`
  (~:632-655): 16→31 min vector, docstrings "15-min"→"30-min"; 10-min False leg unchanged.
- `tests/test_hourly_only_equity_session.py`: `test_5m_enabled_16min_stale_true_pin`
  (~:923-931): 16→31 min, docstring 15→30. The `== 135` pin (~:867) and 10-min False pin
  (~:933) byte-unchanged. Optional comment-only annotation of the "legacy 15-min margin"
  prose (~:864-866/~:889-892) as design-time-historical.
- `tests/test_reconnect_recovery_fixes.py`: add
  `@patch.object(lt_module, "_WATCHDOG_TG_COOLDOWN_SECONDS", 0)` to
  `test_r3_reconnect_attempt_counted_failed_when_subscribe_raises` (~:678) and
  `test_r4_third_consecutive_fruitless_firing_escalates` (~:751) (optionally also
  `test_r4_telegram_failure_never_blocks_escalation_exit` ~:780 for intent clarity), each
  with a one-line docstring note "throttle disabled here — throttle behavior is pinned in
  tests/test_watchdog_telegram_throttle.py"; ALL original assertions byte-preserved.
  Comment-only fix at ~:806-808 ("15-min" → "30-min"). Constant pin `== 3` (~:746-749)
  and counter-reset/fence vectors (60/50/45/40/35 min — all > 30) unchanged.

### 6. NEW `tests/test_watchdog_telegram_throttle.py` (unlocked; Tester authors red-first,
Coder implements to green). Scenarios:
1. Constant pins: `_WATCHDOG_TG_COOLDOWN_SECONDS == 3600`, `_STALE_BAR_THRESHOLD_MINUTES == 30`.
2. First fire on a fresh instance sends exactly once (in-memory, no state path).
3. Second fire within the hour: no send; INFO suppression record contains the suppressed
   message text; `_check_stale_bars` still returns True and still disconnects.
4. Escalation within cooldown: SystemExit still raised, CRITICAL log still emitted,
   Telegram suppressed + INFO logged.
5. Fire > 1 hour after last send: sends with "+N suppressed" suffix; counter resets.
6. Persistence round-trip via the cid-keyed derivation: stub with client_id + db_path
   under tmp_path → T0 send writes `watchdog_tg_cid{N}.json` beside the db; NEW stub
   (simulated fleet_runner restart, same cid/db_path) suppresses at T0+30 min; sends at
   T0+61 min.
7. Corrupt state file → treated as no-state, sends, never raises.
8. Unwritable state path → send still succeeds, never raises.
9. Missing state-path attr (`object.__new__` stub) → in-memory-only, no crash.
10. `telegram.send` raising inside the helper → helper returns normally; budget still
    consumed (next fire suppressed).
11. Per-cid isolation: two stubs, one tmp data root, distinct cids (e.g. 1010/1012) →
    distinct sidecar filenames; A's T0 send does NOT suppress B's first send at T0+1 min.
12. client_id-None degrade: throttle works in-memory; NO file created anywhere under the
    tmp data root; never raises.

### 7. Invariants (must hold, verified by the suite)
- Helper never raises/blocks; watchdog/reconnect/escalation return values, counter
  arithmetic, and send→`raise SystemExit` ordering byte-identical.
- No log line throttled anywhere; suppressed messages fully visible at INFO.
- `TelegramAlerter`, `fleet_runner.py`, `cli.py` untouched; no signature changes.
- Zero disk I/O when client_id is None (livetest/backtest/tests unaffected).
- Lands atomically: live_trader.py + locked-file evolutions (Tester) + new test file in
  one change; FULL suite green before done (the live fleet runs from this tree and picks
  the change up on each child's next restart).

### 8. Disclosed residuals (accepted; NOT in scope)
- Startup banners stay unthrottled → ~1 "LiveTrader Online" per escalation restart
  (~45-75 min cadence at the new threshold); separate follow-up if still noisy.
- `*RECONNECT FAILED*` stays unthrottled by explicit user-family exclusion.
- During a Telegram outage a budget slot can be spent on a failed delivery (log retains
  everything).
- A manually-launched duplicate cid outside fleet_runner is unvalidated, but IBKR
  error-326 rejects duplicate client ids at the gateway (no practical dual-writer).
