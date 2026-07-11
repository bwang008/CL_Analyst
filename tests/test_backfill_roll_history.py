"""
TDD-TESTER AUTHORIZATION
Target Implementation File: scripts/backfill_roll_history.py
Target Class/Function: derive_segments, segments_to_roll_entries, validate_replay, migrate_symbol
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: jit-roll-ratio-empty_07102026_1453 (Stage 1 — roll_history backfill migration)

=============================================================================
API CONTRACT the Coder MUST implement (module: scripts/backfill_roll_history.py)
=============================================================================

All functions below MUST be importable, module-level, and side-effect-free
except ``migrate_symbol`` (the only function allowed to write files).
Every hard-fail gate MUST raise ``ValueError`` (a custom subclass of
ValueError is acceptable) — never warn/log-and-continue, never widen a
tolerance (no-silent-null-defaults rule).

1. ``derive_segments(raw_close: pd.Series, adjusted_close: pd.Series) -> list``
   - Inputs: Close series with DatetimeIndex. The function must operate on the
     TIMESTAMP INTERSECTION of the two (raw may extend beyond adjusted).
   - Computes the per-timestamp quotient q_t = adjusted_close / raw_close and
     segments it into piecewise-constant runs (real roll seams are >= ~0.2%,
     so run-grouping must use a coarse seam threshold — it must NOT split a
     merely-noisy quotient into hundreds of micro-segments).
   - Returns segments ordered oldest -> newest. Each segment supports dict
     access with AT LEAST: ``seg["factor"]`` (float, the constant quotient) and
     ``seg["start"]`` (pd.Timestamp, first bar of the segment). Extra keys OK.
   - CV HARD-FAIL GATE: if any within-segment coefficient of variation of the
     quotient is >= 1e-6, raise ValueError. No silent tolerance.

2. ``segments_to_roll_entries(segments, from_label, to_contract_label) -> list[dict]``
   - For K+1 segments (factors f_0..f_K oldest->newest) produce exactly K
     entries, one per seam, in the live replay convention of
     DataManager.get_ratio_adjusted_df() (multiplies bars index < cutoff by
     ratio): entry k has ``ratio = f_{k-1} / f_k`` (FULL float precision — do
     NOT round to 6dp; the 1e-9 replay gate below fails on 6dp rounding) and
     ``timestamp_cutoff`` = ISO string of the first bar of segment k.
   - Entry schema (swap-proof, data_manager.py:358-367 legacy-restore branch):
     MUST NOT contain a ``"to"`` key. MUST contain: ``"from"`` (=from_label),
     ``"to_contract"`` (=to_contract_label), ``"ratio"`` (float),
     ``"timestamp"`` (ISO string, migration run time), ``"timestamp_cutoff"``
     (ISO string), ``"origin"`` == "seed_backfill_jit-roll-ratio-empty_07102026_1453".
   - Entries must be json.dumps-serializable as returned.

3. ``validate_replay(symbol, cache_path, roll_metadata_path, reference_close) -> None``
   - Constructs the REAL src.live_execution.data_manager.DataManager
     (symbol=..., data_client=None, explicit cache_path/roll_metadata_path
     overrides), runs initialize() + get_ratio_adjusted_df(), and asserts:
     (a) adjusted_close / reference_close constant at 1.0 across ALL overlap
     timestamps, and (b) the final (newest) bar is unchanged vs raw.
   - Returns None on success; raises ValueError on ANY failure (including a
     ratio written in the inverted direction — direction is PROVEN here,
     never assumed from the formula).

4. ``migrate_symbol(symbol, cache_path, metadata_path, reference_close, ...) -> None``
   (extra keyword args with defaults are allowed; these four names are fixed)
   - Orchestrates: load raw cache -> derive_segments -> build entries VIA THE
     MODULE-LEVEL ``segments_to_roll_entries`` (this is a required, patchable
     seam) -> validate against SCRATCH copies -> backup -> write real target.
   - COVERAGE HARD-FAIL: if the raw cache extends beyond reference_close and
     the uncovered tail contains a roll seam (bar-over-bar jump; the test
     fixture uses a 5% level shift while smooth-fixture bar-over-bar moves are
     < 0.05%), raise ValueError BEFORE writing anything.
   - BACKUP-BEFORE-WRITE: before modifying a pre-existing metadata file, copy
     it to a timestamped backup IN THE SAME DIRECTORY (filename must contain
     "roll_metadata"); preserve all unrelated existing keys
     (last_front_month, last_front_month_by_symbol, ...) untouched.
   - ABORT-BEFORE-WRITE: if validation fails, the REAL target metadata file
     content must be byte-for-byte (JSON-content) unchanged.
   - IDEMPOTENT: a second run must detect existing origin-stamped entries and
     either no-op or raise loudly — NEVER duplicate entries.

Environment: conda env "trader" runs pandas 1.5.3 — no pandas>=2 APIs.
All fixtures are SYNTHETIC under tmp_path; no real data files are touched.
=============================================================================
"""

