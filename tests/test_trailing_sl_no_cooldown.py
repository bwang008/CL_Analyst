"""Ticket trailing-sl-no-cooldown_07222026_2050 (opt-in revision).

Operator decision 2026-07-22 (post decision-gate study): which exits arm the
post-exit re-entry cooldown is a PER-SIDE config choice ``cooldown_arming``:
"all" (every exit arms — pre-ticket flavor-blind behavior, the DEFAULT) or
"sl_only" (only an original stop-loss arms; trailing/TP/barrier/flatten/OOB
exits never block re-entry). Optuna searches it as the bool dim
``cooldown_sl_only`` (ladder_enabled pattern).

Pins the shared predicate + resolver and every consuming seam under
"sl_only"; the "all" default is pinned by the restored pre-ticket suites
(test_backtest_engine, test_exit_bar_semantics, test_exit_reason_and_fill_
routing, test_restart_cooldown_recovery, test_weekend_flatten, ...).
"""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from agent.backtest_engine import BacktestEngine, ExitReason
from src.live_execution.live_trader import LiveTrader
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy
from src.live_execution.strategies.execution_models import (
    exit_reason_arms_cooldown,
    resolve_cooldown_arming,
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

SL_ONLY_CFG = {
    "long": {"cooldown_bars": 1, "cooldown_arming": "sl_only"},
    "short": {"cooldown_bars": 11, "cooldown_arming": "sl_only"},
}


# ---------------------------------------------------------------------------
# The shared predicate + resolver
# ---------------------------------------------------------------------------


class TestExitReasonArmsCooldown:
    @pytest.mark.parametrize("reason", [
        ExitReason.SL, "SL", "SL_HIT", "SL_HIT_OOB",
    ])
    def test_sl_family_arms_in_both_modes(self, reason):
        assert exit_reason_arms_cooldown(reason, "sl_only") is True
        assert exit_reason_arms_cooldown(reason, "all") is True

    @pytest.mark.parametrize("reason", [
        ExitReason.TRAILING_BE, "TRAILING_BE",
        ExitReason.TP, "TP", "TP_HIT", "TP_HIT_OOB",
        ExitReason.TIME_BARRIER, "TIME_BARRIER",
        ExitReason.SIGNAL_EXIT, "SIGNAL_EXIT",
        ExitReason.EOD_FLATTEN, ExitReason.WEEKEND_FLATTEN,
        "CLOSED_OOB", "KILL_SWITCH",
    ])
    def test_non_sl_reasons_arm_only_under_all(self, reason):
        assert exit_reason_arms_cooldown(reason, "sl_only") is False
        assert exit_reason_arms_cooldown(reason, "all") is True

    @pytest.mark.parametrize("reason", [None, 42])
    def test_none_and_unknown_never_arm(self, reason):
        assert exit_reason_arms_cooldown(reason, "sl_only") is False
        assert exit_reason_arms_cooldown(reason, "all") is False

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            exit_reason_arms_cooldown("SL_HIT", "sometimes")


class TestResolveCooldownArming:
    def test_absent_defaults_to_all(self):
        assert resolve_cooldown_arming({}, "long") == "all"
        assert resolve_cooldown_arming({"long": {}}, "long") == "all"

    def test_side_beats_top_level(self):
        cfg = {"cooldown_arming": "all",
               "short": {"cooldown_arming": "sl_only"}}
        assert resolve_cooldown_arming(cfg, "short") == "sl_only"
        assert resolve_cooldown_arming(cfg, "long") == "all"

    def test_top_level_fallback(self):
        cfg = {"cooldown_arming": "sl_only", "long": {}}
        assert resolve_cooldown_arming(cfg, "long") == "sl_only"

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            resolve_cooldown_arming({"long": {"cooldown_arming": "never"}},
                                    "long")


# ---------------------------------------------------------------------------
# ConfigurableStrategy.on_exit under sl_only
# ---------------------------------------------------------------------------


def _strategy(config=None) -> ConfigurableStrategy:
    s = object.__new__(ConfigurableStrategy)
    s.config = dict(SL_ONLY_CFG) if config is None else config
    s._last_exit_bars_ago_long = 9999
    s._last_exit_bars_ago_short = 9999
    s._exec_strategy = MagicMock()
    return s


class TestConfigurableStrategyOnExitSlOnly:
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

    @pytest.mark.parametrize("reason", ["SL_HIT", "TRAILING_BE", "TP_HIT"])
    def test_truthful_reason_always_forwarded_to_exec_strategy(self, reason):
        s = _strategy()
        s.on_exit(1, reason, 7)
        s._exec_strategy.on_exit.assert_called_once_with(1, reason, 7)

    def test_per_side_modes_are_independent(self):
        # long=all, short=sl_only: a TP close arms long but not short.
        cfg = {"long": {"cooldown_bars": 2, "cooldown_arming": "all"},
               "short": {"cooldown_bars": 2, "cooldown_arming": "sl_only"}}
        s = _strategy(cfg)
        s.on_exit(1, "TP_HIT", 5)
        assert s._last_exit_bars_ago_long == -1
        s2 = _strategy(cfg)
        s2.on_exit(-1, "TP_HIT", 5)
        assert s2._last_exit_bars_ago_short == 9999


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
            "under sl_only, a trailing-stop exit (SL order after the trail "
            "activated) must NOT arm the re-entry cooldown"
        )
        s._exec_strategy.on_exit.assert_called_once_with(-1, "TRAILING_BE", 9)

    def test_trailed_sl_under_all_mode_still_arms(self):
        # The remap is mode-independent; under "all" the TRAILING_BE reason
        # still arms — byte-compatible with pre-ticket behavior.
        s = _strategy({"short": {"cooldown_bars": 11}})
        lt = _lt_for_reset(s, trailing_activated=True)
        lt._reset_position_state("SL_HIT")
        assert s._last_exit_bars_ago_short == -1
        s._exec_strategy.on_exit.assert_called_once_with(-1, "TRAILING_BE", 9)

    def test_trailed_tp_stays_tp(self):
        s = _strategy()
        lt = _lt_for_reset(s, trailing_activated=True)
        lt._reset_position_state("TP_HIT")
        assert s._last_exit_bars_ago_short == 9999
        s._exec_strategy.on_exit.assert_called_once_with(-1, "TP_HIT", 9)


