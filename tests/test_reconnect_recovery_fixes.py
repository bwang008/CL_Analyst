"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
                            (R1: _backfill_reconnect_gap_async gap reference
                             must be tz-naive UTC, not local wall clock;
                             R2: _reconnect must set _resubscribe_pending=True
                             BEFORE data_client.connect(), preserve the
                             existing clear-after-resubscribe semantics, and
                             not leak the guard True after the attempt loop
                             gives up;
                             R4: module constant _MAX_FRUITLESS_RECONNECTS=3,
                             _check_stale_bars escalates with CRITICAL log +
                             Telegram attempt + SystemExit(non-zero) on the
                             3rd consecutive fruitless firing, counter reset
                             to 0 by any new brain-stream bar via the real
                             _on_bar_update_5m / _on_bar_update_1h handlers),
                            src/live_execution/ibkr_client.py
                            (R3: IBKRConnectionManager.subscribe_live_bars and
                             subscribe_live_bars_async — empty BarDataList =>
                             exactly ONE retry with fallback duration >= "1 D"
                             and keepUpToDate still True; empty again => RAISE
                             a descriptive exception naming the stream;
                             non-empty first snapshot => byte-identical single
                             request)
Target Class/Function: LiveTrader._backfill_reconnect_gap_async,
                       LiveTrader._reconnect, LiveTrader._on_ib_error,
                       LiveTrader._deferred_resubscribe,
                       LiveTrader._check_stale_bars,
                       LiveTrader._on_bar_update_5m,
                       LiveTrader._on_bar_update_1h,
                       IBKRConnectionManager.subscribe_live_bars,
                       IBKRConnectionManager.subscribe_live_bars_async,
                       live_trader._MAX_FRUITLESS_RECONNECTS
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: reconnect-recovery-fixes_07052026_2022
Phase: RED — every requirement R1-R4 has at least one test that FAILS on
current HEAD. Tests explicitly marked "FENCE" in their docstrings pass on
current code by design: they pin behavior the blueprint requires the fix to
PRESERVE (they turn red only if the Coder regresses it).

Conventions (mirroring tests/test_reconnection.py and
tests/test_session_watchdog_rollover.py):
  - LiveTrader stubs via object.__new__ with only the seams each method reads.
  - IBKRConnectionManager stub via object.__new__ with ib=MagicMock() and an
    instance-level ensure_connected=MagicMock() (no gateway, no network).
  - Clocks frozen by patching lt_module.datetime with a datetime subclass and
    lt_module.pd with a delegating proxy whose Timestamp stand-in controls
    now()/utcnow() — this fabricates a UTC-7 local/UTC divergence so the R1
    tests fail on the current local-clock implementation REGARDLESS of the
    machine's timezone (no freezegun in this repo).
  - Async methods driven with asyncio.run + AsyncMock (see
    tests/test_symbol_data_paths.py:800-838).
  - pandas 1.5.3 compatible (no pandas 2.x APIs).

DELIBERATE SEAM OMISSIONS (contract for the Coder):
  - The R4 stubs do NOT pre-set any fruitless-counter attribute. The counter
    must be stub-safe on object.__new__ instances, exactly like the existing
    _enable_5m_stream / _last_bar_time_5m getattr seams inside
    _check_stale_bars (live_trader.py:4083-4089). This is a structural seam,
    not a silent config default.
  - The R4 tests assert only observable behavior (SystemExit or not); the
    counter's attribute name is the Coder's choice.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.live_execution import ibkr_client as ibkr_module
from src.live_execution import live_trader as lt_module


# ===========================================================================
# Frozen clocks — a coherent instant with a fabricated UTC-7 wall clock.
# Tue 2026-07-07 16:00 UTC == 12:00 ET (CL market OPEN — needed by the R4
# watchdog tests; irrelevant but harmless for R1).
# ===========================================================================

_UTC_NOW = datetime(2026, 7, 7, 16, 0, 0)            # tz-naive UTC "now"
_LOCAL_NOW = _UTC_NOW - timedelta(hours=7)           # simulated UTC-7 host


