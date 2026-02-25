"""
DataManager — Three-Tier data architecture for Live Execution.

Tier 1: Immutable Seed  (data/raw/cl-5m_bk.csv — read-only)
Tier 2: Warm-Start Cache (data/processed/warm_start_cache.parquet — working data)
Tier 3: Live Append      (in-memory + periodic flush to cache)

Startup sequence:
    1. If cache exists → load it
    2. Else → seed from CSV (last 60 days)
    3. Calculate gap between cache.max(DateTime) and now()
    4. Backfill missing bars from IBKR continuous contract
    5. Append, dedup, sort, validate monotonicity
    6. Return ready-to-use rolling DataFrame

Author: CL Analyst
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import pandas as pd

from src.live_execution.utils.time_utils import (
    split_duration_into_chunks,
    timedelta_to_ib_duration,
)

if TYPE_CHECKING:
    from src.live_execution.ibkr_client import IBKRConnectionManager

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_SEED_PATH = str(_PROJECT_ROOT / "data" / "raw" / "cl-5m_bk.csv")
_DEFAULT_CACHE_PATH = str(
    _PROJECT_ROOT / "data" / "processed" / "warm_start_cache.parquet"
)

# How many days of seed data to load into the initial cache.
_SEED_LOOKBACK_DAYS = 60

# 5-min bars per day (24-hour period × 12 bars/hour)
_BARS_PER_DAY = 288

# Flush the in-memory cache to disk every N appended bars.
_FLUSH_INTERVAL_BARS = 12  # every hour (12 × 5 min)

# Max single IBKR request for 5-min bars.
_MAX_IB_REQUEST_DAYS = 30


class DataManager:
    """
    Three-Tier data manager for the live execution engine.

    Manages the pipeline:
        Seed CSV → Warm-Start Parquet → IBKR Backfill → Live Append

    Thread Safety:
        Not thread-safe. Designed for single-threaded event loop usage.
    """

    def __init__(
        self,
        *,
        seed_path: str = _DEFAULT_SEED_PATH,
        cache_path: str = _DEFAULT_CACHE_PATH,
        ibkr_manager: Optional["IBKRConnectionManager"] = None,
    ) -> None:
        self.seed_path = Path(seed_path)
        self.cache_path = Path(cache_path)
        self.ibkr_manager = ibkr_manager

        self._df: Optional[pd.DataFrame] = None
        self._bars_since_flush: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initialize(self) -> pd.DataFrame:
        """
        Load or create the warm-start cache and backfill from IBKR.

        Returns:
            pd.DataFrame: Rolling OHLCV DataFrame with DateTime index,
                columns [DateTime, Open, High, Low, Close, Volume].
        """
        # Step 1: Load or create the cache
        if self.cache_path.exists():
            log.info("Loading warm-start cache from %s", self.cache_path)
            self._df = self._load_cache()
            log.info(
                "Cache loaded: %d bars, range %s → %s",
                len(self._df),
                self._df.index.min(),
                self._df.index.max(),
            )
        else:
            log.info("No cache found — seeding from %s", self.seed_path)
            self._df = self._seed_from_csv()
            self.save_cache()
            log.info(
                "Cache seeded: %d bars, range %s → %s",
                len(self._df),
                self._df.index.min(),
                self._df.index.max(),
            )

        # Step 2: Backfill any gap from IBKR
        if self.ibkr_manager is not None:
            self._backfill()
        else:
            log.warning(
                "No IBKR manager provided — skipping backfill. "
                "Cache may have stale data."
            )

        return self._df

    def append_bar(self, row: pd.Series | pd.DataFrame) -> None:
        """
        Append a single closed 5-min bar to the rolling DataFrame.

        Deduplicates by timestamp and maintains sorted order.
        Periodically flushes to the Parquet cache.

        Args:
            row: A single bar as a Series (with DateTime index entry)
                 or a single-row DataFrame.
        """
        if self._df is None:
            raise RuntimeError("DataManager not initialized. Call initialize() first.")

        if isinstance(row, pd.Series):
            row = row.to_frame().T

        # Ensure proper index
        if not isinstance(row.index, pd.DatetimeIndex):
            if "DateTime" in row.columns:
                row = row.set_index(
                    pd.DatetimeIndex(row["DateTime"]), drop=False
                )
                row.index.name = "DateTime"

        self._df = pd.concat([self._df, row])
        self._dedup_and_sort()

        self._bars_since_flush += 1
        if self._bars_since_flush >= _FLUSH_INTERVAL_BARS:
            self.save_cache()
            self._bars_since_flush = 0

    def save_cache(self) -> None:
        """
        Persist the current DataFrame to the Parquet cache file.

        Uses atomic write (temp file + rename) to prevent corruption
        from mid-write crashes.
        """
        if self._df is None or len(self._df) == 0:
            log.warning("Cannot save empty cache — skipping.")
            return

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to temp file, then rename
        fd, tmp_path = tempfile.mkstemp(
            suffix=".parquet",
            dir=str(self.cache_path.parent),
        )
        os.close(fd)

        try:
            # Reset index so DateTime becomes a column for clean storage
            save_df = self._df.copy()
            if save_df.index.name == "DateTime":
                save_df = save_df.reset_index(drop=True)

            save_df.to_parquet(tmp_path, index=False, engine="pyarrow")
            # Atomic rename (on Windows, need to remove target first)
            if self.cache_path.exists():
                self.cache_path.unlink()
            Path(tmp_path).rename(self.cache_path)
            log.info(
                "Cache saved: %d bars → %s", len(self._df), self.cache_path
            )
        except Exception:
            # Clean up temp file on error
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
            raise

    @property
    def dataframe(self) -> Optional[pd.DataFrame]:
        """Access the current rolling DataFrame (read-only reference)."""
        return self._df

    @property
    def last_timestamp(self) -> Optional[pd.Timestamp]:
        """The latest timestamp in the current DataFrame."""
        if self._df is not None and len(self._df) > 0:
            return self._df.index.max()
        return None

    # ------------------------------------------------------------------
    # Private: Seed loading
    # ------------------------------------------------------------------

    def _seed_from_csv(self) -> pd.DataFrame:
        """
        Load the last `_SEED_LOOKBACK_DAYS` of data from the seed CSV.

        The seed file is semicolon-delimited with no header:
            Date;Time;Open;High;Low;Close;Volume
            20/11/2008;00:00;181.588;...

        Returns:
            pd.DataFrame with DateTime index and OHLCV columns.
        """
        if not self.seed_path.exists():
            raise FileNotFoundError(
                f"Seed file not found: {self.seed_path}"
            )

        log.info("Reading seed file: %s", self.seed_path)

        df = pd.read_csv(
            self.seed_path,
            sep=";",
            header=None,
            names=["Date", "Time", "Open", "High", "Low", "Close", "Volume"],
        )

        # Parse DateTime
        df["DateTime"] = pd.to_datetime(
            df["Date"] + " " + df["Time"],
            format="%d/%m/%Y %H:%M",
        )
        df = df[["DateTime", "Open", "High", "Low", "Close", "Volume"]]
        df = df.set_index("DateTime", drop=False)
        df.index.name = "DateTime"
        df = df.sort_index()

        # Take the last N days
        cutoff = df.index.max() - timedelta(days=_SEED_LOOKBACK_DAYS)
        df = df.loc[df.index >= cutoff]

        log.info(
            "Seed: extracted %d bars (last %d days) from %s → %s",
            len(df),
            _SEED_LOOKBACK_DAYS,
            df.index.min(),
            df.index.max(),
        )

        return df

    # ------------------------------------------------------------------
    # Private: Cache I/O
    # ------------------------------------------------------------------

    def _load_cache(self) -> pd.DataFrame:
        """Load the Parquet warm-start cache."""
        df = pd.read_parquet(self.cache_path, engine="pyarrow")

        # Ensure DateTime column exists and is the index
        if "DateTime" not in df.columns:
            raise ValueError(
                "Cache file missing 'DateTime' column: "
                f"{self.cache_path}"
            )

        df["DateTime"] = pd.to_datetime(df["DateTime"])
        df = df.set_index("DateTime", drop=False)
        df.index.name = "DateTime"
        df = df.sort_index()

        return df

    # ------------------------------------------------------------------
    # Private: IBKR backfill
    # ------------------------------------------------------------------

    def _backfill(self) -> None:
        """
        Fetch missing bars from IBKR to bridge the gap between the
        cache's last timestamp and now.
        """
        if self._df is None or len(self._df) == 0:
            log.warning("Cannot backfill — no data in cache.")
            return

        last_ts = self._df.index.max()
        now = pd.Timestamp.now()
        gap = now - last_ts

        log.info(
            "Backfill gap: %s → %s (%.1f hours)",
            last_ts, now, gap.total_seconds() / 3600,
        )

        # If gap is less than 10 minutes, no backfill needed
        if gap.total_seconds() < 600:
            log.info("Gap < 10 minutes — no backfill needed.")
            return

        # Convert gap to IBKR duration chunks
        gap_td = timedelta(seconds=gap.total_seconds())
        chunks = split_duration_into_chunks(
            gap_td, max_chunk_days=_MAX_IB_REQUEST_DAYS
        )

        log.info(
            "Backfill: %d chunk(s) needed: %s",
            len(chunks), chunks,
        )

        from src.live_execution.ibkr_client import build_cl_contract

        contract = build_cl_contract(continuous=True)
        contract = self.ibkr_manager.qualify_contract(contract)

        total_stitched = 0
        for i, duration_str in enumerate(chunks):
            log.info(
                "Backfill chunk %d/%d: requesting %s ...",
                i + 1, len(chunks), duration_str,
            )

            bars = self.ibkr_manager._request_historical_data(
                contract=contract,
                duration_str=duration_str,
                bar_size="5 mins",
                what_to_show="TRADES",
                use_rth=False,
                end_datetime="",
                max_retries=5,
                backoff_seconds=2.0,
                throttle_seconds=0.5,
            )

            if not bars:
                log.warning("Backfill chunk %d: no bars returned.", i + 1)
                continue

            from src.live_execution.ibkr_client import ib_bars_to_dataframe

            chunk_df = ib_bars_to_dataframe(bars)
            n_before = len(self._df)
            self._df = pd.concat([self._df, chunk_df])
            self._dedup_and_sort()
            n_new = len(self._df) - n_before
            total_stitched += n_new
            log.info(
                "Backfill chunk %d: stitched %d new bars "
                "(total now: %d)",
                i + 1, n_new, len(self._df),
            )

        if total_stitched > 0:
            log.info(
                "Backfill complete: stitched %d bars. "
                "Range: %s → %s",
                total_stitched,
                self._df.index.min(),
                self._df.index.max(),
            )
            self.save_cache()
        else:
            log.info("Backfill: no new bars stitched (cache was up-to-date).")

    # ------------------------------------------------------------------
    # Private: Dedup & validation
    # ------------------------------------------------------------------

    def _dedup_and_sort(self) -> None:
        """
        Remove duplicate timestamps and ensure monotonic ascending order.
        """
        if self._df is None:
            return

        n_before = len(self._df)

        # If DateTime is both index and column, deduplicate via the index
        self._df = self._df[~self._df.index.duplicated(keep="last")]
        self._df = self._df.sort_index()

        n_removed = n_before - len(self._df)
        if n_removed > 0:
            log.debug("Dedup: removed %d duplicate bars.", n_removed)

        # Validate monotonicity
        if not self._df.index.is_monotonic_increasing:
            log.warning(
                "DateTime index is NOT monotonic after dedup+sort. "
                "Forcing sort."
            )
            self._df = self._df.sort_index()
