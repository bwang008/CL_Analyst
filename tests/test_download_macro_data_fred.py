"""
TDD-TESTER AUTHORIZATION
Target Implementation File: scripts/download_macro_data.py
Target Class/Function: download_fred_data (F1), save_fred_data (F2), main (F3)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: fred-macro-vol-column-missing_07152026_1836
Phase: RED — pins the REQUIRED post-fix behaviour of the FRED producer.

Bug being pinned
----------------
scripts/download_macro_data.py silently drops a FRED series that fails to
fetch and writes the resulting *partial* file anyway, overwriting a known-good
one. The live trader corrupted its own input (GC lost GVZ, SI lost FED_FUNDS).

These tests fail against CURRENT code for the RIGHT reason:
  * F1 — download_fred_data() catches a per-series failure / empty response,
    logs, and returns a PARTIAL dict instead of raising (and never retries).
  * F2 — save_fred_data() writes with NO schema validation, non-atomically,
    clobbering a known-good file.
  * F3 — main()'s inline CLI writer never routes through save_fred_data(), so a
    partial download is written without validation.

Hermeticity
-----------
  * The FRED network seam (`fred.get_series`) is fully stubbed — no real FRED
    API call, no FRED_API_KEY required. See `_patch_fred_seam`.
  * Every writing test monkeypatches `download_macro_data.OUTPUT_DIR` to a
    tmp_path; nothing under C:\\CL_Analyst_Data is ever touched.

Must-stay-green siblings (NOT modified by this file):
  tests/test_cot_adapters.py, tests/test_macro_vol_parameterization.py
"""

from __future__ import annotations

import contextlib
from collections import Counter
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import scripts.download_macro_data as dm
from src.core.instrument_master import get_instrument

# Contract source of truth (F2's required-column set is derived from these —
# the fix imports the SAME two symbols so producer & consumer agree by
# construction). Importing them here, not restating the literals, mirrors the
# blueprint's "do not mint a third copy of the schema" requirement.
from src.features.macro_features import _FRED_BASE_COLS, vol_label_for


# ---------------------------------------------------------------------------
# FRED seam stub — replaces `fred.get_series(series_id)` (line 104) with a
# controllable MagicMock, patched at BOTH the current in-function import
# (`from fredapi import Fred`) and a hoisted post-fix module-level `Fred`.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _patch_fred_seam(get_series_side_effect):
    """Yield the get_series MagicMock (for retry-count assertions).

    `get_series_side_effect(series_id)` may return a pandas Series (success),
    return None / empty Series (empty response), or raise (fetch failure).
    """
    get_series = MagicMock(name="fred.get_series", side_effect=get_series_side_effect)

    class _FakeFred:
        def __init__(self, *args, **kwargs):
            # never stores/uses a real api_key; no network client is created
            pass

    # A MagicMock is not a descriptor, so binding it as a class attribute
    # means `instance.get_series(sid)` calls the mock with just `sid` (no self).
    _FakeFred.get_series = get_series

    with patch("fredapi.Fred", _FakeFred), \
            patch.object(dm, "Fred", _FakeFred, create=True):
        yield get_series


def _series(n: int = 40) -> pd.Series:
    """A healthy FRED-style Series: DatetimeIndex + float values."""
    idx = pd.bdate_range("2026-01-02", periods=n)
    return pd.Series(np.linspace(10.0, 20.0, n), index=idx)


def _fred_frame(label: str, n: int = 40) -> pd.DataFrame:
    """A per-label FRED DataFrame shaped exactly like download_fred_data's
    `results[label]` entries: columns ["Date", <label>]."""
    idx = pd.bdate_range("2026-01-02", periods=n)
    return pd.DataFrame({"Date": pd.to_datetime(idx),
                         label: np.linspace(10.0, 20.0, n)})


# The GC FRED label set (base + GVZ vol label). Derived from the contract, not
# restated: vol_label_for(GC) == "GVZ".
def _gc_labels() -> list[str]:
    return list(_FRED_BASE_COLS) + [vol_label_for(get_instrument("GC"))]


def _gc_full_data() -> dict[str, pd.DataFrame]:
    return {lbl: _fred_frame(lbl) for lbl in _gc_labels()}


def _gc_partial_data_missing_gvz() -> dict[str, pd.DataFrame]:
    return {lbl: _fred_frame(lbl) for lbl in _FRED_BASE_COLS}  # no GVZ


# ===========================================================================
# F1 — download_fred_data must never return a partial dict
# ===========================================================================

