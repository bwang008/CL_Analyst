# /generate-data — Feature Generation Pipeline

Generate a rich feature dataset (Parquet) from raw exchange data by processing it through the AlphaFactory, MacroFeatureEngine, and Target Generators using a Pydantic `DataMap` JSON config.

## Prerequisites

- Raw execution data must exist (usually generated via `/grab-data`).
- `CL_DATA_ROOT` environment variable should point to your data directory (or use explicit absolute paths).

## The DataMap Config

This workflow relies on a `DataMap` JSON file located in `configs/master/` (e.g., `configs/master/DataMap_CL_HourSet_14B.json`).
This config specifies:
- `dataset_version`: The named version of the dataset (e.g., `HourSet_14B`).
- `output_dir`: The directory to write the final parquet file to.
- `output_filename`: The specific filename (e.g., `CL_HourSet_14B.parquet`).
- `features`: The exact features (windows, macro data, term structure) to generate.
- `targets`: The triple-barrier targets and horizons to compute.

## Step 1: Create or Verify the DataMap

If the user requests a new dataset (e.g., for a new symbol or a new feature combination), the agent must first create the `DataMap` JSON file if it does not already exist.

**Example Agent Action:**
If the user asks to generate `HourSet_15` for symbol `ES`, the agent will create `configs/master/DataMap_ES_HourSet_15.json` detailing the desired features and targets.

## Step 2: Execute the Pipeline

Once the `DataMap` exists, the agent generates the dataset by running the `regenerate_features.py` script.

```powershell
python scripts/regenerate_features.py --config configs/master/DataMap_CL_HourSet_14B.json --exec-data C:\CL_Analyst_Data\data\raw\DataBentoSample\CL_raw.csv
```

### What this does:
1. **Validates** the JSON against the `DataWorkflowConfig` Pydantic schema (`src/config/schemas.py`).
2. **Loads** the raw data (e.g. `CL.csv` and `CL_raw.csv`).
3. **Generates** all features defined in the `features` block via `AlphaFactory` and `MacroFeatureEngine`.
4. **Computes** all targets defined in the `targets` block.
5. **Saves** the final `.parquet` file to the exact path specified by `output_dir` and `output_filename` in the JSON.
6. **Saves** a lineage artifact (a copy of the config used) to the `output_dir` alongside the dataset.

## Output

The result is a fully processed, ML-ready Parquet dataset (e.g., `C:/CL_Analyst_Data/data/processed/CL_HourSet_14B.parquet`) ready to be referenced in a `BatchSweepConfig` manifest for the `/run-cloud-batch` workflow.
