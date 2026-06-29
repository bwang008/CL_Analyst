"""
Download External Macro Data for set_11 Feature Engineering.

Downloads data from two free sources:
  1. FRED API — VIX, OVX, DXY, Yield Curve, Fed Funds Rate
  2. CFTC — Commitment of Traders (COT) for Crude Oil

All data is saved to data/raw/macro/ as CSV files that can be
merged into existing OHLCV datasets during DataProcessor.process().

Prerequisites:
  pip install fredapi requests pandas
  Set FRED_API_KEY in .env or as an environment variable.

Usage:
  python scripts/download_macro_data.py
  python scripts/download_macro_data.py --fred-only
  python scripts/download_macro_data.py --cot-only

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

# Ensure imports resolve
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("MacroDownloader")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

from src.data_paths import get_data_path

# Where to save everything
OUTPUT_DIR = get_data_path("raw/macro")

# CFTC COT report configuration
CFTC_BASE_URL = "https://www.cftc.gov/files/dea/history"
CFTC_CURRENT_YEAR_URL = f"{CFTC_BASE_URL}/fut_disagg_txt_2026.zip"
CFTC_YEAR_RANGE = range(2006, 2026)  # 2006-2025
CFTC_COMBINED_URL = f"{CFTC_BASE_URL}/fut_disagg_txt_hist_2006_2016.zip"

# ---------------------------------------------------------------------------
# FRED Download
# ---------------------------------------------------------------------------

def download_fred_data(api_key: str, instrument=None) -> dict[str, pd.DataFrame]:
    """Download all FRED series and return as dict of DataFrames."""
    try:
        from fredapi import Fred
    except ImportError:
        log.error(
            "fredapi not installed. Run: pip install fredapi"
        )
        sys.exit(1)

    fred = Fred(api_key=api_key)
    results: dict[str, pd.DataFrame] = {}

    from src.core.instrument_master import Instrument

    def _get_fred_series(instrument: Instrument = None) -> dict[str, str]:
        base_series = {
            "VIXCLS": "VIX",
            "DTWEXBGS": "DXY",
            "T10Y2Y": "YIELD_CURVE",
            "FEDFUNDS": "FED_FUNDS",
        }
        if instrument and instrument.volatility_index and instrument.volatility_index != "VIXCLS":
            # Strip CLS for the label if needed, or just use the index name
            label = instrument.volatility_index.replace("CLS", "")
            base_series[instrument.volatility_index] = label
        return base_series

    series_map = _get_fred_series(instrument or getattr(download_fred_data, "instrument", None))
    for series_id, label in series_map.items():
        log.info("Downloading FRED/%s (%s) ...", series_id, label)
        try:
            data = fred.get_series(series_id)
            if data is None or len(data) == 0:
                log.warning("  -> No data returned for %s", series_id)
                continue

            df = pd.DataFrame({
                "Date": data.index,
                label: data.values,
            })
            df["Date"] = pd.to_datetime(df["Date"])

            # Drop rows where value is NaN (FRED uses '.' for missing)
            df = df.dropna(subset=[label])

            log.info(
                "  -> %d rows: %s to %s",
                len(df),
                df["Date"].min().strftime("%Y-%m-%d"),
                df["Date"].max().strftime("%Y-%m-%d"),
            )
            results[label] = df

        except Exception as exc:
            log.error("  -> Failed for %s: %s", series_id, exc)

    return results


def save_fred_data(data: dict[str, pd.DataFrame], instrument=None) -> Path:
    """Save all FRED data merged into a single CSV."""
    if not data:
        log.error("No FRED data to save")
        return Path()

    # Merge all series on Date
    merged = None
    for label, df in data.items():
        if merged is None:
            merged = df
        else:
            merged = merged.merge(df, on="Date", how="outer")

    merged = merged.sort_values("Date").reset_index(drop=True)

    # Forward-fill missing values (weekends, holidays)
    for col in merged.columns:
        if col != "Date":
            merged[col] = merged[col].ffill()

    suffix = f"_{instrument.symbol.lower()}" if instrument else ""
    output_path = OUTPUT_DIR / f"fred_macro_data{suffix}.csv"
    merged.to_csv(output_path, index=False)
    log.info("Saved FRED data to %s (%d rows, %d columns)",
             output_path, len(merged), len(merged.columns))

    # Print summary
    print(f"\n{'='*60}")
    print("FRED DATA SUMMARY".center(60))
    print(f"{'='*60}")
    for col in merged.columns:
        if col == "Date":
            continue
        valid = merged[col].notna()
        first_valid = merged.loc[valid, "Date"].min()
        last_valid = merged.loc[valid, "Date"].max()
        print(f"  {col:20s}: {first_valid:%Y-%m-%d} to {last_valid:%Y-%m-%d}"
              f"  ({valid.sum():,} rows)")
    print(f"  {'File':20s}: {output_path}")
    print(f"  {'Size':20s}: {output_path.stat().st_size / 1024:.0f} KB")
    print(f"{'='*60}\n")

    return output_path


# ---------------------------------------------------------------------------
# CFTC COT Download
# ---------------------------------------------------------------------------

def _download_cot_zip(url: str, timeout: int = 120) -> pd.DataFrame | None:
    """Download a single CFTC COT ZIP file and parse the CSV inside."""
    log.info("  Downloading %s ...", url.split("/")[-1])
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200:
            log.warning("  -> HTTP %d for %s", resp.status_code, url)
            return None

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            # Each ZIP contains one CSV
            csv_names = [n for n in zf.namelist() if n.endswith(".txt")]
            if not csv_names:
                log.warning("  -> No .txt file found in ZIP")
                return None

            with zf.open(csv_names[0]) as f:
                df = pd.read_csv(f, low_memory=False)

        log.info("  -> %d total rows in file", len(df))
        return df

    except requests.exceptions.Timeout:
        log.warning("  -> Timeout downloading %s", url)
        return None
    except Exception as exc:
        log.warning("  -> Error: %s", exc)
        return None


def _filter_cot(df: pd.DataFrame, instrument) -> pd.DataFrame:
    """Filter COT data to the given instrument's rows only."""
    # The CFTC code column may be named differently across files
    code_col = None
    for candidate in ["CFTC_Contract_Market_Code", "CFTC Contract Market Code"]:
        if candidate in df.columns:
            code_col = candidate
            break

    if code_col is None:
        # Try filtering by commodity name instead
        name_col = None
        for candidate in ["Market_and_Exchange_Names", "Market and Exchange Names"]:
            if candidate in df.columns:
                name_col = candidate
                break
        if name_col:
            # simple filter, may not perfectly capture the exchange
            mask = df[name_col].str.contains(instrument.name.split()[0].upper(), case=False, na=False)
            return df[mask].copy()
        log.warning("Could not find CFTC code or name column")
        return pd.DataFrame()

    return df[df[code_col].astype(str).str.strip() == instrument.cftc_code].copy()


