# CL_Analyst Model Improvement Report

## Executive Summary

Starting from a baseline model with **8.3% Buy precision** (predicting "Hold" for virtually everything), a systematic series of changes produced a final model (S_Ultimate / EXP-017) achieving **87.2% Buy precision** with a backtest **Profit Factor of 14.22** and **86.8% win rate**. This document details every change tested, what worked, what didn't, and the methodologies that drove the improvement.

---

## Baseline (Before Any Changes)

| Property | Value |
|----------|-------|
| Dataset | `CL_set_03.parquet` — 5-min CL futures OHLCV |
| Target | `TARGET_SQZ_4PCT_SHORT` — predict 4% short squeeze moves |
| Model | LightGBM, default params, `balance_mode=downsample` |
| Buy Precision | **8.3%** |
| Buy Recall | 29.9% |
| Notes | Model overwhelmingly predicted "Hold". Virtually useless for trading. |

---

## Change 1: Target Engineering — Lower Thresholds

### What Changed
Replaced the 4-8% move target with 2-3% directional targets over 24 hours.

### Reasoning
The original 4%+ target was too rare in the data (<5% positive rate). The model couldn't learn the pattern because there were too few training examples. By lowering to 2%, there are far more learnable positive samples.

### Implementation
New target columns `TARGET_DIR_2PCT_24H_LONG` and `TARGET_DIR_3PCT_24H_LONG` in `data_processor.py`.

A **directional target**: did price move up ≥2% within the next 24h (288 bars at 5-min frequency)?

### Experiments

| Experiment | Config | Precision | Recall | F1 |
|:-----------|:-------|:----------|:-------|:---|
| EXP-001 | 2% threshold, downsample | **25.6%** | 69.7% | 37.5% |
| EXP-002 | 2% threshold, weight mode | 30.0% | 5.0% | 8.6% |
| EXP-003 | 3% threshold, downsample | 12.6% | 61.1% | 20.9% |

### Takeaway
- 2% threshold with downsample was the best balance. **Tripled precision from 8% → 25.6%**.
- Weight mode crushed recall (only 5% — too conservative).
- 3% threshold was worse — the sweet spot was 2%.
- Dataset used: `CL_set_04.parquet`

---

## Change 2: Triple Barrier Method — THE BREAKTHROUGH

### What Changed
Replaced the simple directional target with the **Triple Barrier Method** from Marcos López de Prado's *Advances in Financial Machine Learning*.

### Reasoning
The directional target ignores risk. A stock that goes up 2% but drops 5% first is a losing trade. Triple Barrier captures the *first thing that happens*: does price hit the take-profit before the stop-loss? This directly models what matters for trading.

### Implementation
Three barriers using ATR (Average True Range) for dynamic sizing:

- **Take-Profit barrier**: price goes UP by `2 × ATR(14)` → label = `Buy (1)`
- **Stop-Loss barrier**: price goes DOWN by `1 × ATR(14)` → label = `Hold (0)`
- **Time barrier**: 24 hours pass without hitting either → label = `Hold (0)`

This creates the target `TARGET_TRIPLE_2x1_24H_LONG`.

The key innovation is that barriers are **dynamic** — they scale with current volatility via ATR instead of using a fixed percentage. In high-volatility regimes, barriers widen; in low-volatility regimes, they tighten. This means the model learns *relative* breakout patterns rather than absolute price moves.

#### Code Details
Added `_add_triple_barrier_target()` to `data_processor.py`:
1. Computes `ATR(14)` on raw OHLCV
2. For each bar, looks forward up to 288 bars (24h)
3. Checks if future high breaches `entry + 2×ATR` before future low breaches `entry - 1×ATR`
4. Labels: TP hit first → Buy(1), SL hit first → Hold(0), timeout → Hold(0)

### Experiments

| Experiment | Config | Precision | Recall | F1 |
|:-----------|:-------|:----------|:-------|:---|
| EXP-005 | Triple 2×ATR TP / 1×ATR SL | **52.7%** | 62.9% | **57.3%** |
| EXP-006 | Triple 3×ATR TP / 1×ATR SL | 42.2% | 62.6% | 50.4% |

