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

