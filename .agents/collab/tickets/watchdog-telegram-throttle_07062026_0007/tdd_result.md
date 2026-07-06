# TDD Result — watchdog-telegram-throttle_07062026_0007

**Outcome: PASS (Green).** Full fast suite: **1519 passed / 0 failed** (no
`slow`-marked tests exist in the repo, so this run IS the complete suite).
Red baseline before implementation: 18 failed / 1501 passed — the failures
were exactly this ticket's red set (13 new-file tests + evolved locked pins +
the three cooldown-patch targets), zero collateral breakage. Do NOT commit —
the Ticket-Manager handles commit/attribution.

## Files changed

### Implementation (TDD-Coder) — `src/live_execution/live_trader.py` ONLY
- `:149-154` — `_STALE_BAR_THRESHOLD_MINUTES = 15` → `30`; trailing comment
  cites the ticket + the accepted trade-off (doubled blind window before
  recovery; bracket TP/SL rest server-side on IBKR). `_STALE_BAR_THRESHOLD_MINUTES_1H = 135`
  and `_MAX_FRUITLESS_RECONNECTS = 3` untouched; session-anchor arithmetic
  byte-identical.
- `:167-175` — NEW module constant `_WATCHDOG_TG_COOLDOWN_SECONDS = 3600`
  (patchable seam; tests set 0 to disable — suppression uses strict `<`).
- `:411-426` — `__init__`, immediately after the telemetry identity block:
  `client_id is not None` → `self._watchdog_tg_state_path =
  Path(db_path).with_name(f"watchdog_tg_cid{client_id}.json")` (one sidecar
  per instance, sibling of the SHARED fleet_telemetry.db — Reviewer R1);
  `client_id is None` → `None` → in-memory-only, zero disk I/O.
- `:4128-4225` — NEW `LiveTrader._send_watchdog_telegram(msg)` adjacent to
  `_check_stale_bars`: clock = `datetime.now(timezone.utc)` (module import —
  frozen-clock test seams control it); getattr-seam state + lazy ONE-time
  sidecar hydration (corrupt/missing/unreadable → no-state); SUPPRESS when
  `elapsed < _WATCHDOG_TG_COOLDOWN_SECONDS` → count++, best-effort persist,
  `log.info("TELEGRAM SUPPRESSED (watchdog-family cooldown, %.0fm remaining,
  %d suppressed this window): %s", ...)` with the FULL message text; SEND
  otherwise with `(+N watchdog-family alerts suppressed in the last hour —
  see log)` suffix when N>0; ATTEMPT CONSUMES BUDGET (timestamp recorded
  regardless of send outcome); helper never raises (send and each persistence
  I/O separately wrapped).
- Exactly FIVE send sites converted to the helper:
  - `:3782` `*RECONNECT*` first attempt (`attempt == 1` gate kept)
  - `:3850` `*RECONNECT*` farms-broken (`attempt % 3` gate kept)
  - `:3871` `*RECONNECTED*`
  - `:4305` `*WATCHDOG ESCALATION*` — helper, INCLUDING its persistence
    write, completes BEFORE `raise SystemExit` (:4314)
  - `:4323` `*STALE BAR WATCHDOG*`
- NOT converted (verified by grep): all three `*RECONNECT FAILED*` sites
  (:3917/:3965/:3986), SAFETY MUTE (:4100/:4122), cache-validation, startup
  banners, 1-hour heartbeats, all trade/rollover/macro sends.
  `TelegramAlerter`, `fleet_runner.py`, `cli.py`, session_calendar, configs
  untouched. No signature changes.

### Tests (TDD-Tester; lock owner for the four Strict-Locked files)
- NEW `tests/test_watchdog_telegram_throttle.py` — 13 tests covering
  blueprint §6 scenarios 1-12 (scenario 1 split into two constant pins).
  Verified RED first: all 13 failed on missing constant/helper/state-path
  and un-throttled send counts; zero collection errors.
- `tests/test_session_watchdog_rollover.py` (Strict-Lock evolution, §5):
  `test_stale_threshold_pin_15` → `test_stale_threshold_pin_30` (== 30,
  directive cited); `test_cl_open_hours_16min_stale_true` →
  `test_cl_open_hours_31min_stale_true` (vector 31) + NEW
  `test_cl_open_hours_29min_stale_false` boundary companion (10-min False
  test unchanged); `test_zc_grace_expires_after_threshold` query instant
  Tue 08:50 → 09:05 CT (35 min past the 08:30 anchor). All other
  session-anchor pins byte-untouched.
