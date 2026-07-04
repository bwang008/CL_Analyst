"""
Databento Continuous Futures Data Pipeline — Consolidated Module.

Reads the raw Databento continuous contract CSV (e.g. CL.v.0, ohlcv-1h)
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
    from src.data.databento_data_builder import DatabentoDataBuilder
    builder = DatabentoDataBuilder()
    builder.convert_databento_csv("path/to/raw.csv", "output_dir", symbol="CL")

    # From CLI:
    python -m src.data.databento_data_builder estimate --symbols CL HG GC
    python -m src.data.databento_data_builder canary  --symbols CL HG --days 30
    python -m src.data.databento_data_builder submit  --symbols CL --start 2011-01-01
    python -m src.data.databento_data_builder download --job-id GLBX-XXXXXXXX --outdir data/
    python -m src.data.databento_data_builder convert  raw.csv --symbol CL --outdir output/
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mapping — CLI short names → Databento continuous contract codes
# ---------------------------------------------------------------------------

SYMBOL_MAP: dict[str, str] = {
    "CL": "CL.v.0",   # Crude Oil
    "HG": "HG.v.0",   # Copper
    "PA": "PA.v.0",   # Palladium
    "GC": "GC.v.0",   # Gold
    "NG": "NG.v.0",   # Natural Gas
    "ES": "ES.v.0",   # E-mini S&P 500
    "NQ": "NQ.v.0",   # E-mini Nasdaq 100
    "ZC": "ZC.v.0",   # Corn (CBOT)
    "ZS": "ZS.v.0",   # Soybeans (CBOT)
    "SI": "SI.v.0",   # Silver (COMEX)
}

DEFAULT_DATASET = "GLBX.MDP3"
DEFAULT_SCHEMA = "ohlcv-1h"


def _resolve_symbols(symbols: str | list[str]) -> list[str]:
    """Map CLI short names (CL, HG, ...) to Databento continuous codes.

    Pass-through any symbol that already contains a dot (e.g. ``CL.v.0``).
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    resolved = []
    for s in symbols:
        if "." in s:
            resolved.append(s)
        else:
            resolved.append(SYMBOL_MAP.get(s.upper(), f"{s.upper()}.v.0"))
    return resolved


def _load_api_key() -> str:
    """Load the Databento API key from the environment, trying dotenv first."""
    try:
        from dotenv import load_dotenv
        # Walk up from this file to find .env at the project root
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except ImportError:
        pass
    key = os.getenv("DATABENTO_API_KEY", "")
    if not key:
        raise ValueError(
            "DATABENTO_API_KEY not found. Set it in .env or as an "
            "environment variable."
        )
    return key


# ═══════════════════════════════════════════════════════════════════════════
# DatabentoDataBuilder class
# ═══════════════════════════════════════════════════════════════════════════

