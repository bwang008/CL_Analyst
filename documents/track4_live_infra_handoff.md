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

## Next steps for Task 4.2 (live streamer & DB)
- Use `ib_insync` live tick subscription for CL and aggregate into 5-minute bars.
- Persist to SQLite or append to parquet with the same `DateTime` + OHLCV schema.
- Ensure bar aggregation uses exchange time in `America/New_York`, then standardize to UTC (or keep consistent with Task 4.1).

## Next steps for Task 4.3 (paper execution engine)
- Flow: latest bar batch -> `AlphaFactory.add_all_features` -> `util.get_feature_columns` -> `LGBMLearner.load` -> predict.
- Threshold buy-probabilities to generate signals (see `agent/backtester.py` for logic and default thresholds).
- Use existing triple-barrier config conventions (TP 2x ATR, SL 1x ATR, 24h horizon) from `src/data_processor.py` and `agent/backtester.py`.
- Build bracket order with TP/SL prices derived from ATR at entry bar.
