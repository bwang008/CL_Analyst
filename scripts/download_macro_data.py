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
from abc import ABC, abstractmethod
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

# FRED schema contract — imported (not restated) so this producer and the
# consumer (macro_features) agree on the required column set by construction.
# macro_features imports this script only lazily, so this top-level import is
# cycle-free (verified by the test suite).
from src.features.macro_features import _FRED_BASE_COLS, vol_label_for

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

    import time

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

    series_map = _get_fred_series(instrument)
    failed: list[str] = []
    max_attempts = 3
    for series_id, label in series_map.items():
        log.info("Downloading FRED/%s (%s) ...", series_id, label)

        # Bounded retry: the observed failure mode is a transient outage, so a
        # series that succeeds on a later attempt is fine. Both an empty/None
        # response and a raised exception count as a failed attempt.
        data = None
        for attempt in range(1, max_attempts + 1):
            try:
                fetched = fred.get_series(series_id)
                if fetched is None or len(fetched) == 0:
                    log.warning("  -> No data returned for %s (attempt %d/%d)",
                                series_id, attempt, max_attempts)
                else:
                    data = fetched
                    break
            except Exception as exc:
                log.warning("  -> Fetch failed for %s (attempt %d/%d): %s",
                            series_id, attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(0.5 * attempt)  # short backoff between retries

        if data is None:
            log.error("  -> Giving up on FRED/%s after %d attempts",
                      series_id, max_attempts)
            failed.append(series_id)
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

    if failed:
        # No partial dict may escape: a partial file would silently overwrite a
        # known-good one downstream (save_fred_data / the live refresh path).
        raise RuntimeError(
            "FRED download failed for series: "
            + ", ".join(failed)
            + f" (after {max_attempts} attempts each). "
            "Refusing to return a partial macro dataset."
        )

    return results


def save_fred_data(data: dict[str, pd.DataFrame], instrument=None) -> Path:
    """Save all FRED data merged into a single CSV.

    Validates the assembled frame against the required column contract before
    writing, and writes atomically (temp file + os.replace). A partial/invalid
    frame therefore raises instead of overwriting a known-good file.
    """
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

    # --- Schema guard: never overwrite a known-good file with a partial one.
    # Required columns are derived from the SAME contract the consumer reads
    # (_FRED_BASE_COLS + vol_label_for), so producer and consumer agree by
    # construction. With no instrument, validate against the base columns only.
    required_cols = set(_FRED_BASE_COLS)
    if instrument is not None:
        required_cols.add(vol_label_for(instrument))
    missing = sorted(required_cols - set(merged.columns))
    if missing:
        label = f"_{instrument.symbol.lower()}" if instrument else ""
        raise ValueError(
            f"FRED frame is missing required column(s) {missing}; refusing to "
            f"overwrite fred_macro_data{label}.csv (would corrupt a known-good file)."
        )

    suffix = f"_{instrument.symbol.lower()}" if instrument else ""
    output_path = OUTPUT_DIR / f"fred_macro_data{suffix}.csv"

    # Atomic write: write a temp file in the same directory, then os.replace it
    # onto the destination. A failed write can never leave a corrupt/partial
    # destination behind, and no temp file survives success or failure.
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    try:
        merged.to_csv(tmp_path, index=False)
        os.replace(tmp_path, output_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

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
        result["Date"] = _parse_cot_date(df).values

    return result


def _parse_cot_date(df: pd.DataFrame) -> pd.Series:
    """Parse the CFTC report date column, tolerant of the naming/format
    variants seen across report families and years.

    ISO ``Report_Date_as_YYYY-MM-DD`` (modern files) is preferred; older
    files expose ``As_of_Date_In_Form_YYYYMMDD`` (8-digit) or
    ``As_of_Date_In_Form_YYMMDD`` (6-digit).
    """
    for c in ["Report_Date_as_YYYY-MM-DD", "Report Date as YYYY-MM-DD"]:
        if c in df.columns:
            # No explicit format: pandas auto-detects ISO reliably. (Avoid
            # format="mixed", which is unsupported on pandas 1.5.x and coerces
            # every value to NaT there.)
            return pd.to_datetime(df[c], errors="coerce")
    for c in ["As_of_Date_In_Form_YYYYMMDD", "As of Date in Form YYYYMMDD"]:
        if c in df.columns:
            return pd.to_datetime(df[c].astype(str).str.zfill(8), format="%Y%m%d", errors="coerce")
    for c in ["As_of_Date_In_Form_YYMMDD", "As of Date in Form YYMMDD"]:
        if c in df.columns:
            return pd.to_datetime(df[c].astype(str).str.zfill(6), format="%y%m%d", errors="coerce")
    log.warning("  Could not find a recognizable COT date column")
    return pd.Series([pd.NaT] * len(df))


def _normalize_tff_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize CFTC *Traders in Financial Futures* (TFF) columns onto the
    canonical commodity-role schema used by the rest of the pipeline.

    Approved category mapping (see COT scope review):
        MM   (speculative funds)      <- Leveraged Funds
        Prod (real-money institutional) <- Asset Manager / Institutional
        Spec (dealer / intermediary)  <- Dealer / Intermediary

    This intentionally reuses the MM/Prod/Spec column names so financial
    symbols (ES, NQ) produce the *same* COT_* feature names as commodities.
    """
    col_map: dict[str, list[str]] = {
        "OI": ["Open_Interest_All", "Open Interest (All)"],
        # MM <- Leveraged Funds
        "MM_Long": ["Lev_Money_Positions_Long_All",
                    "Leveraged Funds Positions Long (All)"],
        "MM_Short": ["Lev_Money_Positions_Short_All",
                     "Leveraged Funds Positions Short (All)"],
        # Prod <- Asset Manager / Institutional
        "Prod_Long": ["Asset_Mgr_Positions_Long_All",
                      "Asset Manager Positions Long (All)"],
        "Prod_Short": ["Asset_Mgr_Positions_Short_All",
                       "Asset Manager Positions Short (All)"],
        # Spec <- Dealer / Intermediary
        "Spec_Long": ["Dealer_Positions_Long_All",
                      "Dealer Positions Long (All)"],
        "Spec_Short": ["Dealer_Positions_Short_All",
                       "Dealer Positions Short (All)"],
    }

    result = pd.DataFrame()
    available_cols = set(df.columns)
    for target, candidates in col_map.items():
        for c in candidates:
            if c in available_cols:
                result[target] = df[c].values
                break
        else:
            log.warning("  [TFF] Could not find column for '%s' (tried: %s)",
                        target, candidates)

    result["Date"] = _parse_cot_date(df).values
    return result


# ---------------------------------------------------------------------------
# COT report adapters — one CFTC report family per adapter, all emitting the
# canonical schema: Date, OI, MM_Long/Short, Prod_Long/Short, Spec_Long/Short.
# ---------------------------------------------------------------------------

class CotReportAdapter(ABC):
    """Selects the CFTC report source files and normalizes them to the
    canonical trader-role schema consumed by ``MacroFeatureEngine``."""

    #: URL of a combined multi-year historical file, or ``None`` if the
    #: report family has no such file (fetch per-year from ``start_year``).
    combined_url: str | None = None
    #: Earliest year of per-year files to attempt.
    start_year: int = 2006

    @abstractmethod
    def year_url(self, year: int) -> str:
        """URL of the per-year zip for ``year``."""

    @abstractmethod
    def normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        """Map raw report columns to the canonical schema."""


class DisaggregatedAdapter(CotReportAdapter):
    """Physical-commodity futures (CFTC *Disaggregated* report).

    Roles: Money Manager (MM), Producer/Merchant (Prod), Swap Dealer (Spec).
    Used for CL, NG, HG, GC, PA.
    """

    combined_url = f"{CFTC_BASE_URL}/fut_disagg_txt_hist_2006_2016.zip"
    start_year = 2006

    def year_url(self, year: int) -> str:
        return f"{CFTC_BASE_URL}/fut_disagg_txt_{year}.zip"

    def normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        return _normalize_cot_columns(df)


class TffAdapter(CotReportAdapter):
    """Financial futures (CFTC *Traders in Financial Futures* report).

    Roles are remapped onto the canonical MM/Prod/Spec names per the approved
    mapping in ``_normalize_tff_columns``. Used for equity-index symbols
    (ES, NQ). TFF history begins in 2010 (no pre-2010 combined file).
    """

    combined_url = None
    start_year = 2010

    def year_url(self, year: int) -> str:
        return f"{CFTC_BASE_URL}/fut_fin_txt_{year}.zip"

    def normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        return _normalize_tff_columns(df)


COT_REPORT_ADAPTERS: dict[str, type[CotReportAdapter]] = {
    "disaggregated": DisaggregatedAdapter,
    "tff": TffAdapter,
}

# Which CFTC report family each symbol is reported under. An unmapped symbol
# must raise (no silent default) so new instruments are classified explicitly.
COT_REPORT_BY_SYMBOL: dict[str, str] = {
    "CL": "disaggregated",
    "NG": "disaggregated",
    "HG": "disaggregated",
    "GC": "disaggregated",
    "PA": "disaggregated",
    "ES": "tff",
    "NQ": "tff",
    "ZC": "disaggregated",
    "ZS": "disaggregated",
    "SI": "disaggregated",
}


def get_cot_adapter(instrument) -> CotReportAdapter:
    """Return the COT report adapter for ``instrument``.

    Raises ``ValueError`` if the symbol has no registered report type — we do
    not guess, because commodity vs. financial reports have incompatible
    trader categories.
    """
    symbol = str(instrument.symbol).upper()
    report_type = COT_REPORT_BY_SYMBOL.get(symbol)
    if report_type is None:
        raise ValueError(
            f"No CFTC COT report type registered for symbol '{symbol}'. "
            f"Add it to COT_REPORT_BY_SYMBOL as one of "
            f"{sorted(COT_REPORT_ADAPTERS)}."
        )
    return COT_REPORT_ADAPTERS[report_type]()


def download_cot_data(instrument) -> pd.DataFrame:
    """Download CFTC COT data for ``instrument`` via its report adapter.

    Commodity symbols use the Disaggregated report; financial symbols use the
    TFF report. Both are normalized to the same canonical schema.
    """
    adapter = get_cot_adapter(instrument)
    all_frames: list[pd.DataFrame] = []

    # Strategy: try the combined historical file first (if any), then years.
    log.info("Downloading CFTC COT data for %s (report=%s)...",
             instrument.name, type(adapter).__name__)

    if adapter.combined_url:
        df_hist = _download_cot_zip(adapter.combined_url)
        if df_hist is not None and len(df_hist) > 0:
            hist = _filter_cot(df_hist, instrument)
            if len(hist) > 0:
                all_frames.append(adapter.normalize(hist))
                log.info("  -> Historical combined: %d rows", len(hist))

    # Download individual year files (2017 onwards if combined succeeded,
    # else from the adapter's earliest available year).
    start_year = 2017 if all_frames else adapter.start_year
    for year in range(start_year, datetime.now().year + 1):
        df_year = _download_cot_zip(adapter.year_url(year))
        if df_year is not None and len(df_year) > 0:
            yr = _filter_cot(df_year, instrument)
            if len(yr) > 0:
                all_frames.append(adapter.normalize(yr))
                log.info("  -> %d: %d rows", year, len(yr))

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
            fred_data = download_fred_data(api_key, instrument=instrument)
            save_fred_data(fred_data, instrument=instrument)

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
