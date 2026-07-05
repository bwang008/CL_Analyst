"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/core/instrument_master.py
                            (_EQUITY_SESSION = (("17:00","15:15"),
                            ("15:30","16:00")) on EXACTLY ES/MES/NQ/MNQ),
                            src/live_execution/session_calendar.py
                            (third dispatch arm _equity_market_status in
                            America/Chicago + equity session_open_anchor
                            most-recent 15:30 / 17:00 CT open; GLOBEX and
                            GRAINS bodies untouched; unknown shape still
                            raises),
                            src/live_execution/live_trader.py
                            (live_config.enable_5m_stream — optional,
                            DEFAULT TRUE, loudly logged; ValueError when
                            false + 5m bar_size; skip 5m DataManager /
                            warm-start / brain subscription when false
                            while the front-month hands stream STAYS;
                            _check_trailing_stop frame selection off frame
                            PRESENCE (C4 — no flag read); None-guards at
                            the :726/:889/:2407 sites — :889 is FUNCTIONAL
                            (C2: unguarded, the shared try swallows the
                            AttributeError and SKIPS the 1h cache save);
                            watchdog re-point: hourly-only instances anchor
                            _last_bar_time_1h against a new module constant
                            _STALE_BAR_THRESHOLD_MINUTES_1H = 135; the
                            5m-enabled 15-min/_last_bar_time_5m behavior
                            stays byte-identical),
                            configs/strategies/ES01B_Sharpe_E03_07042026.json
                            (ONE field added: live_config.enable_5m_stream
                            = false — Coder patches, T6 surgical precedent)
