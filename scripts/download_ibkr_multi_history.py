"""
Download Multi-Symbol Continuous Contract History from IBKR.

Downloads 1-hour bars for multiple continuous futures contracts from
Interactive Brokers and optionally runs a parity check against
Databento canary data.

Supported symbols: CL, HG, PA, GC, NG, ES, NQ.

Usage:
    # Download all 7 symbols (30 days of 1-hour bars)
    python scripts/download_ibkr_multi_history.py

    # Download specific symbols
    python scripts/download_ibkr_multi_history.py --symbols CL ES NQ

    # Download + parity check against Databento data
    python scripts/download_ibkr_multi_history.py --parity-check /path/to/databento/canary

    # Skip download, only run parity check
    python scripts/download_ibkr_multi_history.py --skip-download --parity-check /path/to/databento/canary

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data_paths import get_data_root

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("IBKRMultiDownloader")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THROTTLE_SECONDS = 12.0     # Conservative: 12s between symbol requests
PACING_BACKOFF = 120.0      # Wait 2 minutes on pacing violation
MAX_RETRIES = 5             # Retries per symbol request
DEFAULT_DAYS = 30

# Symbol -> (exchange, currency)
SYMBOL_MAP: dict[str, tuple[str, str]] = {
    "CL": ("NYMEX", "USD"),
    "HG": ("COMEX", "USD"),
    "PA": ("NYMEX", "USD"),
    "GC": ("COMEX", "USD"),
    "NG": ("NYMEX", "USD"),
    "ES": ("CME",   "USD"),
    "NQ": ("CME",   "USD"),
}

ALL_SYMBOLS = list(SYMBOL_MAP.keys())

# Pipeline CSV format: DD/MM/YYYY;HH:MM;O;H;L;C;V  (semicolon-separated, no header)
CSV_SEP = ";"
CSV_DATE_FMT = "%d/%m/%Y"
CSV_TIME_FMT = "%H:%M"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _df_to_pipeline_csv(df: pd.DataFrame, path: Path) -> None:
    """Save a DataFrame with OHLCV columns to the pipeline CSV format.

    Format: DD/MM/YYYY;HH:MM;O;H;L;C;V  (semicolon-separated, no header).
    Expects the DataFrame index to be a DatetimeIndex.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for ts, row in df.iterrows():
        date_str = ts.strftime(CSV_DATE_FMT)
        time_str = ts.strftime(CSV_TIME_FMT)
        line = CSV_SEP.join([
            date_str,
            time_str,
            f"{row['Open']:.6f}",
            f"{row['High']:.6f}",
            f"{row['Low']:.6f}",
            f"{row['Close']:.6f}",
            str(int(row["Volume"])),
        ])
        lines.append(line)
    path.write_text("\n".join(lines), encoding="utf-8")


def _load_pipeline_csv(path: Path) -> pd.DataFrame:
    """Load a pipeline-format CSV into a DataFrame with DatetimeIndex.

    Format: DD/MM/YYYY;HH:MM;O;H;L;C;V  (semicolon-separated, no header).
    """
    df = pd.read_csv(
        path,
        sep=CSV_SEP,
        header=None,
        names=["Date", "Time", "Open", "High", "Low", "Close", "Volume"],
    )
    df["Datetime"] = pd.to_datetime(
        df["Date"] + " " + df["Time"],
        format=f"{CSV_DATE_FMT} {CSV_TIME_FMT}",
    )
    df = df.set_index("Datetime").drop(columns=["Date", "Time"])
    for col in ["Open", "High", "Low", "Close"]:
        df[col] = df[col].astype(float)
    df["Volume"] = df["Volume"].astype(int)
    return df


# ---------------------------------------------------------------------------
# Download logic
# ---------------------------------------------------------------------------


