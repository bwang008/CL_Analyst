"""
Ticket: roll-seam-preflip-escalate_07162026 — CONTFUT-lead disambiguation
for the "all overlap bars new-basis" seam-scan outcome.

Incident (2026-07-16, CL fleet child crash loop): the fleet rolls its
front month roll_buffer_days BEFORE IBKR flips the CONTFUT lead (CL: LTD-6d
vs IBKR's flip at LTD). For that whole buffer window the cache and a fresh
CONTFUT fetch are BOTH still old-contract prices, so every overlap bar
matches (q ~= 1) and resolve_roll_seam() classified the window as "all
new-basis -> ESCALATE" — the same signature as a genuinely rebased
(anchor-destroyed) cache. initialize() then hard-failed on a state that is
routine for ~6 days after EVERY roll, on EVERY symbol.

Fix under test: on the all-matching signature, resolve_roll_seam() queries
the data client for IBKR's CURRENT continuous lead
(get_continuous_lead_local_symbol):
  - lead still the pending "from" contract (exact localSymbol OR month-code
    match — micros pend execution symbols like MGCQ6 while the brain
    CONTFUT lead is the parent GCQ6) -> RETRY (pre-flip buffer window;
    the seam has not appeared in the data yet).
  - lead flipped, unknown, unqueryable, or query fails -> ESCALATE
    (conservative status quo: never trade a potentially broken basis).

The Stage-2 contract file tests/test_roll_seam_capture.py is Strict-Lock
and stays untouched; its all-new-basis pins remain green because a bare
MagicMock feed cannot return a str lead (-> unknown -> ESCALATE).

Environment: conda env "trader" runs pandas 1.5.3 — no pandas>=2 APIs.
ALL fixtures are SYNTHETIC under tmp_path; C:\\CL_Analyst_Data is never
touched. _CACHE_BACKUP_DIR is patched wherever initialize() runs.
"""

import json
from unittest import mock
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import src.live_execution.data_manager as dm_module
from src.live_execution.data_manager import DataManager

# ---------------------------------------------------------------------------
# Synthetic series (deterministic — no RNG anywhere in this file)
# ---------------------------------------------------------------------------

N_BARS = 200
OVERLAP_N = 72
OVERLAP_START = N_BARS - OVERLAP_N


def _hidx(n=N_BARS, start="2026-07-01 00:00"):
    return pd.date_range(start, periods=n, freq="1h")


def _u_close(n=N_BARS):
    """Smooth deterministic OLD-basis Close (the 'true underlying')."""
    return pd.Series(80.0 + np.arange(n, dtype=np.float64) * 0.01,
                     index=_hidx(n), name="Close")


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


def _fetch_identical():
    """Stub CONTFUT fetch identical to the old-basis cache (q == 1
    everywhere) — the pre-flip buffer-window signature."""
    return _ohlcv(_u_close().iloc[OVERLAP_START:])


def _feed(frame, lead=None):
    """Fake DataFeedClient: `frame` for every historical fetch; `lead`
    configures get_continuous_lead_local_symbol (None = leave it a bare
    auto-MagicMock, i.e. a client that answers garbage)."""
    feed = MagicMock()
    feed.fetch_historical_bars_by_duration = MagicMock(
        side_effect=lambda **kw: frame.copy()
    )
    if lead is not None:
        feed.get_continuous_lead_local_symbol = MagicMock(return_value=lead)
    return feed


def _base_meta(exec_symbol="CL", front="CLQ6"):
    return {
        "last_front_month": front,
        "updated_at": "2026-07-01T00:00:00",
        "roll_history": [],
        "cumulative_ratio": 1.0,
        "last_front_month_by_symbol": {exec_symbol: front},
    }


