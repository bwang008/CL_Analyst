"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
                            (R1: _deferred_resubscribe retry timer with
                             exponential backoff via new seam method
                             _schedule_resubscribe_retry; retry counter
                             _resubscribe_retry_count; constants
                             _RESUBSCRIBE_RETRY_BASE_SECONDS=60,
                             _RESUBSCRIBE_RETRY_CAP_SECONDS=300,
                             _MAX_RESUBSCRIBE_RETRIES=5;
                             R2: _check_stale_bars non-fatal firing emits a
                             health event via _emit_health_event),
                            src/live_execution/fleet_error_events.py
                            (R3: emit_child_health_event — schema-compatible
                             non-crash queue events with dedup + infra
                             classification, never raises)
Target Class/Function: LiveTrader._deferred_resubscribe,
                       LiveTrader._schedule_resubscribe_retry,
                       LiveTrader._emit_health_event,
                       LiveTrader._check_stale_bars,
                       fleet_error_events.emit_child_health_event
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: resubscribe-retry-blindness_07062026_0640
Phase: RED — every requirement has at least one test that FAILS on current
HEAD (2026-07-06 incident: farm-OK fired during the IP-session conflict, all
resubscribes failed, and no retry path existed — children blind until manual
restart).

Conventions (mirroring tests/test_reconnect_recovery_fixes.py):
  - LiveTrader stubs via object.__new__ with only the seams each method reads.
  - Async methods driven with asyncio.run + AsyncMock.
  - _schedule_resubscribe_retry / _emit_health_event are mocked as instance
    attributes in the _deferred_resubscribe/_check_stale_bars tests — their
    NAMES are part of the blueprint contract.
  - pandas 1.5.3 compatible (no pandas 2.x APIs).

DELIBERATE SEAM OMISSIONS (contract for the Coder):
  - The retry counter must be stub-safe (getattr default 0) on object.__new__
    instances — tests for attempt 1 do NOT pre-set it.
  - The stale-bar tests patch lt_module._session_open_anchor to None rather
    than building a real instrument: the health-event emission must not
    depend on instrument internals.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.live_execution import fleet_error_events as fee_module
from src.live_execution import live_trader as lt_module


# ===========================================================================
# Stub builders
# ===========================================================================

def _make_trader(subscribe_side_effect):
    """LiveTrader stub with exactly the seams _deferred_resubscribe reads."""
    lt = object.__new__(lt_module.LiveTrader)
    lt._live_bars_5m = None
    lt._live_bars_1h = None
    lt._front_month_bars = None
    lt._enable_5m_stream = True
    # _brain_symbol/_brain_instrument are read-only properties backed by
    # the resolved InstrumentContext — stub the context, not the property.
    lt._instrument_context = SimpleNamespace(
        brain_symbol="NG", brain_instrument=MagicMock(),
    )
    lt._bar_size = "1h"
    lt._front_month_local_symbol = None
    lt._subscriptions_lost = True
    lt._resubscribe_pending = True
    lt.data_client = MagicMock()
    lt.data_client.subscribe_live_bars_async = AsyncMock(
        side_effect=subscribe_side_effect
    )
    lt._on_bar_update_5m = MagicMock()
    lt._on_bar_update_1h = MagicMock()
    lt._on_front_month_bar_update = MagicMock()
    lt._backfill_reconnect_gap_async = AsyncMock()
    lt._schedule_resubscribe_retry = MagicMock()
    lt._emit_health_event = MagicMock()
    return lt


def _failing_trader():
    return _make_trader(RuntimeError(
        "Live 5 mins subscription for NGQ26 returned no bars even after the "
        "1 D fallback retry — the keepUpToDate stream is dead on arrival "
        "(IBKR error-162 shape)"
    ))


def _succeeding_trader():
    return _make_trader(lambda **kwargs: MagicMock())


def _make_queue(tmp_path):
    """Real queue layout + a one-pattern infra collection."""
    q = tmp_path / "error_queue"
    for sub in ("pending", "processing", "done"):
        (q / sub).mkdir(parents=True)
    patterns = q / "infra_patterns.json"
    patterns.write_text(json.dumps({"patterns": [{
        "name": "data-farm-broken",
        "regex": "data farm connection is broken",
        "notes": "IBKR farm flap",
    }]}), encoding="utf-8")
    return q, patterns


