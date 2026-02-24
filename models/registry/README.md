# Model Registry

Curated archived model bundles by Experiment ID.

## Purpose

This registry is the permanent catalog for production-candidate models.
Models are archived intentionally by Experiment ID (no auto-overwrite by a single metric).

Each archived bundle contains:

- `*.pkl`: the serialized trained model artifact
- `config.json`: experiment configuration (target, features summary, thresholds, Optuna params)
- `metrics.json`: classification + backtest summary
- `backtest.csv`: backtest summary row

## Archive Command

From project root:

```powershell
python agent/archive_model.py --experiment-id EXP-017
```

Optional explicit model path:

```powershell
python agent/archive_model.py --experiment-id EXP-017 --model-path models/final_model.pkl
```

## Catalog

| Date | Experiment ID | Target | Type (Long/Short) | Win Rate | Profit Factor | Notes |
|------|---------------|--------|-------------------|----------|---------------|-------|
| 2026-02-22 | EXP-017 | TARGET_TRIPLE_2x1_24H_LONG | Long | 86.8% | 14.22 | S_Ultimate; features=MACD, ADX, Microstructure, Volatility Regime + all original features; backtest summary sourced from REPORT.log |
