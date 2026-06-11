# Cross-Batch Canary Analysis: HS09 vs HS10

> [!NOTE]
> Analysis covers **6 canary batches** across 2 datasets and 3 walk-forward periods.
> All runs use 20 Optuna trials (canary-tier). Edge signals at this tier are weak by design — we're looking for **consistency patterns**, not absolute performance.

---

## 1. Batches Analyzed

| Batch | Dataset | Walk-Forward | Sharpe | Sortino |
|---|---|---|---|---|
| `batch_20260609_0933_HS10_Canary_WFB` | HS10 | WFB | ✅ | ✅ |
| `batch_20260609_1120_HS09_Canary_WFB` | HS09 | WFB | ✅ | ✅ |
| `batch_20260609_1921_HS10_Canary_WFC_1` | HS10 | WFC | ✅ | ✅ |
| `batch_20260610_0404_HS10_Canary_WFC_2` | HS10 | WFC | ✅ | ✅ |
| `batch_20260610_0544_HS09_Canary_WFC` | HS09 | WFC | ✅ | ✅ |
| `batch_20260610_1405_HS10_Canary_WFD` | HS10 | WFD | ✅ | ✅ |

---

## 2. HS09 vs HS10: Head-to-Head Comparison

### 2.1 Walk-Forward B (WFB) — Same Period, Different Dataset

#### HS10 WFB (batch_20260609_0933)
- **Pre-opt PnL range**: All targets heavily **positive** ($54k–$89k). Pre-opt PF ≥ 1.16 across the board.
- **Top Sharpe ensemble**: `HS10 3x1 24H / HS10 4x1 12H` (LL+AP) — PnL opt $7,840 (702 trades, very conservative post-opt)
- **Top Sortino ensemble**: `HS10 3x1 24H / HS10 5x1 24H` (LL+AP) — PnL opt $15,261
- **Holdout**: Mixed but several positive holdout values (e.g. `4x1 6H / 4x1 12H` LL+LL: **+$5,187** Sharpe)

#### HS09 WFB (batch_20260609_1120)
- **Pre-opt PnL range**: Nearly all targets **deeply negative** ($-70k to +$2k). Pre-opt PF mostly ≤ 0.95.
- **Top Sharpe ensemble**: `HS09 3x1 6H` (LL+LL) — PnL opt $15,634 (512 trades)
- **Top Sortino ensemble**: `HS09 5x1 24H / HS09 3x1 6H` (LL+AP) — PnL opt $13,204
- **Holdout**: Mostly negative except `3x1 6H / 5x1 24H` AP+LL: **+$1,800**

> [!IMPORTANT]
> **WFB Verdict: HS10 dramatically outperforms HS09.** HS10 pre-optimization baselines are profitable (PF 1.16–1.32) while HS09 baselines are net losers (PF 0.79–1.01). This is the strongest signal in the dataset — the new HS10 features produce meaningfully better raw models in this walk-forward window.

---

### 2.2 Walk-Forward C (WFC) — Same Period, Different Dataset

#### HS10 WFC_1 (batch_20260609_1921)
- **Pre-opt PnL range**: Mixed. Some targets strong (3x1 24H combos at $24k–$44k), others negative (4x1 6H at -$2.5k to -$37k).
- **Top Sharpe ensemble**: `HS10 3x1 24H / HS10 4x1 24H` (LL+LL) — PnL opt **$50,320** with holdout **$-1,771**
- **Top Sortino individual**: `HS10 4x1 6H` (LL, long) — PnL opt $45,039

#### HS10 WFC_2 (batch_20260610_0404)
- **Pre-opt PnL range**: Several strong baselines ($17k–$40k for 4x1 12H, 4x1 24H combos).
- **Top Sharpe ensemble**: `HS10 4x1 12H / HS10 4x1 12H` (LL+AP) — PnL opt **$62,277** but holdout **$-2,805**
- **Positive holdout winners**: `5x1 12H / 3x1 12H` (LL+LL): **+$3,057**; `5x1 12H / 4x1 12H` (LL+LL): **+$2,338**; `5x1 12H / 4x1 12H` (LL+AP): **+$6,188**

#### HS09 WFC (batch_20260610_0544)
- **Pre-opt PnL range**: Mixed. `5x1 24H / 4x1 24H` combos at $32k–$35k (LL_LONG), but `4x1 24H / 5x1 24H` combos deeply negative.
- **Top Sharpe ensemble**: `5x1 12H / 4x1 24H` (AP+AP) — PnL opt $16,564
- **Positive holdout winners**: `5x1 24H / 5x1 24H` (LL+LL): **+$97**; `5x1 24H / 5x1 24H` (AP+AP): **+$1,426**; `4x1 24H / 5x1 24H` (LL+AP): **+$212**

