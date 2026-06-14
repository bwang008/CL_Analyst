import os
import logging
import argparse
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd
import databento as db

log = logging.getLogger(__name__)

class DatabentoDataBuilder:
    """Fetches historical continuous data from Databento and applies various back-adjustments."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("DATABENTO_API_KEY")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not self.api_key:
                raise ValueError("DATABENTO_API_KEY not found in environment.")
            self._client = db.Historical(self.api_key)
        return self._client

    def submit_historical_batch(
        self,
        dataset: str = "GLB.CME",
        symbols: str | list[str] = "CL.v.0",
        start: str | pd.Timestamp = "2011-01-01",
        end: str | pd.Timestamp = "2026-06-01",
        schema: str = "ohlcv-1h",
    ):
        """Submit a batch job to Databento for historical data."""
        log.info(f"Submitting batch job for {symbols} from {start} to {end}...")
        job = self.client.timeseries.submit_job(
            dataset=dataset,
            symbols=symbols,
            schema=schema,
            start=start,
            end=end,
            encoding="csv",
            delivery="download",
            stype_in="continuous",
        )
        log.info(f"Job submitted successfully. Job ID: {job['id']}")
        return job

    def parse_raw_csv(self, path: str) -> pd.DataFrame:
        """Load the raw Databento CSV and convert to a standard DataFrame.

        Returns a DataFrame indexed by DateTime (UTC) with columns:
            open, high, low, close, volume, instrument_id
        Prices are converted from Databento fixed-precision integers to dollars.
        """
        log.info(f"Loading raw Databento CSV from {path}")
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

    def adjust_raw(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return the raw unadjusted OHLCV."""
        return df.copy()

    def adjust_ratio(self, df: pd.DataFrame) -> pd.DataFrame:
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

    def adjust_panama(self, df: pd.DataFrame) -> pd.DataFrame:
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

    def process_and_adjust_data(self, file_path: str) -> pd.DataFrame:
        """Legacy API for backward compatibility. Applies Ratio Back-Adjustment."""
        df_parsed = self.parse_raw_csv(file_path)
        return self.adjust_ratio(df_parsed)

    def convert_databento_csv(
        self,
        input_path: str,
        output_dir: str,
        modes: list[str] | None = None,
        fmt: str = "semicolon",
    ) -> dict[str, str]:
        """Convert raw Databento CSV to adjusted formats.

        Args:
            input_path: Path to the raw Databento CSV.
            output_dir: Directory to save the output files.
            modes: List of adjustment modes: 'raw', 'ratio', 'panama'.
                   Defaults to all three.
            fmt: Output format: 'semicolon' (pipeline format) or 'csv' (standard CSV).

        Returns:
            Dict mapping mode -> output file path.
        """
        if modes is None:
            modes = ["raw", "ratio", "panama"]

        os.makedirs(output_dir, exist_ok=True)
        df_base = self.parse_raw_csv(input_path)

        log.info(f"Parsed {len(df_base):,} bars from {input_path}")
        n_rolls = df_base["is_roll"].sum()
        log.info(f"  Detected {n_rolls} contract rollovers")

        ext = ".csv"
        outputs: dict[str, str] = {}

        adjusters = {
            "raw": self.adjust_raw,
            "ratio": self.adjust_ratio,
            "panama": self.adjust_panama,
        }

        for mode in modes:
            if mode not in adjusters:
                raise ValueError(f"Unknown mode '{mode}'. Choose from: {list(adjusters)}")

            df_adj = adjusters[mode](df_base)
            out_name = f"CL_{mode}{ext}"
            out_path = os.path.join(output_dir, out_name)

            if fmt == "semicolon":
                self.save_semicolon(df_adj, out_path)
            else:
                self.save_csv(df_adj, out_path)

            outputs[mode] = out_path

        return outputs

    def _to_pipeline_format(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convert to the format expected by DataProcessor.load_data()."""
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

    def save_semicolon(self, df: pd.DataFrame, path: str) -> None:
        """Save as semicolon-separated CSV with no headers (pipeline format)."""
        pipe_df = self._to_pipeline_format(df)
        pipe_df.to_csv(path, sep=";", header=False, index=False)
        log.info(f"  Saved ({len(pipe_df):,} rows): {path}")

    def save_csv(self, df: pd.DataFrame, path: str) -> None:
        """Save as standard comma-separated CSV with headers (for inspection)."""
        pipe_df = self._to_pipeline_format(df)
        pipe_df.to_csv(path, index=False)
        log.info(f"  Saved ({len(pipe_df):,} rows): {path}")

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    parser = argparse.ArgumentParser(
        description="Databento Continuous Futures Data Builder & Multi-Adjustment Tool"
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

    args = parser.parse_args()

    if args.command == "convert":
        builder = DatabentoDataBuilder()
        outdir = args.outdir or str(Path(args.input).parent)
        modes = ["raw", "ratio", "panama"] if args.mode == "all" else [args.mode]
        builder.convert_databento_csv(args.input, outdir, modes=modes, fmt=args.fmt)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