def _frozen_clock():
    """Patch lt_module.datetime so datetime.now(timezone.utc) == _UTC_NOW
    (aware) and datetime.now() == _LOCAL_NOW (naive local wall clock)."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            if tz is not None:
                return _UTC_NOW.replace(tzinfo=timezone.utc).astimezone(tz)
            return _LOCAL_NOW

        @classmethod
        def utcnow(cls):  # noqa: D102
            return _UTC_NOW

    return patch.object(lt_module, "datetime", _Frozen)


class _TimestampStandin:
    """Stands in for pd.Timestamp inside live_trader: controls now()/utcnow()
    while delegating construction to the real pd.Timestamp. now() without tz
    returns the LOCAL wall clock (what the buggy code reads); now(tz=...) and
    utcnow() return the frozen UTC instant (tz-aware, as real pandas does)."""

    def __call__(self, *args, **kwargs):
        return pd.Timestamp(*args, **kwargs)

    def now(self, tz=None):
        if tz is None:
            return pd.Timestamp(_LOCAL_NOW)
        return pd.Timestamp(_UTC_NOW, tz="UTC").tz_convert(tz)

    def utcnow(self):
        return pd.Timestamp(_UTC_NOW, tz="UTC")


class _PandasProxy:
    """Delegates every attribute to real pandas except Timestamp."""

    def __init__(self):
        self.Timestamp = _TimestampStandin()

    def __getattr__(self, name):
        return getattr(pd, name)


def _frozen_pandas():
    return patch.object(lt_module, "pd", _PandasProxy())


# ===========================================================================
# Stub builders
# ===========================================================================

def _backfill_stub():
    """LiveTrader stub with only the seams _backfill_reconnect_gap_async
    reads. The fetch mock returns an EMPTY DataFrame so the stitch branch
    (rolling concat / DataManager / telegram) is never entered — the tests
    pin only WHETHER the historical fetch is issued."""
    trader = object.__new__(lt_module.LiveTrader)
    trader.data_client = MagicMock()
    trader.data_client.fetch_historical_bars_by_duration_async = AsyncMock(
        return_value=pd.DataFrame()
    )
    trader._telegram = MagicMock()
    trader._warmup_inference_state = MagicMock()
    trader.rolling_df_5m = pd.DataFrame()
    trader.rolling_df_1h = pd.DataFrame()
    trader.data_manager_5m = MagicMock()
    trader.data_manager_1h = MagicMock()
    trader._bar_size = "5m"
    trader._last_bar_time_5m = None
    trader._last_bar_time_1h = None
    return trader


def _reconnect_stub():
    """Mirror of tests/test_reconnection.py::_make_trader_stub with two
    deliberate differences: _telegram is a MagicMock (asserted on) and
    _on_ib_error is the REAL method (the R2 tests exercise the 2104 race)."""
    trader = object.__new__(lt_module.LiveTrader)

    trader.data_client = MagicMock()
    trader.data_client.connect = MagicMock()
    trader.data_client.disconnect = MagicMock()
    trader.data_client.cancel_subscription = MagicMock()

    trader.exec_client = MagicMock()
    trader.exec_client.connect = MagicMock()
    trader.exec_client.disconnect = MagicMock()

    trader._telegram = MagicMock()

    trader.strategy = MagicMock()
    trader.strategy.name = "MockStrategy"

    trader._running = True
    trader._subscriptions_lost = False
    trader._execution_symbol = "CL"
    trader._front_month_local_symbol = "CLN6"
    trader._front_month_str = "202608"
    trader._last_rollover_check_date = None
    trader._resubscribe_pending = False
    trader._live_bars_5m = None
    trader._live_bars_1h = None
    trader._front_month_bars = None
    trader._last_bar_time_5m = None
    trader._last_bar_time_1h = None
    trader._bar_size = "5m"
    trader._callbacks_registered = False
    trader._needs_restart = False
    trader._restart_count = 0
    trader._data_farm_ok = False
    trader._data_farm_broken_only = False

    trader._subscribe = MagicMock()
    trader._subscribe_front_month = MagicMock()

    trader._virtual_ledger = {"5m": 0, "1h": 0}
    trader._stop_event = threading.Event()
    trader._register_execution_callbacks = MagicMock()

    return trader


def _deferred_stub():
    """LiveTrader stub for the async _deferred_resubscribe path."""
    trader = object.__new__(lt_module.LiveTrader)
    trader.data_client = MagicMock()
    trader.data_client.cancel_subscription = MagicMock()
    trader._telegram = MagicMock()
    trader._execution_symbol = "CL"
    trader._bar_size = "1h"
    trader._live_bars_5m = None
    trader._live_bars_1h = None
    trader._front_month_bars = None
    trader._front_month_local_symbol = "CLN6"
    trader._front_month_str = "202608"
    trader._last_bar_time_5m = None
    trader._last_bar_time_1h = None
    return trader


def _watchdog_stub():
    """Watchdog stub (mirrors tests/test_session_watchdog_rollover.py).

    NOTE: no fruitless-counter attribute is set here ON PURPOSE — see the
    module docstring's DELIBERATE SEAM OMISSIONS."""
    trader = object.__new__(lt_module.LiveTrader)
    trader.data_client = MagicMock()
    trader.exec_client = MagicMock()
    trader._telegram = MagicMock()
    trader._subscriptions_lost = False
    trader._execution_symbol = "CL"
    trader._last_bar_time_5m = pd.Timestamp(_UTC_NOW) - pd.Timedelta(minutes=60)
    return trader


