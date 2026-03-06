# AGENT_LOG

Historical progress and completed track summaries (reverse-chronological; newest first).

## 2026-03-03 — Resubscription Bug Fixes (BUG-005, BUG-006)

### BUG-005: Resubscription crashes with "event loop already running" (bars never recovered)

- **Symptom**: After HMDS disconnect (`Error 10182`) + reconnect (`2106`), resubscription always failed with `RuntimeError: This event loop is already running`. Bars never resumed despite connectivity being restored. Portfolio updates continued, so the bot appeared alive.
- **Root cause**: `_on_ib_error()` is an ib_insync callback running inside the asyncio event loop. `_resubscribe_and_backfill()` calls `subscribe_live_bars()` → `reqHistoricalData()` → `loop.run_until_complete()`, which crashes because the loop is already running. Same async-inside-callback issue as BUG-002 (marketable_limit).
- **Fix (attempt 1 — failed)**: Deferred resubscription via `asyncio.ensure_future()` + `loop.call_soon()`. Still crashed because even in a coroutine, sync ib_insync methods call `loop.run_until_complete()` which fails inside a running loop.
- **Fix (final)**: Added `subscribe_live_bars_async()` to `ibkr_client.py` using `reqHistoricalDataAsync()`. Rewrote `_deferred_resubscribe()` as a fully async method that `await`s the async subscribe. Removed gap backfill from the reconnection path (gaps fill naturally via `keepUpToDate=True`). Added `_resubscribe_pending` flag to deduplicate rapid-fire 2104/2106 events.

### BUG-006: Gap backfill called with invalid keyword argument

- **Symptom**: `TypeError: IBKRConnectionManager.fetch_historical_bars_by_duration() got an unexpected keyword argument 'contract'`
- **Root cause**: `_resubscribe_and_backfill()` passed `contract=self._contract` to `fetch_historical_bars_by_duration()`, but that method doesn't accept a `contract` parameter — it builds its own via `build_cl_contract()`.
- **Fix**: Removed the invalid `contract=self._contract` keyword argument.

### User additions (manual edits)

- Added `cooldown_bars` config param + `_cooldown_remaining` state to `LiveTrader.__init__()`, `_on_new_bar()`, and trade exit handler for parity with backtest engine FSM COOLDOWN state.
- Updated `ensemble_conservative.json`: TP=2.0 ATR, SL=1.0 ATR, sizing tiers all set to 1 contract.
- Updated `ensemble_aggro.json`: SL=3.0 ATR (from 2.5).
- Improved timezone normalization in `_resubscribe_and_backfill()` to handle both tz-aware and tz-naive `_last_bar_time`.

## 2026-03-02 — Live Trader Bug Fixes & Parity Tester Enhancement

### BUG-001: Bar subscriptions lost after IBKR disconnects (never recovered)

- **Symptom**: After `Error 10182` (subscriptions lost), `NEW BAR` and `INFERENCE` logs stopped appearing. `updatePortfolio` continued working. The bot was alive but blind — no new signals generated.
- **Root cause**: `_on_ib_error()` only triggered `_resubscribe_and_backfill()` on error codes 1101/1102 (full connectivity restored). IBKR actually sends warning codes **2104** (Market data farm OK) and **2106** (HMDS data farm OK) after brief disconnects. Resubscription never fired.
- **Fix**: Added 2104 and 2106 to the trigger set in `_on_ib_error()` (`live_trader.py` line ~910).
- **Impact**: Bot now auto-recovers bar subscriptions after brief IBKR data farm blips.

### BUG-002: Marketable limit orders fail with "event loop already running"