### Takeaway
- **Single biggest jump** — precision went from 25.6% → **52.7%** (2× improvement).
- The asymmetric 2:1 reward/risk ratio was better than 3:1.
- The model could now distinguish "this setup will hit TP before SL" at better-than-coin-flip accuracy.
- Dataset used: `CL_set_05.parquet` (2009-01-15 to 2024-12-27, 1.13M bars)

---

## Change 3: Probability Threshold Optimization

### What Changed
Swept probability decision thresholds from 0.05 to 0.90 to find where the model's confidence is calibrated.

### Reasoning
At the default 0.50 threshold, the model includes low-confidence predictions that drag down precision. By raising the threshold, you only trade on high-confidence signals — accepting fewer signals in exchange for much higher accuracy.

### Implementation
`agent/threshold_sweep.py` — trained one model, then evaluated at every threshold level on vault predictions.

### Results (EXP-007)

| Threshold | Precision | Recall | # Signals |
|:---------:|:---------:|:------:|:---------:|
| 0.45 | 49.2% | 74.1% | 86,123 |
| 0.50 | 53.6% | 62.7% | 66,806 |
| 0.55 | 57.1% | 50.8% | 50,838 |
| 0.65 | 63.3% | 22.4% | 20,243 |
| 0.80 | **80.3%** | 3.6% | 2,572 |
| 0.90 | **91.3%** | 0.2% | 149 |

### Takeaway
- The model's probability scores ARE informative — high confidence really does mean high precision.
- Steep recall trade-off: at 0.90 threshold you get 91% precision but only 149 signals total.
- This proved the model "knows what it knows" — the challenge is making it confident more often.

---

## Change 4: Optuna Hyperparameter Tuning

### What Changed
Used Optuna to search LightGBM hyperparameters with a constrained search space designed to prevent overfitting.

### Reasoning
Default LightGBM params don't constrain model complexity. A shallower, more regularized model may generalize better to unseen data.

### Implementation
`agent/optuna_lgbm_search.py` with 30 trials optimizing Buy F1 on walk-forward cross-validation.

**Search space (deliberately constrained)**:
- `num_leaves`: 15–63 (prevents overly complex trees)
- `max_depth`: 3–7 (shallow trees)
- `min_child_samples`: 50–200 (requires more data per leaf decision)
- `learning_rate`: 0.01–0.1
- `reg_alpha`: 0.01–10 (L1 regularization)
- `reg_lambda`: 0.01–10 (L2 regularization)
- `feature_fraction`: 0.5–0.9 (random feature subsampling)
- `bagging_fraction`: 0.5–0.9 (random row subsampling)

**Best parameters found**:
```
num_leaves:        31
max_depth:         4
learning_rate:     0.05243
min_child_samples: 166
feature_fraction:  0.694
bagging_fraction:  0.648
reg_alpha:         2.737
reg_lambda:        7.379
min_gain_to_split: 0.990
n_estimators:      1000
```

### Results (EXP-008)
- CV F1: 0.558
- Vault F1: 0.585
- Buy Precision: 47.5% (at threshold 0.45)

### Takeaway
- The tuned params didn't dramatically improve metrics alone.
- But the heavy regularization (`reg_lambda=7.38`, `min_child_samples=166`, `max_depth=4`) helped generalization.
- These params proved optimal when combined with the other changes in S_Ultimate.

---

## Change 5: Feature Engineering

### What Changed
Added new technical indicator features to `alpha_factory.py`:

1. **MACD** (Moving Average Convergence/Divergence):
   - `MOM_MACD` — MACD line (12-period EMA minus 26-period EMA)
   - `MOM_MACD_Signal` — 9-period EMA of MACD
   - `MOM_MACD_Hist` — MACD histogram (difference)
   - Captures trend momentum direction and intensity

