"""Trailing-stop ladder tests.

Ticket ID: trailing-stop-ladder_07132026_1745
Blueprint: .agents/collab/tickets/trailing-stop-ladder_07132026_1745/blueprint.md

Covers:
- byte-parity: a 1-rung ladder behaves identically to the legacy scalar
  trailing params (single-position AND concurrent paths)
- 2-rung mechanics: second rung ratchets the stop higher; both rungs can
  advance within a single bar; a moved stop is only effective NEXT bar
- short-side mirror
- StrategyConfig `trailing_ladder` parsing + validation crashes
- optimizer derivation: `ladder_enabled` -> fixed geometric rung 2
  (trigger2 = a1 + 0.5*(TP - a1), lock2 = a1) and apply_trial_params routing
"""

from __future__ import annotations

import pandas as pd
import pytest

from agent.backtest_engine import BacktestEngine, ExitReason
from agent.strategy_optimizer import _PARAM_RANGES, _derive_trailing_params
from src.live_execution.strategy_config import StrategyConfig
from src.live_execution.strategies.execution_models import TieredEnsembleStrategy


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_backtest_engine.py conventions)
# ---------------------------------------------------------------------------


def _make_ohlcv(
    n: int,
    *,
    prices: list[float],
    highs: list[float],
    lows: list[float],
    opens: list[float] | None = None,
) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="5min")
    return pd.DataFrame(
        {
            "Open": opens if opens else prices,
            "High": highs,
            "Low": lows,
            "Close": prices,
            "Volume": [100.0] * n,
        },
        index=idx,
    )


def _make_signal(ohlcv: pd.DataFrame, bar_idx: int, side: int = 1) -> pd.DataFrame:
    return pd.DataFrame({"side": [side]}, index=[ohlcv.index[bar_idx]])


def _bt(**kwargs) -> BacktestEngine:
    defaults = {
        "tp_atr_mult": 4.0,
        "sl_atr_mult": 1.0,
        "max_horizon": 288,
        "trailing_atr_mult": 1.0,
        "trailing_sl_atr_offset": 0.5,
        "atr_period": 14,
        "commission_per_side": 0.0,
        "slippage_per_side": 0.0,
        "contract_multiplier": 1000.0,
    }
    defaults.update(kwargs)
    return BacktestEngine(**defaults)


def _flat_path(n: int = 40) -> tuple[list, list, list, list]:
    """Constant path: ATR stabilizes at 0.02 by the bar-20 entry."""
    return (
        [65.0] * n,      # closes
        [65.01] * n,     # highs
        [64.99] * n,     # lows
        [65.0] * n,      # opens
    )


# Ladder used in most engine tests. Entry 65.0, ATR-at-entry 0.02:
#   rung1: trigger 65.02, lock 65.01
#   rung2: trigger 65.04, lock 65.02
#   TP (4.0):     65.08
LADDER = ((1.0, 0.5), (2.0, 1.0))


def _rung2_path() -> pd.DataFrame:
    """Cross rung1 at bar 23, rung2 at bar 25, stop out at bar 30."""
    prices, highs, lows, opens = _flat_path()
    for i in (23, 24):
        opens[i] = 65.02
        prices[i] = 65.02
        highs[i] = 65.03    # >= rung1 trigger 65.02, < rung2 trigger 65.04
        lows[i] = 65.015    # > rung1 lock 65.01
    opens[25] = 65.03
    prices[25] = 65.04
    highs[25] = 65.045      # >= rung2 trigger 65.04, < TP 65.08
    lows[25] = 65.025
    for i in range(26, 30):
        opens[i] = 65.03
        prices[i] = 65.03
        highs[i] = 65.035
        lows[i] = 65.025    # > rung2 lock 65.02
    opens[30] = 65.025
    prices[30] = 65.0
    highs[30] = 65.03
    lows[30] = 65.005       # breaches rung2 lock 65.02
    return _make_ohlcv(40, prices=prices, highs=highs, lows=lows, opens=opens)


# ---------------------------------------------------------------------------
# Byte-parity: 1-rung ladder == legacy scalars
# ---------------------------------------------------------------------------