- **Symptom**: `[ERROR] Failed to place bracket order: This event loop is already running`. Orders placed with `entry_mode="marketable_limit"` always failed. Position stayed at 0 despite valid sell signals.
- **Root cause**: `marketable_limit` mode called `get_bid_ask()` → `ib.reqTickers()` inside the `_on_new_bar` callback. `reqTickers()` internally calls `loop.run_until_complete()`, but the asyncio event loop is already running (we're inside an ib_insync callback). This is a fundamental ib_insync constraint: only cached/sync methods work inside callbacks.
- **Why it wasn't caught earlier**: The initial short position used `adaptive` mode (default), which only sets order attributes — no async calls. `marketable_limit` was added to the config mid-session and failed on its first real invocation.
- **Fix**: `place_bracket_order()` in `ibkr_client.py` now uses `limit_price` (bar close) + 2 ticks instead of fetching live NBBO via `reqTickers()`. Functionally identical result, no async calls.
- **Safe methods inside callbacks**: `ib.portfolio()`, `ib.positions()`, `ib.openTrades()`, `ib.placeOrder()`, setting order attributes.
- **Unsafe methods inside callbacks**: `ib.reqTickers()`, `ib.accountSummary()`, `ib.reqHistoricalData()` — anything making a new request to IBKR.

### BUG-003: Resubscription crashes with timezone mismatch

- **Symptom**: `TypeError: Cannot subtract tz-naive and tz-aware datetime-like objects` in `_resubscribe_and_backfill()`.
- **Root cause**: `pd.Timestamp.utcnow()` returns tz-aware (`+00:00`), but `self._last_bar_time` is tz-naive (naive UTC). Pandas refuses the subtraction.
- **Why it wasn't caught earlier**: Latent bug — `_resubscribe_and_backfill()` was never executed until BUG-001 was fixed (adding 2104/2106 triggers). The code path was dead until the resubscription trigger fix made it reachable.
- **Fix**: Replaced `pd.Timestamp.utcnow()` with `pd.Timestamp.now(tz="UTC").tz_localize(None)` to produce tz-naive UTC.

### BUG-004: TWS mobile blocks paper bot bar data (IBKR HMDS conflict)

- **Symptom**: `Error 162: Trading TWS session is connected from a different IP address`. No `NEW BAR` logs after startup despite "Subscribed to live bars" message.
- **Root cause**: IBKR's Historical Market Data Service (HMDS) is shared per username across all sessions (Gateway, TWS desktop, TWS mobile). Logging into TWS mobile from a different IP causes HMDS to reject `reqHistoricalData(keepUpToDate=True)` on the Gateway session. Portfolio data and order execution continue working (separate channels).
- **Fix**: Not a code fix — IBKR infrastructure limitation. User must avoid running TWS mobile while the paper bot is running, or use a separate IBKR paper username.

### Enhancement: Strategy filter for validate_parity.py

- **Problem**: Running different strategies (manatee vs ensemble_conservative) appends mixed predictions to the shadow log. `validate_parity.py` replays all rows against one model, causing false `[FAIL] PIPELINE DIVERGENCE`.
- **Solution**: Added `--strategy` filter to `validate_parity.py` and `export_shadow_log.py`. When multiple strategies are detected and no `--strategy` is specified, the script auto-selects the most recent strategy.

### Live trade results (Paper DU1899929, session 2026-03-02)

| # | Direction | Entry | Contracts | Exit | P&L |
|---|-----------|-------|-----------|------|-----|
| 1 | Short @ 72.07 | 02:09 UTC | 4 | SL 72.86 (2), TP 70.10 (2) | +$361.04 |
| 2 | Short @ 71.42 | 15:20 UTC | 2 | TP 70.10 | +$2,630.52 |
| 3 | Short @ 70.62 | 16:50 UTC | 3 | *Open* | — |
| | | | | **Session Total** | **+$2,991.56** |

- **Goal**: Implement position sizing (lots) in BacktestEngine, refactor configs to group live-only attributes under `live_config`, add per-config `client_id`, and make `ConfigurableStrategy` support ensemble configs.

### Config refactoring
- All 4 strategy JSONs (`manatee.json`, `koala.json`, `manatee_single.json`, `ensemble_conservative.json`) restructured: `experiment_id` moved under `live_config`, `client_id` added (10/11/12/13).
- Created `configs/strategies/config_readme.md` — full attribute reference with compatibility matrix.

### Sizing tiers implementation
- `execution_models.py` → `BaseExecutionStrategy._parse_sizing_tiers()` and `_prob_to_lots()`: maps probability to lot count via highest-first matching.
- All 3 strategies (`SingleModelStrategy`, `ConservativeEnsembleStrategy`, `AggressiveEnsembleStrategy`) set `Order.lots` from tiers.
- `backtest_engine.py` → `TradeRecord.lots`, `_OpenPosition.lots`, PnL calculations in `_close_trade` and `_check_position` multiply by `lots`.
- `configurable_strategy.py` → `_prob_to_lots()` mirrors the same logic for live trader parity.

### LiveTrader updates
- CLI `--client-id` default changed from `10` → `1`. Reads `live_config.client_id` from config JSON.
- `ConfigurableStrategy` reads `experiment_id` from `live_config` with backward compat fallback to top-level.
- Refactored `ConfigurableStrategy` for ensemble support: detects `models` dict, loads both LONG and SHORT models, runs dual inference, applies per-model thresholds, higher-probability signal wins on conflict.
- Added `buy_prob`/`sell_prob` fields to `TradeSignal` dataclass.
- Enhanced INFERENCE log: shows direction (LONG/SHORT/BOTH), `buy_prob`, `sell_prob`, and explicit position-skip labeling.
- Fixed shadow state logging to use `signal.buy_prob`/`signal.sell_prob` when available.

### Test results
- **52 passed, 0 failed** (backtest_engine + configurable_strategy tests)
- Updated test stub to handle new ensemble attributes (`_long_learner`, `_short_learner`, `_is_ensemble`, `_long_threshold`, `_short_threshold`).

## 2026-03-02 — Entry Order Upgrade: Adaptive Algo + Marketable Limit

- **Goal**: Upgrade entry orders from bare Market Orders to institutional-grade execution that reduces slippage and captures the spread. Avoid fragile async cancel/replace loops.
- **Solution**: Implemented two new entry modes (configurable), defaulting to IBKR Adaptive Algo.

### New methods
- `ibkr_client.py` → `get_bid_ask(contract)` — real-time NBBO snapshot via `reqTickers()` with 2s timeout and polling. Returns `(bid, ask)` tuple.

### Modified methods
- `ibkr_client.py` → `place_bracket_order()` — new `entry_mode` parameter (`adaptive` / `marketable_limit` / `market`), `adaptive_priority` parameter (`Normal` / `Urgent` / `Patient`). Deprecated `use_market` flag preserved for backward compatibility. Added `TagValue` import for algo params.
- `live_trader.py` → `__init__()` — added `entry_mode` and `adaptive_priority` params.
- `live_trader.py` → `_on_new_bar()` — passes `entry_mode` / `adaptive_priority` to bracket order. Enhanced ORDER PLACED log line shows actual order type (e.g. `LMT+Adaptive`) and entry mode.
- `live_trader.py` → `main()` — added `--entry-mode` and `--adaptive-priority` CLI args. Reads `live_config.entry_mode` / `live_config.adaptive_priority` from strategy JSON. Resolution: CLI > config > default.

### Entry modes

| Mode | Parent Order | Description |
|------|-------------|-------------|
| `adaptive` (default) | LMT + IBKR IBALGO | Server-side algo seeks mid-spread improvement. Zero extra data subscriptions. |
| `marketable_limit` | LMT | Prices 2 ticks ($0.02) through best ask/bid. Falls back to MKT if quote unavailable. |
| `market` | MKT | Legacy behavior. |

### Test results
- **356 passed, 0 failed** (127s)
- `test_bracket_order.py` expanded from 6 → 16 tests across 4 classes: `TestBracketOrderConfig`, `TestAdaptiveAlgoOrder`, `TestMarketableLimitOrder`, `TestEntryModeBackwardCompat`

## 2026-02-24 — Track 4.4: Smart Backfill & Dual-Ledger (Live Execution Engine)

- **Goal**: Solve the "Pipeline Parity Problem" — ensure live trading environment mirrors training environment.
- **Problem**: 5-day cold start was insufficient for indicators with 35-day (10,080 bar) lookback windows.

### New modules
- `src/live_execution/data_manager.py` — Three-Tier data architecture:
  - Tier 1: Immutable seed (reads last 60 days from `data/raw/cl-5m_bk.csv`)
  - Tier 2: Warm-start Parquet cache (`data/processed/warm_start_cache.parquet`)
  - Tier 3: IBKR backfill (gap-fills missing bars) + live append
- `src/live_execution/utils/time_utils.py` — `timedelta_to_ib_duration()` and `split_duration_into_chunks()`

### Modified modules
- `telemetry.py`: Added `raw_front_month_bars` table + `log_raw_bar()`/`raw_bar_count()`/`recent_raw_bars()`
- `ibkr_client.py`: Added `get_front_month_contract()` (resolves CLJ6) + `fetch_historical_bars_by_duration()`
- `live_trader.py`: Replaced `_cold_start()` with `_warm_start()` via DataManager; added Two-Stream architecture:
  - **Brain stream**: Continuous contract → AlphaFactory → model inference → signals
  - **Hands stream**: Front-month contract → bracket orders + raw bar logging to telemetry

### IBKR connectivity verified
- Paper account: `DU1899929`, port `4002` (IB Gateway)
- Front-month: `CLJ6` (conId=304037436, expiry 2026-03-20)
- Mock order: limit buy CL @ $5.00 → Submitted → Cancelled ✓

### Test results
- **174 passed, 0 failed** (44s)
- New tests: `test_data_manager.py` (14), `test_time_utils.py` (15), `test_telemetry.py` (+3 raw bar tests)


## 2026-02-23 — Track 2.1: Short Sniper (panic-selling)

- **Target**: `TARGET_TRIPLE_2x1_24H_SHORT` (binary short label)
- **Experiment**: `EXP-020` (`S_Ultimate_Short`)
- **Dataset**: `data/processed/CL_set_06_shortfix.parquet`
- **Key point**: the short edge was **high-confidence only**; max-F1 overtraded and lost money under friction.

### Threshold sweep + friction results (selected)

- F1-optimal threshold: `0.45`
  - Friction backtest PF: `0.87` (unprofitable; too many trades)
- Selected trade threshold: `0.60`
  - **Win rate**: `70.0%`
  - **Profit factor**: `2.39`
  - **Net PnL**: `+$1,071,745.37` (CL multiplier 1000; commission 2.50/side; slippage 0.03/side)

### Archived bundle

- Registry entry created:
  - `models/registry/EXP-020_S_Ultimate_Short/`
- Catalog updated:
  - `models/registry/README.md`

## 2026-02-23 — Track 1: Reality Check (market friction + OOS stress)

### Task 1.1 — Market friction backtest

- Updated `agent/backtester.py` to support:
  - direction-aware slippage (Buy fills worse; Sell fills worse)
  - flat commission per side
  - CL contract multiplier (1000)
- Friction-aware vault run (S_Ultimate):
  - Profit factor: `7.76`
  - Win rate: `84.4%`
  - Net PnL: `+$1,277,168.30`

### Task 1.2 — OOS regime testing

- Added regime runner `agent/oos_regime_test.py` to train pre-regime and test on extreme windows.
- Findings:
  - 2009 window was skipped due to insufficient pre-window training data in processed set.
  - COVID crash window remained profitable (PF > 1), but materially degraded vs vault-era performance.

## 2026-02-23 — Model Registry system

- Added `models/registry/` system and an archiving CLI:
  - `agent/archive_model.py`
- Archived `EXP-017` bundle:
  - `models/registry/EXP-017_S_Ultimate/`

