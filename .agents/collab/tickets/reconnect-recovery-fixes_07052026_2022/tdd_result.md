# TDD Result — reconnect-recovery-fixes_07052026_2022

## Final outcome: GREEN ✅

Full fast suite (`conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"`):
- **Red baseline (before Coder):** 1423 passed / 16 failed — 14 expected-red new tests + 2 pre-existing
  unrelated failures.
- **Green (after Coder):** 1437 passed / 2 failed — all 21 new tests pass (14 formerly red + 7 fence);
  zero regressions.
- The 2 remaining failures are PRE-EXISTING and out of scope (missing disk artifact
  `reports/batch_runs/batch_20260704_0701_ES_01B_SCOUT/predictions/ES01B_Sharpe_E03_predictions.csv`,
  referenced by the shipped ES01B config): `test_config_generator_symbols.py::TestES01BPatchedConfig::
  test_referenced_artifacts_exist_on_disk`, `test_instrument_context.py::TestShippedConfigs::
  test_es01b_shipped_config_resolves_as_es`. Identical before and after this ticket.

## Files changed (uncommitted, on `development`)
- `src/live_execution/live_trader.py` (+79/-7)
  - R1: `_backfill_reconnect_gap_async` gap reference now tz-naive UTC (was local `pd.Timestamp.now()`),
    matching the stale-bar watchdog clock — reconnect backfill can actually trigger.
  - R2: `_resubscribe_pending = True` set BEFORE `data_client.connect()` in `_reconnect`; guard reset on
    both per-attempt failure paths so a later 2104 can still schedule `_deferred_resubscribe`.
  - R4: `_MAX_FRUITLESS_RECONNECTS = 3`; `_check_stale_bars` escalates on the 3rd consecutive fruitless
    watchdog firing (CRITICAL log + best-effort Telegram + SystemExit so fleet_runner restarts the child);
    counter resets to 0 on any new bar via `_on_bar_update_5m` / `_on_bar_update_1h`.
- `src/live_execution/ibkr_client.py` (+97/-0)
  - R3: `subscribe_live_bars` / `subscribe_live_bars_async` — empty snapshot (dead keepUpToDate stream,
    the error-162 "HMDS query returned no data" shape) now retries exactly once with a widened duration
    (`_fallback_duration_str`: ≥ "1 D" and longer than the original), then raises RuntimeError naming the
    contract + bar size. Happy path byte-identical (single request, unchanged params).
- `tests/test_reconnect_recovery_fixes.py` (new, 21 tests, TDD-TESTER strict-lock)

## Provenance
Root cause analysis from the 2026-07-05 first parallel fleet_runner session (ES01B/MES instance dark for
2+ hours while reconnect loop claimed success every ~5 min; CL healthy). Blueprint, audit log, and phase
history in this ticket folder.
