"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/data_manager.py,
                            src/core/instrument_master.py
Target Class/Function: DataManager.resolve_roll_seam,
                       DataManager._append_roll_event,
                       DataManager.set_pending_roll,
                       DataManager.get_pending_roll,
                       DataManager.clear_pending_roll,
                       DataManager.initialize (pending-roll resolution +
                       hard-fail path), DataManager._apply_roll_to_cache
                       (Amendment-1 cutoff/overwrite consistency),
                       INSTRUMENT_REGISTRY CL/MCL roll_ratio_tolerance
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: jit-roll-ratio-empty_07102026_1453 (Stage 2 — mid-run roll-seam
capture; blueprint.md "Stage 2" + auditor_rca_and_fix_proposal.md)

Every test carries an EXPECTED-RED (new behavior, must fail on current
HEAD) or EXPECTED-GREEN (characterization pin, must pass on current HEAD
AND after the fix) tag in its docstring.

=============================================================================
STAGE-2 API CONTRACT the Coder MUST implement (this file and
tests/test_pending_roll_lifecycle.py are the authority wherever the
blueprint left the surface open)
=============================================================================

A. Outcome protocol — module-level constants in
   src/live_execution/data_manager.py (importable by live_trader):
       ROLL_SEAM_RESOLVED = "RESOLVED"
       ROLL_SEAM_RETRY    = "RETRY"
       ROLL_SEAM_ESCALATE = "ESCALATE"
   resolve_roll_seam() returns one of these exact strings.

B. DataManager.resolve_roll_seam(*, from_contract: str, to_contract: str,
                                 detected_at) -> str
   - KEYWORD-ONLY arguments. ``detected_at`` accepts pd.Timestamp OR an
     ISO-8601 string (the pending_roll JSON round-trip delivers str).
   - Fetches CONTFUT history via
     self.data_client.fetch_historical_bars_by_duration(**kwargs) (window
     sized to cover detected_at - 2 days -> now, default "5 D"; the
     initialize() pending path may widen it). Computes the per-bar
     quotient q_t = ibkr_close / cache_close on the timestamp intersection
     with self._df and classifies each overlap bar:
         old-basis  <=> |q_t - 1| >  self.roll_ratio_tolerance
         new-basis  <=> |q_t - 1| <= self.roll_ratio_tolerance
   - State machine:
       * ALL bars old-basis  -> return "RETRY";   record/persist NOTHING.
       * ALL bars new-basis  -> return "ESCALATE"; record/persist NOTHING.
       * old-run followed by new-run ->
             ratio  = median(q_t over the old-basis run)
             cutoff = FIRST new-basis bar timestamp
         MUST record via _append_roll_event(from_contract, to_contract,
         ratio, cutoff) (immediate persistence — no initialize() needed)
         and return "RESOLVED".
   - resolve_roll_seam does NOT read or clear pending_roll — callers own
     the pending lifecycle (pinned: a pending_roll record survives a
     RESOLVED call untouched).

C. DataManager._append_roll_event(from_contract, to_contract, ratio,
                                  timestamp_cutoff) -> None    [POSITIONAL]
   Refactored out of _save_roll_metadata's initialize-only flow.
   - Appends ratio -> self._roll_ratios and cutoff -> self._roll_timestamps
     (in-memory) AND persists the entry to the metadata file IMMEDIATELY.
   - Entry follows the EXISTING live schema:
       {"from": from_contract, "to": to_contract, "ratio": <float>,
        "timestamp": <now iso>, "timestamp_cutoff": <cutoff iso>}
     with "to" carrying the execution-symbol-prefixed contract so the
     Step-0 ownership filter restores it on the next construction. The
     on-disk ratio MAY be 6dp-rounded (existing writer convention) — tests
     assert abs tolerance 1e-6.
   - MUST preserve: every prior roll_history entry byte-identical
     (including Stage-1 origin-stamped entries that have NO "to" key) and
     every unrelated metadata key (pinned via a sentinel key).

D. Pending-roll accessors (DataManager) — record namespaced per execution
   symbol, mirroring last_front_month_by_symbol:
       meta["pending_roll"][execution_symbol] =
           {"from": <old contract>, "to": <new contract>,
            "detected_at": <NAIVE local ISO — datetime.now().isoformat(),
             the existing metadata convention>}
   - set_pending_roll(from_contract, to_contract) -> None   [POSITIONAL]
       stamps detected_at itself and persists IMMEDIATELY (no initialize).
   - get_pending_roll() -> Optional[dict]
       THIS execution symbol's record; None when absent; foreign symbols'
       records are invisible. Must be DISK-BACKED: a fresh instance that
       never ran initialize() must see a record written by another
       instance/process.
   - clear_pending_roll() -> None
       removes ONLY this execution symbol's record and persists; other
       symbols' pending records and unrelated keys survive.

