"""
Multi-Symbol Databento Futures Data Downloader — Convenience CLI.

Thin wrapper around DatabentoDataBuilder for common multi-symbol operations.
Supports cost estimation, canary testing, 3-year test pulls, and full history downloads.

Usage:
    python scripts/download_futures_data.py --canary
    python scripts/download_futures_data.py --symbols HG GC NG PA ES NQ --years 3
    python scripts/download_futures_data.py --symbols HG GC NG PA ES NQ --full
    python scripts/download_futures_data.py --estimate --symbols HG GC NG PA ES NQ

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("FuturesDownloader")

# ---------------------------------------------------------------------------
# Default symbols and Databento notation
# ---------------------------------------------------------------------------

DEFAULT_SYMBOLS = ["HG", "PA", "GC", "NG", "ES", "NQ"]

# Map short symbol names to Databento continuous front-month notation
DATABENTO_SYMBOL_MAP = {
    "CL": "CL.v.0",
    "HG": "HG.v.0",
    "PA": "PA.v.0",
    "GC": "GC.v.0",
    "NG": "NG.v.0",
    "ES": "ES.v.0",
    "NQ": "NQ.v.0",
}


def _resolve_output_dir() -> Path:
    """Resolve the output directory from CL_DATA_ROOT."""
    from src.data_paths import get_data_root

    return get_data_root() / "raw" / "DataBentoSample"


def _to_databento_symbols(symbols: list[str]) -> list[str]:
    """Convert short symbol names to Databento continuous notation."""
    result = []
    for sym in symbols:
        sym_upper = sym.upper()
        if sym_upper in DATABENTO_SYMBOL_MAP:
            result.append(DATABENTO_SYMBOL_MAP[sym_upper])
        elif ".v." in sym:
            # Already in Databento format
            result.append(sym)
        else:
            log.warning("Unknown symbol '%s' — trying '%s.v.0'", sym, sym_upper)
            result.append(f"{sym_upper}.v.0")
    return result


def cmd_estimate(args):
    """Estimate API credit cost without submitting."""
    from src.data.databento_data_builder import DatabentoDataBuilder

    builder = DatabentoDataBuilder()
    db_symbols = _to_databento_symbols(args.symbols)

    end = args.end or datetime.now().strftime("%Y-%m-%d")
    if args.years:
        start = (datetime.now() - timedelta(days=args.years * 365)).strftime("%Y-%m-%d")
    else:
        start = args.start or "2023-06-26"

    log.info("Estimating cost for %s from %s to %s ...", db_symbols, start, end)

    for sym in db_symbols:
        try:
            cost = builder.get_cost_estimate(
                symbols=[sym], start=start, end=end, schema="ohlcv-1h"
            )
            log.info("  %s: %s", sym, cost)
        except Exception as exc:
            log.error("  %s: FAILED — %s", sym, exc)


def cmd_canary(args):
    """Run a canary test with minimal data."""
    from src.data.databento_data_builder import DatabentoDataBuilder

    builder = DatabentoDataBuilder()
    db_symbols = _to_databento_symbols(args.symbols)
    output_dir = args.outdir or str(_resolve_output_dir())

    log.info("Running canary test: %d days for %s", args.days, db_symbols)
    results = builder.run_canary_test(
        symbols=db_symbols, days=args.days, output_dir=output_dir
    )

    # Print summary
    print("\n" + "=" * 60)
    print("CANARY TEST RESULTS".center(60))
    print("=" * 60)
    all_pass = True
    for sym, result in results.items():
        status = "PASS ✓" if result.get("pass") else "FAIL ✗"
        if not result.get("pass"):
            all_pass = False
        rows = result.get("rows", 0)
        print(f"  {sym:12s}  {status:10s}  {rows:>6,} rows")
        if result.get("error"):
            print(f"               Error: {result['error']}")
    print("=" * 60)
    print(f"Overall: {'ALL PASSED' if all_pass else 'SOME FAILED'}")
    print()

    return 0 if all_pass else 1


def cmd_submit(args):
    """Submit a batch download job."""
    from src.data.databento_data_builder import DatabentoDataBuilder

    builder = DatabentoDataBuilder()
    db_symbols = _to_databento_symbols(args.symbols)
    output_dir = args.outdir or str(_resolve_output_dir())

    end = args.end or datetime.now().strftime("%Y-%m-%d")
    if args.years:
        start = (datetime.now() - timedelta(days=args.years * 365)).strftime("%Y-%m-%d")
    elif args.full:
        # Get earliest available date
        dataset_range = builder.get_dataset_range()
        start = dataset_range["start"][:10]
    else:
        start = args.start or "2023-06-26"

    log.info("Submitting batch jobs for %s from %s to %s ...", db_symbols, start, end)

    for sym in db_symbols:
        sym_root = sym.split(".")[0]
        sym_dir = Path(output_dir) / sym_root
        sym_dir.mkdir(parents=True, exist_ok=True)

        log.info("Submitting %s ...", sym)
        try:
            files = builder.submit_and_download(
                symbols=[sym],
                start=start,
                end=end,
                output_dir=str(sym_dir),
            )
            log.info("  Downloaded %d files to %s", len(files), sym_dir)

            # Convert to pipeline format
            for f in files:
                if str(f).endswith(".csv") and "ohlcv" in str(f):
                    log.info("  Converting %s ...", f.name)
                    builder.convert_databento_csv(
                        input_path=str(f),
                        output_dir=str(sym_dir),
                        symbol=sym_root,
                    )
        except Exception as exc:
            log.error("  %s FAILED: %s", sym, exc)


def main():
    parser = argparse.ArgumentParser(
        description="Multi-Symbol Databento Futures Data Downloader"
    )
    sub = parser.add_subparsers(dest="command", help="Sub-command")

    # --- estimate ---
    p_est = sub.add_parser("estimate", help="Estimate API credit cost")
    p_est.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_SYMBOLS,
        help="Symbols to estimate (default: HG PA GC NG ES NQ)",
    )
    p_est.add_argument("--start", help="Start date (YYYY-MM-DD)")
    p_est.add_argument("--end", help="End date (YYYY-MM-DD)")
    p_est.add_argument("--years", type=int, help="Years of history")

    # --- canary ---
    p_can = sub.add_parser("canary", help="Run canary test with minimal data")
    p_can.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_SYMBOLS,
        help="Symbols to test",
    )
    p_can.add_argument("--days", type=int, default=30, help="Days of data (default: 30)")
    p_can.add_argument("--outdir", help="Output directory")

    # --- submit ---
    p_sub = sub.add_parser("submit", help="Submit batch download and convert")
    p_sub.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_SYMBOLS,
        help="Symbols to download",
    )
    p_sub.add_argument("--start", help="Start date (YYYY-MM-DD)")
    p_sub.add_argument("--end", help="End date (YYYY-MM-DD)")
    p_sub.add_argument("--years", type=int, help="Years of history (e.g., 3)")
    p_sub.add_argument("--full", action="store_true", help="Download full available history")
    p_sub.add_argument("--outdir", help="Output directory")

    args = parser.parse_args()

    if args.command == "estimate":
        cmd_estimate(args)
    elif args.command == "canary":
        sys.exit(cmd_canary(args))
    elif args.command == "submit":
        cmd_submit(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
