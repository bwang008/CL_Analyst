"""
Databento CL Futures Data Pipeline — Multi-Adjustment Converter.

Reads the raw Databento continuous contract CSV (CL.v.0, ohlcv-1h)
and produces three adjustment variants, each usable by the existing
data_processor.py / backtest_engine.py pipeline.

Output modes:
    raw         — No price adjustment.  Prices are the literal front-month
                  contract prices at each bar.  Rollover gaps are visible.
    ratio       — Backward ratio scaling (multiplicative).  Anchors to
                  the most recent bar so live prices are unchanged, but
                  deep history is scaled by a cumulative product factor.
    panama      — Backward additive (Panama Canal) adjustment.  At each
                  rollover the gap is computed as an additive diff and
                  propagated backward.  Dollar PnL is preserved in the
                  adjusted series but absolute price levels shift.

All outputs can be emitted as:
    - Semicolon-separated CSV with no headers (DD/MM/YYYY;HH:MM;O;H;L;C;V)
      — the native format consumed by DataProcessor.load_data()
    - Standard comma-separated CSV with headers (for inspection)

Usage:
    # From Python:
    from databento_request import convert_databento_csv
    convert_databento_csv("path/to/raw.csv", "path/to/output_dir")

    # From CLI:
    python databento_request.py convert path/to/raw.csv --outdir path/to/output
    python databento_request.py convert path/to/raw.csv --mode raw --format semicolon
    python databento_request.py submit   # submit a new Databento batch job
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Core: Parse raw Databento CSV into a clean DataFrame
# ---------------------------------------------------------------------------

def _parse_databento_csv(path: str) -> pd.DataFrame:
    """Load the raw Databento CSV and convert to a standard DataFrame.

    Returns a DataFrame indexed by DateTime (UTC) with columns:
        Open, High, Low, Close, Volume, instrument_id
    Prices are converted from Databento fixed-precision integers to dollars.
    """
    df = pd.read_csv(path)

    if df.empty or "instrument_id" not in df.columns:
        raise ValueError(f"File does not look like a Databento ohlcv CSV: {path}")

    # Convert nanosecond timestamps to datetime
    if "ts_event" in df.columns:
        df["ts_event"] = pd.to_datetime(df["ts_event"], unit="ns", utc=True)

    # Convert fixed-precision integers to dollar decimals
    ohlc_cols = ["open", "high", "low", "close"]
    for col in ohlc_cols:
        if col in df.columns:
            df[col] = df[col] / 1e9

    # Detect rollover points (instrument_id changes)
    df["is_roll"] = (
        (df["instrument_id"] != df["instrument_id"].shift(1))
        & df["instrument_id"].shift(1).notna()
    )

    return df


# ---------------------------------------------------------------------------
# Adjustment Methods
# ---------------------------------------------------------------------------

def adjust_raw(df: pd.DataFrame) -> pd.DataFrame:
    """Return the raw unadjusted OHLCV — just strip metadata columns."""
    return df.copy()


def adjust_ratio(df: pd.DataFrame) -> pd.DataFrame:
    """Backward ratio-adjusted (multiplicative) continuous series.

    At each rollover the ratio factor is:
        factor = open_new_contract / close_old_contract

    The cumulative product is applied backward so the most recent data
    is untouched (factor = 1.0) and historical prices are scaled.
    """
    out = df.copy()
    ohlc = ["open", "high", "low", "close"]

    out["roll_factor"] = 1.0
    roll_mask = out["is_roll"]
    out.loc[roll_mask, "roll_factor"] = (
        out.loc[roll_mask, "open"] / out["close"].shift(1)[roll_mask]
    )
    out["roll_factor"] = out["roll_factor"].replace([np.inf, -np.inf], 1.0).fillna(1.0)

    # Cumulative product backward (anchor = newest bar = 1.0)
    out["cum_factor"] = (
        out["roll_factor"].iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
    )

    for col in ohlc:
        out[col] = out[col] * out["cum_factor"]

    out.drop(columns=["roll_factor", "cum_factor"], inplace=True)
    return out


def adjust_panama(df: pd.DataFrame) -> pd.DataFrame:
    """Backward additive (Panama Canal) continuous series.

    At each rollover the additive gap is:
        gap = open_new_contract - close_old_contract

    The cumulative sum is propagated backward so the newest data is
    untouched and historical prices are shifted by the total additive
    offset.  This preserves dollar PnL across the entire series.
    """
    out = df.copy()
    ohlc = ["open", "high", "low", "close"]

    out["roll_gap"] = 0.0
    roll_mask = out["is_roll"]
    out.loc[roll_mask, "roll_gap"] = (
        out.loc[roll_mask, "open"].values
        - out["close"].shift(1)[roll_mask].values
    )

    # Cumulative sum backward (anchor = newest bar = 0.0 offset)
    out["cum_gap"] = (
        out["roll_gap"].iloc[::-1].cumsum().iloc[::-1].shift(-1).fillna(0.0)
    )

    for col in ohlc:
        out[col] = out[col] + out["cum_gap"]

    out.drop(columns=["roll_gap", "cum_gap"], inplace=True)
    return out


ADJUSTERS = {
    "raw": adjust_raw,
    "ratio": adjust_ratio,
    "panama": adjust_panama,
}


# ---------------------------------------------------------------------------
# Output Formatting
# ---------------------------------------------------------------------------

def _to_pipeline_format(df: pd.DataFrame) -> pd.DataFrame:
    """Convert to the format expected by DataProcessor.load_data().

    Returns a DataFrame with columns:
        Date (DD/MM/YYYY), Time (HH:MM), Open, High, Low, Close, Volume
    """
    dt = df["ts_event"]
    out = pd.DataFrame()
    out["Date"] = dt.dt.strftime("%d/%m/%Y")
    out["Time"] = dt.dt.strftime("%H:%M")
    out["Open"] = df["open"].values
    out["High"] = df["high"].values
    out["Low"] = df["low"].values
    out["Close"] = df["close"].values
    out["Volume"] = df["volume"].astype(int).values
    return out


def save_semicolon(df: pd.DataFrame, path: str) -> None:
    """Save as semicolon-separated CSV with no headers (pipeline format)."""
    pipe_df = _to_pipeline_format(df)
    pipe_df.to_csv(path, sep=";", header=False, index=False)
    print(f"  Saved ({len(pipe_df):,} rows): {path}")


def save_csv(df: pd.DataFrame, path: str) -> None:
    """Save as standard comma-separated CSV with headers (for inspection)."""
    pipe_df = _to_pipeline_format(df)
    pipe_df.to_csv(path, index=False)
    print(f"  Saved ({len(pipe_df):,} rows): {path}")


# ---------------------------------------------------------------------------
# High-Level Conversion API
# ---------------------------------------------------------------------------

def convert_databento_csv(
    input_path: str,
    output_dir: str,
    modes: list[str] | None = None,
    fmt: str = "semicolon",
) -> dict[str, str]:
    """Convert a raw Databento CSV into one or more adjusted output files.

    Args:
        input_path:  Path to the raw Databento CSV.
        output_dir:  Directory for output files.
        modes:       List of adjustment modes: 'raw', 'ratio', 'panama'.
                     Defaults to all three.
        fmt:         Output format: 'semicolon' (pipeline) or 'csv' (headers).

    Returns:
        Dict mapping mode name -> output file path.
    """
    if modes is None:
        modes = ["raw", "ratio", "panama"]

    os.makedirs(output_dir, exist_ok=True)
    df_base = _parse_databento_csv(input_path)

    print(f"Parsed {len(df_base):,} bars from {input_path}")
    n_rolls = df_base["is_roll"].sum()
    print(f"  Detected {n_rolls} contract rollovers")

    ext = ".csv"
    saver = save_semicolon if fmt == "semicolon" else save_csv
    outputs: dict[str, str] = {}

    for mode in modes:
        if mode not in ADJUSTERS:
            raise ValueError(f"Unknown mode '{mode}'. Choose from: {list(ADJUSTERS)}")

        adjuster = ADJUSTERS[mode]
        df_adj = adjuster(df_base)

        out_name = f"CL_{mode}{ext}"
        out_path = os.path.join(output_dir, out_name)
        saver(df_adj, out_path)
        outputs[mode] = out_path

    return outputs


# ---------------------------------------------------------------------------
# Legacy API (preserved for backward compatibility)
# ---------------------------------------------------------------------------

def back_adjust_continuous_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Post-processing function to parse Databento's raw CSV format, detect rollovers
    via instrument_id, convert fixed-precision integers to dollar decimals,
    and apply backward Ratio Scaling to the historical OHLC prices.

    NOTE: This is the original function preserved for backward compatibility.
    New code should use convert_databento_csv() instead.
    """
    if df.empty or 'instrument_id' not in df.columns:
        return df

    df_adj = df.copy()

    # Convert Unix nanosecond timestamps to human-readable datetime
    if 'ts_event' in df_adj.columns:
        df_adj['ts_event'] = pd.to_datetime(df_adj['ts_event'], unit='ns', utc=True)

    # 1. Convert fixed-precision Databento integers to standard dollar decimals (divide by 1e9)
    ohlc_cols = ['open', 'high', 'low', 'close']
    for col in ohlc_cols:
        if col in df_adj.columns:
            df_adj[col] = df_adj[col] / 1e9

    # 2. Detect contract rollovers using instrument_id
    # True when the instrument_id changes from the previous row
    df_adj['is_roll'] = (df_adj['instrument_id'] != df_adj['instrument_id'].shift(1)) & (df_adj['instrument_id'].shift(1).notna())

    # 3. Calculate Ratio Multiplier at each rollover
    # Factor = Open of new contract / Close of old contract
    df_adj['roll_factor'] = 1.0

    # We only apply the factor on the exact row where the roll happens
    roll_mask = df_adj['is_roll']
    df_adj.loc[roll_mask, 'roll_factor'] = df_adj.loc[roll_mask, 'open'] / df_adj['close'].shift(1)[roll_mask]

    # 4. Calculate Cumulative Ratio Factor (Backward)
    # Because we anchor to the CURRENT live price, the newest data has a multiplier of 1.0.
    # We must propagate the multiplier BACKWARDS into history.
    df_adj['roll_factor'] = df_adj['roll_factor'].replace([np.inf, -np.inf], 1.0).fillna(1.0)

    # We use cumprod backwards
    df_adj['cum_factor'] = df_adj['roll_factor'].iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)

    # 5. Apply Ratio Scaling to all OHLC historical prices
    for col in ohlc_cols:
        if col in df_adj.columns:
            df_adj[col] = df_adj[col] * df_adj['cum_factor']

    # 6. Clean up temporary columns
    df_adj = df_adj.drop(columns=['is_roll', 'roll_factor', 'cum_factor'])

    return df_adj