def _attach_5m_bar_seams(trader):
    """Seams the REAL _on_bar_update_5m handler reads (heavy deps mocked)."""
    trader.rolling_df_5m = pd.DataFrame()
    trader.data_manager_5m = MagicMock()
    trader.telemetry = MagicMock()
    trader._ledger_lock = threading.Lock()
    trader._check_trailing_stop = MagicMock()
    trader._on_new_bar = MagicMock()
    trader._bar_size = "5m"


def _attach_1h_bar_seams(trader):
    """Seams the REAL _on_bar_update_1h handler reads (heavy deps mocked)."""
    trader.rolling_df_1h = pd.DataFrame()
    trader.data_manager_1h = MagicMock()
    trader.telemetry = MagicMock()
    trader._ledger_lock = threading.Lock()
    trader._check_trailing_stop = MagicMock()
    trader._on_new_bar = MagicMock()
    trader._bar_size = "1h"


def _fake_bars(completed_time_naive_utc, step_minutes):
    """ib_insync-shaped bars list: [completed bar, new incomplete bar].
    The handlers read bars[-2] as the just-closed bar."""
    completed = SimpleNamespace(
        date=completed_time_naive_utc,
        open=70.0, high=70.5, low=69.5, close=70.2, volume=100,
    )
    incomplete = SimpleNamespace(
        date=completed_time_naive_utc + timedelta(minutes=step_minutes),
        open=70.2, high=70.3, low=70.1, close=70.25, volume=7,
    )
    return [completed, incomplete]


# ===========================================================================
# reqHistoricalData call-shape helpers (kwargs OR positional — the current
# manager passes contract positionally and everything else by keyword)
# ===========================================================================

_REQ_PARAM_POS = {
    "endDateTime": 1, "durationStr": 2, "barSizeSetting": 3,
    "whatToShow": 4, "useRTH": 5, "formatDate": 6, "keepUpToDate": 7,
}


def _req_contract(call):
    if call.args:
        return call.args[0]
    return call.kwargs["contract"]


def _req_param(call, name):
    if name in call.kwargs:
        return call.kwargs[name]
    pos = _REQ_PARAM_POS[name]
    assert len(call.args) > pos, (
        f"reqHistoricalData call carries no '{name}' (kwargs={call.kwargs!r}, "
        f"args={call.args!r})"
    )
    return call.args[pos]


_DUR_UNIT_SECONDS = {"S": 1, "D": 86400, "W": 604800, "M": 2592000, "Y": 31536000}


def _duration_seconds(duration_str):
    num, unit = str(duration_str).strip().split()
    return int(num) * _DUR_UNIT_SECONDS[unit.upper()]


def _manager_stub():
    mgr = object.__new__(ibkr_module.IBKRConnectionManager)
    mgr.ib = MagicMock()
    mgr.ensure_connected = MagicMock()  # instance attr shadows the method
    return mgr


def _fake_contract():
    return SimpleNamespace(
        symbol="MES", localSymbol="MESU6", secType="FUT", exchange="CME",
    )


# ===========================================================================
# R1 — Backfill gap must be computed in tz-naive UTC
# ===========================================================================

