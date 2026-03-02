# AGENT_LOG

Historical progress and completed track summaries (reverse-chronological; newest first).

## 2026-03-02 — Config Refactor + Sizing Tiers + Ensemble Live Trader

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

