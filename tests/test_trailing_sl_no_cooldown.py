"""Ticket trailing-sl-no-cooldown_07222026_2050.

Operator decision 2026-07-22: the post-exit re-entry cooldown arms ONLY on an
ORIGINAL stop-loss exit. Trailing-stop exits (profit locking, TRAILING_BE),
TP, time-barrier, signal/flatten, OOB/unknown closes do NOT arm it.

Pins the single shared predicate (``exit_reason_arms_cooldown``) and every
seam that consumes it:
- backtest engine ``last_exit_bars_ago_*`` resets (via full-engine scenarios),
- ConfigurableStrategy.on_exit (live gate arming; forwarding stays universal),
- LiveTrader._reset_position_state (trailed SL_HIT maps to TRAILING_BE),
- LiveTrader._seed_restart_cooldown / _reconstruct_cooldown_from_ledger
  (restart recovery honors the rule; ledger rows carry trailing_activated).

Re-adjudicated by this ticket: test_backtest_engine.py's TP and TIME_BARRIER
cooldown pins (flavor-blind arming was the OLD rule).
"""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from agent.backtest_engine import ExitReason
from src.live_execution.live_trader import LiveTrader
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy
from src.live_execution.strategies.execution_models import (
    Order,
    exit_reason_arms_cooldown,
)

from tests.test_backtest_engine import (
    _bt_with_strategy,
    _cd_cfg,
    _make_ohlcv,
    _prob_buy_signals,
)


BUY_PROB = 0.90
SELL_PROB = 0.80

_NOW = pd.Timestamp("2026-07-22 08:00:00")


# ---------------------------------------------------------------------------
# The shared predicate
# ---------------------------------------------------------------------------


class TestExitReasonArmsCooldown:
    @pytest.mark.parametrize("reason", [
        ExitReason.SL, "SL", "SL_HIT", "SL_HIT_OOB",
    ])
    def test_original_sl_family_arms(self, reason):
        assert exit_reason_arms_cooldown(reason) is True

    @pytest.mark.parametrize("reason", [
        ExitReason.TRAILING_BE, "TRAILING_BE",
        ExitReason.TP, "TP", "TP_HIT", "TP_HIT_OOB",
        ExitReason.TIME_BARRIER, "TIME_BARRIER",
        ExitReason.SIGNAL_EXIT, "SIGNAL_EXIT",
        ExitReason.EOD_FLATTEN, ExitReason.WEEKEND_FLATTEN,
        "CLOSED_OOB", "KILL_SWITCH", None, 42,
    ])
    def test_everything_else_does_not_arm(self, reason):
        assert exit_reason_arms_cooldown(reason) is False


# ---------------------------------------------------------------------------
# ConfigurableStrategy.on_exit — arming filtered, forwarding universal
# ---------------------------------------------------------------------------


def _strategy() -> ConfigurableStrategy:
    s = object.__new__(ConfigurableStrategy)
    s.config = {}
    s._last_exit_bars_ago_long = 9999
    s._last_exit_bars_ago_short = 9999
    s._exec_strategy = MagicMock()
    return s


class TestConfigurableStrategyOnExit:
    def test_sl_hit_arms(self):
        s = _strategy()
        s.on_exit(-1, "SL_HIT", 5)
        assert s._last_exit_bars_ago_short == -1
        assert s._last_exit_bars_ago_long == 9999

    @pytest.mark.parametrize("reason", [
        "TRAILING_BE", "TP_HIT", "TIME_BARRIER", "CLOSED_OOB", None,
    ])
    def test_exempt_reasons_do_not_arm(self, reason):
        s = _strategy()
        s.on_exit(-1, reason, 5)
        assert s._last_exit_bars_ago_short == 9999
        assert s._last_exit_bars_ago_long == 9999

    @pytest.mark.parametrize("reason", ["SL_HIT", "TRAILING_BE", "TP_HIT"])
    def test_truthful_reason_always_forwarded_to_exec_strategy(self, reason):
        # Per-side open/close tracking in the execution strategy must keep
        # seeing EVERY exit — only the cooldown counter is filtered.
        s = _strategy()
        s.on_exit(1, reason, 7)
        s._exec_strategy.on_exit.assert_called_once_with(1, reason, 7)


