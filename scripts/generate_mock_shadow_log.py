"""
Generate Mock Shadow Log — Create test data for the parity validator.

Loads an existing historical features parquet, slices the last N rows,
runs model inference to create predictions, and saves as a shadow-log-
format Parquet file that can be fed into validate_parity.py.

This lets you test the parity pipeline immediately without waiting for
live market data to accumulate.

Usage:
    python scripts/generate_mock_shadow_log.py
    python scripts/generate_mock_shadow_log.py --rows 500
    python scripts/generate_mock_shadow_log.py --source data/processed/CL_set_06.parquet --rows 1000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.features.alpha_factory import AlphaFactory
from src.LGBMLearner import LGBMLearner


# AlphaFactory windows (must match training & live pipeline)
_ALPHA_WINDOWS = [864, 2016, 4032, 10080]


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-x))


def find_default_model_dir() -> Path | None:
    """Auto-detect the first model in the registry."""
    registry = PROJECT_ROOT / "models" / "registry"
    if not registry.exists():
        return None
    for d in sorted(registry.iterdir()):
        if (d / "final_model.pkl").exists():
            return d
    return None


def find_source_parquet() -> Path | None:
    """Auto-detect a source parquet with OHLCV columns in data/processed/.

    Prefers cl_continuous_master.parquet (raw OHLCV) over CL_set_*.parquet
    (which are feature-only processed files without OHLCV).
    """
    processed = PROJECT_ROOT / "data" / "processed"
    if not processed.exists():
        return None
    # Prefer continuous master (has raw OHLCV columns)
    master = processed / "cl_continuous_master.parquet"
    if master.exists():
        return master
    # Fallback: warm_start_cache also has OHLCV
    cache = processed / "warm_start_cache.parquet"
    if cache.exists():
        return cache
    # Last resort: any parquet
    for p in sorted(processed.glob("*.parquet")):
        return p
    return None


def generate_mock(
    source_path: str | None,
    model_dir_str: str | None,
    output_path: str,
    n_rows: int,
) -> None:
    """Generate a mock shadow log from historical data."""
    # Find source
    if source_path:
        src = Path(source_path)
    else:
        src = find_source_parquet()
    if src is None or not src.exists():
        print(f"ERROR: Source parquet not found: {src}")
        print("Use --source to specify the path to a features parquet.")
        sys.exit(1)

    print(f"Loading source: {src.name} ...", end=" ", flush=True)
    full_df = pd.read_parquet(str(src))
    print(f"{len(full_df)} rows")

    # Find model
    if model_dir_str:
        model_dir = Path(model_dir_str)
    else:
        model_dir = find_default_model_dir()
    if model_dir is None or not (model_dir / "final_model.pkl").exists():
        print(f"ERROR: Model not found: {model_dir}")
        print("Use --model-dir to specify the model directory.")
        sys.exit(1)

    learner = LGBMLearner.__new__(LGBMLearner)
    learner.load(str(model_dir / "final_model.pkl"))
    feature_names = learner.feature_names
    print(f"Model: {model_dir.name} ({len(feature_names)} features)")

    # Ensure we have OHLCV columns
    ohlcv_cols = ["Open", "High", "Low", "Close", "Volume"]
    for col in ohlcv_cols:
        if col not in full_df.columns:
            print(f"ERROR: Source parquet missing column: {col}")
            sys.exit(1)

    # We need enough rows for AlphaFactory warmup + the requested slice
    warmup = _ALPHA_WINDOWS[-1] + 500
    total_needed = warmup + n_rows
    if len(full_df) < total_needed:
        print(
            f"WARNING: Source has {len(full_df)} rows, need {total_needed} "
            f"(warmup={warmup} + rows={n_rows}). Using all available."
        )
        work_df = full_df.copy()
        n_rows = max(len(full_df) - warmup, 100)
    else:
        # Take the last total_needed rows
        work_df = full_df.iloc[-total_needed:].copy()

    # Ensure DateTime index
    if "DateTime" in work_df.columns and not isinstance(
        work_df.index, pd.DatetimeIndex
    ):
        work_df = work_df.set_index(
            pd.DatetimeIndex(pd.to_datetime(work_df["DateTime"]))
        )
    elif not isinstance(work_df.index, pd.DatetimeIndex):
        work_df.index = pd.to_datetime(work_df.index)

    # Build features (replicate live pipeline)
    print("Building features via AlphaFactory...")
    minutes = work_df.index.hour * 60 + work_df.index.minute
    work_df["Time_Sin"] = np.sin(2 * np.pi * minutes / 1440)
    work_df["Time_Cos"] = np.cos(2 * np.pi * minutes / 1440)

    work_df = AlphaFactory(work_df).add_all_features(
        windows=_ALPHA_WINDOWS,
        include_momentum=True,
        include_macro=True,
    )

    if "ATR_14" not in work_df.columns:
        import pandas_ta as ta  # noqa: F401
        work_df["ATR_14"] = work_df.ta.atr(length=14)

    work_df["Volume_Log"] = np.log1p(work_df["Volume"])

    # Clean NaN/inf
    work_df.replace([np.inf, -np.inf], np.nan, inplace=True)
    work_df.ffill(inplace=True)
    work_df.bfill(inplace=True)
    work_df.fillna(0, inplace=True)

    # Slice the last n_rows
    tail = work_df.iloc[-n_rows:]

    # Verify feature availability
    missing = set(feature_names) - set(tail.columns)
    if missing:
        print(f"WARNING: {len(missing)} model features missing, filling with 0")
        for m in missing:
            tail[m] = 0.0

    # Run inference
    print(f"Running inference on {len(tail)} rows...")
    probabilities = []
    for i in range(len(tail)):
        row = tail[feature_names].iloc[[i]]
        raw_pred = learner.model.predict(row)
        raw_val = float(np.asarray(raw_pred).ravel()[0])
        if raw_val < 0 or raw_val > 1:
            prob = _sigmoid(raw_val)
        else:
            prob = raw_val
        probabilities.append(prob)

    # Build output DataFrame
    out_data = {
        "timestamp": tail.index.strftime("%Y-%m-%dT%H:%M:%S"),
        "Open": tail["Open"].values,
        "High": tail["High"].values,
        "Low": tail["Low"].values,
        "Close": tail["Close"].values,
        "Volume": tail["Volume"].values,
        "prob_buy": probabilities,
        "prob_sell": [None] * len(probabilities),
        "strategy_name": ["MockGenerated"] * len(probabilities),
    }

    # Add all feature columns
    for feat in feature_names:
        if feat in tail.columns:
            out_data[feat] = tail[feat].values

    out_df = pd.DataFrame(out_data)

    # Export
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(str(out), index=False)
    print(f"\nMock shadow log saved: {out}")
    print(f"  Rows:     {len(out_df)}")
    print(f"  Columns:  {len(out_df.columns)}")
    print(f"\nTo validate, run:")
    print(f"  python scripts/validate_parity.py --file {out}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate mock shadow log data for parity testing"
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Path to source features parquet (auto-detected if omitted)",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help="Path to model directory (auto-detected if omitted)",
    )
    parser.add_argument(
        "--output",
        default=str(
            PROJECT_ROOT / "data" / "processed" / "mock_shadow_log.parquet"
        ),
        help="Output Parquet file path",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=500,
        help="Number of rows to generate (default: 500)",
    )
    args = parser.parse_args()
    generate_mock(args.source, args.model_dir, args.output, args.rows)


if __name__ == "__main__":
    main()
