"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._backfill_reconnect_gap_async
                       (BOTH the 5M stitch block ~:4494-4555 and the 1H stitch
                        block ~:4557-4616 must drop the incomplete tail bar —
                        bar_end = index + fetch_bar_duration > now — from the
                        fetched chunk_df BEFORE the
                        `new_bars = chunk_df[chunk_df.index > _last_bar_time_*]`
                        filter, so the still-forming bar is never stitched into
                        rolling_df_* / the DataManager cache and never advances
                        _last_bar_time_*.  Interacts with the timestamp-only
                        dedup in LiveTrader._on_bar_update_1h (~:4779) /
                        _on_bar_update_5m (~:4721): once the partial no longer
                        advances _last_bar_time_*, the live-stream COMPLETED bar
                        of the same timestamp is no longer deduped away and
                        fires NEW <N> BAR + inference normally.)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: reconnect-backfill-inprogress-hourbar_07202026_1120
Phase: RED — every deliverable has at least one assertion that FAILS on
current HEAD because the reconnect backfill stitches the not-yet-closed final
bar returned by the IBKR historical fetch (an empty `endDateTime` makes IBKR
return the currently-forming bar as the last row; the stitch filter only drops
OLDER bars, nothing drops the incomplete tail).

Incident (2026-07-20): a mid-hour reconnect+backfill stitched the in-progress
16:00 UTC (10:00 PT) 1H bar, advancing _last_bar_time_1h to 16:00; when the
live stream later delivered the COMPLETED 16:00 bar, the `bar_time <=
_last_bar_time_1h: return` dedup dropped it -> that hour's NEW 1H BAR +
inference fired for NONE of the 5 fleet children.

Conventions (mirroring tests/test_reconnect_recovery_fixes.py):
  - LiveTrader stubs via object.__new__ with only the seams each method reads.
  - The wall clock is frozen by patching lt_module.datetime with a datetime
    subclass whose now(timezone.utc) returns a fixed instant, so the backfill's
    `now = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))`
    (live_trader.py:4491) is deterministic on ANY host timezone.  The fetched
    chunk_df's bar timestamps are built relative to that frozen `now`.
  - pd is left UNPATCHED (the backfill's `now` is derived from datetime.now(),
    not pd.Timestamp.now(), so only the datetime seam needs freezing) — real
    pandas 1.5.3 is used for the DatetimeIndex math and the stitch/dedup path.
  - Async method driven with asyncio.run + AsyncMock.

The frozen `now` sits MID-HOUR (16:17:00 UTC), so every "completed" bar's close
is strictly < now and the "in-progress" tail's close is strictly > now — the
tests catch the missing incomplete-bar drop without depending on the exact
boundary semantics of the <= comparison the fix will use.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

from src.live_execution import live_trader as lt_module


# ===========================================================================
# Frozen wall clock — a fixed MID-HOUR UTC instant (matches the incident hour).
# ===========================================================================

_NOW_UTC = datetime(2026, 7, 20, 16, 17, 0)  # tz-naive UTC "now" (mid-hour)


