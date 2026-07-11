# TDD Result — jit-roll-ratio-empty_07102026_1453

**Final outcome:** GREEN. Full fast suite: **2058 passed, 0 failed** (baseline 2006 → +16 Stage-1 tests, +36 Stage-2 tests incl. 3 updated tolerance pins; verified independently by the TDD-Manager 2026-07-10/11). Both blueprint stages implemented; activation remains operator-gated.

## Files changed

Stage 1 (migration tooling):
- `scripts/backfill_roll_history.py` (NEW) — importable derivation/validation core (`derive_segments`, `segments_to_roll_entries`, `validate_replay`, `migrate_symbol`) + operator CLI (`--dry-run`, `--cl-june-ratio` no-default, `--replay-tol` hard-ceilinged at 1e-6, Databento basis-identity gate, feature-level spot check). Entries are no-`"to"` origin-stamped (`seed_backfill_jit-roll-ratio-empty_07102026_1453`) → restored via the legacy branch regardless of execution symbol. NOT yet run against real data.
- `tests/test_backfill_roll_history.py` (NEW, 16 tests) — incl. the replay-equality/ratio-direction gate through the real DataManager.

Stage 2 (live code):
- `src/core/instrument_master.py` — CL/MCL `roll_ratio_tolerance` 0.01 → 0.001 (reverses T5 zero-change pin, ticket-cited).
- `src/live_execution/data_manager.py` — `ROLL_SEAM_*` constants; `resolve_roll_seam()` (RETRY/ESCALATE/RESOLVED quotient scan, median ratio, cutoff = first new-basis bar); `_append_roll_event()` (immediate persistence); `set/get/clear_pending_roll` (disk-backed, namespaced); `initialize()` Step 1.5 pending resolution BEFORE `_backfill()` with RuntimeError hard-fail on un-anchorable pending (replacing the silent ratio≈1 swallow for the pending path); Step-5 double-append guard; `_save_roll_metadata` merge-preserves unknown keys; Amendment-1 fix (`_apply_roll_to_cache` cutoff = first overwritten overlap bar).
- `src/live_execution/live_trader.py` — `_attempt_pending_roll_resolution()` (per-new-1h-bar gate, 3-day log.critical+Telegram escalation, never raises into the loop); `_check_contract_rollover` persists pending at detection + immediate attempt; `_event_loop` wiring outside the heartbeat gate.
- `tests/test_roll_seam_capture.py` (NEW, 25 tests), `tests/test_pending_roll_lifecycle.py` (NEW, 11 tests), `tests/test_session_watchdog_rollover.py` (3 tolerance pins updated, Manager-adjudicated).

## Known residual (tester-pinned contract)
A pending roll resolved at STARTUP leaves a bounded double-adjusted overlap window (backfill's keep-last overwrite vs cutoff-at-flip). The mid-run resolution path and the startup-witnessed roll path are seam-exact. Acceptable per contract; revisit if fleet-down-across-roll becomes common.

## Operator activation checklist (NOT yet performed)
1. Stage 1: run `scripts/backfill_roll_history.py` against CL_DATA_ROOT (dry-run first; CL needs `--cl-june-ratio` from the documented CLQ6/CLN6 estimate). Backups are automatic.
2. Restart the NG child FIRST (live canary); record shadow_log-vs-training-basis comparison into this folder before fleet-wide restart.
3. Stage 2 activates on each child restart; per user rule, canary before treating as production. Must be live before the ~2026-07-20 CL roll (contingency: fleet down across IBKR's lead flip).
4. Rollback: Stage 1 = restore `*_backup_*` metadata files + restart; Stage 2 = revert commit (metadata keys additive/back-compatible).
