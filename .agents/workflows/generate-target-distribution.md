# Workflow: Generate Target Distribution Report

This workflow generates a Markdown report detailing the distribution of binary and multi-class targets within a given parquet dataset. It calculates the True/False imbalance ratios and evaluates Long/Short target overlaps, formatting the results into a readable report saved to the `reports/` directory.

## Step 1: Run the Target Distribution Generator
// turbo
Execute the `agent/target_distribution.py` script and pass the path to the dataset you want to analyze using the `--data` argument.

```powershell
python agent/target_distribution.py --data "data/processed/<dataset_name>.parquet"
```

*Example:*
```powershell
python agent/target_distribution.py --data "data/processed/CL_HourSet_07.parquet"
```

## Step 2: Review the Generated Report
The script will output a success message indicating where the report was saved, typically:
`reports/<dataset_name>_target_distribution_report.md`

You can then view the report to analyze:
1. Severe class imbalances (marked with 🔴 or ⚠️).
2. The Long/Short overlap percentage for each barrier configuration (overlap > 10% indicates contradictory labels).
