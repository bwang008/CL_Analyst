"""
DataManager — Three-Tier data architecture for Live Execution.

Tier 1: Immutable Seed  (data/raw/cl-5m_bk.csv — read-only)
Tier 2: Warm-Start Cache (data/processed/warm_start_cache.parquet — working data)
Tier 3: Live Append      (in-memory + periodic flush to cache)
Tier 4: Master Training Ledger (data/processed/cl_continuous_master.parquet)

Startup sequence:
    1. If cache exists → load it
    2. Else → seed from CSV (last 60 days)
    3. Detect rollover → if rolled, validate cache → rebuild if stale
    4. Backfill missing bars from IBKR continuous contract
    5. Append, dedup, sort, validate monotonicity
    6. Update master training ledger with new bars
    7. Return ready-to-use rolling DataFrame

Author: CL Analyst
"""

from __future__ import annotations

import json
import logging
import os
import shutil
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

# Resolve data paths via centralized helper (CL_DATA_ROOT primary, repo-local fallback)
from src.data_paths import get_data_path, get_data_root, mirror_file as _dp_mirror

_DEFAULT_SEED_PATH = str(get_data_path("raw/cl-5m_bk.csv"))
_DEFAULT_CACHE_PATH = str(get_data_root() / "processed" / "warm_start_cache.parquet")
_DEFAULT_MASTER_LEDGER_PATH = str(
    get_data_root() / "processed" / "cl_continuous_master.parquet"
)
_ROLL_METADATA_PATH = str(
    get_data_root() / "processed" / ".roll_metadata.json"
)

# How many days of seed data to load into the initial cache.
# Minimum requirements (from AlphaFactory feature lookback windows):
#   - MACRO_3M: 2160 hourly bars = 90 days
#   - VOL_ROC_10080: needs 2×10080 bars = ~70 days
#   - VOL_VOLVOL_10080: needs rolling(10080) of VOL_PARK(10080) = ~70 days
# Set to 150 days for ample margin. Cost is negligible (~43K bars from CSV).
_SEED_LOOKBACK_DAYS = 150

# 5-min bars per day (24-hour period × 12 bars/hour)
_BARS_PER_DAY = 288

# Flush the in-memory cache to disk every N appended bars.
_FLUSH_INTERVAL_BARS = 12  # every hour (12 × 5 min)

# Max single IBKR request for 5-min bars.
_MAX_IB_REQUEST_DAYS = 30

# Number of bars to sample when validating cache after rollover.
_ROLL_VALIDATION_BARS = 50

# Maximum price difference ($) before considering cache stale.
_ROLL_PRICE_TOLERANCE = 0.01

# Directory in the repo for cache backups (tracked by git).
_CACHE_BACKUP_DIR = _PROJECT_ROOT / "data" / "cache_backups"


