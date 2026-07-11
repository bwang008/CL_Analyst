"""
backfill_roll_history — Stage 1 seed migration for empty roll_history files.

Ticket: jit-roll-ratio-empty_07102026_1453

Every deployed 1h model trained on RATIO-ADJUSTED HourSet data, but the live
fleet's ``.roll_metadata_<SYM>.json`` files all carry ``roll_history: []`` —
so ``DataManager.get_ratio_adjusted_df()`` is a no-op and inference runs on
the RAW stitched series. This script derives the missing per-roll ratios from
the quotient ``HourSet_close / raw_cache_close`` (piecewise-constant by
construction), converts them into the live replay convention (entry ``ratio``
multiplies bars ``index < timestamp_cutoff``), and writes them back into the
live metadata files with timestamped backups and mandatory pre-write
validation gates.

Entry schema (swap-proof, data_manager.py:358-367 legacy-restore branch):
entries carry NO ``"to"`` key so the restore loop applies them regardless of
execution symbol (CL child, MCL micro, post-swap symbols alike). Contract
labels ride under ``"to_contract"``; provenance under
``"origin": "seed_backfill_jit-roll-ratio-empty_07102026_1453"``.

Hard-fail policy (no-silent-null-defaults): every gate raises
``BackfillValidationError`` (a ``ValueError``) — never log-and-continue,
never a silently widened tolerance.

Pure, unit-tested core (tests/test_backfill_roll_history.py):
    derive_segments / segments_to_roll_entries / validate_replay /
    migrate_symbol
Operator CLI (``main``): per-symbol resolution of the deployed child's
HourSet from the live strategy config, Databento post-HourSet seam
derivation with a basis-identity gate, the documented one-time CL
``--cl-june-ratio`` estimate, ``--dry-run``, and the feature-level
spot-check gate.

ACTIVATION IS OPERATOR-GATED: ratios only restore at ``initialize()``.
Restart the NG child FIRST as the live canary and record the shadow-log
vs training-basis probability comparison into the ticket folder BEFORE any
fleet-wide restart. Rollback = restore the timestamped metadata backups +
restart.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd

# Allow "python scripts/backfill_roll_history.py" from anywhere: put the repo
# root on sys.path before the src imports (no-op under pytest/module runs).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.live_execution.data_manager import DataManager, derive_data_paths

log = logging.getLogger(__name__)

TICKET_ID = "jit-roll-ratio-empty_07102026_1453"

# Provenance stamp carried by every entry this migration writes. The
# idempotency gate keys off it, so it must never change.
ORIGIN_STAMP = "seed_backfill_" + TICKET_ID

# Bar-over-bar relative change that splits the adjusted/raw quotient into a
# new segment. Real roll seams are >= ~0.2% (CL 0.2-0.9%, NG up to 31%);
# within-segment float noise is <= ~1e-5 — 0.1% separates them by two
# orders of magnitude in both directions, so noise can never shatter into
# micro-segments and no real seam can hide.
SEAM_REL_THRESHOLD = 1e-3

# Within-segment coefficient-of-variation hard-fail bound. Observed real
# value on the live files is ~2e-8; anything >= 1e-6 means the quotient is
# NOT piecewise-constant and the derivation is invalid (hard fail — the
# tolerance is never widened to "make it pass").
SEGMENT_CV_LIMIT = 1e-6

# Replay-equality gate: max |adjusted/reference/expected_scale - 1| must be
# below this on ALL overlap timestamps. Pinned by the test contract at 1e-9
# (full-precision ratios; 6dp rounding fails this on purpose).
REPLAY_REL_TOL = 1e-9

# validate_replay refuses tolerances wider than the blueprint's own outer
# bound — a relaxation past this is a design change, not an operator knob.
_REPLAY_REL_TOL_CEILING = 1e-6

# Heuristic bar-over-bar jump that flags an UNCOVERED roll seam in the raw
# cache window the reference does not reach. 1% sits between smooth hourly
# moves in the fixtures (<0.05%) and the fixture seam (5%). NOTE: real
# sub-1% roll gaps in the uncovered tail are NOT reliably separable from
# ordinary hourly volatility at this resolution — the authoritative
# post-HourSet coverage comes from the seam SOURCES (Databento / the CL
# IBKR estimate), not from this net. Operator-overridable via the CLI.
TAIL_SEAM_REL_THRESHOLD = 0.01

# Feature-level spot-check gate defaults (blueprint 5c).
DEFAULT_SPOTCHECK_FEATURES = ("TS_VOL_YZ_ZSCORE_72v840",)
_SPOTCHECK_MIN_ROWS = 200
_SPOTCHECK_RTOL = 1e-4   # "within float32 tolerance" — float32 eps ~1.2e-7,
_SPOTCHECK_ATOL = 1e-5   # widened for accumulated rolling-window arithmetic.

_FLEET_SYMBOLS = ("CL", "ES", "NG", "GC", "SI")


class BackfillValidationError(ValueError):
    """Any hard-fail gate in the backfill migration (ValueError subclass)."""


class BackfillIdempotencyError(BackfillValidationError):
    """Second run detected existing origin-stamped entries — loud refusal."""


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

def _check_close_series(series: pd.Series, name: str) -> pd.Series:
    """Validate and normalize a Close series (sorted DatetimeIndex, finite)."""
    if not isinstance(series, pd.Series):
        raise BackfillValidationError(
            f"{name} must be a pandas Series, got {type(series).__name__}."
        )
    if not isinstance(series.index, pd.DatetimeIndex):
        raise BackfillValidationError(
            f"{name} must have a DatetimeIndex, got "
            f"{type(series.index).__name__}."
        )
    if len(series) == 0:
        raise BackfillValidationError(f"{name} is empty — nothing to derive.")
    if series.index.has_duplicates:
        raise BackfillValidationError(
            f"{name} has duplicate timestamps — dedup the source before "
            "running the migration (a duplicated bar makes the quotient "
            "alignment ambiguous)."
        )
    out = series.sort_index()
    vals = out.to_numpy(dtype=np.float64)
    if not np.all(np.isfinite(vals)):
        raise BackfillValidationError(
            f"{name} contains NaN/inf Close values — refusing to derive "
            "ratios from a corrupt series (no silent dropna)."
        )
    if np.any(vals <= 0.0):
        raise BackfillValidationError(
            f"{name} contains non-positive Close values — multiplicative "
            "ratio derivation is undefined."
        )
    return out


def _load_cache_close(cache_path: Path) -> pd.Series:
    """Load the live warm-start cache parquet and return its Close series."""
    if not cache_path.exists():
        raise BackfillValidationError(
            f"Live cache not found at {cache_path}. Resolve the per-symbol "
            "cache path (derive_data_paths) and verify CL_DATA_ROOT before "
            "running the migration."
        )
    df = pd.read_parquet(str(cache_path), engine="pyarrow")
    for col in ("DateTime", "Close"):
        if col not in df.columns:
            raise BackfillValidationError(
                f"Cache file {cache_path} is missing the '{col}' column — "
                "not a live warm-start cache."
            )
    df["DateTime"] = pd.to_datetime(df["DateTime"])
    close = pd.Series(
        df["Close"].to_numpy(dtype=np.float64),
        index=pd.DatetimeIndex(df["DateTime"]),
        name="Close",
    )
    return _check_close_series(close, f"raw cache Close ({cache_path.name})")


def _load_metadata_strict(metadata_path: Path) -> dict:
    """Load the roll metadata JSON; hard fail on anything malformed."""
    if not metadata_path.exists():
        raise BackfillValidationError(
            f"Roll metadata file not found at {metadata_path}. The Stage 1 "
            "migration only amends PRE-EXISTING live metadata files — "
            "create/verify the file (DataManager writes it at startup) "
            "before migrating."
        )
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise BackfillValidationError(
            f"Could not parse roll metadata at {metadata_path}: {exc}"
        ) from exc
    if not isinstance(meta, dict):
        raise BackfillValidationError(
            f"Roll metadata at {metadata_path} is not a JSON object."
        )
    if "roll_history" not in meta:
        raise BackfillValidationError(
            f"Roll metadata at {metadata_path} has no 'roll_history' key — "
            "refusing to guess (no silent null default). Add "
            '"roll_history": [] explicitly if this file is intended.'
        )
    if not isinstance(meta["roll_history"], list):
        raise BackfillValidationError(
            f"'roll_history' in {metadata_path} is not a list."
        )
    return meta


# ---------------------------------------------------------------------------
# 1. Quotient segmentation (pure)
# ---------------------------------------------------------------------------

def derive_segments(raw_close: pd.Series, adjusted_close: pd.Series) -> list:
    """Segment the adjusted/raw Close quotient into piecewise-constant runs.

    Operates on the TIMESTAMP INTERSECTION of the two series (the raw live
    cache extends beyond the training HourSet). Runs are grouped with the
    coarse ``SEAM_REL_THRESHOLD`` (real seams >= ~0.2%) so float noise can
    never shatter into micro-segments; instead, a noisy quotient trips the
    within-segment CV hard-fail gate.

    Returns:
        list[dict]: oldest -> newest, each with keys ``factor`` (float,
        constant quotient), ``start`` (pd.Timestamp, first bar of the
        segment), plus ``end``, ``n_bars`` and ``cv`` diagnostics.

    Raises:
        BackfillValidationError: empty intersection, corrupt inputs, or any
            within-segment coefficient of variation >= 1e-6 (hard fail — the
            quotient is not piecewise-constant, so the derivation is invalid).
    """
    raw = _check_close_series(raw_close, "raw_close")
    adj = _check_close_series(adjusted_close, "adjusted_close")

    common = raw.index.intersection(adj.index).sort_values()
    if len(common) == 0:
        raise BackfillValidationError(
            "raw_close and adjusted_close share no timestamps — cannot "
            "derive a quotient. Check bar alignment / timezone handling."
        )

    q = adj.loc[common].to_numpy(dtype=np.float64) / raw.loc[common].to_numpy(
        dtype=np.float64
    )

    # Segment boundaries: bar-over-bar relative jump above the seam floor.
    rel_step = np.abs(q[1:] / q[:-1] - 1.0)
    seam_starts = np.where(rel_step > SEAM_REL_THRESHOLD)[0] + 1
    bounds = [0] + list(seam_starts) + [len(q)]

    segments: list[dict] = []
    for b0, b1 in zip(bounds[:-1], bounds[1:]):
        vals = q[b0:b1]
        mean = float(np.mean(vals))
        cv = float(np.std(vals, ddof=0) / abs(mean))
        if cv >= SEGMENT_CV_LIMIT:
            raise BackfillValidationError(
                f"Within-segment quotient CV {cv:.3e} >= {SEGMENT_CV_LIMIT} "
                f"for segment starting {common[b0]} ({b1 - b0} bars) — the "
                "adjusted/raw quotient is NOT piecewise-constant, so the "
                "roll-ratio derivation is invalid for this source pair. "
                "Do NOT widen this tolerance; fix the source data instead "
                "(no-silent-null-defaults)."
            )
        segments.append(
            {
                "factor": float(np.median(vals)),
                "start": pd.Timestamp(common[b0]),
                "end": pd.Timestamp(common[b1 - 1]),
                "n_bars": int(b1 - b0),
                "cv": cv,
            }
        )
    return segments


# ---------------------------------------------------------------------------
# 2. Segments -> roll_history entries (pure)
# ---------------------------------------------------------------------------

def segments_to_roll_entries(
    segments: list, from_label: str, to_contract_label: str
) -> list:
    """Convert K+1 quotient segments into K replay-convention roll entries.

    ``get_ratio_adjusted_df()`` multiplies bars ``index < timestamp_cutoff``
    by ``ratio``, so for factors f_0..f_K (oldest -> newest) the seam between
    segments k-1 and k gets ``ratio = f_{k-1} / f_k`` at FULL float precision
    (6dp rounding fails the 1e-9 replay gate by design) and
    ``timestamp_cutoff`` = first bar of segment k.

    Entries deliberately carry NO ``"to"`` key: the legacy restore branch
    (data_manager.py:358-367) then restores them unconditionally, keeping the
    adjustment execution-symbol independent (CL/MCL/post-swap safe). The
    contract label rides under ``"to_contract"`` instead.
    """
    if not isinstance(segments, list) or len(segments) == 0:
        raise BackfillValidationError(
            "segments must be a non-empty list (derive_segments output)."
        )
    factors: list[float] = []
    starts: list[pd.Timestamp] = []
    for i, seg in enumerate(segments):
        try:
            factor = float(seg["factor"])
            start = pd.Timestamp(seg["start"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BackfillValidationError(
                f"segment #{i} lacks a usable 'factor'/'start': {exc}"
            ) from exc
        if not np.isfinite(factor) or factor <= 0.0:
            raise BackfillValidationError(
                f"segment #{i} factor {factor!r} is not a positive finite "
                "float — ratio derivation is undefined."
            )
        factors.append(factor)
        starts.append(start)

    run_ts = datetime.now().isoformat()
    entries: list[dict] = []
    for k in range(1, len(segments)):
        entries.append(
            {
                "from": from_label,
                "to_contract": to_contract_label,
                "ratio": float(factors[k - 1] / factors[k]),
                "timestamp": run_ts,
                "timestamp_cutoff": starts[k].isoformat(),
                "origin": ORIGIN_STAMP,
            }
        )
    return entries


# ---------------------------------------------------------------------------
# 3. Replay-equality validation gate (through the REAL DataManager)
# ---------------------------------------------------------------------------

def _replay_adjusted_df(
    *,
    symbol: str,
    cache_path: Path,
    roll_metadata_path: Path,
    bar_size: str,
    bars_per_day: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the REAL DataManager restore+replay; return (adjusted, raw) frames.

    data_client=None + front_month_id=None keeps initialize() to: restore
    roll_history -> load cache -> return (no IBKR, no metadata write). The
    seed/ledger paths are explicit never-touched placeholders so nothing can
    resolve to the real data root.
    """
    cache_path = Path(cache_path)
    roll_metadata_path = Path(roll_metadata_path)
    if not cache_path.exists():
        raise BackfillValidationError(
            f"validate_replay: cache not found at {cache_path}."
        )
    if not roll_metadata_path.exists():
        raise BackfillValidationError(
            f"validate_replay: roll metadata not found at {roll_metadata_path}."
        )

    dm = DataManager(
        symbol=symbol,
        data_client=None,
        seed_path=str(cache_path.parent / "_backfill_unused_seed.parquet"),
        cache_path=str(cache_path),
        master_ledger_path=str(
            cache_path.parent / "_backfill_unused_ledger.parquet"
        ),
        roll_metadata_path=str(roll_metadata_path),
        bar_size=bar_size,
        bars_per_day=bars_per_day,
    )
    dm.initialize()
    adj = dm.get_ratio_adjusted_df()
    if adj.index.has_duplicates:
        raise BackfillValidationError(
            f"Replayed cache for {symbol} has duplicate timestamps — dedup "
            "the cache before migrating."
        )
    return adj, dm._df


