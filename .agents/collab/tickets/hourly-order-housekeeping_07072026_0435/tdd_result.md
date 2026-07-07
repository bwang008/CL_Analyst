# TDD Result - hourly-order-housekeeping_07072026_0435
Outcome: GREEN, independently verified by TDD-Manager.
- tests/test_hourly_order_housekeeping.py: 49/49 (43 red -> green, 6 FENCEs held).
- Guarded suites: test_oob_entry_state_recovery.py 41/41, test_fleet_health.py 17/17.
- Full fast suite: 1659 passed; only failures = 10 pre-existing ES01B-Sortino sentinels.
- Production queue/log/DB untouched throughout.
Feature: in-child hourly housekeeping sweep (~:15 wall-clock, self-gated in the
event-loop poll body) — auto-clean: targeted orphan cancel (A-4 live-id exclusion),
OOB drift recovery via _recover_oob_close seams (A-5 grace), A-1(b) whitelisted
ledger repair from same-day executions; detect-only: naked/untracked/ambiguous/
unknown-order (human alerts); A-2 five-primitive cache-safe boundary with
disconnect-abort; A-3 duration budget + batched Telegram; never-raises.
New primitives: ExecutionClient.get_cached_position, get_open_trades(None)=all,
TelemetryDB.repair_closed_position + get_recent_closed_positions.
Deviations from amendments: none. One contract-forced addition: sim adapter
_current_bar_time init (latent AttributeError, inert for existing flows).
Deploy: PENDING operator fleet restart; first live :15 sweep verified via the
hourly monitor thereafter.