def download_symbol(
    manager: "IBKRConnectionManager",  # noqa: F821
    symbol: str,
    exchange: str,
    currency: str,
    days: int,
) -> Optional[pd.DataFrame]:
    """Download 1-hour bars for a single symbol's continuous contract.

    Parameters
    ----------
    manager : IBKRConnectionManager
        Connected IBKR connection manager.
    symbol : str
        Futures symbol (e.g. ``"CL"``).
    exchange : str
        IBKR exchange (e.g. ``"NYMEX"``).
    currency : str
        Contract currency (e.g. ``"USD"``).
    days : int
        Number of days of history to request.

    Returns
    -------
    pd.DataFrame or None
        DataFrame with DatetimeIndex and OHLCV columns, or ``None``
        on failure.
    """
    from ib_insync import ContFuture

    from src.live_execution.ibkr_client import ib_bars_to_dataframe

    contract = ContFuture(symbol=symbol, exchange=exchange, currency=currency)

    # Qualify contract
    try:
        qualified = manager.ib.qualifyContracts(contract)
        if not qualified:
            log.error("  Could not qualify %s ContFuture — skipping", symbol)
            return None
        contract = qualified[0]
        log.info(
            "  Qualified %s ContFuture: %s (conId=%s)",
            symbol, contract.localSymbol, contract.conId,
        )
    except Exception as exc:
        log.error("  Qualify failed for %s: %s", symbol, exc)
        return None

    # Request historical data
    duration_str = f"{days} D"
    try:
        bars = manager._request_historical_data(
            contract=contract,
            duration_str=duration_str,
            bar_size="1 hour",
            what_to_show="TRADES",
            use_rth=False,
            end_datetime="",
            max_retries=MAX_RETRIES,
            backoff_seconds=5.0,
            throttle_seconds=1.0,
        )

        if not bars:
            log.warning("  No bars returned for %s", symbol)
            return None

        df = ib_bars_to_dataframe(bars)
        log.info(
            "  -> %s: %d bars, %s to %s (%d days)",
            symbol,
            len(df),
            df.index.min(),
            df.index.max(),
            (df.index.max() - df.index.min()).days,
        )
        return df

    except Exception as exc:
        err_str = str(exc).lower()
        log.error("  Download failed for %s: %s", symbol, exc)
        if "pacing" in err_str:
            log.info("  Pacing violation — waiting %.0fs ...", PACING_BACKOFF)
            time.sleep(PACING_BACKOFF)
        return None


def download_all(
    *,
    host: str = "127.0.0.1",
    port: int = 4002,
    client_id: int = 30,
    days: int = DEFAULT_DAYS,
    symbols: list[str] | None = None,
    outdir: Path | None = None,
) -> dict[str, dict]:
    """Download 1-hour bars for multiple symbols from IBKR.

    Parameters
    ----------
    host : str
        IBKR Gateway host.
    port : int
        IBKR Gateway port.
    client_id : int
        IBKR client ID (use a value that won't conflict with live traders).
    days : int
        Days of history to download per symbol.
    symbols : list[str] or None
        Symbols to download. Defaults to all 7.
    outdir : Path or None
        Output directory. Defaults to ``$CL_DATA_ROOT/data/raw/ibkr_samples``.

    Returns
    -------
    dict[str, dict]
        Per-symbol results: ``{"status", "bars", "date_range", "path"}``.
    """
    from src.live_execution.ibkr_client import IBKRConnectionManager

    if symbols is None:
        symbols = ALL_SYMBOLS
    if outdir is None:
        outdir = get_data_root() / "raw" / "ibkr_samples"

    outdir.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict] = {}

    log.info("=" * 60)
    log.info("IBKR Multi-Symbol History Downloader")
    log.info("=" * 60)
    log.info("  Symbols:   %s", ", ".join(symbols))
    log.info("  Days:      %d", days)
    log.info("  Bar size:  1 hour")
    log.info("  Output:    %s", outdir)
    log.info("  Host:      %s:%d  ClientID: %d", host, port, client_id)
    log.info("")

    manager = IBKRConnectionManager(
        host=host,
        port=port,
        client_id=client_id,
        readonly=True,
    )

    try:
        log.info("Connecting to IBKR...")
        manager.connect()
        log.info("Connected!")
        log.info("")

        for i, sym in enumerate(symbols):
            if sym not in SYMBOL_MAP:
                log.error("Unknown symbol: %s — skipping", sym)
                results[sym] = {"status": "UNKNOWN_SYMBOL", "bars": 0, "date_range": "-", "path": ""}
                continue

            exchange, currency = SYMBOL_MAP[sym]
            log.info("-" * 40)
            log.info("Downloading %s (%d/%d) ...", sym, i + 1, len(symbols))
            log.info("-" * 40)

            df = download_symbol(manager, sym, exchange, currency, days)

            if df is not None and len(df) > 0:
                csv_path = outdir / f"{sym}_ibkr_1h.csv"
                _df_to_pipeline_csv(df, csv_path)
                log.info("  Saved: %s (%d bars)", csv_path, len(df))

                results[sym] = {
                    "status": "OK",
                    "bars": len(df),
                    "date_range": f"{df.index.min()} -> {df.index.max()}",
                    "path": str(csv_path),
                }
            else:
                log.warning("  FAILED: no data for %s", sym)
                results[sym] = {
                    "status": "FAILED",
                    "bars": 0,
                    "date_range": "-",
                    "path": "",
                }

            # Throttle between symbols to avoid IBKR pacing violations
            if i < len(symbols) - 1:
                log.info("  Throttling %.0fs before next symbol...", THROTTLE_SECONDS)
                time.sleep(THROTTLE_SECONDS)

    except Exception as exc:
        log.error("Fatal connection error: %s", exc)
        # Mark remaining symbols as failed
        for sym in symbols:
            if sym not in results:
                results[sym] = {"status": "CONNECTION_ERROR", "bars": 0, "date_range": "-", "path": ""}

    finally:
        log.info("")
        log.info("Disconnecting from IBKR...")
        manager.disconnect()
        log.info("Disconnected.")

    # Print summary table
    _print_summary_table(results)
    return results


