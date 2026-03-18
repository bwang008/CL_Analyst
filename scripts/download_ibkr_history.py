"""
Download CL Continuous Contract History from IBKR.

Downloads 5-minute bars for the CL continuous futures contract from
Interactive Brokers, going as far back as IBKR allows (~2 years for
5-min bars).

IBKR API Quirk: ContFuture does not allow setting endDateTime
(Error 10339). To work around this, we use two strategies:

  Strategy A: Request ContFuture with empty endDateTime and maximum
              duration. This returns the most recent N days of data.

  Strategy B: If Strategy A doesn't go back far enough, download
              individual expired contracts (each Future with specific
              expiry month) and stitch them together with ratio
              back-adjustment matching IBKR's methodology.

Usage:
    python scripts/download_ibkr_history.py
    python scripts/download_ibkr_history.py --days 365
    python scripts/download_ibkr_history.py --port 7497 --client-id 25

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.live_execution.ibkr_client import (
    IBKRConnectionManager,
    build_cl_contract,
    ib_bars_to_dataframe,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("IBKRDownloader")

THROTTLE_SECONDS = 12.0     # Conservative: 12s between requests
PACING_BACKOFF = 120.0      # Wait 2 minutes on pacing violation
MAX_RETRIES = 8             # Retry per chunk
DEFAULT_DAYS = 730          # 2 years

# Max duration per request for 5-min bars on ContFuture
# IBKR docs say max is "2 Y" but ContFuture may be more limited
CONTFUTURE_DURATIONS = [
    "2 Y",   # Try 2 years first
    "1 Y",   # Fall back to 1 year
    "6 M",   # 6 months
    "3 M",   # 3 months
    "1 M",   # 1 month
]

# CL futures expiry months (all 12 months for CL)
# Format: YYYYMM — we generate these dynamically
CL_EXPIRY_MONTHS = list(range(1, 13))

# Default output path
from src.data_paths import get_data_root
DEFAULT_OUTPUT = str(get_data_root() / "processed" / "cl_ibkr_continuous.parquet")


def _try_contfuture_download(
    manager: IBKRConnectionManager,
    target_days: int,
) -> pd.DataFrame:
    """
    Strategy A: Download using ContFuture with empty endDateTime.

    Tries progressively smaller duration strings until one succeeds.
    ContFuture doesn't allow explicit endDateTime, so we can only
    request "the most recent N days/months/years."
    """
    contract = build_cl_contract(continuous=True)
    contract = manager.qualify_contract(contract)
    log.info("Qualified ContFuture: %s (conId=%s)", contract.localSymbol, contract.conId)

    for duration_str in CONTFUTURE_DURATIONS:
        log.info("Trying ContFuture with duration=%s ...", duration_str)  

        try:
            bars = manager._request_historical_data(
                contract=contract,
                duration_str=duration_str,
                bar_size="5 mins",
                what_to_show="TRADES",
                use_rth=False,
                end_datetime="",  # MUST be empty for ContFuture
                max_retries=MAX_RETRIES,
                backoff_seconds=5.0,
                throttle_seconds=1.0,
            )

            if bars:
                df = ib_bars_to_dataframe(bars)
                log.info(
                    "  -> SUCCESS: %d bars, %s to %s (%d days)",
                    len(df),
                    df.index.min(),
                    df.index.max(),
                    (df.index.max() - df.index.min()).days,
                )
                return df
            else:
                log.warning("  -> No bars returned for %s", duration_str)

        except Exception as exc:
            err_str = str(exc).lower()
            log.warning("  -> Failed for %s: %s", duration_str, exc)

            if "pacing" in err_str:
                log.info("  Pacing violation — waiting %.0fs ...", PACING_BACKOFF)
                time.sleep(PACING_BACKOFF)
            elif "invalid step" in err_str or "no data" in err_str:
                log.info("  Duration too large — trying smaller...")
            else:
                log.info("  Waiting before retry...")

        time.sleep(THROTTLE_SECONDS)

    log.warning("ContFuture download: all durations failed")
    return pd.DataFrame()


def _generate_contract_months(years_back: int = 2) -> list[str]:
    """Generate YYYYMM strings for CL contract months going back N years."""
    now = datetime.now()
    months = []
    for y in range(now.year, now.year - years_back - 1, -1):
        for m in range(12, 0, -1):
            ym = f"{y}{m:02d}"
            # Skip future months
            if y == now.year and m > now.month:
                continue
            months.append(ym)
    return months


def _download_individual_contracts(
    manager: IBKRConnectionManager,
    target_days: int,
) -> pd.DataFrame:
    """
    Strategy B: Download individual expired CL contracts and stitch.

    Downloads each contract month separately (e.g., CLJ6, CLK6, CLG6...)
    going backwards, then stitches them using ratio adjustment to match
    IBKR's continuous contract methodology.
    """
    from ib_insync import Future

    contract_months = _generate_contract_months(years_back=3)
    all_data: list[tuple[str, pd.DataFrame]] = []
    target_date = datetime.now() - timedelta(days=target_days)

    log.info("Strategy B: downloading individual contracts...")
    log.info("  Target start date: %s", target_date.strftime("%Y-%m-%d"))

    for ym in contract_months:
        log.info("  Trying CL %s ...", ym)

        contract = Future(
            symbol="CL",
            lastTradeDateOrContractMonth=ym,
            exchange="NYMEX",
            currency="USD",
            includeExpired=True,
        )

        try:
            qualified = manager.ib.qualifyContracts(contract)
            if not qualified:
                log.warning("    Could not qualify CL %s — skipping", ym)
                time.sleep(2)
                continue
            contract = qualified[0]
        except Exception as exc:
            log.warning("    Qualify failed for CL %s: %s", ym, exc)
            time.sleep(2)
            continue

        # Request max duration for this individual contract
        try:
            bars = manager._request_historical_data(
                contract=contract,
                duration_str="60 D",  # Individual contracts: ~2 months of data each
                bar_size="5 mins",
                what_to_show="TRADES",
                use_rth=False,
                end_datetime="",
                max_retries=5,
                backoff_seconds=5.0,
                throttle_seconds=1.0,
            )

            if bars:
                df = ib_bars_to_dataframe(bars)
                log.info(
                    "    -> %d bars: %s to %s (CL %s)",
                    len(df), df.index.min(), df.index.max(), ym,
                )
                all_data.append((ym, df))

                # Check if we've gone back far enough
                if df.index.min().to_pydatetime().replace(tzinfo=None) < target_date:
                    log.info("  Reached target date — stopping download")
                    break
            else:
                log.warning("    -> No bars for CL %s", ym)

        except Exception as exc:
            log.warning("    -> Failed for CL %s: %s", ym, exc)
            if "pacing" in str(exc).lower():
                log.info("  Pacing — waiting %.0fs ...", PACING_BACKOFF)
                time.sleep(PACING_BACKOFF)

        time.sleep(THROTTLE_SECONDS)

    if not all_data:
        log.error("No individual contract data downloaded")
        return pd.DataFrame()

    # Stitch contracts together with ratio adjustment
    log.info("Stitching %d contracts with ratio adjustment...", len(all_data))
    return _ratio_adjust_contracts(all_data)


def _ratio_adjust_contracts(
    contracts_data: list[tuple[str, pd.DataFrame]],
) -> pd.DataFrame:
    """
    Stitch individual CL contracts using ratio back-adjustment.

    Matches IBKR's methodology:
    1. Start from the most recent (front) contract
    2. At each rollover point, compute ratio = front_close / back_close
    3. Multiply all historical data by this ratio
    """
    # Sort by contract month (most recent first)
    contracts_data.sort(key=lambda x: x[0], reverse=True)

    if len(contracts_data) == 1:
        return contracts_data[0][1]

    # Start with the most recent contract (unadjusted)
    result = contracts_data[0][1].copy()
    log.info("  Base contract: CL %s (%d bars)", contracts_data[0][0], len(result))

    for i in range(1, len(contracts_data)):
        current_month, current_df = contracts_data[i]
        prev_df = contracts_data[i - 1][1]

        # Find overlap period
        overlap = current_df.index.intersection(prev_df.index)

        if len(overlap) == 0:
            log.warning(
                "  No overlap between CL %s and CL %s — using gap join",
                contracts_data[i - 1][0], current_month,
            )
            # Just prepend with no adjustment (not ideal but better than nothing)
            non_overlap = current_df[current_df.index < prev_df.index.min()]
            if len(non_overlap) > 0:
                result = pd.concat([non_overlap, result])
            continue

        # Use the last overlapping timestamp for the ratio
        roll_ts = overlap[-1]
        front_close = float(prev_df.loc[roll_ts, "Close"])
        back_close = float(current_df.loc[roll_ts, "Close"])

        if back_close == 0:
            log.warning("  Zero close price at roll — skipping CL %s", current_month)
            continue

        ratio = front_close / back_close
        log.info(
            "  Roll CL %s: ratio=%.6f (front=$%.2f / back=$%.2f at %s)",
            current_month, ratio, front_close, back_close, roll_ts,
        )

        # Adjust the back contract and prepend non-overlapping bars
        adjusted = current_df.copy()
        for col in ["Open", "High", "Low", "Close"]:
            adjusted[col] = adjusted[col] * ratio

        non_overlap = adjusted[adjusted.index < prev_df.index.min()]
        if len(non_overlap) > 0:
            result = pd.concat([non_overlap, result])

    result = result[~result.index.duplicated(keep='last')].sort_index()
    log.info("  Stitched result: %d bars, %s to %s",
             len(result), result.index.min(), result.index.max())
    return result


def download_history(
    *,
    host: str = "127.0.0.1",
    port: int = 4002,
    client_id: int = 25,
    days_back: int = DEFAULT_DAYS,
    output_path: str = DEFAULT_OUTPUT,
) -> pd.DataFrame:
    """Download CL continuous contract 5-min bars from IBKR."""
    log.info("=" * 60)
    log.info("IBKR CL History Downloader")
    log.info("=" * 60)
    log.info("  Target: %d days back", days_back)
    log.info("  Output: %s", output_path)
    log.info("  Host: %s:%d  ClientID: %d", host, port, client_id)
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

        # Strategy A: Try ContFuture (simplest, preserves IBKR's own adjustment)
        log.info("")
        log.info("=" * 40)
        log.info("Strategy A: ContFuture download")
        log.info("=" * 40)
        df = _try_contfuture_download(manager, days_back)

        if len(df) > 0:
            days_covered = (df.index.max() - df.index.min()).days
            log.info("ContFuture returned %d days of data", days_covered)

            if days_covered < days_back * 0.8:
                # Got some data but not enough — try Strategy B for more
                log.info(
                    "Only got %d/%d days — trying Strategy B for more...",
                    days_covered, days_back,
                )
                df_b = _download_individual_contracts(manager, days_back)
                if len(df_b) > 0 and (df_b.index.max() - df_b.index.min()).days > days_covered:
                    log.info("Strategy B produced more data — using it")
                    df = df_b
                else:
                    log.info("Strategy A data is better — keeping it")
        else:
            # ContFuture failed entirely — fall back to Strategy B
            log.info("")
            log.info("=" * 40)
            log.info("Strategy B: Individual contract download")
            log.info("=" * 40)
            df = _download_individual_contracts(manager, days_back)

        if len(df) == 0:
            log.error("No data downloaded from either strategy.")
            log.error("Check:")
            log.error("  1. Is IB Gateway/TWS running and logged in?")
            log.error("  2. Do you have market data subscriptions for CL?")
            log.error("  3. Try again when markets are open")
            return pd.DataFrame()

        # Dedup and sort
        df = df[~df.index.duplicated(keep='last')].sort_index()

        # Save to Parquet
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        save_df = df.reset_index(drop=True)
        save_df.to_parquet(str(output), index=False, engine="pyarrow")
        log.info("Saved to: %s", output)

        # Print summary
        print()
        print("=" * 60)
        print("DOWNLOAD COMPLETE".center(60))
        print("=" * 60)
        print(f"  Bars:       {len(df):,}")
        print(f"  Date range: {df.index.min()} to {df.index.max()}")
        print(f"  Days:       {(df.index.max() - df.index.min()).days}")
        print(f"  File:       {output}")
        print(f"  File size:  {output.stat().st_size / 1024 / 1024:.1f} MB")
        print()
        print("  Price check (sanity):")
        print(f"    First bar close: ${df.iloc[0]['Close']:.2f}")
        print(f"    Last bar close:  ${df.iloc[-1]['Close']:.2f}")
        print()
        print("  Next steps:")
        print("    1. Process through DataProcessor")
        print("    2. Retrain models on IBKR-native data")
        print("    3. Run parity validation")
        print("=" * 60)

        return df

    finally:
        log.info("Disconnecting from IBKR...")
        manager.disconnect()
        log.info("Done.")


def main():
    parser = argparse.ArgumentParser(
        description="Download CL continuous contract history from IBKR"
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"Days of history to download (default: {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help=f"Output Parquet file path",
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
        "--client-id", type=int, default=25,
        help="IBKR client ID (default: 25)",
    )
    args = parser.parse_args()

    download_history(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        days_back=args.days,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