class TestR1BackfillGapUTC:
    """R1: _backfill_reconnect_gap_async currently anchors the gap on
    pd.Timestamp.now() (LOCAL wall clock) while _last_bar_time_5m/_1h are
    tz-naive UTC, so the gap computes ~real_gap - 7h on the UTC-7 host and
    the backfill never fires. Both clock seams (lt_module.datetime and
    lt_module.pd) are frozen to a coherent instant with a fabricated UTC-7
    local clock, so these tests are red on the current implementation on ANY
    host timezone. A tz-AWARE "now" would raise TypeError against the
    tz-naive last-bar timestamps — the pass condition implicitly pins the
    tz-naive-UTC requirement too (pandas 1.5.3: pd.Timestamp.utcnow() is
    aware and must be stripped)."""

    def test_r1_5m_gap_90min_utc_must_trigger_backfill_fetch(self):
        """With _last_bar_time_5m 90 UTC-minutes in the past, the 5M backfill
        MUST issue exactly one historical fetch ('1 D', 5 mins)."""
        trader = _backfill_stub()
        trader._last_bar_time_5m = pd.Timestamp(_UTC_NOW) - pd.Timedelta(minutes=90)

        with _frozen_clock(), _frozen_pandas():
            asyncio.run(trader._backfill_reconnect_gap_async())

        fetch = trader.data_client.fetch_historical_bars_by_duration_async
        assert fetch.await_count == 1, (
            "R1 violated: a 90-min UTC gap on the 5m brain stream did not "
            "trigger a reconnect backfill fetch (gap anchored on the local "
            "wall clock instead of tz-naive UTC computes negative)"
        )
        kwargs = fetch.await_args.kwargs
        assert kwargs.get("bar_size") == "5 mins"
        assert kwargs.get("duration_str") == "1 D", (
            "90-min gap must request the unchanged max(1, ...) = '1 D' window"
        )

    def test_r1_1h_gap_90min_utc_must_trigger_backfill_fetch(self):
        """Same bug on the 1H leg: gap > 70 min (UTC) MUST fetch 1-hour bars."""
        trader = _backfill_stub()
        trader._bar_size = "1h"
        trader._last_bar_time_5m = None
        trader._last_bar_time_1h = pd.Timestamp(_UTC_NOW) - pd.Timedelta(minutes=90)

        with _frozen_clock(), _frozen_pandas():
            asyncio.run(trader._backfill_reconnect_gap_async())

        fetch = trader.data_client.fetch_historical_bars_by_duration_async
        assert fetch.await_count == 1, (
            "R1 violated: a 90-min UTC gap on the 1h brain stream did not "
            "trigger a reconnect backfill fetch"
        )
        kwargs = fetch.await_args.kwargs
        assert kwargs.get("bar_size") == "1 hour"
        assert kwargs.get("duration_str") == "1 D"

    def test_r1_fresh_5m_bars_do_not_backfill(self):
        """FENCE (passes today): a 5-min-old bar is inside the 10-min
        threshold — the UTC fix must not make the backfill over-trigger."""
        trader = _backfill_stub()
        trader._last_bar_time_5m = pd.Timestamp(_UTC_NOW) - pd.Timedelta(minutes=5)

        with _frozen_clock(), _frozen_pandas():
            asyncio.run(trader._backfill_reconnect_gap_async())

        fetch = trader.data_client.fetch_historical_bars_by_duration_async
        assert fetch.await_count == 0, (
            "gap < 10 min must not trigger a 5M backfill fetch"
        )


# ===========================================================================
# R2 — _resubscribe_pending guard ordering in _reconnect
# ===========================================================================

