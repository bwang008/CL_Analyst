# Track 4 Live Infrastructure Handoff

## Implemented in this worktree
- `src/live_execution/ibkr_client.py`: ib_insync-based connection manager, CL contract builder, historical 5-minute poller, and OHLCV normalization.

## Confirmed interfaces to match
- Feature pipeline requires OHLCV columns with exact casing: `Open`, `High`, `Low`, `Close`, `Volume`.
  - Enforced by `AlphaFactory.REQUIRED_COLUMNS` in `src/features/alpha_factory.py`.
- Datetime index should be monotonically increasing and named `DateTime`.
  - Data checks exist in `src/data_verifier.py`.

## Task 4.1 output shape (historical poller)
- DataFrame columns: `DateTime`, `Open`, `High`, `Low`, `Close`, `Volume`.
- Timezone: standardized to UTC (default) and optionally made naive to align with existing datasets.
- Sorted ascending, with `DateTime` as the index (also retained as a column).

## Pacing guardrails
- `IBKRConnectionManager._request_historical_data` applies:
  - Backoff retries on exceptions.
  - Explicit pacing error detection (IB error code 162 + message checks).
  - Throttle sleep after successful request.

## Next steps for Task 4.2 (live streamer & DB) — ✅ COMPLETE
- Live bar subscription implemented via `ib_insync` `reqHistoricalData(keepUpToDate=True)`.
- Bars persisted to SQLite (`market_bars` + `raw_front_month_bars`) and Parquet warm-start cache.
- Bar aggregation uses exchange time, standardized to UTC.

## Next steps for Task 4.3 (paper execution engine) — ✅ COMPLETE
- Flow: DataManager warm-start → `AlphaFactory.add_all_features` → `LGBMLearner.load` → predict.
- Threshold buy-probabilities generate signals (default 0.45).
- Bracket order with TP/SL prices derived from ATR at entry bar.

## Task 4.4 — Smart Backfill & Dual-Ledger — ✅ COMPLETE (2026-02-24)
- `DataManager` (Three-Tier: seed CSV → Parquet cache → IBKR backfill → live append).
- Two-Stream architecture: Brain (continuous) for signals, Hands (front-month) for execution.
- `raw_front_month_bars` table in telemetry for training data.
- `get_front_month_contract()` resolves current front-month (CLJ6 verified).
- `timedelta_to_ib_duration()` + `split_duration_into_chunks()` for backfill requests.
