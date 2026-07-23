"""Ticket cooldown-not-restored-on-restart_07082026_0230.

After a process restart the strategy's post-exit re-entry cooldown was silently
dropped: the startup OOB-recovery branch never armed it, and a clean restart
never reconstructed it from the ledger. Result: a model re-entered the SAME side
it was just stopped out of on the next bar, ignoring `sl_cooldown_bars`.

These tests pin the two LiveTrader seams that fix it — `_seed_restart_cooldown`
(Part 1) and `_reconstruct_cooldown_from_ledger` (Part 2) — by asserting they set
the ConfigurableStrategy gate state that the (already-tested) single-authority
cooldown gate then acts on.

re-adjudicated: cooldown-single-authority-wiring_07222026_1051 —
(1) the seams reach the strategy via the REAL attribute ``lt.strategy`` (the
    phantom ``_strategy`` alias masked a production no-op; a missing strategy
    now crashes loudly instead of silently skipping);
(2) the gate is flavor-blind per-side cooldown_bars, so the strategy no longer
    records ``_last_exit_reason_*`` — the counter IS the armed state; the
    truthful reason still flows through on_exit to the execution strategy.
"""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution.live_trader import LiveTrader
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy

_NOW = pd.Timestamp("2026-07-08 08:00:00")


def _strategy() -> ConfigurableStrategy:
    """Real ConfigurableStrategy with only the cooldown gate state (no disk/models)."""
    s = object.__new__(ConfigurableStrategy)
    s.config = {}
    s._last_exit_bars_ago_long = 9999
    s._last_exit_bars_ago_short = 9999
    s._exec_strategy = MagicMock()  # on_exit forwards the truthful reason here
    return s


def _trader(strategy, *, bar_size="1h", last_bar_time=_NOW):
    lt = object.__new__(LiveTrader)
    lt.strategy = strategy
    lt._bar_size = bar_size
    lt._position_bars_held = 0
    if last_bar_time is not None:
        # Contiguous hourly brain frame ending at last_bar_time (168 bars):
        # bars_elapsed is now COUNTED from this frame (gap-immune _bars_since,
        # ticket recovery-barsheld-wallclock_07092026_1239), no longer derived
        # from wall-clock division.
        idx = pd.date_range(end=last_bar_time, periods=168, freq="h")
        lt.rolling_df_1h = pd.DataFrame({"Close": [1.0] * len(idx)}, index=idx)
    else:
        lt.rolling_df_1h = None
    lt.rolling_df_5m = None
    lt.telemetry = MagicMock()
    return lt


def _closed(side, reason, hours_ago):
    return {
        "side": side, "close_reason": reason,
        "close_time": (_NOW - pd.Timedelta(hours=hours_ago)).isoformat(),
    }


# ── Part 1: startup OOB recovery arms the full window ─────────────────────────

class TestSeedRestartCooldown:
    def test_oob_close_now_arms_full_window_on_ledger_side(self):
        s = _strategy()
        lt = _trader(s)
        # close_time=None → "just exited" → full window (bars_ago == -1).
        lt._seed_restart_cooldown(-1, "SL_HIT_OOB", close_time=None)
        assert s._last_exit_bars_ago_short == -1
        # truthful reason forwarded to the execution strategy
        s._exec_strategy.on_exit.assert_called_once_with(-1, "SL_HIT_OOB", 0)
        # the other side is untouched
        assert s._last_exit_bars_ago_long == 9999

    def test_historical_exit_seeds_honest_bars_ago(self):
        s = _strategy()
        lt = _trader(s, bar_size="1h", last_bar_time=_NOW)
        # exit 2 bars ago → bars_elapsed=2 → seed bars_ago = 1 (gate pre-inc → 2)
        lt._seed_restart_cooldown(
            1, "SL_HIT", close_time=(_NOW - pd.Timedelta(hours=2)).isoformat(),
        )
        assert s._last_exit_bars_ago_long == 1

    def test_no_bar_time_stays_inert(self):
        # guard (a): both rolling frames None + a historical exit → cannot
        # measure staleness → do NOT arm (never over-block).
        s = _strategy()
        lt = _trader(s, last_bar_time=None)
        lt._seed_restart_cooldown(
            1, "SL_HIT", close_time=(_NOW - pd.Timedelta(hours=2)).isoformat(),
        )
        assert s._last_exit_bars_ago_long == 9999
        s._exec_strategy.on_exit.assert_not_called()

    def test_unknown_bar_size_stays_inert_does_not_raise(self):
        # guard (b), re-pinned per reviewer C1 (recovery-barsheld-wallclock
        # _07092026_1239): an unsupported bar size cannot be counted honestly
        # (2h/4h brains are resampled from 1h rows) → stay INERT, never guess
        # via a wall-clock fallback, never raise.
        s = _strategy()
        lt = _trader(s, bar_size="7m", last_bar_time=_NOW)
        lt._seed_restart_cooldown(
            1, "SL_HIT", close_time=(_NOW - pd.Timedelta(minutes=30)).isoformat(),
        )
        assert s._last_exit_bars_ago_long == 9999
        s._exec_strategy.on_exit.assert_not_called()

    def test_missing_strategy_crashes_loudly(self):
        # re-adjudicated: cooldown-single-authority-wiring_07222026_1051 —
        # the old guard silently no-opped on a missing/None strategy, which is
        # exactly how the phantom-attribute bug hid in production. A trader
        # without a strategy is a programming error: crash, don't skip.
        lt = object.__new__(LiveTrader)
        lt.strategy = None
        lt._bar_size = "1h"
        lt.rolling_df_5m = None
        lt.rolling_df_1h = None
        with pytest.raises(AttributeError):
            lt._seed_restart_cooldown(-1, "SL_HIT_OOB", close_time=None)

    def test_none_reason_not_armed(self):
        s = _strategy()
        lt = _trader(s)
        lt._seed_restart_cooldown(1, None, close_time=None)
        assert s._last_exit_bars_ago_long == 9999


