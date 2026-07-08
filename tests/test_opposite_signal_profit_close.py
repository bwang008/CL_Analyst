"""Feature A — opposite-signal profit-close (Phase 2 of
exit-triggers-eod-oppsignal_07072026_1924).

New ``conflict_resolution`` mode ``close_existing_position_if_profit`` on
TieredEnsembleStrategy: when IN_POSITION, EXIT iff the OPPOSITE side's signal
fires AND the current side's own signal has stopped confirming AND the position
is green.  Both-firing -> HOLD; losing -> HOLD.  All existing modes unchanged.

Design points under test:
  - Price-basis correctness: "green" is judged on the EXEC (raw) basis via
    engine-published ``EngineState.floating_pnl_points`` — NEVER by comparing
    the brain (ratio-adjusted) close against the exec-basis entry fill.
  - Impact-review binding condition: if the mode is active in-position and
    ``floating_pnl_points`` is None (an environment that does not feed the
    field, e.g. today's live path), raise loudly instead of silently holding.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agent.backtest_engine import BacktestEngine, ExitReason
from src.live_execution.strategies.execution_models import (
    BaseExecutionStrategy,
    EngineState,
    Order,
    TieredEnsembleStrategy,
)


def _cfg(mode: str = "close_existing_position_if_profit") -> dict:
    return {
        "nickname": "oppo-test",
        "conflict_resolution": mode,
        "long": {"tiers": [{"min_prob": 0.60, "lots": 1}]},
        "short": {"tiers": [{"min_prob": 0.60, "lots": 1}]},
    }


def _state(side: int, floating: float | None, entry: float | None = 100.0) -> EngineState:
    return EngineState(
        position=1 if side != 0 else 0,
        side=side,
        bars_held=5,
        open_positions=1 if side != 0 else 0,
        last_exit_bars_ago_long=9999,
        last_exit_bars_ago_short=9999,
        entry_price=entry,
        floating_pnl_points=floating,
    )


def _bar(strat: TieredEnsembleStrategy, state: EngineState,
         prob_buy: float, prob_sell: float) -> list[Order]:
    return strat.on_bar(
        pd.Timestamp("2026-06-02 10:00:00"), 100.0, 100.2, 99.8, 100.1,
        0.5, prob_buy, prob_sell, state,
    )


# ---------------------------------------------------------------------------
# EngineState — new fields default to None (flat)
# ---------------------------------------------------------------------------


class TestEngineStateFields:
    def test_defaults_none(self):
        es = EngineState()
        assert es.entry_price is None
        assert es.floating_pnl_points is None


# ---------------------------------------------------------------------------
# mode validation
# ---------------------------------------------------------------------------


class TestModeValidation:
    def test_new_mode_accepted(self):
        strat = TieredEnsembleStrategy(_cfg())
        assert strat.conflict_resolution == "close_existing_position_if_profit"

    @pytest.mark.parametrize("mode", ["hold", "close_existing_position",
                                      "reverse_position"])
    def test_existing_modes_still_accepted(self, mode):
        assert TieredEnsembleStrategy(_cfg(mode)).conflict_resolution == mode

    def test_typo_rejected(self):
        with pytest.raises(ValueError, match="conflict_resolution"):
            TieredEnsembleStrategy(_cfg("close_if_profit"))


# ---------------------------------------------------------------------------
# decision rule (long position; short is mirrored)
# ---------------------------------------------------------------------------


class TestDecisionRule:
    def test_exit_when_opposite_fires_same_stopped_and_green(self):
        strat = TieredEnsembleStrategy(_cfg())
        orders = _bar(strat, _state(side=1, floating=+0.5),
                      prob_buy=0.30, prob_sell=0.70)
        assert len(orders) == 1
        assert orders[0].action == "EXIT"
        assert orders[0].side == 1

    def test_hold_when_losing(self):
        strat = TieredEnsembleStrategy(_cfg())
        orders = _bar(strat, _state(side=1, floating=-0.5),
                      prob_buy=0.30, prob_sell=0.70)
        assert all(o.action == "HOLD" for o in orders)

    def test_hold_when_flat_pnl(self):
        """Exactly zero unrealized is NOT green (strict > 0)."""
        strat = TieredEnsembleStrategy(_cfg())
        orders = _bar(strat, _state(side=1, floating=0.0),
                      prob_buy=0.30, prob_sell=0.70)
        assert all(o.action == "HOLD" for o in orders)

    def test_hold_when_both_sides_fire(self):
        strat = TieredEnsembleStrategy(_cfg())
        orders = _bar(strat, _state(side=1, floating=+0.5),
                      prob_buy=0.70, prob_sell=0.70)
        assert all(o.action == "HOLD" for o in orders)

    def test_hold_when_opposite_does_not_fire(self):
        strat = TieredEnsembleStrategy(_cfg())
        orders = _bar(strat, _state(side=1, floating=+0.5),
                      prob_buy=0.30, prob_sell=0.30)
        assert all(o.action == "HOLD" for o in orders)

    def test_short_position_mirrored(self):
        strat = TieredEnsembleStrategy(_cfg())
        orders = _bar(strat, _state(side=-1, floating=+0.5),
                      prob_buy=0.70, prob_sell=0.30)
        assert len(orders) == 1
        assert orders[0].action == "EXIT"
        assert orders[0].side == -1

    def test_flat_entry_path_unchanged(self):
        strat = TieredEnsembleStrategy(_cfg())
        orders = _bar(strat, _state(side=0, floating=None, entry=None),
                      prob_buy=0.70, prob_sell=0.30)
        assert orders[0].action == "BUY"

    def test_raises_loudly_when_floating_pnl_missing(self):
        """Binding impact-review condition: an environment that does not feed
        floating_pnl_points (today's live path) must CRASH, not silently
        degrade to hold semantics."""
        strat = TieredEnsembleStrategy(_cfg())
        with pytest.raises(RuntimeError, match="floating_pnl_points"):
            _bar(strat, _state(side=1, floating=None),
                 prob_buy=0.30, prob_sell=0.70)

    def test_existing_hold_mode_never_raises_without_floating(self):
        """Pre-existing modes must not be affected by the new field."""
        strat = TieredEnsembleStrategy(_cfg("hold"))
        orders = _bar(strat, _state(side=1, floating=None),
                      prob_buy=0.30, prob_sell=0.70)
        assert all(o.action == "HOLD" for o in orders)


# ---------------------------------------------------------------------------
# engine populates EngineState on the EXEC basis
# ---------------------------------------------------------------------------


class _SpyStrategy(BaseExecutionStrategy):
    """Enters long once past ATR warm-up, records state each bar."""

    def __init__(self):
        super().__init__({"nickname": "spy"})
        self.seen: list[tuple] = []
        self._entered = False

    def on_bar(self, dt, open_, high, low, close, atr, prob_buy, prob_sell,
               state: EngineState) -> list[Order]:
        self.seen.append(
            (pd.Timestamp(dt), state.position, state.entry_price,
             state.floating_pnl_points)
        )
        if not self._entered and len(self.seen) >= 16:
            self._entered = True
            return [Order(action="BUY", side=1, lots=1, reason="spy-entry")]
        return [Order(action="HOLD", side=0, lots=0, reason="spy-hold")]


BRAIN_EXEC_OFFSET = 50.0  # exec prices deliberately diverge from brain prices


def _dual_basis_data(n: int = 40):
    idx = pd.date_range("2026-06-01 00:00", periods=n, freq="h")
    brain_close = 100.0 + 0.10 * np.arange(n)
    ohlcv = pd.DataFrame(
        {"Open": brain_close - 0.02, "High": brain_close + 0.10,
         "Low": brain_close - 0.10, "Close": brain_close, "Volume": 1000.0},
        index=idx,
    )
    ohlcv_exec = ohlcv[["Open", "High", "Low", "Close"]] + BRAIN_EXEC_OFFSET
    ohlcv_exec["Volume"] = 1000.0
    signals = pd.DataFrame({"prob_Buy": np.zeros(n)}, index=idx)
    return signals, ohlcv, ohlcv_exec


class TestEnginePopulatesExecBasis:
    def test_single_strategy_loop_feeds_exec_basis_pnl(self):
        spy = _SpyStrategy()
        engine = BacktestEngine(
            tp_atr_mult=100.0, sl_atr_mult=100.0, max_horizon=500,
            slippage_per_side=0.01, contract_multiplier=1000.0,
            execution_strategy=spy,
        )
        signals, ohlcv, ohlcv_exec = _dual_basis_data()
        engine.run(signals, ohlcv, ohlcv_exec_df=ohlcv_exec)

        flat_rows = [s for s in spy.seen if s[1] == 0]
        in_pos_rows = [s for s in spy.seen if s[1] != 0]
        assert in_pos_rows, "spy never saw itself in position"

        # Flat bars: both fields None
        for _, _, entry, floating in flat_rows:
            assert entry is None and floating is None

        # In-position bars: entry_price is the EXEC-basis fill
        # (exec close of the entry bar + slippage), never the brain close.
        entry_bar_brain_close = float(ohlcv["Close"].iloc[15])
        expected_entry_fill = entry_bar_brain_close + BRAIN_EXEC_OFFSET + 0.01
        for dt, _, entry, floating in in_pos_rows:
            assert entry == pytest.approx(expected_entry_fill, abs=1e-9)
            exec_close = float(ohlcv_exec.loc[dt, "Close"])
            assert floating == pytest.approx(exec_close - expected_entry_fill,
                                             abs=1e-9)
            # Guard against the price-basis bug: brain-close-based PnL would
            # be off by exactly the offset.
            brain_close = float(ohlcv.loc[dt, "Close"])
            wrong = brain_close - expected_entry_fill
            assert abs(floating - wrong) > 1.0


# ---------------------------------------------------------------------------
# end-to-end through from_config: profitable long flips out on opposite signal
# ---------------------------------------------------------------------------


def _e2e_cfg() -> dict:
    return {
        "nickname": "oppo-e2e",
        "execution_class": "TieredEnsembleStrategy",
        "conflict_resolution": "close_existing_position_if_profit",
        "tp_atr_mult": 100.0,
        "sl_atr_mult": 100.0,
        "max_hold_bars": 500,
        "long": {"tiers": [{"min_prob": 0.60, "lots": 1,
                            "tp_atr_mult": 100.0, "sl_atr_mult": 100.0,
                            "max_hold_bars": 500}]},
        "short": {"tiers": [{"min_prob": 0.60, "lots": 1,
                             "tp_atr_mult": 100.0, "sl_atr_mult": 100.0,
                             "max_hold_bars": 500}]},
    }


def _e2e_data(trend: float):
    n = 60
    idx = pd.date_range("2026-06-01 00:00", periods=n, freq="h")
    close = 100.0 + trend * np.arange(n)
    ohlcv = pd.DataFrame(
        {"Open": close - 0.02, "High": close + 0.10, "Low": close - 0.10,
         "Close": close, "Volume": 1000.0},
        index=idx,
    )
    prob_buy = np.zeros(n)
    prob_sell = np.zeros(n)
    prob_buy[20] = 0.90          # long entry
    prob_sell[40] = 0.90         # opposite signal fires later; buy silent
    signals = pd.DataFrame({"prob_Buy": prob_buy, "prob_Sell": prob_sell},
                           index=idx)
    return signals, ohlcv


class TestEndToEnd:
    def test_green_long_exits_on_opposite_signal(self):
        engine = BacktestEngine.from_config(_e2e_cfg())
        signals, ohlcv = _e2e_data(trend=+0.10)  # long is green by bar 40
        result = engine.run(signals, ohlcv)

        assert result.trade_count == 1
        t = result.trades[0]
        assert t.exit_reason == ExitReason.SIGNAL_EXIT
        assert t.exit_dt == ohlcv.index[40]
        assert t.net_pnl_dollars > 0

    def test_losing_long_holds_through_opposite_signal(self):
        engine = BacktestEngine.from_config(_e2e_cfg())
        signals, ohlcv = _e2e_data(trend=-0.10)  # long is red by bar 40
        result = engine.run(signals, ohlcv)

        # No SIGNAL_EXIT: the loser rides (brackets/horizon are wide, so it
        # simply never closes within the window).
        assert all(t.exit_reason != ExitReason.SIGNAL_EXIT for t in result.trades)
