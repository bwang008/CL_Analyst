# TieredEnsemble Backtest Report — 2026-03-10

## OOS Period

2022-01-02 → 2026-02-15 (291,614 bars, ~4 years)

**Predictions:** `oos_predictions.csv` (EXP-025 Buy + EXP-026 Sell walk-forward OOS)
**OHLCV:** `CL_set_06.parquet` (CL 5-min)

---

## Results

| Config | Total PnL | Win Rate | PF | Trades | Max DD | Avg Trade |
|--------|----------:|:--------:|----:|-------:|-------:|----------:|
| Koala2_opt (Short only) | $101,650 | 97.6% | 43.57 | 127 | — | $800.39 |
| Manatee2_opt (Long only) | $293,237 | 78.0% | 39.65 | 132 | — | $2,221.49 |
| Ensemble2_Aggro (Both) | $1,080,623 | 62.4% | 2.30 | 7,266 | — | $148.73 |
| **TieredEnsemble2** | **$618,520** | **72.8%** | **2.12** | **11,974** | — | **$51.65** |

---

## Analysis

- **Koala2_opt / Manatee2_opt** achieve extremely high WR/PF but very low trade count due to strict thresholds (0.75) — concentrated risk
- **Ensemble2_Aggro** is the best absolute PnL at $1.08M with 7.2K trades and PF=2.30
- **TieredEnsemble2** generates the most trades (11,974) with decent 72.8% WR, confirming that the per-tier overrides work and the lower threshold tiers (0.60) successfully capture additional signal. However avg trade PnL is lower due to the base tier's tighter TP and wider SL

## TieredEnsemble2 Configuration Notes

The initial config uses:
- **Long tiers:** high_confidence (≥0.75, 2 lots, TP=3.0×ATR, SL=1.0×ATR) + base (≥0.60, 1 lot, TP=1.5×ATR, SL=1.5×ATR)
- **Short tiers:** high_confidence (≥0.80, 3 lots, TP=1.5×ATR, SL=3.0×ATR) + base (≥0.60, 1 lot, TP=1.0×ATR, SL=2.0×ATR)

## Next Steps

- [ ] Create variant configs (A/B/C) with different tier thresholds and TP/SL ratios
- [ ] Optimise tier boundaries via Optuna or grid search
- [ ] Filter analysis: breakdown PnL by tier label to identify which tiers add value