def _normalize_cot_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Extract and rename the columns we need from COT data."""
    # Column name mapping — CFTC uses both space-separated and underscore names
    col_map: dict[str, list[str]] = {
        "Date": ["Report_Date_as_YYYY-MM-DD", "As_of_Date_In_Form_YYYYMMDD",
                 "As of Date in Form YYYYMMDD"],
        "OI": ["Open_Interest_All", "Open Interest (All)"],
        "MM_Long": ["M_Money_Positions_Long_All", "Money Manager Longs",
                     "M_Money_Positions_Long_ALL"],
        "MM_Short": ["M_Money_Positions_Short_All", "Money Manager Shorts",
                      "M_Money_Positions_Short_ALL"],
        "Prod_Long": ["Prod_Merc_Positions_Long_All", "Producer/Merchant Longs",
                       "Prod_Merc_Positions_Long_ALL"],
        "Prod_Short": ["Prod_Merc_Positions_Short_All", "Producer/Merchant Shorts",
                        "Prod_Merc_Positions_Short_ALL"],
        "Spec_Long": ["Swap_Positions_Long_All", "Swap Dealer Longs",
                       "Swap__Positions_Long_All", "Swap_Positions_Long_ALL",
                       "Swap__Positions_Long_ALL"],
        "Spec_Short": ["Swap_Positions_Short_All", "Swap Dealer Shorts",
                        "Swap__Positions_Short_All", "Swap_Positions_Short_ALL",
                        "Swap__Positions_Short_ALL"],
    }

    result = pd.DataFrame()
    available_cols = set(df.columns)

    for target, candidates in col_map.items():
        found = False
        for c in candidates:
            if c in available_cols:
                result[target] = df[c].values
                found = True
                break
        if not found:
            log.warning("  Could not find column for '%s' (tried: %s)",
                        target, candidates)
            # Try partial match
            for col in available_cols:
                if target.lower().replace("_", " ") in col.lower().replace("_", " "):
                    result[target] = df[col].values
                    log.info("    -> Partial match: '%s'", col)
                    found = True
                    break

    if "Date" in result.columns:
        result["Date"] = pd.to_datetime(result["Date"], format="mixed", errors="coerce")

    return result


def download_cot_data(instrument) -> pd.DataFrame:
    """Download CFTC COT disaggregated futures data for CL."""
    all_frames: list[pd.DataFrame] = []

    # Strategy: try the combined historical file first, then individual years
    log.info("Downloading CFTC COT data for %s...", instrument.name)

    # Try the big historical combined file (2006-2016)
    df_hist = _download_cot_zip(CFTC_COMBINED_URL)
    if df_hist is not None and len(df_hist) > 0:
        cl_hist = _filter_cot(df_hist, instrument)
        if len(cl_hist) > 0:
            normalized = _normalize_cot_columns(cl_hist)
            all_frames.append(normalized)
            log.info("  -> Historical combined: %d rows", len(cl_hist))

    # Download individual year files (2017 onwards, or all if combined failed)
    start_year = 2017 if all_frames else 2006
    for year in range(start_year, datetime.now().year + 1):
        url = f"{CFTC_BASE_URL}/fut_disagg_txt_{year}.zip"
        df_year = _download_cot_zip(url)
        if df_year is not None and len(df_year) > 0:
            cl_year = _filter_cot(df_year, instrument)
            if len(cl_year) > 0:
                normalized = _normalize_cot_columns(cl_year)
                all_frames.append(normalized)
                log.info("  -> %d: %d rows", year, len(cl_year))

    if not all_frames:
        log.error("No COT data downloaded")
        return pd.DataFrame()

    # Combine all years
    combined = pd.concat(all_frames, ignore_index=True)
    combined = combined.dropna(subset=["Date"])
    combined = combined.drop_duplicates(subset=["Date"])
    combined = combined.sort_values("Date").reset_index(drop=True)

    # Compute derived columns
    if "MM_Long" in combined.columns and "MM_Short" in combined.columns:
        combined["MM_Net"] = combined["MM_Long"] - combined["MM_Short"]
    if "Prod_Long" in combined.columns and "Prod_Short" in combined.columns:
        combined["Prod_Net"] = combined["Prod_Long"] - combined["Prod_Short"]
    if "Spec_Long" in combined.columns and "Spec_Short" in combined.columns:
        combined["Spec_Net"] = combined["Spec_Long"] - combined["Spec_Short"]

    log.info("Combined COT: %d rows, %s to %s",
             len(combined),
             combined["Date"].min().strftime("%Y-%m-%d"),
             combined["Date"].max().strftime("%Y-%m-%d"))

    return combined


def save_cot_data(df: pd.DataFrame, instrument=None) -> Path:
    """Save COT data to CSV."""
    if df.empty:
        return Path()

    suffix = f"_{instrument.symbol.lower()}" if instrument else "_crude_oil"
    output_path = OUTPUT_DIR / f"cftc_cot{suffix}.csv"
    df.to_csv(output_path, index=False)
    log.info("Saved COT data to %s (%d rows)", output_path, len(df))

    # Print summary
    print(f"\n{'='*60}")
    print("CFTC COT DATA SUMMARY".center(60))
    print(f"{'='*60}")
    print(f"  {'Rows':20s}: {len(df):,}")
    print(f"  {'Date range':20s}: {df['Date'].min():%Y-%m-%d} to "
          f"{df['Date'].max():%Y-%m-%d}")
    print(f"  {'Columns':20s}: {list(df.columns)}")
    if "OI" in df.columns:
        print(f"  {'OI range':20s}: {df['OI'].min():,.0f} to {df['OI'].max():,.0f}")
    if "MM_Net" in df.columns:
        print(f"  {'MM Net range':20s}: {df['MM_Net'].min():,.0f} to "
              f"{df['MM_Net'].max():,.0f}")
    print(f"  {'File':20s}: {output_path}")
    print(f"  {'Size':20s}: {output_path.stat().st_size / 1024:.0f} KB")
    print(f"{'='*60}\n")

    return output_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download external macro data for CL Analyst set_11"
    )
    parser.add_argument("--fred-only", action="store_true",
                        help="Download only FRED data (VIX, DXY, etc.)")
    parser.add_argument("--cot-only", action="store_true",
                        help="Download only CFTC COT data")
    parser.add_argument("--fred-key", type=str, default=None,
                        help="FRED API key (overrides FRED_API_KEY env var)")
    parser.add_argument("--symbol", type=str, default="CL",
                        help="Symbol to fetch macro data for (determines CFTC code and Vol index)")
    args = parser.parse_args()

    from src.core.instrument_master import get_instrument
    instrument = get_instrument(args.symbol)

    # Inject instrument into global scope for downloading functions
    download_fred_data.instrument = instrument

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    do_fred = not args.cot_only
    do_cot = not args.fred_only

    print(f"\n{'='*60}")
    print("CL ANALYST — MACRO DATA DOWNLOADER".center(60))
    print(f"{'='*60}")
    print(f"  Symbol: {instrument.symbol}")
    print(f"  Output: {OUTPUT_DIR}")
    print(f"  FRED:   {'Yes' if do_fred else 'Skip'}")
    print(f"  COT:    {'Yes' if do_cot else 'Skip'}")
    print(f"{'='*60}\n")

    # --- FRED ---
    if do_fred:
        api_key = args.fred_key or os.environ.get("FRED_API_KEY", "")
        if not api_key:
            log.error(
                "FRED_API_KEY not found. Either:\n"
                "  1. Add FRED_API_KEY=your_key to .env\n"
                "  2. Set it as env var: $env:FRED_API_KEY='your_key'\n"
                "  3. Pass --fred-key your_key"
            )
            if not args.fred_only:
                log.info("Skipping FRED, continuing with COT...")
            else:
                sys.exit(1)
        else:
            fred_data = download_fred_data(api_key)
            if fred_data:
                # Combine FRED data and save
                fred_path = OUTPUT_DIR / f"fred_macro_data_{instrument.symbol.lower()}.csv"
                combined_fred = None
                for label, df in fred_data.items():
                    if combined_fred is None:
                        combined_fred = df
                    else:
                        combined_fred = pd.merge(combined_fred, df, on="Date", how="outer")
                
                if combined_fred is not None:
                    combined_fred.sort_values("Date", inplace=True)
                    combined_fred.to_csv(fred_path, index=False)
                    log.info("Saved combined FRED data to %s", fred_path)

    # --- CFTC COT ---
    if do_cot:
        cot_data = download_cot_data(instrument)
        if not cot_data.empty:
            cot_path = OUTPUT_DIR / f"cftc_cot_{instrument.symbol.lower()}.csv"
            cot_data.to_csv(cot_path)
            log.info("Saved combined COT data to %s", cot_path)

    print(f"\n{'='*60}")
    print("DOWNLOAD COMPLETE".center(60))
    print(f"{'='*60}")
    print(f"\nFiles saved to: {OUTPUT_DIR}")
    print(f"  fred_macro_data_{instrument.symbol.lower()}.csv       — Vol Index, DXY, Yield Curve, Fed Funds")
    print(f"  cftc_cot_{instrument.symbol.lower()}.csv    — COT positioning (weekly)")
    print(f"\nNext step: run regenerate_features.py with the config for {instrument.symbol}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