class DatabentoDataBuilder:
    """Fetches historical continuous data from Databento and applies various back-adjustments."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("DATABENTO_API_KEY")
        self._client = None

    @property
    def client(self):
        """Lazy-initialised Databento Historical client."""
        if self._client is None:
            import databento as db

            if not self.api_key:
                self.api_key = _load_api_key()
            self._client = db.Historical(self.api_key)
        return self._client

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    def get_dataset_range(self, dataset: str = DEFAULT_DATASET) -> dict:
        """Query the Databento metadata API for the available date range.

        Returns:
            dict with ``'start'`` and ``'end'`` date strings.
        """
        log.info("Querying dataset range for %s ...", dataset)
        result = self.client.metadata.get_dataset_range(dataset=dataset)
        start = str(result["start"])[:10]
        end = str(result["end"])[:10]
        log.info("  Available range: %s → %s", start, end)
        return {"start": start, "end": end}

    def get_cost_estimate(
        self,
        symbols: str | list[str],
        start: str,
        end: str,
        schema: str = DEFAULT_SCHEMA,
        dataset: str = DEFAULT_DATASET,
    ) -> dict:
        """Estimate API credit cost *before* submitting a batch job.

        Returns:
            The cost information dict from the Databento API.
        """
        symbols = _resolve_symbols(symbols)
        log.info(
            "Estimating cost for %s  %s  %s → %s ...",
            symbols, schema, start, end,
        )
        cost = self.client.metadata.get_cost(
            dataset=dataset,
            symbols=symbols,
            schema=schema,
            start=start,
            end=end,
            stype_in="continuous",
        )
        log.info("  Estimated cost: %s", cost)
        return cost

    # ------------------------------------------------------------------
    # Batch job lifecycle
    # ------------------------------------------------------------------

    def submit_historical_batch(
        self,
        symbols: str | list[str] = "CL.v.0",
        start: str | pd.Timestamp = "2011-01-01",
        end: str | pd.Timestamp | None = None,
        schema: str = DEFAULT_SCHEMA,
        dataset: str = DEFAULT_DATASET,
    ) -> dict:
        """Submit a batch job to Databento for historical data.

        Args:
            symbols: One or more Databento continuous contract codes
                     (e.g. ``"CL.v.0"``).
            start: Start date (inclusive).
            end: End date (exclusive).  Defaults to today (UTC).
            schema: Databento data schema (default ``ohlcv-1h``).
            dataset: Databento dataset identifier.

        Returns:
            The job metadata dict (contains ``'id'``).
        """
        if end is None:
            end = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")

        symbols = _resolve_symbols(symbols)
        log.info("Submitting batch job for %s from %s to %s ...", symbols, start, end)

        job = self.client.batch.submit_job(
            dataset=dataset,
            symbols=symbols,
            schema=schema,
            start=start,
            end=end,
            encoding="csv",
            compression="none",
            split_duration="none",
            stype_in="continuous",
        )
        log.info("Job submitted successfully. Job ID: %s", job["id"])
        return job

    def poll_job(
        self,
        job_id: str,
        interval: int = 30,
        timeout: int = 3600,
    ) -> dict:
        """Poll ``client.batch.list_jobs()`` until the job is done or timeout.

        Args:
            job_id: The batch job ID (e.g. ``GLBX-20260601-...``).
            interval: Seconds between polling attempts.
            timeout: Maximum total seconds to wait.

        Returns:
            The job info dict once status is ``'done'``.

        Raises:
            TimeoutError: If the job does not finish within *timeout* seconds.
            RuntimeError: If the job reaches an error state.
        """
        log.info("Polling job %s (interval=%ds, timeout=%ds) ...", job_id, interval, timeout)
        elapsed = 0
        while elapsed < timeout:
            jobs = self.client.batch.list_jobs()
            job = None
            for j in jobs:
                if j.get("id") == job_id:
                    job = j
                    break

            if job is None:
                log.warning("Job %s not found in list_jobs response.", job_id)
            else:
                status = job.get("state", job.get("status", "unknown"))
                log.info(
                    "  [%ds elapsed] Job %s status: %s",
                    elapsed, job_id, status,
                )
                if status in ("done", "expired"):
                    return job
                if status in ("error", "failed"):
                    raise RuntimeError(
                        f"Batch job {job_id} failed with status '{status}': "
                        f"{job}"
                    )

            time.sleep(interval)
            elapsed += interval

        raise TimeoutError(
            f"Job {job_id} did not complete within {timeout}s."
        )

    def download_job(
        self,
        job_id: str,
        output_dir: str | Path,
    ) -> list[Path]:
        """Download the files for a completed batch job.

        Args:
            job_id: The batch job ID.
            output_dir: Local directory to save files to.

        Returns:
            List of downloaded file paths.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        log.info("Downloading job %s → %s ...", job_id, output_dir)

        downloaded = self.client.batch.download(job_id, str(output_dir))
        paths = [Path(p) for p in downloaded]
        log.info("  Downloaded %d file(s): %s", len(paths), [p.name for p in paths])
        return paths

    def submit_and_download(
        self,
        symbols: str | list[str],
        start: str,
        end: str,
        output_dir: str | Path,
        schema: str = DEFAULT_SCHEMA,
    ) -> list[Path]:
        """Convenience: submit → poll → download in one call.

        Returns:
            List of downloaded file paths.
        """
        job = self.submit_historical_batch(
            symbols=symbols, start=start, end=end, schema=schema,
        )
        job_id = job["id"]
        self.poll_job(job_id)
        return self.download_job(job_id, output_dir)

    # ------------------------------------------------------------------
    # Canary / smoke test
    # ------------------------------------------------------------------

    def run_canary_test(
        self,
        symbols: str | list[str],
        days: int = 30,
        output_dir: str | Path | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Run a minimal N-day smoke test for each symbol.

        Submits **one batch per symbol** so each gets its own CSV file
        (Databento interleaves multiple symbols into a single CSV when
        submitted together, making per-symbol validation harder).

        Files are saved to ``<output_dir>/<SYMBOL>/`` (one sub-directory
        per symbol).  If ``output_dir`` is None, defaults to
        ``$CL_DATA_ROOT/data/raw/DataBentoSample/``.

        Checks performed per symbol:
            - Expected columns present
            - ``price / 1e9`` produces plausible values (0.01–100 000)
            - Row count > 0
            - Timestamps are UTC hourly

        Args:
            symbols: List of short symbol names or Databento codes.
            days: Number of recent calendar days to request.
            output_dir: Root directory for per-symbol sub-folders.

        Returns:
            Per-symbol dict of ``{"pass": bool, "details": str, ...}``.
        """
        symbols_resolved = _resolve_symbols(symbols)

        if output_dir is None:
            from src.data_paths import get_data_root
            output_dir = get_data_root() / "raw" / "DataBentoSample"
        else:
            output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Databento CME data has a publication lag (~1 hour); using today
        # triggers 422 dataset_unavailable_range.  Use yesterday as safe end.
        end_date = (
            pd.Timestamp.now(tz="UTC") - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        start_date = (
            pd.Timestamp.now(tz="UTC") - timedelta(days=days + 1)
        ).strftime("%Y-%m-%d")

        log.info(
            "Canary test: %s, %s → %s (%d days)",
            symbols_resolved, start_date, end_date, days,
        )

        results: dict[str, dict[str, Any]] = {}

        for db_sym in symbols_resolved:
            # Extract short name (e.g. "ES" from "ES.v.0")
            short_name = db_sym.split(".")[0]
            sym_dir = output_dir / short_name
            sym_dir.mkdir(parents=True, exist_ok=True)

            log.info("--- Canary: %s (%s) ---", short_name, db_sym)

            try:
                # Submit one batch per symbol → clean, single-symbol CSV
                files = self.submit_and_download(
                    symbols=[db_sym],
                    start=start_date,
                    end=end_date,
                    output_dir=sym_dir,
                )

                # Validate the downloaded CSV
                csv_files = [f for f in files if f.suffix == ".csv"]
                if not csv_files:
                    results[short_name] = {
                        "pass": False,
                        "details": "No CSV file in download",
                        "rows": 0,
                    }
                    continue

                result = self._validate_canary_file(csv_files[0], [db_sym])
                result["output_dir"] = str(sym_dir)
                result["csv_file"] = str(csv_files[0])
                results[short_name] = result

            except Exception as exc:
                log.error("  %s FAILED: %s", short_name, exc)
                results[short_name] = {
                    "pass": False,
                    "details": str(exc),
                    "rows": 0,
                }

        # Summary
        n_pass = sum(1 for r in results.values() if r.get("pass"))
        log.info(
            "Canary summary: %d/%d passed",
            n_pass, len(results),
        )
        for sym, info in results.items():
            status = "PASS" if info["pass"] else "FAIL"
            log.info("  %s: %s — %s", sym, status, info.get("details", ""))

        return results

    def _validate_canary_file(
        self,
        path: Path,
        expected_symbols: list[str],
    ) -> dict[str, Any]:
        """Validate a single Databento CSV file for the canary test."""
        expected_cols = {
            "ts_event", "rtype", "publisher_id", "instrument_id",
            "open", "high", "low", "close", "volume",
        }

        df = pd.read_csv(path)
        issues: list[str] = []

        # Column check
        missing = expected_cols - set(df.columns)
        if missing:
            issues.append(f"Missing columns: {missing}")

        # Row count
        if len(df) == 0:
            issues.append("0 rows — empty file")
            return {"pass": False, "details": "; ".join(issues), "rows": 0}

        # Price sanity (divide by 1e9)
        sample_price = df["close"].iloc[0] / 1e9
        if not (0.01 <= sample_price <= 100_000):
            issues.append(
                f"Price/1e9 = {sample_price:.4f} outside [0.01, 100000]"
            )

        # Timestamp checks
        ts_col = pd.to_datetime(df["ts_event"], unit="ns", utc=True)
        if ts_col.dt.tz is None:
            issues.append("Timestamps are not UTC")
        # Multi-symbol files interleave rows (same ts for different symbols).
        # Filter to the first instrument_id before checking bar intervals.
        first_iid = df["instrument_id"].iloc[0]
        ts_single = ts_col[df["instrument_id"] == first_iid]
        hour_diffs = ts_single.diff().dropna().dt.total_seconds()
        # Check that the modal diff is 3600s (hourly bars)
        if len(hour_diffs) > 0:
            modal_diff = hour_diffs.mode()
            if len(modal_diff) == 0 or modal_diff.iloc[0] != 3600.0:
                issues.append(
                    f"Modal bar interval = {modal_diff.iloc[0] if len(modal_diff) else 'N/A'}s "
                    f"(expected 3600s)"
                )

        passed = len(issues) == 0
        return {
            "pass": passed,
            "details": "; ".join(issues) if issues else "OK",
            "rows": len(df),
            "first_ts": str(ts_col.iloc[0]),
            "last_ts": str(ts_col.iloc[-1]),
            "sample_price": round(sample_price, 4),
        }

    # ------------------------------------------------------------------
    # CSV parsing and adjustment
    # ------------------------------------------------------------------

    def parse_raw_csv(self, path: str) -> pd.DataFrame:
        """Load the raw Databento CSV and convert to a standard DataFrame.

        Returns a DataFrame indexed by DateTime (UTC) with columns:
            open, high, low, close, volume, instrument_id
        Prices are converted from Databento fixed-precision integers to dollars.
        """
        log.info("Loading raw Databento CSV from %s", path)
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

    # ------------------------------------------------------------------
    # Conversion & output
    # ------------------------------------------------------------------

    def convert_databento_csv(
        self,
        input_path: str,
        output_dir: str,
        symbol: str = "CL",
        modes: list[str] | None = None,
        fmt: str = "semicolon",
    ) -> dict[str, str]:
        """Convert raw Databento CSV to adjusted formats.

        Args:
            input_path: Path to the raw Databento CSV.
            output_dir: Directory to save the output files.
            symbol: Short symbol name used in output filenames
                    (e.g. ``"CL"`` → ``CL_raw.csv``).
            modes: List of adjustment modes: 'raw', 'ratio', 'panama'.
                   Defaults to all three.
            fmt: Output format: 'semicolon' (pipeline format) or 'csv' (standard CSV).

        Returns:
            Dict mapping mode → output file path.
        """
        if modes is None:
            modes = ["raw", "ratio", "panama"]

        os.makedirs(output_dir, exist_ok=True)
        df_base = self.parse_raw_csv(input_path)

        log.info("Parsed %s bars from %s", f"{len(df_base):,}", input_path)
        n_rolls = df_base["is_roll"].sum()
        log.info("  Detected %d contract rollovers", n_rolls)

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
            out_name = f"{symbol}_{mode}{ext}"
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
        log.info("  Saved (%s rows): %s", f"{len(pipe_df):,}", path)

    def save_csv(self, df: pd.DataFrame, path: str) -> None:
        """Save as standard comma-separated CSV with headers (for inspection)."""
        pipe_df = self._to_pipeline_format(df)
        pipe_df.to_csv(path, index=False)
        log.info("  Saved (%s rows): %s", f"{len(pipe_df):,}", path)


# ═══════════════════════════════════════════════════════════════════════════
# Module-level legacy API (preserved for backward compatibility)
# ═══════════════════════════════════════════════════════════════════════════

def back_adjust_continuous_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Post-processing function to parse Databento's raw CSV format, detect rollovers
    via instrument_id, convert fixed-precision integers to dollar decimals,
    and apply backward Ratio Scaling to the historical OHLC prices.

    NOTE: This is the original function preserved for backward compatibility.
    New code should use ``DatabentoDataBuilder.adjust_ratio()`` instead.
    """
    if df.empty or "instrument_id" not in df.columns:
        return df

    df_adj = df.copy()

    # Convert Unix nanosecond timestamps to human-readable datetime
    if "ts_event" in df_adj.columns:
        df_adj["ts_event"] = pd.to_datetime(df_adj["ts_event"], unit="ns", utc=True)

    # 1. Convert fixed-precision Databento integers to standard dollar decimals (divide by 1e9)
    ohlc_cols = ["open", "high", "low", "close"]
    for col in ohlc_cols:
        if col in df_adj.columns:
            df_adj[col] = df_adj[col] / 1e9

    # 2. Detect contract rollovers using instrument_id
    # True when the instrument_id changes from the previous row
    df_adj["is_roll"] = (
        (df_adj["instrument_id"] != df_adj["instrument_id"].shift(1))
        & (df_adj["instrument_id"].shift(1).notna())
    )

    # 3. Calculate Ratio Multiplier at each rollover
    # Factor = Open of new contract / Close of old contract
    df_adj["roll_factor"] = 1.0

    # We only apply the factor on the exact row where the roll happens
    roll_mask = df_adj["is_roll"]
    df_adj.loc[roll_mask, "roll_factor"] = (
        df_adj.loc[roll_mask, "open"] / df_adj["close"].shift(1)[roll_mask]
    )

    # 4. Calculate Cumulative Ratio Factor (Backward)
    # Because we anchor to the CURRENT live price, the newest data has a multiplier of 1.0.
    # We must propagate the multiplier BACKWARDS into history.
    df_adj["roll_factor"] = df_adj["roll_factor"].replace(
        [np.inf, -np.inf], 1.0
    ).fillna(1.0)

    # We use cumprod backwards
    df_adj["cum_factor"] = (
        df_adj["roll_factor"].iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
    )

    # 5. Apply Ratio Scaling to all OHLC historical prices
    for col in ohlc_cols:
        if col in df_adj.columns:
            df_adj[col] = df_adj[col] * df_adj["cum_factor"]

    # 6. Clean up temporary columns
    df_adj = df_adj.drop(columns=["is_roll", "roll_factor", "cum_factor"])

    return df_adj


# ═══════════════════════════════════════════════════════════════════════════
# Module-level convenience wrappers (for direct imports)
# ═══════════════════════════════════════════════════════════════════════════


def convert_databento_csv(
    input_path: str,
    output_dir: str,
    symbol: str = "CL",
    modes: list[str] | None = None,
    fmt: str = "semicolon",
) -> dict[str, str]:
    """Module-level convenience wrapper for DatabentoDataBuilder.convert_databento_csv.

    Preserved for backward compatibility with scripts that import it directly::

        from src.data.databento_data_builder import convert_databento_csv
    """
    builder = DatabentoDataBuilder()
    return builder.convert_databento_csv(
        input_path=input_path,
        output_dir=output_dir,
        symbol=symbol,
        modes=modes,
        fmt=fmt,
    )


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Databento Continuous Futures Data Builder & Multi-Adjustment Tool",
    )
    sub = parser.add_subparsers(dest="command", help="Sub-command")

    # ── estimate ──────────────────────────────────────────────────────
    p_est = sub.add_parser(
        "estimate", help="Estimate API credit cost before submitting",
    )
    p_est.add_argument(
        "--symbols", nargs="+", required=True,
        help="Symbol short names (e.g. CL HG GC NG PA ES NQ)",
    )
    p_est.add_argument("--start", default="2011-01-01", help="Start date (YYYY-MM-DD)")
    p_est.add_argument("--end", default=None, help="End date (YYYY-MM-DD, default: today)")

    # ── canary ────────────────────────────────────────────────────────
    p_can = sub.add_parser(
        "canary", help="Run a small smoke test download & validate structure",
    )
    p_can.add_argument(
        "--symbols", nargs="+", required=True,
        help="Symbol short names",
    )
    p_can.add_argument("--days", type=int, default=30, help="Days of recent data (default: 30)")
    p_can.add_argument("--outdir", default=None, help="Output directory")

    # ── submit ────────────────────────────────────────────────────────
    p_sub = sub.add_parser(
        "submit", help="Submit a new Databento batch download job",
    )
    p_sub.add_argument(
        "--symbols", nargs="+", required=True,
        help="Symbol short names",
    )
    p_sub.add_argument("--start", default="2011-01-01", help="Start date (YYYY-MM-DD)")
    p_sub.add_argument("--end", default=None, help="End date (YYYY-MM-DD, default: today)")
    p_sub.add_argument("--outdir", default=None, help="Download output dir (polls & downloads)")

    # ── download ──────────────────────────────────────────────────────
    p_dl = sub.add_parser(
        "download", help="Download files from a completed batch job",
    )
    p_dl.add_argument("--job-id", required=True, help="Batch job ID (e.g. GLBX-XXXXXXXX)")
    p_dl.add_argument("--outdir", required=True, help="Output directory")

    # ── convert ───────────────────────────────────────────────────────
    p_conv = sub.add_parser(
        "convert", help="Convert raw Databento CSV to adjusted files",
    )
    p_conv.add_argument("input", help="Path to raw Databento ohlcv-1h CSV")
    p_conv.add_argument(
        "--symbol", default="CL",
        help="Symbol short name for output filenames (default: CL)",
    )
    p_conv.add_argument(
        "--outdir", default=None,
        help="Output directory (default: same dir as input)",
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

    # ── dispatch ──────────────────────────────────────────────────────

    if args.command == "estimate":
        builder = DatabentoDataBuilder()
        end = args.end or pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        cost = builder.get_cost_estimate(
            symbols=args.symbols, start=args.start, end=end,
        )
        log.info("Cost estimate result: %s", cost)

    elif args.command == "canary":
        builder = DatabentoDataBuilder()
        results = builder.run_canary_test(
            symbols=args.symbols,
            days=args.days,
            output_dir=args.outdir,
        )
        for sym, info in results.items():
            status = "PASS" if info["pass"] else "FAIL"
            log.info("[%s] %s — %s", sym, status, info.get("details", ""))

    elif args.command == "submit":
        builder = DatabentoDataBuilder()
        end = args.end or pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        if args.outdir:
            # Full lifecycle: submit → poll → download
            files = builder.submit_and_download(
                symbols=args.symbols,
                start=args.start,
                end=end,
                output_dir=args.outdir,
            )
            log.info("Downloaded %d file(s):", len(files))
            for f in files:
                log.info("  %s", f)
        else:
            # Just submit and print the job ID
            job = builder.submit_historical_batch(
                symbols=args.symbols, start=args.start, end=end,
            )
            log.info("Job ID: %s", job["id"])
            log.info("Use  download --job-id %s --outdir <DIR>  to fetch results.", job["id"])

    elif args.command == "download":
        builder = DatabentoDataBuilder()
        files = builder.download_job(args.job_id, args.outdir)
        log.info("Downloaded %d file(s):", len(files))
        for f in files:
            log.info("  %s", f)

    elif args.command == "convert":
        builder = DatabentoDataBuilder()
        outdir = args.outdir or str(Path(args.input).parent)
        modes = ["raw", "ratio", "panama"] if args.mode == "all" else [args.mode]
        builder.convert_databento_csv(
            args.input, outdir, symbol=args.symbol, modes=modes, fmt=args.fmt,
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
