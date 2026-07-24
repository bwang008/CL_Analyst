# trailing-latch-reconnect-restore_07232026_1920 — findings (root-cause proof)

Investigator: worktree agent, 2026-07-23. Evidence source:
`reports/fleet/fleet_20260722.log` (NG cid=3000; log timestamps PT,
bar timestamps UTC). Line numbers below cite that file.

## Verdict

**The trailing latch was NEVER lost mid-session.** Both spurious
re-activations trace to **process restarts whose ledger recovery does not
restore `_trailing_activated`** (`_recover_inherited_position`,
live_trader.py:2684-2861, restores everything else). The leading
hypothesis — reconnect execDetails replay re-entering the entry-fill init
and clearing the flag mid-trade — is **REFUTED** by the log and by code
structure. The blueprint premise "at 23:00 with NO restart" is factually
wrong: the log shows a second fleet restart at 22:55:59 -> 22:57:09.

A second, unanticipated gap: the restore target column does not exist.
`active_positions` has **no `trailing_activated` column** in either schema
(telemetry.py:184-207 legacy, 288-316 fleet v2) — the column cited from
telemetry.py:170 belongs to `decision_state_log`. Nothing ever persists
the latch onto the position row, and the two already-shipped readers
(live_trader.py:2634 cooldown reconstruction, :2763 OOB remap, from
trailing-sl-no-cooldown_07222026_2050) silently always read None. The
restore-on-recovery fix therefore requires adding the column + a write at
activation time.

## Trade timeline (trade_108, NG LONG 1 @ 2.905, order 108; SL order 110)

| PT time | line | event |
|---|---|---|
| 06:00:11 | 1070 | `[TRADE] ENTRY FILLED: orderId=108 action=BUY fill=2.90 qty=1` — the ONLY entry-fill line all day |
| 14:15:06 | 2476-2600 | process start (API connect cid 3000/3001) — session that carries the trade into the evening |
| 18:25:06 | 3418, 3421 | genuine first activation: `TRAILING STOP: activated — entry=2.90 ATR=0.0193 ... new_SL=2.94`; `modified SL order 110: 2.85 -> 2.94` (real price change) |
| 19:10:48 | 3547 | `Received signal 2 — stopping` (operator fleet restart) |
| 19:12:00 | 3837, 3841-3851 | process start; API reconnect |
| 19:12:10 | 3912-3914 | `[RECOVERY] Restoring position from ledger: trade_id=trade_108 ...`; 12 bars held; `TP/SL verified on IBKR: TP orderId=109 (3.06) SL orderId=110 (2.94)` — **no latch restore** |
| 19:15:06 | 4112, 4115 | spurious re-activation #1 at the FIRST 5M bar close after restart: `modified SL order 110: 2.94 -> 2.94` (no-op) |
| 21:04:32-21:22:16 | 4406-4906 | the "nightly reconnect" flap: farm/TWS codes 2105/2103/1100 + restore 2106/2104/1102, three resubscribe cycles, backfills "no new bars". **No API socket reconnect** (no `Connecting to 127.0.0.1:4002` between lines 3851 and 5443) and no exec replay |
| 21:25:06-22:55:06 | 4914-5141 | process stays up, 5M bars close every 5 min, trigger condition TRUE throughout — and **no activation line fires**: the latch is still True |
| 22:55:59 | 5149 | `Received signal 2 — stopping` (second fleet restart) |
| 22:57:09 | 5439, 5443-5453 | process start; API reconnect |
| 22:57:17 | 5514-5516 | `[RECOVERY] Restoring position from ledger: trade_id=trade_108 ...`; 15 bars held; TP 109 / SL 110 verified — again no latch restore |
| 23:00:05 | 5715, 5716-5718 | spurious re-activation #2 at the FIRST 5M bar close after restart: `MODIFY ORDER: re-placing orderId=110 auxPrice=2.937`; `modified SL order 110: 2.94 -> 2.94` (no-op) |

(The 05:10:06 activation at line 903 belongs to the PREVIOUS trade,
order 107, entry 2.87.)

## Proof the latch survived the 21:04-21:22 reconnect

