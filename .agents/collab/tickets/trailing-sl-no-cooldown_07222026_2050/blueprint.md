# trailing-sl-no-cooldown_07222026_2050 — Only the original SL arms the re-entry cooldown

**Operator decision (2026-07-22 ~20:45 PT):** the post-exit re-entry cooldown
should arm ONLY on an original stop-loss exit. Trailing-stop exits (profit
locking), TP exits, time-barrier exits, and EOD/weekend flattens must NOT
block re-entry. Validation mode: **backtest-first** — implement in both
engines, re-run the 5 fleet configs, present before/after; live deploy is a
separate operator decision after seeing the numbers.

## Rationale / risk

The cooldown gate went flavor-blind in cooldown-single-authority-wiring
(c031d96, live 19:10 PT 07-22) to mirror the backtest. The operator observed
SI/ES/GC short-side blocks arming from *profit-taking* trailing exits and
wants losers-via-original-SL to be the only trigger. Risks:
- Optuna chose per-side cooldown_bars (CL 1/1, ES 13/13, NG 3/5, GC 1/13,
  SI 1/11) under flavor-blind semantics — the study re-validates them under
  the new rule before anything touches live.
- TRAILING_BE is *usually* profit but can be ~break-even or slightly negative
  after costs (ladder rung 1); accepted by the operator's rule.
- MUST land backtest+live in one change or live out-trades the backtest.

## Semantics (exit reason -> arms cooldown?)

| Exit                              | Backtest reason      | Live reason        | Arms? |
|-----------------------------------|----------------------|--------------------|-------|
| Original SL                       | ExitReason.SL        | SL_HIT (untrailed) | YES   |
| OOB-discovered SL                 | —                    | SL_HIT_OOB (untrailed) | YES |
| Trailing SL (trail activated)     | ExitReason.TRAILING_BE | SL_HIT + _trailing_activated -> map to TRAILING_BE | no |
| TP                                | ExitReason.TP        | TP_HIT / TP_HIT_OOB | no  |
| Time barrier                      | ExitReason.TIME_BARRIER | TIME_BARRIER    | no    |
| Signal/conflict exit              | ExitReason.SIGNAL_EXIT | (engine)         | no    |
| EOD / weekend flatten             | EOD/WEEKEND_FLATTEN  | (flatten reasons)  | no    |
| OOB/manual/unknown close          | —                    | CLOSED_OOB etc.    | no    |

Rule of thumb: arm iff the reason is the SL family AND the trail never
activated. Unknown/None reason -> do NOT arm (it is not a proven original-SL).

## Change sites

1. **agent/backtest_engine.py** — `last_exit_bars_ago_<side> = 0` resets at
   ~775/777 (`_close_trade`) and ~1217/1219 (tranche final-event path): make
   conditional on `exit_reason == ExitReason.SL`. The booked reason already
   distinguishes (TRAILING_BE iff trailing_rung > 0, lines 984/992/1070/1357/1365).
2. **src/live_execution/live_trader.py `_reset_position_state`** (~1343):
   map the reason before `strategy.on_exit`: if `self._trailing_activated`
   and reason is SL-family, pass "TRAILING_BE" (flag is still live; the
   reset to False happens after the call). Ledger/tradebook strings UNCHANGED.
3. **ConfigurableStrategy.on_exit** (~593): set `_last_exit_bars_ago_<side> = -1`
   only for SL-family reasons ("SL_HIT", "SL_HIT_OOB", ExitReason.SL, "SL").
   KEEP forwarding every exit to `_exec_strategy.on_exit` unconditionally
   (per-side open/close tracking must not change).
4. **Restart recovery** — `_seed_restart_cooldown` (~2525): return without
   arming when the reason is exempt (it currently calls on_exit AND
   hard-overwrites the counter, so the filter must live here too).
   `_reconstruct_cooldown_from_ledger` (~2578): rows carry exit reason and
   the ledger persists `trailing_activated` (telemetry.py:170); skip rows
   whose reason is exempt or whose trailing_activated=1. If
   `get_recent_closed_positions` doesn't select those columns, add them to
   the SELECT (read path only).
5. **Coherence (non-fleet strategies):** IsolatedAsymmetrical/JointPortfolio
   keep private `_bars_since_*_exit` counters in their own on_exit — apply the
   same reason filter for consistency (not used by the fleet tiered ensembles).

## Tests (new file tests/test_trailing_sl_no_cooldown.py + engine tests)

- ConfigurableStrategy.on_exit: SL_HIT arms; TRAILING_BE / TP_HIT /
  TIME_BARRIER / CLOSED_OOB / None do not; forwarding to exec strategy happens
  for ALL reasons.
- Engine: trade closed by original SL -> next-bar re-entry blocked for
  cooldown_bars; closed by trailing (rung>0) -> next-bar re-entry allowed;
  TP -> allowed; barrier -> allowed. Tranche path (1217/1219) same.
- Restart: _seed_restart_cooldown with TRAILING_BE stays inert;
  ledger reconstruction skips trailing_activated=1 SL rows and arms
  untrailed SL rows.
- Re-adjudicate any existing test that pins flavor-blind arming (e.g.
  parity tests asserting TP exits arm) — expected-behavior change, document
  in the test docstring with this ticket id.

## Study (before any live deploy)

Re-run the 5 fleet configs' backtests old-rule vs new-rule (same data, same
seeds): net PnL, Sharpe, MAR, trade count, per-side splits; full period +
holdout window. Deliverable: comparison table + go/no-go recommendation.
Live deploy only on operator approval after the study; deploy = operator
fleet restart.

## Status

- [x] Blueprint approved (operator, 2026-07-22: rule = only original SL;
      backtest-first)
- [ ] Implementation (both engines + restart recovery)
- [ ] Tests green (new + full fast suite, delta-clean vs baseline)
- [ ] Study results presented
- [ ] Operator go/no-go on live deploy
