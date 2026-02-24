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
The live execution engine connects to IBKR (TWS or IB Gateway), monitors real-time CL futures data, runs inference using the S_Ultimate model, and executes bracket orders when buy signals are generated.

### Files
| File | Purpose |
|------|---------|
| `src/live_execution/ibkr_client.py` | IBKR connection manager, historical data, position queries, bracket orders |
| `src/live_execution/live_trader.py` | Main execution loop: cold start → live bars → features → inference → orders |
| `src/live_execution/telemetry.py` | SQLite telemetry backend (`data/live_telemetry.db`) |

### Prerequisites
1. Install and start **TWS** or **IB Gateway** in Paper Trading mode
2. Enable API connections: Configure → API → Settings
   - Port: `7497` (TWS) or `4002` (Gateway)
   - Enable ActiveX and Socket Clients

### Running
```bash
# Dry run (no real orders, logs signals only)
conda run -n trader python -m src.live_execution.live_trader --dry-run

# Live paper trading
conda run -n trader python -m src.live_execution.live_trader

# Custom port (IB Gateway)
conda run -n trader python -m src.live_execution.live_trader --port 4002
```

### Execution Loop
1. **Cold start**: Fetches 5 days of historical 5-min bars
2. **Subscribe**: Registers for live 5-min bar updates (`keepUpToDate=True`)
3. **On each new bar**:
   - Appends to rolling window (capped at ~11,000 bars)
   - Runs `AlphaFactory` to generate 80 features
   - Runs model inference (sigmoid on focal loss logits → probability)
   - If probability ≥ 0.45 and position is flat → places bracket order
4. **Bracket order**: Market buy + Limit TP (price + 2×ATR) + Stop SL (price − 1×ATR)

### Telemetry
All bars and signals are logged to `data/live_telemetry.db`:
- **`market_bars`** — every closed 5-min bar
- **`trade_ledger`** — every signal (Hold/Buy), confidence %, action taken, order details

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
│       ├── ibkr_client.py     # IBKR connection, data, orders
│       ├── live_trader.py     # Live execution engine
│       └── telemetry.py       # SQLite logging
├── models/
│   ├── registry/              # Archived model bundles
│   │   ├── EXP-017_S_Ultimate/
│   │   └── EXP-020_S_Ultimate_Short/
│   └── final_model.pkl        # Current production model
├── data/
│   ├── raw/                   # Source OHLCV CSVs
│   └── processed/             # ML-ready parquet files
├── agent/                     # Automation scripts (backtester, sweeps, etc.)
├── tests/                     # Pytest test suite
└── reports/                   # Evaluation outputs
```