# ---------------------------------------------------------------------------
# Restart recovery honors the side's mode
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


class TestSeedRestartCooldownSlOnly:
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


class TestReconstructionHonorsMode:
    def test_trailed_sl_row_not_armed_under_sl_only(self):
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

    def test_trailed_sl_row_still_arms_under_all_mode(self):
        s = _strategy({"short": {"cooldown_bars": 11}})
        lt = _trader(s, bar_size="1h")
        lt.telemetry.get_recent_closed_positions.return_value = [
            _closed("SHORT", "SL_HIT", hours_ago=2, trailing_activated=1),
        ]
        lt._reconstruct_cooldown_from_ledger()
        assert s._last_exit_bars_ago_short == 1


# ---------------------------------------------------------------------------
# Optimizer param routing (ladder_enabled pattern)
# ---------------------------------------------------------------------------


class TestApplyTrialParamsCooldownArming:
    def _tiered(self, cfg):
        from src.live_execution.strategies.execution_models import (
            TieredEnsembleStrategy,
        )
        return TieredEnsembleStrategy({
            "nickname": "t", "conflict_resolution": "hold",
            "long": {"tiers": [{"min_prob": 0.6, "lots": 1}]},
            "short": {"tiers": [{"min_prob": 0.6, "lots": 1}]},
            **cfg,
        })

    def test_bool_true_writes_sl_only(self):
        strat = self._tiered({})
        cfg = {"long": {}, "short": {}}
        strat.apply_trial_params(cfg, {"cooldown_sl_only": True}, side="long")
        assert cfg["long"]["cooldown_arming"] == "sl_only"
        assert "cooldown_arming" not in cfg["short"]

    def test_bool_false_writes_all(self):
        strat = self._tiered({})
        cfg = {"long": {}, "short": {}}
        strat.apply_trial_params(cfg, {"cooldown_sl_only": False}, side="short")
        assert cfg["short"]["cooldown_arming"] == "all"


# ---------------------------------------------------------------------------
# Backtest engine — per-side mode at the reset sites
# ---------------------------------------------------------------------------


class TestEngineSlOnlyMode:
    def test_trailing_exit_allows_immediate_reentry_under_sl_only(self):
        """Trade 1 exits via the trailed stop (TRAILING_BE) at bar 30; with
        long cooldown_arming=sl_only the bar-32 signal (inside the 5-bar
        window) must enter. Under the default "all" it would be blocked —
        pinned by the restored pre-ticket suites."""
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        opens = [65.0] * n
        for i in range(23, 30):
            opens[i] = 65.02
            prices[i] = 65.02
            highs[i] = 65.03
            lows[i] = 65.015
        opens[30] = 65.015
        prices[30] = 65.005
        highs[30] = 65.02
        lows[30] = 65.005
        lows[36] = 64.90  # trade 2 closes via original SL (records it)

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows, opens=opens)
        signals = _prob_buy_signals(ohlcv, [20, 32])

        cfg = _cd_cfg()
        cfg["long"]["cooldown_arming"] = "sl_only"
        cfg["trailing_atr_mult"] = 1.0
        cfg["trailing_sl_atr_offset"] = 0.5
        result = _bt_with_strategy(cfg).run(signals, ohlcv)

        assert result.trade_count == 2
        assert result.trades[0].exit_reason == ExitReason.TRAILING_BE, (
            f"scenario must produce a trailing exit, got "
            f"{result.trades[0].exit_reason}"
        )
        assert result.trades[1].entry_dt == ohlcv.index[32], (
            f"sl_only: TRAILING_BE must NOT arm the cooldown; bar-32 entry "
            f"expected, got {result.trades[1].entry_dt}"
        )
        assert result.trades[1].exit_reason == ExitReason.SL

    def test_original_sl_still_blocks_under_sl_only(self):
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        lows[25] = 64.97
        lows[38] = 64.95
        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _prob_buy_signals(ohlcv, [20, 28, 35])

        cfg = _cd_cfg()
        cfg["long"]["cooldown_arming"] = "sl_only"
        cfg["short"]["cooldown_arming"] = "sl_only"
        result = _bt_with_strategy(cfg).run(signals, ohlcv)

        assert result.trade_count == 2
        assert result.trades[0].exit_reason == ExitReason.SL
        assert result.trades[1].entry_dt == ohlcv.index[35]

    def test_invalid_mode_raises_at_construction(self):
        with pytest.raises(ValueError):
            BacktestEngine(cooldown_arming_long="sometimes")