import json
from unittest import mock

import numpy as np
import pandas as pd
import pytest

import src.live_execution.data_manager as dm_module
from src.live_execution.data_manager import DataManager

# Ghost import (TDD): scripts/backfill_roll_history.py does not exist yet.
# Collection MUST fail with ModuleNotFoundError until the Coder implements it.
from scripts.backfill_roll_history import (  # noqa: E402
    derive_segments,
    migrate_symbol,
    segments_to_roll_entries,
    validate_replay,
)


# ---------------------------------------------------------------------------
# Synthetic fixture constants (deterministic — no RNG anywhere in this file)
# ---------------------------------------------------------------------------

TICKET_ID = "jit-roll-ratio-empty_07102026_1453"
EXPECTED_ORIGIN = "seed_backfill_" + TICKET_ID

N_RAW = 300           # raw live cache length (hourly bars)
N_REF = 280           # adjusted reference (training HourSet) overlap length
SEAM_POSITIONS = (100, 200)          # first bar OF THE NEW segment (cutoffs)
FACTORS = (0.97, 0.99, 1.0)          # oldest -> newest; newest EXACTLY 1.0

# Per-roll ratios in the get_ratio_adjusted_df() replay convention:
# entry k: ratio = f_{k-1} / f_k, cutoff = first bar of segment k.
EXPECTED_RATIOS = (FACTORS[0] / FACTORS[1], FACTORS[1] / FACTORS[2])


def _hourly_index(n):
    return pd.date_range("2026-01-05 00:00", periods=n, freq="1h")


def _make_raw_close(n=N_RAW):
    """Smooth deterministic raw Close: bar-over-bar moves ~0.017% (no seams)."""
    idx = _hourly_index(n)
    close = 60.0 + np.arange(n, dtype=np.float64) * 0.01
    return pd.Series(close, index=idx, name="Close")


def _factor_vector(n=N_REF):
    fvec = np.empty(n, dtype=np.float64)
    fvec[: SEAM_POSITIONS[0]] = FACTORS[0]
    fvec[SEAM_POSITIONS[0]: SEAM_POSITIONS[1]] = FACTORS[1]
    fvec[SEAM_POSITIONS[1]:] = FACTORS[2]
    return fvec


def _make_reference_close(raw_close):
    """Adjusted reference = raw x piecewise-constant factors on first N_REF bars."""
    ref = raw_close.iloc[:N_REF] * _factor_vector(N_REF)
    ref.name = "Close"
    return ref


def _make_cache_df(close):
    df = pd.DataFrame({
        "DateTime": close.index,
        "Open": close.values - 0.005,
        "High": close.values + 0.01,
        "Low": close.values - 0.01,
        "Close": close.values,
        "Volume": np.full(len(close), 1000.0),
    })
    df = df.set_index("DateTime", drop=False)
    df.index.name = "DateTime"
    return df


