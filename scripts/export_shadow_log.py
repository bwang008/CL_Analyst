"""
Export Shadow Log — Dump SQLite shadow_log table to Parquet.

Reads the shadow_log table from the live telemetry database, expands the
features_json column into individual feature columns, and exports the
result to a Parquet file for offline parity validation.

Usage:
    python scripts/export_shadow_log.py
    python scripts/export_shadow_log.py --db-path data/live_telemetry.db --output data/processed/live_shadow_log.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def export_shadow_log(db_path: str, output_path: str) -> None:
    """Read shadow_log from SQLite and export to Parquet."""
    db_file = Path(db_path)
    if not db_file.exists():
        print(f"ERROR: Database not found: {db_file}")
        sys.exit(1)

    import sqlite3

    conn = sqlite3.connect(str(db_file))

    # Check table exists
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_log'"
    ).fetchall()
    if not tables:
        print("ERROR: shadow_log table does not exist in the database.")
        conn.close()
        sys.exit(1)

    df = pd.read_sql_query(
        "SELECT * FROM shadow_log ORDER BY timestamp ASC", conn
    )
    conn.close()

    if df.empty:
        print("WARNING: shadow_log table is empty — nothing to export.")
        sys.exit(0)

    print(f"Loaded {len(df)} rows from shadow_log")

    # Expand features_json → individual columns
    feature_rows = []
    for _, row in df.iterrows():
        fj = row.get("features_json")
        if fj and isinstance(fj, str):
            try:
                feature_rows.append(json.loads(fj))
            except json.JSONDecodeError:
                feature_rows.append({})
        else:
            feature_rows.append({})

    features_df = pd.DataFrame(feature_rows)
    if not features_df.empty:
        # Drop the JSON column and merge expanded features
        df = df.drop(columns=["features_json"])
        df = pd.concat([df.reset_index(drop=True), features_df], axis=1)

    # Convert timestamp to datetime
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Drop auto-generated SQLite columns
    for col in ("id", "created_at"):
        if col in df.columns:
            df = df.drop(columns=[col])

    # Export
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(str(out), index=False)
    print(f"Exported {len(df)} rows → {out}")
    print(f"Columns: {list(df.columns)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export shadow_log from SQLite to Parquet"
    )
    parser.add_argument(
        "--db-path",
        default=str(PROJECT_ROOT / "data" / "live_telemetry.db"),
        help="Path to the telemetry SQLite database",
    )
    parser.add_argument(
        "--output",
        default=str(
            PROJECT_ROOT / "data" / "processed" / "live_shadow_log.parquet"
        ),
        help="Output Parquet file path",
    )
    args = parser.parse_args()
    export_shadow_log(args.db_path, args.output)


if __name__ == "__main__":
    main()