class TestDownloadFredDataFailsLoud:
    def test_failing_series_raises_naming_series_id(self):
        """A series that fails on EVERY attempt -> RuntimeError naming the
        FRED series id (GVZCLS). CURRENT code catches + returns a partial
        dict (no raise) -> RED (DID NOT RAISE)."""
        def se(series_id):
            if series_id == "GVZCLS":
                raise ConnectionError("simulated transient outage")
            return _series()

        with _patch_fred_seam(se):
            with pytest.raises(RuntimeError) as ei:
                dm.download_fred_data("dummy_key", instrument=get_instrument("GC"))
        # Names the exact FRED series id so the operator knows what failed.
        assert "GVZCLS" in str(ei.value), str(ei.value)

    @pytest.mark.parametrize(
        "empty_value",
        [pd.Series(dtype=float), None],
        ids=["empty-series", "none"],
    )
    def test_empty_response_raises(self, empty_value):
        """An empty / None response on every attempt is the silent-drop path
        at :105-107 (log.warning + continue). The fix must treat it as a
        failure and raise. CURRENT code drops it silently -> RED."""
        def se(series_id):
            if series_id == "GVZCLS":
                return empty_value
            return _series()

        with _patch_fred_seam(se):
            with pytest.raises(RuntimeError) as ei:
                dm.download_fred_data("dummy_key", instrument=get_instrument("GC"))
        assert "GVZCLS" in str(ei.value), str(ei.value)

    def test_transient_failure_recovers_via_retry(self):
        """A series that fails once then succeeds must NOT raise, and the
        recovered series MUST be present in the returned dict. Pins the
        bounded-retry contract loosely: does-not-raise + series present +
        get_series called >1 time for that series. CURRENT code never retries
        -> GVZ absent -> RED."""
        attempts = Counter()

        def se(series_id):
            attempts[series_id] += 1
            if series_id == "GVZCLS" and attempts[series_id] < 2:
                raise ConnectionError("first-attempt blip")
            return _series()

        with _patch_fred_seam(se) as get_series:
            result = dm.download_fred_data("dummy_key",
                                           instrument=get_instrument("GC"))

        assert "GVZ" in result, (
            "recovered series missing from dict — no retry happened "
            f"(keys={sorted(result)})"
        )
        assert attempts["GVZCLS"] > 1, (
            "GVZCLS was not retried after a transient failure "
            f"(attempts={dict(attempts)})"
        )
        # Sanity: the seam was actually exercised (not short-circuited).
        assert get_series.call_count >= len(_gc_labels())

    def test_happy_path_returns_all_series_including_vol_label(self):
        """Guardrail (stays green): all series present -> full dict whose
        labels include the instrument's vol label (GVZ for GC)."""
        with _patch_fred_seam(lambda sid: _series()):
            result = dm.download_fred_data("dummy_key",
                                           instrument=get_instrument("GC"))
        assert set(result) >= set(_gc_labels())
        assert vol_label_for(get_instrument("GC")) in result  # GVZ present


# ===========================================================================
# F2 — save_fred_data must validate schema before writing (and never corrupt
#      a good file)
# ===========================================================================