E. DataManager.initialize() pending resolution — must run BEFORE
   _backfill() (backfill's dedup keep="last" overwrites old-basis overlap
   bars with fresh new-basis IBKR values and destroys the anchor):
   - unresolved pending_roll for this execution symbol + scan RESOLVED ->
     the ratio is recorded EXACTLY ONCE (the Step-5 writer must not append
     a duplicate) and the pending record is cleared on disk.
   - pending + scan cannot anchor (all new-basis) -> raise RuntimeError
     whose message contains "pending" and "roll" (case-insensitive) plus
     an actionable operator remedy. This hard fail REPLACES the silent
     ratio~1 -> "within tolerance" swallow ONLY for the pending-roll path.
   - NO pending + startup-witnessed front-month change whose computed
     ratio satisfies |ratio - 1| <= tolerance -> the existing silent skip
     is CORRECT behavior and must be preserved (EXPECTED-GREEN pin below).

F. Amendment 1 — single-seam invariant, pinned end-to-end through the FULL
   initialize() flow (_detect_rollover -> _compute_roll_ratio ->
   _apply_roll_to_cache -> _backfill): the ratio-adjusted output vs the
   true continuous series must show AT MOST ONE seam step and NO
   double-adjusted (ratio-squared) plateau. The cutoff/overwrite
   convention is the Coder's choice as long as this invariant holds
   end-to-end (the test does not pin the cutoff choice).

G. src/core/instrument_master.py: CL and MCL roll_ratio_tolerance
   0.01 -> 0.001 (Amendment 2; deliberately reverses the T5 zero-change
   pin — companion updates to tests/test_session_watchdog_rollover.py
   :843-844 and :859 are part of this ticket).

Environment: conda env "trader" runs pandas 1.5.3 — no pandas>=2 APIs.
ALL fixtures are SYNTHETIC under tmp_path; C:\\CL_Analyst_Data is never
touched. _CACHE_BACKUP_DIR is patched wherever initialize() runs.
=============================================================================
"""

import json
from unittest import mock
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import src.live_execution.data_manager as dm_module
from src.core.instrument_master import get_instrument
from src.live_execution.data_manager import DataManager

TICKET_ID = "jit-roll-ratio-empty_07102026_1453"
BACKFILL_ORIGIN = "seed_backfill_" + TICKET_ID

# ---------------------------------------------------------------------------
# Synthetic series constants (deterministic — no RNG anywhere in this file)
# ---------------------------------------------------------------------------

N_BARS = 200                 # hourly cache length
OVERLAP_N = 72               # bars the stub CONTFUT fetch covers (cache tail)
OVERLAP_START = N_BARS - OVERLAP_N   # 128
FLIP_POS = 168               # first NEW-basis bar (mid-run stream flip)
R_TRUE = 0.97                # true roll ratio for the seam-scan fixtures
R_AMEND = 0.95               # ratio for the Amendment-1 startup-roll fixture

# Old-basis run inside the overlap = positions 128..167 (40 bars);
# new-basis run = positions 168..199 (32 bars).
OLD_RUN_LEN = FLIP_POS - OVERLAP_START   # 40


def _hidx(n=N_BARS, start="2026-06-20 00:00"):
    return pd.date_range(start, periods=n, freq="1h")


FLIP_TS = _hidx()[FLIP_POS]


def _u_close(n=N_BARS):
    """Smooth deterministic OLD-basis Close (the 'true underlying')."""
    idx = _hidx(n)
    return pd.Series(60.0 + np.arange(n, dtype=np.float64) * 0.01,
                     index=idx, name="Close")


def _ohlcv(close):
    """OHLCV frame in live-cache layout (DateTime column AND index)."""
    df = pd.DataFrame({
        "DateTime": close.index,
        "Open": close.values,
        "High": close.values + 0.02,
        "Low": close.values - 0.02,
        "Close": close.values,
        "Volume": np.full(len(close), 500.0),
    })
    df = df.set_index("DateTime", drop=False)
    df.index.name = "DateTime"
    return df


def _mixed_close():
    """Cache that flipped basis MID-RUN: old (U) before FLIP_TS, new (U*R)
    from FLIP_TS on — the silently-contaminated live cache."""
    c = _u_close().copy()
    c.iloc[FLIP_POS:] = c.iloc[FLIP_POS:] * R_TRUE
    return c


def _fetch_new_basis(scale=R_TRUE):
    """Stub CONTFUT fetch: NEW basis (U*scale) over the overlap window."""
    u = _u_close()
    return _ohlcv(u.iloc[OVERLAP_START:] * scale)


def _fetch_identical():
    """Stub CONTFUT fetch identical to the old-basis cache (q == 1)."""
    return _ohlcv(_u_close().iloc[OVERLAP_START:])


def _feed(frame):
    """Fake DataFeedClient returning `frame` for every historical fetch."""
    feed = MagicMock()
    feed.fetch_historical_bars_by_duration = MagicMock(
        side_effect=lambda **kw: frame.copy()
    )
    return feed


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

SENTINEL_KEY = "operator_note"
SENTINEL_VAL = "unrelated-key-must-survive-jit-roll-ratio-empty"


def _base_meta(front="CLQ6"):
    return {
        "last_front_month": front,
        "updated_at": "2026-07-01T00:00:00",
        "roll_history": [],
        "cumulative_ratio": 1.0,
        "last_front_month_by_symbol": {"CL": front},
        SENTINEL_KEY: SENTINEL_VAL,
    }


def _live_entry():
    """A pre-existing live-schema roll_history entry (owned by CL)."""
    return {
        "from": "CLM6", "to": "CLN6", "ratio": 1.002,
        "timestamp": "2026-05-20T10:00:00",
        "timestamp_cutoff": "2026-05-19T18:00:00",
    }


def _backfill_entry():
    """A Stage-1 origin-stamped entry: NO "to" key (swap-proof schema)."""
    return {
        "from": "seed", "to_contract": "CLN6", "ratio": 0.99,
        "timestamp": "2026-07-10T12:00:00",
        "timestamp_cutoff": "2026-04-15T00:00:00",
        "origin": BACKFILL_ORIGIN,
    }


def _write_meta(path, meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _read_meta(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_frame_parquet(path, frame):
    """Write a frame in save_cache's on-disk layout (DateTime as a column)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.reset_index(drop=True).to_parquet(
        str(path), index=False, engine="pyarrow"
    )