> [!NOTE]
> **WFC Verdict: HS10 shows stronger absolute PnL magnitudes** (opt PnL peaks at $50k–$62k vs $16k–$43k for HS09). However, HS09 shows slightly better holdout stability on its best combos. HS10's advantage is in top-line optimized PnL; HS09's advantage is in some ensembles showing less holdout degradation.

---

### 2.3 Walk-Forward D (WFD) — HS10 Only

#### HS10 WFD (batch_20260610_1405)
- **Pre-opt PnL**: `3x1 24H` long model baseline is modest ($1k–$10k range, PF ~1.0–1.04).
- **Top Sharpe ensemble**: `3x1 24H / 3x1 12H` (LL+AP) — PnL opt **$54,912** with holdout **+$1,320** ✅
- **Positive holdout winners**:
  - `3x1 24H / 4x1 6H` (LL+AP): **+$1,461**
  - `3x1 24H / 3x1 12H` (LL+AP): **+$1,320**
  - `3x1 24H / 3x1 24H` (LL+AP): **+$720**
  - `3x1 24H / 3x1 24H` (LL+LL): **+$1,367**
  - `3x1 24H / 4x1 24H` (LL+LL): **+$546**

> [!TIP]
> WFD is notable because despite weak pre-opt baselines (PF ~1.0), optimization finds real edge. The `3x1 24H` long model is the universal backbone — it appears in every single ensemble. Multiple combos produce **positive holdout PnL**, which is a strong validation signal for canary-tier runs.

---

## 3. Target-Level Performance Heatmap

### 3.1 Targets as LONG Model (Ensemble Left Side)

| Target | HS10 WFB Pre-PF | HS10 WFC Pre-PF | HS10 WFD Pre-PF | HS09 WFB Pre-PF | HS09 WFC Pre-PF | Verdict |
|---|---|---|---|---|---|---|
| **3x1 6H** | 1.25 ↑ | — | — | 0.95 ↓ | — | HS10 >> HS09 |
| **3x1 12H** | — | — | — | — | — | Insufficient data |
| **3x1 24H** | 1.16–1.32 ↑ | 0.99–1.15 → | 1.00–1.04 → | — | — | HS10 strong in WFB, flat elsewhere |
| **4x1 6H** | 1.29–1.31 ↑ | — | — | — | — | Strong when present |
| **4x1 12H** | — | 1.00–1.18 → | — | — | — | Mediocre |
| **4x1 24H** | — | 0.95–1.15 → | — | — | 0.91–1.11 ↓ | Below average both |
| **5x1 12H** | — | 0.98–1.00 → | — | — | 1.00 → | Flat / no edge |
| **5x1 24H** | — | 0.92–1.03 → | — | 0.84–1.01 ↓ | 0.91–1.13 → | HS09 weak, HS10 mixed |

### 3.2 Targets as SHORT Model (Ensemble Right Side)

| Short Target | Consistently Positive Holdout? | Opt PnL Magnitude | Notes |
|---|---|---|---|
| **3x1 6H** | HS10 WFB: mixed | Moderate | HS09 WFB: short side very weak |
| **3x1 12H** | HS10 WFD: ✅ multiple positive | High ($24k–$55k) | **Best short-side target for HS10** |
| **3x1 24H** | HS10 WFD: ✅ positive | High ($15k–$26k) | Strong when paired with 3x1 24H long |
| **4x1 6H** | Mixed | High ($9k–$39k) | High opt PnL but volatile holdout |
| **4x1 12H** | HS10 WFB: positive; WFC: positive | Moderate ($7k–$62k) | Decent consistency |
| **4x1 24H** | HS10 WFC: ✅; HS09 WFC: mixed | High ($19k–$50k) | **Most consistently positive holdout** |
| **5x1 12H** | HS10 WFD: negative | Moderate ($9k–$29k) | Inconsistent |
| **5x1 24H** | Mostly negative holdout | Low–Moderate | Weakest short target |

---

## 4. Ensemble Consistency Patterns

### 4.1 🏆 Consistently Strong Ensembles (Appear in Multiple Batches)