class TestOneRungParity:
    def _parity_path(self) -> pd.DataFrame:
        prices, highs, lows, opens = _flat_path()
        for i in range(23, 30):
            opens[i] = 65.02
            prices[i] = 65.02
            highs[i] = 65.03
            lows[i] = 65.015
        opens[30] = 65.015
        prices[30] = 65.005
        highs[30] = 65.02
        lows[30] = 65.005
        return _make_ohlcv(40, prices=prices, highs=highs, lows=lows, opens=opens)

    def test_single_position_parity(self) -> None:
        ohlcv = self._parity_path()
        signals = _make_signal(ohlcv, bar_idx=20)

        legacy = _bt().run(signals, ohlcv).to_dataframe()
        ladder = _bt(trailing_ladder_long=((1.0, 0.5),)).run(signals, ohlcv).to_dataframe()

        pd.testing.assert_frame_equal(legacy, ladder)
        assert legacy.iloc[0]["exit_reason"] == ExitReason.TRAILING_BE.value

    def test_concurrent_parity(self) -> None:
        ohlcv = self._parity_path()
        signals = _make_signal(ohlcv, bar_idx=20)

        legacy = _bt(allow_concurrent=True).run(signals, ohlcv).to_dataframe()
        ladder = _bt(
            allow_concurrent=True, trailing_ladder_long=((1.0, 0.5),)
        ).run(signals, ohlcv).to_dataframe()

        pd.testing.assert_frame_equal(legacy, ladder)


# ---------------------------------------------------------------------------
# 2-rung mechanics — single-position path
# ---------------------------------------------------------------------------


class TestSecondRung:
    def test_second_rung_ratchets_stop_higher(self) -> None:
        ohlcv = _rung2_path()
        signals = _make_signal(ohlcv, bar_idx=20)

        result = _bt(trailing_ladder_long=LADDER).run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TRAILING_BE
        assert trade.exit_price == pytest.approx(65.0 + 1.0 * 0.02)  # rung2 lock
        assert trade.net_pnl_dollars == pytest.approx(20.0)          # 0.02 * 1000

    def test_single_rung_engine_exits_lower_on_same_path(self) -> None:
        """Contrast: without rung 2 the same path gives back down to rung1 lock."""
        ohlcv = _rung2_path()
        signals = _make_signal(ohlcv, bar_idx=20)

        result = _bt().run(signals, ohlcv)  # legacy single rung (1.0, 0.5)

        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TRAILING_BE
        assert trade.exit_price == pytest.approx(65.0 + 0.5 * 0.02)  # rung1 lock
        assert trade.net_pnl_dollars == pytest.approx(10.0)

    def test_both_rungs_advance_in_one_bar(self) -> None:
        prices, highs, lows, opens = _flat_path()
        opens[23] = 65.0
        prices[23] = 65.03
        highs[23] = 65.05    # crosses BOTH triggers (65.02, 65.04), < TP 65.08
        lows[23] = 64.995    # > initial SL 64.98
        opens[24] = 65.03
        prices[24] = 65.03
        highs[24] = 65.03
        lows[24] = 65.025
        opens[25] = 65.03
        prices[25] = 65.0
        highs[25] = 65.03
        lows[25] = 65.015    # breaches rung2 lock 65.02
        ohlcv = _make_ohlcv(40, prices=prices, highs=highs, lows=lows, opens=opens)
        signals = _make_signal(ohlcv, bar_idx=20)

        result = _bt(trailing_ladder_long=LADDER).run(signals, ohlcv)

        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TRAILING_BE
        assert trade.exit_dt == ohlcv.index[25]
        assert trade.exit_price == pytest.approx(65.02)

    def test_moved_stop_effective_next_bar_only(self) -> None:
        """A bar that advances the ladder cannot be stopped at the NEW level
        intrabar — the raised stop applies from the following bar."""
        prices, highs, lows, opens = _flat_path()
        opens[23] = 65.0
        prices[23] = 65.03
        highs[23] = 65.05    # crosses both triggers ...
        lows[23] = 65.015    # ... and dips below rung2 lock 65.02 SAME bar
        opens[24] = 65.04
        prices[24] = 65.03
        highs[24] = 65.04
        lows[24] = 65.025    # above rung2 lock — survives
        opens[25] = 65.03
        prices[25] = 65.0
        highs[25] = 65.03
        lows[25] = 65.0      # breaches rung2 lock
        ohlcv = _make_ohlcv(40, prices=prices, highs=highs, lows=lows, opens=opens)
        signals = _make_signal(ohlcv, bar_idx=20)

        result = _bt(trailing_ladder_long=LADDER).run(signals, ohlcv)

        trade = result.trades[0]
        assert trade.exit_dt == ohlcv.index[25]  # NOT bar 23
        assert trade.exit_price == pytest.approx(65.02)

    def test_short_side_ladder(self) -> None:
        prices, highs, lows, opens = _flat_path()
        # Short entry 65.0: rung1 trigger 64.98 / lock 64.99;
        # rung2 trigger 64.96 / lock 64.98; TP 64.92; initial SL 65.02.
        for i in (23, 24):
            opens[i] = 64.98
            prices[i] = 64.98
            highs[i] = 64.985
            lows[i] = 64.975     # <= rung1 trigger 64.98, > rung2 trigger 64.96
        opens[25] = 64.97
        prices[25] = 64.96
        highs[25] = 64.975
        lows[25] = 64.955        # <= rung2 trigger 64.96, > TP 64.92
        for i in range(26, 30):
            opens[i] = 64.97
            prices[i] = 64.97
            highs[i] = 64.975    # < rung2 lock 64.98 — survives
            lows[i] = 64.96
        opens[30] = 64.975
        prices[30] = 65.0
        highs[30] = 65.0         # breaches rung2 lock 64.98
        lows[30] = 64.97
        ohlcv = _make_ohlcv(40, prices=prices, highs=highs, lows=lows, opens=opens)
        signals = _make_signal(ohlcv, bar_idx=20, side=-1)

        result = _bt(trailing_ladder_short=LADDER).run(signals, ohlcv)

        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TRAILING_BE
        assert trade.exit_price == pytest.approx(65.0 - 1.0 * 0.02)
        assert trade.net_pnl_dollars == pytest.approx(20.0)

    def test_concurrent_second_rung(self) -> None:
        ohlcv = _rung2_path()
        signals = _make_signal(ohlcv, bar_idx=20)

        result = _bt(
            allow_concurrent=True, trailing_ladder_long=LADDER
        ).run(signals, ohlcv)

        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TRAILING_BE
        assert trade.exit_price == pytest.approx(65.02)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestEngineLadderValidation:
    def test_non_increasing_activation_raises(self) -> None:
        with pytest.raises(ValueError):
            _bt(trailing_ladder_long=((2.0, 1.0), (1.5, 1.5)))

    def test_non_increasing_lock_raises(self) -> None:
        with pytest.raises(ValueError):
            _bt(trailing_ladder_long=((2.0, 1.0), (3.0, 0.5)))

    def test_lock_at_or_above_activation_raises(self) -> None:
        with pytest.raises(ValueError):
            _bt(trailing_ladder_long=((2.0, 2.0),))

    def test_empty_ladder_raises(self) -> None:
        with pytest.raises(ValueError):
            _bt(trailing_ladder_long=())


