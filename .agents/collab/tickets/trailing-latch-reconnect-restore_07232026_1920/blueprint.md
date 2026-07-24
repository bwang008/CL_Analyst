# trailing-latch-reconnect-restore_07232026_1920 — stop losing _trailing_activated mid-trade

**Operator approval 2026-07-23 ~19:20 PT.** Evidence (memory:
trailing-latch-lost-on-reconnect): `_check_trailing_stop` latches
(`if self._trailing_activated: return`), yet NG order-110's trade re-fired
"TRAILING STOP: activated" at 19:15 (explained: 19:10 fleet restart —
recovery does NOT restore the flag) and AGAIN at 23:00 on 2026-07-22 with
NO restart — the latch was lost across the 21:04-21:22 nightly reconnect.
Only two `= False` sites exist (close reset in _reset_position_state;
entry-fill init in _on_standard_execution_event), so a reconnect path is
re-running one of them (or fresh state seeding) mid-trade.

Why it matters beyond noise: at exit-fill time
`_reset_position_state` remaps a trailed SL_HIT -> TRAILING_BE using this
flag ([[trailing-sl-no-cooldown]] lineage). A falsely-False flag books a
profit-lock trailing exit as plain SL_HIT -> wrongly arms "sl_only"
cooldowns and mis-stamps the ledger `trailing_activated` column (which
restart cooldown reconstruction then trusts). The Phase-3 skip-guard
re-latch (34bf260) repairs the flag only at the NEXT trigger-true bar — a
fill in the gap window still books wrong.

## Part 1 — ROOT CAUSE (investigate before fixing; evidence in the repo)

Fleet log reports/fleet/fleet_20260722.log, NG cid=3000, window
21:04-21:22 PT (the flap) and up to 23:00. Leading hypothesis to
verify/refute: on reconnect ib_insync replays execDetails/orderStatus; a
replayed ENTRY fill event re-enters the entry-fill branch (dedup miss —
check `_processed_entry_order_ids` lifetime across reconnects and how the
replayed event's order id/type reaches the router) and re-runs the
position-init block (`self._trailing_activated = False`,
`_position_bars_held = 0`, ...). Alternative candidates: the reconnect
backfill/recovery path re-seeding position state. Document the proven
chain with log line citations in the ticket dir (findings.md).

## Part 2 — Fix (scope to what the root cause proves; likely BOTH)

1. Whatever mid-trade re-init path fired: make it idempotent (dedupe the
   replayed fill / guard the init so an already-tracked position is not
   re-seeded). No blanket suppression — a REAL new entry fill must still
   init.
2. Restore-on-recovery: startup/reconnect position recovery restores
   `_trailing_activated` (and `_tracked_sl_price` if not already) from the
   ledger row's `trailing_activated`/`sl_price` columns (telemetry
   persists both; get_open_position returns the row). Absent column ->
   False (legacy rows), never a silent invented True.

## Tests (TDD, tests/test_trailing_latch_persistence.py)

- Replayed/duplicate entry-fill event mid-trade does NOT reset
  _trailing_activated / _position_bars_held (and a genuinely new entry
  still does init).
- Recovery of an OPEN ledger row with trailing_activated=1 restores the
  flag (and tracked SL from sl_price); =0/absent leaves False.
- Regression fence: the exit-fill remap books TRAILING_BE when the
  restored flag is True (ties to tests/test_trailing_sl_no_cooldown.py
  patterns).

## Hard constraints (same as the parallel worktree ticket)

- ISOLATED GIT WORKTREE: commit there on its branch; report branch + sha;
  no push/merge; never touch configs/ or .agents/collab/error_queue/.
- A PARALLEL agent is editing the ROLLOVER/fill-booking seams of
  live_trader.py — keep your diff surgical to reconnect/recovery/entry-
  dedup + the restore path.
- Trader env pytest; full fast suite baseline BEFORE, delta-clean after;
  stub repairs / re-adjudications cite this ticket id; no cheap fixes.
- Commit: no-BOM file + git commit -F; subject
  `fix(trailing-latch-reconnect-restore_07232026_1920): <summary>`; body
  includes "deploy pending operator fleet restart".

## Definition of done
findings.md (root-cause proof w/ log citations) + fix + tests green +
suite delta-clean + worktree commit. Report: root cause in two sentences,
files, test counts, branch + sha, deviations w/ reasoning.
