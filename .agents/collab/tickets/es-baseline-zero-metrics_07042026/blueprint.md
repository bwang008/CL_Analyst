# Ticket Resolution Blueprint — es-baseline-zero-metrics_07042026
**Ticket Directory:** `.agents/collab/tickets/es-baseline-zero-metrics_07042026/`
**Status:** APPROVED by Ticket-Impact-Reviewer (Options A + B; C as optional follow-up). NOT yet implemented — this ticket was read-only; hand off to `/tdd-manager` to execute.

## Bug Summary
All non-CL (ES/NG/NQ/ZC/GC/ZS/SI) baseline per-model backtest metrics in `pipeline_summary.json` → `batch_summary.md` are silently zero. Root cause: the new-symbol execution parquets (`<SYM>_raw.parquet`) store `DateTime` as a column over an int64 RangeIndex, and `gcp/vm_e2e_pipeline.py` reads them with raw `pd.read_parquet` (no index promotion) at `:271-277` (`run_backtest`) and `:896-902` (baseline ensemble block). `BacktestEngine` then converts the int index to 1970-epoch timestamps (`agent/backtest_engine.py:1206`) and `prob_buy_lookup.get(ts, 0.0)` (`:1221-1222`) silently defaults every bar's probability to 0.0 → 0 trades, no error, exit 0. Latent since commit `c5111ca1` (2026-06-16); triggered by the first int-indexed symbol parquet (ES, 2026-07-01). Full RCA + evidence in `case_report.md` (same folder).

## Target Files
- `gcp/vm_e2e_pipeline.py` (two exec-data read sites: ~`:271-277`, ~`:896-902`)
- `agent/backtest_engine.py` (`BacktestEngine.run`, ~`:1009-1045`: add zero-overlap guard)
- `.agents/workflows/build-symbol-pipeline.md` (`:49`: schema-contract doc line — optional follow-up, Option C)
- Tests: extend coverage per Required Changes below

## Required Changes

### Change 1 (Option A — normalize exec-data reads)
In `gcp/vm_e2e_pipeline.py`, replace both raw exec-data reads (`run_backtest` at ~`:271-277` and the baseline ensemble block at ~`:896-902`) with the existing normalizing loader `agent.backtest_engine.load_ohlcv(exec_data_path)`. That loader already handles parquet+CSV, promotes a `DateTime` column to a DatetimeIndex, validates required OHLCV columns, and raises on failure (repo policy: fail loudly, no silent defaults). Do not change `run_backtest`'s signature (internal caller only, `vm_e2e_pipeline.py:641`).

### Change 2 (Option B — fail-loud guard in the engine)
In `BacktestEngine.run` (`agent/backtest_engine.py`), after the probability lookups are built, raise `ValueError` when the lookups are NON-EMPTY yet zero of their timestamps intersect `ohlcv.index`. Error message must name the likely cause, e.g. "0 of N signal timestamps found in OHLCV index — index misalignment (int64 vs datetime64?)".

**Binding conditions from the Impact-Reviewer (must hold):**
1. Guard lives on the strategy-aware path and raises ONLY when prob lookups are non-empty AND intersection with `ohlcv.index` is zero.
2. Must NOT raise when `signals_df` is empty, when no prob/side/Predicted column resolves, or on the legacy path's thresholded `signal_sides` (a legitimately signal-free model must still report 0 trades honestly, not crash).
3. Note: `run()` has ~25+ production call sites and 37 test calls in `tests/test_backtest_engine.py`; existing fixtures build signals with `index=ohlcv.index`, and the Optuna fold evaluator wraps `run` in `except Exception: return 0.0` — the guard as scoped breaks no legitimate caller. Keep it that way.

### Change 3 (Option C — optional follow-up, not a gate)
Rebuild the seven `<SYM>_raw.parquet` files with a proper DatetimeIndex and re-upload to GCS; fix `.agents/workflows/build-symbol-pipeline.md:49` to state the schema contract ("DatetimeIndex named DateTime, no DateTime column" — or standardize on DateTime-as-column and rely on the normalizing loader).

### Residual (do not treat as fixed)
`vm_e2e_pipeline.py:756` (`ohlcv = pd.read_parquet(data_path)`) is a third raw read feeding the `:902` ensemble fallback when `exec_data_path` is null — practically dead (manifest schema now requires `exec_data_path`) and covered by the Change-2 guard, but it is NOT normalized by Change 1.

## Acceptance Tests (verification plan)
1. `run_backtest(preds_df, cfg, "long", tmp, exec_data_path=<ES_raw.parquet>)` → `trade_count > 0` (expected ≈934 trades / ≈$264k for ES01B 2x1_6h long_logloss, slippage 0.01).
2. Same call with a CL input → metrics byte-identical to `sweep_hs14a_2x1_6h_scout_20260704-0310` (no CL regression).
3. Int-indexed exec frame + non-empty signals into `engine.run` → loud `ValueError`; legitimately signal-free model → 0 trades, no raise.
4. (When authorized) one ES 01B canary (n_trials=3) → all `backtest_results.*.trade_count > 0` and nonzero `batch_summary.md` tables.