class TestR2ResubscribeGuard:
    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.05)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 5)
    @patch.object(lt_module, "_DATA_FARM_WAIT_SECONDS", 1.0)
    def test_r2_guard_already_true_when_data_client_connect_called(self):
        """The guard must be True at the moment data_client.connect() runs —
        today it is set only AFTER connect + callback re-registration
        (live_trader.py:3752), leaving the 2104-during-handshake window
        open."""
        trader = _reconnect_stub()
        observed = []
        trader.data_client.connect.side_effect = (
            lambda: observed.append(trader._resubscribe_pending)
        )

        result = trader._reconnect()

        assert result is True
        assert observed, "data_client.connect was never called"
        assert observed[0] is True, (
            "R2 violated: _resubscribe_pending was False when "
            "data_client.connect() was called — a 2104 delivered during the "
            "connect handshake can still schedule _deferred_resubscribe and "
            "race the sync _resubscribe_and_backfill"
        )
        # Existing clear-afterward semantics preserved (set False at the end
        # of _resubscribe_and_backfill on the successful attempt).
        assert trader._resubscribe_pending is False

    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.05)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 5)
    @patch.object(lt_module, "_DATA_FARM_WAIT_SECONDS", 1.0)
    def test_r2_2104_during_connect_cannot_schedule_deferred_resubscribe(self):
        """A 2104 (market data farm OK) arriving DURING connect, with
        _subscriptions_lost=True, must NOT schedule _deferred_resubscribe.
        Exercises the REAL _on_ib_error; the loop seam is a MagicMock so
        scheduling is observable via call_soon without running a loop."""
        trader = _reconnect_stub()
        trader._subscriptions_lost = True
        fake_loop = MagicMock()

        def _connect_fires_2104():
            trader._on_ib_error(
                -1, 2104, "Market data farm connection is OK:usfarm", None
            )

        trader.data_client.connect.side_effect = _connect_fires_2104

        with patch("asyncio.get_event_loop", return_value=fake_loop):
            result = trader._reconnect()

        assert result is True
        assert not fake_loop.call_soon.called, (
            "R2 violated: the 2104 handler scheduled _deferred_resubscribe "
            "during the connect handshake — this is the double-subscription "
            "race from the fleet logs"
        )
        # The sync resubscription path still ran exactly once.
        trader._subscribe.assert_called_once()
        trader._subscribe_front_month.assert_called_once()
        assert trader._resubscribe_pending is False

    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.02)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 3)
    @patch.object(lt_module, "_DATA_FARM_WAIT_SECONDS", 1.0)
    def test_r2_guard_not_leaked_after_reconnect_gives_up(self):
        """FENCE (passes today, guards the Green implementation): after
        _reconnect exhausts every attempt and returns False, the guard must
        not be leaked True — a later legitimate 2104 (subscriptions still
        lost) must still schedule _deferred_resubscribe."""
        trader = _reconnect_stub()
        trader._subscriptions_lost = True
        trader.data_client.connect.side_effect = ConnectionError("refused")

        result = trader._reconnect()
        assert result is False
        assert trader.data_client.connect.call_count == 3

        fake_loop = MagicMock()
        with patch("asyncio.get_event_loop", return_value=fake_loop):
            trader._on_ib_error(
                -1, 2104, "Market data farm connection is OK:usfarm", None
            )
        assert fake_loop.call_soon.call_count == 1, (
            "R2 reset semantics violated: _resubscribe_pending leaked True "
            "after _reconnect gave up, permanently blocking the deferred "
            "2104 recovery path"
        )


# ===========================================================================
# R3 — empty snapshot (dead keepUpToDate stream) must retry once, then raise
# ===========================================================================