def _print_summary_table(results: dict[str, dict]) -> None:
    """Log a summary table of download results."""
    log.info("")
    log.info("=" * 70)
    log.info("DOWNLOAD SUMMARY")
    log.info("=" * 70)
    log.info("  %-6s  %-8s  %6s  %s", "Symbol", "Status", "Bars", "Date Range")
    log.info("  %-6s  %-8s  %6s  %s", "------", "--------", "------", "----------")
    for sym, info in results.items():
        log.info(
            "  %-6s  %-8s  %6d  %s",
            sym, info["status"], info["bars"], info["date_range"],
        )
    log.info("=" * 70)

    ok_count = sum(1 for v in results.values() if v["status"] == "OK")
    log.info("  %d/%d symbols downloaded successfully", ok_count, len(results))
    log.info("")


# ---------------------------------------------------------------------------
# Parity check
# ---------------------------------------------------------------------------


def run_parity_check(
    databento_dir: Path,
    symbols: list[str] | None = None,
    ibkr_dir: Path | None = None,
) -> dict[str, dict]:
    """Compare IBKR-downloaded bars against Databento canary CSVs.

    For each symbol, loads the Databento canary file from
    ``<databento_dir>/<SYMBOL>/<SYMBOL>_raw.csv`` and the IBKR file from
    ``<ibkr_dir>/<SYMBOL>_ibkr_1h.csv``, finds overlapping timestamps, and
    computes close-price parity statistics.

    Parameters
    ----------
    databento_dir : Path
        Root directory containing per-symbol Databento canary CSVs.
    symbols : list[str] or None
        Symbols to check. Defaults to all 7.
    ibkr_dir : Path or None
        Directory containing IBKR sample CSVs. Defaults to
        ``$CL_DATA_ROOT/data/raw/ibkr_samples``.

    Returns
    -------
    dict[str, dict]
        Per-symbol parity statistics.
    """
    if symbols is None:
        symbols = ALL_SYMBOLS
    if ibkr_dir is None:
        ibkr_dir = get_data_root() / "raw" / "ibkr_samples"

    databento_dir = Path(databento_dir)
    parity_results: dict[str, dict] = {}

    log.info("")
    log.info("=" * 60)
    log.info("PARITY CHECK: IBKR vs Databento")
    log.info("=" * 60)
    log.info("  Databento dir: %s", databento_dir)
    log.info("  IBKR dir:      %s", ibkr_dir)
    log.info("")

    for sym in symbols:
        db_path = databento_dir / sym / f"{sym}_raw.csv"
        ibkr_path = ibkr_dir / f"{sym}_ibkr_1h.csv"

        log.info("-" * 40)
        log.info("Parity check: %s", sym)
        log.info("-" * 40)

        # Check files exist
        if not db_path.exists():
            log.warning("  Databento file not found: %s — skipping", db_path)
            parity_results[sym] = {"status": "DATABENTO_MISSING"}
            continue
        if not ibkr_path.exists():
            log.warning("  IBKR file not found: %s — skipping", ibkr_path)
            parity_results[sym] = {"status": "IBKR_MISSING"}
            continue

        try:
            df_db = _load_pipeline_csv(db_path)
            df_ibkr = _load_pipeline_csv(ibkr_path)
        except Exception as exc:
            log.error("  Error loading CSVs for %s: %s", sym, exc)
            parity_results[sym] = {"status": f"LOAD_ERROR: {exc}"}
            continue

        log.info("  Databento: %d bars (%s to %s)", len(df_db), df_db.index.min(), df_db.index.max())
        log.info("  IBKR:      %d bars (%s to %s)", len(df_ibkr), df_ibkr.index.min(), df_ibkr.index.max())

        # Find overlapping timestamps
        overlap_idx = df_db.index.intersection(df_ibkr.index)
        n_overlap = len(overlap_idx)

        if n_overlap == 0:
            log.warning("  No overlapping timestamps for %s", sym)
            parity_results[sym] = {
                "status": "NO_OVERLAP",
                "db_bars": len(df_db),
                "ibkr_bars": len(df_ibkr),
                "overlap_bars": 0,
            }
            continue

        # Compute OHLCV differences on overlapping bars
        ohlcv_stats: dict[str, dict[str, float]] = {}
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            db_vals = df_db.loc[overlap_idx, col].astype(float)
            ibkr_vals = df_ibkr.loc[overlap_idx, col].astype(float)

            abs_diff = (ibkr_vals - db_vals).abs()
            denom = db_vals.replace(0, np.nan)
            pct_diff = (abs_diff / denom * 100.0)

            ohlcv_stats[col] = {
                "mean_abs_error": float(abs_diff.mean()),
                "max_abs_error": float(abs_diff.max()),
                "mean_pct_error": float(pct_diff.mean()),
            }

        # Correlation on Close
        db_close = df_db.loc[overlap_idx, "Close"].astype(float)
        ibkr_close = df_ibkr.loc[overlap_idx, "Close"].astype(float)
        if n_overlap >= 2:
            corr = float(np.corrcoef(db_close.values, ibkr_close.values)[0, 1])
        else:
            corr = float("nan")

        # Volume correlation
        db_vol = df_db.loc[overlap_idx, "Volume"].astype(float)
        ibkr_vol = df_ibkr.loc[overlap_idx, "Volume"].astype(float)
        if n_overlap >= 2:
            vol_corr = float(np.corrcoef(db_vol.values, ibkr_vol.values)[0, 1])
        else:
            vol_corr = float("nan")

        stats = {
            "status": "OK",
            "db_bars": len(df_db),
            "ibkr_bars": len(df_ibkr),
            "overlap_bars": n_overlap,
            "close_mean_abs_error": ohlcv_stats["Close"]["mean_abs_error"],
            "close_max_abs_error": ohlcv_stats["Close"]["max_abs_error"],
            "close_mean_pct_error": ohlcv_stats["Close"]["mean_pct_error"],
            "close_correlation": corr,
            "volume_mean_pct_error": ohlcv_stats["Volume"]["mean_pct_error"],
            "volume_correlation": vol_corr,
            "ohlcv_details": ohlcv_stats,
        }
        parity_results[sym] = stats

        log.info("  Overlapping bars: %d", n_overlap)
        log.info("  --- Price Parity (Close) ---")
        log.info("    Mean |Close err|: %.6f", stats["close_mean_abs_error"])
        log.info("    Max  |Close err|: %.6f", stats["close_max_abs_error"])
        log.info("    Mean %%Close err: %.4f%%", stats["close_mean_pct_error"])
        log.info("    Correlation:      %.6f", stats["close_correlation"])
        log.info("  --- Volume Parity ---")
        log.info("    Mean %%Vol err:   %.4f%%", stats["volume_mean_pct_error"])
        log.info("    Vol correlation:  %.6f", stats["volume_correlation"])
        log.info("  --- All OHLCV ---")
        for col, col_stats in ohlcv_stats.items():
            log.info(
                "    %-7s  mean_abs=%.6f  max_abs=%.6f  mean_pct=%.4f%%",
                col, col_stats["mean_abs_error"],
                col_stats["max_abs_error"], col_stats["mean_pct_error"],
            )

        # Save per-symbol report
        report_path = ibkr_dir / f"parity_report_{sym}.txt"
        _save_parity_report(report_path, sym, stats, db_path, ibkr_path)
        log.info("  Report saved: %s", report_path)

    # Print summary table
    _print_parity_summary(parity_results)
    return parity_results