# ---------------------------------------------------------------------------
# StrategyConfig parsing + validation
# ---------------------------------------------------------------------------


def _ladder_cfg(ladder, *, trailing=2.0, offset=1.0, tp=4.0) -> dict:
    side = {
        "tp_atr_mult": tp,
        "sl_atr_mult": 1.0,
        "trailing_atr_mult": trailing,
        "trailing_sl_atr_offset": offset,
    }
    if ladder is not None:
        side["trailing_ladder"] = ladder
    return {
        "tp_atr_mult": tp,
        "sl_atr_mult": 1.0,
        "long": side,
        "short": {
            "tp_atr_mult": tp,
            "sl_atr_mult": 1.0,
            "trailing_atr_mult": trailing,
            "trailing_sl_atr_offset": offset,
        },
    }


def _rung(a: float, o: float) -> dict:
    return {"activation_atr": a, "lock_atr": o}


class TestStrategyConfigLadder:
    def test_absent_is_none(self) -> None:
        sc = StrategyConfig.from_dict(_ladder_cfg(None))
        assert sc.long.trailing_ladder is None
        assert sc.short.trailing_ladder is None

    def test_valid_ladder_parses(self) -> None:
        sc = StrategyConfig.from_dict(
            _ladder_cfg([_rung(2.0, 1.0), _rung(3.0, 2.0)])
        )
        assert sc.long.trailing_ladder == ((2.0, 1.0), (3.0, 2.0))
        assert sc.short.trailing_ladder is None

    def test_rung1_must_match_legacy_scalars(self) -> None:
        with pytest.raises(ValueError, match="rung 1"):
            StrategyConfig.from_dict(
                _ladder_cfg([_rung(2.5, 1.0), _rung(3.0, 2.0)])  # trailing=2.0
            )
        with pytest.raises(ValueError, match="rung 1"):
            StrategyConfig.from_dict(
                _ladder_cfg([_rung(2.0, 0.8), _rung(3.0, 2.0)])  # offset=1.0
            )

    def test_non_increasing_activations_raise(self) -> None:
        with pytest.raises(ValueError):
            StrategyConfig.from_dict(
                _ladder_cfg([_rung(2.0, 1.0), _rung(2.0, 1.5)])
            )

    def test_non_increasing_locks_raise(self) -> None:
        with pytest.raises(ValueError):
            StrategyConfig.from_dict(
                _ladder_cfg([_rung(2.0, 1.0), _rung(3.0, 1.0)])
            )

    def test_lock_at_or_above_activation_raises(self) -> None:
        with pytest.raises(ValueError):
            StrategyConfig.from_dict(_ladder_cfg([_rung(2.0, 2.0)]))

    def test_last_activation_must_be_below_tp(self) -> None:
        with pytest.raises(ValueError, match="tp_atr_mult"):
            StrategyConfig.from_dict(
                _ladder_cfg([_rung(2.0, 1.0), _rung(4.0, 2.0)], tp=4.0)
            )

    def test_incomplete_rung_raises(self) -> None:
        with pytest.raises(ValueError):
            StrategyConfig.from_dict(_ladder_cfg([{"activation_atr": 2.0}]))

    def test_empty_ladder_raises(self) -> None:
        with pytest.raises(ValueError):
            StrategyConfig.from_dict(_ladder_cfg([]))

    def test_non_list_raises(self) -> None:
        with pytest.raises(ValueError):
            StrategyConfig.from_dict(_ladder_cfg({"activation_atr": 2.0}))

    def test_from_config_wires_ladder_into_engine(self) -> None:
        cfg = _ladder_cfg([_rung(2.0, 1.0), _rung(3.0, 2.0)])
        bt = BacktestEngine.from_config(cfg)
        assert bt.trailing_ladder_long == ((2.0, 1.0), (3.0, 2.0))
        assert bt.trailing_ladder_short is None


