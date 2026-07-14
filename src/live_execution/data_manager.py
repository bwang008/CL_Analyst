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
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import pandas as pd

from src.core.instrument_master import get_instrument
from src.live_execution.utils.time_utils import (
    split_duration_into_chunks,
    timedelta_to_ib_duration,
)

from src.live_execution.interfaces.data_feed_interface import DataFeedClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Roll-seam resolution outcome protocol (jit-roll-ratio-empty_07102026_1453
# Stage 2). Module-level constants so live_trader compares against the same
# strings resolve_roll_seam() returns.
# ---------------------------------------------------------------------------
ROLL_SEAM_RESOLVED = "RESOLVED"  # seam anchored: ratio recorded + persisted
ROLL_SEAM_RETRY = "RETRY"        # IBKR lead not flipped yet — retry later
ROLL_SEAM_ESCALATE = "ESCALATE"  # nothing to anchor on — operator must act

# ---------------------------------------------------------------------------
# Per-symbol data-path derivation (T2 — single naming authority)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Resolve data paths via centralized helper (CL_DATA_ROOT primary, repo-local fallback)
from src.data_paths import get_data_path, get_data_root, mirror_file as _dp_mirror


@dataclass(frozen=True)
class DataPaths:
    """The 7 per-symbol live data artifacts (audit D4 naming table)."""

    seed_5m: Path
    cache_5m: Path
    ledger_5m: Path
    seed_1h: Path
    cache_1h: Path
    ledger_1h: Path
    roll_metadata: Path


def derive_data_paths(symbol: str) -> DataPaths:
    """Single naming authority for per-symbol live data artifacts (T2, D2/D4).

    Expression fidelity (C1): the 5m seed resolves via the existence-aware
    ``get_data_path()`` (CL_DATA_ROOT primary, repo-local fallback); the
    other six artifacts are composed from ``get_data_root()`` — exactly the
    expressions the legacy CL literals used.

    CL keeps its 3 legacy exception names (5m cache, 1h cache, roll
    metadata); every other artifact already follows the generic pattern.

    Raises:
        ValueError: unknown symbol (via get_instrument — no silent CL
            fallback).
    """
    get_instrument(symbol)  # fail-fast: ValueError("Unknown instrument symbol: ...")
    sym_u = symbol.upper()
    sym_l = symbol.lower()

    seed_5m = Path(get_data_path(f"raw/{sym_l}-5m_bk.csv"))
    processed = get_data_root() / "processed"

    if sym_u == "CL":
        # Legacy CL exceptions — byte-identical to the pre-T2 literals.
        cache_5m = processed / "warm_start_cache.parquet"
        cache_1h = processed / "warm_start_cache_1h.parquet"
        roll_metadata = processed / ".roll_metadata.json"
    else:
        cache_5m = processed / f"warm_start_cache_{sym_u}.parquet"
        cache_1h = processed / f"warm_start_cache_{sym_u}_1h.parquet"
        roll_metadata = processed / f".roll_metadata_{sym_u}.json"

    return DataPaths(
        seed_5m=seed_5m,
        cache_5m=cache_5m,
        ledger_5m=processed / f"{sym_l}_continuous_master.parquet",
        seed_1h=processed / f"{sym_u}_raw_1h.parquet",
        cache_1h=cache_1h,
        ledger_1h=processed / f"{sym_l}_continuous_master_1h.parquet",
        roll_metadata=roll_metadata,
    )

# Deepest 1h feature lookback (MACRO_6M: rolling(4320) on 1H bars). The
# live_trader startup validation enforces this floor after warm-start; the
# per-instrument seed lookback below is derived from it (T5).
REQUIRED_1H_BARS = 4320


def derive_seed_lookback_days(bars_per_day_1h: int) -> int:
    """Calendar-day seed window that covers REQUIRED_1H_BARS for an instrument.

    Formula (T5, audit §4e): ceil(ceil(4320 / bars_per_day_1h) * 7 / 5) + 28
      - ceil(4320 / bph)  -> trading days needed,
      - * 7/5 (ceil)      -> calendar days (5 trading days per week),
      - + 28              -> holiday/gap buffer (the legacy 280 - 252 margin,
                             now explicit).

    CL (bars_per_day_1h=24): 180 -> 252 -> 280 — reproduces the legacy
    _SEED_LOOKBACK_DAYS constant EXACTLY (pinned). ES (23) -> 292;
    ZC/ZS (16) -> 406.

    Raises:
        ValueError: bars_per_day_1h <= 0 (no silent default).
    """
    if bars_per_day_1h <= 0:
        raise ValueError(
            f"bars_per_day_1h must be positive, got {bars_per_day_1h!r} — "
            "cannot derive a seed lookback window."
        )
    trading_days = math.ceil(REQUIRED_1H_BARS / bars_per_day_1h)
    return math.ceil(trading_days * 7 / 5) + 28


# ---------------------------------------------------------------------------
# Launch-time data preflight (ticket fleet-seed-preflight-gap_07052026_2215)
# ---------------------------------------------------------------------------
# _backfill() bridges cache-end -> now with a SINGLE NOW-anchored
# "{gap_days} D" IBKR request. Gaps beyond what one request can serve are
# UNFILLABLE at startup: the child either crashes or — worse — stitches
# recent bars onto a stale cache leaving a silent hole mid-series. These
# horizons are operator-observed practical IBKR limits (user ruling
# 2026-07-05: ~2 months for 1h), deliberately conservative; exceeding them
# must fail the launch with a "refresh via Databento" remedy.
MAX_BACKFILL_GAP_DAYS_1H = 60
MAX_BACKFILL_GAP_DAYS_5M = 30  # 5m history serves ~60 days; halve for safety


def required_live_data_artifacts(strategy_config: dict) -> list[dict]:
    """The per-symbol data artifacts a LiveTrader for this config hard-requires.

    Single authority consumed by FleetRunner.validate_data_prerequisites()
    (follow-up ticket: live_trader.__init__ itself) so the preflight can
    never drift from the trader's real requirements. Mirrors
    live_trader.__init__ exactly:

      - ``bar_size`` defaults to "5m" (live_trader.py:336);
      - ``live_config.enable_5m_stream`` defaults True; ``false`` with a
        non-hourly bar_size is a config error (live_trader.py:350);
      - hourly models (1h/2h/4h): require cache_1h OR seed_1h, honoring the
        ``live_config.seed_path_1h`` override (relative paths resolved
        against get_data_root(); live_trader.py:438-448);
      - 5m models: require seed_5m OR cache_5m (allow_shallow_bootstrap is
        False for 5m models — live_trader.py:421 / DataManager.initialize);
      - hourly models need NO 5m artifacts (shallow bootstrap sanctioned).

    Returns a list of requirement dicts:
        {"stream": "1h"|"5m", "cache": Path, "seed": Path,
         "max_gap_days": int}

    Raises:
        ValueError: enable_5m_stream=false with a non-hourly bar_size
            (mirror of the live_trader config error), or unknown symbol.
    """
    from src.live_execution.instrument_context import resolve_instrument_context

    ctx = resolve_instrument_context(strategy_config)
    paths = derive_data_paths(ctx.brain_symbol)

    bar_size = strategy_config.get("bar_size", "5m").lower()
    live_cfg = strategy_config.get("live_config", {}) or {}
    enable_5m = bool(live_cfg.get("enable_5m_stream", True))
    if not enable_5m and bar_size not in ("1h", "2h", "4h"):
        raise ValueError(
            f"live_config.enable_5m_stream=false requires an hourly bar_size "
            f"(got {bar_size!r}) — the 5m stream IS the inference stream for "
            f"5m configs."
        )

    requirements: list[dict] = []
    if bar_size in ("1h", "2h", "4h"):
        seed_1h = paths.seed_1h
        seed_override = live_cfg.get("seed_path_1h")
        if seed_override:
            seed_p = Path(seed_override)
            if not seed_p.is_absolute():
                seed_p = get_data_root() / seed_override
            seed_1h = seed_p
        requirements.append({
            "stream": "1h",
            "cache": Path(paths.cache_1h),
            "seed": Path(seed_1h),
            "max_gap_days": MAX_BACKFILL_GAP_DAYS_1H,
        })
    else:  # 5m model — hard seed requirement (no shallow bootstrap)
        requirements.append({
            "stream": "5m",
            "cache": Path(paths.cache_5m),
            "seed": Path(paths.seed_5m),
            "max_gap_days": MAX_BACKFILL_GAP_DAYS_5M,
        })
    return requirements


# Flush the in-memory cache to disk every N appended bars.
_FLUSH_INTERVAL_BARS = 12  # every hour (12 × 5 min)

# Shallow-bootstrap fetch window (seedless non-5m models — ticket
# seedless-5m-live-stream_07052026_0546, audit §1.2). "5 D" covers the deepest
# realistic in-position horizon (per-side max_hold_bars / the 288-bar engine
# rail = 24h) ACROSS a weekend, guarantees full overlap for the "3 D"
# roll-ratio fetch from day one, and is ONE request well inside IBKR pacing
# limits (5m history serves ~60 days). NOT training-grade history.
_SHALLOW_BOOTSTRAP_DURATION = "5 D"

# Number of bars to sample when validating cache after rollover.
_ROLL_VALIDATION_BARS = 50

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
        symbol: str,
        seed_path: Optional[str] = None,
        cache_path: Optional[str] = None,
        master_ledger_path: Optional[str] = None,
        roll_metadata_path: Optional[str] = None,
        data_client: Optional["DataFeedClient"] = None,
        front_month_id: Optional[str] = None,
        bar_size: str = "5 mins",
        bars_per_day: int = 288,
        execution_symbol: Optional[str] = None,
        allow_shallow_bootstrap: bool = False,
    ) -> None:
        # T2 (D2): symbol is REQUIRED keyword-only (no silent CL default) and
        # validated against the instrument registry via derive_data_paths
        # (raises ValueError on unknown symbols). Explicit path arguments
        # always win; None falls back to the per-symbol derived default.
        paths = derive_data_paths(symbol)
        self.symbol = symbol
        # T5 (C2 deferral): execution_symbol namespaces the roll metadata —
        # an outright's execution symbol IS its brain symbol (structural
        # derivation, T2 _brain_symbol precedent — NOT a silent default);
        # live_trader passes micros (MCL/MES/...) explicitly.
        self.execution_symbol = execution_symbol or symbol
        self.seed_path = Path(seed_path) if seed_path is not None else paths.seed_5m
        self.cache_path = Path(cache_path) if cache_path is not None else paths.cache_5m
        self.master_ledger_path = (
            Path(master_ledger_path)
            if master_ledger_path is not None
            else paths.ledger_5m
        )
        # Roll metadata is a per-instance attribute (pre-T2 it was the module
        # global _ROLL_METADATA_PATH). The 5m and 1h managers of one process
        # intentionally share the same per-symbol file.
        self.roll_metadata_path = (
            Path(roll_metadata_path)
            if roll_metadata_path is not None
            else paths.roll_metadata
        )
        self.data_client = data_client
        self.front_month_id = front_month_id  # e.g. "CLJ6"
        self.bar_size = bar_size
        self.bars_per_day = bars_per_day
        # seedless-5m-live-stream_07052026_0546: when True (live_trader wires
        # it ONLY for non-5m models, bar_size in ("1h","2h","4h")), a missing
        # seed+cache triggers ONE shallow "5 D" IBKR fetch instead of the
        # No-Silent-Bootstrap raise. Defaults False so every existing
        # construction site stays byte-identical (5m models keep the raise).
        self.allow_shallow_bootstrap = allow_shallow_bootstrap
        # True only when THIS run founded the window via the shallow fetch
        # (read by live_trader for the 3-surface loud disclosure).
        self.shallow_bootstrapped: bool = False
        # T5: instrument-derived facts from the registry (raises on unknown
        # symbols — no silent defaults). Derived ONLY here in __init__:
        # the ratio/adjustment methods keep working on __new__ test stubs.
        _instrument = get_instrument(symbol)
        # Ratio-space noise floor: |1 - roll_ratio| <= tolerance means the
        # roll is DETECTED (front-month string change) but the adjustment
        # (cache/ledger scale + roll_history append) is SKIPPED.
        self.roll_ratio_tolerance = _instrument.roll_ratio_tolerance
        # Calendar-day seed trim window (bar-size independent, like the
        # legacy shared _SEED_LOOKBACK_DAYS=280 — which CL reproduces).
        self.seed_lookback_days = derive_seed_lookback_days(
            _instrument.bars_per_day_1h
        )

        self._df: Optional[pd.DataFrame] = None
        self._bars_since_flush: int = 0
        self._roll_detected: bool = False
        self._roll_ratios: list[float] = []      # multiplicative rollover ratios
        self._roll_timestamps: list[pd.Timestamp] = []  # when each rollover happened
        # jit-roll-ratio-empty_07102026_1453 (Stage 2): count of leading
        # _roll_ratios entries already persisted to the metadata file
        # (Step-0 restores + _append_roll_event writes). The Step-5
        # _save_roll_metadata writer skips them so a seam recorded mid-run
        # is never double-appended at the next startup.
        self._persisted_roll_events: int = 0

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
        # Step 0: Restore roll ratios from saved metadata.
        # C2 (T5): ownership-filtered — a shared brain file (CL+MCL, ES+MES)
        # accumulates one roll_history entry PER execution symbol for the
        # same economic roll; restoring them all would double-apply the roll.
        # Only entries whose "to" contract belongs to this execution symbol
        # are restored. Entries without a "to" field (pre-history legacy
        # files were single-symbol by construction) pass through unchanged,
        # as does every entry of a CL-only file (pinned). The intra-process
        # 5m/1h managers share one execution_symbol, so their sharing is
        # preserved. Note: "cumulative_ratio" stays global/mixed across
        # symbols — it is informational only (no runtime consumer).
        meta = self._load_roll_metadata()
        for entry in meta.get("roll_history", []):
            to_fm = entry.get("to")
            if isinstance(to_fm, str) and not to_fm.startswith(
                self.execution_symbol
            ):
                continue
            if "ratio" in entry:
                self._roll_ratios.append(entry["ratio"])
                ts = pd.Timestamp(entry.get("timestamp_cutoff", entry.get("timestamp")))
                self._roll_timestamps.append(ts)
        # jit-roll-ratio-empty_07102026_1453: everything restored from disk
        # is by definition already persisted — the Step-5 writer must not
        # re-append it.
        self._persisted_roll_events = len(self._roll_ratios)

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
                if self.allow_shallow_bootstrap and self.data_client is not None:
                    # seedless-5m-live-stream_07052026_0546: SANCTIONED
                    # shallow bootstrap (non-5m models only — user ruling:
                    # only HISTORICAL 5m purchases are banned; live 5m
                    # streaming is the default). Loud, cached, never silent.
                    self._df = self._shallow_bootstrap_from_ibkr()
                    self.shallow_bootstrapped = True
                    self.save_cache()  # run 2+ warm-starts from this cache
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
                        f"Seed file not found for {self.symbol}: {self.seed_path}\n"
                        f"The warm-start seed must exist before the live trader starts.\n"
                        f"Check CL_DATA_ROOT ({os.environ.get('CL_DATA_ROOT', '(not set)')}) "
                        f"and verify the file is present at the expected path."
                    )

        # Step 1.5 (jit-roll-ratio-empty_07102026_1453 Stage 2): resolve any
        # unresolved pending_roll for this execution symbol BEFORE
        # _backfill() — backfill's dedup keep="last" overwrites the
        # old-basis overlap bars with fresh new-basis IBKR values and
        # destroys the seam anchor. This REPLACES the silent
        # "ratio≈1 → within tolerance" swallow for the pending-roll path
        # only: an un-anchorable pending seam is a hard stop, never
        # wrong-basis trading.
        pending = self.get_pending_roll()
        if pending is not None:
            if self.data_client is None:
                log.warning(
                    "Unresolved pending roll %s → %s for %s but no data "
                    "client — offline construction cannot scan the seam; "
                    "record kept for the next live startup.",
                    pending["from"], pending["to"], self.execution_symbol,
                )
            else:
                log.warning(
                    "PENDING ROLL FOUND at startup: %s → %s (detected %s) — "
                    "running seam scan before backfill...",
                    pending["from"], pending["to"], pending["detected_at"],
                )
                outcome = self.resolve_roll_seam(
                    from_contract=pending["from"],
                    to_contract=pending["to"],
                    detected_at=pending["detected_at"],
                )
                if outcome == ROLL_SEAM_RESOLVED:
                    self.clear_pending_roll()
                    log.warning(
                        "PENDING ROLL RESOLVED at startup: %s → %s "
                        "(ratio recorded, record cleared).",
                        pending["from"], pending["to"],
                    )
                elif outcome == ROLL_SEAM_ESCALATE:
                    raise RuntimeError(
                        f"Unresolvable PENDING ROLL for "
                        f"{self.execution_symbol}: {pending['from']} → "
                        f"{pending['to']} (detected {pending['detected_at']}) "
                        f"— the CONTFUT seam scan found no old-basis anchor "
                        f"bars (every overlap bar already matches the new "
                        f"basis), so the roll ratio cannot be recovered from "
                        f"IBKR. Refusing to trade on a broken price basis. "
                        f"Operator remedy: derive the ratio from an "
                        f"expired-contract overlap fetch (median "
                        f"same-timestamp Close ratio of {pending['from']} vs "
                        f"{pending['to']}), append it to roll_history in "
                        f"{self.roll_metadata_path} with the seam "
                        f"timestamp_cutoff, remove this symbol's "
                        f"pending_roll entry, then restart."
                    )
                else:  # ROLL_SEAM_RETRY — IBKR lead not flipped yet
                    log.warning(
                        "Pending roll %s → %s NOT yet resolvable "
                        "(outcome=%s) — record kept; the live retry loop "
                        "re-attempts on each new 1h bar.",
                        pending["from"], pending["to"], outcome,
                    )

        # Step 2: Detect rollover and record ratio (cache stays RAW)
        if self.data_client is not None and self.front_month_id is not None:
            self._roll_detected = self._detect_rollover()
            if self._roll_detected:
                log.warning(
                    "CONTRACT ROLLOVER DETECTED — computing roll ratio..."
                )
                roll_ratio = self._compute_roll_ratio()
                if roll_ratio is not None and abs(roll_ratio - 1.0) > self.roll_ratio_tolerance:
                    self._apply_roll_to_cache(roll_ratio)
                else:
                    log.info(
                        "Roll detected but ratio within tolerance "
                        "(%.6f) — no adjustment needed.",
                        roll_ratio if roll_ratio is not None else 1.0,
                    )

        # Step 3: Backfill any gap from IBKR (recent bars only, not cold-start seeding)
        if self.data_client is not None:
            self._backfill()
        else:
            log.warning(
                "No IBKR manager provided — skipping backfill. "
                "Cache may have stale data."
            )

        # Step 4: Update master training ledger
        if self.data_client is not None:
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

        # Coerce OHLCV to numeric BEFORE the concat. The reconnect gap-backfill
        # loop appends bars via ``row.to_frame().T`` over an ``iterrows()`` Series
        # (a mixed Timestamp+float row is object-dtype), and the ``isinstance(row,
        # pd.Series)`` branch above does the same for raw-Series callers. Concat-ing
        # an object-dtype row upcasts the cache's float64 OHLCV columns to object,
        # which later makes the shared feature engine's ``np.log(Close/…)`` raise a
        # ufunc TypeError — the fleet then goes hourly-blind after every reconnect
        # (ticket 1h-reconnect-object-dtype_07082026_0032). ``errors="raise"`` keeps
        # a genuinely corrupt bar loud rather than silently NaN-ing it. Copy first so
        # a DataFrame caller passing a view is never mutated in place (and no
        # SettingWithCopyWarning); the row is a single bar, so the copy is cheap.
        row = row.copy()
        for col in ("Open", "High", "Low", "Close", "Volume"):
            if col in row.columns:
                row[col] = pd.to_numeric(row[col], errors="raise")

        # DateTime needs the same treatment: an object-dtype DateTime row upcasts
        # the cache's datetime64 column on concat, and every downstream
        # ``replace([inf, -inf])`` over the poisoned frame then fires the pandas
        # 2.x downcast FutureWarning (alpha_factory.py:292, ticket
        # alpha-factory-downcast-warning_07142026_0816). ``errors="raise"`` again:
        # an unparseable bar timestamp must crash loudly, never coerce to NaT.
        if "DateTime" in row.columns:
            row["DateTime"] = pd.to_datetime(row["DateTime"], errors="raise")

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
        Load the last `seed_lookback_days` of data from the seed file.

        Supports two formats:
          - Parquet (.parquet): read directly, expects DateTime column + OHLCV.
          - CSV (.csv): semicolon-delimited with no header:
              Date;Time;Open;High;Low;Close;Volume
              20/11/2008;00:00;181.588;...

        Returns:
            pd.DataFrame with DateTime index and OHLCV columns.
        """
        if not self.seed_path.exists():
            _alt = _PROJECT_ROOT / "data" / "raw" / self.seed_path.name
            raise FileNotFoundError(
                f"Seed file not found: {self.seed_path}\n"
                f"The {self.symbol} seed file ({self.seed_path.name}) must exist in one of:\n"
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

        # Take the last N days (instrument-derived window — T5)
        cutoff = df.index.max() - timedelta(days=self.seed_lookback_days)
        df = df.loc[df.index >= cutoff]

        log.info(
            "Seed: extracted %d bars (last %d days) from %s → %s",
            len(df),
            self.seed_lookback_days,
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

    def _drop_incomplete_bar(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Drop the final bar from the DataFrame if it has not yet completed.
        IBKR historical requests return the currently forming bar, which would
        contaminate the cache with partial volume and a mid-bar close price.
        """
        if df.empty:
            return df
            
        now_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)
        try:
            bar_duration = pd.Timedelta(self.bar_size.replace("mins", "min"))
            is_complete = (df.index + bar_duration) <= now_utc
            if not is_complete[-1]:
                log.info("Dropping incomplete current bar at %s", df.index[-1])
                return df[is_complete]
        except Exception as e:
            log.warning("Could not filter incomplete bars: %s", e)

        return df

    def _shallow_bootstrap_from_ibkr(self) -> pd.DataFrame:
        """SHALLOW 5M BOOTSTRAP (seedless non-5m models): fetch a few days of
        bars from IBKR to build the rolling window. NOT training-grade
        history. Ticket seedless-5m-live-stream_07052026_0546 (audit §5.1)."""
        log.warning(
            "SHALLOW 5M BOOTSTRAP: no seed/cache for %s — fetching %s of %s "
            "bars from IBKR (trailing/telemetry window; NOT training data)...",
            self.symbol, _SHALLOW_BOOTSTRAP_DURATION, self.bar_size,
        )
        df = self.data_client.fetch_historical_bars_by_duration(
            duration_str=_SHALLOW_BOOTSTRAP_DURATION,
            continuous=True,
            bar_size=self.bar_size,
            what_to_show="TRADES",
            use_rth=False,
        )
        if df is not None and not df.empty:
            df = self._drop_incomplete_bar(df)
        if df is None or df.empty:
            # C3: raise BEFORE any save_cache() — never a silent empty
            # window, never a cache founded on nothing.
            raise RuntimeError(
                f"SHALLOW 5M BOOTSTRAP failed: IBKR returned no "
                f"{self.bar_size} bars for {self.symbol}."
            )
        log.warning(
            "SHALLOW 5M BOOTSTRAP: %d bars  %s → %s",
            len(df), df.index.min(), df.index.max(),
        )
        return df

    def _backfill(self) -> None:
        """
        Fetch missing bars from IBKR to bridge the gap between the
        cache's last timestamp and now.
        """
        if self._df is None or len(self._df) == 0:
            log.warning("Cannot backfill — no data in cache.")
            return

        last_ts = self._df.index.max()
        now = pd.Timestamp.now(tz="UTC").tz_localize(None)
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

        total_stitched = 0
        for i, duration_str in enumerate(chunks):
            log.info(
                "Backfill chunk %d/%d: requesting %s ending NOW ...",
                i + 1, len(chunks), duration_str
            )

            chunk_df = self.data_client.fetch_historical_bars_by_duration(
                duration_str=duration_str,
                continuous=True,
                bar_size=self.bar_size,
                what_to_show="TRADES",
                use_rth=False,
            )
            
            if chunk_df.empty:
                log.warning("Backfill chunk %d: no bars returned.", i + 1)
                continue

            chunk_df = self._drop_incomplete_bar(chunk_df)
            if chunk_df.empty:
                continue

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
        meta_path = Path(self.roll_metadata_path)
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read roll metadata: %s", exc)
        return {}

    def _stored_front_month(self, meta: dict) -> Optional[str]:
        """Namespaced read of this execution symbol's last front month (T5).

        Read order (C2/C1 — no cross-symbol reads):
          1. ``last_front_month_by_symbol[execution_symbol]`` when present;
          2. else the legacy ``last_front_month`` ONLY when it belongs to
             this execution symbol (startswith ownership check —
             "CLQ6".startswith("MCL") and "MCLQ6".startswith("CL") are both
             False, so shared files cannot cross-read);
          3. else None (first-run semantics).

        A CL restart on a pre-T5 legacy file resolves through (2) to exactly
        the value today's code read — comparison-identical (pinned).
        """
        by_symbol = meta.get("last_front_month_by_symbol") or {}
        if self.execution_symbol in by_symbol:
            return by_symbol[self.execution_symbol]
        legacy = meta.get("last_front_month")
        if isinstance(legacy, str) and legacy.startswith(self.execution_symbol):
            return legacy
        return None

    def _write_roll_metadata(self, meta: dict) -> None:
        """Write the roll-metadata dict to disk (shared low-level writer).

        jit-roll-ratio-empty_07102026_1453 (Stage 2): shared by
        _save_roll_metadata, _append_roll_event and the pending-roll
        accessors so every writer uses one serialization convention.
        OSError propagates — the mid-run callers must be LOUD when the
        crash-safety record cannot be persisted.
        """
        meta_path = Path(self.roll_metadata_path)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

    def _append_roll_event(
        self,
        from_contract: str,
        to_contract: str,
        ratio: float,
        timestamp_cutoff,
    ) -> None:
        """Record one roll event in-memory AND persist it IMMEDIATELY.

        jit-roll-ratio-empty_07102026_1453 (Stage 2): refactored out of
        _save_roll_metadata's initialize-only flow so mid-run seam capture
        (resolve_roll_seam) no longer depends on initialize() to persist.
        Appends ratio/cutoff to _roll_ratios/_roll_timestamps and writes the
        entry to the metadata file in the existing live schema ("to" carries
        the execution-symbol-prefixed contract so the Step-0 ownership
        filter restores it on the next construction). Prior roll_history
        entries and unrelated metadata keys are preserved untouched.
        """
        ratio_f = float(ratio)
        cutoff_ts = pd.Timestamp(timestamp_cutoff)
        self._roll_ratios.append(ratio_f)
        self._roll_timestamps.append(cutoff_ts)

        meta = self._load_roll_metadata()
        roll_history = meta.get("roll_history", [])
        roll_history.append({
            "from": from_contract,
            "to": to_contract,
            "ratio": round(ratio_f, 6),
            "timestamp": datetime.now().isoformat(),
            "timestamp_cutoff": cutoff_ts.isoformat(),
        })
        meta["roll_history"] = roll_history
        # cumulative_ratio is informational only (no runtime consumer).
        meta["cumulative_ratio"] = round(
            meta.get("cumulative_ratio", 1.0) * ratio_f, 6
        )
        self._write_roll_metadata(meta)
        # Everything in-memory up to here is now on disk — the Step-5
        # writer must not re-append this event.
        self._persisted_roll_events = len(self._roll_ratios)
        log.warning(
            "ROLL EVENT RECORDED + PERSISTED: %s → %s ratio=%.6f "
            "cutoff=%s (%s)",
            from_contract, to_contract, ratio_f, cutoff_ts,
            self.roll_metadata_path,
        )

    # ------------------------------------------------------------------
    # Pending-roll lifecycle (jit-roll-ratio-empty_07102026_1453 Stage 2)
    # ------------------------------------------------------------------
    # A mid-run rollover detection persists {from, to, detected_at} under
    # meta["pending_roll"][execution_symbol] BEFORE any resolution attempt
    # (crash-safe: the seam survives a process death between detection and
    # the IBKR CONTFUT basis flip). Namespaced per execution symbol,
    # mirroring last_front_month_by_symbol. All accessors are disk-backed —
    # no initialize() dependency.

    def set_pending_roll(self, from_contract: str, to_contract: str) -> None:
        """Persist this execution symbol's pending roll record IMMEDIATELY.

        Stamps detected_at itself (naive local datetime.now().isoformat(),
        the existing metadata convention).
        """
        meta = self._load_roll_metadata()
        pending = dict(meta.get("pending_roll") or {})
        pending[self.execution_symbol] = {
            "from": from_contract,
            "to": to_contract,
            "detected_at": datetime.now().isoformat(),
        }
        meta["pending_roll"] = pending
        self._write_roll_metadata(meta)
        log.warning(
            "PENDING ROLL PERSISTED: %s → %s for %s (%s)",
            from_contract, to_contract, self.execution_symbol,
            self.roll_metadata_path,
        )

    def get_pending_roll(self) -> Optional[dict]:
        """This execution symbol's pending roll record, or None.

        Disk-backed: a record written by another instance/process is
        visible to a fresh instance that never ran initialize(). Foreign
        execution symbols' records are invisible.
        """
        meta = self._load_roll_metadata()
        return (meta.get("pending_roll") or {}).get(self.execution_symbol)

    def clear_pending_roll(self) -> None:
        """Remove ONLY this execution symbol's pending record and persist.

        Other symbols' pending records and unrelated keys survive.
        """
        meta = self._load_roll_metadata()
        pending = dict(meta.get("pending_roll") or {})
        if self.execution_symbol in pending:
            del pending[self.execution_symbol]
            meta["pending_roll"] = pending
            self._write_roll_metadata(meta)
            log.info(
                "Pending roll record cleared for %s.", self.execution_symbol,
            )

    def _save_roll_metadata(self) -> None:
        """Save the current front-month ID and roll history to metadata file."""
        # Load existing metadata to preserve roll history
        existing = self._load_roll_metadata()
        roll_history = existing.get("roll_history", [])
        cumulative_ratio = existing.get("cumulative_ratio", 1.0)

        # Append this roll event if a ratio was applied.
        # jit-roll-ratio-empty_07102026_1453 (Stage 2): events already
        # persisted via _append_roll_event (pending-roll resolutions,
        # Step-0 restores) are on disk — appending them again here would
        # double-record the same economic roll. getattr default keeps
        # __new__-constructed test stubs working (pre-existing convention
        # for the ratio methods).
        current_ratio = self._roll_ratios[-1] if self._roll_ratios else 1.0
        n_persisted = getattr(self, "_persisted_roll_events", 0)
        if (
            self._roll_detected
            and abs(current_ratio - 1.0) > self.roll_ratio_tolerance
            and len(self._roll_ratios) > n_persisted
        ):
            # C1 (T5): "from" uses the SAME namespaced read order as
            # _detect_rollover — never the raw legacy key, which in a shared
            # CL+MCL file is last-writer-wins across symbols. CL-only files:
            # value identical to the legacy read (pinned).
            old_fm = self._stored_front_month(existing)
            if old_fm is None:
                old_fm = "unknown"
            roll_ts = self._roll_timestamps[-1] if self._roll_timestamps else None
            roll_history.append({
                "from": old_fm,
                "to": self.front_month_id,
                "ratio": round(current_ratio, 6),
                "timestamp": datetime.now().isoformat(),
                "timestamp_cutoff": roll_ts.isoformat() if roll_ts is not None else None,
            })
            cumulative_ratio *= current_ratio
            self._persisted_roll_events = len(self._roll_ratios)

        # T5 (C2): per-execution-symbol namespace, MERGED with (never
        # replacing) other symbols' entries. The legacy last_front_month key
        # is still written exactly as today (CL-only fleets: the file gains
        # one redundant key, behavior unchanged). Known residual: concurrent
        # same-second startups still last-writer-win on the whole JSON
        # (startup-only write, tiny window) — no file locking added.
        by_symbol = dict(existing.get("last_front_month_by_symbol") or {})
        by_symbol[self.execution_symbol] = self.front_month_id

        # jit-roll-ratio-empty_07102026_1453 (Stage 2): merge into the
        # existing dict instead of rebuilding it — additive keys
        # (pending_roll, origin-stamped fields, operator notes) must
        # survive a startup save. A RETRY-outcome pending record spans
        # restarts by design.
        meta = existing
        meta["last_front_month"] = self.front_month_id
        meta["updated_at"] = datetime.now().isoformat()
        meta["roll_history"] = roll_history
        meta["cumulative_ratio"] = round(cumulative_ratio, 6)
        meta["last_front_month_by_symbol"] = by_symbol
        try:
            self._write_roll_metadata(meta)
            log.info(
                "Roll metadata saved: front_month=%s  cumulative_ratio=%.6f",
                self.front_month_id, cumulative_ratio,
            )
        except OSError as exc:
            log.warning("Could not save roll metadata: %s", exc)

    def _backup_cache_to_repo(self, reason: str = "rollover") -> None:
        """
        Save a timestamped snapshot of the cache to the git repo.

        Creates data/cache_backups/ in the project root and copies:
          - <cache stem>_<timestamp>_<reason>.parquet
          - <roll-metadata stem>_<timestamp>_<reason>.json

        T6 cosmetic: names derive from the instance's own cache/metadata
        filenames, so per-symbol DataManagers cannot collide in the shared
        backup dir. CL's stems are ``warm_start_cache`` / ``.roll_metadata``
        -> legacy backup names byte-identical; an ES manager produces
        ``warm_start_cache_ES_*`` / ``roll_metadata_ES_*``.

        Args:
            reason: Why the backup was triggered (for the log filename).
        """
        backup_dir = _CACHE_BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Backup cache
        if self._df is not None and len(self._df) > 0:
            cache_backup = backup_dir / f"{self.cache_path.stem}_{ts}_{reason}.parquet"
            try:
                self._df.to_parquet(str(cache_backup), engine="pyarrow")
                log.info(
                    "Cache backup saved: %s (%d bars)",
                    cache_backup.name, len(self._df),
                )
            except Exception as exc:
                log.warning("Failed to backup cache: %s", exc)

        # Backup roll metadata (lstrip('.') drops the hidden-file dot from
        # the stem: '.roll_metadata' -> 'roll_metadata' — today's literal)
        meta_src = Path(self.roll_metadata_path)
        if meta_src.exists():
            meta_backup = backup_dir / f"{meta_src.stem.lstrip('.')}_{ts}_{reason}.json"
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
        # T5 (C2): namespaced read — kills the CL<->MCL shared-file restart
        # ping-pong (a foreign symbol's legacy key reads as first-run, not
        # as a phantom rollover + backup spam).
        last_fm = self._stored_front_month(meta)

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

    def _compute_roll_ratio(self) -> Optional[float]:
        """Compute the multiplicative rollover ratio between cached and IBKR prices.

        Fetches recent bars from IBKR's continuous contract (now ratio-adjusted
        to the new front month) and compares Close prices with our cache
        (still on the old contract basis).

        Returns:
            The median price ratio (ibkr_close / cache_close), or None if
            comparison is not possible.
        """
        import numpy as np

        if self._df is None or len(self._df) == 0:
            log.warning("Cannot compute roll ratio — empty cache.")
            return None

        ibkr_df = self.data_client.fetch_historical_bars_by_duration(
            duration_str="3 D",
            continuous=True,
            bar_size=self.bar_size,
            what_to_show="TRADES",
            use_rth=False,
        )
        if ibkr_df.empty:
            log.warning("No bars returned from IBKR for roll ratio.")
            return None

        overlap = self._df.index.intersection(ibkr_df.index)
        if len(overlap) == 0:
            log.warning("No overlapping timestamps — cannot compute roll ratio.")
            return None

        sample = overlap[-_ROLL_VALIDATION_BARS:]
        cache_close = self._df.loc[sample, "Close"].values
        ibkr_close = ibkr_df.loc[sample, "Close"].values

        # Avoid division by zero
        valid = cache_close > 0
        if not valid.any():
            log.warning("All cache close prices are zero — cannot compute ratio.")
            return None

        ratios = ibkr_close[valid] / cache_close[valid]
        median_ratio = float(np.median(ratios))
        mean_ratio = float(np.mean(ratios))
        max_spread = float(np.max(np.abs(ratios - median_ratio)))

        log.info(
            "Roll ratio: compared %d bars — "
            "median=%.6f  mean=%.6f  max_spread=%.6f",
            len(sample), median_ratio, mean_ratio, max_spread,
        )

        self._ibkr_overlap_df = ibkr_df
        return median_ratio

    def resolve_roll_seam(
        self,
        *,
        from_contract: str,
        to_contract: str,
        detected_at,
    ) -> str:
        """Locate and record a mid-run roll seam via a CONTFUT overlap scan.

        jit-roll-ratio-empty_07102026_1453 (Stage 2): shared by mid-run
        capture (live_trader pending-roll retries) and the initialize()
        pending-roll resolution. Fetches CONTFUT history covering
        detected_at − 2 days → now (min "5 D"), computes the per-bar
        quotient q_t = ibkr_close / cache_close on the timestamp
        intersection with the cache, and classifies each overlap bar:
            old-basis  ⇔ |q_t − 1| >  roll_ratio_tolerance
            new-basis  ⇔ |q_t − 1| <= roll_ratio_tolerance

        Outcomes (module constants):
          - ALL bars old-basis → ROLL_SEAM_RETRY: IBKR's lead has not
            flipped yet; recording now would strand later old-basis appends
            unadjusted. Nothing recorded, nothing persisted.
          - ALL bars new-basis → ROLL_SEAM_ESCALATE: nothing to anchor on.
            Nothing recorded; the caller owns the loud stop.
          - old-run followed by new-run → ROLL_SEAM_RESOLVED:
            ratio = median of the old-run quotients (robust to contaminated
            bars inside the run), cutoff = FIRST new-basis bar timestamp;
            recorded + persisted IMMEDIATELY via _append_roll_event.

        Does NOT read or clear pending_roll — callers own that lifecycle.

        Args:
            from_contract: old (pre-roll) local contract symbol.
            to_contract: new local contract symbol (execution-symbol
                prefixed — becomes the persisted entry's "to").
            detected_at: pd.Timestamp or ISO-8601 str (the pending_roll
                JSON round-trip delivers str).
        """
        import numpy as np

        if self.data_client is None:
            raise RuntimeError(
                "resolve_roll_seam requires a data client — cannot scan a "
                f"roll seam for {self.symbol} offline."
            )
        if self._df is None or len(self._df) == 0:
            log.warning(
                "resolve_roll_seam: empty cache for %s — nothing to compare "
                "against; RETRY.", self.symbol,
            )
            return ROLL_SEAM_RETRY

        detected_ts = pd.Timestamp(detected_at)
        now = pd.Timestamp.now(tz="UTC").tz_localize(None)
        span_s = (now - (detected_ts - pd.Timedelta(days=2))).total_seconds()
        window_days = max(5, int(math.ceil(span_s / 86_400.0)))

        ibkr_df = self.data_client.fetch_historical_bars_by_duration(
            duration_str=f"{window_days} D",
            continuous=True,
            bar_size=self.bar_size,
            what_to_show="TRADES",
            use_rth=False,
        )
        if ibkr_df is None or ibkr_df.empty:
            log.warning(
                "resolve_roll_seam: IBKR returned no bars (%d D window) — "
                "RETRY.", window_days,
            )
            return ROLL_SEAM_RETRY
        ibkr_df = self._drop_incomplete_bar(ibkr_df)

        overlap = self._df.index.intersection(ibkr_df.index).sort_values()
        if len(overlap) == 0:
            log.warning(
                "resolve_roll_seam: no overlapping timestamps between cache "
                "and the CONTFUT fetch — RETRY.",
            )
            return ROLL_SEAM_RETRY

        cache_close = self._df.loc[overlap, "Close"].to_numpy(dtype=float)
        ibkr_close = ibkr_df.loc[overlap, "Close"].to_numpy(dtype=float)
        valid = cache_close > 0
        if not valid.all():
            log.warning(
                "resolve_roll_seam: dropping %d zero-Close cache bars from "
                "the scan.", int((~valid).sum()),
            )
            overlap = overlap[valid]
            cache_close = cache_close[valid]
            ibkr_close = ibkr_close[valid]
            if len(overlap) == 0:
                return ROLL_SEAM_RETRY

        q = ibkr_close / cache_close
        is_old = np.abs(q - 1.0) > self.roll_ratio_tolerance

        if is_old.all():
            log.info(
                "resolve_roll_seam: all %d overlap bars still old-basis "
                "(IBKR lead not flipped yet) — RETRY.", len(overlap),
            )
            return ROLL_SEAM_RETRY
        if not is_old.any():
            log.error(
                "resolve_roll_seam: all %d overlap bars already new-basis — "
                "no old-basis anchor for %s → %s. ESCALATE.",
                len(overlap), from_contract, to_contract,
            )
            return ROLL_SEAM_ESCALATE

        first_new = int(np.argmax(~is_old))
        if first_new == 0 or bool(is_old[first_new:].any()):
            # Not a clean old-run → new-run split (basis noise or an
            # unsettled stream). Retrying with fresh data is safer than
            # anchoring a wrong cutoff; the deadline escalation keeps this
            # loud if it never settles.
            log.warning(
                "resolve_roll_seam: overlap is not a clean old-run → "
                "new-run split (first_new=%d, %d old bars after it) — "
                "RETRY.", first_new, int(is_old[first_new:].sum()),
            )
            return ROLL_SEAM_RETRY

        ratio = float(np.median(q[:first_new]))
        cutoff = overlap[first_new]
        log.warning(
            "resolve_roll_seam: seam anchored for %s → %s: %d old-basis "
            "bars, ratio=%.6f, cutoff=%s.",
            from_contract, to_contract, first_new, ratio, cutoff,
        )
        self._append_roll_event(from_contract, to_contract, ratio, cutoff)
        return ROLL_SEAM_RESOLVED

    def _apply_roll_to_cache(self, ratio: float) -> None:
        """Record a rollover ratio and stitch IBKR overlap data into the cache.

        Does NOT multiply the cache — the cache stays 100% RAW.
        The ratio is stored and applied JIT by get_ratio_adjusted_df().

        Args:
            ratio: The multiplicative ratio (ibkr_price / cache_price).
        """
        if self._df is None or len(self._df) == 0:
            log.warning("Cannot apply roll — empty cache.")
            return

        # jit-roll-ratio-empty_07102026_1453 (Stage 2, Amendment 1): the
        # cutoff must be basis-consistent with the overlap overwrite below.
        # The old code recorded roll_ts = index.max() and THEN overwrote the
        # overlap window with fresh NEW-basis IBKR bars, so the JIT replay
        # multiplied those already-new-basis bars by the ratio again
        # (double-adjusted r² plateau + a spurious second seam step).
        # Convention chosen: cutoff = FIRST overwritten (new-basis) overlap
        # bar — consistent with both the overwrite here and _backfill's
        # dedup keep="last" overwrite of the same window. Fallback when
        # there is no IBKR overlap to stitch: index.max() (no overwrite
        # happens; the most-recent bar stays raw — legacy convention).
        ibkr_df = getattr(self, "_ibkr_overlap_df", None)
        overlap = None
        if ibkr_df is not None and len(ibkr_df) > 0:
            overlap = self._df.index.intersection(ibkr_df.index)
        if overlap is not None and len(overlap) > 0:
            roll_ts = overlap.min()
        else:
            roll_ts = self._df.index.max()
        self._roll_ratios.append(ratio)
        self._roll_timestamps.append(roll_ts)

        log.info(
            "ROLLOVER RECORDED: ratio=%.6f at %s  "
            "(total rollovers: %d)",
            ratio, roll_ts, len(self._roll_ratios),
        )

        # Overwrite overlapping bars with fresh IBKR data for a clean seam
        if ibkr_df is not None and len(ibkr_df) > 0:
            if overlap is not None and len(overlap) > 0:
                for col in ("Open", "High", "Low", "Close", "Volume"):
                    if col in ibkr_df.columns and col in self._df.columns:
                        self._df.loc[overlap, col] = ibkr_df.loc[overlap, col]
                log.info(
                    "Overwrote %d overlapping bars with fresh IBKR data.",
                    len(overlap),
                )
            del self._ibkr_overlap_df

        self.save_cache()

    def get_ratio_adjusted_df(self) -> pd.DataFrame:
        """Return a COPY of the cache with cumulative ratio adjustment applied.

        The raw cache contains unadjusted prices across multiple contract months.
        This method applies all accumulated rollover ratios to historical bars,
        producing a ratio-adjusted series where features are continuous.

        The most recent bar is always == raw price (ratio adjusts history only).
        """
        if self._df is None:
            raise RuntimeError("DataManager not initialized.")

        df = self._df.copy()
        if not self._roll_ratios:
            return df

        for ratio, roll_ts in zip(self._roll_ratios, self._roll_timestamps):
            mask = df.index < roll_ts
            for col in ("Open", "High", "Low", "Close"):
                if col in df.columns:
                    df.loc[mask, col] = df.loc[mask, col] * ratio

        return df

    def _full_rebuild_cache(self) -> None:
        """
        FALLBACK: Delete and rebuild cache from seed + IBKR backfill.

        WARNING: This requires IBKR to serve enough historical data to
        bridge the gap from the seed CSV to present.  IBKR's 5-min bar
        limit is ~60 days. If the gap exceeds this, the rebuild will fail.

        Full rebuilds reset all accumulated roll ratios.
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

        # Full rebuild resets all roll ratios
        self._roll_ratios = []
        self._roll_timestamps = []

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
            # seedless-5m-live-stream_07052026_0546 (audit §1.4): while
            # seedless, founding a ledger from a shallow IBKR fetch would
            # masquerade as training-grade history — skip LOUDLY instead.
            # The warm-start cache still accrues every raw bar + roll
            # metadata; the ledger founds itself the day a real seed lands.
            # 5m models (allow_shallow_bootstrap=False) keep the raise below.
            if self.allow_shallow_bootstrap and not self.seed_path.exists():
                log.warning(
                    "SHALLOW MODE: master-ledger accrual SKIPPED for %s — no "
                    "seed to found a training ledger (cache still accrues raw "
                    "bars + roll metadata). Provision %s to start the ledger.",
                    self.symbol, self.seed_path,
                )
                return
            # First run: create from seed CSV (full history)
            log.info("Creating master ledger from seed CSV (full file)...")
            ledger = self._load_full_seed()
            log.info(
                "Seed loaded for ledger: %d bars, range %s → %s",
                len(ledger), ledger.index.min(), ledger.index.max(),
            )

        current_ratio = self._roll_ratios[-1] if self._roll_ratios else 1.0
        if self._roll_detected and abs(current_ratio - 1.0) > self.roll_ratio_tolerance:
            # After rollover: apply ratio adjustment to ENTIRE ledger.
            # Every single row (back to 2008) gets the ratio applied.
            # This is the institutional standard — the model uses
            # relative features (ATR, MACD, returns).
            log.warning(
                "RATIO ADJUSTMENT: scaling ENTIRE ledger (%d bars) by %.6f",
                len(ledger), current_ratio,
            )
            for col in ("Open", "High", "Low", "Close"):
                if col in ledger.columns:
                    ledger[col] = ledger[col] * current_ratio
            log.info(
                "Ledger scaled: %d bars by %.6f. "
                "New range: $%.2f → $%.2f",
                len(ledger), current_ratio,
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
        """Load the entire seed file (not just the last N days)."""
        if not self.seed_path.exists():
            _alt = _PROJECT_ROOT / "data" / "raw" / self.seed_path.name
            raise FileNotFoundError(
                f"Seed file not found: {self.seed_path}\n"
                f"The {self.symbol} seed file must exist in one of:\n"
                f"  1. CL_DATA_ROOT env var location: "
                f"{os.environ.get('CL_DATA_ROOT', '(not set)')}\n"
                f"  2. Project-relative path: {_alt}\n"
                f"Set CL_DATA_ROOT or copy the file to fix this."
            )

        # ── Parquet seed ──────────────────────────────────────────────────
        if self.seed_path.suffix.lower() == ".parquet":
            df = pd.read_parquet(self.seed_path, engine="pyarrow")
            if "DateTime" not in df.columns:
                # Try resetting index if DateTime is the index
                df = df.reset_index()
            df["DateTime"] = pd.to_datetime(df["DateTime"])
            df = df[["DateTime", "Open", "High", "Low", "Close", "Volume"]]
            df = df.set_index("DateTime", drop=False)
            df.index.name = "DateTime"
            return df.sort_index()
        else:
            # ── Legacy CSV seed ───────────────────────────────────────────
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
            now_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)
            return now_utc - timedelta(days=730)

    def _fetch_ibkr_range(
        self, start_ts: pd.Timestamp
    ) -> Optional[pd.DataFrame]:
        """
        Fetch continuous contract bars from IBKR covering start_ts to now.

        Uses a single NOW-anchored request covering the whole range.
        """
        now = pd.Timestamp.now(tz="UTC").tz_localize(None)
        gap = now - start_ts
        if gap.total_seconds() < 600:
            log.info("Ledger gap < 10 minutes — no IBKR fetch needed.")
            return None

        # Continuous futures reject specific endDateTime values (IBKR 10339).
        # Use a single NOW-anchored request covering the whole range.
        gap_td = timedelta(seconds=gap.total_seconds())
        gap_days = max(1, int((gap_td.total_seconds() + 86_399) // 86_400))
        chunks = [f"{gap_days} D"]

        all_dfs = []
        for i, duration_str in enumerate(chunks):
            log.info(
                "Ledger fetch chunk %d/%d: %s ending NOW",
                i + 1, len(chunks), duration_str
            )
            df = self.data_client.fetch_historical_bars_by_duration(
                duration_str=duration_str,
                continuous=True,
                bar_size=self.bar_size,
                what_to_show="TRADES",
                use_rth=False,
            )
            
            if not df.empty:
                df = self._drop_incomplete_bar(df)
                if not df.empty:
                    all_dfs.append(df)

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
