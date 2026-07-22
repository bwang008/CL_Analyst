# TDD Result — cooldown-single-authority-wiring_07222026_1051

## RED (pre-fix)
tests/test_cooldown_wiring.py: 6 failed / 2 passed — failures exactly the
phantom-attribute no-ops:
- _reset_position_state never called strategy.on_exit (SHORT, LONG, end-to-end
  SL-fill variants)
- _seed_restart_cooldown never reached the strategy
- source scans found `self._strategy` in live_trader.py and the alias patch in
  livetest_engine.py

## GREEN (post-fix)
- tests/test_cooldown_wiring.py: 8/8
- Full suite: **2546 passed, 1 skipped** (pytest, conda env `trader`,
  --timeout=300), zero failures.

## Collateral stub repairs surfaced BY the loud-failure design
Three stubs that previously depended on the silent no-op crashed with
AttributeError('strategy') once the guards were removed — each repaired by
setting the REAL attribute (marker comments in-file):
- tests/test_live_trader_bugs.py::test_out_of_band_exit_routing
- tests/test_settle_confirm_loop_deferral.py::_base_trader (3 tests)
- tests/test_oob_entry_state_recovery.py::_recovery_stub (4 tests — the
  recovery path's cooldown seeding was ALSO silently skipped there pre-fix)

## Behavioral pins (re-adjudicated files, all green)
- Gate release timelines unchanged from B(b)+F: exit bar reads 0, release at
  exit+N+1 (test_parity_cooldown_single_authority, test_exit_bar_semantics)
- Flavor-blind arming: TP/TIME_BARRIER/CLOSED/CLOSED_OOB arm per-side
  cooldown_bars identically to SL (test_exit_reason_and_fill_routing,
  test_backtest_engine::TestExecutionStrategyCooldown)
- Sentinel neutralization + per-side advance semantics preserved
- Restart seeding: counters armed via lt.strategy; truthful reason forwarded
  to the execution strategy; missing strategy now RAISES (was silent no-op)
- Backtest: new TestExecutionStrategyCooldown pins the TieredEnsemble re-gate
  timeline with explicitly-closed second trades (end-of-data vacuity trap
  defused); negative control (cooldown_bars=0) included
- Inverted A7 pin: flavored vocabulary tuples must stay ABSENT from
  configurable_strategy.py