def validate_replay(
    *,
    symbol: str,
    cache_path,
    roll_metadata_path,
    reference_close: pd.Series,
    expected_scale: float = 1.0,
    rel_tol: float = REPLAY_REL_TOL,
    bar_size: str = "1 hour",
    bars_per_day: int = 24,
) -> None:
    """PROVE the written ratios reproduce the training basis via live replay.

    Constructs the real ``DataManager`` (data_client=None) against the given
    cache/metadata paths, runs ``initialize()`` + ``get_ratio_adjusted_df()``
    and asserts:
      (a) ``adjusted_close / reference_close`` is constant at
          ``expected_scale`` (default 1.0) across ALL overlap timestamps with
          max relative deviation < ``rel_tol`` (1e-9), and
      (b) the final (newest) bar is unchanged vs raw (adjustment touches
          history only).

    This is the load-bearing direction proof: an inverted ratio
    (f_new/f_prev) scales history the wrong way and fails (a) — direction is
    never assumed from the formula.

    ``expected_scale`` != 1.0 is the CLI's post-HourSet case: entries whose
    cutoff lies AFTER the reference end scale the whole overlap by their
    product (blueprint gate 5a "equals the product of post-HourSet per-roll
    ratios"). The unit-tested contract (no post-reference entries) keeps the
    default 1.0.

    Returns None on success; raises BackfillValidationError (ValueError) on
    ANY failure.
    """
    reference = _check_close_series(reference_close, "reference_close")
    expected_scale = float(expected_scale)
    if not np.isfinite(expected_scale) or expected_scale <= 0.0:
        raise BackfillValidationError(
            f"expected_scale {expected_scale!r} must be a positive finite "
            "float."
        )
    rel_tol = float(rel_tol)
    if not (0.0 < rel_tol <= _REPLAY_REL_TOL_CEILING):
        raise BackfillValidationError(
            f"rel_tol {rel_tol!r} outside (0, {_REPLAY_REL_TOL_CEILING}] — "
            "the replay gate tolerance is not an open-ended knob "
            "(no-silent-null-defaults)."
        )

    adj, raw_df = _replay_adjusted_df(
        symbol=symbol,
        cache_path=Path(cache_path),
        roll_metadata_path=Path(roll_metadata_path),
        bar_size=bar_size,
        bars_per_day=bars_per_day,
    )

    missing = reference.index.difference(adj.index)
    if len(missing) > 0:
        raise BackfillValidationError(
            f"Replay coverage failure for {symbol}: {len(missing)} reference "
            f"timestamps missing from the cache (first: {missing[0]}). The "
            "replay must cover ALL overlap timestamps."
        )

    q = adj.loc[reference.index, "Close"].to_numpy(
        dtype=np.float64
    ) / reference.to_numpy(dtype=np.float64)
    dev = np.abs(q / expected_scale - 1.0)
    max_dev = float(np.max(dev))
    if not np.isfinite(max_dev) or max_dev >= rel_tol:
        worst = reference.index[int(np.argmax(dev))]
        raise BackfillValidationError(
            f"Replay-equality gate FAILED for {symbol}: max relative "
            f"deviation of adjusted/reference vs expected scale "
            f"{expected_scale} is {max_dev:.3e} >= {rel_tol} (worst bar "
            f"{worst}). Ratio value, DIRECTION, or cutoff is wrong — do not "
            "write these entries."
        )

    raw_last = float(raw_df["Close"].iloc[-1])
    adj_last = float(adj["Close"].iloc[-1])
    if abs(adj_last - raw_last) > abs(raw_last) * 1e-12:
        raise BackfillValidationError(
            f"Final-bar invariant FAILED for {symbol}: newest adjusted bar "
            f"{adj_last!r} != raw {raw_last!r}. Ratio adjustment must touch "
            "HISTORY only — a cutoff at/after the newest bar is invalid."
        )
    return None