def _make_dm(tmp_path, *, data_client=None, front_month_id="CLQ6",
             execution_symbol=None):
    """Real DataManager fully path-isolated to tmp_path (never touches
    C:\\CL_Analyst_Data). bar_size='1 hour' — every fleet brain is 1h."""
    kwargs = dict(
        symbol="CL",
        data_client=data_client,
        seed_path=str(tmp_path / "unused_seed.csv"),
        cache_path=str(tmp_path / "warm_start_cache_1h.parquet"),
        master_ledger_path=str(tmp_path / "ledger.parquet"),
        roll_metadata_path=str(tmp_path / ".roll_metadata.json"),
        front_month_id=front_month_id,
        bar_size="1 hour",
        bars_per_day=24,
    )
    if execution_symbol is not None:
        kwargs["execution_symbol"] = execution_symbol
    return DataManager(**kwargs)


@pytest.fixture
def isolated_backup_dir(tmp_path):
    """Redirect the repo-level cache-backup dir into tmp_path (initialize()
    step 6 writes there). Pre-seeded with a sentinel parquet so the
    'first run' backup trigger stays off unless a roll fires."""
    bdir = tmp_path / "_isolated_cache_backups"
    bdir.mkdir()
    (bdir / "sentinel.parquet").touch()
    with mock.patch.object(dm_module, "_CACHE_BACKUP_DIR", bdir):
        yield bdir


# ===========================================================================
# A. Outcome protocol constants
# ===========================================================================

class TestSeamOutcomeConstants:

    def test_outcome_constants_exported(self):
        """EXPECTED-RED (AttributeError): the outcome protocol must be
        importable module constants so live_trader compares against the
        same strings resolve_roll_seam returns."""
        assert dm_module.ROLL_SEAM_RESOLVED == "RESOLVED"
        assert dm_module.ROLL_SEAM_RETRY == "RETRY"
        assert dm_module.ROLL_SEAM_ESCALATE == "ESCALATE"


# ===========================================================================
# B. resolve_roll_seam() state machine (blueprint Stage-2 item 1)
# ===========================================================================

