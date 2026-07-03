"""
TDD-TESTER AUTHORIZATION
Target Implementation File: agent/backtest_engine.py
Target Class/Function: BacktestEngine._on_flat (static SL/TP price derivation)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: parity-exit-signal_07022026_1930

Phenomenon B(a) — the backtest's static SL/TP prices must mimic IBKR reality
(mirroring live_trader.py:1622/1635):

1. SL and TP are derived from the slippage-adjusted entry FILL price
   (self._entry_fill, produced by the existing 1-tick adverse _apply_slippage),
   NOT the raw self._entry_price.
2. The resulting SL and TP are rounded to 2 decimals (CL 0.01 penny grid)
   with a SINGLE rounding — matching live's round(fill_price ± offset, 2).
3. Scope guard: entry price, entry fill, slippage model, ATR-at-entry, side,
   and lots are unchanged — only the SL/TP price derivation shifts.

Numbers are chosen so a raw-basis and/or unrounded derivation FAILS by at
least a tick:
    entry 70.003, slippage 0.01/side, ATR 0.5537, sl_mult 1.0, tp_mult 2.0
    LONG : fill 70.013 -> SL round(70.013-0.5537,2)=69.46, TP round(70.013+1.1074,2)=71.12
           (raw unrounded: 69.4493 / 71.1104; raw rounded: 69.45 / 71.11 — all wrong)
    SHORT: fill 69.993 -> SL round(69.993+0.5537,2)=70.55, TP round(69.993-1.1074,2)=68.89
           (raw unrounded: 70.5567 / 68.8956; raw rounded: 70.56 / 68.90 — all wrong)

Deterministic, no data files, no network — the FSM entry is driven directly
via _on_flat() with a synthetic bar stub.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from agent.backtest_engine import BacktestEngine, TradeState
from src.live_execution.strategies.execution_models import Order


ENTRY_PRICE = 70.003
ATR = 0.5537
TICK = 0.01

ENTRY_DT = pd.Timestamp("2026-06-02 07:00:00")


def _make_engine(**overrides) -> BacktestEngine:
    kwargs = dict(
        tp_atr_mult=2.0,
        sl_atr_mult=1.0,
        slippage_per_side=TICK,
        commission_per_side=2.50,
        contract_multiplier=1000.0,
        max_horizon=24,
    )
    kwargs.update(overrides)
    return BacktestEngine(**kwargs)


def _bar(close: float) -> SimpleNamespace:
    """Minimal bar stub exposing the exec_* fields _on_flat reads."""
    return SimpleNamespace(
        exec_Close=close,
        exec_High=close + 0.05,
        exec_Low=close - 0.05,
    )


# ---------------------------------------------------------------------------
# Fill-basis + penny-grid rounding (the actual behavior change)
# ---------------------------------------------------------------------------


class TestSlTpFillBasisPennyRounded:
    def test_long_sl_tp_from_slippage_fill_rounded_to_cents(self):
        """LONG: SL/TP = round(entry_fill -/+ mult*ATR, 2), entry_fill = entry + 1 tick."""
        engine = _make_engine()
        engine._on_flat(ENTRY_DT, _bar(ENTRY_PRICE), signal_side=1, atr=ATR)

        assert engine._state == TradeState.IN_POSITION
        # SL from fill basis (70.013), single-rounded to the penny grid
        assert engine._sl_price == pytest.approx(69.46, abs=1e-9), (
            f"LONG SL must be round(fill 70.013 - 1.0*{ATR}, 2) = 69.46; "
            f"got {engine._sl_price!r} (raw-basis unrounded would be 69.4493, "
            f"raw-basis rounded would be 69.45 — both wrong)"
        )
        # TP from fill basis, single-rounded to the penny grid
        assert engine._tp_price == pytest.approx(71.12, abs=1e-9), (
            f"LONG TP must be round(fill 70.013 + 2.0*{ATR}, 2) = 71.12; "
            f"got {engine._tp_price!r} (raw-basis unrounded would be 71.1104, "
            f"raw-basis rounded would be 71.11 — both wrong)"
        )
        assert engine._original_sl_price == pytest.approx(engine._sl_price, abs=0)

    def test_short_sl_tp_from_slippage_fill_rounded_to_cents(self):
        """SHORT: SL/TP = round(entry_fill +/- mult*ATR, 2), entry_fill = entry - 1 tick."""
        engine = _make_engine()
        engine._on_flat(ENTRY_DT, _bar(ENTRY_PRICE), signal_side=-1, atr=ATR)

        assert engine._state == TradeState.IN_POSITION
        assert engine._sl_price == pytest.approx(70.55, abs=1e-9), (
            f"SHORT SL must be round(fill 69.993 + 1.0*{ATR}, 2) = 70.55; "
            f"got {engine._sl_price!r} (raw-basis unrounded would be 70.5567, "
            f"raw-basis rounded would be 70.56 — both wrong)"
        )
        assert engine._tp_price == pytest.approx(68.89, abs=1e-9), (
            f"SHORT TP must be round(fill 69.993 - 2.0*{ATR}, 2) = 68.89; "
            f"got {engine._tp_price!r} (raw-basis unrounded would be 68.8956, "
            f"raw-basis rounded would be 68.90 — both wrong)"
        )
        assert engine._original_sl_price == pytest.approx(engine._sl_price, abs=0)

    def test_per_trade_order_overrides_also_use_fill_basis(self):
        """Order-level tp/sl multiplier overrides go through the same fill-basis derivation."""
        engine = _make_engine()
        order = Order(
            action="BUY", side=1, lots=2, tp_atr_mult=1.5, sl_atr_mult=0.75,
        )
        engine._on_flat(
            ENTRY_DT, _bar(ENTRY_PRICE), signal_side=1, atr=ATR,
            lots=order.lots, order=order,
        )

        # fill 70.013: SL = round(70.013 - 0.75*0.5537, 2) = round(69.597725, 2) = 69.60
        assert engine._sl_price == pytest.approx(69.60, abs=1e-9), (
            f"Override LONG SL must be 69.60 (fill basis, penny grid); "
            f"got {engine._sl_price!r}"
        )
        # TP = round(70.013 + 1.5*0.5537, 2) = round(70.84355, 2) = 70.84
        assert engine._tp_price == pytest.approx(70.84, abs=1e-9), (
            f"Override LONG TP must be 70.84 (fill basis, penny grid); "
            f"got {engine._tp_price!r}"
        )

    def test_sl_tp_land_exactly_on_penny_grid(self):
        """Whatever the inputs, static SL/TP must be single-rounded 2dp values."""
        engine = _make_engine()
        engine._on_flat(ENTRY_DT, _bar(ENTRY_PRICE), signal_side=1, atr=ATR)

        for label, price in (("SL", engine._sl_price), ("TP", engine._tp_price)):
            assert price == pytest.approx(round(price, 2), abs=1e-9), (
                f"{label} price {price!r} is not on the CL 0.01 penny grid — "
                f"live brackets are round(..., 2) (live_trader.py:1622/1635)"
            )


# ---------------------------------------------------------------------------
# Scope guard: everything EXCEPT the SL/TP derivation is untouched
# ---------------------------------------------------------------------------


class TestScopeGuardUnchanged:
    def test_entry_fields_and_slippage_model_unchanged_long(self):
        engine = _make_engine()
        engine._on_flat(ENTRY_DT, _bar(ENTRY_PRICE), signal_side=1, atr=ATR)

        # Entry price stays the RAW bar close (trade selection/entry unchanged)
        assert engine._entry_price == pytest.approx(ENTRY_PRICE, abs=1e-12)
        # Entry fill stays the existing 1-tick adverse slippage
        assert engine._entry_fill == pytest.approx(ENTRY_PRICE + TICK, abs=1e-12)
        assert engine._atr_at_entry == pytest.approx(ATR, abs=1e-12)
        assert engine._side == 1
        assert engine._lots == 1
        assert engine._bars_held == 0
        assert engine._entry_dt == ENTRY_DT

    def test_entry_fields_and_slippage_model_unchanged_short(self):
        engine = _make_engine()
        engine._on_flat(ENTRY_DT, _bar(ENTRY_PRICE), signal_side=-1, atr=ATR)

        assert engine._entry_price == pytest.approx(ENTRY_PRICE, abs=1e-12)
        assert engine._entry_fill == pytest.approx(ENTRY_PRICE - TICK, abs=1e-12)
        assert engine._atr_at_entry == pytest.approx(ATR, abs=1e-12)
        assert engine._side == -1

    def test_apply_slippage_semantics_preserved(self):
        engine = _make_engine()
        assert engine._apply_slippage(70.00, "Buy") == pytest.approx(70.01, abs=1e-12)
        assert engine._apply_slippage(70.00, "Sell") == pytest.approx(69.99, abs=1e-12)
