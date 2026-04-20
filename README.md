# CL_Analyst

Machine learning pipeline for predicting significant price movements in Crude Oil (CL) futures using 5-minute OHLCV data.

## Setup

Create a Python environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create a `.env` file from `.env.example` and set `CL_DATA_ROOT` to a directory
that contains `data/` and `models/`. `CL_DATA_ROOT` is required — the code
raises if it is missing.

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

## Experiment Pipeline

The model improvement pipeline has three phases: hyperparameter search, training, and backtesting.

### Phase 1: Hyperparameter Search (Optuna)

```bash
# Run 100-trial search for a specific metric (logloss, f1, f0.5, sharpe)
python agent/optuna_lgbm_search_v2.py --ml-metric logloss --n-trials 100 \
  --data C:\CL_Analyst_Data\data\processed\CL_set_07.parquet \
  --target TARGET_TRIPLE_2x1_24H_LONG \
  --study-name wf_v2_long_logloss_set07

# Check progress of a running or completed study
python agent/check_optuna_db.py models/optuna_studies/wf_v2_long_logloss_set07.db
```

**Output:** Best hyperparameters saved to `models/optuna_studies/` (SQLite DB) and `reports/optuna_best_params_*.json`.

### Phase 2: Train Model (Experiment Runner)

Create a config file under `configs/experiments/` (see `EXP-030.json` as template):

```json
{
    "experiment_id": "EXP-030",
    "strategy": "optuna_v2_set07_logloss",
    "hypothesis": "Description of what you're testing",
    "data_path": "C:\\CL_Analyst_Data\\data\\processed\\CL_set_07.parquet",
    "target_name": "TARGET_TRIPLE_2x1_24H_LONG",
    "method": "walk_forward",
    "balance_mode": "downsample",
    "train_cutoff_date": "2022-01-01",
    "model_params": { "...paste best params from Optuna..." }
}
```

```bash
# Config-driven training (recommended — outputs isolated to reports/EXP-030/)
python agent/experiment_runner.py --config configs/experiments/EXP-030.json

# Legacy direct CLI (outputs go to reports/ directly, will overwrite)
python agent/experiment_runner.py --id EXP-030 --data ... --target ... --train-cutoff-date 2022-01-01
```

**Output:** When using `--config`, files are isolated per experiment:
- `reports/EXP-030/oos_predictions.csv` — OOS probability predictions (used for backtesting)
- `reports/EXP-030/vault_predictions.csv` — Vault holdout predictions (pre-cutoff diagnostics)
- `reports/EXP-030/metrics_predictions.csv` — Walk-forward fold predictions (in-sample diagnostics)
- `models/EXP-030/final_model.pkl` — Trained model artifact

### Phase 3: Backtest

```bash
# Single-model: auto-resolves predictions from config's models.long.predictions_path
python agent/backtest_engine.py \
  --config configs/strategies/OPTUNA_EXP-030_Set07.json \
  --data C:\CL_Analyst_Data\data\processed\CL_set_07.parquet

# Dual-model: auto-merges long + short predictions (outer join on DateTime)
python agent/backtest_engine.py \
  --config configs/strategies/ensemble2_alt.json \
  --data C:\CL_Analyst_Data\data\processed\CL_set_07.parquet

# Override auto-resolve with explicit predictions file
python agent/backtest_engine.py \
  --config configs/strategies/OPTUNA_EXP-030_Set07.json \
  --predictions reports/oos_predictions.csv \
  --data C:\CL_Analyst_Data\data\processed\CL_set_07.parquet
```

**Auto-resolve**: When `--predictions` is omitted and `--config` is given, the backtest engine reads `predictions_path` from `models.long` and/or `models.short`. For dual-model configs with both long and short `predictions_path`, it auto-merges them (outer join, NaN→0.0).

**Note:** The backtest engine does NOT load the model. It reads pre-computed predictions from the CSV and applies trade management rules (TP/SL/trailing/cooldown) from the strategy config.

### Phase 4: Archive & Deploy

When using `--config` mode, the experiment runner **automatically archives** to `models/registry/{EXP_ID}/` after training — no separate archive step needed.

```bash
# Manual archive (if needed)
python agent/archive_model.py --experiment-id EXP-030 \
  --oos-predictions-path reports/oos_predictions.csv \
  --experiment-config-path configs/experiments/EXP-030.json

# Deploy to live trading (uses strategy config to load model from registry)
python -m src.live_execution.live_trader --config configs/strategies/OPTUNA_EXP-030_Set07.json --dry-run
```