class TestR3EmptySnapshotSubscription:
    """R3 at the manager seam (src/live_execution/ibkr_client.py:1020-1085):
    IBKR error 162 makes reqHistoricalData return an EMPTY BarDataList
    without raising, and the keepUpToDate stream is dead on arrival. The
    manager must retry ONCE with a fallback duration >= '1 D' (keepUpToDate
    still True, same contract) and RAISE if the retry is also empty."""

    # -- sync path ----------------------------------------------------------

    def test_r3_sync_empty_snapshot_retries_once_with_fallback_at_least_1d(self):
        mgr = _manager_stub()
        contract = _fake_contract()
        live_bars = [MagicMock()]
        mgr.ib.reqHistoricalData.side_effect = [[], live_bars]

        result = mgr.subscribe_live_bars(
            contract, bar_size="5 mins", duration_str="60 S"
        )

        assert mgr.ib.reqHistoricalData.call_count == 2, (
            "R3 violated: an empty snapshot (error-162 shape) was returned "
            "as a 'successful' subscription without a retry"
        )
        first, second = mgr.ib.reqHistoricalData.call_args_list
        assert _req_param(first, "durationStr") == "60 S"
        retry_duration = _req_param(second, "durationStr")
        assert _duration_seconds(retry_duration) >= 86400, (
            f"fallback duration {retry_duration!r} is shorter than '1 D' — "
            "weekend/holiday startups would still get an empty window"
        )
        assert _req_param(second, "keepUpToDate") is True, (
            "the retry must still be a live (keepUpToDate=True) subscription"
        )
        assert _req_param(second, "barSizeSetting") == "5 mins"
        assert _req_contract(second) is contract
        assert result is live_bars, (
            "the retry's live BarDataList must be the returned subscription"
        )

    def test_r3_sync_empty_after_retry_raises_naming_stream(self):
        mgr = _manager_stub()
        contract = _fake_contract()
        mgr.ib.reqHistoricalData.side_effect = [[], []]

        with pytest.raises(Exception) as excinfo:
            mgr.subscribe_live_bars(
                contract, bar_size="5 mins", duration_str="60 S"
            )

        msg = str(excinfo.value)
        assert "MES" in msg, (
            f"exception must name the stream's contract, got: {msg!r}"
        )
        assert "5 mins" in msg, (
            f"exception must name the stream's bar size, got: {msg!r}"
        )
        assert mgr.ib.reqHistoricalData.call_count == 2, (
            "exactly ONE retry — no unbounded retry loops"
        )

    def test_r3_sync_nonempty_first_snapshot_byte_identical_single_request(self):
        """FENCE (passes today): when the first snapshot returns data the
        request shape must stay byte-identical — one request, same params."""
        mgr = _manager_stub()
        contract = _fake_contract()
        live_bars = [MagicMock()]
        mgr.ib.reqHistoricalData.side_effect = [live_bars]

        result = mgr.subscribe_live_bars(
            contract, bar_size="5 mins", duration_str="60 S"
        )

        assert result is live_bars
        assert mgr.ib.reqHistoricalData.call_count == 1, (
            "no extra requests when the first snapshot has data"
        )
        call = mgr.ib.reqHistoricalData.call_args
        assert _req_contract(call) is contract
        assert _req_param(call, "endDateTime") == ""
        assert _req_param(call, "durationStr") == "60 S"
        assert _req_param(call, "barSizeSetting") == "5 mins"
        assert _req_param(call, "whatToShow") == "TRADES"
        assert _req_param(call, "useRTH") is False
        assert _req_param(call, "formatDate") == 1
        assert _req_param(call, "keepUpToDate") is True

    def test_r3_one_hour_stream_empty_after_retry_raises(self):
        """The 1h brain stream ('2 D' request shape) gets the same
        contract: retry once, then raise naming the stream."""
        mgr = _manager_stub()
        contract = _fake_contract()
        mgr.ib.reqHistoricalData.side_effect = [[], []]

        with pytest.raises(Exception) as excinfo:
            mgr.subscribe_live_bars(
                contract, bar_size="1 hour", duration_str="2 D"
            )

        msg = str(excinfo.value)
        assert "MES" in msg
        assert "1 hour" in msg
        assert mgr.ib.reqHistoricalData.call_count == 2
        retry_duration = _req_param(
            mgr.ib.reqHistoricalData.call_args_list[1], "durationStr"
        )
        assert _duration_seconds(retry_duration) >= 86400

    # -- async path ---------------------------------------------------------

    def test_r3_async_empty_snapshot_retries_once_with_fallback_at_least_1d(self):
        mgr = _manager_stub()
        contract = _fake_contract()
        live_bars = [MagicMock()]
        mgr.ib.reqHistoricalDataAsync = AsyncMock(side_effect=[[], live_bars])

        result = asyncio.run(
            mgr.subscribe_live_bars_async(
                contract, bar_size="5 mins", duration_str="60 S"
            )
        )

        assert mgr.ib.reqHistoricalDataAsync.await_count == 2, (
            "R3 violated (async): empty snapshot treated as a successful "
            "subscription without a retry"
        )
        second = mgr.ib.reqHistoricalDataAsync.await_args_list[1]
        retry_duration = _req_param(second, "durationStr")
        assert _duration_seconds(retry_duration) >= 86400
        assert _req_param(second, "keepUpToDate") is True
        assert _req_contract(second) is contract
        assert result is live_bars

    def test_r3_async_empty_after_retry_raises_naming_stream(self):
        mgr = _manager_stub()
        contract = _fake_contract()
        mgr.ib.reqHistoricalDataAsync = AsyncMock(side_effect=[[], []])

        with pytest.raises(Exception) as excinfo:
            asyncio.run(
                mgr.subscribe_live_bars_async(
                    contract, bar_size="5 mins", duration_str="60 S"
                )
            )

        msg = str(excinfo.value)
        assert "MES" in msg
        assert "5 mins" in msg
        assert mgr.ib.reqHistoricalDataAsync.await_count == 2

    def test_r3_async_nonempty_first_snapshot_byte_identical_single_request(self):
        """FENCE (passes today): async happy path — one request, unchanged."""
        mgr = _manager_stub()
        contract = _fake_contract()
        live_bars = [MagicMock()]
        mgr.ib.reqHistoricalDataAsync = AsyncMock(side_effect=[live_bars])

        result = asyncio.run(
            mgr.subscribe_live_bars_async(
                contract, bar_size="5 mins", duration_str="60 S"
            )
        )

        assert result is live_bars
        assert mgr.ib.reqHistoricalDataAsync.await_count == 1
        call = mgr.ib.reqHistoricalDataAsync.await_args
        assert _req_param(call, "durationStr") == "60 S"
        assert _req_param(call, "keepUpToDate") is True

    # -- propagation into the LiveTrader recovery paths ----------------------

    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.05)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 5)
    @patch.object(lt_module, "_DATA_FARM_WAIT_SECONDS", 1.0)
    @patch.object(lt_module, "_WATCHDOG_TG_COOLDOWN_SECONDS", 0)
    def test_r3_reconnect_attempt_counted_failed_when_subscribe_raises(self):
        """FENCE (passes today): the R3 raise must propagate into
        _reconnect's attempt-level except — the attempt is FAILED, retried
        with backoff, and 'Reconnected successfully' is only announced for
        the attempt whose subscriptions actually came up.
        (throttle disabled here — throttle behavior is pinned in
        tests/test_watchdog_telegram_throttle.py)"""
        trader = _reconnect_stub()
        trader._subscribe.side_effect = [
            RuntimeError("live 5 mins subscription for MES returned no bars"),
            None,
        ]

        result = trader._reconnect()

        assert result is True
        assert trader.data_client.connect.call_count == 2, (
            "the empty-stream raise must fail attempt 1 and force attempt 2"
        )
        assert trader._subscribe.call_count == 2
        success_msgs = [
            c.args[0]
            for c in trader._telegram.send.call_args_list
            if "*RECONNECTED*" in str(c.args[0])
        ]
        assert len(success_msgs) == 1, (
            f"success must be announced exactly once, got: {success_msgs!r}"
        )
        assert "attempt 2" in success_msgs[0]

    def test_r3_deferred_resubscribe_failure_keeps_subscriptions_lost_true(self):
        """FENCE (passes today): when the async resubscription raises (as the
        R3 empty-after-retry path now will), _deferred_resubscribe must
        swallow it via its except/log.exception path, leave
        _subscriptions_lost True (so recovery re-triggers) and clear
        _resubscribe_pending in finally."""
        trader = _deferred_stub()
        trader.data_client.subscribe_live_bars_async = AsyncMock(
            side_effect=RuntimeError(
                "live 5 mins subscription for CL returned no bars after retry"
            )
        )
        trader._subscriptions_lost = True
        trader._resubscribe_pending = True

        asyncio.run(trader._deferred_resubscribe())  # must not propagate

        assert trader._subscriptions_lost is True, (
            "a failed deferred resubscription must leave _subscriptions_lost "
            "True so the next 2104/watchdog cycle can re-trigger recovery"
        )
        assert trader._resubscribe_pending is False


