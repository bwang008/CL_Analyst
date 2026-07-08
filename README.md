# CL_Analyst

> **Note**: Datasets before `HS09` (including `HS08` and earlier 5-min models) are deprecated and invalid due to data leakage. The project now uses hourly models (e.g., `HS09`, `HS11`). 5-minute bars are retained exclusively as a live subscription for heartbeat updates, not for training.

Machine learning pipeline for predicting significant price movements in Crude Oil (CL) futures using multi-timeframe (5-minute and hourly) OHLCV data.

## Setup

Create a Python environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# Optional: Install additional dependencies to run the streamlit dashboard
python -m pip install -r requirements-dashboard.txt
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

## Architecture (current champion: HourSet_08_Ensemble_03)

### Data flow
1. **Raw OHLCV**: `data/raw/CL.csv`
2. **Processing**: `src/data_processor.py`
   - time features + AlphaFactory feature generation (supports 5m and 1h intervals)
   - target construction (Triple Barrier)
   - cleanup + save to `data/processed/*.parquet`
3. **Training/Evaluation**: `main.py train`
   - walk-forward validation
   - final vault evaluation + artifact export to `reports/`
4. **Backtesting**: `agent/backtest_engine.py` (config-driven, FSM-based)
   - friction-aware PnL, long/short compatible (commission + slippage + CL multiplier)
   - supports `--config configs/strategies/*.json` for strategy-driven backtests
   - sizing tiers, trailing stops, concurrent positions, ensemble strategies
   - **Global Risk Filters**: automatic entry blocking during toxic hours/holidays (see below)

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