# ---------------------------------------------------------------------------
# Coverage-scan helpers (pure)
# ---------------------------------------------------------------------------

def _detect_seams_in_window(close: pd.Series, threshold: float) -> list:
    """Timestamps of the FIRST bar after each bar-over-bar jump > threshold."""
    vals = close.to_numpy(dtype=np.float64)
    if len(vals) < 2:
        return []
    rel = np.abs(vals[1:] / vals[:-1] - 1.0)
    return [pd.Timestamp(close.index[i + 1]) for i in np.where(rel > threshold)[0]]


def _validate_extra_entries(extra_entries: Optional[Iterable[dict]]) -> list:
    """Strict schema check for CLI-supplied post-reference entries."""
    if extra_entries is None:
        return []
    required = {"from", "to_contract", "ratio", "timestamp",
                "timestamp_cutoff", "origin"}
    validated: list[dict] = []
    for i, entry in enumerate(extra_entries):
        if not isinstance(entry, dict):
            raise BackfillValidationError(
                f"extra_entries[{i}] is not a dict."
            )
        if "to" in entry:
            raise BackfillValidationError(
                f'extra_entries[{i}] carries a "to" key — backfill entries '
                "must be swap-proof (no-'to' schema)."
            )
        missing = required - set(entry.keys())
        if missing:
            raise BackfillValidationError(
                f"extra_entries[{i}] missing required keys: {sorted(missing)}."
            )
        if entry["origin"] != ORIGIN_STAMP:
            raise BackfillValidationError(
                f"extra_entries[{i}] origin {entry['origin']!r} != "
                f"{ORIGIN_STAMP!r} — every entry this migration writes must "
                "carry the ticket origin stamp (idempotency depends on it)."
            )
        ratio = entry["ratio"]
        if not isinstance(ratio, float) or not np.isfinite(ratio) or ratio <= 0:
            raise BackfillValidationError(
                f"extra_entries[{i}] ratio {ratio!r} must be a positive "
                "finite float."
            )
        pd.Timestamp(entry["timestamp_cutoff"])  # must parse (raises)
        validated.append(entry)
    return validated