def _mirror_to_root(src_path: Path, project_root: Path) -> None:
    """Copy a file to the other data location (shared ↔ repo-local)."""
    try:
        _dp_mirror(src_path)
        log.info("Mirrored %s", src_path.name)
    except (ValueError, OSError) as exc:
        log.warning("Could not mirror to root: %s", exc)


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
        master_ledger_path: str = _DEFAULT_MASTER_LEDGER_PATH,
        ibkr_manager: Optional["IBKRConnectionManager"] = None,
        front_month_id: Optional[str] = None,
        bar_size: str = "5 mins",
        bars_per_day: int = 288,
    ) -> None:
        self.seed_path = Path(seed_path)
        self.cache_path = Path(cache_path)
        self.master_ledger_path = Path(master_ledger_path)
        self.ibkr_manager = ibkr_manager
        self.front_month_id = front_month_id  # e.g. "CLJ6"
        self.bar_size = bar_size
        self.bars_per_day = bars_per_day

        self._df: Optional[pd.DataFrame] = None
        self._bars_since_flush: int = 0
        self._roll_detected: bool = False
        self._roll_delta: float = 0.0  # Panama Canal shift applied this session

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
            if self.seed_path.exists():
                log.info("No cache found — seeding from %s", self.seed_path)
                self._df = self._seed_from_csv()
                self.save_cache()
                log.info(
                    "Cache seeded: %d bars, range %s → %s",
                    len(self._df),
                    self._df.index.min(),
                    self._df.index.max(),
                )
            else:
                # ── HARD FAIL — Design Rule: No Silent Bootstrap ────────────────
                # There is ONE pipeline for live data: seed file → cache → live append.
                # Silently substituting IBKR as a seed creates fake environments that
                # corrupt data quality and mask bugs. If the seed is missing, we stop.
                log.error(
                    "CRITICAL: Seed file missing at '%s'. "
                    "The live trader cannot run without its historical seed. "
                    "Restore the file or update the seed_path configuration.",
                    self.seed_path,
                )
                raise FileNotFoundError(
                    f"Seed file not found: {self.seed_path}\n"
                    f"The warm-start seed must exist before the live trader starts.\n"
                    f"Check CL_DATA_ROOT ({os.environ.get('CL_DATA_ROOT', '(not set)')}) "
                    f"and verify the file is present at the expected path."
                )

        # Step 2: Detect rollover and apply Panama Canal back-adjustment
        if self.ibkr_manager is not None and self.front_month_id is not None:
            self._roll_detected = self._detect_rollover()
            if self._roll_detected:
                log.warning(
                    "CONTRACT ROLLOVER DETECTED — computing roll delta..."
                )
                roll_delta = self._compute_roll_delta()
                if roll_delta is not None and abs(roll_delta) > _ROLL_PRICE_TOLERANCE:
                    self._back_adjust_cache(roll_delta)
                else:
                    log.info(
                        "Roll detected but delta within tolerance "
                        "($%.4f) — no adjustment needed.",
                        roll_delta if roll_delta is not None else 0.0,
                    )

        # Step 3: Backfill any gap from IBKR (recent bars only, not cold-start seeding)
        if self.ibkr_manager is not None:
            self._backfill()
        else:
            log.warning(
                "No IBKR manager provided — skipping backfill. "
                "Cache may have stale data."
            )

        # Step 4: Update master training ledger
        if self.ibkr_manager is not None:
            self._update_training_ledger()

        # Step 5: Save rollover metadata
        if self.front_month_id is not None:
            self._save_roll_metadata()

        # Step 6: Backup cache to repo on rollover (or first run)
        if self._roll_detected or not any(_CACHE_BACKUP_DIR.glob("*.parquet")):
            self._backup_cache_to_repo(
                reason="rollover" if self._roll_detected else "initial"
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

        Uses atomic write (temp file + os.replace) to prevent corruption
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
            # Reset index so DateTime becomes a column for clean storage.
            # Only persist OHLCV columns: extra columns (feature engineering
            # artifacts, training labels, etc.) must never be written to the
            # live cache — they introduce NaN-filled rows when new bars are
            # appended as plain OHLCV, which can corrupt downstream features.
            _required_cols = [c for c in
                              ["DateTime", "Open", "High", "Low", "Close", "Volume"]
                              if c in self._df.columns]
            save_df = self._df[_required_cols].copy()
            if save_df.index.name == "DateTime":
                save_df = save_df.reset_index(drop=True)

            save_df.to_parquet(tmp_path, index=False, engine="pyarrow")
            # Single-step atomic replace (avoids unlink+rename; unlink on /mnt/c/ under WSL
            # often fails with PermissionError when the cache was touched from Windows).
            os.replace(tmp_path, self.cache_path)
            log.info(
                "Cache saved: %d bars → %s", len(self._df), self.cache_path
            )
            _mirror_to_root(self.cache_path, _PROJECT_ROOT)
        except PermissionError:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
            log.error(
                "Cannot write cache at %s (permission denied). "
                "If you use WSL, set CL_DATA_ROOT to a Linux path (e.g. ~/CL_Analyst_Data) "
                "instead of /mnt/c/... — DrvFs often blocks replace/delete on Windows-owned files.",
                self.cache_path,
            )
            raise
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
        Load the last `_SEED_LOOKBACK_DAYS` of data from the seed file.

        Supports two formats:
          - Parquet (.parquet): read directly, expects DateTime column + OHLCV.
          - CSV (.csv): semicolon-delimited with no header:
              Date;Time;Open;High;Low;Close;Volume
              20/11/2008;00:00;181.588;...

        Returns:
            pd.DataFrame with DateTime index and OHLCV columns.
        """
        if not self.seed_path.exists():
            _alt = _PROJECT_ROOT / "data" / "raw" / "cl-5m_bk.csv"
            raise FileNotFoundError(
                f"Seed file not found: {self.seed_path}\n"
                f"The CL seed CSV (cl-5m_bk.csv) must exist in one of:\n"
                f"  1. CL_DATA_ROOT env var location: "
                f"{os.environ.get('CL_DATA_ROOT', '(not set)')}\n"
                f"  2. Project-relative path: {_alt}\n"
                f"Set CL_DATA_ROOT or copy the file to fix this."
            )

        log.info("Reading seed file: %s", self.seed_path)

        # ── Parquet seed (e.g. cl-1h_bk_HourSet_02.parquet) ──────────────
        if self.seed_path.suffix.lower() == ".parquet":
            df = pd.read_parquet(self.seed_path, engine="pyarrow")
            if "DateTime" not in df.columns:
                # Try resetting index if DateTime is the index
                df = df.reset_index()
            df["DateTime"] = pd.to_datetime(df["DateTime"])
            df = df[["DateTime", "Open", "High", "Low", "Close", "Volume"]]
            df = df.set_index("DateTime", drop=False)
            df.index.name = "DateTime"
            df = df.sort_index()
        else:
            # ── Legacy semicolon-delimited CSV ────────────────────────────────
            df = pd.read_csv(
                self.seed_path,
                sep=";",
                header=None,
                names=["Date", "Time", "Open", "High", "Low", "Close", "Volume"],
            )
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

        # Strip any non-OHLCV columns that may have been written by a bug
        # (e.g. feature columns from a training dataset accidentally used as
        # a cache, or a session that wrote enriched DataFrames to disk).
        # Keeping extra columns propagates NaN-filled feature rows into the
        # rolling window, which can silently corrupt downstream computations.
        _ohlcv_cols = {"DateTime", "Open", "High", "Low", "Close", "Volume"}
        extra_cols = set(df.columns) - _ohlcv_cols
        if extra_cols:
            log.warning(
                "Cache %s has %d extra columns beyond OHLCV — stripping: %s",
                self.cache_path.name, len(extra_cols),
                sorted(extra_cols)[:10],
            )
            df = df[[c for c in ["DateTime", "Open", "High", "Low", "Close", "Volume"]
                      if c in df.columns]]

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

        # Continuous futures reject specific endDateTime values (IBKR 10339).
        # Use a single NOW-anchored request covering the whole gap.
        gap_td = timedelta(seconds=gap.total_seconds())
        gap_days = max(1, int((gap_td.total_seconds() + 86_399) // 86_400))
        chunks = [f"{gap_days} D"]

        log.info(
            "Backfill: continuous contract using single chunk: %s ending NOW",
            chunks[0],
        )

        from src.live_execution.ibkr_client import build_cl_contract

        contract = build_cl_contract(continuous=True)
        contract = self.ibkr_manager.qualify_contract(contract)

        total_stitched = 0
        for i, duration_str in enumerate(chunks):
            # Keep endDateTime blank for continuous futures (required by IBKR).
            end_dt_str = ""

            log.info(
                "Backfill chunk %d/%d: requesting %s ending %s ...",
                i + 1, len(chunks), duration_str, end_dt_str if end_dt_str else "NOW"
            )

            bars = self.ibkr_manager._request_historical_data(
                contract=contract,
                duration_str=duration_str,
                bar_size=self.bar_size,
                what_to_show="TRADES",
                use_rth=False,
                end_datetime=end_dt_str,
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
    # Private: Rollover detection & cache validation
    # ------------------------------------------------------------------

    def _load_roll_metadata(self) -> dict:
        """Load the last known front-month ID from metadata file."""
        meta_path = Path(_ROLL_METADATA_PATH)
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read roll metadata: %s", exc)
        return {}

    def _save_roll_metadata(self) -> None:
        """Save the current front-month ID and roll history to metadata file."""
        meta_path = Path(_ROLL_METADATA_PATH)
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing metadata to preserve roll history
        existing = self._load_roll_metadata()
        roll_history = existing.get("roll_history", [])
        cumulative_delta = existing.get("cumulative_delta", 0.0)

        # Append this roll event if a delta was applied
        if self._roll_detected and abs(self._roll_delta) > _ROLL_PRICE_TOLERANCE:
            old_fm = existing.get("last_front_month", "unknown")
            roll_history.append({
                "from": old_fm,
                "to": self.front_month_id,
                "delta": round(self._roll_delta, 6),
                "timestamp": datetime.now().isoformat(),
            })
            cumulative_delta += self._roll_delta

        meta = {
            "last_front_month": self.front_month_id,
            "updated_at": datetime.now().isoformat(),
            "roll_history": roll_history,
            "cumulative_delta": round(cumulative_delta, 6),
        }
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            log.info(
                "Roll metadata saved: front_month=%s  cumulative_delta=$%.4f",
                self.front_month_id, cumulative_delta,
            )
        except OSError as exc:
            log.warning("Could not save roll metadata: %s", exc)

    def _backup_cache_to_repo(self, reason: str = "rollover") -> None:
        """
        Save a timestamped snapshot of the cache to the git repo.

        Creates data/cache_backups/ in the project root and copies:
          - warm_start_cache_<timestamp>.parquet
          - roll_metadata_<timestamp>.json

        Args:
            reason: Why the backup was triggered (for the log filename).
        """
        backup_dir = _CACHE_BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Backup cache
        if self._df is not None and len(self._df) > 0:
            cache_backup = backup_dir / f"warm_start_cache_{ts}_{reason}.parquet"
            try:
                self._df.to_parquet(str(cache_backup), engine="pyarrow")
                log.info(
                    "Cache backup saved: %s (%d bars)",
                    cache_backup.name, len(self._df),
                )
            except Exception as exc:
                log.warning("Failed to backup cache: %s", exc)

        # Backup roll metadata
        meta_src = Path(_ROLL_METADATA_PATH)
        if meta_src.exists():
            meta_backup = backup_dir / f"roll_metadata_{ts}_{reason}.json"
            try:
                shutil.copy2(str(meta_src), str(meta_backup))
                log.info("Roll metadata backup saved: %s", meta_backup.name)
            except Exception as exc:
                log.warning("Failed to backup roll metadata: %s", exc)

    def _detect_rollover(self) -> bool:
        """
        Compare current front-month contract with the last known one.

        Returns True if a rollover has occurred since the last run.
        """
        meta = self._load_roll_metadata()
        last_fm = meta.get("last_front_month")

        if last_fm is None:
            log.info(
                "No previous front-month recorded — first run. "
                "Current: %s", self.front_month_id,
            )
            return False

        if last_fm == self.front_month_id:
            log.info(
                "Front-month unchanged: %s — no rollover.", last_fm,
            )
            return False

        log.warning(
            "ROLLOVER: %s → %s", last_fm, self.front_month_id,
        )
        return True

    def _compute_roll_delta(self) -> Optional[float]:
        """
        Compute the Panama Canal roll delta between cached and IBKR prices.

        Fetches recent bars from IBKR's continuous contract (which is now
        back-adjusted to the new front month) and compares Close prices
        with our cache (still on the old back-adjustment basis).

        Returns:
            The median price delta (ibkr - cache), or None if comparison
            is not possible.
        """
        import numpy as np

        if self._df is None or len(self._df) == 0:
            log.warning("Cannot compute roll delta — empty cache.")
            return None

        from src.live_execution.ibkr_client import (
            build_cl_contract,
            ib_bars_to_dataframe,
        )

        contract = build_cl_contract(continuous=True)
        contract = self.ibkr_manager.qualify_contract(contract)

        # Request recent bars from IBKR
        bars = self.ibkr_manager._request_historical_data(
            contract=contract,
            duration_str="3 D",
            bar_size=self.bar_size,
            what_to_show="TRADES",
            use_rth=False,
            end_datetime="",
            max_retries=3,
            backoff_seconds=2.0,
            throttle_seconds=0.5,
        )
        if not bars:
            log.warning(
                "No bars returned from IBKR for roll delta — "
                "cannot compute delta."
            )
            return None

        ibkr_df = ib_bars_to_dataframe(bars)

        # Find overlapping timestamps
        overlap = self._df.index.intersection(ibkr_df.index)
        if len(overlap) == 0:
            log.warning(
                "No overlapping timestamps between cache and IBKR — "
                "cannot compute roll delta."
            )
            return None

        # Use the most recent overlapping bars for a robust delta
        sample = overlap[-_ROLL_VALIDATION_BARS:]
        cache_close = self._df.loc[sample, "Close"].values
        ibkr_close = ibkr_df.loc[sample, "Close"].values

        deltas = ibkr_close - cache_close
        median_delta = float(np.median(deltas))
        mean_delta = float(np.mean(deltas))
        max_spread = float(np.max(np.abs(deltas - median_delta)))

        log.info(
            "Roll delta: compared %d bars — "
            "median=$%.4f  mean=$%.4f  max_spread=$%.4f",
            len(sample), median_delta, mean_delta, max_spread,
        )

        # Store the IBKR data for use by _back_adjust_cache
        self._ibkr_overlap_df = ibkr_df

        return median_delta

    def _back_adjust_cache(self, delta: float) -> None:
        """
        Apply Panama Canal back-adjustment to the entire cache.

        Shifts all OHLC prices by `delta` to align with the new
        continuous contract. Volume is untouched. Then overwrites
        any overlapping bars with fresh IBKR data for a clean seam.

        Args:
            delta: The price shift to apply (ibkr_price - cache_price).
        """
        if self._df is None or len(self._df) == 0:
            log.warning("Cannot back-adjust — empty cache.")
            return

        n_bars = len(self._df)
        log.info(
            "PANAMA CANAL BACK-ADJUSTMENT: shifting %d bars by $%.4f",
            n_bars, delta,
        )

        # Step 1: Shift all OHLC columns by delta
        for col in ("Open", "High", "Low", "Close"):
            if col in self._df.columns:
                self._df[col] = self._df[col] + delta

        # Step 2: Overwrite overlapping bars with fresh IBKR data
        # for a perfectly clean seam at the transition
        ibkr_df = getattr(self, "_ibkr_overlap_df", None)
        if ibkr_df is not None and len(ibkr_df) > 0:
            overlap = self._df.index.intersection(ibkr_df.index)
            if len(overlap) > 0:
                for col in ("Open", "High", "Low", "Close", "Volume"):
                    if col in ibkr_df.columns and col in self._df.columns:
                        self._df.loc[overlap, col] = ibkr_df.loc[overlap, col]
                log.info(
                    "Overwrote %d overlapping bars with fresh IBKR data.",
                    len(overlap),
                )
            # Clean up temporary reference
            del self._ibkr_overlap_df

        # Step 3: Store delta for training ledger update and metadata
        self._roll_delta = delta

        # Step 4: Save the adjusted cache
        self.save_cache()

        log.info(
            "Back-adjustment complete: %d bars shifted by $%.4f. "
            "Range: %s → %s",
            n_bars, delta,
            self._df.index.min(),
            self._df.index.max(),
        )

    def _full_rebuild_cache(self) -> None:
        """
        FALLBACK: Delete and rebuild cache from seed + IBKR backfill.

        WARNING: This requires IBKR to serve enough historical data to
        bridge the gap from the seed CSV to present.  IBKR's 5-min bar
        limit is ~60 days. If the gap exceeds this, the rebuild will fail.

        Prefer _back_adjust_cache() for rollovers.
        """
        log.warning(
            "FULL REBUILD (FALLBACK): deleting cache and re-seeding. "
            "This requires IBKR historical data to bridge the gap."
        )

        if self.cache_path.exists():
            try:
                self.cache_path.unlink()
            except PermissionError:
                log.error(
                    "Cannot delete cache at %s (permission denied). "
                    "If you use WSL, set CL_DATA_ROOT to a Linux path instead of /mnt/c/...",
                    self.cache_path,
                )
                raise
            log.info("Deleted stale cache: %s", self.cache_path)

        self._df = self._seed_from_csv()
        self.save_cache()

        log.info(
            "Cache rebuilt from seed: %d bars, range %s → %s. "
            "Backfill will follow.",
            len(self._df),
            self._df.index.min(),
            self._df.index.max(),
        )

    # ------------------------------------------------------------------
    # Private: Master training ledger
    # ------------------------------------------------------------------

    def _update_training_ledger(self) -> None:
        """
        Maintain a growing master ledger of back-adjusted continuous data.

        - On first run: create from seed CSV + IBKR backfill of full history
        - On normal startup: append any new bars since the ledger's last timestamp
        - After rollover: re-fetch the IBKR portion to get back-adjusted prices

        The original seed file is never modified.
        """
        from src.live_execution.ibkr_client import (
            build_cl_contract,
            ib_bars_to_dataframe,
        )

        if self.master_ledger_path.exists():
            ledger = pd.read_parquet(
                self.master_ledger_path, engine="pyarrow"
            )
            if "DateTime" in ledger.columns:
                ledger["DateTime"] = pd.to_datetime(ledger["DateTime"])
                ledger = ledger.set_index("DateTime", drop=False)
                ledger.index.name = "DateTime"
            ledger = ledger.sort_index()
            log.info(
                "Master ledger loaded: %d bars, range %s → %s",
                len(ledger), ledger.index.min(), ledger.index.max(),
            )
        else:
            # First run: create from seed CSV (full history)
            log.info("Creating master ledger from seed CSV (full file)...")
            ledger = self._load_full_seed()
            log.info(
                "Seed loaded for ledger: %d bars, range %s → %s",
                len(ledger), ledger.index.min(), ledger.index.max(),
            )

        if self._roll_detected and abs(self._roll_delta) > _ROLL_PRICE_TOLERANCE:
            # After rollover: apply Panama Canal shift to ENTIRE ledger.
            # Every single row (back to 2008) gets the delta applied.
            # This is the institutional standard — negative absolute
            # prices in deep history are expected and harmless because
            # the model uses relative features (ATR, MACD, returns).
            log.warning(
                "PANAMA CANAL: shifting ENTIRE ledger (%d bars) by $%.4f",
                len(ledger), self._roll_delta,
            )
            for col in ("Open", "High", "Low", "Close"):
                if col in ledger.columns:
                    ledger[col] = ledger[col] + self._roll_delta
            log.info(
                "Ledger shifted: %d bars by $%.4f. "
                "New range: $%.2f → $%.2f",
                len(ledger), self._roll_delta,
                ledger["Close"].min(), ledger["Close"].max(),
            )

        # Always append any new bars from IBKR (covers both roll and
        # normal startup cases)
        ibkr_new = self._fetch_ibkr_range(ledger.index.max())
        if ibkr_new is not None and len(ibkr_new) > 0:
            n_before = len(ledger)
            ledger = pd.concat([ledger, ibkr_new])
            ledger = ledger[
                ~ledger.index.duplicated(keep="last")
            ].sort_index()
            n_new = len(ledger) - n_before
            log.info(
                "Ledger appended: %d new bars (total: %d)",
                n_new, len(ledger),
            )

        # Save the ledger
        self._save_ledger(ledger)

    def _load_full_seed(self) -> pd.DataFrame:
        """Load the entire seed CSV (not just the last N days)."""
        if not self.seed_path.exists():
            _alt = _PROJECT_ROOT / "data" / "raw" / "cl-5m_bk.csv"
            raise FileNotFoundError(
                f"Seed file not found: {self.seed_path}\n"
                f"The CL seed CSV (cl-5m_bk.csv) must exist in one of:\n"
                f"  1. CL_DATA_ROOT env var location: "
                f"{os.environ.get('CL_DATA_ROOT', '(not set)')}\n"
                f"  2. Project-relative path: {_alt}\n"
                f"Set CL_DATA_ROOT or copy the file to fix this."
            )

        df = pd.read_csv(
            self.seed_path,
            sep=";",
            header=None,
            names=["Date", "Time", "Open", "High", "Low", "Close", "Volume"],
        )
        df["DateTime"] = pd.to_datetime(
            df["Date"] + " " + df["Time"],
            format="%d/%m/%Y %H:%M",
        )
        df = df[["DateTime", "Open", "High", "Low", "Close", "Volume"]]
        df = df.set_index("DateTime", drop=False)
        df.index.name = "DateTime"
        return df.sort_index()

    def _get_seed_end_timestamp(self, ledger: pd.DataFrame) -> pd.Timestamp:
        """
        Estimate where the original seed data ends in the ledger.

        Uses the max timestamp from the seed CSV as the boundary.
        """
        try:
            seed_df = self._load_full_seed()
            return seed_df.index.max()
        except Exception:
            # Fallback: assume seed ends 2 years ago (IBKR's max range)
            return pd.Timestamp.now() - timedelta(days=730)

    def _fetch_ibkr_range(
        self, start_ts: pd.Timestamp
    ) -> Optional[pd.DataFrame]:
        """
        Fetch continuous contract bars from IBKR covering start_ts to now.

        Handles chunking for requests longer than _MAX_IB_REQUEST_DAYS.
        """
        from src.live_execution.ibkr_client import (
            build_cl_contract,
            ib_bars_to_dataframe,
        )

        now = pd.Timestamp.now()
        gap = now - start_ts
        if gap.total_seconds() < 600:
            log.info("Ledger gap < 10 minutes — no IBKR fetch needed.")
            return None

        # Continuous futures reject specific endDateTime values (IBKR 10339).
        # Use a single NOW-anchored request covering the whole range.
        gap_td = timedelta(seconds=gap.total_seconds())
        gap_days = max(1, int((gap_td.total_seconds() + 86_399) // 86_400))
        chunks = [f"{gap_days} D"]

        contract = build_cl_contract(continuous=True)
        contract = self.ibkr_manager.qualify_contract(contract)

        all_dfs = []
        for i, duration_str in enumerate(chunks):
            # Keep endDateTime blank for continuous futures (required by IBKR).
            end_dt_str = ""

            log.info(
                "Ledger fetch chunk %d/%d: %s ending %s",
                i + 1, len(chunks), duration_str, end_dt_str if end_dt_str else "NOW"
            )
            bars = self.ibkr_manager._request_historical_data(
                contract=contract,
                duration_str=duration_str,
                bar_size=self.bar_size,
                what_to_show="TRADES",
                use_rth=False,
                end_datetime=end_dt_str,
                max_retries=5,
                backoff_seconds=2.0,
                throttle_seconds=0.5,
            )
            
            if bars:
                all_dfs.append(ib_bars_to_dataframe(bars))

        if not all_dfs:
            return None

        result = pd.concat(all_dfs)
        result = result[~result.index.duplicated(keep="last")].sort_index()
        # Only keep bars after start_ts
        result = result.loc[result.index > start_ts]
        return result

    def _save_ledger(self, ledger: pd.DataFrame) -> None:
        """Persist the master training ledger to Parquet."""
        self.master_ledger_path.parent.mkdir(parents=True, exist_ok=True)

        save_df = ledger.copy()
        if save_df.index.name == "DateTime":
            save_df = save_df.reset_index(drop=True)

        fd, tmp_path = tempfile.mkstemp(
            suffix=".parquet",
            dir=str(self.master_ledger_path.parent),
        )
        os.close(fd)

        try:
            save_df.to_parquet(tmp_path, index=False, engine="pyarrow")
            os.replace(tmp_path, self.master_ledger_path)
            log.info(
                "Master ledger saved: %d bars → %s",
                len(ledger), self.master_ledger_path,
            )
            _mirror_to_root(self.master_ledger_path, _PROJECT_ROOT)
        except PermissionError:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
            log.error(
                "Cannot write master ledger at %s (permission denied). "
                "If you use WSL, set CL_DATA_ROOT to a Linux path instead of /mnt/c/...",
                self.master_ledger_path,
            )
            raise
        except Exception:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
            raise

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
