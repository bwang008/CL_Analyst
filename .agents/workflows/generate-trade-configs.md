# Generate Trade Configs Workflow

This workflow guides an agent through the process of generating production strategy configuration files from optimized batch results, generating the necessary out-of-sample predictions, and verifying the configuration via backtesting.

## Prerequisites
- A completed batch optimization run containing a baseline `ensemble_config_<metric>.json` and an optimization summary (e.g., `batch_summary_optimized_sharpe.md`).
- Target dataset for the backtest (e.g., `CL_HourSet_11.parquet`).

## Step 1: Identify Models and Parameters
1. Review the batch summary to identify the best performing Bidirectional Pairs (Long + Short).
2. Locate the corresponding `.pkl` models inside the `registry/canary_output/` folders of their respective experiment directories.
3. Extract the optimized parameters (Thresholds, TP/SL Multipliers, Trailing ATR, Max Hold Bars, Cooldowns, ATR Period) for both the Long and Short sides from the batch summary.

## Step 2: Generate Extended Predictions
To run a backtest on an extended dataset, you must generate new prediction CSVs because the original `oos_predictions.csv` files only span up to the canary training cutoff date.

> [!WARNING]
> By default, the prediction script assumes it is predicting `prob_Buy`. For short models, you **MUST** pass `--prob-col prob_Sell`.

Run the prediction generator for each model:

```powershell
# For Long Models
python agent/generate_model_predictions.py `
  --model-path "reports/sweep_.../registry/canary_output/final_long_model.pkl" `
  --data-path "C:\CL_Analyst_Data\data\processed\CL_HourSet_11.parquet" `
  --oos-start-date "2022-01-01" `
  --output "reports/sweep_.../registry/canary_output/extended_predictions_long.csv"

# For Short Models (Notice the --prob-col argument)
python agent/generate_model_predictions.py `
  --model-path "reports/sweep_.../registry/canary_output/final_short_model.pkl" `
  --data-path "C:\CL_Analyst_Data\data\processed\CL_HourSet_11.parquet" `
  --prob-col prob_Sell `
  --oos-start-date "2022-01-01" `
  --output "reports/sweep_.../registry/canary_output/extended_predictions_short.csv"
```

## Step 3: Construct Strategy Configurations
Create the new `.json` execution config in `configs/strategies/`.

> [!WARNING]
> **Duplicating a donor config inherits its symbol — this is the ES01B defect verbatim.** The ES
> standup shipped a config with `execution_symbol: "CL"` inherited from the CL donor. Symbol
> fields must be stamped explicitly (sub-steps 2-3) and the config must pass the validation
> checks (sub-step 6) before any backtest or live use.

1. Duplicate the baseline canary config.
2. Set `execution_symbol` to the target symbol (never leave the donor's value).
3. Set `models.long.symbol` and `models.short.symbol` to the target symbol.
4. Update the `models.long.predictions_path` and `models.short.predictions_path` to point to the `extended_predictions.csv` files you just generated.
5. Apply the optimized parameters exactly to three places in the config:
   - The top-level settings (`tp_atr_mult`, `sl_atr_mult`, `trailing_atr_mult`, `max_hold_bars`, etc.)
   - The `long` and `short` object dictionaries.
   - The `models.long.threshold` and `models.short.threshold` fields.
6. **Validate the config (blocking, BEFORE Step 4's backtest)** — run the CONFIG VALIDATION GATE
   checks from [build-symbol-pipeline](build-symbol-pipeline.md) Phase 6 on the single config
   (single-config variant: stage a copy as `<tmpdir>\configs\<name>.json` next to a minimal
   `<tmpdir>\manifest.json` stub `{"baseline": {"symbol": "<SYM>"}}`, then run the script on
   `<tmpdir>` from the repo root). It asserts: `resolve_instrument_context` succeeds,
   `execution_symbol` matches the target symbol, `models.*.symbol` present, and every
   `model_path`/`predictions_path` exists on disk.

*Tip: Using a Python script to programmatically load the base JSON, inject the new parameters, and dump the JSON is significantly less error-prone than manual text replacements.*

## Step 4: Verify via Backtest
Run the backtest engine to ensure the config is syntactically valid and the predictions map correctly.

```powershell
python agent/backtest_engine.py `
  --config "configs/strategies/HS11_Prod_Ensemble_E01_06162026.json" `
  --data "C:\CL_Analyst_Data\data\processed\CL_HourSet_11.parquet" `
  --exec-data "C:\CL_Analyst_Data\data\raw\DataBentoSample\CL_raw.csv" `
  --slippage-per-side 0.01
```

If it successfully completes and generates the backtest report, the config is fully validated for production use.