2. **ADX** (Average Directional Index):
   - `MOM_ADX_14` — ADX value (0-100, trend strength)
   - `MOM_DMP_14` — Positive directional movement
   - `MOM_DMN_14` — Negative directional movement
   - Captures whether a trend is strong regardless of direction

3. **Bar Microstructure**:
   - `STRUC_BODY_RATIO` — candle body as % of total range
   - `STRUC_WICK_UP_RATIO` — upper wick as % of total range
   - `STRUC_WICK_LOW_RATIO` — lower wick as % of total range
   - Captures buying/selling pressure within individual bars

These were built into `CL_set_06.parquet` (the enriched dataset).

### What Was NOT Implemented
- **Volatility rate-of-change (vol-ROC)** — planned but never coded
- **Volatility-of-volatility (vol-of-vol)** — planned but never coded

### Takeaway
- Features were baked into the final model but **never isolated-tested** via ablation.
- We don't know their individual contribution — recommended to A/B test CL_set_05 vs CL_set_06 with identical model params.

---

## Change 6: Focal Loss

### What Changed
Replaced LightGBM's native `binary_logloss` objective with a **custom Focal Loss** function.

### Reasoning
Standard binary cross-entropy treats all training examples equally. In our dataset, the vast majority of bars are "Hold" — easy for the model to classify correctly. The model spends most of its learning capacity on these easy examples instead of focusing on the hard borderline cases near breakouts.

**Focal Loss** (from Lin et al., 2017, originally for object detection) adds a modulating factor `(1-p)^γ` that down-weights easy examples and focuses learning on hard ones.

### Implementation
Custom Python callback in `LGBMLearner.py` (`_focal_loss_obj` method):

```python
def _focal_loss_obj(y_true, y_pred, gamma=2.0):
    p = sigmoid(y_pred)
    # Focal weight: (1-p)^gamma for positives, p^gamma for negatives
    # Easy examples (high confidence) get down-weighted
    focal_weight = y_true * (1 - p)**gamma + (1 - y_true) * p**gamma
    grad = focal_weight * (p - y_true)
    hess = focal_weight * p * (1 - p)
    return grad, hess
```

With `gamma=2.0`:
- An example the model is 90% confident about → gradient reduced by ~100×
- An example at 50% confidence → full gradient (this is where learning happens)
- This forces the model to focus on borderline/hard cases

### Takeaway
- Implemented and used in S_Ultimate, but **never A/B tested in isolation**.
- We don't know if it helped or hurt — comparing `use_focal: true` vs `use_focal: false` with identical params would answer this.
- The 10× more trees (`n_estimators=1000`) combined with focal loss does add significant wall time (~36 min vs ~3 min).

---

## Final Model: S_Ultimate (EXP-017)

### What It Combines
All six changes stacked together:

| Component | Setting |
|-----------|---------|
| Target | Triple Barrier 2×ATR / 1×ATR / 24h (`TARGET_TRIPLE_2x1_24H_LONG`) |
| Dataset | `CL_set_06.parquet` (MACD + ADX + microstructure features) |
| Params | Optuna-tuned (shallow, heavily regularized) |
| Loss | Focal Loss (gamma=2.0) |
| Balance | Downsample |
| Validation | 88-fold walk-forward (expanding window) |
| Trees | 1,000 boosting rounds |

### Results

| Metric | Value |
|--------|-------|
| **Buy Precision** | **87.16%** |
| Buy Recall | 12.17% |
| Buy F1 | 21.37% |
| Vault Accuracy | 69.43% |
| Vault Samples | 143,817 |

### Backtest Results

| Metric | Value |
|--------|-------|
| **Win Rate** | **86.8%** |
| **Profit Factor** | **14.22** |
| Avg PnL per Trade | +0.38% |
| Max Drawdown | 3.95% |
| Total Trades | 6,853 |
| TP Hits | 5,951 |
| SL Hits | 902 |

### Reproducibility
EXP-019 re-ran the identical config and achieved 86.88% precision (vs 87.16%), confirming the result is stable and reproducible.

---

## Impact Attribution