**Note:** The backtest engine does NOT load the model. It reads pre-computed predictions from the CSV and applies trade management rules (TP/SL/trailing/cooldown, plus the optional default-off exit-trigger overlays — see [Exit-Trigger Overlays](#exit-trigger-overlays-default-off-backtest-only)) from the strategy config.

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


### Model Diagnostics (Early Stopping / Iterations)

To verify if a trained LightGBM model hit early stopping or reached its maximum estimators, inspect the `.pkl` artifact directly using `joblib`. Since the framework saves the raw `lightgbm.basic.Booster` or the `LGBMClassifier` wrapper, you can extract the attributes like so:

```python
import joblib

# Load the trained model
model = joblib.load('models/registry/EXP-030_optuna_v2_set07_logloss/final_model.pkl')

print(f"Num trees: {getattr(model, 'num_trees', lambda: 'unknown')()}")
print(f"Best Iteration: {getattr(model, 'best_iteration', 'Not found')}")
```
If `Best Iteration` equals `Num trees` (e.g., 500 = 500), the model did not early stop and completed its full configured duration.

### Interactive Streamlit Dashboard

An interactive dashboard is available to inspect batch experiments, optimization trials, model registries, and detailed performance metrics.

#### Setup & Launch
First, ensure that the dashboard dependencies are installed:
```bash
python -m pip install -r requirements-dashboard.txt
```
To run the dashboard locally:
```bash
streamlit run dashboard.py
```

#### Dashboard Overview & Features
The dashboard is split into three main modules:
1. **Section 1: Batch Overview**
   * **Dropdown Batch Selector**: Scans the `reports/batch_runs/` folder and allows you to select any run.
   * **KPI Highlights**: Surfaces Total Experiments, Successful Runs, Best Optimization PnL, and Best Holdout PnL, along with a warning card if zero trades were triggered.
   * **Full Batch Metadata**: Parses and displays GCS paths, hyperparameter search bounds, machine specs, and Optuna settings directly from `manifest.json`.
   * **Experiment Leaderboard**: Displays a comprehensive, color-coded leaderboard sorted by Sharpe or Sortino objectives across Long and Short positions.
2. **Section 2: Experiment Drill-down**
   * **Performance Comparison**: Compares the metrics for the Pre-Optimization Baseline, Post-Optimization, and the Holdout validation set side-by-side.
   * **Interactive Equity Charts**: Rendered with Plotly, including Monthly Net PnLs, Drawdowns, and Cumulative PnL curves.
   * **Exit Distributions**: Pie charts detailing whether trades exited via Take Profit, Stop Loss, Trailing BE, or Time Barriers.
   * **LGBM Gain Feature Importance**: Interactive Top 15 horizontal bar charts and bottom 15 unused/low importance tables extracted directly from LGBM models.
   * **Diagnostic Execution Reports**: Surface VM names, wall-times, exit codes, and failure details.
3. **Section 3: Model Registry Browser**
   * Inspects all models located in `models/registry/`.
   * Shows model strategies, training targets, feature sizes, and integrity validation (presence of model `.pkl` and `.csv` predictions).
   * Allows drills down to read raw configurations inside `experiment_config.json`.

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

The production-grade model is **`HourSet_08_Ensemble_03`**, an asymmetric ensemble strategy that integrates hourly LightGBM predictors for long and short directions optimized independently.

* **Backtest Command**:
  ```bash
  python agent/backtest_engine.py \
    --config configs/strategies/HourSet_08_Ensemble_03_05242026.json \
    --data "C:\CL_Analyst_Data\data\processed\CL_HourSet_08.parquet" \
    --slippage-per-side 0.01
  ```
* **Strategy Configuration**: [HourSet_08_Ensemble_03_05242026.json](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/configs/strategies/HourSet_08_Ensemble_03_05242026.json)
* **Dataset**: `C:\CL_Analyst_Data\data\processed\CL_HourSet_08.parquet` (Hourly bar size: `1h`)
* **Execution Strategy**: `TieredEnsembleStrategy` / `TIERED` exit mode
* **Structure & Parameters**:

| Side | Underling Experiment / Model | Probability Threshold | TP (ATR Mult) | SL (ATR Mult) | Trailing (ATR Mult) | Cooldown (Bars) | Max Hold (Bars) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Long** | `E2E_HourSet_08_long_logloss` (Trial #191) | `0.59` | `5.75` | `3.75` | `2.0` | `21` | `144` |
| **Short** | `E2E_HourSet_08_short_logloss` (Trial #703) | `0.68` | `9.50` | `3.25` | `4.75` | `13` | `48` |

#### Key Performance Metrics (Holdout / OOS)
* **Long Model Holdout PnL**: `$8,172` (51 trades, 70.76% win rate, Sharpe: 2.5883)
* **Short Model Holdout PnL**: `$2,018` (31 trades, 49.26% win rate, Sharpe: 2.4803)

### Primary artifacts
* **Asymmetric Ensemble Config**: `configs/strategies/HS09_Ensemble_E01_06032026.json`
* **Registry (archived bundles)**: `models/registry/` (catalog in `models/registry/README.md`)

## Global Risk Filters (ExecutionGuard)

The system includes a **Global Execution Guard** that blocks new trade entries during structurally toxic market periods. This guard is applied **automatically to every strategy** (both backtest and live) unless explicitly overridden.

### How It Works

A centralized config file `configs/global_risk_filters.json` defines house-wide risk rules. When any strategy config is loaded (via `backtest_engine.py` or `live_trader.py`), the `load_strategy_config()` function in `src/live_execution/config_loader.py` automatically merges these global rules into the strategy config — **only if the strategy does not already define those keys itself**.

```
Strategy JSON (trade params only)
        |
        v
load_strategy_config() ──merges──> global_risk_filters.json
        |
        v
Merged config dict ──> BacktestEngine / ConfigurableStrategy
        |
        v
ExecutionGuard.is_entry_allowed(timestamp) ──> blocks or allows new entries
```

### Config File: `configs/global_risk_filters.json`

```json
{
  "blocked_entry_hours_est": [9],
  "blocked_entry_hours_by_day": {"Wednesday": [12]},
  "block_long_weekends": false,
  "long_weekend_block_scope": ["BEFORE_LONG_WEEKEND", "AFTER_LONG_WEEKEND"],
  "override_global_filters": false
}
```

| Key | Type | Description |
|-----|------|-------------|
| `blocked_entry_hours_est` | `list[int]` | Blocked **fill hours** (EST/EDT). `[9]` prevents fills at 9:00 AM NYMEX pit open. The backtest shifts bar timestamps by +1h (bar.Close fill model); the live trader uses wall-clock time directly. |
| `blocked_entry_hours_by_day` | `dict[str, list[int]]` | Day-specific blocked fill hours. `{"Wednesday": [12]}` prevents noon fills on EIA inventory report day. |
| `block_long_weekends` | `bool` | If `true`, blocks entries on days adjacent to CME holidays (long weekend transitions). |
| `long_weekend_block_scope` | `list[str]` | Which adjacency types to block: `BEFORE_LONG_WEEKEND`, `AFTER_LONG_WEEKEND`. |
| `override_global_filters` | `bool` | Set `true` in a **strategy JSON** to skip global filter inheritance entirely. |

### Key Design Rules

1. **All strategies inherit global filters by default.** You do not need to add filter keys to individual strategy JSONs.
2. **To disable for a specific strategy**, add `"override_global_filters": true` to that strategy's JSON file.
3. **The guard only blocks new entries.** Open positions continue managing TP/SL/trailing stops through blocked periods.
4. **Actual holiday shortened sessions are NOT blocked** — only the adjacent transition days (which are structurally toxic).
5. **Fill-time semantics.** Hours represent when the fill would occur, not bar-start time. The backtest engine fills at `bar.Close` (= next bar's Open for hourly bars), so it passes `bar_start + 1h` to the guard. The live trader fills immediately at wall-clock time.

### Files

| File | Purpose |
|------|---------|
| `configs/global_risk_filters.json` | Global house rules (hours, holidays) |
| `src/live_execution/config_loader.py` | Centralized config loader with inheritance merge |
| `src/live_execution/execution_guard.py` | Guard logic (`is_entry_allowed()`) with edge-triggered logging |
| `tests/test_execution_guard.py` | 10 unit tests covering hours, holidays, overrides, timezones |

### Live Trader Logging

When the guard blocks an entry in live mode, the terminal shows:
```
WARNING [GUARD ACTIVATED] BLOCKED: 09:00 bar in blocked_entry_hours_est
WARNING [EXECUTION GUARD] new entries blocked (bar=2026-01-20 09:00, buy_prob=0.72, sell_prob=0.34)
```
When the blocked period ends:
```
INFO [GUARD DEACTIVATED] new entries allowed
```
Heartbeats, PnL updates, and position management continue normally. Telemetry records `action_taken="SKIP_EXECUTION_GUARD"` for audit.

---

## Exit-Trigger Overlays (default-off, backtest-only)

The exit-side counterpart to the ExecutionGuard: three **config-gated, default-off** exit enhancements in `agent/backtest_engine.py`. When the config keys are absent, engine behavior is **byte-identical** to before they existed (pinned by tests). None of them are wired into the live trader — live parity is explicitly deferred and human-gated (ticket `exit-triggers-eod-oppsignal_07072026_1924`, blueprint + impact review in `.agents/collab/tickets/exit-triggers-eod-oppsignal_07072026_1924/`).

### 1–2. Flatten triggers: `weekend_flatten` / `eod_flatten`

Flatten a still-open **winner** on the last bar before a market gap. Detection is **data-driven from bar spacing** (no calendar, no lookahead, symbol-agnostic):

| Trigger | Gap band (to next bar) | Catches | ExitReason |
|---------|------------------------|---------|------------|
| `eod_flatten` | `[min_gap_hours (2h), weekend threshold)` | Daily 17:00–18:00 ET halt, holiday early closes | `EOD_FLATTEN` |
| `weekend_flatten` | `>= min_gap_hours (40h)` | Weekends + holiday-extended weekends (Thu pre-gap bars included automatically) | `WEEKEND_FLATTEN` |

The two bands are **disjoint by construction**, so an EOD-only config does not flatten Fridays and attribution never overlaps.

```json
"weekend_flatten": { "enabled": true, "profit_atr_mult": 1.0 },
"eod_flatten":     { "enabled": true, "profit_atr_mult": 0.0 }
```

- `profit_atr_mult` is **required when enabled** (loader raises if missing — no silent null defaults). `0.0` = flatten any non-losing position; `1.0` = only winners ≥ 1×ATR-at-entry.
- Precedence: TP/SL (intrabar) and TIME_BARRIER are evaluated **first**; a flatten fires only if the position would otherwise survive the bar. Fill = bar open (TIME_BARRIER convention).
- Applies in both single-position and concurrent modes.

### 3. Opposite-signal profit-close (`conflict_resolution`)

New `TieredEnsembleStrategy` conflict mode alongside `hold` / `close_existing_position` / `reverse_position`:

```json
"conflict_resolution": "close_existing_position_if_profit"
```

EXIT (as `SIGNAL_EXIT`) **iff** the opposite side's signal fires AND the current side's own signal has stopped confirming AND the position is green — judged **gross, on the EXEC (raw) price basis** via engine-fed `EngineState.entry_price` / `floating_pnl_points` (never by comparing the ratio-adjusted brain close against the raw entry fill). Both sides firing → HOLD; losing → HOLD. If a runtime does not feed `floating_pnl_points` (today's live path), the mode **raises loudly** instead of silently degrading to hold.

### A/B harness

```bash
# trigger arms (none/eod/wkd/both) at profit gates, holdout-only decision framing
python -m agent.ab_exit_triggers --arms eod,wkd,both --gates 0.0,1.0
# phase-2 individual toggles incl. the conflict-mode arm
python -m agent.ab_exit_triggers --arms eod,wkd,oppo
```

Runs baseline vs. each arm per fleet-manifest config on the holdout window and reports annualized-monthly Sharpe/Sortino, PnL, maxDD, and per-trigger exit attribution. 2026-07-07 holdout results: EOD strongly positive on ES/SI/NG, negative on GC, inert on CL; weekend negative in aggregate; `oppo` positive on NG. Full report: `.agents/collab/tickets/exit-triggers-eod-oppsignal_07072026_1924/ab_report.md`.

### Files

| File | Purpose |
|------|---------|
| `src/live_execution/strategy_config.py` | `WeekendFlattenConfig` + `parse_weekend_flatten` / `parse_eod_flatten` (crash-if-half-configured) |
| `agent/backtest_engine.py` | Gap-band precompute, `_flatten_exit_reason()`, exec-basis `EngineState` feed |
| `src/live_execution/strategies/execution_models.py` | `EngineState.entry_price` / `floating_pnl_points`; the new conflict mode |
| `agent/ab_exit_triggers.py` | Fleet-wide A/B harness |
| `tests/test_weekend_flatten.py`, `tests/test_opposite_signal_profit_close.py` | 51 tests: default-off byte-identity, band exclusivity, precedence, price-basis, loud-raise |

---

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

### Execution Parity Suite (`/validate-parity`)

A multi-layer validation framework that confirms the BacktestEngine and LiveTrader produce identical behavior from the same strategy config and data inputs. Run before deploying new strategy configs or after modifying execution logic.

```bash
# Full parity suite (43+ tests across 4 test files)
python -m pytest tests/test_config_parity.py tests/test_pipeline_parity.py tests/test_per_side_atr.py tests/test_execution_parity.py -v

# Prediction parity (shadow log replay — requires model + telemetry data)
python scripts/validate_parity.py

# Side-by-side config parameter comparison (manual review)
python tests/test_config_parity.py --compare configs/strategies/HS09_Ensemble_E01_06032026.json
```

**Validation layers:**

| Layer | Test File | What It Catches |
|-------|-----------|-----------------|
| Config Parity | `tests/test_config_parity.py` | Parameter naming mismatches, missing config keys, default value divergence between BacktestEngine and LiveTrader |
| Feature Parity | `tests/test_pipeline_parity.py` | Training vs live feature computation drift (AlphaFactory batch vs incremental) |
| ATR Parity | `tests/test_per_side_atr.py` | Per-side bracket sizing, trailing offset routing, backward-compatible fallback |
| Execution Parity | `tests/test_execution_parity.py` | Recovery `bars_held` time dilation (bar_size-aware), `initial_sl_price` schema integrity |
| Prediction Parity | `scripts/validate_parity.py` | End-to-end model inference divergence via shadow log replay |

**Key bugs caught by this suite:**
- **Time Dilation** — Recovery `bars_held` estimation used hardcoded `/5` (5-minute assumption), causing premature `TIME_BARRIER` exits for hourly strategies after reboots. Fixed to use `bar_size` from strategy config.
- **Initial SL Tracking** — `active_positions.sl_price` was overwritten by trailing stop modifications, making bracket reconciliation impossible. `initial_sl_price` column preserves the original SL for auditing.

## Headless & Production Deployment (WSL 2 / Ubuntu VPS)

The system is equipped with a production-grade, headless background deployment architecture suitable for local execution inside WSL 2 (Ubuntu 22.04) or porting directly to a remote Cloud Linux VPS (e.g. AWS EC2, GCP Compute Engine).

### Headless Architecture Overview
To achieve 100% autonomous background operations without requiring a continuous interactive terminal or graphical display session, the deployment uses a dual-service systemd topology:
1. **Automated GUI Virtualization**: IB Gateway requires an X-server graphical display to run. The system uses a virtual framebuffer wrapper (`xvfb-run`) to provision a virtual display (`:99`) in memory.
2. **IBC Automated Logon**: The Java-based **IBC** daemon wraps the Gateway launcher, automatically passing encrypted credentials, bypassing standard EULA warnings, and handling automated daily restarts.
3. **Pre-flight Port Synchronization**: The live trader systemd unit holds Python initialization until the headless gateway has successfully negotiated logon and opened the local socket API port (`4002`).

```
                +-------------------------------------------------------+
                |                    WSL 2 / VPS Init                   |
                +-------------------------------------------------------+
                                           |
                                           v
                +-------------------------------------------------------+
                |         ibc-gateway.service (xvfb-run Display :99)    |
                +-------------------------------------------------------+
                                           |
                         starts and logs into paper account
                                           |
                                           v
                                   Is Port 4002 open?
                                           |
                           +---------------+---------------+
                           | No                            | Yes
                           v                               v
                     [Loop & Sleep]               +-----------------+
                    (ExecStartPre check)          | Start Python    |
                                                  | Live Trader     |
                                                  +-----------------+
```

### Automatic Setup & Cloud VPS Portability
A unified, dynamic bash utility is located at `deploy/setup_ubuntu.sh` to automate environment setup. The script is **environment-agnostic** (it dynamically reads user home paths, active shell environment, and CPU architectures instead of hardcoding folders).

#### What the setup script automates:
* **System packages**: Installs Java JRE, Xvfb, tightvncserver, socat, net-tools, and logrotate.
* **Scaffolding**: Configures `/opt/cl-trader/...` structures and correctly binds read/write chown privileges to the active user.
* **IB Gateway & IBC**: Performs a silent headless download and extraction of the stable Gateway offline binary and IBC zip, configuring version and configuration symlinks automatically.
* **Credential Vault**: Generates `/etc/cl-trader.env` equipped with strict secure permissions (`chmod 600`) so that passwords and API keys are readable only by root.
* **Dynamic Interpolation**: Copies systemd template configurations to `/etc/systemd/system/` while dynamically substituting paths with the cloud user's actual home directory.

To prepare a fresh instance, run:
```bash
chmod +x deploy/setup_ubuntu.sh
./deploy/setup_ubuntu.sh
```

---

### Core Operational Commands

Once deployed, you can fully control the live trading stack using standard systemd utilities from **any directory** inside your terminal:

#### 1. Controlling the Live Stack
```bash
# Start the entire stack (triggers gateway startup, waits for port 4002, then starts trader)
sudo systemctl start live-trader.service

# Stop the entire stack gracefully (positions are closed out, logs flushed, gateway stopped)
sudo systemctl stop live-trader.service

# Restart the live trader (useful for deploying fast config updates)
sudo systemctl restart live-trader.service
```

#### 2. Following Live Outputs (Stdout/Stderr)
```bash
# Follow the live trade logs, model inferences, and heartbeats in real-time
journalctl -u live-trader.service -f

# Follow the background IB Gateway startup and automated login sequence
journalctl -u ibc-gateway.service -f
```

#### 3. Managing Boot Auto-Start
```bash
# Enable the services to automatically start whenever WSL or the Cloud VPS boots up
sudo systemctl enable ibc-gateway.service live-trader.service

# Disable automatic boot-start (forces manual activation only)
sudo systemctl disable live-trader.service ibc-gateway.service
```

---

### Log Rotation & Retention
To protect disk space from ballooning due to continuous standard output streams, the system deploys a custom logrotate configuration to `/etc/logrotate.d/cl-trader`:
* **Frequency**: Rotates all `.log` files in `/home/bwang008/projects/CL_Analyst/reports/` and `/opt/cl-trader/logs/` daily.
* **Retention**: Holds 14 days of back-history.
* **Compression**: Automatically compresses rotated logs (`gzip`) to minimize the storage footprint.

---

### Cloud VPS GUI / VNC Troubleshooting (One-time EULA Check)
When deploying to a remote cloud VPS (like AWS or GCP) for the very first time, IB Gateway requires you to manually accept EULAs once on screen. To do this headlessly:
1. Connect VNC Server on the VPS:
   ```bash
   tightvncserver :1
   ```
2. Create an SSH tunnel from your local PC to forward port `5901`:
   ```bash
   ssh -L 5901:127.0.0.1:5901 user@vps-ip
   ```
3. Open a local VNC Viewer on your PC and connect to `127.0.0.1:5901`.
4. Open a terminal inside the VNC desktop view and launch the Gateway manually once:
   ```bash
   ~/Jts/ibgateway/ibgateway
   ```
5. Log in, check the EULA boxes, and exit the Gateway.
6. Kill VNC on the cloud VPS and start the systemd services—they will now log in automatically forever!
   ```bash
   vncserver -kill :1
   sudo systemctl start live-trader.service
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
|   |   +-- alpha_factory.py       # Feature generation engine (159 features in set_07, 174 in set_08)
|   +-- live_execution/
|       +-- config_loader.py       # Centralized config loader (global filter inheritance)
|       +-- data_manager.py        # Three-Tier data manager (seed -> cache -> backfill)
|       +-- execution_guard.py     # Global Execution Guard (hour/holiday entry blocking)
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
|   +-- global_risk_filters.json   # Global execution guard rules (auto-inherited by all strategies)
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
|   +-- validate_parity.py               # Shadow log prediction parity validation
|   +-- trade_reconciler.py              # Live-to-backtest trade reconciliation
+-- tests/                         # Pytest test suite (440+ tests)
|   +-- test_execution_guard.py          # ExecutionGuard unit tests (hours, holidays, overrides)
|   +-- test_config_parity.py            # Config parameter parity (BT vs LT)
|   +-- test_pipeline_parity.py          # Feature pipeline parity (batch vs live)
|   +-- test_per_side_atr.py             # Per-side ATR bracket sizing
|   +-- test_execution_parity.py         # Recovery bars_held + initial_sl_price schema
+-- reports/                       # Evaluation outputs
```
