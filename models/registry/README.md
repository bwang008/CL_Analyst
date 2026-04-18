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
| 2026-02-23 | EXP-020 | TARGET_TRIPLE_2x1_24H_SHORT | Short | 70.0% | 2.39 | Short Sniper (panic-selling). Friction: commission 2.50/side, slippage 0.03/side, multiplier 1000. Backtest on inner vault window. |
| 2026-02-28 | EXP-020-FLAT | TARGET_TRIPLE_2x1_24H_SHORT | Short | 29.2% | 2.26 | Concurrent flat sizing candidate (threshold 0.60, TP 5.0x, SL 0.75x) using aligned processed OHLCV data. |
| 2026-03-06 | EXP-025 | TARGET_TRIPLE_2x1_24H_LONG | Long | N/A | N/A | OOS walk-forward Buy model cutoff 2022-01-01 |
| 2026-03-06 | EXP-026 | TARGET_TRIPLE_2x1_24H_SHORT | Short | N/A | N/A | OOS walk-forward Sell model cutoff 2022-01-01 |
| 2026-03-11 | EXP-030 | TARGET_TRIPLE_2x1_24H_LONG | Long | N/A | N/A | Optuna v2 logloss bake-off (trial #114, set_07, .67M PnL) |
| 2026-03-11 | EXP-031 | TARGET_TRIPLE_2x1_24H_LONG | Long | N/A | N/A | OOS training with Optuna v2 logloss-optimized hyperparams on set_08 (trial #86, +15 exhaustion features vs set_07) |
| 2026-03-12 | EXP-032 | TARGET_TRIPLE_2x1_24H_SHORT | Short | N/A | N/A | OOS training with Optuna v2 logloss-optimized SHORT hyperparams on set_08 (trial #91, 106 trials, exhaustion features) |
| 2026-03-12 | EXP-025_retrain | TARGET_TRIPLE_2x1_24H_LONG | Long | N/A | N/A | Retrain EXP-025 long model to regenerate OOS predictions for ensemble2_alt backtest comparison |
| 2026-03-12 | EXP-026_retrain | TARGET_TRIPLE_2x1_24H_SHORT | Short | N/A | N/A | Retrain EXP-026 short model to regenerate OOS predictions for ensemble2_alt backtest comparison |
| 2026-03-13 | EXP-033 | TARGET_TRIPLE_2x1_24H_LONG | Long | N/A | N/A | Retrain LONG model on 154-feature set_08 (with 15 EXHAUST features). Uses EXP-031 Optuna hyperparams. Previous EXP-031 was trained on 139-feature set_08 before EXHAUST features were added. |
| 2026-04-16 | scout_5m_v2_long_E2E | TARGET_TRIPLE_2x1_24H_LONG | Long | N/A | N/A | e2e_alpha_factory_long; trade_threshold=None; features=Unknown; backtest sourced from N/A |
| 2026-04-16 | scout_5m_v2_short_E2E | TARGET_TRIPLE_2x1_24H_SHORT | Short | N/A | N/A | e2e_alpha_factory_short; trade_threshold=None; features=Unknown; backtest sourced from N/A |