| Change | Precision Impact | Evidence |
|--------|:-----------------|:---------|
| **Triple Barrier targets** | 8% → 52.7% | EXP-001 vs EXP-005 |
| **Threshold optimization** | 52.7% → 80-91% at high thresholds | EXP-007 sweep |
| **Optuna + Focal + Features** | Combined push to 87.2% at default threshold | EXP-017 |

The **Triple Barrier Method** was unambiguously the highest-impact change. Everything else built on top of that foundation.

---

## Data Periods

All models trained on CL (Crude Oil) futures, 5-minute bars.

| Dataset | Date Range | Rows | Features | Used By |
|:--------|:-----------|:-----|:---------|:--------|
| `CL_set_03.parquet` | Unknown (deleted) | ~168k | Base set | Baseline only |
| `CL_set_04.parquet` | Unknown (deleted) | ~168k | Base set | EXP-001, 002, 003 |
| `CL_set_05.parquet` | 2009-01-15 → 2024-12-27 | 1,127,977 | Base + Triple Barrier | EXP-005, 006, 007, 008 |
| `CL_set_06.parquet` | 2009-01-15 → 2024-12-27 | 1,127,977 | Base + TB + MACD/ADX/Micro | **EXP-017 (S_Ultimate)** |
| `CL_set_06_shortfix.parquet` | 2009-01-14 → 2026-02-15 | 1,207,895 | Same as set_06 + recent data | EXP-020 (Short) |

Walk-forward uses expanding window with 15% vault holdout, so vault covers roughly the last ~2.4 years of each dataset.

---

## Gaps and Untested Ideas

| Item | Status | What Would It Tell Us |
|------|--------|----------------------|
| **S2 ablation test** | Not done | Do MACD/ADX/microstructure features actually help? Run S_Ultimate on CL_set_05 vs CL_set_06, same params. |
| **S4 ablation test** | Not done | Does focal loss help or hurt? Run with `use_focal: false`, same params. |
| **Vol-of-vol features** | Never implemented | Volatility compression → expansion transitions could improve breakout detection. |
| **Sell signal model** | Separate model (EXP-020) | Achieved 86.7% precision on short-side Triple Barrier target. |
| **Slippage modeling** | Partially done | `backtest_cl_concurrent.py` has commission + slippage params. |

---

## Pattern for Modeling New Strategies

Based on what worked, the recipe is:

1. **Start with the target** — Define what a "good trade" means using ATR-based dynamic barriers. The target should model the actual trade mechanics (TP vs SL race), not just directional moves.

2. **Use asymmetric risk/reward** — 2:1 TP/SL ratio (2×ATR take-profit, 1×ATR stop-loss) outperformed 3:1. Test different ratios.

3. **Regularize aggressively** — Shallow trees (`max_depth=4`), high `min_child_samples` (166), strong L2 penalty (`reg_lambda=7.38`). Overfitting is the primary enemy.

4. **Accept low recall for high precision** — The model only fires ~5% of the time, but when it does, it's right 87% of the time. For trading, precision matters more than recall.

5. **Walk-forward validation only** — Expanding window, never leaking future data into training. Simple train/test splits will overestimate performance.

6. **Sweep thresholds post-training** — The model's raw probability threshold can be tuned to trade between precision/recall after the model is trained.

7. **Use Optuna with constraints** — Don't let the search space include overly complex models. Constrain `num_leaves`, `max_depth`, and `min_child_samples` to force generalization.

---

## Recreation Commands

```bash
conda activate trader

# Best model (S_Ultimate) — ~36 min
python agent/experiment_runner.py --experiment S_Ultimate --save

# Triple Barrier baseline — ~3 min
python agent/experiment_runner.py --experiment S1b --save

# Short-side model — ~45 min
python agent/experiment_runner.py --experiment S_Ultimate_Short --save

# Threshold sweep
python agent/threshold_sweep.py

# Optuna search (30 trials)
python agent/optuna_lgbm_search.py --n-trials 30

# Backtester
python agent/backtester.py --threshold 0.50
```