# ── Part 2: clean-restart ledger reconstruction ──────────────────────────────

class TestReconstructCooldownFromLedger:
    def test_recent_sl_row_reconstructed(self):
        s = _strategy()
        lt = _trader(s, bar_size="1h")
        lt.telemetry.get_recent_closed_positions.return_value = [
            _closed("LONG", "SL_HIT", hours_ago=2),
        ]
        lt._reconstruct_cooldown_from_ledger()
        assert s._last_exit_bars_ago_long == 1  # 2 bars elapsed - 1
        s._exec_strategy.on_exit.assert_called_once_with(1, "SL_HIT", 0)

    def test_aged_out_row_is_inert(self):
        s = _strategy()
        lt = _trader(s, bar_size="1h")
        lt.telemetry.get_recent_closed_positions.return_value = [
            _closed("LONG", "SL_HIT", hours_ago=100),
        ]
        lt._reconstruct_cooldown_from_ledger()
        # armed but far beyond any cooldown → gate stays inert (bars_ago >> 7)
        assert s._last_exit_bars_ago_long == 99

    def test_each_side_from_its_own_most_recent_row(self):
        # guard (d): per-side, not rows[0]-only.
        # re-adjudicated: trailing-sl-no-cooldown_07222026_2050 — both rows
        # are SL closes now (a TIME_BARRIER row would correctly stay inert
        # under the only-original-SL rule and no longer probes per-side
        # selection).
        s = _strategy()
        lt = _trader(s, bar_size="1h")
        lt.telemetry.get_recent_closed_positions.return_value = [
            _closed("LONG", "SL_HIT", hours_ago=1),      # most recent overall
            _closed("SHORT", "SL_HIT_OOB", hours_ago=3),
        ]
        lt._reconstruct_cooldown_from_ledger()
        assert s._last_exit_bars_ago_long == 0            # 1 - 1
        assert s._last_exit_bars_ago_short == 2           # 3 - 1

    def test_only_most_recent_per_side_used(self):
        s = _strategy()
        lt = _trader(s, bar_size="1h")
        lt.telemetry.get_recent_closed_positions.return_value = [
            _closed("LONG", "SL_HIT", hours_ago=1),   # most recent LONG → used
            _closed("LONG", "TP_HIT", hours_ago=5),   # older LONG → ignored
        ]
        lt._reconstruct_cooldown_from_ledger()
        assert s._last_exit_bars_ago_long == 0

    def test_most_recent_side_reason_none_not_backfilled_from_older(self):
        s = _strategy()
        lt = _trader(s, bar_size="1h")
        lt.telemetry.get_recent_closed_positions.return_value = [
            _closed("LONG", None, hours_ago=1),       # most recent LONG, malformed
            _closed("LONG", "SL_HIT", hours_ago=3),   # older → must NOT be used
        ]
        lt._reconstruct_cooldown_from_ledger()
        assert s._last_exit_bars_ago_long == 9999
        s._exec_strategy.on_exit.assert_not_called()

    def test_empty_ledger_is_noop(self):
        s = _strategy()
        lt = _trader(s, bar_size="1h")
        lt.telemetry.get_recent_closed_positions.return_value = []
        lt._reconstruct_cooldown_from_ledger()
        assert s._last_exit_bars_ago_long == 9999
        assert s._last_exit_bars_ago_short == 9999

    def test_ledger_query_failure_never_blocks_startup(self):
        s = _strategy()
        lt = _trader(s, bar_size="1h")
        lt.telemetry.get_recent_closed_positions.side_effect = RuntimeError("db locked")
        lt._reconstruct_cooldown_from_ledger()  # must not raise
        assert s._last_exit_bars_ago_long == 9999