def _check_uncovered_windows(
    raw_close: pd.Series,
    reference: pd.Series,
    covered_cutoffs: set,
    tail_seam_threshold: float,
    seam_match_tolerance: pd.Timedelta,
    symbol: str,
) -> None:
    """HARD FAIL if the raw series has a seam the reference+extras don't cover.

    Partial history = a silent basis break mid-window: a seam in the raw
    cache before/after the reference window that no entry adjusts would leave
    part of the replayed series on the wrong basis without the replay gate
    (which only sees the reference overlap) noticing.
    """
    ref_start = reference.index.min()
    ref_end = reference.index.max()

    def _is_covered(ts: pd.Timestamp) -> bool:
        if ts in covered_cutoffs:
            return True
        if seam_match_tolerance > pd.Timedelta(0):
            return any(abs(ts - c) <= seam_match_tolerance
                       for c in covered_cutoffs)
        return False

    head = raw_close.loc[raw_close.index <= ref_start]
    head_seams = _detect_seams_in_window(head, tail_seam_threshold)
    if head_seams:
        raise BackfillValidationError(
            f"Uncovered roll seam(s) BEFORE the reference start for {symbol} "
            f"at {[str(t) for t in head_seams]} — the reference does not "
            "reach them and pre-reference extras are unsupported. Extend the "
            "reference or trim the cache head; refusing a partial history."
        )

    tail = raw_close.loc[raw_close.index >= ref_end]
    tail_seams = _detect_seams_in_window(tail, tail_seam_threshold)
    uncovered = [ts for ts in tail_seams if not _is_covered(ts)]
    if uncovered:
        raise BackfillValidationError(
            f"Uncovered roll seam(s) AFTER the reference end ({ref_end}) for "
            f"{symbol} at {[str(t) for t in uncovered]} — partial coverage "
            "is a silent basis break mid-window. Supply the post-reference "
            "seam source (Databento ratio/raw derivation, or --cl-june-ratio "
            "for CL) so every seam between seed start and NOW is covered."
        )


# ---------------------------------------------------------------------------
# 4. Orchestrated per-symbol migration (the ONLY writer)
# ---------------------------------------------------------------------------