def _save_parity_report(
    path: Path,
    symbol: str,
    stats: dict,
    db_path: Path,
    ibkr_path: Path,
) -> None:
    """Write a human-readable parity report to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"Parity Report: {symbol}",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"Databento source: {db_path}",
        f"IBKR source:      {ibkr_path}",
        "",
        f"Databento bars:     {stats['db_bars']}",
        f"IBKR bars:          {stats['ibkr_bars']}",
        f"Overlapping bars:   {stats['overlap_bars']}",
        "",
        "Close Price Parity:",
        f"  Mean |error|:     {stats['close_mean_abs_error']:.6f}",
        f"  Max  |error|:     {stats['close_max_abs_error']:.6f}",
        f"  Mean % error:     {stats['close_mean_pct_error']:.4f}%",
        f"  Correlation:      {stats['close_correlation']:.6f}",
        "",
        "Volume Parity:",
        f"  Mean % error:     {stats['volume_mean_pct_error']:.4f}%",
        f"  Correlation:      {stats['volume_correlation']:.6f}",
        "",
    ]
    # Add per-column details
    if "ohlcv_details" in stats:
        lines.append("Per-Column Detail:")
        for col, cs in stats["ohlcv_details"].items():
            lines.append(
                f"  {col:7s}  mean_abs={cs['mean_abs_error']:.6f}  "
                f"max_abs={cs['max_abs_error']:.6f}  "
                f"mean_pct={cs['mean_pct_error']:.4f}%"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _print_parity_summary(results: dict[str, dict]) -> None:
    """Log a summary table of parity check results."""
    log.info("")
    log.info("=" * 80)
    log.info("PARITY SUMMARY")
    log.info("=" * 80)
    log.info(
        "  %-6s  %-14s  %8s  %12s  %12s  %10s  %10s  %10s  %10s",
        "Symbol", "Status", "Overlap", "ClsMeanErr", "ClsMaxErr", "Cls%Err", "ClsCorr", "Vol%Err", "VolCorr",
    )
    log.info(
        "  %-6s  %-14s  %8s  %12s  %12s  %10s  %10s  %10s  %10s",
        "------", "--------------", "--------", "----------", "----------", "--------", "--------", "--------", "--------",
    )
    for sym, info in results.items():
        if info["status"] == "OK":
            log.info(
                "  %-6s  %-14s  %8d  %12.6f  %12.6f  %9.4f%%  %10.6f  %9.4f%%  %10.6f",
                sym,
                info["status"],
                info["overlap_bars"],
                info["close_mean_abs_error"],
                info["close_max_abs_error"],
                info["close_mean_pct_error"],
                info["close_correlation"],
                info["volume_mean_pct_error"],
                info["volume_correlation"],
            )
        else:
            log.info("  %-6s  %-14s  %8s", sym, info["status"], "-")
    log.info("=" * 80)
    log.info("")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for CLI invocation."""
    parser = argparse.ArgumentParser(
        description="Download multi-symbol continuous contract history from IBKR "
                    "and optionally run parity checks against Databento data.",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="IBKR Gateway host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=4002,
        help="IBKR Gateway port (default: 4002 for Gateway Paper)",
    )
    parser.add_argument(
        "--client-id", type=int, default=30,
        help="IBKR client ID (default: 30, avoids conflict with live trader)",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"Days of history to download per symbol (default: {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        metavar="SYM",
        help="Space-separated symbols to download (default: all 7)",
    )
    parser.add_argument(
        "--outdir", type=str, default=None,
        help="Output directory for IBKR CSVs (default: $CL_DATA_ROOT/data/raw/ibkr_samples)",
    )
    parser.add_argument(
        "--parity-check", type=str, default=None, dest="parity_check",
        metavar="DATABENTO_DIR",
        help="Path to Databento canary data directory for comparison",
    )
    parser.add_argument(
        "--skip-download", action="store_true", dest="skip_download",
        help="Skip IBKR download, only run parity check (requires --parity-check)",
    )

    args = parser.parse_args()

    # Resolve symbols
    symbols = [s.upper() for s in args.symbols] if args.symbols else None

    # Resolve output directory
    outdir = Path(args.outdir) if args.outdir else None

    # Validate skip-download
    if args.skip_download and not args.parity_check:
        parser.error("--skip-download requires --parity-check")

    # Step 1: Download
    if not args.skip_download:
        download_all(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            days=args.days,
            symbols=symbols,
            outdir=outdir,
        )

    # Step 2: Parity check (optional)
    if args.parity_check:
        run_parity_check(
            databento_dir=Path(args.parity_check),
            symbols=symbols,
            ibkr_dir=outdir,
        )


if __name__ == "__main__":
    main()