class TestResolveRollSeam:

    def _dm(self, tmp_path, fetch_frame, cache_close=None, meta=None):
        _write_meta(tmp_path / ".roll_metadata.json",
                    _base_meta() if meta is None else meta)
        dm = _make_dm(tmp_path, data_client=_feed(fetch_frame))
        dm._df = _ohlcv(_u_close() if cache_close is None else cache_close)
        return dm

    def test_all_old_basis_returns_retry_records_nothing(self, tmp_path):
        """EXPECTED-RED (AttributeError: resolve_roll_seam). Mid-run
        context: every overlap bar has q = R_TRUE != 1 (cache still fully
        old-basis, fetch new-basis) -> "RETRY". Recording now would strand
        later old-basis appends unadjusted, so NOTHING may be recorded
        in-memory or persisted."""
        dm = self._dm(tmp_path, _fetch_new_basis())  # cache = all old basis

        outcome = dm.resolve_roll_seam(
            from_contract="CLN6", to_contract="CLQ6",
            detected_at=pd.Timestamp("2026-06-27 00:00"),
        )

        assert outcome == "RETRY"
        assert dm._roll_ratios == []
        assert dm._roll_timestamps == []
        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        assert meta_after["roll_history"] == [], (
            "RETRY must not persist any roll_history entry"
        )

    def test_all_new_basis_returns_escalate_records_nothing(self, tmp_path):
        """EXPECTED-RED (AttributeError: resolve_roll_seam). Every overlap
        bar has q == 1 (nothing to anchor on) -> "ESCALATE", nothing
        recorded, nothing persisted."""
        dm = self._dm(tmp_path, _fetch_identical())  # q == 1 everywhere

        outcome = dm.resolve_roll_seam(
            from_contract="CLN6", to_contract="CLQ6",
            detected_at=pd.Timestamp("2026-06-27 00:00"),
        )

        assert outcome == "ESCALATE"
        assert dm._roll_ratios == []
        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        assert meta_after["roll_history"] == []

    def test_old_run_then_new_run_resolves_and_persists_immediately(
        self, tmp_path
    ):
        """EXPECTED-RED (AttributeError: resolve_roll_seam). Old-basis run
        (40 bars, q=R_TRUE) followed by new-basis run (32 bars, q=1) ->
        "RESOLVED": ratio == median of the old-run quotients, cutoff ==
        FIRST new-basis bar timestamp, appended to _roll_ratios /
        _roll_timestamps AND persisted to the metadata file IMMEDIATELY —
        no initialize() call anywhere in this test. A pending_roll record
        must survive untouched (resolve does not own the pending
        lifecycle)."""
        meta = _base_meta()
        meta["pending_roll"] = {
            "CL": {"from": "CLN6", "to": "CLQ6",
                   "detected_at": "2026-06-27T00:00:00"},
        }
        dm = self._dm(tmp_path, _fetch_new_basis(),
                      cache_close=_mixed_close(), meta=meta)

        outcome = dm.resolve_roll_seam(
            from_contract="CLN6", to_contract="CLQ6",
            detected_at=pd.Timestamp("2026-06-27 00:00"),
        )

        assert outcome == "RESOLVED"
        # In-memory record
        assert len(dm._roll_ratios) == 1
        assert dm._roll_ratios[-1] == pytest.approx(R_TRUE, abs=1e-9)
        assert dm._roll_timestamps[-1] == FLIP_TS, (
            "cutoff must be the FIRST new-basis bar timestamp"
        )
        # Persisted IMMEDIATELY (no initialize())
        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        history = meta_after["roll_history"]
        assert len(history) == 1, (
            "roll_history must grow on disk at resolution time — mid-run "
            "persistence must not depend on initialize()"
        )
        entry = history[0]
        assert entry["from"] == "CLN6"
        assert entry["to"] == "CLQ6"   # execution-symbol-prefixed live schema
        assert entry["ratio"] == pytest.approx(R_TRUE, abs=1e-6)
        assert pd.Timestamp(entry["timestamp_cutoff"]) == FLIP_TS
        # resolve_roll_seam must NOT clear pending_roll (caller-owned)
        assert meta_after["pending_roll"]["CL"]["from"] == "CLN6"

    def test_median_robust_to_contaminated_old_run_tail(self, tmp_path):
        """EXPECTED-RED (AttributeError: resolve_roll_seam). 5 outlier bars
        inside the 40-bar old-basis run (each still |q-1| > tolerance under
        BOTH the current 0.01 and the new 0.001 CL tolerance) must not move
        the recovered ratio: median of 35x0.97 + {0.95,0.985,...} == 0.97
        exactly."""
        fetch = _fetch_new_basis()
        idx = _u_close().index
        # Absolute positions inside the old run (128..167)
        for pos, scale in ((130, 0.95), (135, 0.985), (140, 0.95),
                           (145, 0.985), (150, 0.95)):
            ts = idx[pos]
            base = _u_close().iloc[pos]
            for col in ("Open", "High", "Low", "Close"):
                fetch.loc[ts, col] = base * scale
        dm = self._dm(tmp_path, fetch, cache_close=_mixed_close())

        outcome = dm.resolve_roll_seam(
            from_contract="CLN6", to_contract="CLQ6",
            detected_at=pd.Timestamp("2026-06-27 00:00"),
        )

        assert outcome == "RESOLVED"
        assert dm._roll_ratios[-1] == pytest.approx(R_TRUE, abs=1e-9), (
            "ratio must be the MEDIAN of the old-basis run quotients — "
            "a mean would be dragged by the contaminated tail"
        )
        assert dm._roll_timestamps[-1] == FLIP_TS

    def test_resolved_entry_round_trips_through_initialize(
        self, tmp_path, isolated_backup_dir
    ):
        """EXPECTED-RED (AttributeError: resolve_roll_seam). The entry a
        RESOLVED scan persists must follow the existing live schema so a
        FRESH DataManager construction restores it via the Step-0 ownership
        filter and get_ratio_adjusted_df() reproduces the continuous
        series."""
        cache_close = _mixed_close()
        _write_frame_parquet(
            tmp_path / "warm_start_cache_1h.parquet", _ohlcv(cache_close)
        )
        dm = self._dm(tmp_path, _fetch_new_basis(), cache_close=cache_close)
        outcome = dm.resolve_roll_seam(
            from_contract="CLN6", to_contract="CLQ6",
            detected_at=pd.Timestamp("2026-06-27 00:00"),
        )
        assert outcome == "RESOLVED"

        dm2 = _make_dm(tmp_path, data_client=None, front_month_id=None)
        dm2.initialize()

        assert len(dm2._roll_ratios) == 1, (
            "the persisted entry must be restored on the next construction "
            '(its "to" must pass the CL execution-symbol ownership filter)'
        )
        assert dm2._roll_ratios[-1] == pytest.approx(R_TRUE, abs=1e-6)
        assert dm2._roll_timestamps[-1] == FLIP_TS
        adj = dm2.get_ratio_adjusted_df()
        # Continuous new-basis series: adjusted / (U * R_TRUE) constant at 1
        q = adj["Close"].to_numpy() / (_u_close().to_numpy() * R_TRUE)
        assert float(np.max(np.abs(q - 1.0))) < 1e-6, (
            "restored ratio replay must produce a seamless series"
        )


