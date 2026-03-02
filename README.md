# CL_Analyst

Machine learning pipeline for predicting significant price movements in Crude Oil (CL) futures using 5-minute OHLCV data.

## Setup

Activate your environment:

```bash
conda activate trader
```

## Quick start

```bash
# Process raw CL data into ML-ready features
python main.py process

# Train/evaluate with walk-forward validation (writes reports/ + models/)
python main.py train
```

## Architecture (current champion: S_Ultimate / EXP-017)

### Data flow
1. **Raw OHLCV**: `data/raw/CL.csv`
2. **Processing**: `src/data_processor.py`
   - time features + AlphaFactory feature generation
   - target construction (Triple Barrier)
   - cleanup + save to `data/processed/*.parquet`
3. **Training/Evaluation**: `main.py train`
   - walk-forward validation
   - final vault evaluation + artifact export to `reports/`
4. **Backtesting**: `agent/backtest_engine.py` (config-driven, FSM-based)
   - friction-aware PnL, long/short compatible (commission + slippage + CL multiplier)
   - supports `--config configs/strategies/*.json` for strategy-driven backtests
   - sizing tiers, trailing stops, concurrent positions, ensemble strategies

### Prediction + backtest file conventions
This is the shared contract so prediction files can be used across backtesters.

**Prediction CSV schema (model output):**
- Index: `DateTime` (timestamp index)
- Columns: `prob_Buy` and/or `prob_Sell` (float probabilities in [0,1])
- Optional: `Predicted` (legacy), but prefer probability columns

**OHLCV parquet schema (backtest input):**
- Index: `DateTime` (timestamp index)
- Required columns: `Open`, `High`, `Low`, `Close`, `Volume`

**Alignment rules:**
- Predictions and OHLCV must share the same `DateTime` index for exact alignment.
- Do not rely on raw CSV fallback for backtests; use processed parquets that retain OHLCV.
- Use `data/processed/CL_set_06_shortfix.parquet` for aligned OHLCV backtests.

**Backtester expectations:**
- `agent/backtest_cl_concurrent.py` and `agent/backtest_engine.py` assume the schemas above.
- `agent/backtester.py` and `agent/oos_regime_test.py` can fallback to raw CSV if OHLCV is missing; avoid this by using aligned parquets.

**Dual-model regime tests:**
- `agent/regime_dual_model_backtest.py` merges `prob_Buy` + `prob_Sell` into a single signal stream.
- Outputs go to `reports/oos_regimes/` (summary CSV + per-regime predictions).

### Champion model configuration
- **Experiment ID**: `EXP-017` (`S_Ultimate`)
- **Target**: `TARGET_TRIPLE_2x1_24H_LONG`
- **Dataset**: `data/processed/CL_set_06.parquet`
- **Training**: walk-forward (expanding window), `balance_mode=downsample`
- **Objective**: binary + focal loss (`use_focal=true`)
- **Key params**:
  - `num_leaves=31`, `max_depth=4`, `learning_rate~0.0524`, `min_child_samples=166`, `n_estimators=1000`

### Primary artifacts
- **Metrics**: `reports/vault_metrics.json`
- **Predictions**: `reports/vault_predictions.csv`
- **Model artifact**: `models/final_model.pkl`
- **Registry (archived bundles)**: `models/registry/` (catalog in `models/registry/README.md`)

## Live Execution (Paper Trading)

### Overview
The live execution engine connects to IBKR (TWS or IB Gateway), uses a **Three-Tier** data architecture for warm-start initialization, and runs a **Two-Stream** architecture separating signal generation from order execution.

### Files
| File | Purpose |
|------|---------|
| `src/live_execution/data_manager.py` | Three-Tier data manager: Seed CSV -> Parquet cache -> IBKR backfill -> live append |
| `src/live_execution/ibkr_client.py` | IBKR connection manager, historical data, front-month resolution, bracket orders |
| `src/live_execution/live_trader.py` | Main execution loop: warm start -> Two-Stream bars -> features -> inference -> orders |
| `src/live_execution/strategy.py` | Strategy ABC + `TradeSignal` dataclass |
| `src/live_execution/strategies/configurable_strategy.py` | Config-driven strategy: reads JSON, supports single + ensemble configs |
| `src/live_execution/strategies/execution_models.py` | Backtest execution strategies: Single, ConservativeEnsemble, AggressiveEnsemble |
| `src/live_execution/telemetry.py` | SQLite telemetry backend (`data/live_telemetry.db`) |
| `configs/strategies/*.json` | Strategy config files (TP/SL, thresholds, sizing tiers, live_config) |
| `configs/strategies/config_readme.md` | Full config attribute reference with compatibility matrix |

### Prerequisites
1. Install and start **TWS** or **IB Gateway** in Paper Trading mode
2. Enable API connections: Configure -> API -> Settings
   - Port: `7497` (TWS) or `4002` (Gateway)
   - Enable ActiveX and Socket Clients

