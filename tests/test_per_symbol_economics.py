"""Per-symbol economics resolution (ticket per-symbol-economics_07052026_0930).

Guards the fix for the fleet-wide bug where the optimizer/artifacts chain ran
every symbol at CL economics (contract_multiplier 1000 $/pt, slippage 0.01).

Hard constraint: CL must resolve to EXACTLY the legacy constants
(1000.0 $/pt, 0.01 slippage) — the ledger-parity gate depends on it.
"""
import numpy as np
import pandas as pd
import pytest

from src.core.instrument_master import (
    default_slippage_points,
    dollars_per_point,
)


class TestRegistryHelpers:
    def test_cl_byte_identical_to_legacy_constants(self):
        assert dollars_per_point("CL") == 1000.0
        assert default_slippage_points("CL") == 0.01

    def test_zc_dollars_per_point_uses_quote_unit(self):
        # 5,000 bu x $0.01 per cent = $50 per quoted point (cent/bushel)
        assert dollars_per_point("ZC") == 50.0
        assert default_slippage_points("ZC") == 0.25  # 1 tick

    def test_es_and_ng(self):
        assert dollars_per_point("ES") == 50.0
        assert default_slippage_points("ES") == 0.25
        assert dollars_per_point("NG") == 10000.0
        assert default_slippage_points("NG") == pytest.approx(0.001)

    def test_every_registered_symbol_resolves_positive(self):
        from src.core.instrument_master import INSTRUMENT_REGISTRY
        for sym in INSTRUMENT_REGISTRY:
            assert dollars_per_point(sym) > 0
            assert default_slippage_points(sym) > 0

    def test_unknown_symbol_raises(self):
        with pytest.raises(ValueError, match="Unknown instrument symbol"):
            dollars_per_point("QQ")

    def test_tick_value_invariant(self):
        # tick_value == tick_size * dollars_per_point for every instrument
        from src.core.instrument_master import INSTRUMENT_REGISTRY
        for sym, inst in INSTRUMENT_REGISTRY.items():
            assert inst.tick_value == pytest.approx(
                inst.tick_size * dollars_per_point(sym)
            ), f"{sym}: tick_value invariant broken"


class TestOptimizerEconomicsResolution:
    def test_resolve_none_symbol_is_legacy(self):
        from agent.strategy_optimizer import _resolve_symbol_economics
        mult, slip = _resolve_symbol_economics(None, 0.01)
        assert mult is None
        assert slip == 0.01

    def test_resolve_zc(self):
        from agent.strategy_optimizer import _resolve_symbol_economics
        mult, slip = _resolve_symbol_economics("ZC", None)
        assert mult == 50.0
        assert slip == 0.25

    def test_resolve_explicit_slippage_wins(self):
        from agent.strategy_optimizer import _resolve_symbol_economics
        mult, slip = _resolve_symbol_economics("ZC", 0.125)
        assert mult == 50.0
        assert slip == 0.125

    def test_resolve_cl_matches_legacy(self):
        from agent.strategy_optimizer import _resolve_symbol_economics
        mult, slip = _resolve_symbol_economics("CL", 0.01)
        assert mult == 1000.0
        assert slip == 0.01


class TestEngineReceivesMultiplier:
    def _tiny_cfg(self):
        return {
            "nickname": "econ_test",
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
            "entry_threshold": 0.5,
        }

    def test_from_config_multiplier_override(self):
        from agent.backtest_engine import BacktestEngine
        bt = BacktestEngine.from_config(self._tiny_cfg(), contract_multiplier=50.0)
        assert bt.contract_multiplier == 50.0

    def test_from_config_default_is_legacy_1000(self):
        from agent.backtest_engine import BacktestEngine
        bt = BacktestEngine.from_config(self._tiny_cfg())
        assert bt.contract_multiplier == 1000.0

    def test_pnl_scales_with_multiplier(self):
        """Same signals/prices, 20x multiplier gap -> gross PnL scales 20x but
        fixed commission does NOT — the cost-realism core of the ticket."""
        from agent.backtest_engine import BacktestEngine

        idx = pd.date_range("2024-01-02 09:00", periods=60, freq="h")
        rng = np.random.default_rng(7)
        close = pd.Series(100.0 + np.cumsum(rng.normal(0, 0.2, len(idx))), index=idx)
        ohlcv = pd.DataFrame({
            "Open": close.shift(1).fillna(close.iloc[0]),
            "High": close + 0.3,
            "Low": close - 0.3,
            "Close": close,
            "Volume": 1000,
        }, index=idx)
        preds = pd.DataFrame({"prob_Buy": 0.0}, index=idx)
        preds.iloc[30, 0] = 0.9  # single long entry (past the 14-bar ATR warm-up)

        results = {}
        for mult in (1000.0, 50.0):
            bt = BacktestEngine(
                tp_atr_mult=2.0, sl_atr_mult=1.0, prob_threshold=0.5,
                commission_per_side=2.50, slippage_per_side=0.0,
                contract_multiplier=mult,
            )
            res = bt.run(preds, ohlcv, label=f"mult={mult}")
            assert res.trade_count == 1
            results[mult] = res.trades[0]

        t_big, t_small = results[1000.0], results[50.0]
        assert t_big.gross_pnl_dollars == pytest.approx(t_small.gross_pnl_dollars * 20.0)
        # commission identical in dollars -> different relative weight
        assert t_big.commission_dollars == t_small.commission_dollars