class TestSaveFredDataSchemaGuard:
    def test_missing_required_column_raises(self, tmp_path, monkeypatch):
        """A frame MISSING a required column (GVZ for GC) must raise instead
        of writing. CURRENT code writes it with no validation -> RED."""
        monkeypatch.setattr(dm, "OUTPUT_DIR", tmp_path)
        assert dm.OUTPUT_DIR == tmp_path  # never the real data dir

        with pytest.raises((ValueError, RuntimeError)) as ei:
            dm.save_fred_data(_gc_partial_data_missing_gvz(),
                              instrument=get_instrument("GC"))
        assert "GVZ" in str(ei.value), str(ei.value)  # names the missing column

    def test_failed_save_leaves_existing_good_file_byte_identical(
        self, tmp_path, monkeypatch
    ):
        """THE core guarantee (the Auditor's byte-identical reproduction):
        a failed save must NOT overwrite a pre-existing known-good file.

        CURRENT code clobbers the good file with the partial one -> the
        byte-identity assertion fails -> RED. This is exactly how the live
        trader corrupted its own input.
        """
        monkeypatch.setattr(dm, "OUTPUT_DIR", tmp_path)
        assert dm.OUTPUT_DIR == tmp_path

        dest = tmp_path / "fred_macro_data_gc.csv"
        good_bytes = (
            b"Date,VIX,DXY,YIELD_CURVE,FED_FUNDS,GVZ\n"
            b"2026-01-02,15.0,102.0,0.5,4.25,17.5\n"
            b"KNOWN_GOOD_SENTINEL_DO_NOT_CORRUPT\n"
        )
        dest.write_bytes(good_bytes)
        before = dest.read_bytes()

        raised = False
        try:
            dm.save_fred_data(_gc_partial_data_missing_gvz(),
                              instrument=get_instrument("GC"))
        except Exception:
            raised = True

        # Core assertion — checked regardless of whether it raised.
        assert dest.read_bytes() == before, (
            "known-good FRED file was overwritten by a failed/partial save"
        )
        assert raised, "save_fred_data must raise on a missing required column"
        # A failed save must not litter the dir with a leftover temp file.
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != dest.name]
        assert leftovers == [], f"failed save left temp artifacts: {leftovers}"

    def test_happy_path_writes_file_and_leaves_no_temp(self, tmp_path, monkeypatch):
        """Guardrail: a complete GC frame writes correctly, atomically — the
        destination has all required columns and NO leftover temp file remains
        (the fix writes via temp + os.replace)."""
        monkeypatch.setattr(dm, "OUTPUT_DIR", tmp_path)
        assert dm.OUTPUT_DIR == tmp_path

        dm.save_fred_data(_gc_full_data(), instrument=get_instrument("GC"))

        dest = tmp_path / "fred_macro_data_gc.csv"
        assert dest.exists()
        written = pd.read_csv(dest)
        required = set(_FRED_BASE_COLS) | {vol_label_for(get_instrument("GC"))}
        assert required.issubset(set(written.columns)), set(written.columns)
        # Atomic-write cleanup: nothing but the destination file remains.
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != dest.name]
        assert leftovers == [], f"leftover temp artifacts after write: {leftovers}"

    def test_vix_proxy_symbol_base_only_frame_is_complete(self, tmp_path, monkeypatch):
        """VIX-dedup case: for a VIX-proxy symbol (SI) vol_label_for == 'VIX',
        which is already in _FRED_BASE_COLS, so a base-only frame is COMPLETE
        and must NOT raise. Pins that the fix uses vol_label_for (not a naive
        'always require a distinct vol column')."""
        monkeypatch.setattr(dm, "OUTPUT_DIR", tmp_path)
        si = get_instrument("SI")
        assert vol_label_for(si) == "VIX"  # sanity: SI is a VIX proxy

        base_only = {lbl: _fred_frame(lbl) for lbl in _FRED_BASE_COLS}
        # Must not raise:
        dm.save_fred_data(base_only, instrument=si)

        dest = tmp_path / "fred_macro_data_si.csv"
        assert dest.exists()
        written = pd.read_csv(dest)
        assert set(_FRED_BASE_COLS).issubset(set(written.columns))


# ===========================================================================
# F3 — main() CLI FRED path must route through save_fred_data
# ===========================================================================
#
# Drivable cleanly: patch download_fred_data at the module seam, patch argv to
# `--symbol GC --fred-only --fred-key dummy` (so no COT network, no env var),
# and redirect OUTPUT_DIR. --fred-only sets do_cot=False, so no CFTC download.

class TestMainRoutesThroughSaveFredData:
    _ARGV = ["download_macro_data.py", "--symbol", "GC",
             "--fred-only", "--fred-key", "dummy_key"]

    def test_main_routes_partial_download_through_save_and_raises(
        self, tmp_path, monkeypatch
    ):
        """A partial download must make main() RAISE and write NO partial file
        — because the fix routes the CLI through the validating save_fred_data.
        CURRENT main() has its own inline writer that writes the partial file
        without validating -> partial file exists + no raise -> RED."""
        monkeypatch.setattr(dm, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(dm, "download_fred_data",
                            MagicMock(return_value=_gc_partial_data_missing_gvz()))

        dest = tmp_path / "fred_macro_data_gc.csv"
        raised = False
        try:
            with patch("sys.argv", self._ARGV):
                dm.main()
        except SystemExit:
            raise  # unexpected — surface it loudly
        except Exception:
            raised = True

        assert not dest.exists(), (
            "main() wrote a PARTIAL FRED file — CLI did not route through the "
            "validating save_fred_data"
        )
        assert raised, "main() must raise on a partial FRED download"

    def test_main_invokes_save_fred_data_with_instrument(self, tmp_path, monkeypatch):
        """Directly pins the routing: main() must call save_fred_data(...) with
        the instrument. CURRENT main() never calls it (dead in the CLI path)
        -> assert_called_once fails -> RED."""
        monkeypatch.setattr(dm, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(dm, "download_fred_data",
                            MagicMock(return_value=_gc_full_data()))
        save_spy = MagicMock(return_value=tmp_path / "fred_macro_data_gc.csv")
        monkeypatch.setattr(dm, "save_fred_data", save_spy)

        with patch("sys.argv", self._ARGV):
            dm.main()

        save_spy.assert_called_once()
        ca = save_spy.call_args
        inst = ca.kwargs.get("instrument",
                             ca.args[1] if len(ca.args) > 1 else None)
        assert inst is not None and inst.symbol == "GC", ca