def _write_cache(cache_path, close):
    """Write a live-cache-format parquet (DateTime as a COLUMN, like save_cache)."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    out = _make_cache_df(close).reset_index(drop=True)
    out.to_parquet(str(cache_path), index=False, engine="pyarrow")


def _base_metadata():
    """Pre-existing live metadata conventions (roll_history empty — the bug)."""
    return {
        "last_front_month": "CLQ6",
        "updated_at": "2026-07-01T00:00:00",
        "roll_history": [],
        "cumulative_ratio": 1.0,
        "last_front_month_by_symbol": {"CL": "CLQ6"},
    }


def _write_metadata(meta_path, meta):
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _read_metadata(meta_path):
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_dm(tmp_path, cache_path, meta_path, execution_symbol=None):
    """Real DataManager, fully path-isolated to tmp_path (never touches
    C:\\CL_Analyst_Data). data_client=None + front_month_id=None means
    initialize() only does: restore roll_history -> load cache -> return."""
    kwargs = dict(
        symbol="CL",  # registry symbol — get_instrument("CL") resolves
        data_client=None,
        seed_path=str(tmp_path / "unused_seed.parquet"),
        cache_path=str(cache_path),
        master_ledger_path=str(tmp_path / "unused_ledger.parquet"),
        roll_metadata_path=str(meta_path),
        bar_size="1 hour",
        bars_per_day=24,
    )
    if execution_symbol is not None:
        kwargs["execution_symbol"] = execution_symbol
    return DataManager(**kwargs)


@pytest.fixture
def isolated_backup_dir(tmp_path):
    """Redirect DataManager's repo-level cache-backup dir into tmp_path.

    initialize() step 6 writes a cache backup into the module constant
    _CACHE_BACKUP_DIR when it holds no *.parquet — patch it to an isolated
    dir pre-seeded with a sentinel so tests NEVER write into the repo.
    """
    bdir = tmp_path / "_isolated_cache_backups"
    bdir.mkdir()
    (bdir / "sentinel.parquet").touch()
    with mock.patch.object(dm_module, "_CACHE_BACKUP_DIR", bdir):
        yield bdir


@pytest.fixture
def happy_paths(tmp_path):
    """Standard happy-path layout: raw cache (300 bars, smooth uncovered tail
    of 20 bars AFTER the reference ends — a correct implementation must
    tolerate a seam-free uncovered tail), reference (280 bars, 2 seams),
    pre-existing metadata with empty roll_history."""
    raw = _make_raw_close()
    reference = _make_reference_close(raw)
    cache_path = tmp_path / "cache" / "warm_start_cache_1h.parquet"
    meta_path = tmp_path / "meta" / ".roll_metadata.json"
    _write_cache(cache_path, raw)
    _write_metadata(meta_path, _base_metadata())
    return {
        "raw": raw,
        "reference": reference,
        "cache_path": cache_path,
        "meta_path": meta_path,
    }


def _entries_via_api(raw, reference):
    segments = derive_segments(raw, reference)
    return segments_to_roll_entries(segments, "CLM6", "CLN6")


def _assert_replay_matches_reference(adj, reference, rel_tol=1e-9):
    overlap = adj.index.intersection(reference.index)
    assert len(overlap) == len(reference), (
        "replay must cover ALL overlap timestamps "
        f"(got {len(overlap)} of {len(reference)})"
    )
    q = adj.loc[overlap, "Close"].to_numpy() / reference.loc[overlap].to_numpy()
    max_dev = float(np.max(np.abs(q - 1.0)))
    assert max_dev < rel_tol, (
        f"adjusted_close / reference_close must be constant at 1.0 "
        f"(max rel deviation {max_dev:.3e} >= {rel_tol})"
    )


# ===========================================================================
# 1. Quotient segmentation
# ===========================================================================

class TestDeriveSegments:

    def test_quotient_segmentation_finds_exact_seams(self):
        """3 piecewise-constant factors -> exactly 3 segments with the right
        factors and the right start timestamps (cutoff = first bar of the new
        segment); raw longer than adjusted -> intersection handled."""
        raw = _make_raw_close()                 # 300 bars
        reference = _make_reference_close(raw)  # 280 bars (intersection test)

        segments = derive_segments(raw, reference)

        assert len(segments) == 3, (
            f"expected exactly 3 segments, got {len(segments)} — segmentation "
            "must group constant runs, not split on float noise"
        )
        expected_starts = (
            reference.index[0],
            reference.index[SEAM_POSITIONS[0]],
            reference.index[SEAM_POSITIONS[1]],
        )
        for seg, factor, start in zip(segments, FACTORS, expected_starts):
            assert seg["factor"] == pytest.approx(factor, rel=1e-9)
            assert pd.Timestamp(seg["start"]) == start

    def test_newest_segment_factor_is_exactly_one(self):
        """The newest segment is the unadjusted (current-basis) window: its
        factor must be 1.0 to float precision."""
        raw = _make_raw_close()
        reference = _make_reference_close(raw)

        segments = derive_segments(raw, reference)

        assert segments[-1]["factor"] == pytest.approx(1.0, abs=1e-12)

    # =======================================================================
    # 2. CV hard-fail gate
    # =======================================================================

    def test_noisy_quotient_raises_cv_hard_fail(self):
        """A quotient with ~1e-5 relative noise (CV ~7e-6 >= 1e-6) must RAISE.
        The noise (deterministic sinusoid) is far below any real seam (>=0.2%)
        so run-grouping must treat it as ONE noisy segment and the CV gate
        must hard-fail it — resolving it into micro-segments or silently
        widening the tolerance are both bugs."""
        raw = _make_raw_close()
        reference = _make_reference_close(raw)
        noise = 1.0 + 1e-5 * np.sin(np.arange(N_REF, dtype=np.float64))
        noisy_reference = reference * noise
        noisy_reference.name = "Close"

        with pytest.raises(ValueError):
            derive_segments(raw, noisy_reference)


# ===========================================================================
# 4. Entry schema (swap-proof)  [numbered per ticket requirement list]
# ===========================================================================

class TestSegmentsToRollEntries:

    def test_entries_schema_no_to_key(self):
        """Entries must NOT carry "to" (so the legacy restore branch at
        data_manager.py:358-367 restores them unconditionally, regardless of
        execution symbol) and MUST carry the swap-proof schema keys with the
        exact ticket origin stamp. Must be JSON-serializable as returned."""
        raw = _make_raw_close()
        reference = _make_reference_close(raw)

        entries = _entries_via_api(raw, reference)

        assert len(entries) == 2
        required = {"from", "to_contract", "ratio", "timestamp",
                    "timestamp_cutoff", "origin"}
        for entry in entries:
            assert "to" not in entry, (
                'entries must have NO "to" key — its presence makes the '
                "ownership filter execution-symbol-dependent (swap-unsafe)"
            )
            missing = required - set(entry.keys())
            assert not missing, f"entry missing required keys: {missing}"
            assert entry["origin"] == EXPECTED_ORIGIN
            assert entry["from"] == "CLM6"
            assert entry["to_contract"] == "CLN6"
            assert isinstance(entry["ratio"], float)
            assert isinstance(entry["timestamp_cutoff"], str)
            assert isinstance(entry["timestamp"], str)
            pd.Timestamp(entry["timestamp"])  # parseable

        json.dumps(entries)  # must not raise

    def test_entries_ratio_and_cutoff_values(self):
        """Per-roll ratio must be f_{k-1}/f_k at FULL precision (rounding to
        6dp fails the rel=1e-12 pin AND the 1e-9 replay gate) with cutoff =
        first bar of the new segment, ISO-encoded."""
        raw = _make_raw_close()
        reference = _make_reference_close(raw)

        entries = _entries_via_api(raw, reference)
        entries = sorted(entries, key=lambda e: e["timestamp_cutoff"])

        expected_cutoffs = (
            reference.index[SEAM_POSITIONS[0]],
            reference.index[SEAM_POSITIONS[1]],
        )
        for entry, exp_ratio, exp_cutoff in zip(
            entries, EXPECTED_RATIOS, expected_cutoffs
        ):
            assert entry["ratio"] == pytest.approx(exp_ratio, rel=1e-12), (
                "ratio must be f_prev/f_new at full float precision "
                "(no 6dp rounding)"
            )
            assert pd.Timestamp(entry["timestamp_cutoff"]) == exp_cutoff


# ===========================================================================
# 3. THE CRITICAL replay-equality property (through the REAL DataManager)
# ===========================================================================

class TestReplayEquality:

    def test_replay_through_real_datamanager_reproduces_reference(
        self, tmp_path, happy_paths, isolated_backup_dir
    ):
        """Entries written to the metadata JSON and restored by the REAL
        DataManager.initialize() must make get_ratio_adjusted_df() reproduce
        the adjusted reference EXACTLY (rel deviation < 1e-9) on ALL overlap
        timestamps. This PROVES the ratio direction — the test goes through
        the real index<cutoff mask, never a reimplementation."""
        hp = happy_paths
        entries = _entries_via_api(hp["raw"], hp["reference"])
        meta = _base_metadata()
        meta["roll_history"] = entries
        _write_metadata(hp["meta_path"], meta)

        dm = _make_dm(tmp_path, hp["cache_path"], hp["meta_path"])
        dm.initialize()
        adj = dm.get_ratio_adjusted_df()

        _assert_replay_matches_reference(adj, hp["reference"], rel_tol=1e-9)

    def test_final_bar_unchanged_vs_raw(
        self, tmp_path, happy_paths, isolated_backup_dir
    ):
        """Ratio adjustment touches HISTORY only: the newest bar (and the
        whole post-reference tail, which lies after every cutoff) must be
        byte-identical to raw."""
        hp = happy_paths
        entries = _entries_via_api(hp["raw"], hp["reference"])
        meta = _base_metadata()
        meta["roll_history"] = entries
        _write_metadata(hp["meta_path"], meta)

        dm = _make_dm(tmp_path, hp["cache_path"], hp["meta_path"])
        dm.initialize()
        adj = dm.get_ratio_adjusted_df()

        raw = hp["raw"]
        assert adj["Close"].iloc[-1] == pytest.approx(
            raw.iloc[-1], rel=1e-12
        ), "newest bar must be unchanged vs raw"
        tail_idx = raw.index[N_REF:]
        np.testing.assert_allclose(
            adj.loc[tail_idx, "Close"].to_numpy(),
            raw.loc[tail_idx].to_numpy(),
            rtol=1e-12,
            err_msg="post-reference tail must be unchanged vs raw",
        )

    def test_restore_independent_of_execution_symbol(
        self, tmp_path, happy_paths, isolated_backup_dir
    ):
        """No-"to" entries must be restored under ANY execution symbol: a
        DataManager with execution_symbol="MCL" (micro swap) must restore ALL
        backfilled entries and reproduce the reference identically."""
        hp = happy_paths
        entries = _entries_via_api(hp["raw"], hp["reference"])
        meta = _base_metadata()
        meta["roll_history"] = entries
        _write_metadata(hp["meta_path"], meta)

        dm = _make_dm(
            tmp_path, hp["cache_path"], hp["meta_path"], execution_symbol="MCL"
        )
        dm.initialize()

        assert len(dm._roll_ratios) == len(entries), (
            "an MCL-execution DataManager must restore every backfilled "
            "entry — restoration must be execution-symbol independent"
        )
        adj = dm.get_ratio_adjusted_df()
        _assert_replay_matches_reference(adj, hp["reference"], rel_tol=1e-9)


# ===========================================================================
# validate_replay — the load-bearing gate (direction PROVEN, not assumed)
# ===========================================================================

class TestValidateReplay:

    def test_validate_replay_passes_on_correct_entries(
        self, tmp_path, happy_paths, isolated_backup_dir
    ):
        """Correct entries -> validate_replay returns None (no raise)."""
        hp = happy_paths
        entries = _entries_via_api(hp["raw"], hp["reference"])
        meta = _base_metadata()
        meta["roll_history"] = entries
        _write_metadata(hp["meta_path"], meta)

        result = validate_replay(
            symbol="CL",
            cache_path=hp["cache_path"],
            roll_metadata_path=hp["meta_path"],
            reference_close=hp["reference"],
        )
        assert result is None

    def test_validate_replay_raises_on_corrupted_ratio(
        self, tmp_path, happy_paths, isolated_backup_dir
    ):
        """A single ratio off by 2% must hard-fail the replay gate."""
        hp = happy_paths
        entries = _entries_via_api(hp["raw"], hp["reference"])
        entries[0]["ratio"] = entries[0]["ratio"] * 1.02
        meta = _base_metadata()
        meta["roll_history"] = entries
        _write_metadata(hp["meta_path"], meta)

        with pytest.raises(ValueError):
            validate_replay(
                symbol="CL",
                cache_path=hp["cache_path"],
                roll_metadata_path=hp["meta_path"],
                reference_close=hp["reference"],
            )

    def test_validate_replay_raises_on_inverted_ratio_direction(
        self, tmp_path, happy_paths, isolated_backup_dir
    ):
        """Ratios written in the INVERTED direction (f_new/f_prev) must fail
        the gate — this is the blueprint's 'direction must be PROVEN by the
        validation gate, never assumed from the formula' requirement."""
        hp = happy_paths
        entries = _entries_via_api(hp["raw"], hp["reference"])
        for entry in entries:
            entry["ratio"] = 1.0 / entry["ratio"]
        meta = _base_metadata()
        meta["roll_history"] = entries
        _write_metadata(hp["meta_path"], meta)

        with pytest.raises(ValueError):
            validate_replay(
                symbol="CL",
                cache_path=hp["cache_path"],
                roll_metadata_path=hp["meta_path"],
                reference_close=hp["reference"],
            )


