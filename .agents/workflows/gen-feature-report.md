# Generate Feature Report Workflow

This workflow generates a Markdown report detailing the feature importance of a strategy ensemble or a single trained model. It breaks down the features sorted by best to worst based on their gain/importance to the model.

## Command

To generate the feature report, run the following command. The output will be saved to the `reports/` directory automatically.

```powershell
# For a strategy configuration JSON:
python scripts/gen_feature_report.py "configs\strategies\YOUR_CONFIG.json"

# For a single model registry ID:
python scripts/gen_feature_report.py "E2E_HourSet_09_long_logloss"
```

## Description
- **Input:** Takes either a valid JSON config (like those used for ensembles or batch orchestrators) or a single registry experiment ID.
- **Output:** A formatted `.md` file inside the `reports/` folder. The file will be named `<config_or_model_name>_feature_importance_report.md`.
- **Contents:** The report lists the Top 50 most important features and the Bottom 10 least important features for both the Long and Short models. If the model has fewer features, it prints all of them.

## Best Practices
- When analyzing a strategy config that performed well, you can run this workflow to understand which indicators or features are driving the alpha.
- You can cross-reference the bottom features and consider dropping them in future training batches to reduce noise.