# ===========================================================================
# R1 — retry timer with exponential backoff
# ===========================================================================

class TestResubscribeRetry:

    def test_module_constants(self):
        assert lt_module._RESUBSCRIBE_RETRY_BASE_SECONDS == 60
        assert lt_module._RESUBSCRIBE_RETRY_CAP_SECONDS == 300
        assert lt_module._MAX_RESUBSCRIBE_RETRIES == 5

    def test_failure_schedules_retry_with_base_delay(self):
        """First failure: retry in 60s, guard stays up, counter == 1."""
        lt = _failing_trader()
        asyncio.run(lt._deferred_resubscribe())
        lt._schedule_resubscribe_retry.assert_called_once_with(60)
        assert lt._resubscribe_pending is True, (
            "guard must stay True while a retry timer is outstanding so a "
            "racing farm-OK event cannot double-schedule"
        )
        assert lt._resubscribe_retry_count == 1

    @pytest.mark.parametrize("prior_count,expected_delay", [
        (1, 120),   # attempt 2: 60 * 2
        (2, 240),   # attempt 3: 60 * 4
        (3, 300),   # attempt 4: 60 * 8 = 480 -> capped
        (4, 300),   # attempt 5: capped
    ])
    def test_backoff_progression_and_cap(self, prior_count, expected_delay):
        lt = _failing_trader()
        lt._resubscribe_retry_count = prior_count
        asyncio.run(lt._deferred_resubscribe())
        lt._schedule_resubscribe_retry.assert_called_once_with(expected_delay)
        assert lt._resubscribe_retry_count == prior_count + 1

    def test_retries_exhausted_emits_health_event_and_clears_guard(self):
        """After _MAX_RESUBSCRIBE_RETRIES failures: no new timer, guard
        cleared (farm-OK path re-arms; watchdog is the backstop), and a
        health event surfaces the exhaustion to the error queue."""
        lt = _failing_trader()
        lt._resubscribe_retry_count = lt_module._MAX_RESUBSCRIBE_RETRIES
        asyncio.run(lt._deferred_resubscribe())
        lt._schedule_resubscribe_retry.assert_not_called()
        assert lt._resubscribe_pending is False
        assert lt._emit_health_event.call_count == 1
        args = lt._emit_health_event.call_args.args
        kwargs = lt._emit_health_event.call_args.kwargs
        kind = args[0] if args else kwargs.get("kind")
        assert kind == "resubscribe-retries-exhausted"

    def test_success_resets_counter_and_clears_guard(self):
        lt = _succeeding_trader()
        lt._resubscribe_retry_count = 3
        asyncio.run(lt._deferred_resubscribe())
        assert lt._subscriptions_lost is False
        assert lt._resubscribe_retry_count == 0
        assert lt._resubscribe_pending is False
        lt._schedule_resubscribe_retry.assert_not_called()
        lt._emit_health_event.assert_not_called()

    def test_schedule_seam_reinvokes_deferred_resubscribe(self):
        """The real seam arms an event-loop timer that re-enters
        _deferred_resubscribe — no farm-OK event required."""
        lt = object.__new__(lt_module.LiveTrader)
        lt._deferred_resubscribe = AsyncMock()

        async def drive():
            lt_module.LiveTrader._schedule_resubscribe_retry(lt, 0.01)
            await asyncio.sleep(0.08)

        asyncio.run(drive())
        lt._deferred_resubscribe.assert_awaited()


# ===========================================================================
# R2 — watchdog firing emits a health event
# ===========================================================================