def _frozen_clock():
    """Patch lt_module.datetime so datetime.now(timezone.utc) == _NOW_UTC
    (tz-aware), which the backfill strips to tz-naive at live_trader.py:4491."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            if tz is not None:
                return _NOW_UTC.replace(tzinfo=timezone.utc).astimezone(tz)
            return _NOW_UTC

        @classmethod
        def utcnow(cls):  # noqa: D102
            return _NOW_UTC

    return patch.object(lt_module, "datetime", _Frozen)


# ===========================================================================
# Stub builders — only the seams _backfill_reconnect_gap_async /
# _on_bar_update_1h / _on_bar_update_5m read.
# ===========================================================================

def _backfill_and_bar_stub(bar_size):
    """LiveTrader stub carrying the seams for BOTH the reconnect backfill AND
    the live-stream bar handlers, so a single trader can be driven through the
    backfill and then the completed-bar handler (deliverable 2)."""
    trader = object.__new__(lt_module.LiveTrader)

    # backfill seams
    trader.data_client = MagicMock()
    trader.data_client.fetch_historical_bars_by_duration_async = AsyncMock()
    trader._telegram = MagicMock()
    trader._warmup_inference_state = MagicMock()
    trader.rolling_df_5m = pd.DataFrame()
    trader.rolling_df_1h = pd.DataFrame()
    trader.data_manager_5m = MagicMock()
    trader.data_manager_1h = MagicMock()

    # live-stream bar-handler seams
    trader.telemetry = MagicMock()
    trader._ledger_lock = threading.Lock()
    trader._check_trailing_stop = MagicMock()
    trader._on_new_bar = MagicMock()
    trader._fruitless_reconnect_count = 0

    trader._bar_size = bar_size
    trader._last_bar_time_5m = None
    trader._last_bar_time_1h = None
    return trader


def _make_chunk_df(index_times):
    """Build a fetch-shaped OHLCV DataFrame with a tz-naive DatetimeIndex —
    the exact shape fetch_historical_bars_by_duration_async returns
    (make_naive=True)."""
    idx = pd.DatetimeIndex([pd.Timestamp(t) for t in index_times], name="DateTime")
    n = len(idx)
    return pd.DataFrame(
        {
            "Open": [70.0 + i * 0.1 for i in range(n)],
            "High": [70.5 + i * 0.1 for i in range(n)],
            "Low": [69.5 + i * 0.1 for i in range(n)],
            "Close": [70.2 + i * 0.1 for i in range(n)],
            "Volume": [100.0 + i for i in range(n)],
        },
        index=idx,
    )


def _fake_bars(completed_time_naive_utc, step_minutes):
    """ib_insync-shaped bars list: [completed bar, new incomplete bar].
    _on_bar_update_1h / _on_bar_update_5m read bars[-2] as the just-closed
    bar (identical to tests/test_reconnect_recovery_fixes.py::_fake_bars)."""
    completed = SimpleNamespace(
        date=completed_time_naive_utc,
        open=70.0, high=70.5, low=69.5, close=70.2, volume=100,
    )
    incomplete = SimpleNamespace(
        date=completed_time_naive_utc + timedelta(minutes=step_minutes),
        open=70.2, high=70.3, low=70.1, close=70.25, volume=7,
    )
    return [completed, incomplete]


# Timestamps built relative to the frozen mid-hour `now` (16:17:00 UTC).
_T13 = pd.Timestamp("2026-07-20 13:00:00")
_T14 = pd.Timestamp("2026-07-20 14:00:00")
_T15 = pd.Timestamp("2026-07-20 15:00:00")   # last COMPLETED 1H bar (close 16:00 < now)
_T16 = pd.Timestamp("2026-07-20 16:00:00")   # IN-PROGRESS 1H bar (close 17:00 > now)

_M1600 = pd.Timestamp("2026-07-20 16:00:00")
_M1605 = pd.Timestamp("2026-07-20 16:05:00")
_M1610 = pd.Timestamp("2026-07-20 16:10:00")  # last COMPLETED 5M bar (close 16:15 < now)
_M1615 = pd.Timestamp("2026-07-20 16:15:00")  # IN-PROGRESS 5M bar (close 16:20 > now)


# ===========================================================================
# Deliverable 1 — 1H: the in-progress tail must NOT be stitched (regression).
# ===========================================================================

class TestBackfillDropsIncomplete1HBar:
    """RED on HEAD: the still-forming 16:00 bar (close 17:00 > now) is stitched
    into rolling_df_1h and advances _last_bar_time_1h to 16:00, because the
    stitch filter `chunk_df[chunk_df.index > _last_bar_time_1h]` only drops
    OLDER bars.  The fix must drop the incomplete tail (bar_end > now) from
    chunk_df before that filter."""

    def _run_backfill(self, trader):
        with _frozen_clock():
            asyncio.run(trader._backfill_reconnect_gap_async())

    def test_1h_inprogress_tail_not_stitched_and_last_bar_not_advanced(self):
        trader = _backfill_and_bar_stub(bar_size="1h")
        trader._last_bar_time_5m = None            # skip the 5M block entirely
        trader._last_bar_time_1h = _T13            # 197-min gap > 70 -> backfill fires
        # IBKR returns the full window INCLUDING the still-forming 16:00 bar
        # (empty endDateTime => currently-forming bar is the last row).
        trader.data_client.fetch_historical_bars_by_duration_async.return_value = (
            _make_chunk_df([_T13, _T14, _T15, _T16])
        )

        self._run_backfill(trader)

        # (a) the in-progress tail (16:00, close 17:00 > now=16:17) is NOT stitched
        assert _T16 not in trader.rolling_df_1h.index, (
            "REGRESSION: the still-forming 16:00 1H bar (close 17:00 > now) was "
            "stitched into rolling_df_1h — the reconnect backfill must drop the "
            "incomplete tail bar (bar_end > now) before the stitch filter"
        )
        assert trader._last_bar_time_1h != _T16, (
            "REGRESSION: _last_bar_time_1h advanced to the in-progress 16:00 "
            "label — the live stream's COMPLETED 16:00 bar will then be deduped "
            "away and that hour's inference is silently skipped"
        )
        assert trader._last_bar_time_1h == _T15, (
            "_last_bar_time_1h must end at the last COMPLETED bar (15:00), not "
            "the in-progress tail"
        )

        # (b) the genuinely-completed earlier bars ARE stitched (no gap)...
        assert _T14 in trader.rolling_df_1h.index
        assert _T15 in trader.rolling_df_1h.index
        # ...and exactly those two, no double-count of the already-held 13:00 anchor
        assert list(trader.rolling_df_1h.index) == [_T14, _T15], (
            "only the completed bars newer than the last-known bar must be "
            f"stitched; got {list(trader.rolling_df_1h.index)!r}"
        )


# ===========================================================================
# Deliverable 2 — 1H end-to-end: the COMPLETED same-timestamp bar then fires.
# ===========================================================================

class TestCompleted1HBarFiresAfterBackfill:
    """RED on HEAD: because the backfill advanced _last_bar_time_1h to the
    partial 16:00 label, the live-stream COMPLETED 16:00 bar hits the
    `bar_time <= _last_bar_time_1h: return` dedup and is dropped -> _on_new_bar
    never runs for 16:00 (the fleet-wide missed decision).  Once the fix stops
    the partial from advancing _last_bar_time_1h (stays 15:00), the completed
    16:00 bar sails past the dedup and fires NEW 1H BAR + inference."""

    def test_completed_1h_bar_not_deduped_runs_inference(self):
        trader = _backfill_and_bar_stub(bar_size="1h")
        trader._last_bar_time_5m = None
        trader._last_bar_time_1h = _T13
        trader.data_client.fetch_historical_bars_by_duration_async.return_value = (
            _make_chunk_df([_T13, _T14, _T15, _T16])
        )

        with _frozen_clock():
            asyncio.run(trader._backfill_reconnect_gap_async())

            # The live stream now delivers the COMPLETED 16:00 bar (bars[-2]).
            trader._on_bar_update_1h(
                _fake_bars(datetime(2026, 7, 20, 16, 0, 0), step_minutes=60),
                has_new_bar=True,
            )

        trader._on_new_bar.assert_called_once()
        fired_bar_time = trader._on_new_bar.call_args.args[0]
        assert fired_bar_time == _T16, (
            "the completed 16:00 1H bar must drive inference for the 16:00 "
            f"timestamp; _on_new_bar fired for {fired_bar_time!r}"
        )
        assert trader._last_bar_time_1h == _T16, (
            "after the completed 16:00 bar is processed, _last_bar_time_1h must "
            "advance to 16:00"
        )


# ===========================================================================
# Deliverable 3 — 5M twin: the in-progress tail must NOT be stitched.
# ===========================================================================

class TestBackfillDropsIncomplete5MBar:
    """RED on HEAD: identical latent bug on the 5M stitch block — the
    still-forming 16:15 bar (close 16:20 > now=16:17) is stitched and advances
    _last_bar_time_5m to 16:15."""

    def test_5m_inprogress_tail_not_stitched_and_last_bar_not_advanced(self):
        trader = _backfill_and_bar_stub(bar_size="5m")
        trader._last_bar_time_1h = None            # 1H block guarded off by bar_size
        trader._last_bar_time_5m = _M1600          # 17-min gap > 10 -> backfill fires
        trader.data_client.fetch_historical_bars_by_duration_async.return_value = (
            _make_chunk_df([_M1600, _M1605, _M1610, _M1615])
        )

        with _frozen_clock():
            asyncio.run(trader._backfill_reconnect_gap_async())

        assert _M1615 not in trader.rolling_df_5m.index, (
            "REGRESSION: the still-forming 16:15 5M bar (close 16:20 > now) was "
            "stitched into rolling_df_5m — the 5M backfill block must drop the "
            "incomplete tail bar before the stitch filter"
        )
        assert trader._last_bar_time_5m != _M1615, (
            "REGRESSION: _last_bar_time_5m advanced to the in-progress 16:15 "
            "label"
        )
        assert trader._last_bar_time_5m == _M1610, (
            "_last_bar_time_5m must end at the last COMPLETED 5M bar (16:10)"
        )

        assert _M1605 in trader.rolling_df_5m.index
        assert _M1610 in trader.rolling_df_5m.index
        assert list(trader.rolling_df_5m.index) == [_M1605, _M1610], (
            "only the completed 5M bars newer than the last-known bar must be "
            f"stitched; got {list(trader.rolling_df_5m.index)!r}"
        )
