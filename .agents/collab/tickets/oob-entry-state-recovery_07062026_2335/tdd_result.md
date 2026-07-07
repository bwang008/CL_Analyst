# TDD Result - oob-entry-state-recovery_07062026_2335
Outcome: GREEN, independently verified by TDD-Manager.
- tests/test_oob_entry_state_recovery.py: 41/41 (32 red -> green, 9 FENCE held).
- Full fast suite: 1610 passed; only failures = 10 pre-existing ES01B-Sortino
  sentinel reds (config pins from the operator's intentional model swap; out of scope).
- Locked tests/test_fleet_health.py: untouched, green. Production queue/log/DB: untouched.
Files changed: live_trader.py (OOB recovery _recover_oob_close, pending/in-position
state split, trailing gate, _clear_pending_entry, A1 partial-fill routing, A2 real-close
scoping, A3 fill-price re-seed, A5 deterministic recovery event ids),
execution_interface.py + ibkr_execution.py + simulated_execution.py (cancel_orders_by_ids,
get_executions; A4 honest sim), configurable_strategy.py + scripts/trade_reconciler.py
(A7 vocabulary), fleet_health.py (A6 fill_evidence + 48h incomplete-close scope).
Coder deviations (2, documented, non-amendment, forced by Strict-Locked pins elsewhere):
compound trailing gate (_active_trade_id OR tracked SL - production-equivalent, both
fill-time-only); ctx .get() for legacy-shaped decision contexts with loud whole-ctx miss.
Deploy: PENDING operator fleet restart (changes inert until then). Commit precedes deploy
by operator-approved convention (64ccccb precedent); post-restart verification via the
hourly monitor per SKILL.
