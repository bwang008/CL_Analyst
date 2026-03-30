---
description: How to automate the Cartesian sweep of hourly model ensembles
---

# Sweep Ensembles Workflow

This workflow executes an automated Cartesian Cross-Backtest across all available Long and Short model permutations to identify the most resilient ensemble pair, specifically targeting historical drawdowns and late-cycle performance.

## Prerequisites
You must formulate a base `.json` config to define the threshold, TP/SL modifiers, and fractional exits that the swept models will inherit. Ensure `agent/backtest_engine.py` is fully functional.

## Step 1: Run the Ensemble Sweeper
Execute the `agent/sweep_ensembles.py` script. The script automatically reads predefined model buckets and iterates over $N \times M$ combinations.

```bash
// turbo
python agent/sweep_ensembles.py --base-config configs/strategies/hourly_ensemble_002.json --data data/cl-1h_bk_HourSet_02.parquet --output-md reports/ensemble_sweep_results.md
```

## Step 2: Review Results
Evaluate `reports/ensemble_sweep_results.md` to identify the most performant combinations balancing high Net PnL with low Max Drawdown and optimal 2025/2026 performance logic. 

## Step 3: Deploy Superior Pairing
Once approved, branch out a new configuration increment (e.g. `hourly_ensemble_004.json`) adopting the successful model pair.