| Ensemble | Batches Present in Top-8 | Avg Holdout PnL Direction | Assessment |
|---|---|---|---|
| **3x1 24H / 4x1 24H** (HS10, LL+LL) | WFC_1 (#1), WFD (#4, Sortino #2) | Mixed but mostly positive | ⭐ **Core keeper** |
| **3x1 24H / 3x1 12H** (HS10, LL+AP) | WFD (#3 Sharpe, #6 Sortino) | **+$1,320 / +$8,475** | ⭐ **Best holdout of any combo** |
| **3x1 24H / 4x1 6H** (HS10, LL+AP) | WFC_1, WFD | Mixed | ⭐ Promising |
| **3x1 24H / 3x1 24H** (HS10, LL+LL) | WFD (#7 Sharpe, #3 Sortino) | **+$1,367 / +$844** | ⭐ **Consistently positive holdout** |
| **3x1 24H / 5x1 12H** (HS10, LL+AP) | WFC_1, WFD | Mixed | Worth keeping |
| **4x1 12H / 4x1 12H** (HS10, LL+AP) | WFC_2 (#1 Sharpe) | -$2,805 Sharpe | **High opt PnL but holdout concern** |
| **5x1 24H / 4x1 24H** (HS09, LL+AP/LL) | WFB, WFC | Mostly negative | ⚠️ Only survives with heavy optimization |
| **4x1 24H / 5x1 24H** (HS09, LL+AP) | WFC (Sortino #1) | +$1,901 | One strong showing |

### 4.2 🚫 Consistently Weak Patterns

| Pattern | Evidence | Assessment |
|---|---|---|
| **HS09 3x1 6H as Long** | WFB: PF 0.95, all combos deeply negative pre-opt | ❌ Unreliable |
| **HS09 5x1 24H as Long** | WFB: PF 0.84–1.01, all negative pre-opt | ❌ Consistently negative baseline |
| **HS10 4x1 6H / HS10 4x1 6H** (LL+LL, short side) | WFD: PF 0.92 pre-opt, $-27,903 pre-opt PnL, only $110 after opt | ❌ No edge, optimizer squeezes to 4 trades |
| **5x1 24H as Short target** | Mixed but holdout frequently negative (-$4k to -$10k) | ⚠️ Weakest short target |
| **Short-side models in general** | Post-optimization frequently collapses short to 0 trades | ⚠️ Short models struggle across both HS09 and HS10 |

---

## 5. HS09 vs HS10 Summary Verdict

| Dimension | HS09 | HS10 | Winner |
|---|---|---|---|
| **Pre-opt baseline PF (WFB)** | 0.79–1.01 | 1.16–1.32 | **HS10** by a mile |
| **Pre-opt baseline PF (WFC)** | 0.84–1.13 | 0.90–1.18 | **HS10** slightly |
| **Top optimized PnL** | $16k–$52k | $7k–$62k | **HS10** higher ceiling |
| **Holdout consistency** | 2–3 positive holdouts per batch | 3–5 positive holdouts per batch | **HS10** more positive holdouts |
| **Worst-case drawdown** | -$70k (5x1 24H combos) | -$56k (4x1 6H combos) | **HS10** (less extreme) |
| **Number of viable ensembles** | 2–3 per batch | 4–6 per batch | **HS10** more diverse |

> [!IMPORTANT]
> **Overall: HS10 is meaningfully better than HS09.** The new dataset produces:
> 1. **Stronger baselines** — more targets start with PF > 1.0 before optimization
> 2. **More positive holdouts** — a higher proportion of ensembles survive into holdout
> 3. **Higher peak PnL** — optimization finds deeper edges
> 4. **More viable diversity** — more distinct ensembles show promise, reducing concentration risk
>
> HS09 is not fatally broken but requires heavier optimization to extract edge, and fewer combos survive holdout. **Recommend prioritizing HS10 for scout/production runs.**

---

## 6. Target Pruning Recommendation for Scout Config

Based on the analysis above, here is the recommendation for which targets to keep vs cut in the HS10 scout config:

### ✅ KEEP (5 targets)

| Target | Rationale |
|---|---|
| **3x1 24H** | Universal backbone long model. Appears in every top ensemble. Positive holdout across multiple WF periods. |
| **3x1 12H** | Best short-side target in WFD. +$8,475 holdout (Sortino). Strong pairing with 3x1 24H. |
| **4x1 24H** | Most consistently positive holdout as short target. Strong in WFC and WFD. |
| **4x1 12H** | Decent consistency across WFB (positive holdout) and WFC. Good optPnL potential. |
| **4x1 6H** | High opt PnL when paired with 3x1 24H. Volatile but viable at scout-tier. |

### ⚠️ OPTIONAL (1 target)

| Target | Rationale |
|---|---|
| **3x1 6H** | Strong in WFB (PF 1.25) but missing from WFC/WFD sweeps. Include if budget allows; cut if you need to keep runs lean. |

### ❌ CUT (2 targets)

| Target | Rationale |
|---|---|
| **5x1 12H** | Mostly flat pre-opt (PF ~1.0). Generates signal but no consistent edge. Holdout frequently negative. |
| **5x1 24H** | Weakest short-side target (holdout consistently negative: -$3k to -$10k). As long model, PF < 1.0 in WFC. Not worth scout compute. |

---

## 7. Proposed Pruned Scout Config

Based on the above, here is a proposed 5-target (or 6 with 3x1 6H optional) scout config to replace the current 8-target [sweep_batch_hourset10_scout.json](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/configs/sweep_batch_hourset10_scout.json):

```json
{
  "_comment": "HourSet_10 SCOUT (PRUNED) — 5 targets after canary analysis. Removed 5x1_12H and 5x1_24H for consistently weak performance.",
  "defaults": {
    "machine_type": "c2-standard-16",
    "provisioning_model": "STANDARD",
    "gcs_data_path": "gs://cltrainer-optuna-results/data/cl-1h_bk_HourSet_10.parquet",
    "strategy_config": "hourly_ensemble_010.json",
    "metrics": "logloss,average_precision",
    "timeout_minutes": 360,
    "max_concurrent_vcpus": 288,
    "vcpus_per_vm": 16,
    "max_concurrent_vms": 12,
    "n_trials": 80,
    "max_depth_min": 3,
    "max_depth_max": 8,
    "num_leaves_min": 15,
    "num_leaves_max": 64,
    "max_n_estimators": 1500,
    "early_stopping_rounds": 30,
    "max_folds": 5,
    "learning_rate_min": 0.001,
    "learning_rate_max": 0.05,
    "min_child_samples_min": 100,
    "min_child_samples_max": 500,
    "feature_fraction_min": 0.4,
    "feature_fraction_max": 0.8,
    "post_optimizer_trials": 500,
    "post_optimizer_holdout_months": 6
  },
  "experiments": [
    {
      "label": "HS10 3x1 12H",
      "target_long": "TARGET_TRIPLE_3x1_12H_LONG",
      "target_short": "TARGET_TRIPLE_3x1_12H_SHORT",
      "gcs_prefix": "sweep_hs10_3x1_12h_scout"
    },
    {
      "label": "HS10 3x1 24H",
      "target_long": "TARGET_TRIPLE_3x1_24H_LONG",
      "target_short": "TARGET_TRIPLE_3x1_24H_SHORT",
      "gcs_prefix": "sweep_hs10_3x1_24h_scout"
    },
    {
      "label": "HS10 4x1 6H",
      "target_long": "TARGET_TRIPLE_4x1_6H_LONG",
      "target_short": "TARGET_TRIPLE_4x1_6H_SHORT",
      "gcs_prefix": "sweep_hs10_4x1_6h_scout"
    },
    {
      "label": "HS10 4x1 12H",
      "target_long": "TARGET_TRIPLE_4x1_12H_LONG",
      "target_short": "TARGET_TRIPLE_4x1_12H_SHORT",
      "gcs_prefix": "sweep_hs10_4x1_12h_scout"
    },
    {
      "label": "HS10 4x1 24H",
      "target_long": "TARGET_TRIPLE_4x1_24H_LONG",
      "target_short": "TARGET_TRIPLE_4x1_24H_SHORT",
      "gcs_prefix": "sweep_hs10_4x1_24h_scout"
    }
  ]
}
```

> [!TIP]
> **Compute savings**: Cutting from 8 → 5 targets saves **37.5%** of VM-hours per scout run. With 80 trials × 2 metrics × 2 sides per target, that's ~480 fewer individual model trains per batch.

---

## 8. Additional Observations

### 8.1 Short-Side Model Collapse
Across both HS09 and HS10, the optimizer frequently **collapses short-side trades to 0**. Post-optimization trade breakdowns show patterns like "Pre: 228 short → Post: 0 short" repeatedly. This suggests:
- The short-side models may not have enough independent signal to survive threshold tightening
- AP (Average Precision) metric short models outperform LL (Logloss) short models as the counter-party in ensembles
- Consider whether your short-model training pipeline needs structural changes vs just more data

### 8.2 Holdout as the Real Signal at Canary Tier
With only 20 Optuna trials, optimized PnL is weakly overfit. **Holdout PnL direction** (positive vs negative) is the most reliable signal. Ensembles with positive holdout across multiple walk-forward windows are the strongest candidates for scout promotion.

### 8.3 The "3x1 24H" Backbone Effect (HS10)
In HS10, the `3x1 24H` target dominates as the long-side model. It appears in **every single top-8 ensemble** across WFC_1 and WFD. This is a double-edged sword:
- ✅ Clearly the strongest single model
- ⚠️ Creates concentration risk — if this one model has a structural flaw, all ensembles fail together
- Consider diversifying the long-side backbone in production by also promoting `4x1 24H` and `4x1 12H` as long models
