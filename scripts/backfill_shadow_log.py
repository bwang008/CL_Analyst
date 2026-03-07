"""
Backfill Shadow Log — Generate shadow log from existing live market_bars.

Reads the market_bars table from live_telemetry.db, augments with
features from cl_continuous_master.parquet for warmup, runs AlphaFactory
and model inference, and saves a shadow-log-format Parquet for the
parity validator.

Usage:
    python scripts/backfill_shadow_log.py
    python scripts/backfill_shadow_log.py --db-path data/live_telemetry.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.features.alpha_factory import AlphaFactory
from src.LGBMLearner import LGBMLearner

_ALPHA_WINDOWS = [864, 2016, 4032, 10080]
_WARMUP_ROWS = _ALPHA_WINDOWS[-1] + 500


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-x))


def find_default_model_dir() -> Path | None:
    from src.data_paths import get_models_root
    registry = get_models_root() / "registry"
    if not registry.exists():
        return None
    for d in sorted(registry.iterdir()):
        if (d / "final_model.pkl").exists():
            return d
    return None


def backfill(db_path: str, model_dir_str: str | None, output_path: str) -> None:
    """Pull live OHLCV, prepend warmup data, compute features, and run inference."""

    # 1. Load live bars from SQLite
    db_file = Path(db_path)
    if not db_file.exists():
        print(f"ERROR: Database not found: {db_file}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_file))
    live_df = pd.read_sql_query(
        "SELECT timestamp, open, high, low, close, volume "
        "FROM market_bars ORDER BY timestamp ASC",
        conn,
    )
    conn.close()

    if live_df.empty:
        print("ERROR: market_bars table is empty.")
        sys.exit(1)

    print(f"Live bars loaded: {len(live_df)} rows")
    print(f"  Range: {live_df['timestamp'].iloc[0]}  ->  {live_df['timestamp'].iloc[-1]}")

    # Normalize column names to match pipeline expectations
    live_df.columns = ["DateTime", "Open", "High", "Low", "Close", "Volume"]
    live_df["DateTime"] = pd.to_datetime(live_df["DateTime"])

    # 2. Load warmup data from cl_continuous_master.parquet
    from src.data_paths import get_data_path as _gdp
    warmup_path = _gdp("processed/cl_continuous_master.parquet")
    if not warmup_path.exists():
        print(f"ERROR: Warmup data not found: {warmup_path}")
        sys.exit(1)

    print(f"Loading warmup data from {warmup_path.name}...")
    warmup_df = pd.read_parquet(str(warmup_path))
    warmup_df["DateTime"] = pd.to_datetime(warmup_df["DateTime"])

    # Take the last WARMUP_ROWS from historical data that are BEFORE our live data starts
    live_start = live_df["DateTime"].iloc[0]
    hist_before_live = warmup_df[warmup_df["DateTime"] < live_start]
    if len(hist_before_live) < _WARMUP_ROWS:
        print(f"WARNING: Only {len(hist_before_live)} historical rows before live start.")
        warmup_slice = hist_before_live
    else:
        warmup_slice = hist_before_live.iloc[-_WARMUP_ROWS:]

    print(f"  Warmup rows: {len(warmup_slice)} (before {live_start})")

    # 3. Concatenate warmup + live
    combined = pd.concat([warmup_slice, live_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["DateTime"], keep="last")
    combined = combined.sort_values("DateTime").reset_index(drop=True)
    combined = combined.set_index(pd.DatetimeIndex(combined["DateTime"]))
    combined.index.name = "DateTime"

    print(f"  Combined: {len(combined)} rows ({len(warmup_slice)} warmup + {len(live_df)} live)")

    # 4. Load model
    if model_dir_str:
        model_dir = Path(model_dir_str)
    else:
        model_dir = find_default_model_dir()
    if model_dir is None or not (model_dir / "final_model.pkl").exists():
        print(f"ERROR: Model not found: {model_dir}")
        sys.exit(1)

    learner = LGBMLearner.__new__(LGBMLearner)
    learner.load(str(model_dir / "final_model.pkl"))
    feature_names = learner.feature_names
    print(f"Model: {model_dir.name} ({len(feature_names)} features)")

    # 5. Build features
    print("Building features via AlphaFactory...")
    minutes = combined.index.hour * 60 + combined.index.minute
    combined["Time_Sin"] = np.sin(2 * np.pi * minutes / 1440)
    combined["Time_Cos"] = np.cos(2 * np.pi * minutes / 1440)

    combined = AlphaFactory(combined).add_all_features(
        windows=_ALPHA_WINDOWS,
        include_momentum=True,
        include_macro=True,
    )

    if "ATR_14" not in combined.columns:
        import pandas_ta as ta  # noqa: F401
        combined["ATR_14"] = combined.ta.atr(length=14)

    combined["Volume_Log"] = np.log1p(combined["Volume"])

    combined.replace([np.inf, -np.inf], np.nan, inplace=True)
    combined.ffill(inplace=True)
    combined.bfill(inplace=True)
    combined.fillna(0, inplace=True)

    # 6. Slice to just the live rows
    live_timestamps = set(live_df["DateTime"].values)
    mask = combined.index.isin(live_timestamps)
    live_features = combined[mask]
    print(f"Live rows with features: {len(live_features)}")

    # Fill missing model features
    missing = set(feature_names) - set(live_features.columns)
    if missing:
        print(f"WARNING: {len(missing)} model features missing, filling with 0")
        for m in missing:
            live_features[m] = 0.0

    # 7. Run inference
    print(f"Running inference on {len(live_features)} rows...")
    probabilities = []
    for i in range(len(live_features)):
        row = live_features[feature_names].iloc[[i]]
        raw_pred = learner.model.predict(row)
        raw_val = float(np.asarray(raw_pred).ravel()[0])
        if raw_val < 0 or raw_val > 1:
            prob = _sigmoid(raw_val)
        else:
            prob = raw_val
        probabilities.append(prob)

    # 8. Build output
    out_data = {
        "timestamp": live_features.index.strftime("%Y-%m-%dT%H:%M:%S"),
        "Open": live_features["Open"].values,
        "High": live_features["High"].values,
        "Low": live_features["Low"].values,
        "Close": live_features["Close"].values,
        "Volume": live_features["Volume"].values,
        "prob_buy": probabilities,
        "prob_sell": [None] * len(probabilities),
        "strategy_name": ["LiveBackfill"] * len(probabilities),
    }

    # Add feature columns
    for feat in feature_names:
        if feat in live_features.columns:
            out_data[feat] = live_features[feat].values

    out_df = pd.DataFrame(out_data)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(str(out), index=False)

    print(f"\nLive shadow log saved: {out}")
    print(f"  Rows: {len(out_df)}")
    print(f"  Date range: {out_df['timestamp'].iloc[0]} -> {out_df['timestamp'].iloc[-1]}")
    print(f"\nTo validate parity, run:")
    print(f"  conda run -n trader --no-capture-output python scripts/validate_parity.py --file {out}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill shadow log from existing live market_bars"
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to the telemetry SQLite database",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help="Path to model directory (auto-detected if omitted)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output Parquet file path",
    )
    args = parser.parse_args()

    # Resolve paths via CL_DATA_ROOT fallback
    from src.data_paths import get_data_path as _gdp2
    if args.db_path is None:
        args.db_path = str(_gdp2("live_telemetry.db"))
    if args.output is None:
        args.output = str(_gdp2("processed/live_shadow_log.parquet"))
    if args.model_dir:
        from src.data_paths import resolve_cli_path
        args.model_dir = resolve_cli_path(args.model_dir)

    backfill(args.db_path, args.model_dir, args.output)


if __name__ == "__main__":
    main()