# ===========================================================================
# C. _append_roll_event() — immediate persistence, preservation
# ===========================================================================

class TestAppendRollEvent:

    def test_appends_in_memory_and_persists_preserving_entries_and_keys(
        self, tmp_path
    ):
        """EXPECTED-RED (AttributeError: _append_roll_event). Appends to
        _roll_ratios/_roll_timestamps AND writes the metadata file in the
        same call; prior roll_history entries (live-schema AND Stage-1
        origin-stamped no-"to" entries) must survive byte-identical, as
        must unrelated keys (sentinel)."""
        meta = _base_meta()
        meta["roll_history"] = [_live_entry(), _backfill_entry()]
        _write_meta(tmp_path / ".roll_metadata.json", meta)
        dm = _make_dm(tmp_path, front_month_id="CLQ6")
        cutoff = pd.Timestamp("2026-06-27 08:00")

        dm._append_roll_event("CLN6", "CLQ6", 0.98, cutoff)

        # In-memory
        assert dm._roll_ratios == [pytest.approx(0.98, abs=1e-9)]
        assert dm._roll_timestamps == [cutoff]
        # Persisted immediately — no initialize() anywhere in this test
        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        history = meta_after["roll_history"]
        assert len(history) == 3
        assert history[0] == _live_entry(), (
            "pre-existing live entries must be preserved byte-identical"
        )
        assert history[1] == _backfill_entry(), (
            'Stage-1 origin-stamped no-"to" entries must be preserved '
            "byte-identical"
        )
        new = history[2]
        assert new["from"] == "CLN6"
        assert new["to"] == "CLQ6"
        assert new["ratio"] == pytest.approx(0.98, abs=1e-6)
        assert pd.Timestamp(new["timestamp_cutoff"]) == cutoff
        pd.Timestamp(new["timestamp"])  # parseable write-time stamp
        # Unrelated keys preserved
        assert meta_after[SENTINEL_KEY] == SENTINEL_VAL
        assert "last_front_month" in meta_after
        assert "last_front_month_by_symbol" in meta_after
        assert "cumulative_ratio" in meta_after


# ===========================================================================
# D. Pending-roll accessors
# ===========================================================================