# ---------------------------------------------------------------------------
# Optimizer: ladder_enabled -> fixed geometric rung 2
# ---------------------------------------------------------------------------


class TestOptimizerLadder:
    def test_param_ranges_expose_boolean_dim(self) -> None:
        assert "ladder_enabled" in _PARAM_RANGES
        assert _PARAM_RANGES["ladder_enabled"][3] == "bool"

    def test_derive_enabled_builds_fixed_rung2(self) -> None:
        out = _derive_trailing_params({
            "tp_atr_mult": 8.0,
            "trigger_frac": 0.3,
            "distance_frac": 0.3,
            "ladder_enabled": True,
        })
        # rung1 from existing derivation: a1 = 8*0.3 = 2.4, o1 = 2.4*0.7 = 1.68
        assert out["trailing_atr_mult"] == pytest.approx(2.4)
        assert out["trailing_sl_atr_offset"] == pytest.approx(1.68)
        # fixed rule: trigger2 = 2.4 + 0.5*(8-2.4) = 5.2, lock2 = 2.4
        assert out["trailing_ladder"] == [
            {"activation_atr": 2.4, "lock_atr": 1.68},
            {"activation_atr": 5.2, "lock_atr": 2.4},
        ]
        assert "ladder_enabled" not in out

    def test_derive_disabled_emits_removal_sentinel(self) -> None:
        out = _derive_trailing_params({
            "tp_atr_mult": 8.0,
            "trigger_frac": 0.3,
            "distance_frac": 0.3,
            "ladder_enabled": False,
        })
        assert out["trailing_ladder"] is None
        assert "ladder_enabled" not in out

    def test_apply_trial_params_writes_and_removes_side_ladder(self) -> None:
        strat = TieredEnsembleStrategy({
            "execution_class": "TieredEnsembleStrategy",
            "long": {"tiers": [{"min_prob": 0.5, "lots": 1}]},
            "short": {},
        })
        cfg = {
            "long": {"tiers": [{"min_prob": 0.5, "lots": 1}]},
            "short": {},
        }
        ladder = [
            {"activation_atr": 2.4, "lock_atr": 1.68},
            {"activation_atr": 5.2, "lock_atr": 2.4},
        ]
        strat.apply_trial_params(
            cfg,
            {"trailing_atr_mult": 2.4, "trailing_sl_atr_offset": 1.68,
             "trailing_ladder": ladder},
            side="long",
        )
        assert cfg["long"]["trailing_ladder"] == ladder
        # ladder must stay per-side: never written to top level
        assert "trailing_ladder" not in cfg

    def test_apply_trial_params_removal(self) -> None:
        strat = TieredEnsembleStrategy({
            "execution_class": "TieredEnsembleStrategy",
            "long": {"tiers": [{"min_prob": 0.5, "lots": 1}]},
            "short": {},
        })
        cfg = {
            "long": {
                "tiers": [{"min_prob": 0.5, "lots": 1}],
                "trailing_ladder": [{"activation_atr": 2.4, "lock_atr": 1.68}],
            },
            "short": {},
        }
        strat.apply_trial_params(cfg, {"trailing_ladder": None}, side="long")
        assert "trailing_ladder" not in cfg["long"]
