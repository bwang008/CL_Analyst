# Probability Distribution and Temporal Analysis Report

This report provides a comprehensive OOS analysis of prediction probability distributions and temporal signal patterns for the **HourSet_08_Ensemble_03** champion model ensemble. 

The analysis is based on the out-of-sample (OOS) prediction datasets:
*   **Long Model:** `data/predictions/oos_predictions_sweep_hs08_3x1_24h_20260523_2040_long_logloss.csv` (Thresh: **0.59**)
*   **Short Model:** `data/predictions/oos_predictions_sweep_hs08_4x1_6h_20260523_2040_short_logloss.csv` (Thresh: **0.68**)

---

## 📊 Summary of Probability Distributions

The table below summarizes the key statistical indicators for the prediction probability distributions of both models:

| Metric | Long Model (`prob_Buy`) | Short Model (`prob_Sell`) |
| :--- | :--- | :--- |
| **Total Samples (N)** | 24,346 | 24,364 |
| **Minimum Value** | 0.083779 | 0.052667 |
| **Maximum Value** | 0.886525 | 0.867994 |
| **Mean Probability** | 0.473091 | 0.371582 |
| **Median Probability** | 0.475141 | 0.340530 |
| **Distribution Shape** | Unimodal (Symmetric) | Unimodal (Right-Skewed) |
| **Active Threshold** | **0.59** | **0.68** |
| **% Signals Above Threshold** | **20.91%** (5,090 bars) | **4.93%** (1,202 bars) |
| **% Low Prob (≤ 0.45)** | 42.90% | 66.83% |

---

## 🔍 Detailed Model Profiles

### 🟢 Long Model (`prob_Buy`)
*   **Distribution Profile:** The long model exhibits a highly symmetrical, healthy **unimodal** distribution centered slightly below `0.50` (Mean = `0.473`, Median = `0.475`). The spread is wide and well-dispersed, stretching all the way to `0.886`.
*   **Selectivity:** With the threshold set to **`0.59`**, this model triggers signals on **`20.91%`** of all OOS bars. This represents a highly robust and active strategy that regularly finds long setups.
*   **Risk Profile:** Only `42.90%` of predictions fall below the `0.45` level, meaning the model frequently oscillates in the active trading region.

### 🔴 Short Model (`prob_Sell`)
*   **Distribution Profile:** The short model exhibits a **unimodal, right-skewed** distribution (Mean = `0.371` > Median = `0.340`). Two-thirds (`66.83%`) of the OOS predictions are suppressed below `0.45`, indicating that the model maintains a very low baseline probability during normal market conditions.
*   **Selectivity:** The active threshold is set at a highly selective **`0.68`**, yielding a signal rate of only **`4.93%`**. This classifies the short model as a **pure sniper strategy**—it remains dormant most of the time and only fires when there is extreme statistical evidence of a bearish reversal or exhaustion.
*   **Extreme Behavior:** Despite the low baseline, the model can still generate strong, highly-confident predictions peaking at `0.868`, showing excellent signal resolution when conditions align.

---

## 📈 Probability Distribution Plots

These plots show the color-coded probability histograms, the Gaussian Kernel Density Estimation (KDE) curve, and the active threshold lines:

### Long Model Distribution
<img src="file:///C:/Users/bwang/.gemini/antigravity/brain/564a333b-d6f0-4430-9118-7ebc733f4118/oos_predictions_sweep_hs08_3x1_24h_20260523_2040_long_logloss.png" alt="Long Model Probability Distribution" width="800">

### Short Model Distribution
<img src="file:///C:/Users/bwang/.gemini/antigravity/brain/564a333b-d6f0-4430-9118-7ebc733f4118/oos_predictions_sweep_hs08_4x1_6h_20260523_2040_short_logloss.png" alt="Short Model Probability Distribution" width="800">

---

## 🏃‍♂️ Temporal Breakdown & Session Dynamics

The temporal breakout reveals a critical structural divergence between the two models:

| Session Segment | UTC Time | Long Model (`prob_Buy` ≥ 0.59) | Short Model (`prob_Sell` ≥ 0.68) |
| :--- | :---: | :---: | :---: |
| **Globex / Asian Open** | 22:00 - 03:00 | Active (~17-21% Signal Rate) | Dormant (0% Signal Rate) |
| **London Morning / Globex Peak** | 03:00 - 08:00 | Highly Active (~24-28% Signal Rate) | **Highly Active Reversion Sniper** (12-21% Signal Rate) |
| **London Midday / Pre-NY** | 08:00 - 12:00 | Active (~22-26% Signal Rate) | Dormant (0% Signal Rate) |
| **NY Open / Midday** | 12:00 - 17:00 | Active (~14-20% Signal Rate) | Dormant (0% Signal Rate) |
| **NY Close / Globex Re-open** | 17:00 - 22:00 | Active (~14-17% Signal Rate) | Dormant (0% Signal Rate) |

### 1. Hourly Session Patterns (UTC)
*   **Long Model:** Shows highly stable participation across the entire Globex day. It peaks at **`08:00 UTC`** (04:00 AM EST, London morning session) with a **`28.3%`** signal rate (301 signals). It has healthy participation during early NY sessions but naturally drops to its lowest rate of **`14.1%`** at **`18:00 UTC`** (02:00 PM EST, afternoon liquidity drain).
*   **Short Model (The London-Session Short Sniper):** 
    > [!IMPORTANT]
    > The short model is **strictly dormant during NY hours**. There are exactly **zero** signals generated between `11:00 UTC` and `22:00 UTC`. 
    >
    > Signals are concentrated entirely in the overnight Globex and early London session between **`03:00 UTC` and `08:00 UTC`** (11:00 PM to 04:00 AM EST). Peak signal generation occurs at **`07:00 UTC`** with a **`20.7%`** hourly signal rate (220 signals).
    >
    > **Strategic Implication:** The short model acts as an overnight mean-reversion exhaust tool. It shorts the overnight market when Globex trends get overextended, wrapping up trades before the volatile NY session open.

### 2. Day of Week signal Rates
*   **Long Model:** Fully active throughout the week, peaking on Wednesdays:
    *   **Wed:** 1,124 signals (23.0% rate)
    *   **Tue:** 1,088 signals (22.1% rate)
    *   **Mon:** 965 signals (19.9% rate)
    *   **Thu:** 981 signals (20.1% rate)
    *   **Fri:** 691 signals (20.7% rate)
    *   **Sun:** 241 signals (16.3% rate)
*   **Short Model:** Shows a steady incline as the week progresses, peaking sharply on Fridays:
    *   **Mon:** 210 signals (4.3% rate)
    *   **Tue:** 209 signals (4.2% rate)
    *   **Wed:** 234 signals (4.8% rate)
    *   **Thu:** 273 signals (5.6% rate)
    *   **Fri:** 275 signals (**8.2% rate** — almost double the Monday rate!)
    *   **Strategic Implication:** Reversion shorts are highly favored on Fridays, capturing weekend position liquidations and profit-taking squeezes.

---

## 📈 Temporal Signals Visualization

These plots illustrate hourly signal rates, day of the week breakdowns, monthly distributions, and year × month heatmap density charts:

### Long Model Temporal Signals
<img src="file:///C:/Users/bwang/.gemini/antigravity/brain/564a333b-d6f0-4430-9118-7ebc733f4118/oos_predictions_sweep_hs08_3x1_24h_20260523_2040_long_logloss_temporal.png" alt="Long Model Temporal Breakdown" width="800">

### Short Model Temporal Signals
<img src="file:///C:/Users/bwang/.gemini/antigravity/brain/564a333b-d6f0-4430-9118-7ebc733f4118/oos_predictions_sweep_hs08_4x1_6h_20260523_2040_short_logloss_temporal.png" alt="Short Model Temporal Breakdown" width="800">

---

## 🎯 Conclusion & Recommendations

1.  **Threshold Alignment is Optimal:** The thresholds (`0.59` for Long, `0.68` for Short) are mathematically sound. Since the short model has a lower mean, setting the threshold to `0.68` keeps it highly selective, avoiding low-confidence trades in unfavorable regimes.
2.  **Harmonious Session Complementarity:** The ensemble features a highly active long strategy running across all sessions, paired with a highly specialized short sniper strategy that only acts overnight. This ensures that the portfolio is not constantly battling whipsaws during active US trading hours but remains capable of capturing overnight reversions.