# ===========================================================================
# R4 — escalate after N fruitless watchdog reconnects
# ===========================================================================

class TestR4FruitlessReconnectEscalation:
    """R4: consecutive _check_stale_bars firings with NO new brain-stream bar
    between them must escalate at _MAX_FRUITLESS_RECONNECTS = 3: CRITICAL
    log + Telegram attempt + SystemExit(non-zero) so fleet_runner restarts
    the child. Any new bar on the 5m/1h brain streams resets the counter
    to 0. All instants are frozen at Tue 2026-07-07 16:00 UTC (12:00 ET,
    CL OPEN); the initial last bar is 60 min old (>> the 15-min threshold)."""

    def test_r4_module_constant_max_fruitless_reconnects_is_3(self):
        assert lt_module._MAX_FRUITLESS_RECONNECTS == 3, (
            "_MAX_FRUITLESS_RECONNECTS must be a module-level constant == 3"
        )

    @patch.object(lt_module, "_WATCHDOG_TG_COOLDOWN_SECONDS", 0)
    def test_r4_third_consecutive_fruitless_firing_escalates(self, caplog):
        """Fires 1 and 2 behave exactly like today (return True → caller
        reconnects); the 3rd consecutive fruitless firing must raise
        SystemExit with a non-zero code, emit a CRITICAL log record, and
        attempt a Telegram notification.
        (throttle disabled here — throttle behavior is pinned in
        tests/test_watchdog_telegram_throttle.py)"""
        trader = _watchdog_stub()

        with _frozen_clock():
            with caplog.at_level(logging.INFO):
                assert trader._check_stale_bars() is True   # fruitless #1
                assert trader._check_stale_bars() is True   # fruitless #2

                trader._telegram.send.reset_mock()
                caplog.clear()

                with pytest.raises(SystemExit) as excinfo:
                    trader._check_stale_bars()              # fruitless #3

        assert excinfo.value.code not in (None, 0), (
            "escalation must terminate with a NON-ZERO exit so fleet_runner "
            "sees a crashed child and restarts it"
        )
        assert any(r.levelno >= logging.CRITICAL for r in caplog.records), (
            "escalation must log at CRITICAL level"
        )
        assert trader._telegram.send.called, (
            "escalation must attempt a Telegram notification"
        )

    @patch.object(lt_module, "_WATCHDOG_TG_COOLDOWN_SECONDS", 0)
    def test_r4_telegram_failure_never_blocks_escalation_exit(self):
        """A broken Telegram must not prevent the SystemExit escalation.
        (throttle disabled here — throttle behavior is pinned in
        tests/test_watchdog_telegram_throttle.py)"""
        trader = _watchdog_stub()
        trader._telegram.send.side_effect = Exception("telegram down")

        with _frozen_clock():
            assert trader._check_stale_bars() is True
            assert trader._check_stale_bars() is True
            with pytest.raises(SystemExit) as excinfo:
                trader._check_stale_bars()

        assert excinfo.value.code not in (None, 0)
        assert trader._telegram.send.called

    def test_r4_new_5m_brain_bar_resets_counter_to_zero(self):
        """Two fruitless fires, then a REAL 5m brain bar (driven through the
        actual _on_bar_update_5m handler) — the counter must reset to ZERO:
        the next two fires are ordinary reconnect signals and only the third
        post-reset fire escalates."""
        trader = _watchdog_stub()
        _attach_5m_bar_seams(trader)

        with _frozen_clock():
            assert trader._check_stale_bars() is True       # fruitless #1
            assert trader._check_stale_bars() is True       # fruitless #2

            # New brain bar 40 min old: newer than the 60-min-old last bar
            # (handler accepts it) but still past the 30-min staleness
            # threshold (watchdog keeps firing afterwards).
            trader._on_bar_update_5m(
                _fake_bars(_UTC_NOW - timedelta(minutes=40), step_minutes=5),
                has_new_bar=True,
            )
            assert trader._last_bar_time_5m == pd.Timestamp(
                _UTC_NOW - timedelta(minutes=40)
            ), "sanity: the real 5m handler must have accepted the bar"

            assert trader._check_stale_bars() is True       # post-reset #1
            assert trader._check_stale_bars() is True       # post-reset #2
            with pytest.raises(SystemExit):
                trader._check_stale_bars()                  # post-reset #3

    def test_r4_new_1h_brain_bar_resets_counter_hourly_only_instance(self):
        """Hourly-only instances (enable_5m_stream=False) watch the 1h
        stream — a REAL 1h bar via _on_bar_update_1h must reset the counter
        exactly like a 5m bar."""
        trader = _watchdog_stub()
        trader._enable_5m_stream = False
        trader._last_bar_time_1h = pd.Timestamp(_UTC_NOW) - pd.Timedelta(minutes=200)
        _attach_1h_bar_seams(trader)

        with _frozen_clock():
            assert trader._check_stale_bars() is True       # fruitless #1
            assert trader._check_stale_bars() is True       # fruitless #2

            # 140-min-old 1h bar: newer than the 200-min-old anchor, still
            # past the 135-min 1h threshold.
            trader._on_bar_update_1h(
                _fake_bars(_UTC_NOW - timedelta(minutes=140), step_minutes=60),
                has_new_bar=True,
            )
            assert trader._last_bar_time_1h == pd.Timestamp(
                _UTC_NOW - timedelta(minutes=140)
            ), "sanity: the real 1h handler must have accepted the bar"

            assert trader._check_stale_bars() is True       # post-reset #1
            assert trader._check_stale_bars() is True       # post-reset #2
            with pytest.raises(SystemExit):
                trader._check_stale_bars()                  # post-reset #3

    def test_r4_watchdog_fires_with_recovery_between_never_escalate(self):
        """FENCE (passes today): four fire→recover cycles (a new bar lands
        after every firing) must NEVER escalate even though the total number
        of firings exceeds the threshold — only CONSECUTIVE fruitless
        firings count."""
        trader = _watchdog_stub()
        _attach_5m_bar_seams(trader)

        with _frozen_clock():
            for i, minutes_old in enumerate((50, 45, 40, 35)):
                assert trader._check_stale_bars() is True, (
                    f"cycle {i}: an ordinary stale firing must still signal "
                    "a reconnect"
                )
                trader._on_bar_update_5m(
                    _fake_bars(
                        _UTC_NOW - timedelta(minutes=minutes_old),
                        step_minutes=5,
                    ),
                    has_new_bar=True,
                )