### Running
```bash
# Config-driven strategy (recommended)
python -m src.live_execution.live_trader --config configs/strategies/manatee.json --dry-run

# Ensemble strategy (dual long+short models)
python -m src.live_execution.live_trader --config configs/strategies/ensemble_conservative.json --dry-run

# Live paper trading (IB Gateway, default port 4002)
python -m src.live_execution.live_trader --config configs/strategies/manatee.json

# Legacy strategy registry (still works)
python -m src.live_execution.live_trader --strategy BUY70_SIZED_MANATEE
```

### Execution Loop
1. **Warm start**: `DataManager` loads last 60 days from seed CSV (`cl-5m_bk.csv`), creates/loads a Parquet cache, and backfills any gap from IBKR
2. **Two-Stream subscribe**:
   - **Brain stream**: Continuous contract live 5-min bars for signal generation
   - **Hands stream**: Front-month contract live 5-min bars for execution + raw data logging
3. **On each new bar** (Brain stream):
   - Appends to rolling window (capped at ~11,000 bars) + warm-start cache
   - Runs `AlphaFactory` to generate 80 features
   - Runs model inference (sigmoid on focal loss logits -> probability)
   - If probability >= threshold and position is flat -> places bracket order (sizing tiers determine lot count)
4. **On each new bar** (Hands stream):
   - Logs raw front-month OHLCV + `contract_month` to `raw_front_month_bars` for future retraining
5. **Bracket order**: Adaptive Algo entry (default) + Limit TP + Stop SL. Supports `--entry-mode adaptive|marketable_limit|market`.

### Telemetry
All bars and signals are logged to `data/live_telemetry.db`:
- **`market_bars`** -- smoothed continuous contract bars (used for training)
- **`raw_front_month_bars`** -- raw front-month bars with `contract_month` (for retraining)
- **`trade_ledger`** -- every signal (Hold/Buy/Sell), confidence %, action taken, order details
- **`tradebook_events`** -- append-only normalized execution lifecycle events (order submit/status/fills/commissions)

Tradebook normalization notes:
- `tradebook_events` keeps execution facts and join keys only (`signal_id` / `decision_id`).
- strategy/model diagnostics remain in `trade_ledger` and are joined when needed.
- futures identity includes `local_symbol` and `contract_month` for contract-level auditability.
- `decision_timestamp_utc` is stored so latency to execution can be measured.

Quick reader path:
```python
from src.live_execution.telemetry import TelemetryDB

db = TelemetryDB("data/live_telemetry.db")
rows = db.read_tradebook(limit=1000)  # list[dict], oldest-first
db.close()
```

## Project Structure

```
CL_Analyst/
+-- src/
|   +-- data_processor.py          # ETL pipeline (OHLCV -> features -> targets)
|   +-- LGBMLearner.py             # LightGBM wrapper (train/query/save/load)
|   +-- evaluator.py               # Walk-forward evaluation and metrics
|   +-- walk_forward.py            # Walk-forward validation framework
|   +-- visualizer.py              # Plotting and visualization
|   +-- features/
|   |   +-- alpha_factory.py       # Feature generation engine (80 features)
|   +-- live_execution/
|       +-- data_manager.py        # Three-Tier data manager (seed -> cache -> backfill)
|       +-- ibkr_client.py         # IBKR connection, data, front-month, orders
|       +-- live_trader.py         # Live execution engine (Two-Stream)
|       +-- strategy.py            # Strategy ABC + TradeSignal dataclass
|       +-- telemetry.py           # SQLite logging (smoothed + raw front-month)
|       +-- strategies/
|       |   +-- configurable_strategy.py  # Config-driven strategy (single + ensemble)
|       |   +-- execution_models.py       # Backtest execution strategies
|       +-- utils/
|           +-- time_utils.py      # IBKR duration string utilities
+-- configs/
|   +-- strategies/                # Strategy JSON configs
|       +-- manatee.json           # Long strategy (EXP-017, client_id=10)
|       +-- koala.json             # Short strategy (EXP-020, client_id=11)
|       +-- manatee_single.json    # Single-model variant (client_id=12)
|       +-- ensemble_conservative.json  # Dual-model ensemble (client_id=13)
|       +-- config_readme.md       # Full attribute reference
+-- models/
|   +-- registry/                  # Archived model bundles
|   |   +-- EXP-017_S_Ultimate/
|   |   +-- EXP-020_S_Ultimate_Short/
|   +-- final_model.pkl            # Current production model
+-- data/
|   +-- raw/                       # Source OHLCV CSVs (cl-5m_bk.csv = immutable seed)
|   +-- processed/                 # ML-ready parquet + warm_start_cache.parquet
+-- agent/                         # Automation scripts (backtester, sweeps, etc.)
+-- scripts/                       # Utility scripts (shadow log, parity validation)
+-- tests/                         # Pytest test suite (350+ tests)
+-- reports/                       # Evaluation outputs
```