`_check_trailing_stop` runs on every 5M bar close (activation lines all
sit seconds after `NEW 5M BAR` lines; pinned by
test_trailing_stop_5m_scheduling.py). Its trigger uses the frozen entry
ATR and the monotonic `_highest_high` accumulator:

- threshold = entry + 2.40 x ATR_entry = 2.905 + 2.40 x 0.0193 = **2.9513**
- market from 21:00 onward: 2.959-2.98 (PNL line 4387 `mktPrice=2.959`;
  5M bars H=2.96-2.98, lines 4914, 5119, 5141), i.e. the trigger
  condition was continuously TRUE after the flap ended.

Had the flap cleared `_trailing_activated`, the very next 5M bar close
(21:25:06, line 4914) would have re-logged `TRAILING STOP: activated`
exactly as restarts did at 19:15:06/23:00:05. Nothing fired for the
93 minutes the process remained up (lines 4914-5141). The only gate that
silences the check while the trigger is true is the latch itself
(live_trader.py:1589 `if self._trailing_activated: return`). Therefore
the latch was True across all three reconnect cycles and was lost only at
process death.

## Why the execDetails-replay hypothesis is structurally impossible

1. Mid-session: `_on_standard_execution_event` dedups every Filled event
   against `_processed_entry_order_ids` (live_trader.py:6876-6877), which
   is populated at the first entry fill (:7059) and never cleared during
   a session — a replayed order-108 fill returns before any state write.
2. Across restarts: the entry-init block is reachable only when
   `str(order_id) in self._entry_order_ids` (:6938-6939), a set populated
   at order SUBMISSION (:5887). A fresh process never submitted order 108,
   so a replayed fill lands in the `UNRECOGNIZED FILL ... ignoring
   (position state unchanged)` branch (:7050-7057).
3. Empirically: exactly one `ENTRY FILLED` line exists all day (1070),
   and API sockets only ever (re)connected at process starts
   (2476/2556/2589, 3841/3848, 5443/5450) — there was no socket-level
   reconnect during 21:04-21:22 for ib_insync to replay into.

## Root-cause chain (proven)

1. `_check_trailing_stop` latches once per trade (:1589) and persists the
   trailed SL price via `update_position_sl` (:1709) — but the latch
   itself is persisted nowhere on the position row (no column exists).
2. On restart, `_recover_inherited_position` (:2684) restores trade_id,
   entry, ATR, side, per-trade overrides, bars_held, TP/SL ids + prices
   (`_tracked_sl_price` via `_verify_and_heal_protective_legs`, :2907) —
   `_trailing_activated` keeps its `__init__` False (:741).
3. First 5M bar close after restart: trigger still true, latch False ->
   spurious "activated" log + redundant broker modify (no-op re-place of
   order 110), and — the real risk — until that bar fires, an SL fill
   books plain `SL_HIT` instead of `TRAILING_BE` (:1356-1357), wrongly
   arming "sl_only" cooldowns and mis-stamping downstream records.

## Fix scope justified by the evidence

- **Restore-on-recovery (blueprint Part 2 item 2): required.** Restore
  `_trailing_activated` from the ledger row in
  `_recover_inherited_position`; absent column / 0 / NULL -> False, never
  an invented True. `_tracked_sl_price` is already restored (:2907) from
  `sl_price`, which `update_position_sl` keeps at the trailed value — no
  change needed there.
- **Persistence prerequisite (blueprint deviation — premise gap):** add
  `trailing_activated INTEGER DEFAULT 0` to `active_positions` (legacy +
  fleet v2 DDL + `_migrate_active_positions_columns`, and run that
  migration on the fleet init path, which today skips it), and stamp it
  TRUE in the same `update_position_sl` call that persists the trailed SL
  (atomic with the price update). This also makes the two already-shipped
  readers (:2634, :2763) functional instead of silently None.
- **Entry-fill idempotency (blueprint Part 2 item 1): no production
  change.** The dedup + submission-gate already make the replayed-fill
  re-init unreachable (see above); the ticket adds regression-fence tests
  pinning both behaviors so a future refactor cannot open the hole the
  blueprint feared.