Target Class/Function: _EQUITY_SESSION, market_status, session_open_anchor,
                       LiveTrader.__init__ / _warm_start / _subscribe /
                       _subscribe_front_month / _shutdown /
                       _check_trailing_stop / _check_stale_bars,
                       _STALE_BAR_THRESHOLD_MINUTES_1H
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: t7-es-ops-runway_07052026_0214
Phase: RED — at HEAD 03218af:
  - ES/MES/NQ/MNQ carry _GLOBEX_SESSION -> every equity-tuple /
    equity-halt / equity-anchor assertion fails (GLOBEX reports OPEN
    inside the real 15:15-15:30 CT halt and anchors None).
  - live_trader has no enable_5m_stream mechanism: the 5m DataManager is
    constructed unconditionally, _subscribe always issues the 5m brain
    subscription, _warm_start unconditionally initializes the 5m manager
    (FileNotFoundError on a missing 5m seed), _check_trailing_stop reads
    rolling_df_5m.iloc[-1] unguarded (AttributeError when None), _shutdown
    calls data_manager_5m.save_cache() inside the SHARED try (a None
    manager's AttributeError silently skips the 1h save — C2), and
    _check_stale_bars watches only _last_bar_time_5m (hourly-only
    instances would be permanently unwatched).
  - The shipped ES01B config does not carry enable_5m_stream (Coder
    patches the config; this file only pins the outcome).
  Pin tests (GLOBEX/grains tuples, GLOBEX anchor None, 5m-preferred
  trailing, 5m-enabled watchdog, unknown-shape raise, T6 sentinels) pass
  at Red BY DESIGN — they are the anti-drift fence.

Governing documents (this file transcribes their pins, it does not invent):
  - blueprint.md (t7-es-ops-runway_07052026_0214) — manager rulings:
    mechanism = live_config.enable_5m_stream optional/default-true/loud;
    calendar lands BEFORE the watchdog re-point (C3); threshold 135
    (120 max normal 1h staleness + 15 legacy margin); CL byte-identity is
    hard constraint 1; trailing asymmetry (CL 5m / ES 1h) is user-ruled.
  - audit.md §1 — equity calendar semantics + strings (only "OPEN" is
    byte-load-bearing; CLOSED strings are shape-pinned startswith CLOSED
    + weekend/halt/maintenance marker).
  - audit_hourly_only.md — consumer map, mechanism, watchdog design, §8
    test list.
  - impact_review.md — C2 (functional :889 guard: assert the 1h save
    HAPPENS), C4 (frame selection with NO flag read — the stubs below
    deliberately omit _enable_5m_stream so any direct flag read fails),
    C7 (15-min pin stays for 5m-enabled instances; the 135 constant is
    pinned here).

All I/O boundaries are mocked or tmp_path-local: MagicMock data/exec
clients, no IBKR gateway, no Telegram (MagicMock), no real seed files —
the hourly-only boot tests pass EXPLICIT tmp 5m seed/cache paths
(guaranteed absent) and a tmp 1h seed via the existing
live_config.seed_path_1h override, so nothing depends on machine state.
Clocks are frozen by injecting utc_now into the pure calendar functions
and by patching live_trader.datetime for the watchdog (T5 conventions,
tests/test_session_watchdog_rollover.py).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import pytz

from src.core.instrument_master import INSTRUMENT_REGISTRY, get_instrument
from src.live_execution import live_trader as lt_module
from src.live_execution.instrument_context import resolve_instrument_context
from src.live_execution.interfaces.execution_interface import (
    StandardExecutionEvent,
)
from src.live_execution.live_trader import LiveTrader
from src.live_execution.session_calendar import (
    market_status,
    session_open_anchor,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "configs" / "strategies"

# ---------------------------------------------------------------------------
# Registry tuple literals (blueprint §Target Files). Asserted as VALUES, not
# ghost-imported names, so the file collects at Red and each pin fails with a
# readable assertion instead of a collection error.
# ---------------------------------------------------------------------------

_EQUITY_TUPLE = (("17:00", "15:15"), ("15:30", "16:00"))
_GLOBEX_TUPLE = (("17:00", "16:00"),)
_GRAINS_TUPLE = (("19:00", "07:45"), ("08:30", "13:20"))

_ES = get_instrument("ES")
_NQ = get_instrument("NQ")
_MES = get_instrument("MES")
_MNQ = get_instrument("MNQ")
_CL = get_instrument("CL")
_MCL = get_instrument("MCL")
_GC = get_instrument("GC")

# ---------------------------------------------------------------------------
# Time helpers (T5 conventions: frozen wall-clock -> tz-naive UTC)
# ---------------------------------------------------------------------------

_CT = pytz.timezone("America/Chicago")
_ET = pytz.timezone("America/New_York")


def _utc_from_ct(y, m, d, hh, mm):
    return _CT.localize(datetime(y, m, d, hh, mm)).astimezone(pytz.utc).replace(
        tzinfo=None
    )


def _utc_from_et(y, m, d, hh, mm):
    return _ET.localize(datetime(y, m, d, hh, mm)).astimezone(pytz.utc).replace(
        tzinfo=None
    )


def _frozen_lt_clock(naive_utc: datetime):
    """Patch live_trader.datetime so datetime.now(timezone.utc) is frozen
    (T5 seam — the module reads `from datetime import datetime, timezone`)."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            if tz is not None:
                return naive_utc.replace(tzinfo=timezone.utc).astimezone(tz)
            return naive_utc

    return patch.object(lt_module, "datetime", _Frozen)


# Same wall-clock instants in a CST week (Jan) and a CDT week (Jul).
# Weekday-verified (T5 _GRAIN_WEEKS convention): Mon 2026-01-05 / 2026-07-06,
# Tue 01-06 / 07-07, Fri 01-09 / 07-10, Sat 01-10 / 07-11; sun_prev is the
# Sunday BEFORE that Monday (01-04 / 07-05), sun_next the following Sunday
# (01-11 / 07-12).
_WEEKS = [
    {"mon": (2026, 1, 5), "tue": (2026, 1, 6), "fri": (2026, 1, 9),
     "sat": (2026, 1, 10), "sun_prev": (2026, 1, 4), "sun_next": (2026, 1, 11)},
    {"mon": (2026, 7, 6), "tue": (2026, 7, 7), "fri": (2026, 7, 10),
     "sat": (2026, 7, 11), "sun_prev": (2026, 7, 5), "sun_next": (2026, 7, 12)},
]


def _assert_closed(status: str, marker: str, ctx: str) -> None:
    """T5 string contract: only "OPEN" is byte-load-bearing (watchdog gate);
    CLOSED strings are shape-pinned — startswith CLOSED + marker."""
    assert status != "OPEN", f"{ctx}: got 'OPEN'"
    assert status.startswith("CLOSED"), f"{ctx}: got {status!r}"
    assert marker in status, f"{ctx}: {status!r} lacks marker {marker!r}"


# ===========================================================================
# 1. TestEquityRegistryShape — the registry tuple change (blueprint: EXACTLY
#    ES/MES/NQ/MNQ; micros move with parents; everything else untouched)
# ===========================================================================

class TestEquityRegistryShape:
    def test_equity_family_carries_equity_tuple(self):
        """ES/MES/NQ/MNQ carry the EQUITY session tuple (audit §1.2)."""
        for sym in ("ES", "MES", "NQ", "MNQ"):
            got = get_instrument(sym).session_hours_ct
            assert got == _EQUITY_TUPLE, (
                f"{sym}: session_hours_ct {got!r} != _EQUITY_SESSION "
                f"{_EQUITY_TUPLE!r}"
            )

    def test_equity_tuple_on_exactly_four_symbols(self):
        """EXACTLY the 4 equity-index entries — no drift onto any other
        symbol (the dispatch is tuple-equality: membership IS behavior)."""
        carriers = {
            sym for sym, inst in INSTRUMENT_REGISTRY.items()
            if inst.session_hours_ct == _EQUITY_TUPLE
        }
        assert carriers == {"ES", "MES", "NQ", "MNQ"}, (
            f"EQUITY tuple carriers must be exactly ES/MES/NQ/MNQ, "
            f"got {sorted(carriers)}"
        )

    def test_micros_equal_parents(self):
        """Micros move WITH parents (pins the
        test_instrument_master_live_fields.py:205 invariant from this side)."""
        assert _MES.session_hours_ct == _ES.session_hours_ct
        assert _MNQ.session_hours_ct == _NQ.session_hours_ct

    def test_globex_and_grains_tuples_unchanged_pin(self):
        """Anti-drift pin (passes at Red AND Green): every non-equity entry
        keeps today's exact tuple — CL/grains byte-identity by construction."""
        for sym in ("CL", "MCL", "GC", "MGC", "SI", "SIL", "NG", "HG", "PA"):
            got = get_instrument(sym).session_hours_ct
            assert got == _GLOBEX_TUPLE, (
                f"{sym}: GLOBEX tuple changed — {got!r}"
            )
        for sym in ("ZC", "ZS"):
            got = get_instrument(sym).session_hours_ct
            assert got == _GRAINS_TUPLE, (
                f"{sym}: GRAINS tuple changed — {got!r}"
            )


# ===========================================================================
# 2. TestEquityCalendar — frozen-clock probes, Jan (CST) + Jul (CDT)
#    (audit §1.3 semantics; impact_review §1.1 sweep table)
# ===========================================================================

class TestEquityCalendar:
    @pytest.mark.parametrize("week", _WEEKS)
    def test_equity_halt_closed_tue_1520(self, week):
        """The REAL daily 15:15-15:30 CT halt — the T5 C4 defect window —
        must read CLOSED with a 'halt' marker."""
        y, m, d = week["tue"]
        _assert_closed(
            market_status(_ES, _utc_from_ct(y, m, d, 15, 20)),
            "halt",
            f"ES Tue 15:20 CT ({y}-{m:02d}-{d:02d}) must be a CLOSED halt",
        )

    @pytest.mark.parametrize("week", _WEEKS)
    def test_maintenance_closed_tue_1630(self, week):
        """Mon-Thu 16:00-17:00 CT maintenance -> CLOSED + 'maintenance'
        marker (the equity calendar's own string — CL's byte-frozen halt
        string is NOT reused)."""
        y, m, d = week["tue"]
        _assert_closed(
            market_status(_ES, _utc_from_ct(y, m, d, 16, 30)),
            "maintenance",
            f"ES Tue 16:30 CT ({y}-{m:02d}-{d:02d}) must be CLOSED "
            "maintenance",
        )

    @pytest.mark.parametrize("week", _WEEKS)
    def test_open_instants_byte_exact(self, week):
        """OPEN pins (byte-exact — the watchdog gate compares == 'OPEN'):
        post-halt reopen 15:35, Sunday reopen 17:05, Friday post-halt
        15:35, plain trading hours."""
        ty, tm, td = week["tue"]
        fy, fm, fd = week["fri"]
        sy, sm, sd = week["sun_next"]
        open_cases = [
            (ty, tm, td, 15, 35),  # Tue post-halt reopen window
            (ty, tm, td, 12, 0),   # Tue plain trading hours
            (fy, fm, fd, 15, 35),  # Fri post-halt (weekend close is 16:00)
            (sy, sm, sd, 17, 5),   # Sunday 17:00 CT open
        ]
        for y, m, d, hh, mm in open_cases:
            got = market_status(_ES, _utc_from_ct(y, m, d, hh, mm))
            assert got == "OPEN", (
                f"ES {y}-{m:02d}-{d:02d} {hh:02d}:{mm:02d} CT must be exactly "
                f"'OPEN', got {got!r}"
            )

    @pytest.mark.parametrize("week", _WEEKS)
    def test_weekend_closed(self, week):
        """Sun pre-open 16:00 CT, Fri 16:30 CT (weekend close — NOT
        maintenance) and Saturday are CLOSED + 'weekend' marker."""
        sy, sm, sd = week["sun_next"]
        fy, fm, fd = week["fri"]
        ay, am, ad = week["sat"]
        _assert_closed(
            market_status(_ES, _utc_from_ct(sy, sm, sd, 16, 0)),
            "weekend", "ES Sun 16:00 CT (pre-open)",
        )
        _assert_closed(
            market_status(_ES, _utc_from_ct(fy, fm, fd, 16, 30)),
            "weekend", "ES Fri 16:30 CT (weekend close at 16:00 CT)",
        )
        _assert_closed(
            market_status(_ES, _utc_from_ct(ay, am, ad, 12, 0)),
            "weekend", "ES Sat 12:00 CT",
        )

    @pytest.mark.parametrize("week", _WEEKS)
    def test_equity_family_dispatches_same_calendar(self, week):
        """ES/MES/NQ/MNQ all dispatch the SAME equity calendar
        (registry-tuple dispatch): halt CLOSED at 15:20, byte-'OPEN' at
        15:35."""
        y, m, d = week["tue"]
        t_halt = _utc_from_ct(y, m, d, 15, 20)
        t_open = _utc_from_ct(y, m, d, 15, 35)
        for inst in (_ES, _MES, _NQ, _MNQ):
            _assert_closed(
                market_status(inst, t_halt), "halt",
                f"{inst.symbol} Tue 15:20 CT must be the equity CLOSED halt",
            )
            assert market_status(inst, t_open) == "OPEN", (
                f"{inst.symbol} Tue 15:35 CT must be exactly 'OPEN'"
            )

    def test_unknown_session_shape_still_raises(self):
        """Pin (no silent default, passes at Red AND Green): a shape that is
        neither GLOBEX nor GRAINS nor EQUITY raises ValueError naming the
        symbol."""
        fake = dataclasses.replace(_ES, session_hours_ct=(("09:00", "10:00"),))
        with pytest.raises(ValueError) as excinfo:
            market_status(fake, _utc_from_ct(2026, 7, 7, 10, 0))
        assert "ES" in str(excinfo.value)
        with pytest.raises(ValueError):
            session_open_anchor(fake, _utc_from_ct(2026, 7, 7, 10, 0))


# ===========================================================================
# 3. TestEquityAnchor — session_open_anchor equity vectors (audit §1.3:
#    opens are Mon-Fri 15:30 CT and Sun-Thu 17:00 CT; most recent, tz-naive
#    UTC) + the GLOBEX None pin (CL watchdog arithmetic bit-identical)
# ===========================================================================

class TestEquityAnchor:
    @pytest.mark.parametrize("week", _WEEKS)
    def test_equity_anchor_vectors(self, week):
        mon, tue, fri = week["mon"], week["tue"], week["fri"]
        sat, sun_prev, sun_next = (
            week["sat"], week["sun_prev"], week["sun_next"],
        )
        cases = [
            # (query instant CT, expected most-recent-open CT)
            (tue + (12, 0), mon + (17, 0)),    # mid-day -> Mon 17:00 reopen
            (tue + (15, 35), tue + (15, 30)),  # post-halt -> 15:30 reopen
            (tue + (16, 30), tue + (15, 30)),  # maintenance -> 15:30
            (tue + (17, 5), tue + (17, 0)),    # post-maintenance -> 17:00
            (mon + (10, 0), sun_prev + (17, 0)),   # Monday morning -> Sun open
            (sun_next + (17, 30), sun_next + (17, 0)),  # Sunday evening
            (sat + (12, 0), fri + (15, 30)),   # Sat: Fri 17:00 is NOT an open
            (fri + (16, 30), fri + (15, 30)),  # Fri close: same — no Fri 17:00
        ]
        for query, expected in cases:
            q = _utc_from_ct(*query)
            want = _utc_from_ct(*expected)
            anchor = session_open_anchor(_ES, q)
            assert anchor is not None, (
                f"ES @ CT{query}: equity anchor must not be None"
            )
            assert anchor == want, (
                f"ES @ CT{query}: anchor {anchor!r} != expected {want!r} "
                "(tz-naive UTC of the most recent Mon-Fri 15:30 / "
                "Sun-Thu 17:00 CT open)"
            )
            assert getattr(anchor, "tzinfo", None) is None

    def test_micros_anchor_equals_parent(self):
        t = _utc_from_ct(2026, 7, 7, 16, 30)
        assert session_open_anchor(_MES, t) == session_open_anchor(_ES, t)
        assert session_open_anchor(_MNQ, t) == session_open_anchor(_NQ, t)

    def test_globex_anchor_still_none_pin(self):
        """Pin (passes at Red AND Green): GLOBEX anchor stays None — the CL
        watchdog arithmetic stays bit-identical (Q1 reopen false-positive
        pinned as-is). ES is deliberately ABSENT here (it moved to the
        equity calendar — the sanctioned T5 evolution)."""
        instants = [
            _utc_from_et(2026, 7, 13, 12, 0),   # open hours
            _utc_from_et(2026, 7, 13, 17, 30),  # daily halt
            _utc_from_et(2026, 7, 12, 18, 5),   # Sunday open
        ]
        for inst in (_CL, _MCL, _GC):
            for t in instants:
                assert session_open_anchor(inst, t) is None, (
                    f"{inst.symbol} @ {t}: GLOBEX anchor must stay None"
                )


# ===========================================================================
# Construction seams
# ===========================================================================

class DummyStrategy:
    """Minimal strategy stub (mirrors tests/test_instrument_context.py)."""

    def __init__(self, config: dict):
        self.feature_names = ["MACD"]
        self.name = "DummyStrategy"
        self.direction = "LONG"
        self.config = config


def _build_trader_with_mock_dm(cfg: dict, tmp_path: Path):
    """T5 construction seam: mocked DataManager class + Path.exists so no
    real seed is consulted (tests/test_session_watchdog_rollover.py)."""
    with patch("src.live_execution.live_trader.DataManager") as MockDM, patch(
        "pathlib.Path.exists", return_value=True
    ):
        trader = LiveTrader(
            data_client=MagicMock(),
            exec_client=MagicMock(),
            strategy=DummyStrategy(config=cfg),
            db_path=str(tmp_path / "telemetry.db"),
            dry_run=True,
        )
    trader._telegram = MagicMock()
    return trader, MockDM


def _write_hourly_seed(path: Path, n_bars: int = 60) -> pd.DatetimeIndex:
    idx = pd.date_range("2026-06-01", periods=n_bars, freq="1H")
    pd.DataFrame({
        "DateTime": idx,
        "Open": 7500.0, "High": 7510.0, "Low": 7490.0, "Close": 7500.0,
        "Volume": 10.0,
    }).to_parquet(path, engine="pyarrow")
    return idx


def _es_hourly_only_cfg(tmp_path: Path) -> dict:
    """ES-style 1h config opting out of the 5m stream. The 1h seed lives in
    tmp_path via the EXISTING live_config.seed_path_1h override
    (live_trader.py:392-400 at HEAD) — no real data files anywhere."""
    seed_1h = tmp_path / "ES_raw_1h_seed.parquet"
    if not seed_1h.exists():
        _write_hourly_seed(seed_1h)
    return {
        "nickname": "ES_hourly_only_style",
        "execution_symbol": "ES",
        "bar_size": "1h",
        "live_config": {
            "enable_5m_stream": False,
            "seed_path_1h": str(seed_1h),
        },
    }


def _build_hourly_only_trader(tmp_path: Path) -> LiveTrader:
    """REAL LiveTrader construction (unmocked DataManager) with every disk
    dependency in tmp_path: the 5m seed/cache paths are passed EXPLICITLY
    and are guaranteed ABSENT — no 5m seed exists anywhere."""
    cfg = _es_hourly_only_cfg(tmp_path)
    trader = LiveTrader(
        data_client=MagicMock(),
        exec_client=MagicMock(),
        strategy=DummyStrategy(config=cfg),
        db_path=str(tmp_path / "telemetry.db"),
        seed_path=str(tmp_path / "es-5m_bk.csv"),                # ABSENT
        cache_path=str(tmp_path / "warm_start_cache_ES.parquet"),  # ABSENT
        dry_run=True,
    )
    trader._telegram = MagicMock()
    return trader


def _mock_1h_manager(tmp_path: Path, n_bars: int = 4400) -> MagicMock:
    """1h DataManager stub for _warm_start (T5 _warm_start_stub pattern):
    initialize() returns an hourly frame satisfying the 4320 floor."""
    idx = pd.date_range("2026-01-01", periods=n_bars, freq="1H")
    df = pd.DataFrame({"Close": 7500.0}, index=idx)
    dm = MagicMock()
    dm.initialize.return_value = df
    dm.cache_path = tmp_path / "warm_start_cache_ES_1h.parquet"
    return dm


# ===========================================================================
# 4. TestEnable5mStreamFlag — the opt-in mechanism (manager ruling: optional,
#    DEFAULT TRUE, loudly logged; false + 5m bar_size -> ValueError)
# ===========================================================================

class TestEnable5mStreamFlag:
    def test_default_true_cl_construction_byte_identical(self, tmp_path):
        """A CL config WITHOUT the flag resolves to True and constructs
        exactly as at HEAD (reuses the T5 construction pins: two managers,
        legacy 288/24 literals, 5m first)."""
        cfg = {
            "nickname": "HS14B_style",
            "execution_symbol": "CL",
            "bar_size": "1h",
        }
        trader, MockDM = _build_trader_with_mock_dm(cfg, tmp_path)
        assert trader._enable_5m_stream is True, (
            "enable_5m_stream must DEFAULT to True (CL byte-identity — "
            "hard constraint 1)"
        )
        assert trader.data_manager_5m is not None
        assert MockDM.call_count == 2, (
            "default-true CL construction must build BOTH managers "
            f"(got {MockDM.call_count} DataManager call(s))"
        )
        first, second = MockDM.call_args_list
        assert first.kwargs.get("bar_size") == "5 mins"
        assert first.kwargs.get("bars_per_day") == 288
        assert first.kwargs.get("symbol") == "CL"
        assert second.kwargs.get("bar_size") == "1 hour"
        assert second.kwargs.get("bars_per_day") == 24

    def test_live_config_present_key_absent_defaults_true(self, tmp_path):
        """A live_config WITHOUT the key (today's HS14B shape) also
        defaults True — zero config edits for the CL fleet."""
        cfg = {
            "nickname": "HS14B_style",
            "execution_symbol": "CL",
            "bar_size": "1h",
            "live_config": {"client_id": 1400},
        }
        trader, MockDM = _build_trader_with_mock_dm(cfg, tmp_path)
        assert trader._enable_5m_stream is True
        assert MockDM.call_count == 2

    def test_flag_false_with_5m_bar_size_raises_value_error(self, tmp_path):
        """enable_5m_stream=false is ILLEGAL for a 5m config (the 5m stream
        IS the inference stream) — ValueError at __init__, before any
        network side-effect."""
        cfg = {
            "nickname": "Bad5mHourlyOff",
            "execution_symbol": "CL",
            "bar_size": "5m",
            "live_config": {"enable_5m_stream": False},
        }
        with patch("src.live_execution.live_trader.DataManager"), patch(
            "pathlib.Path.exists", return_value=True
        ):
            with pytest.raises(ValueError, match="enable_5m_stream"):
                LiveTrader(
                    data_client=MagicMock(),
                    exec_client=MagicMock(),
                    strategy=DummyStrategy(config=cfg),
                    db_path=str(tmp_path / "telemetry.db"),
                    dry_run=True,
                )

    def test_flag_false_constructs_only_1h_manager(self, tmp_path):
        """Hourly-only: exactly ONE DataManager (the 1h one) — no 5m
        manager construction at all."""
        cfg = _es_hourly_only_cfg(tmp_path)
        trader, MockDM = _build_trader_with_mock_dm(cfg, tmp_path)
        assert trader._enable_5m_stream is False
        assert trader.data_manager_5m is None
        assert MockDM.call_count == 1, (
            "hourly-only construction must build ONLY the 1h manager "
            f"(got {MockDM.call_count} DataManager call(s))"
        )
        only = MockDM.call_args_list[0]
        assert only.kwargs.get("bar_size") == "1 hour"
        assert only.kwargs.get("bars_per_day") == 23  # ES registry value
        assert only.kwargs.get("symbol") == "ES"

    def test_flag_false_logs_loud_hourly_only_banner(self, tmp_path, caplog):
        """No silent forks: the resolved mode is declared LOUDLY at startup
        (shape pin — audit_hourly_only §8 test 10: the banner contains
        'HOURLY-ONLY' and 'enable_5m_stream')."""
        cfg = _es_hourly_only_cfg(tmp_path)
        with caplog.at_level(logging.INFO):
            _build_trader_with_mock_dm(cfg, tmp_path)
        messages = [r.getMessage() for r in caplog.records]
        assert any("HOURLY-ONLY" in m for m in messages), (
            "startup must emit a loud HOURLY-ONLY banner; got no record "
            "containing 'HOURLY-ONLY'"
        )
        assert any("enable_5m_stream" in m for m in messages), (
            "the banner must name the flag (enable_5m_stream)"
        )


# ===========================================================================
# 5. TestHourlyOnlyBoot — ES-style config, enable_5m_stream=false, REAL
#    construction, NO 5m seed exists anywhere (audit_hourly_only §8 test 1)
# ===========================================================================

class TestHourlyOnlyBoot:
    def test_constructs_without_5m_seed_and_5m_manager_is_none(self, tmp_path):
        """__init__ completes (no FileNotFoundError) with zero 5m artifacts
        on disk; the 5m manager is the design's None sentinel
        (audit_hourly_only §7 item 1)."""
        trader = _build_hourly_only_trader(tmp_path)
        assert trader.data_manager_5m is None, (
            "hourly-only mode must not construct a 5m DataManager "
            f"(got {type(trader.data_manager_5m).__name__})"
        )
        assert trader.data_manager_1h is not None
        assert trader._enable_5m_stream is False
        assert not (tmp_path / "es-5m_bk.csv").exists()  # never created

    def test_warm_start_skips_5m_and_needs_no_5m_seed(self, tmp_path):
        """_warm_start succeeds with NO 5m seed/cache anywhere: the 5m block
        is skipped (rolling_df_5m / _last_bar_time_5m stay None) and the 1h
        window initializes. At Red this raises the No-Silent-Bootstrap
        FileNotFoundError from the (absent) tmp 5m seed."""
        trader = _build_hourly_only_trader(tmp_path)
        trader.data_manager_1h = _mock_1h_manager(tmp_path)

        trader._warm_start()  # must NOT raise FileNotFoundError

        assert trader.rolling_df_5m is None
        assert trader._last_bar_time_5m is None
        assert trader.rolling_df_1h is not None
        assert len(trader.rolling_df_1h) == 4400
        assert trader._last_bar_time_1h == trader.rolling_df_1h.index[-1]
        trader.data_manager_1h.initialize.assert_called_once()

    def test_subscribe_no_5m_brain_stream_1h_and_hands_still_subscribed(
        self, tmp_path
    ):
        """_subscribe issues NO continuous 5m (brain) subscription, but the
        1h brain subscription AND the front-month hands subscription (order-
        pricing-critical: _front_month_last_close -> marketable-limit entry
        and time-barrier exits) both still happen."""
        trader = _build_hourly_only_trader(tmp_path)

        trader._subscribe()
        trader._subscribe_front_month()

        calls = trader.data_client.subscribe_live_bars.call_args_list
        brain_5m = [
            c for c in calls
            if c.kwargs.get("bar_size") == "5 mins"
            and c.kwargs.get("continuous") is True
        ]
        assert brain_5m == [], (
            "hourly-only mode must NOT subscribe the continuous 5m brain "
            f"stream, got {brain_5m}"
        )
        assert trader._live_bars_5m is None

        brain_1h = [c for c in calls if c.kwargs.get("bar_size") == "1 hour"]
        assert len(brain_1h) == 1
        assert brain_1h[0].kwargs.get("symbol") == "ES"
        assert brain_1h[0].kwargs.get("continuous") is True

        hands = [
            c for c in calls
            if c.kwargs.get("continuous") is False
            and c.kwargs.get("bar_size") == "5 mins"
        ]
        assert len(hands) == 1, (
            "the front-month hands stream MUST stay subscribed in "
            "hourly-only mode (blueprint: order-pricing-critical)"
        )
        assert hands[0].kwargs.get("symbol") == "ES"

    def test_shutdown_path_safe_and_saves_1h_cache(self, tmp_path):
        """_shutdown on a booted hourly-only instance completes without
        AttributeError and still persists the 1h cache."""
        trader = _build_hourly_only_trader(tmp_path)
        trader.data_manager_1h = MagicMock()

        trader._shutdown()  # must not raise

        assert trader.data_manager_5m is None
        trader.data_manager_1h.save_cache.assert_called_once()


# ===========================================================================
# 6. TestHourlyOnlyCacheSaveGuard — C2 is FUNCTIONAL, not cosmetic: at HEAD
#    the :889 data_manager_5m.save_cache() sits in the SHARED try, so a None
#    manager's AttributeError is swallowed and SKIPS the 1h save.
# ===========================================================================

def _shutdown_stub() -> LiveTrader:
    """__new__ stub in the hourly-only end state (data_manager_5m=None) with
    every attribute _shutdown touches."""
    trader = LiveTrader.__new__(LiveTrader)
    trader._live_bars_5m = None
    trader._live_bars_1h = None
    trader._front_month_bars = None
    trader.data_manager_5m = None          # hourly-only mode
    trader.data_manager_1h = MagicMock()
    trader._heartbeat_stop_event = threading.Event()
    trader.data_client = MagicMock()
    trader.exec_client = MagicMock()
    trader.telemetry = MagicMock()
    trader._log_capture = logging.NullHandler()
    return trader


class TestHourlyOnlyCacheSaveGuard:
    def test_1h_cache_save_executes_despite_none_5m_manager(self, caplog):
        """impact_review C2: with data_manager_5m=None the 1h save_cache()
        must STILL execute. At Red the unguarded None.save_cache() raises
        inside the shared try/except (:888-893), the except swallows it,
        and the 1h save is silently skipped — this test fails then for
        exactly that reason."""
        trader = _shutdown_stub()
        with caplog.at_level(logging.INFO):
            trader._shutdown()  # must not raise

        trader.data_manager_1h.save_cache.assert_called_once()
        assert not any(
            "Failed to save warm-start cache" in r.getMessage()
            for r in caplog.records
        ), (
            "the shared-except warning fired — the None 5m manager's "
            "AttributeError was swallowed and the 1h save was skipped (C2)"
        )


# ===========================================================================
# 7. TestTrailingFrameSelection — C4: extremes frame selected ONCE off frame
#    PRESENCE (rolling_df_5m if present else rolling_df_1h). NO flag read:
#    every stub below deliberately OMITS _enable_5m_stream, so an
#    implementation reading self._enable_5m_stream inside the method breaks
#    here AND in the Strict-Lock trailing suites (test_modify_order_transmit,
#    test_tick_order_pricing) whose stubs also lack the attribute.
# ===========================================================================

def _bar_df(high: float, low: float = 69.8) -> pd.DataFrame:
    return pd.DataFrame(
        [{"Open": 70.2, "High": high, "Low": low, "Close": 70.4}]
    )


def _make_inposition_trader(symbol: str, *, df_5m, df_1h):
    """S6 __new__ seam (mirrors tests/test_modify_order_transmit.py
    _make_trailing_trader): long, entry 70.00, ATR 1.0, trigger mult 1.0
    (bar High >= 71.00 activates), offset 0.5 -> new SL 70.50 (on both the
    CL 0.01 and ES 0.25 tick grids). Returns (trader, raw_order, evt)."""
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = symbol
    trader._trailing_activated = False
    trader._entry_price = 70.0
    trader._atr_at_entry = 1.0
    trader._trade_trailing_atr_mult = None
    trader._trailing_atr_mult = 1.0
    trader._trailing_sl_atr_offset_long = 0.5
    trader._trailing_sl_atr_offset_short = 0.5
    trader._position_side = 1
    trader._highest_high = 70.0
    trader._lowest_low = 70.0
    trader._position_bars_held = 3
    trader._active_trade_id = "trade_t7_es_ops"
    trader._tracked_sl_price = 68.50
    trader.telemetry = MagicMock()
    trader._utc_iso_now = MagicMock(return_value="2026-07-05T02:14:00Z")
    trader.rolling_df_5m = df_5m
    trader.rolling_df_1h = df_1h
    trader._sl_order_id = 888
    raw_order = SimpleNamespace(orderId=888, auxPrice=68.50)
    evt = StandardExecutionEvent(
        order_id="888",
        symbol=symbol,
        status="Submitted",
        filled_qty=0,
        remaining_qty=1,
        avg_price=0.0,
        raw_event=SimpleNamespace(
            order=raw_order, contract=SimpleNamespace(symbol=symbol)
        ),
    )
    trader._open_orders = {"888": evt}
    trader.exec_client = MagicMock()
    return trader, raw_order, evt


class TestTrailingFrameSelection:
    def test_hourly_only_reads_1h_frame_and_modifies_sl(self):
        """In-position ES hourly-only instance (rolling_df_5m is None): the
        extremes come from the 1h frame — High 71.5 crosses the trigger,
        activation transmits via modify_order (S6 seam). At Red this is the
        :1100 hard blocker: None.iloc -> AttributeError."""
        trader, raw_order, evt = _make_inposition_trader(
            "ES", df_5m=None, df_1h=_bar_df(71.5)
        )
        trader._check_trailing_stop()

        trader.exec_client.modify_order.assert_called_once_with(
            evt.order_id, evt
        )
        assert trader._trailing_activated is True
        assert trader._tracked_sl_price == 70.5
        assert raw_order.auxPrice == 70.5
        assert trader._highest_high == 71.5  # sourced from the 1h bar

    def test_hourly_only_1h_extremes_tracked_without_trigger(self):
        """Below-trigger 1h bar: no activation, no transmit — but the
        monotonic extremes accumulate FROM THE 1H FRAME (proves the frame
        source, not just the trigger path)."""
        trader, _, _ = _make_inposition_trader(
            "ES", df_5m=None, df_1h=_bar_df(70.5, low=69.4)
        )
        trader._check_trailing_stop()

        trader.exec_client.modify_order.assert_not_called()
        assert trader._trailing_activated is False
        assert trader._highest_high == 70.5
        assert trader._lowest_low == 69.4

    def test_5m_frame_still_drives_when_present_pin(self):
        """CL-style pin (passes at Red AND Green): with BOTH frames present
        and DIVERGENT highs, the 5m frame drives — 5m High 71.5 triggers
        although the 1h High (69.9) would not. Keeps the parity harness's
        populated 5m mirror byte-identical (C4)."""
        trader, _, evt = _make_inposition_trader(
            "CL", df_5m=_bar_df(71.5), df_1h=_bar_df(69.9, low=69.0)
        )
        trader._check_trailing_stop()

        trader.exec_client.modify_order.assert_called_once_with(
            evt.order_id, evt
        )
        assert trader._trailing_activated is True
        assert trader._highest_high == 71.5   # 5m High, not the 1h 69.9
        assert trader._lowest_low == 69.8     # 5m Low, not the 1h 69.0

    def test_5m_preference_negative_control_pin(self):
        """Anti-fork pin (passes at Red AND Green): 5m present but below
        trigger while the 1h frame WOULD trigger (High 75.0) -> NOT
        activated. An implementation that consults the 1h frame while the
        5m frame exists fails here."""
        trader, _, _ = _make_inposition_trader(
            "CL", df_5m=_bar_df(70.3), df_1h=_bar_df(75.0)
        )
        trader._check_trailing_stop()

        trader.exec_client.modify_order.assert_not_called()
        assert trader._trailing_activated is False
        assert trader._highest_high == 70.3


# ===========================================================================
# 8. TestHourlyOnlyWatchdog — _check_stale_bars re-point: hourly-only
#    instances anchor _last_bar_time_1h against 135 min (120 max normal
#    1h-bar staleness + 15 legacy margin); 5m-enabled instances keep the
#    15-min / _last_bar_time_5m behavior byte-identical (C7).
# ===========================================================================

def _watchdog_stub(symbol: str, *, enable_5m: bool):
    """T5 watchdog stub + the explicit mode flag. NOTE: the T5 Strict-Lock
    stubs do NOT set _enable_5m_stream, so the implementation must tolerate
    its absence (default True); here it is always set explicitly."""
    trader = LiveTrader.__new__(LiveTrader)
    trader.data_client = MagicMock()
    trader.exec_client = MagicMock()
    trader._telegram = MagicMock()
    trader._subscriptions_lost = False
    trader._execution_symbol = symbol
    trader._enable_5m_stream = enable_5m
    trader._last_bar_time_5m = None
    trader._last_bar_time_1h = None
    return trader


class TestHourlyOnlyWatchdog:
    def test_stale_threshold_1h_constant_135(self):
        """The new module constant (C7 pairing): 135 = 120 max normal
        oscillation (open-time-stamped 1h bars land at T+60) + the legacy
        15-min margin."""
        assert getattr(lt_module, "_STALE_BAR_THRESHOLD_MINUTES_1H", None) == 135

    @pytest.mark.parametrize("week", _WEEKS)
    def test_hourly_only_stale_true_at_140_min(self, week):
        """ES hourly-only, market OPEN (Tue 12:00 CT), last 1h bar 140 min
        old -> stale (140 >= 135): disconnect + reconnect signalled."""
        y, m, d = week["tue"]
        now = _utc_from_ct(y, m, d, 12, 0)
        trader = _watchdog_stub("ES", enable_5m=False)
        trader._last_bar_time_1h = pd.Timestamp(now - timedelta(minutes=140))
        with _frozen_lt_clock(now):
            result = trader._check_stale_bars()
        assert result is True, (
            "hourly-only watchdog must anchor _last_bar_time_1h — 140 min "
            "stale during OPEN hours must trigger"
        )
        trader.data_client.disconnect.assert_called_once()
        trader.exec_client.disconnect.assert_called_once()
        assert trader._subscriptions_lost is True

    @pytest.mark.parametrize("week", _WEEKS)
    def test_hourly_only_not_stale_at_120_min(self, week):
        """120 min is NORMAL 1h-bar staleness (bar T arrives at T+60) —
        below the 135 threshold -> False. This is the threshold
        discriminator: re-pointing to _last_bar_time_1h while keeping the
        15-min constant fails here."""
        y, m, d = week["tue"]
        now = _utc_from_ct(y, m, d, 12, 0)
        trader = _watchdog_stub("ES", enable_5m=False)
        trader._last_bar_time_1h = pd.Timestamp(now - timedelta(minutes=120))
        with _frozen_lt_clock(now):
            result = trader._check_stale_bars()
        assert result is False
        trader._telegram.send.assert_not_called()
        trader.data_client.disconnect.assert_not_called()

    def test_hourly_only_market_closed_equity_halt_false(self):
        """During the equity halt (Tue 15:20 CT -> CLOSED once the calendar
        lands) staleness is irrelevant -> False. Integration of the equity
        calendar with the re-pointed watchdog (C3 sequencing)."""
        now = _utc_from_ct(2026, 7, 7, 15, 20)
        trader = _watchdog_stub("ES", enable_5m=False)
        trader._last_bar_time_1h = pd.Timestamp(now - timedelta(minutes=300))
        with _frozen_lt_clock(now):
            result = trader._check_stale_bars()
        assert result is False
        trader._telegram.send.assert_not_called()
        trader.data_client.disconnect.assert_not_called()

    def test_hourly_only_no_1h_bar_yet_false(self):
        """Warm start in progress (_last_bar_time_1h None) -> False (the
        legacy None guard transfers to the 1h anchor)."""
        trader = _watchdog_stub("ES", enable_5m=False)
        with _frozen_lt_clock(_utc_from_ct(2026, 7, 7, 12, 0)):
            assert trader._check_stale_bars() is False

    def test_5m_enabled_16min_stale_true_pin(self):
        """Pin (passes at Red AND Green): a 5m-enabled CL instance keeps
        the byte-identical 15-min / _last_bar_time_5m behavior (C7: the
        15-min pin is CLARIFIED to 5m-enabled instances, not flipped)."""
        now = _utc_from_et(2026, 7, 13, 12, 0)
        trader = _watchdog_stub("CL", enable_5m=True)
        trader._last_bar_time_5m = pd.Timestamp(now - timedelta(minutes=16))
        with _frozen_lt_clock(now):
            assert trader._check_stale_bars() is True

    def test_5m_enabled_10min_stale_false_pin(self):
        now = _utc_from_et(2026, 7, 13, 12, 0)
        trader = _watchdog_stub("CL", enable_5m=True)
        trader._last_bar_time_5m = pd.Timestamp(now - timedelta(minutes=10))
        with _frozen_lt_clock(now):
            assert trader._check_stale_bars() is False
        trader._telegram.send.assert_not_called()

    def test_5m_enabled_ignores_stale_1h_anchor_pin(self):
        """Pin: a 5m-enabled instance watches ONLY the 5m anchor — a
        300-min-old _last_bar_time_1h with a fresh 5m bar must NOT trigger
        (the pre-existing 1h-stream gap is a deferred micro-ticket, C6)."""
        now = _utc_from_et(2026, 7, 13, 12, 0)
        trader = _watchdog_stub("CL", enable_5m=True)
        trader._last_bar_time_5m = pd.Timestamp(now - timedelta(minutes=5))
        trader._last_bar_time_1h = pd.Timestamp(now - timedelta(minutes=300))
        with _frozen_lt_clock(now):
            assert trader._check_stale_bars() is False


# ===========================================================================
# 9. TestES01BFlagPatch — the shipped config carries the ONE new field
#    (Coder patches configs/strategies/ES01B_Sharpe_E03_07042026.json);
#    the T6 sentinel fields stay untouched.
# ===========================================================================

_ES01B_NAME = "ES01B_Sharpe_E03_07042026.json"


def _load_es01b() -> dict:
    return json.loads(
        (_CONFIG_DIR / _ES01B_NAME).read_text(encoding="utf-8")
    )


class TestES01BFlagPatch:
    def test_es01b_no_longer_carries_enable_5m_stream_key(self):
        """EVOLVED + RENAMED (ticket seedless-5m-live-stream_07052026_0546,
        impact_review C4 test-name honesty): the user-decided seedless-5m
        design REMOVES the key — ES01B rides the default-true 5m path
        (shallow IBKR bootstrap) and 'flag present <=> deviation from
        default' is restored. The absent-key pin is the anti-drift fence
        against silently re-adding false. FAILS at Red until the Coder
        removes the key in the SAME commit as the code (C1)."""
        cfg = _load_es01b()
        live = cfg.get("live_config", {})
        assert "enable_5m_stream" not in live, (
            "ES01B must NOT carry the live_config.enable_5m_stream key — "
            "removed by ticket seedless-5m-live-stream_07052026_0546 "
            f"(got enable_5m_stream={live.get('enable_5m_stream')!r})"
        )

    def test_es01b_still_resolves_es_es_cme(self):
        """EVOLVED (ticket seedless-5m-live-stream_07052026_0546,
        sanctioned 336d29f repair): commit 336d29f flipped ES01B to
        execution MES / client_id 2000 without evolving this pin — broken
        at HEAD f165b9d. Repaired to the CURRENT shipped values:
        execution MES / brain ES / exchange CME / tick 0.25."""
        cfg = _load_es01b()
        ctx = resolve_instrument_context(cfg)
        assert ctx.execution_symbol == "MES"
        assert ctx.brain_symbol == "ES"
        assert ctx.execution_instrument.exchange == "CME"
        assert ctx.execution_instrument.tick_size == 0.25

    def test_es01b_t6_sentinel_fields_unchanged(self):
        """The T6 sentinel pins (test_config_generator_symbols.py
        TestES01BPatchedConfig enumerated fields) survive the config
        patches — guards the Coder's patch surface.
        EVOLVED (ticket seedless-5m-live-stream_07052026_0546, sanctioned
        336d29f repair): execution_symbol ES->MES and client_id 1010->2000
        pin the CURRENT shipped values (336d29f flipped the config without
        evolving these pins — broken at HEAD f165b9d). All other
        sentinels unchanged."""
        cfg = _load_es01b()
        assert cfg["nickname"] == "ES01B_Sharpe_E03_07042026"
        assert cfg["execution_symbol"] == "MES"
        assert cfg["bar_size"] == "1h"
        assert "brain_symbol" not in cfg
        assert cfg["live_config"]["client_id"] == 2000
        assert cfg["live_config"]["entry_mode"] == "marketable_limit"
        assert cfg["live_config"]["exit_mode"] == "marketable_limit"
        for side in ("long", "short"):
            entry = cfg["models"][side]
            assert entry["symbol"] == "ES"
            assert entry["experiment_id"] == (
                f"E2E_HourSet_01B_{side}_logloss"
            )
        assert cfg["models"]["long"]["threshold"] == 0.53
        assert cfg["models"]["short"]["threshold"] == 0.56
        assert cfg["holdout_months"] == 6
        assert cfg["conflict_resolution"] == "reverse_position"