# ---------------------------------------------------------------------------
# LiveTrader._reset_position_state — trailed SL maps to TRAILING_BE
# ---------------------------------------------------------------------------


def _lt_for_reset(strategy, *, side=-1, trailing_activated=False) -> LiveTrader:
    lt = object.__new__(LiveTrader)
    lt.strategy = strategy
    lt._position_side = side
    lt._position_bars_held = 9
    lt._trailing_activated = trailing_activated
    lt._tp_order_ids = []
    lt._sl_order_id = None
    lt._active_trade_id = None
    return lt


class TestResetPositionStateReasonMapping:
    def test_untrailed_sl_arms_cooldown(self):
        s = _strategy()
        lt = _lt_for_reset(s, trailing_activated=False)
        lt._reset_position_state("SL_HIT")
        assert s._last_exit_bars_ago_short == -1
        s._exec_strategy.on_exit.assert_called_once_with(-1, "SL_HIT", 9)

    def test_trailed_sl_maps_to_trailing_be_and_does_not_arm(self):
        s = _strategy()
        lt = _lt_for_reset(s, trailing_activated=True)
        lt._reset_position_state("SL_HIT")
        assert s._last_exit_bars_ago_short == 9999, (
            "a trailing-stop exit (SL order after the trail activated) must "
            "NOT arm the re-entry cooldown"
        )
        # the mapped reason is what flows onward
        s._exec_strategy.on_exit.assert_called_once_with(-1, "TRAILING_BE", 9)

    def test_trailed_tp_stays_tp(self):
        # trailing flag only reinterprets SL-family fills; a TP fill while
        # the trail happened to be active is still a TP.
        s = _strategy()
        lt = _lt_for_reset(s, trailing_activated=True)
        lt._reset_position_state("TP_HIT")
        assert s._last_exit_bars_ago_short == 9999
        s._exec_strategy.on_exit.assert_called_once_with(-1, "TP_HIT", 9)


# ---------------------------------------------------------------------------
# Restart recovery honors the rule
# ---------------------------------------------------------------------------


def _trader(strategy, *, bar_size="1h", last_bar_time=_NOW) -> LiveTrader:
    lt = object.__new__(LiveTrader)
    lt.strategy = strategy
    lt._bar_size = bar_size
    lt._position_bars_held = 0
    if last_bar_time is not None:
        idx = pd.date_range(end=last_bar_time, periods=168, freq="h")
        lt.rolling_df_1h = pd.DataFrame({"Close": [1.0] * len(idx)}, index=idx)
    else:
        lt.rolling_df_1h = None
    lt.rolling_df_5m = None
    lt.telemetry = MagicMock()
    return lt


def _closed(side, reason, hours_ago, trailing_activated=0):
    return {
        "side": side, "close_reason": reason,
        "close_time": (_NOW - pd.Timedelta(hours=hours_ago)).isoformat(),
        "trailing_activated": trailing_activated,
    }


class TestSeedRestartCooldownFilter:
    @pytest.mark.parametrize("reason", ["TRAILING_BE", "TP_HIT", "TP_HIT_OOB", "CLOSED_OOB"])
    def test_exempt_reason_stays_inert(self, reason):
        s = _strategy()
        lt = _trader(s)
        lt._seed_restart_cooldown(-1, reason, close_time=None)
        assert s._last_exit_bars_ago_short == 9999
        s._exec_strategy.on_exit.assert_not_called()

    def test_sl_oob_still_arms_full_window(self):
        s = _strategy()
        lt = _trader(s)
        lt._seed_restart_cooldown(-1, "SL_HIT_OOB", close_time=None)
        assert s._last_exit_bars_ago_short == -1


