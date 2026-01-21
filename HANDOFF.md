# Handoff Summary

## What we just finished building
- Walk-forward training pipeline in `main.py` (`train_and_evaluate`) that:
  - Loads processed data
  - Splits into gym/vault
  - Runs walk-forward validation
  - Evaluates folds and holdout
  - Saves metrics, predictions, plots, and final model
- Evaluation layer in `src/evaluator.py` that computes classification metrics and actual move magnitude analysis using `RAW_` columns.
- Visualization utilities in `src/visualizer.py` (signals, fold summary, actual move distribution, and confusion matrix).
- Confusion matrix plot output added to the report outputs.
- Wall-clock timing output added to the train command.
- Class-imbalance mitigation added via `class_weight="balanced"` in `src/LGBMLearner.py`.
- Tests added for `walk_forward`, `evaluator`, and `visualizer` modules under `tests/`.
- README updated to reflect new process/train commands and outputs.

## Current state of the code
### What works
- `python main.py process` generates processed datasets with `RAW_` and `TARGET_` columns.
- `python main.py train`:
  - Trains LightGBM models via walk-forward validation.
  - Evaluates each fold and a final holdout (vault).
  - Writes outputs to `reports/` and `models/`.
  - Saves `vault_confusion_matrix.png`.
  - Prints wall-clock runtime.
- `src/LGBMLearner.py` now applies `class_weight="balanced"` by default to combat class imbalance.
- Tests exist for the new modules and should run under pytest.

### What doesn’t work / gaps
- Model performance remains poor on Buy/Sell (likely still imbalanced/noisy); class weighting is applied but no follow-up metrics have been captured yet.
- `reports/` and `models/` are only created when running training; they will not exist until you run `python main.py train`.
- Visualizer tests assume a headless matplotlib backend; if your environment lacks it, plot tests may fail.

## Prioritized next steps
1. **Re-run training and review metrics**
   - Validate whether `class_weight="balanced"` materially improves Buy/Sell recall.
   - Compare `reports/metrics.json` vs `reports/vault_metrics.json`.
2. **Add fold-level confusion matrices (optional but useful)**
   - Save per-fold confusion matrices to `reports/fold_{n}_confusion_matrix.png`.
3. **Tune LightGBM hyperparameters for imbalance**
   - Consider `scale_pos_weight` or explicit `class_weight` dicts.
   - Increase `num_leaves`, adjust `min_child_samples`, and consider `max_depth`.
4. **Add sampling strategy**
   - Explore undersampling Hold or oversampling Buy/Sell.
   - Evaluate with the same walk-forward structure.
5. **Revisit feature set / target density**
   - Check label distribution and consider alternative thresholds/horizons.

## Known bugs and pending decisions
- **Class imbalance remains the core issue.** Class weighting is applied but there’s no confirmation yet that it improves Buy/Sell recall meaningfully.
- **Reports/models directories** are created at runtime only; if users expect them pre-existing, add a setup step or CLI command.
- **Pending: fold-level confusion matrix outputs**. Only the vault confusion matrix is currently saved.
- **Pending: documentation of label mapping**. The label mapping is: `0 = Hold`, `1 = Buy`, `2 = Sell`.