### Diagnostics: Prediction Distribution Visualizer

Quickly identify models with compressed probability distributions (never reaching the trading threshold) versus models with healthy spreads.

```bash
# Generate distribution plots for all models in the registry
python scripts/plot_prediction_distributions.py

# Force regenerate all plots (even if PNGs already exist)
python scripts/plot_prediction_distributions.py --force

# Use a custom probability threshold (default: 0.60)
python scripts/plot_prediction_distributions.py --threshold 0.55
```

**What it does:**
- Auto-discovers all `models/registry/*/oos_predictions.csv` files
- Generates per-model histogram + KDE plots with threshold lines (0.60 primary, 0.45 secondary)
- Color-codes green (≥ threshold) vs red (< threshold)
- Annotates each plot with: direction, N, min/max/mean/median, % above threshold, distribution shape (unimodal/bimodal/skewed)
- Generates a combined comparison grid of all models
- Skips models without prediction files; skips existing PNGs unless `--force`
- **Temporal breakdown** (2×2 grid per model): hourly signal frequency + rate, day of week, monthly time series, year×month heatmap

**Output:** `reports/prediction_distributions/` — per-model distribution + temporal PNGs + `all_models_comparison.png`


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

### Telegram Notifications
LiveTrader natively integrates a "push-only" mechanism inside `src/live_execution/utils/telegram_alert.py`. 
Any interaction with the Telegram API is designed strictly as fire-and-forget; its functions are wrapped seamlessly in `try/except` clauses so connection issues with Telegram never bubble up and crash the main execution loop.

Setup:
To enable notifications, talk to @BotFather on Telegram to generate a `TELEGRAM_BOT_TOKEN`, grab your `TELEGRAM_CHAT_ID`, and provide them inside your `.env` configuration file.

The trader will broadcast messages to your chat for:
- **Startup:** When the execution engine initiates the event loop, identifying the active strategy and environment.
- **Heartbeat:** At the top of every hour (appended system resource and MLOps metrics).
- **Trade Execution:** When a new trade entry or exit (Take Profit, Stop Loss) occurs, with execution price details.
- **Fatal Error:** Broadcasting stack traces immediately upon severe unhandled exceptions before graceful exit.

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
|   |   +-- alpha_factory.py       # Feature generation engine (159 features in set_07, 174 in set_08)
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
|   +-- strategies/                # Strategy JSON configs (trade management + model refs)
|       +-- manatee.json           # Long strategy (EXP-017, client_id=10)
|       +-- koala.json             # Short strategy (EXP-020, client_id=11)
|       +-- manatee_single.json    # Single-model variant (client_id=12)
|       +-- ensemble_conservative.json  # Dual-model ensemble (client_id=13)
|       +-- config_readme.md       # Full attribute reference
|   +-- experiments/               # Experiment config JSONs (model params + data + target)
|       +-- EXP-030.json           # Template: Optuna v2 logloss bake-off
+-- models/
|   +-- registry/                  # Archived model bundles (source of truth)
|   |   +-- EXP-017_S_Ultimate/
|   |   +-- EXP-020_S_Ultimate_Short/
|   |   +-- EXP-030_optuna_v2_set07_logloss/  # Model + predictions + configs
|   +-- optuna_studies/             # Optuna SQLite study DBs
|   +-- final_model.pkl            # Current production model
+-- data/
|   +-- raw/                       # Source OHLCV CSVs (cl-5m_bk.csv = immutable seed)
|   +-- processed/                 # ML-ready parquet + warm_start_cache.parquet
+-- agent/                         # Automation scripts
|   +-- experiment_runner.py       # Config-driven training + auto-archive
|   +-- backtest_engine.py         # FSM backtester with auto-resolve predictions
|   +-- archive_model.py           # Archive model bundles to registry
|   +-- optuna_lgbm_search_v2.py   # Hyperparameter search (Optuna)
|   +-- check_optuna_db.py         # Check Optuna study progress
+-- scripts/                       # Utility scripts
|   +-- plot_prediction_distributions.py  # Distribution visualizer (histogram+KDE per model)
+-- tests/                         # Pytest test suite (350+ tests)
+-- reports/                       # Evaluation outputs
```