- `tests/test_shallow_5m_bootstrap.py` (§5):
  `test_es01b_watchdog_anchors_5m_15min` vector 16→31 min, prose
  15-min→30-min; 10-min False leg unchanged. (Function name retained —
  a rename was not in the §5 authorization for this file.)
- `tests/test_hourly_only_equity_session.py` (§5):
  `test_5m_enabled_16min_stale_true_pin` vector 16→31, docstring 15→30
  (name retained per §5 scope); `== 135` pin and 10-min False pin
  assertions byte-unchanged; optional comment-only design-time-historical
  annotations added to the "legacy 15-min margin" prose.
- `tests/test_reconnect_recovery_fixes.py` (§5):
  `@patch.object(lt_module, "_WATCHDOG_TG_COOLDOWN_SECONDS", 0)` + one-line
  docstring note added to `test_r3_reconnect_attempt_counted_failed_when_subscribe_raises`,
  `test_r4_third_consecutive_fruitless_firing_escalates`, and (optional,
  authorized for intent clarity) `test_r4_telegram_failure_never_blocks_escalation_exit`;
  ALL original assertions byte-preserved; comment-only "15-min"→"30-min" fix
  in `test_r4_new_5m_brain_bar_resets_counter_to_zero`; `== 3` pin and the
  60/50/45/40/35-min fence vectors untouched.

## Test counts
| Run | Result |
|---|---|
| Red — new file | 13 failed (right reasons: missing constant/helper, 15≠30, un-throttled counts) |
| Red — full suite | 18 failed / 1501 passed (exactly the ticket's red set) |
| Green — new file | 13 passed |
| Green — 4 evolved locked files | 159 passed |
| Green — FULL suite (`-q`, no `-x`, `-m "not slow"`) | **1519 passed / 0 failed** |

## Deviations from the blueprint
None functional. Two documented judgment calls inside §5 scope:
- Kept the historical function names `test_es01b_watchdog_anchors_5m_15min`
  and `test_5m_enabled_16min_stale_true_pin` (renames were authorized ONLY
  for the rollover file's threshold pin/vector tests; the "nothing more"
  lock rule governs). Docstrings in both note the evolution and cite the
  ticket.
- The helper uses a nested best-effort `_persist()` closure rather than a
  second class method, keeping the public surface to exactly the one new
  method the blueprint specifies.

Note: `READBEN.me` shows as modified in the working tree — that edit
(user scheduling notes for the error watcher) predates/parallels this
ticket, is unrelated, and was NOT touched by this work.

## Runtime behavior summary (operator)
- Stale-bar watchdog for 5m-enabled instances now fires at **30 min** (was
  15). Hourly-only instances keep 135 min. Recovery machinery (disconnect,
  reconnect, 3-strike SystemExit escalation) is byte-identical in behavior —
  only Telegram noise changed.
- Watchdog-family Telegram (STALE BAR WATCHDOG / WATCHDOG ESCALATION /
  RECONNECT / RECONNECTED) is now **at most 1 message per hour per fleet
  instance**; swallowed alerts appear on the next send as
  "(+N watchdog-family alerts suppressed in the last hour — see log)".
- Every suppressed message remains FULLY visible in the log at INFO:
  "TELEGRAM SUPPRESSED (watchdog-family cooldown, Xm remaining, N suppressed
  this window): <full text>". No log line is throttled.
- The hourly budget survives escalation restarts via
  `<data_root>/watchdog_tg_cid<client_id>.json` beside fleet_telemetry.db
  (one per instance; corrupt/unwritable degrades to in-memory-only, never
  blocks). livetest/backtests (client_id None) do zero disk I/O.
- RECONNECT FAILED, SAFETY MUTE, startup banners and heartbeats are
  deliberately NOT throttled (startup-banner noise ~1 per escalation restart
  is a disclosed residual, §8).
- The running 4-model fleet picks the change up on each child's next
  restart. NOT committed — Ticket-Manager owns commit/attribution.