class TestPendingRollAccessors:

    def test_set_pending_roll_persists_immediately(self, tmp_path):
        """EXPECTED-RED (AttributeError: set_pending_roll). Persists
        {from, to, detected_at} under meta["pending_roll"][exec_symbol]
        with NO initialize() call; unrelated keys survive."""
        _write_meta(tmp_path / ".roll_metadata.json", _base_meta())
        dm = _make_dm(tmp_path, front_month_id="CLQ6")

        dm.set_pending_roll("CLN6", "CLQ6")

        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        rec = meta_after["pending_roll"]["CL"]
        assert rec["from"] == "CLN6"
        assert rec["to"] == "CLQ6"
        assert isinstance(rec["detected_at"], str)
        pd.Timestamp(rec["detected_at"])  # parseable ISO stamp
        assert meta_after[SENTINEL_KEY] == SENTINEL_VAL
        assert meta_after["roll_history"] == []

    def test_get_pending_roll_disk_backed_on_fresh_instance(self, tmp_path):
        """EXPECTED-RED (AttributeError: get_pending_roll). A record
        written by another process must be visible to a FRESH instance
        that never ran initialize()."""
        meta = _base_meta()
        meta["pending_roll"] = {
            "CL": {"from": "CLN6", "to": "CLQ6",
                   "detected_at": "2026-07-08T00:00:00"},
        }
        _write_meta(tmp_path / ".roll_metadata.json", meta)
        dm = _make_dm(tmp_path, front_month_id="CLQ6")

        rec = dm.get_pending_roll()

        assert rec is not None
        assert rec["from"] == "CLN6"
        assert rec["to"] == "CLQ6"
        assert rec["detected_at"] == "2026-07-08T00:00:00"

    def test_get_pending_roll_none_and_foreign_symbol_invisible(
        self, tmp_path
    ):
        """EXPECTED-RED (AttributeError: get_pending_roll). No record ->
        None; another execution symbol's record -> None (namespaced)."""
        meta = _base_meta()
        _write_meta(tmp_path / ".roll_metadata.json", meta)
        dm = _make_dm(tmp_path, front_month_id="CLQ6")
        assert dm.get_pending_roll() is None

        meta["pending_roll"] = {
            "NG": {"from": "NGQ6", "to": "NGU6",
                   "detected_at": "2026-07-08T00:00:00"},
        }
        _write_meta(tmp_path / ".roll_metadata.json", meta)
        assert dm.get_pending_roll() is None, (
            "a foreign execution symbol's pending record must be invisible"
        )

    def test_clear_pending_roll_removes_only_own_symbol(self, tmp_path):
        """EXPECTED-RED (AttributeError: clear_pending_roll). Clearing must
        remove ONLY this execution symbol's record; other symbols' pending
        records and unrelated keys survive on disk."""
        meta = _base_meta()
        meta["pending_roll"] = {
            "CL": {"from": "CLN6", "to": "CLQ6",
                   "detected_at": "2026-07-08T00:00:00"},
            "NG": {"from": "NGQ6", "to": "NGU6",
                   "detected_at": "2026-07-09T00:00:00"},
        }
        _write_meta(tmp_path / ".roll_metadata.json", meta)
        dm = _make_dm(tmp_path, front_month_id="CLQ6")

        dm.clear_pending_roll()

        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        assert meta_after.get("pending_roll", {}).get("CL") is None, (
            "CL's pending record must be gone after clear"
        )
        assert meta_after["pending_roll"]["NG"]["from"] == "NGQ6", (
            "another symbol's pending record must survive the clear"
        )
        assert meta_after[SENTINEL_KEY] == SENTINEL_VAL


# ===========================================================================
# E. initialize() pending-roll resolution + hard-fail + sanctioned skip
# ===========================================================================

def _init_scenario(tmp_path, *, cache_close, fetch_frame, meta,
                   front_month_id):
    """Full-initialize scenario: cache + ledger parquet + metadata on disk,
    stub feed returning `fetch_frame` for every historical fetch."""
    frame = _ohlcv(cache_close)
    _write_frame_parquet(tmp_path / "warm_start_cache_1h.parquet", frame)
    _write_frame_parquet(tmp_path / "ledger.parquet", frame)
    _write_meta(tmp_path / ".roll_metadata.json", meta)
    return _make_dm(tmp_path, data_client=_feed(fetch_frame),
                    front_month_id=front_month_id)


class TestInitializePendingRoll:

    def test_pending_roll_resolved_at_startup_clears_and_records_once(
        self, tmp_path, isolated_backup_dir
    ):
        """EXPECTED-RED (AssertionError: no ratio recorded — today's
        initialize() ignores pending_roll entirely). An unresolved
        pending_roll for THIS execution symbol + an anchorable seam
        (old-run then new-run) must: record the ratio EXACTLY ONCE
        (no duplicate append from the Step-5 writer), cutoff at the first
        new-basis bar, and clear the pending record on disk."""
        meta = _base_meta(front="CLQ6")  # front month UNCHANGED
        meta["pending_roll"] = {
            "CL": {"from": "CLN6", "to": "CLQ6",
                   "detected_at": "2026-06-27T00:00:00"},
        }
        dm = _init_scenario(
            tmp_path, cache_close=_mixed_close(),
            fetch_frame=_fetch_new_basis(), meta=meta,
            front_month_id="CLQ6",
        )

        dm.initialize()

        assert len(dm._roll_ratios) == 1, (
            "initialize() must resolve an unresolved pending_roll via the "
            "seam scan and record the ratio"
        )
        assert dm._roll_ratios[-1] == pytest.approx(R_TRUE, abs=1e-6)
        assert dm._roll_timestamps[-1] == FLIP_TS
        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        history = meta_after["roll_history"]
        assert len(history) == 1, (
            "the resolved roll must be persisted EXACTLY ONCE — the Step-5 "
            "writer must not double-append it"
        )
        assert history[0]["from"] == "CLN6"
        assert history[0]["to"] == "CLQ6"
        assert pd.Timestamp(history[0]["timestamp_cutoff"]) == FLIP_TS
        assert meta_after.get("pending_roll", {}).get("CL") is None, (
            "the pending record must be cleared on success"
        )

    def test_pending_roll_unanchorable_hard_fails(
        self, tmp_path, isolated_backup_dir
    ):
        """EXPECTED-RED (Failed: DID NOT RAISE — today the seam is
        silently lost). pending_roll present but the scan cannot anchor
        (ALL overlap bars new-basis, q == 1): initialize() must raise
        RuntimeError with an actionable message — NOT the silent
        'within tolerance' skip. Loud stop over silent wrong-basis
        trading."""
        meta = _base_meta(front="CLQ6")
        meta["pending_roll"] = {
            "CL": {"from": "CLN6", "to": "CLQ6",
                   "detected_at": "2026-06-27T00:00:00"},
        }
        dm = _init_scenario(
            tmp_path, cache_close=_u_close(),
            fetch_frame=_fetch_identical(), meta=meta,
            front_month_id="CLQ6",
        )

        with pytest.raises(RuntimeError) as excinfo:
            dm.initialize()

        msg = str(excinfo.value).lower()
        assert "pending" in msg and "roll" in msg, (
            "the hard-fail message must name the pending roll so the "
            f"operator remedy is actionable; got: {excinfo.value}"
        )

    def test_small_genuine_roll_without_pending_still_skips_quietly(
        self, tmp_path, isolated_backup_dir
    ):
        """EXPECTED-GREEN (characterization pin — must pass today AND after
        the fix). NO pending_roll + a startup-witnessed front-month change
        whose genuine gap (5 bps) is within tolerance under BOTH the old
        0.01 and the new 0.001 CL tolerance: the silent skip is CORRECT
        behavior. Only the un-anchorable pending case hard-fails."""
        meta = _base_meta(front="CLN6")  # front month changed CLN6 -> CLQ6
        dm = _init_scenario(
            tmp_path, cache_close=_u_close(),
            fetch_frame=_fetch_new_basis(scale=1.0005), meta=meta,
            front_month_id="CLQ6",
        )

        dm.initialize()  # must NOT raise

        assert dm._roll_ratios == [], (
            "a 5 bps genuine roll is below the noise floor — adjustment "
            "must be skipped"
        )
        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        assert meta_after["roll_history"] == []


