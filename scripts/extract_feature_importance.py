"""Extract feature importance from a trained model in the registry.

Usage:
    python scripts/extract_feature_importance.py EXP-032_optuna_v2_set08_short_logloss
    python scripts/extract_feature_importance.py EXP-033_optuna_v2_set08_154feat_logloss --top 30
    python scripts/extract_feature_importance.py EXP-032_optuna_v2_set08_short_logloss --filter EXHAUST
    python scripts/extract_feature_importance.py --all --top 10

Handles both LGBMClassifier and raw Booster objects, and both dict-wrapped
and plain model PKL formats.  Outputs to stdout and optionally overwrites
the registry's feature_importance.csv with the full ranked list.
"""

import argparse
import os
import sys

import joblib
import pandas as pd


REGISTRY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models", "registry",
)


def _extract(model_obj, feature_names=None):
    """Return (feature_names, importances) from any supported model format."""

    # Case 1: dict wrapper  {"model": <Booster|LGBM>, "feature_names": [...]}
    if isinstance(model_obj, dict):
        inner = model_obj.get("model", model_obj)
        names = model_obj.get("feature_names", feature_names)
        return _extract(inner, names)

    # Case 2: LGBMClassifier / LGBMRegressor (sklearn API)
    if hasattr(model_obj, "feature_importances_"):
        names = (
            feature_names
            or getattr(model_obj, "feature_name_", None)
            or [f"f{i}" for i in range(len(model_obj.feature_importances_))]
        )
        return names, model_obj.feature_importances_

    # Case 3: raw LightGBM Booster
    if hasattr(model_obj, "feature_importance"):
        imp = model_obj.feature_importance(importance_type="gain")
        names = (
            feature_names
            or model_obj.feature_name()
            or [f"f{i}" for i in range(len(imp))]
        )
        return names, imp

    raise TypeError(
        f"Cannot extract feature importance from {type(model_obj)}. "
        f"Expected LGBMClassifier, LGBMRegressor, Booster, or dict wrapper."
    )


def process_model(experiment_id, top_n=20, filter_str=None, save=False):
    """Extract and display feature importance for a single experiment."""

    model_dir = os.path.join(REGISTRY_DIR, experiment_id)
    pkl_path = os.path.join(model_dir, "final_model.pkl")

    if not os.path.exists(pkl_path):
        print(f"ERROR: {pkl_path} not found", file=sys.stderr)
        return None

    model_obj = joblib.load(pkl_path)
    names, importances = _extract(model_obj)

    # Build sorted DataFrame
    df = pd.DataFrame({"feature": names, "gain": importances})
    df = df.sort_values("gain", ascending=False).reset_index(drop=True)
    df.index += 1  # 1-based rank
    df.index.name = "rank"

    n_features = len(df)
    header = f"\n{'='*70}\n{experiment_id}  ({n_features} features)\n{'='*70}"
    print(header)

    # Filter view
    if filter_str:
        filtered = df[df["feature"].str.contains(filter_str, case=False)]
        print(f"\nFiltered by '{filter_str}' ({len(filtered)} matches):")
        if filtered.empty:
            print("  (none)")
        else:
            for rank, row in filtered.iterrows():
                print(f"  #{rank:3d}  {row['feature']:45s}  gain={row['gain']:.1f}")

    # Top-N view
    print(f"\nTop {min(top_n, n_features)} features:")
    for rank, row in df.head(top_n).iterrows():
        marker = f" ** {filter_str}" if filter_str and filter_str.upper() in row["feature"] else ""
        print(f"  #{rank:2d}  {row['feature']:45s}  gain={row['gain']:.1f}{marker}")

    # Save to registry
    if save:
        csv_path = os.path.join(model_dir, "feature_importance.csv")
        save_df = df.reset_index(drop=True)[["feature", "gain"]]
        save_df.columns = ["feature", "mean_importance"]
        save_df.to_csv(csv_path, index=False)
        print(f"\nSaved: {csv_path} ({len(save_df)} features)")

    return df


def main():
    parser = argparse.ArgumentParser(
        description="Extract feature importance from registry models"
    )
    parser.add_argument(
        "experiment_id",
        nargs="?",
        help="Experiment ID (directory name under models/registry/)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Process all models in the registry",
    )
    parser.add_argument(
        "--top", type=int, default=20,
        help="Number of top features to display (default: 20)",
    )
    parser.add_argument(
        "--filter", type=str, default=None,
        help="Filter features containing this string (case-insensitive)",
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Overwrite feature_importance.csv in the registry with full ranked list",
    )
    args = parser.parse_args()

    if args.all:
        experiments = sorted([
            d for d in os.listdir(REGISTRY_DIR)
            if os.path.isfile(os.path.join(REGISTRY_DIR, d, "final_model.pkl"))
        ])
        if not experiments:
            print("No models with final_model.pkl found in registry.")
            return
        print(f"Found {len(experiments)} models in registry")
        for exp_id in experiments:
            process_model(exp_id, top_n=args.top, filter_str=args.filter, save=args.save)
    elif args.experiment_id:
        process_model(args.experiment_id, top_n=args.top, filter_str=args.filter, save=args.save)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
