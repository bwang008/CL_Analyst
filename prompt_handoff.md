# Agent Handoff Prompt

**Context:**
We are working on the "Modern Alpha Pipeline" for a Crude Oil (CL) ML trading system. The system uses LightGBM models trained on hourly data (`HourSet_06`) with stationary features (Ichimoku, normalized DMA, PPO) to predict 72H Triple Barrier targets. 

**Current State & Recent Discoveries:**
1. **Optuna Constriction:** We discovered that unconstrained Optuna searches were memorizing noise (overfitting) and failing out-of-sample. We tightened the bounds (`min_child_samples` 150-400, `reg_alpha` <= 1.0, `learning_rate` <= 0.02) which fixed the overfitting.
2. **Horizon Mismatch:** We tried training a 12H horizon model to match the stationary features, but the execution strategy (`hourly_ensemble_005.json`) requires a 2.0x ATR Take Profit, which is mathematically disproportionate to a 12-hour timeframe (too short, chopped out by noise).
3. **Data Starvation (The 3-Way Split Issue):** We previously attempted to create a 3-way split (Train / Validation for Thresholds / OOS Holdout) by pushing the Train Cutoff date back to `2019-01-01` (to leave 2019-2022 for validation). This starved the tree-builder of COVID-era volatility data, causing severe performance degradation.
4. **Current Execution:** We have shifted the entire 3-way split forward to fix the data starvation. The current pipeline trains on data up to `2023-01-01`, uses `2023` to `2025` for execution threshold validation, and reserves `2025-Present` for true OOS Holdout. 

**Your Immediate Task:**
A 48-core GCP Canary VM (`optuna-runner-72h-mod`) is currently running the E2E pipeline for this new architecture.
- **Strategy Config:** `hourly_ensemble_006.json`
- **Dataset:** `gs://cltrainer-optuna-results/data/cl-1h_bk_HourSet_06.parquet`
- **GCS Prefix:** `scout_hourset06_72h_modern`
- **Holdout:** 2025-01-01 to Present.

**How to Monitor:**
You can poll the GCS status file to check the progress of the Optuna sweep.
```powershell
gsutil cat gs://cltrainer-optuna-results/scout_hourset06_72h_modern/STATUS.json
```
Wait for `completed` to equal `total` (2). After that, the E2E packaging runs for ~5 minutes and produces `pipeline_summary.json` in the GCS bucket. Download it to `./reports/scout_hourset06_72h_modern_summary.json` to analyze the final Profit Factor.

**Goal:**
Compare the final OOS Holdout Profit Factor of this run against our historical frozen baseline (PF 1.196 / +$8K). If it is profitable, the Modern Alpha Pipeline is validated and ready for production deployment. If not, analyze the `pipeline_summary.json` to determine the point of failure.