# ===========================================================================
# F. Amendment 1 — single-seam invariant (end-to-end initialize flow)
# ===========================================================================

class TestSingleSeamInvariant:

    def test_startup_witnessed_roll_produces_at_most_one_seam(
        self, tmp_path, isolated_backup_dir
    ):
        """EXPECTED-RED (AssertionError: 2 seam steps today). Startup roll
        replayed through the REAL initialize() (detect -> compute ratio ->
        apply with overlap overwrite -> backfill): the adjusted output vs
        the true continuous underlying U must show AT MOST ONE seam step
        and NO double-adjusted (r^2) plateau. Today _apply_roll_to_cache
        records cutoff = index.max() and THEN overwrites the overlap with
        new-basis bars, so the JIT replay lands those bars at U*r^2 —
        a spurious step at overlap-start and a second at the last bar.
        The test pins the INVARIANT, not the Coder's cutoff convention:
        both 'cutoff = first overwritten bar' and 'no overwrite' solutions
        pass, provided they hold end-to-end (including _backfill's
        dedup keep='last' overwrite of the same window)."""
        meta = _base_meta(front="CLH6")  # front month changed CLH6 -> CLM6
        dm = _init_scenario(
            tmp_path, cache_close=_u_close(),
            fetch_frame=_fetch_new_basis(scale=R_AMEND), meta=meta,
            front_month_id="CLM6",
        )

        dm.initialize()

        assert dm._roll_ratios, "harness sanity: the roll must be applied"
        assert dm._roll_ratios[-1] == pytest.approx(R_AMEND, rel=1e-6)

        adj = dm.get_ratio_adjusted_df()
        u = _u_close()
        assert len(adj) == len(u), "harness sanity: no bars added/removed"
        q = adj["Close"].to_numpy() / u.to_numpy()

        # History must land on the new basis: q[0] == r under any correct
        # convention (the recorded ratio multiplies all pre-cutoff bars).
        assert abs(q[0] - R_AMEND) < 1e-6, (
            "pre-roll history must be scaled by the roll ratio"
        )

        # Invariant 1: at most ONE seam step in adjusted / true-underlying.
        rel_step = np.abs(np.diff(q) / q[:-1])
        n_steps = int((rel_step > 1e-6).sum())
        assert n_steps <= 1, (
            f"adjusted series has {n_steps} seam steps vs the true "
            "continuous series — the overlap window is basis-inconsistent "
            "(cutoff recorded AFTER overwriting the overlap with new-basis "
            "bars => JIT replay double-adjusts it)"
        )

        # Invariant 2: every plateau is r or 1 — NEVER r^2 (double-adjusted).
        dist = np.minimum(np.abs(q - R_AMEND), np.abs(q - 1.0))
        assert float(dist.max()) < 1e-6, (
            "adjusted/true quotient contains a plateau that is neither the "
            "roll ratio nor 1 — a double-adjusted (r^2) overlap window"
        )


# ===========================================================================
# G. Tolerance registry — Amendment 2 (reverses the T5 zero-change pin)
# ===========================================================================