def migrate_symbol(
    *,
    symbol: str,
    cache_path,
    metadata_path,
    reference_close: pd.Series,
    from_label: Optional[str] = None,
    to_contract_label: Optional[str] = None,
    extra_entries: Optional[list] = None,
    dry_run: bool = False,
    replay_rel_tol: float = REPLAY_REL_TOL,
    tail_seam_threshold: float = TAIL_SEAM_REL_THRESHOLD,
    seam_match_tolerance: Optional[pd.Timedelta] = None,
    feature_check: Optional[Callable[[pd.DataFrame], None]] = None,
    bar_size: str = "1 hour",
    bars_per_day: int = 24,
) -> None:
    """Backfill one symbol's roll_history from its adjusted training reference.

    Pipeline: load raw cache -> coverage hard-fail scan -> derive_segments ->
    entries via the MODULE-LEVEL ``segments_to_roll_entries`` (patchable
    seam) -> validate against SCRATCH copies (real DataManager replay +
    optional ``feature_check`` on the adjusted frame) -> timestamped backup
    -> atomic write -> post-write re-validation.

    Guarantees:
      * ABORT-BEFORE-WRITE: any gate failure leaves the real target metadata
        file byte-for-byte unchanged (all validation runs on scratch copies).
      * BACKUP-BEFORE-WRITE: the pre-migration file is copied to a
        timestamped sibling whose name contains "roll_metadata".
      * Unrelated keys (last_front_month, last_front_month_by_symbol,
        updated_at, ...) are preserved untouched; ``cumulative_ratio`` is
        recomputed as the product of all entry ratios (informational only,
        blueprint step 4).
      * IDEMPOTENT: existing origin-stamped entries raise
        BackfillIdempotencyError before anything else happens.

    Args beyond the fixed four:
        from_label / to_contract_label: entry labels. Defaults mirror
            data_manager conventions — "unknown" for ``from`` (the
            _save_roll_metadata fallback, data_manager.py:882-883) and the
            stored front month for ``to_contract`` (raises if unresolvable —
            pass it explicitly, no silent default).
        extra_entries: pre-built origin-stamped entries covering seams AFTER
            the reference end (CLI: Databento derivation / CL IBKR estimate).
        dry_run: run every gate, write nothing.
        feature_check: callable given the scratch-validated ADJUSTED frame;
            raise to abort (blueprint gate 5c).
    """
    cache_path = Path(cache_path)
    metadata_path = Path(metadata_path)
    if seam_match_tolerance is None:
        seam_match_tolerance = pd.Timedelta(0)

    meta = _load_metadata_strict(metadata_path)
    history = meta["roll_history"]

    # Idempotency FIRST — before any backup/scratch/derivation side effects.
    stamped = [e for e in history
               if isinstance(e, dict) and e.get("origin") == ORIGIN_STAMP]
    if stamped:
        raise BackfillIdempotencyError(
            f"{metadata_path} already contains {len(stamped)} entries with "
            f"origin {ORIGIN_STAMP!r} — the migration already ran for "
            f"{symbol}. Refusing to duplicate. Rollback = restore the "
            "timestamped backup, then re-run."
        )

    raw_close = _load_cache_close(cache_path)
    reference = _check_close_series(reference_close, "reference_close")

    # Labels (explicit args win; defaults mirror data_manager conventions).
    if from_label is None:
        from_label = "unknown"
    if to_contract_label is None:
        by_symbol = meta.get("last_front_month_by_symbol") or {}
        candidate = by_symbol.get(symbol)
        if not isinstance(candidate, str):
            legacy = meta.get("last_front_month")
            candidate = (
                legacy
                if isinstance(legacy, str) and legacy.startswith(symbol)
                else None
            )
        if candidate is None:
            raise BackfillValidationError(
                f"Cannot resolve a to_contract label for {symbol} from "
                f"{metadata_path} (no last_front_month[_by_symbol] entry) — "
                "pass to_contract_label explicitly (no silent default)."
            )
        to_contract_label = candidate

    extra = _validate_extra_entries(extra_entries)
    covered_cutoffs = {pd.Timestamp(e["timestamp_cutoff"]) for e in extra}

    # COVERAGE HARD-FAIL: seams outside the reference window must be covered
    # by extra entries or the whole symbol is refused BEFORE anything writes.
    _check_uncovered_windows(
        raw_close,
        reference,
        covered_cutoffs,
        tail_seam_threshold,
        seam_match_tolerance,
        symbol,
    )

    segments = derive_segments(raw_close, reference)
    # Module-level lookup on purpose: this is the sanctioned patchable seam.
    entries = segments_to_roll_entries(segments, from_label, to_contract_label)

    all_new = sorted(
        list(entries) + extra, key=lambda e: pd.Timestamp(e["timestamp_cutoff"])
    )
    if not all_new:
        raise BackfillValidationError(
            f"No roll seams derived for {symbol} (reference quotient is a "
            "single segment and no extra entries were supplied) — refusing "
            "an empty migration write."
        )

    candidate = copy.deepcopy(meta)
    candidate["roll_history"] = list(history) + all_new
    ratios = [
        float(e["ratio"])
        for e in candidate["roll_history"]
        if isinstance(e, dict) and "ratio" in e
    ]
    # Informational only (no runtime consumer) — recomputed as the product
    # per blueprint step 4, full precision.
    candidate["cumulative_ratio"] = float(np.prod(ratios)) if ratios else 1.0

    ref_end = reference.index.max()
    post_ref_ratios = [
        float(e["ratio"])
        for e in all_new
        if pd.Timestamp(e["timestamp_cutoff"]) > ref_end
    ]
    expected_scale = float(np.prod(post_ref_ratios)) if post_ref_ratios else 1.0

    # ---- MANDATORY validation against SCRATCH copies (abort-before-write) --
    with tempfile.TemporaryDirectory(prefix="backfill_roll_scratch_") as td:
        scratch_dir = Path(td)
        scratch_cache = scratch_dir / cache_path.name
        scratch_meta = scratch_dir / metadata_path.name
        shutil.copy2(str(cache_path), str(scratch_cache))
        with open(scratch_meta, "w", encoding="utf-8") as f:
            json.dump(candidate, f, indent=2)

        validate_replay(
            symbol=symbol,
            cache_path=scratch_cache,
            roll_metadata_path=scratch_meta,
            reference_close=reference,
            expected_scale=expected_scale,
            rel_tol=replay_rel_tol,
            bar_size=bar_size,
            bars_per_day=bars_per_day,
        )

        if feature_check is not None:
            adj, _ = _replay_adjusted_df(
                symbol=symbol,
                cache_path=scratch_cache,
                roll_metadata_path=scratch_meta,
                bar_size=bar_size,
                bars_per_day=bars_per_day,
            )
            feature_check(adj)  # raises BackfillValidationError to abort

    if dry_run:
        log.info(
            "[DRY-RUN] %s: %d entries validated (replay gate + coverage "
            "passed, expected overlap scale %.12g) — nothing written to %s.",
            symbol, len(all_new), expected_scale, metadata_path,
        )
        for e in all_new:
            log.info(
                "[DRY-RUN] %s entry: cutoff=%s ratio=%.12g",
                symbol, e["timestamp_cutoff"], e["ratio"],
            )
        return None

    # ---- BACKUP-BEFORE-WRITE (timestamped, same directory) -----------------
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = metadata_path.parent / (
        f"{metadata_path.stem}_backup_{stamp}{metadata_path.suffix}"
    )
    if "roll_metadata" not in backup_path.name:
        # Defensive: the rollback contract keys off this substring.
        backup_path = metadata_path.parent / (
            f"roll_metadata_backup_{symbol}_{stamp}.json"
        )
    shutil.copy2(str(metadata_path), str(backup_path))
    _load_metadata_strict(backup_path)  # backup must be a readable copy

    # ---- Atomic write of the real target -----------------------------------
    fd, tmp_name = tempfile.mkstemp(
        suffix=".json", dir=str(metadata_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(candidate, f, indent=2)
        os.replace(tmp_name, str(metadata_path))
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    # ---- Post-write re-validation against the REAL files -------------------
    try:
        validate_replay(
            symbol=symbol,
            cache_path=cache_path,
            roll_metadata_path=metadata_path,
            reference_close=reference,
            expected_scale=expected_scale,
            rel_tol=replay_rel_tol,
            bar_size=bar_size,
            bars_per_day=bars_per_day,
        )
    except ValueError as exc:
        raise BackfillValidationError(
            f"POST-WRITE replay validation failed for {symbol} after the "
            f"target was written — restore the backup at {backup_path} "
            f"immediately. Underlying failure: {exc}"
        ) from exc

    log.info(
        "MIGRATED %s: %d roll_history entries written to %s "
        "(backup: %s, cumulative_ratio=%.12g).",
        symbol, len(all_new), metadata_path, backup_path.name,
        candidate["cumulative_ratio"],
    )
    return None


# ===========================================================================
# Operator CLI (blueprint Stage 1 items 1, 3, 5c, 7 — NOT unit-tested here;
# activation is operator-gated and must never run unattended)
# ===========================================================================

def _resolve_dataset_path_from_strategy_config(strategy_cfg: dict,
                                               cfg_path: Path) -> Path:
    """Resolve the deployed child's training HourSet from its model registry.

    Follows each model leg's ``model_path`` to its registry folder and reads
    the dataset reference from ``experiment_config.json`` (``data_path``) or
    ``config.json`` (``dataset_path``) — never a hardcoded filename. All legs
    must agree on ONE dataset.
    """
    models = strategy_cfg.get("models")
    if not isinstance(models, dict) or not models:
        raise BackfillValidationError(
            f"Strategy config {cfg_path} has no 'models' block — cannot "
            "resolve the training dataset."
        )
    resolved: set = set()
    for leg, leg_cfg in models.items():
        model_path = leg_cfg.get("model_path")
        if not model_path:
            raise BackfillValidationError(
                f"Strategy config {cfg_path} leg '{leg}' has no model_path."
            )
        registry_dir = (_REPO_ROOT / model_path).parent
        dataset: Optional[str] = None
        exp_cfg = registry_dir / "experiment_config.json"
        if exp_cfg.exists():
            with open(exp_cfg, "r", encoding="utf-8") as f:
                dataset = json.load(f).get("data_path")
        if dataset is None:
            model_cfg = registry_dir / "config.json"
            if model_cfg.exists():
                with open(model_cfg, "r", encoding="utf-8") as f:
                    dataset = json.load(f).get("dataset_path")
        if dataset is None:
            raise BackfillValidationError(
                f"No dataset reference (experiment_config.json:data_path or "
                f"config.json:dataset_path) beside model {registry_dir} — "
                "cannot resolve the training HourSet (no hardcoded fallback)."
            )
        resolved.add(str(Path(dataset)))
    if len(resolved) != 1:
        raise BackfillValidationError(
            f"Model legs in {cfg_path} reference DIFFERENT datasets: "
            f"{sorted(resolved)} — refusing to pick one."
        )
    dataset_path = Path(next(iter(resolved)))
    if not dataset_path.exists():
        raise BackfillValidationError(
            f"Resolved dataset path {dataset_path} does not exist. Verify "
            "CL_DATA_ROOT and that the HourSet parquet is present."
        )
    return dataset_path


def _load_hourset(dataset_path: Path) -> pd.DataFrame:
    """Load a training HourSet parquet with a DateTime index."""
    df = pd.read_parquet(str(dataset_path), engine="pyarrow")
    if "DateTime" in df.columns:
        df["DateTime"] = pd.to_datetime(df["DateTime"])
        df = df.set_index("DateTime", drop=False)
    elif not isinstance(df.index, pd.DatetimeIndex):
        raise BackfillValidationError(
            f"HourSet {dataset_path} has neither a DateTime column nor a "
            "DatetimeIndex."
        )
    if "Close" not in df.columns:
        raise BackfillValidationError(
            f"HourSet {dataset_path} has no Close column."
        )
    return df.sort_index()


def _load_databento_close(csv_path: Path) -> pd.Series:
    """Load a Databento hourly CSV (semicolon, MM/DD/YYYY;HH:MM, no header)."""
    if not csv_path.exists():
        raise BackfillValidationError(
            f"Databento file not found at {csv_path} — post-HourSet seam "
            "coverage for this symbol requires it."
        )
    df = pd.read_csv(
        str(csv_path),
        sep=";",
        header=None,
        names=["Date", "Time", "Open", "High", "Low", "Close", "Volume"],
    )
    # pandas 1.5.3 (trader env): explicit format, errors='raise' — a silent
    # NaT here would fake an empty overlap downstream.
    idx = pd.to_datetime(
        df["Date"] + " " + df["Time"], format="%m/%d/%Y %H:%M", errors="raise"
    )
    close = pd.Series(
        df["Close"].to_numpy(dtype=np.float64), index=pd.DatetimeIndex(idx),
        name="Close",
    )
    if close.index.has_duplicates:
        close = close[~close.index.duplicated(keep="last")]
    return _check_close_series(close, f"Databento Close ({csv_path.name})")


def _derive_post_reference_entries_databento(
    symbol: str,
    hourset_close: pd.Series,
    from_label: str,
    to_contract_label: str,
) -> list:
    """Post-HourSet entries from Databento ratio÷raw, basis-gated first.

    Gate (blueprint step 3): the ratio.csv basis must be IDENTICAL to the
    HourSet basis on their overlap — the segmented-quotient test between the
    two adjusted series must return exactly ONE segment (a constant global
    factor; different anchor dates are fine, extra seams are not).
    """
    from src.data_paths import get_data_root

    db_dir = get_data_root() / "raw" / "DataBento" / symbol
    db_raw = _load_databento_close(db_dir / f"{symbol}_raw.csv")
    db_ratio = _load_databento_close(db_dir / f"{symbol}_ratio.csv")

    basis_segments = derive_segments(db_ratio, hourset_close)
    if len(basis_segments) != 1:
        raise BackfillValidationError(
            f"Databento basis-identity gate FAILED for {symbol}: "
            f"HourSet/ratio.csv quotient has {len(basis_segments)} segments "
            "on the overlap (expected exactly 1 constant segment) — the two "
            "adjusted series disagree on seam placement; do not derive "
            "post-HourSet seams from this source."
        )

    hs_end = hourset_close.index.max()
    raw_tail = db_raw.loc[db_raw.index >= hs_end]
    ratio_tail = db_ratio.loc[db_ratio.index >= hs_end]
    if len(raw_tail) < 2 or len(ratio_tail) < 2:
        return []
    tail_segments = derive_segments(raw_tail, ratio_tail)
    return segments_to_roll_entries(tail_segments, from_label,
                                    to_contract_label)


def _make_feature_check(
    hourset_df: pd.DataFrame,
    feature_cols: list,
    symbol: str,
    bar_size_short: str = "1h",
) -> Callable[[pd.DataFrame], None]:
    """Blueprint gate 5c: adjusted-frame features must reproduce the HourSet.

    ``build_live_features`` on the ratio-adjusted frame must match the
    HourSet's STORED feature columns on overlap timestamps within float32
    tolerance — at minimum TS_VOL_YZ_ZSCORE_72v840 for NG.
    """

    def _check(adjusted_df: pd.DataFrame) -> None:
        from src.core.instrument_master import get_instrument
        from src.live_execution.feature_pipeline import build_live_features

        feats = build_live_features(
            adjusted_df,
            feature_names=list(feature_cols),
            lean=False,
            bar_size=bar_size_short,
            return_last_n=len(adjusted_df),
            instrument=get_instrument(symbol),
        )
        if feats is None:
            raise BackfillValidationError(
                f"Feature spot-check for {symbol}: build_live_features "
                "returned None (insufficient bars?) — gate 5c cannot pass."
            )
        overlap = feats.index.intersection(hourset_df.index)
        for col in feature_cols:
            if col not in feats.columns:
                raise BackfillValidationError(
                    f"Feature spot-check for {symbol}: live pipeline did not "
                    f"produce '{col}'."
                )
            live = feats.loc[overlap, col].to_numpy(dtype=np.float64)
            stored = hourset_df.loc[overlap, col].to_numpy(dtype=np.float64)
            both = np.isfinite(live) & np.isfinite(stored)
            n = int(both.sum())
            if n < _SPOTCHECK_MIN_ROWS:
                raise BackfillValidationError(
                    f"Feature spot-check for {symbol}/'{col}': only {n} "
                    f"comparable overlap rows (< {_SPOTCHECK_MIN_ROWS})."
                )
            if not np.allclose(
                live[both], stored[both],
                rtol=_SPOTCHECK_RTOL, atol=_SPOTCHECK_ATOL,
            ):
                max_abs = float(np.max(np.abs(live[both] - stored[both])))
                raise BackfillValidationError(
                    f"Feature spot-check FAILED for {symbol}/'{col}': live "
                    f"vs stored max abs deviation {max_abs:.3e} exceeds "
                    f"float32 tolerance (rtol={_SPOTCHECK_RTOL}, "
                    f"atol={_SPOTCHECK_ATOL}) on {n} rows — the adjusted "
                    "basis does NOT reproduce the training features."
                )
            log.info(
                "Feature spot-check OK for %s/%s on %d rows.", symbol, col, n
            )

    return _check


def _resolve_symbol_strategy_configs(fleet_manifest_path: Path) -> dict:
    """Map brain symbol -> strategy config path from the fleet manifest."""
    if not fleet_manifest_path.exists():
        raise BackfillValidationError(
            f"Fleet manifest not found at {fleet_manifest_path}."
        )
    with open(fleet_manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    mapping: dict = {}
    for inst in manifest.get("instances", []):
        if not inst.get("enabled", False):
            continue
        cfg_path = _REPO_ROOT / inst["config"]
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        symbols = {
            leg.get("symbol")
            for leg in (cfg.get("models") or {}).values()
            if leg.get("symbol")
        }
        if len(symbols) != 1:
            raise BackfillValidationError(
                f"Strategy config {cfg_path} model legs disagree on the "
                f"brain symbol: {sorted(symbols)}."
            )
        mapping[next(iter(symbols))] = cfg_path
    return mapping


def main(argv: Optional[list] = None) -> int:
    """Operator entry point. DO NOT run while the fleet is being restarted.

    Metadata writes are safe while children run (ratios restore only at
    initialize()); the RESTART is the activation and is operator-gated:
    NG child first (live canary), record the shadow-log vs training-basis
    probability comparison into the ticket folder, THEN fleet-wide.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Backfill empty roll_history metadata from ratio-adjusted "
            f"training HourSets (ticket {TICKET_ID})."
        )
    )
    parser.add_argument(
        "--symbols", nargs="+", default=list(_FLEET_SYMBOLS),
        choices=list(_FLEET_SYMBOLS),
        help="Brain symbols to migrate (default: all five fleet symbols).",
    )
    parser.add_argument(
        "--fleet-manifest",
        default=str(_REPO_ROOT / "configs" / "fleet" / "fleet_manifest.json"),
        help="Fleet manifest used to resolve each deployed child's strategy "
             "config (and from it the training HourSet — never hardcoded).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run every gate (coverage, replay, features) but write nothing.",
    )
    parser.add_argument(
        "--cl-june-ratio", type=float, default=None,
        help="CL ONLY, NO DEFAULT (no-silent-null-defaults): the one-time "
             "IBKR expired-pair estimate for the ~2026-06 CL roll — "
             "median same-timestamp Close ratio CLQ6/CLN6 (NEW contract / "
             "OLD contract, includeExpired=True, ~2026-06-15..19). Required "
             "iff a post-reference seam exists in the CL cache; the run "
             "hard-fails without it.",
    )
    parser.add_argument("--cl-from", default="CLN6",
                        help="Label for the CL post-reference entry 'from'.")
    parser.add_argument("--cl-to", default="CLQ6",
                        help="Label for the CL post-reference 'to_contract'.")
    parser.add_argument(
        "--replay-tol", type=float, default=REPLAY_REL_TOL,
        help=f"Replay-equality gate tolerance (default {REPLAY_REL_TOL}; "
             f"hard ceiling {_REPLAY_REL_TOL_CEILING} — anything wider is "
             "refused).",
    )
    parser.add_argument(
        "--tail-seam-threshold", type=float, default=TAIL_SEAM_REL_THRESHOLD,
        help="Bar-over-bar jump flagged as an uncovered seam outside the "
             f"reference window (default {TAIL_SEAM_REL_THRESHOLD}).",
    )
    parser.add_argument(
        "--seam-match-hours", type=float, default=0.0,
        help="Tolerance (hours) when matching detected cache seams against "
             "extra-entry cutoffs from Databento/IBKR sources (default: "
             "exact timestamp match).",
    )
    parser.add_argument(
        "--spotcheck-features", nargs="+",
        default=list(DEFAULT_SPOTCHECK_FEATURES),
        help="Feature columns for the gate-5c reproduction check (each is "
             "checked when stored in that symbol's HourSet; NG additionally "
             "REQUIRES TS_VOL_YZ_ZSCORE_72v840).",
    )
    parser.add_argument(
        "--skip-feature-check", action="store_true",
        help="Skip gate 5c (NOT recommended; loud opt-out, never silent).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg_by_symbol = _resolve_symbol_strategy_configs(Path(args.fleet_manifest))
    seam_match_tol = pd.Timedelta(hours=args.seam_match_hours)
    failures: dict = {}

    for symbol in args.symbols:
        log.info("=" * 70)
        log.info("Migrating %s ...", symbol)
        try:
            if symbol not in cfg_by_symbol:
                raise BackfillValidationError(
                    f"No enabled fleet instance found for {symbol} in "
                    f"{args.fleet_manifest} — cannot resolve its HourSet."
                )
            cfg_path = cfg_by_symbol[symbol]
            with open(cfg_path, "r", encoding="utf-8") as f:
                strategy_cfg = json.load(f)
            dataset_path = _resolve_dataset_path_from_strategy_config(
                strategy_cfg, cfg_path
            )
            log.info("%s HourSet: %s", symbol, dataset_path)
            hourset = _load_hourset(dataset_path)
            reference = _check_close_series(
                hourset["Close"].astype(np.float64),
                f"{symbol} HourSet Close",
            )

            paths = derive_data_paths(symbol)
            cache_path = paths.cache_1h
            metadata_path = paths.roll_metadata

            meta_now = _load_metadata_strict(metadata_path)
            raw_close = _load_cache_close(cache_path)

            # Labels: to_contract from the metadata's stored front month
            # (same resolution migrate_symbol applies); from is "unknown"
            # for the pre-history seams (data_manager.py:882-883 precedent).
            extra: list = []
            hs_end = reference.index.max()
            if symbol == "CL":
                tail = raw_close.loc[raw_close.index >= hs_end]
                seams = _detect_seams_in_window(
                    tail, args.tail_seam_threshold
                )
                if seams and args.cl_june_ratio is None:
                    raise BackfillValidationError(
                        f"CL has {len(seams)} post-reference seam(s) at "
                        f"{[str(t) for t in seams]} and --cl-june-ratio was "
                        "not provided — the CL post-HourSet seam has no "
                        "Databento source; supply the documented IBKR "
                        "expired-pair estimate (CLQ6/CLN6 median "
                        "same-timestamp Close ratio). NO default."
                    )
                if len(seams) > 1:
                    raise BackfillValidationError(
                        f"CL has {len(seams)} post-reference seams but only "
                        "one --cl-june-ratio is supported — cover the extra "
                        "seams manually before re-running."
                    )
                if not seams and args.cl_june_ratio is not None:
                    raise BackfillValidationError(
                        "--cl-june-ratio was provided but NO post-reference "
                        "seam was detected in the CL cache — refusing to "
                        "write an unanchored entry."
                    )
                if seams:
                    extra = [{
                        "from": args.cl_from,
                        "to_contract": args.cl_to,
                        "ratio": float(args.cl_june_ratio),
                        "timestamp": datetime.now().isoformat(),
                        "timestamp_cutoff": seams[0].isoformat(),
                        "origin": ORIGIN_STAMP,
                    }]
            else:
                extra = _derive_post_reference_entries_databento(
                    symbol, reference,
                    from_label="unknown",
                    to_contract_label=f"{symbol}_databento_seam",
                )

            feature_check = None
            if not args.skip_feature_check:
                present = [c for c in args.spotcheck_features
                           if c in hourset.columns]
                if symbol == "NG" and "TS_VOL_YZ_ZSCORE_72v840" not in present:
                    raise BackfillValidationError(
                        "NG spot-check REQUIRES TS_VOL_YZ_ZSCORE_72v840 in "
                        "its HourSet (blueprint 5c) — column missing or "
                        "excluded."
                    )
                if not present:
                    raise BackfillValidationError(
                        f"None of the spot-check features "
                        f"{args.spotcheck_features} are stored in the "
                        f"{symbol} HourSet — pass columns that exist, or "
                        "--skip-feature-check to opt out LOUDLY."
                    )
                feature_check = _make_feature_check(hourset, present, symbol)
            else:
                log.warning(
                    "%s: feature spot-check gate 5c SKIPPED by operator "
                    "flag.", symbol,
                )

            migrate_symbol(
                symbol=symbol,
                cache_path=cache_path,
                metadata_path=metadata_path,
                reference_close=reference,
                extra_entries=extra,
                dry_run=args.dry_run,
                replay_rel_tol=args.replay_tol,
                tail_seam_threshold=args.tail_seam_threshold,
                seam_match_tolerance=seam_match_tol,
                feature_check=feature_check,
            )
            _ = meta_now  # loaded above purely to fail early on bad files
        except ValueError as exc:
            log.error("HARD FAIL for %s: %s", symbol, exc)
            failures[symbol] = str(exc)

    log.info("=" * 70)
    if failures:
        log.error(
            "Migration FAILED for %d symbol(s): %s — NO activation until "
            "every symbol passes.", len(failures), sorted(failures),
        )
        return 1
    log.info(
        "All requested symbols migrated%s. ACTIVATION (operator-gated): "
        "1) restart the NG child FIRST as live canary; 2) record the "
        "shadow-log vs training-basis probability comparison into "
        ".agents/collab/tickets/%s/ BEFORE any fleet-wide restart; "
        "3) rollback = restore the *_backup_* metadata files + restart.",
        " (dry-run)" if args.dry_run else "", TICKET_ID,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