def _write_meta(path, meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _read_meta(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_frame_parquet(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.reset_index(drop=True).to_parquet(
        str(path), index=False, engine="pyarrow"
    )


def _make_dm(tmp_path, *, symbol="CL", execution_symbol=None,
             data_client=None, front_month_id="CLQ6"):
    """Real DataManager fully path-isolated to tmp_path."""
    kwargs = dict(
        symbol=symbol,
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


def _seam_dm(tmp_path, feed, *, symbol="CL", execution_symbol=None,
             front="CLQ6"):
    """DataManager wired for a direct resolve_roll_seam() call."""
    exec_sym = execution_symbol or symbol
    _write_meta(tmp_path / ".roll_metadata.json",
                _base_meta(exec_symbol=exec_sym, front=front))
    dm = _make_dm(tmp_path, symbol=symbol, execution_symbol=execution_symbol,
                  data_client=feed, front_month_id=front)
    dm._df = _ohlcv(_u_close())
    return dm


DETECTED = pd.Timestamp("2026-07-15 17:00")


@pytest.fixture
def isolated_backup_dir(tmp_path):
    bdir = tmp_path / "_isolated_cache_backups"
    bdir.mkdir()
    (bdir / "sentinel.parquet").touch()
    with mock.patch.object(dm_module, "_CACHE_BACKUP_DIR", bdir):
        yield bdir


# ===========================================================================
# 1. resolve_roll_seam(): all-matching overlap disambiguated by the lead
# ===========================================================================

class TestSeamLeadDisambiguation:

    def test_all_matching_lead_still_old_returns_retry(self, tmp_path):
        """Pre-flip buffer window: every overlap bar matches AND the
        CONTFUT lead is still the 'from' contract -> RETRY (the seam has
        not appeared in the data yet). Nothing recorded, nothing
        persisted, pending lifecycle untouched."""
        dm = _seam_dm(tmp_path, _feed(_fetch_identical(), lead="CLQ6"))

        outcome = dm.resolve_roll_seam(
            from_contract="CLQ6", to_contract="CLU6", detected_at=DETECTED,
        )

        assert outcome == "RETRY"
        assert dm._roll_ratios == []
        assert dm._roll_timestamps == []
        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        assert meta_after["roll_history"] == []

    def test_all_matching_lead_flipped_escalates(self, tmp_path):
        """Lead already the 'to' contract while every overlap bar matches:
        the cache holds no old-basis anchor -> genuine ESCALATE."""
        dm = _seam_dm(tmp_path, _feed(_fetch_identical(), lead="CLU6"))

        outcome = dm.resolve_roll_seam(
            from_contract="CLQ6", to_contract="CLU6", detected_at=DETECTED,
        )

        assert outcome == "ESCALATE"
        assert dm._roll_ratios == []

    def test_all_matching_lead_query_fails_escalates(self, tmp_path):
        """A failing lead query must NOT be swallowed into RETRY — unknown
        lead keeps the conservative loud path."""
        feed = _feed(_fetch_identical())
        feed.get_continuous_lead_local_symbol = MagicMock(
            side_effect=RuntimeError("gateway hiccup")
        )
        dm = _seam_dm(tmp_path, feed)

        outcome = dm.resolve_roll_seam(
            from_contract="CLQ6", to_contract="CLU6", detected_at=DETECTED,
        )

        assert outcome == "ESCALATE"

    @pytest.mark.parametrize("bogus", [None, "", "   ", 42])
    def test_all_matching_non_string_lead_escalates(self, tmp_path, bogus):
        """Garbage lead answers (None/empty/non-str) are 'unknown' ->
        ESCALATE. This is also what keeps the Strict-Lock Stage-2 pins
        green (a bare MagicMock lead is not a str)."""
        dm = _seam_dm(tmp_path, _feed(_fetch_identical(), lead=bogus))

        outcome = dm.resolve_roll_seam(
            from_contract="CLQ6", to_contract="CLU6", detected_at=DETECTED,
        )

        assert outcome == "ESCALATE"

    def test_all_matching_client_without_capability_escalates(self, tmp_path):
        """A client that does not expose get_continuous_lead_local_symbol
        (spec-restricted mock) cannot vouch for the lead -> ESCALATE."""
        frame = _fetch_identical()
        feed = MagicMock(spec=["fetch_historical_bars_by_duration"])
        feed.fetch_historical_bars_by_duration = MagicMock(
            side_effect=lambda **kw: frame.copy()
        )
        dm = _seam_dm(tmp_path, feed)

        outcome = dm.resolve_roll_seam(
            from_contract="CLQ6", to_contract="CLU6", detected_at=DETECTED,
        )

        assert outcome == "ESCALATE"

    def test_micro_execution_month_code_match_retries(self, tmp_path):
        """Micros pend EXECUTION localSymbols (MGCQ6) while the brain
        CONTFUT lead is the PARENT contract (GCQ6). The pre-flip match
        must be month-code aware, not exact-string: lead GCQ6 vs pending
        from MGCQ6 -> RETRY."""
        dm = _seam_dm(
            tmp_path, _feed(_fetch_identical(), lead="GCQ6"),
            symbol="GC", execution_symbol="MGC", front="MGCQ6",
        )

        outcome = dm.resolve_roll_seam(
            from_contract="MGCQ6", to_contract="MGCV6", detected_at=DETECTED,
        )

        assert outcome == "RETRY"

    def test_micro_execution_flipped_parent_lead_escalates(self, tmp_path):
        """Parent lead already on the 'to' month (GCV6 vs pending
        MGCQ6 -> MGCV6): no pre-flip window -> ESCALATE."""
        dm = _seam_dm(
            tmp_path, _feed(_fetch_identical(), lead="GCV6"),
            symbol="GC", execution_symbol="MGC", front="MGCQ6",
        )

        outcome = dm.resolve_roll_seam(
            from_contract="MGCQ6", to_contract="MGCV6", detected_at=DETECTED,
        )

        assert outcome == "ESCALATE"

    def test_anchorable_seam_still_resolves_without_lead_query(self, tmp_path):
        """A clean old-run -> new-run split must resolve WITHOUT consulting
        the lead (the anchor is in the data; the lead query is only the
        tie-breaker for the ambiguous all-matching signature)."""
        r_true = 0.97
        flip_pos = 168
        mixed = _u_close().copy()
        mixed.iloc[flip_pos:] = mixed.iloc[flip_pos:] * r_true
        fetch = _ohlcv(_u_close().iloc[OVERLAP_START:] * r_true)
        feed = _feed(fetch, lead="CLQ6")  # stale answer must be ignored
        _write_meta(tmp_path / ".roll_metadata.json", _base_meta())
        dm = _make_dm(tmp_path, data_client=feed)
        dm._df = _ohlcv(mixed)

        outcome = dm.resolve_roll_seam(
            from_contract="CLQ6", to_contract="CLU6", detected_at=DETECTED,
        )

        assert outcome == "RESOLVED"
        assert dm._roll_ratios[-1] == pytest.approx(r_true, abs=1e-9)
        feed.get_continuous_lead_local_symbol.assert_not_called()


# ===========================================================================
# 2. initialize(): the startup gate must survive the pre-flip buffer window
# ===========================================================================

def _init_scenario(tmp_path, *, feed, meta, front_month_id):
    frame = _ohlcv(_u_close())
    _write_frame_parquet(tmp_path / "warm_start_cache_1h.parquet", frame)
    _write_frame_parquet(tmp_path / "ledger.parquet", frame)
    _write_meta(tmp_path / ".roll_metadata.json", meta)
    return _make_dm(tmp_path, data_client=feed,
                    front_month_id=front_month_id)


class TestInitializePreFlipWindow:

    def test_pending_preflip_lead_starts_up_and_keeps_pending(
        self, tmp_path, isolated_backup_dir
    ):
        """THE INCIDENT SCENARIO: restart during the buffer window
        (pending roll parked, fetch matches cache everywhere, CONTFUT lead
        still the old contract). initialize() must complete WITHOUT
        raising, record no ratio, and keep the pending record on disk for
        the live hourly retry loop to resolve after the real flip."""
        meta = _base_meta(front="CLQ6")
        meta["pending_roll"] = {
            "CL": {"from": "CLQ6", "to": "CLU6",
                   "detected_at": "2026-07-15T17:02:25.548933"},
        }
        dm = _init_scenario(
            tmp_path, feed=_feed(_fetch_identical(), lead="CLQ6"),
            meta=meta, front_month_id="CLU6",
        )

        df = dm.initialize()  # must NOT raise

        assert len(df) > 0
        assert dm._roll_ratios == [], (
            "no seam exists yet — nothing may be recorded pre-flip"
        )
        meta_after = _read_meta(tmp_path / ".roll_metadata.json")
        assert meta_after["pending_roll"]["CL"]["from"] == "CLQ6", (
            "the pending record must survive startup so the live retry "
            "loop can capture the real flip at LTD"
        )
        assert meta_after["roll_history"] == []

    def test_pending_flipped_lead_still_hard_fails(
        self, tmp_path, isolated_backup_dir
    ):
        """Guard intact: all-matching overlap with the lead ALREADY
        flipped (anchor genuinely lost) must still raise the loud
        RuntimeError at startup."""
        meta = _base_meta(front="CLQ6")
        meta["pending_roll"] = {
            "CL": {"from": "CLQ6", "to": "CLU6",
                   "detected_at": "2026-07-15T17:02:25.548933"},
        }
        dm = _init_scenario(
            tmp_path, feed=_feed(_fetch_identical(), lead="CLU6"),
            meta=meta, front_month_id="CLU6",
        )

        with pytest.raises(RuntimeError) as excinfo:
            dm.initialize()

        msg = str(excinfo.value).lower()
        assert "pending" in msg and "roll" in msg


# ===========================================================================
# 3. Month-code helper
# ===========================================================================

class TestContractMonthCode:

    @pytest.mark.parametrize("local,expected", [
        ("CLQ6", "Q6"),
        ("CLU6", "U6"),
        ("MGCQ6", "Q6"),
        ("SILU6", "U6"),
        ("GCV6", "V6"),
        ("NGF27", "F27"),   # two-digit year form
        ("ESZ6", "Z6"),
    ])
    def test_extracts_month_code(self, local, expected):
        assert dm_module._contract_month_code(local) == expected

    @pytest.mark.parametrize("bogus", ["", "CL", "Q", "6", "CLQ", "CLA6"])
    def test_unparseable_returns_none(self, bogus):
        assert dm_module._contract_month_code(bogus) is None

    def test_same_contract_month_exact_and_cross_prefix(self):
        assert dm_module._same_contract_month("CLQ6", "CLQ6")
        assert dm_module._same_contract_month("GCQ6", "MGCQ6")
        assert not dm_module._same_contract_month("GCV6", "MGCQ6")
        assert not dm_module._same_contract_month("bogus", "MGCQ6")


# ===========================================================================
# 4. IBKRDataFeedClient.get_continuous_lead_local_symbol
# ===========================================================================

class TestIBKRFeedLeadQuery:

    def _client(self, qualified_local_symbol):
        from src.core.instrument_master import INSTRUMENT_REGISTRY
        from src.live_execution.instrument_context import InstrumentContext

        ctx = InstrumentContext(
            execution_symbol="MGC",
            brain_symbol="GC",
            execution_instrument=INSTRUMENT_REGISTRY["MGC"],
            brain_instrument=INSTRUMENT_REGISTRY["GC"],
        )
        with mock.patch(
            "src.live_execution.adapters.ibkr_data_feed.IBKRConnectionManager"
        ) as mgr_cls:
            from src.live_execution.adapters.ibkr_data_feed import (
                IBKRDataFeedClient,
            )
            client = IBKRDataFeedClient(instrument_context=ctx)
        qualified = MagicMock()
        qualified.localSymbol = qualified_local_symbol
        client.manager.qualify_contract = MagicMock(return_value=qualified)
        return client

    def test_returns_fresh_lead_local_symbol_for_brain_symbol(self):
        """Qualifies the BRAIN symbol's continuous contract on every call
        (no caching — the whole question is whether the lead flipped) and
        returns its localSymbol."""
        client = self._client("GCQ6")

        assert client.get_continuous_lead_local_symbol() == "GCQ6"
        assert client.get_continuous_lead_local_symbol() == "GCQ6"

        assert client.manager.qualify_contract.call_count == 2
        contract = client.manager.qualify_contract.call_args[0][0]
        # ContFuture for the brain symbol (GC), not the micro execution
        assert type(contract).__name__ == "ContFuture"
        assert contract.symbol == "GC"

    def test_empty_local_symbol_raises(self):
        """An empty qualification answer must raise loudly — never hand
        the seam scan an empty string it could mis-compare."""
        client = self._client("")

        with pytest.raises(RuntimeError):
            client.get_continuous_lead_local_symbol()

    def test_interface_default_raises_not_implemented(self):
        """The DataFeedClient default must refuse to answer so that a
        provider without the capability can never silently vouch for a
        price-basis decision."""
        from src.live_execution.adapters.simulated_data_feed import (
            SimulatedDataFeed,
        )
        sim = object.__new__(SimulatedDataFeed)  # no ctor deps needed
        with pytest.raises(NotImplementedError):
            sim.get_continuous_lead_local_symbol()