class TestWatchdogHealthEvent:

    def _make_stale_trader(self):
        lt = object.__new__(lt_module.LiveTrader)
        lt._get_market_status = lambda now: "OPEN"
        lt._enable_5m_stream = True
        lt._last_bar_time_5m = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=45)
        )
        lt._instrument_context = SimpleNamespace(
            brain_symbol="NG", brain_instrument=MagicMock(),
        )
        lt._subscriptions_lost = True
        lt._fruitless_reconnect_count = 0
        lt._send_watchdog_telegram = MagicMock()
        lt._emit_health_event = MagicMock()
        lt.data_client = MagicMock()
        lt.exec_client = MagicMock()
        return lt

    def test_non_fatal_watchdog_fire_emits_health_event(self):
        lt = self._make_stale_trader()
        with patch.object(lt_module, "_session_open_anchor",
                          return_value=None):
            result = lt._check_stale_bars()
        assert result is True  # caller should reconnect (unchanged)
        assert lt._emit_health_event.call_count == 1
        args = lt._emit_health_event.call_args.args
        kwargs = lt._emit_health_event.call_args.kwargs
        kind = args[0] if args else kwargs.get("kind")
        detail = (args[1] if len(args) > 1 else kwargs.get("detail")) or ""
        assert kind == "stale-bars-watchdog"
        assert "no bars" in detail.lower()

    def test_not_stale_no_health_event(self):
        lt = self._make_stale_trader()
        lt._last_bar_time_5m = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=5)
        )
        with patch.object(lt_module, "_session_open_anchor",
                          return_value=None):
            result = lt._check_stale_bars()
        assert result is False
        lt._emit_health_event.assert_not_called()


# ===========================================================================
# R3 — emit_child_health_event
# ===========================================================================

class TestEmitChildHealthEvent:

    def test_health_event_written_with_schema(self, tmp_path):
        q, patterns = _make_queue(tmp_path)
        detail = "STALE BAR WATCHDOG: no bars for 45 min (subs_lost=True)"
        path = fee_module.emit_child_health_event(
            model_name="NG01B_Sharpe_E02_07052026", client_id=3000,
            kind="stale-bars-watchdog", detail=detail,
            queue_dir=q, patterns_path=patterns,
        )
        assert path is not None and path.parent.name == "pending"
        event = json.loads(path.read_text(encoding="utf-8"))
        assert event["event_kind"] == "health"
        assert event["health_kind"] == "stale-bars-watchdog"
        assert event["model_name"] == "NG01B_Sharpe_E02_07052026"
        assert event["client_id"] == 3000
        assert event["classification"] == "unknown"
        assert event["gave_up"] is False
        assert event["occurrences"] == 1
        assert event["traceback"] == detail
        assert event["event_id"].startswith("NG01B_Sharpe_E02_07052026_")

    def test_health_event_infra_classification(self, tmp_path):
        q, patterns = _make_queue(tmp_path)
        path = fee_module.emit_child_health_event(
            model_name="GC", client_id=4000, kind="stale-bars-watchdog",
            detail="Market data farm connection is broken:usfarm — no bars",
            queue_dir=q, patterns_path=patterns,
        )
        event = json.loads(path.read_text(encoding="utf-8"))
        assert event["classification"] == "infrastructure"
        assert event["matched_infra_pattern"] == "data-farm-broken"

    def test_health_event_dedup_updates_occurrences(self, tmp_path):
        q, patterns = _make_queue(tmp_path)
        kwargs = dict(model_name="NG", client_id=3000,
                      kind="stale-bars-watchdog",
                      detail="no bars for 45 min",
                      queue_dir=q, patterns_path=patterns)
        first = fee_module.emit_child_health_event(**kwargs)
        second = fee_module.emit_child_health_event(**kwargs)
        assert second == first
        pending = list((q / "pending").glob("*.json"))
        assert len(pending) == 1
        event = json.loads(pending[0].read_text(encoding="utf-8"))
        assert event["occurrences"] == 2

    def test_health_event_processing_skip(self, tmp_path):
        q, patterns = _make_queue(tmp_path)
        kwargs = dict(model_name="NG", client_id=3000,
                      kind="stale-bars-watchdog",
                      detail="no bars for 45 min",
                      queue_dir=q, patterns_path=patterns)
        first = fee_module.emit_child_health_event(**kwargs)
        (q / "processing" / first.name).write_text(
            first.read_text(encoding="utf-8"), encoding="utf-8")
        first.unlink()
        again = fee_module.emit_child_health_event(**kwargs)
        assert again is None
        assert list((q / "pending").glob("*.json")) == []

    def test_health_event_never_raises(self, tmp_path):
        # Missing patterns file — must swallow and return None, never raise
        # (emission failure must not take down the trading child).
        result = fee_module.emit_child_health_event(
            model_name="NG", client_id=3000, kind="k", detail="d",
            queue_dir=tmp_path / "q",
            patterns_path=tmp_path / "missing_patterns.json",
        )
        assert result is None