class TestRollToleranceAmendment2:

    def test_cl_tolerance_is_10bps(self):
        """EXPECTED-RED (AssertionError: 0.01 != 0.001). Real CL roll gaps
        are 0.2-2.8%; the legacy 0.01 tolerance silently swallows any gap
        < 1% even when correctly witnessed."""
        assert get_instrument("CL").roll_ratio_tolerance == 0.001

    def test_mcl_tolerance_is_10bps(self):
        """EXPECTED-RED (AssertionError: 0.01 != 0.001). MCL shares CL's
        metadata file semantics and must move with it."""
        assert get_instrument("MCL").roll_ratio_tolerance == 0.001

    def test_cl_40bps_roll_gap_is_applied(
        self, tmp_path, isolated_backup_dir
    ):
        """EXPECTED-RED (AssertionError: ratio swallowed today). A 40 bps
        CL roll gap witnessed at startup must be APPLIED under the new
        0.001 tolerance (today's 0.01 swallows it — sub-tolerance seams
        compound over months). NOTE: this deliberately contradicts the
        pre-existing pin tests/test_session_watchdog_rollover.py
        parametrize case ("CL", 1.004, False) at :866, which pins today's
        swallow — that line must be adjudicated when this ticket lands."""
        meta = _base_meta(front="CLH6")
        dm = _init_scenario(
            tmp_path, cache_close=_u_close(),
            fetch_frame=_fetch_new_basis(scale=1.004), meta=meta,
            front_month_id="CLM6",
        )

        dm.initialize()

        assert dm._roll_ratios, (
            "a 40 bps CL roll gap must be applied under the 0.001 tolerance"
        )
        assert dm._roll_ratios[-1] == pytest.approx(1.004, rel=1e-6)
        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        assert len(meta_after["roll_history"]) == 1
        assert meta_after["roll_history"][0]["ratio"] == pytest.approx(
            1.004, abs=1e-4
        )


# ===========================================================================
# Requirement 6 — restore-loop characterization (swap-proof no-"to" entries)
# ===========================================================================

class TestBackfillEntryRestore:

    @pytest.mark.parametrize(
        "exec_sym",
        ["CL", "MES", "MGC", "NG", "SIL", "SI"],  # SI = hypothetical swap
    )
    def test_no_to_entries_restore_under_every_execution_symbol(
        self, tmp_path, isolated_backup_dir, exec_sym
    ):
        """EXPECTED-GREEN (characterization pin of the existing Step-0
        legacy-restore branch). Entries lacking a "to" key (Stage-1
        origin-stamped backfill schema) must be restored unconditionally
        under every current fleet execution symbol AND under a
        hypothetical swapped symbol (SIL -> SI): restoration must be
        execution-symbol independent."""
        _write_frame_parquet(
            tmp_path / "warm_start_cache_1h.parquet", _ohlcv(_u_close())
        )
        meta = _base_meta()
        meta["roll_history"] = [_backfill_entry()]
        _write_meta(tmp_path / ".roll_metadata.json", meta)
        dm = _make_dm(tmp_path, data_client=None, front_month_id=None,
                      execution_symbol=exec_sym)

        dm.initialize()

        assert len(dm._roll_ratios) == 1, (
            f"execution_symbol={exec_sym!r} failed to restore the "
            'no-"to" backfill entry — restoration must be swap-proof'
        )
        assert dm._roll_ratios[0] == pytest.approx(0.99, abs=1e-9)


# ===========================================================================
# Requirement 8 — metadata schema back-compat (keys are additive)
# ===========================================================================

class TestMetadataSchemaBackCompat:

    def test_new_schema_file_readable_by_current_reader(
        self, tmp_path, isolated_backup_dir
    ):
        """EXPECTED-GREEN (characterization pin — must pass today AND after
        the fix; today's pass proves a REVERT of Stage 2 keeps files
        written by the new path readable). A metadata file carrying the
        NEW keys (pending_roll for a foreign symbol, origin-stamped
        entries) initializes without error and restores exactly the
        entries the ownership filter allows."""
        _write_frame_parquet(
            tmp_path / "warm_start_cache_1h.parquet", _ohlcv(_u_close())
        )
        meta = _base_meta()
        meta["roll_history"] = [_live_entry(), _backfill_entry()]
        meta["pending_roll"] = {
            "NG": {"from": "NGQ6", "to": "NGU6",
                   "detected_at": "2026-07-09T00:00:00"},
        }
        _write_meta(tmp_path / ".roll_metadata.json", meta)
        dm = _make_dm(tmp_path, data_client=None, front_month_id=None)

        dm.initialize()  # must not raise on the additive keys

        assert len(dm._roll_ratios) == 2, (
            "both the CL-owned live entry and the no-\"to\" backfill entry "
            "must restore; the additive pending_roll/origin keys must be "
            "inert to the reader"
        )
        adj = dm.get_ratio_adjusted_df()
        assert len(adj) == N_BARS