# ---------------------------------------------------------------------------
# Databento batch submission
# ---------------------------------------------------------------------------

def submit_batch():
    """Submit a batch download request to Databento for historical futures data."""
    import databento as db
    import os
    from dotenv import load_dotenv
    from pathlib import Path

    # Attempt to load .env from project root
    load_dotenv(Path(__file__).resolve().parent / ".env")

    api_key = os.getenv("DATABENTO_API_KEY")
    if not api_key:
        raise ValueError("DATABENTO_API_KEY not found. Please set it in .env or as an environment variable.")

    client = db.Historical(api_key)

    # Fetch the earliest available start date from metadata
    dataset_range = client.metadata.get_dataset_range(dataset="GLBX.MDP3")
    earliest_start = dataset_range['start'][:10]
    today_date = pd.Timestamp.today().strftime('%Y-%m-%d')

    print(f"Requesting full history from {earliest_start} to {today_date}...")

    try:
        job = client.batch.submit_job(
            dataset="GLBX.MDP3",
            symbols="CL.v.0",
            stype_in="continuous",
            schema="ohlcv-1h",
            start=earliest_start,
            end=today_date,
            encoding="csv",
            split_duration="none",
            compression="none"
        )

        print("Batch job submitted successfully!")
        print(f"Job Details: {job}")

    except db.BentoError as e:
        print(f"Databento API Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Databento CL Futures Data Pipeline"
    )
    sub = parser.add_subparsers(dest="command", help="Sub-command")

    # --- convert ---
    p_conv = sub.add_parser("convert", help="Convert raw Databento CSV to adjusted files")
    p_conv.add_argument("input", help="Path to raw Databento ohlcv-1h CSV")
    p_conv.add_argument(
        "--outdir", default=None,
        help="Output directory (default: same dir as input)"
    )
    p_conv.add_argument(
        "--mode", default="all",
        choices=["raw", "ratio", "panama", "all"],
        help="Adjustment mode (default: all)",
    )
    p_conv.add_argument(
        "--format", dest="fmt", default="semicolon",
        choices=["semicolon", "csv"],
        help="Output format (default: semicolon — pipeline-ready)",
    )

    # --- submit ---
    sub.add_parser("submit", help="Submit a new Databento batch download job")

    args = parser.parse_args()

    if args.command == "convert":
        outdir = args.outdir or str(Path(args.input).parent)
        modes = ["raw", "ratio", "panama"] if args.mode == "all" else [args.mode]
        convert_databento_csv(args.input, outdir, modes=modes, fmt=args.fmt)

    elif args.command == "submit":
        submit_batch()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
