"""Generate out-of-sample predictions for strategy backtest configs.

Loads trained models from the model registry and generates prediction CSVs
that are referenced by strategy config JSONs.  Predictions are saved to
both the repo-local ``data/predictions/`` directory AND mirrored to
``CL_DATA_ROOT/data/predictions/`` for disaster-recovery redundancy.
"""

import pandas as pd
import numpy as np
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.LGBMLearner import LGBMLearner
from src.data_paths import get_data_path, get_model_path, mirror_file

# ---------------------------------------------------------------------------
# Configuration — edit these when running new prediction sets
# ---------------------------------------------------------------------------

# Input data: resolved via CL_DATA_ROOT with repo-local fallback
PARQUET_RELATIVE = "processed/CL_HourSet_09.parquet"

# Models and outputs: use relative paths so this works on any machine
MODELS_TO_RUN = [
    {
        "model_relative": "registry/E2E_HourSet_09_long_logloss/final_model.pkl",
        "output_name": "oos_predictions_sweep_hs09_3x1_24h_20260602_0330_long_logloss.csv",
        "prob_col": "prob_Buy",
    },
    {
        "model_relative": "registry/E2E_HourSet_09_short_average_precision/final_model.pkl",
        "output_name": "oos_predictions_sweep_hs09_3x1_24h_20260602_0330_short_average_precision.csv",
        "prob_col": "prob_Sell",
    },
]


def main():
    # Resolve input data via CL_DATA_ROOT → repo-local fallback
    parquet_path = get_data_path(PARQUET_RELATIVE)
    print(f"Loading data from {parquet_path}")
    df = pd.read_parquet(parquet_path)

    if 'DateTime' in df.columns:
        df = df.set_index('DateTime')

    for cfg in MODELS_TO_RUN:
        # Resolve model path via CL_DATA_ROOT → repo-local fallback
        model_path = get_model_path(cfg["model_relative"])
        print(f"Loading model: {model_path}")
        learner = LGBMLearner()
        learner.load(str(model_path))

        # Generate predictions
        feature_names = learner.feature_names
        X = df[feature_names]

        print(f"Generating predictions for {len(X)} rows...")
        probs = learner.model.predict(X)

        out_df = pd.DataFrame(index=df.index)
        out_df['y_true'] = 0
        out_df[cfg['prob_col']] = probs
        out_df['prob_Hold'] = 1 - probs

        # Add OHLCV columns if available
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            if col in df.columns:
                out_df[col] = df[col]
            elif f'RAW_{col}' in df.columns:
                out_df[col] = df[f'RAW_{col}']
            else:
                out_df[col] = 0.0

        if cfg['prob_col'] == 'prob_Buy':
            out_df['predicted'] = np.where(probs > 0.53, 'Buy', 'Hold')
        else:
            out_df['predicted'] = np.where(probs > 0.53, 'Sell', 'Hold')

        # Column order matches standard format
        cols = ['y_true', cfg['prob_col'], 'prob_Hold', 'predicted',
                'Open', 'High', 'Low', 'Close', 'Volume']
        out_df = out_df[cols]

        # Save to repo-local data/predictions/
        output_dir = PROJECT_ROOT / "data" / "predictions"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / cfg["output_name"]

        # Backup existing file
        if output_path.exists():
            bak_path = output_path.with_suffix(output_path.suffix + ".bak")
            if not bak_path.exists():
                os.rename(output_path, bak_path)
            else:
                os.remove(output_path)

        out_df.to_csv(output_path)
        print(f"Saved: {output_path}")

        # Mirror to CL_DATA_ROOT/data/predictions/ for disaster recovery
        mirror_file(output_path)
        print(f"Mirrored to CL_DATA_ROOT")

    print("\nAll predictions generated and mirrored successfully.")


if __name__ == "__main__":
    main()
