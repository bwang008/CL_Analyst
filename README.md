# CL_Analyst

Machine learning pipeline for predicting significant price movements in Crude Oil (CL) futures using 5‑minute OHLCV data.

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
4. **Backtesting**: `agent/backtester.py`
   - friction-aware PnL, long/short compatible (commission + slippage + CL multiplier)

### Champion model configuration
- **Experiment ID**: `EXP-017` (`S_Ultimate`)
- **Target**: `TARGET_TRIPLE_2x1_24H_LONG`
- **Dataset**: `data/processed/CL_set_06.parquet`
- **Training**: walk-forward (expanding window), `balance_mode=downsample`
- **Objective**: binary + focal loss (`use_focal=true`)
- **Key params**:
  - `num_leaves=31`, `max_depth=4`, `learning_rate≈0.0524`, `min_child_samples=166`, `n_estimators=1000`

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
| `src/live_execution/data_manager.py` | Three-Tier data manager: Seed CSV → Parquet cache → IBKR backfill → live append |
| `src/live_execution/ibkr_client.py` | IBKR connection manager, historical data, front-month resolution, bracket orders |
| `src/live_execution/live_trader.py` | Main execution loop: warm start → Two-Stream bars → features → inference → orders |
| `src/live_execution/telemetry.py` | SQLite telemetry backend (`data/live_telemetry.db`) |
| `src/live_execution/utils/time_utils.py` | `timedelta` → IBKR duration string converter |

### Prerequisites
1. Install and start **TWS** or **IB Gateway** in Paper Trading mode
2. Enable API connections: Configure → API → Settings
   - Port: `7497` (TWS) or `4002` (Gateway)
   - Enable ActiveX and Socket Clients

### Running
```bash
# Dry run (no real orders, logs signals only)
conda activate trader
python -m src.live_execution.live_trader --dry-run

# Live paper trading (IB Gateway, default port 4002)
python -m src.live_execution.live_trader

# Custom port (TWS)
python -m src.live_execution.live_trader --port 7497

# Custom seed/cache paths
python -m src.live_execution.live_trader --seed-path data/raw/cl-5m_bk.csv --cache-path data/processed/warm_start_cache.parquet
```

### Execution Loop
1. **Warm start**: `DataManager` loads last 60 days from seed CSV (`cl-5m_bk.csv`), creates/loads a Parquet cache, and backfills any gap from IBKR
2. **Two-Stream subscribe**:
   - **Brain stream**: Continuous contract live 5-min bars for signal generation
   - **Hands stream**: Front-month contract live 5-min bars for execution + raw data logging
3. **On each new bar** (Brain stream):
   - Appends to rolling window (capped at ~11,000 bars) + warm-start cache
   - Runs `AlphaFactory` to generate 80 features
   - Runs model inference (sigmoid on focal loss logits → probability)
   - If probability ≥ 0.45 and position is flat → places bracket order
4. **On each new bar** (Hands stream):
   - Logs raw front-month OHLCV + `contract_month` to `raw_front_month_bars` for future retraining
5. **Bracket order**: Market buy + Limit TP (price + 2×ATR) + Stop SL (price − 1×ATR)

### Telemetry
All bars and signals are logged to `data/live_telemetry.db`:
- **`market_bars`** — smoothed continuous contract bars (used for training)
- **`raw_front_month_bars`** — raw front-month bars with `contract_month` (for retraining)
- **`trade_ledger`** — every signal (Hold/Buy), confidence %, action taken, order details
- **`tradebook_events`** — append-only normalized execution lifecycle events (order submit/status/fills/commissions)

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
├── src/
│   ├── data_processor.py      # ETL pipeline (OHLCV → features → targets)
│   ├── LGBMLearner.py         # LightGBM wrapper (train/query/save/load)
│   ├── evaluator.py           # Walk-forward evaluation and metrics
│   ├── walk_forward.py        # Walk-forward validation framework
│   ├── visualizer.py          # Plotting and visualization
│   ├── features/
│   │   └── alpha_factory.py   # Feature generation engine (80 features)
│   └── live_execution/
│       ├── data_manager.py    # Three-Tier data manager (seed → cache → backfill)
│       ├── ibkr_client.py     # IBKR connection, data, front-month, orders
│       ├── live_trader.py     # Live execution engine (Two-Stream)
│       ├── telemetry.py       # SQLite logging (smoothed + raw front-month)
│       └── utils/
│           └── time_utils.py  # IBKR duration string utilities
├── models/
│   ├── registry/              # Archived model bundles
│   │   ├── EXP-017_S_Ultimate/
│   │   └── EXP-020_S_Ultimate_Short/
│   └── final_model.pkl        # Current production model
├── data/
│   ├── raw/                   # Source OHLCV CSVs (cl-5m_bk.csv = immutable seed)
│   └── processed/             # ML-ready parquet + warm_start_cache.parquet
├── agent/                     # Automation scripts (backtester, sweeps, etc.)
├── tests/                     # Pytest test suite (174 tests)
└── reports/                   # Evaluation outputs
```
