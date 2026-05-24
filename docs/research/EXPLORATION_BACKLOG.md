# Exploration Backlog — Optuna & Model Improvement

Future experiments to explore once the current set_07 vs set_08 bake-off is complete.

**Priority ranking:**

| # | Exploration | Impact | Time Cost |
|:-:|-------------|:------:|:---------:|
| 1 | Wider hyperparameter ranges | 🟢 High | ~2× per trial |
| 2 | Bake-off remaining metrics (f1, f0.5, sharpe) | 🟢 High | Same as current |
| 3 | More trials (200-300) | 🟡 Medium | Linear |
| 4 | Additional search dimensions (boosting_type, etc.) | 🟡 Medium | Moderate |
| 5 | Multi-objective optimization | 🟡 Medium | High complexity |
| 6 | Full walk-forward Optuna (all 68 folds) | 🔴 Low | 10× slower |

---

## 1. Wider Hyperparameter Search Ranges

**Current ranges** (hardcoded in `agent/optuna_lgbm_search_v2.py`, lines 278-289):

| Parameter | Current | Wider Option | Why |
|-----------|:-------:|:------------:|-----|
| `num_leaves` | 15–63 | 15–**127** | More complex trees for 1.2M row dataset |
| `min_child_samples` | 50–300 | **20**–300 | Allow finer-grained leaf splits |
| `learning_rate` | 0.01–0.1 | **0.005**–0.1 | Slower learning + more estimators = potentially better generalization |
| `feature_fraction` | 0.4–0.9 | 0.3–**1.0** | Allow full feature usage |
| `bagging_fraction` | 0.4–0.9 | 0.3–**1.0** | Allow full bagging |
| `reg_alpha` | 0.1–10.0 | **0.01**–10.0 | Less regularization option |
| `reg_lambda` | 0.1–10.0 | **0.01**–10.0 | Less regularization option |
| `max_depth` | 3–8 | 3–**12** | Deeper trees for complex interactions |
| `n_estimators` | 500–2000 | 500–**3000** | More boosting rounds (pair with lower learning rate) |

**To run:** Edit lines 278-289 in `optuna_lgbm_search_v2.py`, use a new `--study-name` (e.g. `wf_v2_long_logloss_wide`), and run 150+ trials (wider space needs more exploration).

**Risk:** Wider ranges with too few trials = poor convergence. Wider `num_leaves` + `max_depth` = longer trials. Estimate: ~2× time per trial with max ranges.

---

## 2. Full Walk-Forward Optuna (All 68 Folds)

Currently Optuna samples **5-8 folds** per trial for speed. Using all 68 folds would give the most accurate parameter evaluation but is ~10× slower.

**Math:**
- Current: ~8 min/trial × 100 trials = **~6h** (3 workers)
- Full WF: ~54 min/trial × 100 trials = **~30h** (3 workers) = **~1.25 days**

**How to implement:** In `optuna_lgbm_search_v2.py`, change the fold sampling logic (~line 300-320) to use all folds instead of `random.sample(folds, k=n_sample_folds)`.

**Expected benefit:** Marginal. The sampled approach is standard in ML — if params score well on 5 random folds, they almost certainly score well on 68. Main value: catches rare edge cases where params overfit the sampled folds.

**Recommendation:** Only worth trying after all other experiments are done. Run a single comparison: take the current best params and evaluate on all 68 folds to validate the sampling approach.

---

## 3. More Trials (200-300) on Current Ranges

Linear scaling: 200 trials = 2× the time, 300 = 3×.

**Diminishing returns curve:**
- First 30-50 trials: Biggest gains (random exploration → smart narrowing)
- 50-100 trials: Refinement
- 100-200: Marginal improvements
- 200+: Diminishing returns

EXP-030 found its best at trial #114/119 — so more trials can help but gains are small.

**Recommendation:** Better ROI to widen search ranges (Exploration #1) with 100 trials than to run 200 trials on current ranges.

---

## 4. Bake-off: Remaining Metrics

EXP-030 used `logloss`. Still need to compare:

```bash
# F1 (precision-recall balance)
python agent/optuna_lgbm_search_v2.py --ml-metric f1 --n-trials 100 \
  --data C:\CL_Analyst_Data\data\processed\CL_set_07.parquet \
  --target TARGET_TRIPLE_2x1_24H_LONG --study-name wf_v2_long_f1

# F0.5 (precision-heavy — fewer but higher-quality trades)
python agent/optuna_lgbm_search_v2.py --ml-metric f0.5 --n-trials 100 \
  --data C:\CL_Analyst_Data\data\processed\CL_set_07.parquet \
  --target TARGET_TRIPLE_2x1_24H_LONG --study-name wf_v2_long_f05

# Sharpe (requires strategy config for backtest in the loop)
python agent/optuna_lgbm_search_v2.py --ml-metric sharpe --n-trials 100 \
  --data C:\CL_Analyst_Data\data\processed\CL_set_07.parquet \
  --target TARGET_TRIPLE_2x1_24H_LONG --study-name wf_v2_long_sharpe \
  --strategy-config configs/strategies/OPTUNA_EXP-030_Set07.json
```

**Note:** Run these on whichever dataset wins the set_07 vs set_08 comparison.

---

## 5. Additional Search Dimensions (Not Currently Searched)

These LightGBM parameters are currently fixed but could be added to the search:

| Parameter | Current Fixed Value | Search Range Idea |
|-----------|:-------------------:|:-----------------:|
| `boosting_type` | `gbdt` | `{gbdt, dart, goss}` |
| `bagging_freq` | searched 1-7 | could try 0 (disabled) |
| `scale_pos_weight` | not set (uses downsample) | 1.0–10.0 (alternative to downsample) |
| `path_smooth` | not set | 0.0–10.0 (regularization for small leaves) |

**How:** Add `trial.suggest_categorical()` or `trial.suggest_float()` calls in the `objective()` function.

---

## 6. Multi-Objective Optimization

Instead of optimizing a single metric, Optuna supports **multi-objective** optimization (e.g., maximize logloss AND maximize Sharpe simultaneously). This produces a Pareto front of solutions rather than a single best.

**Implementation:** Use `optuna.create_study(directions=["maximize", "maximize"])` and return two values from the objective function. Requires reworking the objective function to compute both metrics per trial.

**Complexity:** High. Defer until single-objective bake-off is complete.
