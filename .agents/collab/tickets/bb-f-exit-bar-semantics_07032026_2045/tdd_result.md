# TDD Result — bb-f-exit-bar-semantics_07032026_2045

**Outcome:** ✅ **PARITY: PASS** — the 336-bar trailing-symmetric replay reconciles PERFECTLY:
```
trades: backtest=15  livetest=15  matched=15   (bt_only=0, lt_only=0)
exact-cent: 15/15   exit mapping: 15/15   sides: 15/15
max per-trade PnL delta: $0.00
total PnL: backtest=$1,695.01  livetest=$1,695.01  delta=$0.00
RECONCILE EXIT CODE: 0
```
The ledger-parity workflow is now a true PASS/FAIL regression gate.

## Human authorization
> "Authorize B(b)+F: backtest adopts live's intrabar SL/TP-before-barrier precedence, live adopts
> backtest's exit-bar evaluation semantics, accepting that historical backtest metrics shift and
> ensembles may need re-scoring." (2026-07-03)

## Commits (branch ticket/bb-f-exit-bar-semantics)
- `6d6a421` — B(b) precedence flip in BOTH backtest paths (`_on_in_position`, concurrent
  `_check_position`): TP/SL breach evaluated before the time barrier (pessimistic SL-over-TP and
  barrier-at-bar-open preserved). F(1) `on_exit` resets counter to -1 (exit-bar evaluate reads 0 =
  backtest). F(2) harness flushes exit-fill callbacks before the bar's evaluation. F(3)
  `_on_new_bar` no longer skips evaluation on time-barrier exit bars. 12 new tests
  (`tests/test_exit_bar_semantics.py`); 4 superseded tests updated to the authorized convention.
- `c3a3cff` — discovered by the first replay (15/15 exact-cent but 2 lt-only next-bar re-entries
  after TP): evaluate()'s gate now enforces the UNION `max(flavored tp/sl cooldown, per-side
  cooldown_bars)` — the backtest applies cooldown_bars via the TieredEnsemble re-gate with real
  counters, which the 9999 sentinel had silently disabled on the live side. +1 test; 1 superseded
  test rewritten.

## Historical backtest impact (replay-window sample, authorized)
Old vs new precedence on the identical 336-bar window: **1 of 15 trades changed** — the 05-26
06:00 LONG flipped TIME_BARRIER (+$1,115) → TP (+$2,615); entries, trade count, and all other
exits identical; window total +$1,500. No cooldown cascade in this window. NOTE: this is a small
sample — full-history backtests will contain more same-bar-conflict trades; **re-score ensembles
(HS14A/14B/15A) before relying on pre-change metrics** (follow-up flagged, not in this ticket).

## Full suite
764 passed / 0 failed (fast suite, `trader` env, worktree).

## Remaining known-open
Only **C** (sub-tick trailed-SL residuals) — unmeasurable in the 1h harness; owned by the future
5m-harness ticket. The `--disable-trailing` setup flag (commit `21b7ada`) is the documented way
to run this workflow until then.