class TestReconstructionSkipsTrailedRows:
    def test_trailed_sl_row_not_armed(self):
        s = _strategy()
        lt = _trader(s, bar_size="1h")
        lt.telemetry.get_recent_closed_positions.return_value = [
            _closed("SHORT", "SL_HIT", hours_ago=2, trailing_activated=1),
        ]
        lt._reconstruct_cooldown_from_ledger()
        assert s._last_exit_bars_ago_short == 9999

    def test_untrailed_sl_row_still_armed(self):
        s = _strategy()
        lt = _trader(s, bar_size="1h")
        lt.telemetry.get_recent_closed_positions.return_value = [
            _closed("SHORT", "SL_HIT", hours_ago=2, trailing_activated=0),
        ]
        lt._reconstruct_cooldown_from_ledger()
        assert s._last_exit_bars_ago_short == 1

    def test_row_without_trailing_column_still_armed(self):
        # Legacy rows predating the column: absent -> treat as untrailed
        # (errs toward blocking, the pre-ticket behavior).
        s = _strategy()
        lt = _trader(s, bar_size="1h")
        row = _closed("SHORT", "SL_HIT", hours_ago=2)
        del row["trailing_activated"]
        lt.telemetry.get_recent_closed_positions.return_value = [row]
        lt._reconstruct_cooldown_from_ledger()
        assert s._last_exit_bars_ago_short == 1


# ---------------------------------------------------------------------------
# Backtest engine — trailing exit does not block re-entry
# ---------------------------------------------------------------------------


class TestEngineTrailingExitNoCooldown:
    def test_trailing_exit_allows_immediate_reentry(self):
        """Trade 1 exits via the trailed stop (TRAILING_BE) at bar 30; the
        bar-32 signal is INSIDE the 5-bar window and must now enter (old
        flavor-blind rule blocked it until bar 36)."""
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        opens = [65.0] * n

        # Entry bar 20 @65.0, ATR ~= 0.02: TP 65.04, trail trigger 65.02.
        # Bars 23-29 elevated -> trail activates (65.03 >= 65.02, below TP).
        for i in range(23, 30):
            opens[i] = 65.02
            prices[i] = 65.02
            highs[i] = 65.03
            lows[i] = 65.015
        # Bar 30 dips through the trailed stop -> TRAILING_BE exit.
        opens[30] = 65.015
        prices[30] = 65.005
        highs[30] = 65.02
        lows[30] = 65.005
        # Trade 2 (entry bar 32 @~65.0) closes via original SL at bar 36.
        lows[36] = 64.90

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows, opens=opens)
        signals = _prob_buy_signals(ohlcv, [20, 32])

        cfg = _cd_cfg()
        cfg["trailing_atr_mult"] = 1.0
        cfg["trailing_sl_atr_offset"] = 0.5
        result = _bt_with_strategy(cfg).run(signals, ohlcv)

        assert result.trade_count == 2
        assert result.trades[0].exit_reason == ExitReason.TRAILING_BE, (
            f"scenario must produce a trailing exit, got "
            f"{result.trades[0].exit_reason}"
        )
        assert result.trades[1].entry_dt == ohlcv.index[32], (
            f"TRAILING_BE must NOT arm the cooldown; bar-32 entry expected, "
            f"got {result.trades[1].entry_dt}"
        )
        assert result.trades[1].exit_reason == ExitReason.SL

    def test_original_sl_still_blocks(self):
        """Control: original SL still arms the window (unchanged behavior)."""
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        lows[25] = 64.97
        lows[38] = 64.95
        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _prob_buy_signals(ohlcv, [20, 28, 35])

        result = _bt_with_strategy(_cd_cfg()).run(signals, ohlcv)

        assert result.trade_count == 2
        assert result.trades[0].exit_reason == ExitReason.SL
        assert result.trades[1].entry_dt == ohlcv.index[35]
