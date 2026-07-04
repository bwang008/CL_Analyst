# TDD Result — parity-exit-signal_07022026_1930

**Outcome:** ✅ GREEN — full fast suite `conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"`: **740 passed, 0 failed** (2026-07-03). Red-phase baseline was 726 passed / 14 failed (11 = this ticket's new tests; 3 = parallel ticket `trailing-stop-log-type-error_07022026_2225`, resolved by that ticket's own coder mid-flight).

## Tests added (Strict-Lock: TRUE)
- `tests/test_parity_cooldown_single_authority.py` — 8 tests: pre-gate counter increment (backtest start-of-bar convention), 9999 `EngineState` sentinel neutralizing the `TieredEnsembleStrategy` re-gate, SHORT→cooldown-release→LONG boundary released on exactly the 8th bar (vs real `TieredEnsembleStrategy`, HS14B-like config), `_parity_on_exit` removal scan, per-side counter-advance preservation.
- `tests/test_parity_sltp_fill_basis.py` — 7 tests: backtest static SL/TP derived from slippage-adjusted entry fill with single 2dp penny rounding (long/short/per-trade-override/penny-grid), plus 3 scope-guard preservation tests (raw entry price, 1-tick `_apply_slippage`, ATR/side/lots unchanged).

## Implementation changes (TDD-Coder)
- `src/live_execution/strategies/configurable_strategy.py` — Phenomenon A: counter increment relocated above the cooldown gate; `EngineState` fed sentinel `last_exit_bars_ago_*=9999` so `evaluate()` is the sole cooldown authority. `execution_models.py` untouched per blueprint.
- `scripts/livetest_engine.py` — Phenomenon A: `_parity_on_exit` compensating monkey-patch machinery deleted (comment block, `orig_on_exit` capture, def, install). PARITY FIX 1 alias preserved.
- `agent/backtest_engine.py` — Phenomenon B(a): static SL/TP in `_on_flat` (and the concurrent-mode `_open_new_position` for engine consistency) now `round(entry_fill ± mult*atr, 2)` — slippage-fill basis, single penny rounding, matching live `live_trader.py:1622/1635`. Entry logic, `_apply_slippage`, ATR, sizing, TIME_BARRIER precedence (:688) unchanged.

## Caveats / recommended follow-up validation (not part of the unit-test gate)
1. **End-to-end correctness anchor not yet executed:** the blueprint's replay anchor (336-bar Parity-Mode livetest → 18 trades matching backtest, 05-28 SHORT at 13:00, 05-29 follow-on LONG taken, 06-02 static-SL gap closed to ≤$5) requires running `scripts/livetest_engine.py` against the HS14B_Sharpe_E01 config — schedule this integration validation next.
2. **Blueprint item A.4 (reconciler labeling)** — "cooldown-gated skip must not be mislabeled SIGNAL_EXIT" was not covered by a dedicated unit test; verify during the replay validation above.
3. Historical backtest PnL on SL/TP-exit trades shifts ≤ ~1 tick (authorized/accepted per blueprint); regenerated reports will differ accordingly. `_open_new_position` (concurrent mode) received the same basis change — check concurrent-mode ledgers if any downstream numbers move unexpectedly.

## Audit trail
Per-ticket logs in this folder: `tdd_status.md`, `tdd_audit_log.md`, `ticket_audit_log.md` (migrated 2026-07-03 from former shared collab logs).

---

## ADDENDUM 2026-07-03 — 336-bar replay validation (follow-up #1) executed

Ran `/validate-ledger-parity` (setup → livetest harness → reconcile) on the replay window.

**Run 1 (original config, trailing on):** FAIL as expected — all 9 violations were backtest
`TRAILING_BE` exits. Post-`922dea5` the 1h harness cannot trail (5m callback never fires), so
trailing-dependent trades cannot reconcile here (known limitation, pitfall #3). Not attributable
to this ticket.

**Run 2 (trailing disabled symmetrically — `trailing_atr_mult=10000` in parity config; livetest
ledger reused since it is trailing-invariant in this harness):**
```
trades: backtest=15  livetest=22  matched=15   (bt_only=0)
exact-cent matches: 14/15   entry_fill delta: $0.0000   side match 15/15
```
**Verdict on this ticket's scope:**
- **Phenomenon B(a) VERIFIED** — backtest and livetest bracket prices identical to the cent
  (e.g. 05-28 08:00 LONG: TP 92.87 / SL 88.27 in BOTH ledgers); 14/15 exact-cent matches.
- **Phenomenon A VERIFIED at entry level** — every backtest trade matched bar-for-bar with
  $0.0000 entry deltas. (The blueprint's 05-28 13:00 SHORT anchor is unreachable in the 1h
  harness: it only exists downstream of a backtest TRAILING_BE exit; with trailing symmetric,
  both engines identically skip it. Unit tests pin the boundary semantics instead.)

**Residual divergences discovered (OUT of this ticket's authorized scope — need new tickets):**
1. **(R1) Exit-reason vocabulary mismatch** — `LiveTrader._reset_position_state()` defaults to
   `reason="CLOSED"` (time-barrier and OOB-cleanup exits), but the evaluate() cooldown flavor
   tuple (`configurable_strategy.py` ~:423) recognizes only `SL_HIT/TIME_BARRIER/REVERSE` →
   time-barrier exits get tp_cooldown (0) instead of sl_cooldown (7). Previously masked by the
   TieredEnsembleStrategy re-gate's blanket per-side cooldown, now exposed by the 9999 sentinel.
2. **(R2) Exit-bar off-by-one at cooldown=0** — backtest blocks re-entry on the exit bar itself
   (counter 0 ≤ 0); live's exit-bar evaluate reads 1 > 0 → same-bar re-entry after TP exits.
3. **(R3) Harness/adapter fill misrouting (pre-existing)** — protective SL fills arrive at
   `_on_standard_execution_event` with `action=UNKNOWN` and are processed as ENTRY fills
   (spawning bracket children around an exit); TP fills produce no exit event at all; exits are
   salvaged one step later by OOB cleanup with reason "CLOSED" (feeding R1). Also, bracket
   children are placed TWICE per entry (two TP/SL sets resting). Not touched by this ticket's
   diff (`f4f0732` deleted only the monkey-patch); previously masked by the re-gate.
   → The 1 remaining violation (05-26: BT TIME_BARRIER vs LT TP_HIT, same bar) is the
   **backlogged B(b)** same-bar precedence inversion. The 7 lt_only extras all trace to R1/R2/R3.

Artifacts: `reports/_ledger_parity/` (backtest_ledger.csv, livetest_ledger.csv,
parity_config.json = trailing-off, parity_config_with_trailing.json.bak = original).