# ===========================================================================
# 5-8. migrate_symbol — coverage gate, backup, idempotency, abort-before-write
# ===========================================================================

class TestMigrateSymbol:

    def _run_migration(self, hp):
        migrate_symbol(
            symbol="CL",
            cache_path=hp["cache_path"],
            metadata_path=hp["meta_path"],
            reference_close=hp["reference"],
        )

    def _origin_entries(self, meta):
        return [e for e in meta.get("roll_history", [])
                if e.get("origin") == EXPECTED_ORIGIN]

    def test_migrate_writes_entries_and_replay_validates(
        self, tmp_path, happy_paths, isolated_backup_dir
    ):
        """Happy path (smooth seam-free uncovered tail MUST be tolerated):
        exactly 2 origin-stamped no-"to" entries land in the real metadata
        file, and a real DataManager replay of the migrated file reproduces
        the reference (< 1e-9)."""
        hp = happy_paths

        self._run_migration(hp)

        meta = _read_metadata(hp["meta_path"])
        entries = self._origin_entries(meta)
        assert len(entries) == 2, (
            f"expected exactly 2 backfilled entries, got {len(entries)}"
        )
        entries = sorted(entries, key=lambda e: e["timestamp_cutoff"])
        for entry, exp_ratio in zip(entries, EXPECTED_RATIOS):
            assert "to" not in entry
            assert entry["ratio"] == pytest.approx(exp_ratio, rel=1e-9)

        dm = _make_dm(tmp_path, hp["cache_path"], hp["meta_path"])
        dm.initialize()
        adj = dm.get_ratio_adjusted_df()
        _assert_replay_matches_reference(adj, hp["reference"], rel_tol=1e-9)

    def test_uncovered_seam_after_reference_hard_fails(
        self, tmp_path, isolated_backup_dir
    ):
        """A 5% roll seam in the raw series AFTER the reference ends
        (uncovered window) must RAISE — partial history = silent basis break
        mid-window. The real target metadata must be unchanged."""
        raw = _make_raw_close()
        reference = _make_reference_close(raw)  # from the pre-jump raw
        # Inject the seam at bar 290 — 10 bars past the reference end (280).
        raw_with_seam = raw.copy()
        raw_with_seam.iloc[290:] = raw_with_seam.iloc[290:] * 0.95

        cache_path = tmp_path / "cache" / "warm_start_cache_1h.parquet"
        meta_path = tmp_path / "meta" / ".roll_metadata.json"
        _write_cache(cache_path, raw_with_seam)
        original_meta = _base_metadata()
        _write_metadata(meta_path, original_meta)

        with pytest.raises(ValueError):
            migrate_symbol(
                symbol="CL",
                cache_path=cache_path,
                metadata_path=meta_path,
                reference_close=reference,
            )

        assert _read_metadata(meta_path) == original_meta, (
            "coverage hard-fail must not write ANY partial history"
        )

    def test_backup_created_before_write_and_keys_preserved(
        self, tmp_path, happy_paths, isolated_backup_dir
    ):
        """The write step must (a) leave a timestamped backup copy of the
        pre-migration metadata IN THE SAME DIRECTORY (name contains
        'roll_metadata', content == the pre-migration JSON) and (b) preserve
        unrelated existing keys untouched."""
        hp = happy_paths
        original_meta = _read_metadata(hp["meta_path"])
        before = {p.name for p in hp["meta_path"].parent.iterdir()}

        self._run_migration(hp)

        after = {p.name for p in hp["meta_path"].parent.iterdir()}
        new_names = after - before
        backups = []
        for name in new_names:
            p = hp["meta_path"].parent / name
            if p == hp["meta_path"] or "roll_metadata" not in name:
                continue
            try:
                if _read_metadata(p) == original_meta:
                    backups.append(p)
            except (json.JSONDecodeError, OSError):
                continue
        assert backups, (
            "no timestamped backup with the pre-migration content was found "
            "in the metadata file's directory"
        )

        meta = _read_metadata(hp["meta_path"])
        assert meta["last_front_month"] == "CLQ6"
        assert meta["last_front_month_by_symbol"] == {"CL": "CLQ6"}
        assert "cumulative_ratio" in meta
        assert "updated_at" in meta

    def test_idempotent_second_run_no_duplicates(
        self, tmp_path, happy_paths, isolated_backup_dir
    ):
        """Running the migration twice must not duplicate entries: the second
        run either no-ops or raises loudly, and the metadata file ends with
        exactly ONE entry set either way."""
        hp = happy_paths

        self._run_migration(hp)
        meta_first = _read_metadata(hp["meta_path"])
        entries_first = self._origin_entries(meta_first)
        assert len(entries_first) == 2

        try:
            self._run_migration(hp)  # loud refusal is sanctioned
        except Exception:
            pass

        meta_second = _read_metadata(hp["meta_path"])
        entries_second = self._origin_entries(meta_second)
        assert entries_second == entries_first, (
            "second run must not duplicate or mutate the backfilled entries"
        )
        assert len(meta_second["roll_history"]) == len(
            meta_first["roll_history"]
        ), "roll_history grew on re-run — idempotency violated"

    def test_abort_before_write_on_validation_failure(
        self, tmp_path, happy_paths, isolated_backup_dir
    ):
        """If the replay-equality validation fails (forced here by corrupting
        the ratios at the module-level segments_to_roll_entries seam), NOTHING
        may be written to the real target metadata path."""
        hp = happy_paths
        original_meta = _read_metadata(hp["meta_path"])

        import scripts.backfill_roll_history as bh

        real_fn = bh.segments_to_roll_entries

        def _corrupted(*args, **kwargs):
            entries = real_fn(*args, **kwargs)
            entries[0]["ratio"] = entries[0]["ratio"] * 1.02
            return entries

        with mock.patch.object(
            bh, "segments_to_roll_entries", side_effect=_corrupted
        ):
            with pytest.raises(ValueError):
                migrate_symbol(
                    symbol="CL",
                    cache_path=hp["cache_path"],
                    metadata_path=hp["meta_path"],
                    reference_close=hp["reference"],
                )

        assert _read_metadata(hp["meta_path"]) == original_meta, (
            "validation failure must abort BEFORE any write to the real "
            "target metadata file"
        )
