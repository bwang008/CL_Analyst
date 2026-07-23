"""Ticket live-trailing-ladder-phase3_07232026_0035.

Phase 3 of trailing-stop-ladder_07132026_1745: the LIVE trader executes
multi-rung ``trailing_ladder`` configs with the engine's exact semantics
(backtest_engine.py ~1046: advance through the rung list while the
favorable extreme has crossed the rung's activation; lock = entry +/-
lock_atr x ATR-at-entry). Live design is STATELESS: each bar the highest
activated rung is recomputed from the extremes and a modify is
transmitted only when the target is STRICTLY tighter than the tracked
(ledger-restored) SL — restart-safe and never-loosening without a
persisted rung counter.

Stub pattern mirrors tests/test_log_cosmetics.py::TestNoOpTrailingSkip
(object.__new__ + _instrument_context tick seam + _tracked_sl_price).
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.live_execution.live_trader import LiveTrader

TICKET_ID = "live-trailing-ladder-phase3_07232026_0035"

# Ladder geometry used throughout (long side, entry=100, ATR=2):
#   rung 1: activation 1.0xATR -> 102.0, lock 0.0xATR -> SL 100.0 (BE)
#   rung 2: activation 2.0xATR -> 104.0, lock 1.0xATR -> SL 102.0
LADDER_2RUNG = ((1.0, 0.0), (2.0, 1.0))


def _ladder_stub(
    *,
    side=1,
    ladder_long=None,
    ladder_short=None,
    tracked_sl,
    aux_price,
    bar_high,
    bar_low,
    trailing_activated=False,
):
    """LiveTrader stub for _check_trailing_stop (entry=100, ATR=2, tick=0.01).

    Mirrors tests/test_log_cosmetics.py::_trailing_stub; adds the Phase-3
    per-side ladder attributes (None = legacy single rung).
    """
    lt = object.__new__(LiveTrader)
    lt._active_trade_id = "trade_1"
    lt._sl_order_id = 55
    lt._trailing_activated = trailing_activated
    lt._entry_price = 100.0
    lt._atr_at_entry = 2.0
    lt._position_side = side
    lt._trade_trailing_atr_mult = None
    # Legacy scalars kept rung-1-consistent (parse enforces this for real
    # ladder configs; the ladder path itself never reads them).
    lt._trailing_atr_mult = 1.0
    lt._trailing_sl_atr_offset_long = 0.0
    lt._trailing_sl_atr_offset_short = 0.0
    lt._trailing_ladder_long = ladder_long
    lt._trailing_ladder_short = ladder_short
    # _tick_size is a property; feed it via the instrument-context seam
    lt._instrument_context = SimpleNamespace(
        execution_instrument=SimpleNamespace(tick_size=0.01)
    )
    lt._tracked_sl_price = tracked_sl
    lt._highest_high = 0.0
    lt._lowest_low = float("inf")
    lt.rolling_df_5m = _bar_frame(bar_high, bar_low)
    lt.rolling_df_1h = None
    lt._execution_symbol = "CL"
    lt._open_orders = {
        55: SimpleNamespace(
            symbol="CL", order_id=55,
            raw_event=SimpleNamespace(
                order=SimpleNamespace(auxPrice=aux_price)
            ),
        )
    }
    lt.exec_client = MagicMock()
    lt.telemetry = MagicMock()
    lt._position_bars_held = 0
    lt._last_decision_context_by_order_id = {}
    return lt


def _bar_frame(bar_high, bar_low, ts="2026-07-23 06:00:00"):
    return pd.DataFrame(
        {"High": [bar_high], "Low": [bar_low]},
        index=[pd.Timestamp(ts)],
    )


# ---------------------------------------------------------------------------
# Identity fence: ladder-less configs keep today's single-rung behavior
# ---------------------------------------------------------------------------


class TestLegacySingleRungIdentity:
    def test_single_rung_fires_once_latch_honored(self):
        """No ladder: the trail fires once (one-shot latch); a later, larger
        favorable move must NOT re-modify — byte-identical legacy path."""
        lt = _ladder_stub(tracked_sl=94.0, aux_price=94.0,
                          bar_high=102.5, bar_low=100.5)
        lt._check_trailing_stop()
        lt.exec_client.modify_order.assert_called_once()
        assert lt._tracked_sl_price == pytest.approx(100.0)
        assert lt._trailing_activated is True

        # Second, larger move: latch returns BEFORE the extremes update
        lt.rolling_df_5m = _bar_frame(104.5, 101.0, "2026-07-23 07:00:00")
        lt._check_trailing_stop()
        lt.exec_client.modify_order.assert_called_once()
        assert lt._tracked_sl_price == pytest.approx(100.0)
        assert lt._highest_high == pytest.approx(102.5), (
            "legacy latch must return before the extremes update "
            "(byte-identical to the pre-ladder path)"
        )

    def test_prelatched_stub_returns_untouched(self):
        """A pre-set latch on a ladder-less config short-circuits everything
        (no extremes update, no transmit) — the exact legacy early return."""
        lt = _ladder_stub(tracked_sl=100.0, aux_price=100.0,
                          bar_high=105.0, bar_low=101.0,
                          trailing_activated=True)
        lt._check_trailing_stop()
        lt.exec_client.modify_order.assert_not_called()
        assert lt._highest_high == 0.0
        assert lt._lowest_low == float("inf")


# ---------------------------------------------------------------------------
# Multi-rung ladder: long side
# ---------------------------------------------------------------------------


class TestLadderLong:
    def test_rung1_then_rung2_two_modifies(self):
        """Rung 1 crossing modifies to entry+lock1*ATR; a later rung-2
        crossing modifies again to entry+lock2*ATR; tracked price follows;
        _trailing_activated True from the first modify."""
        lt = _ladder_stub(ladder_long=LADDER_2RUNG,
                          tracked_sl=94.0, aux_price=94.0,
                          bar_high=102.5, bar_low=100.5)
        lt._check_trailing_stop()
        assert lt.exec_client.modify_order.call_count == 1
        assert lt._tracked_sl_price == pytest.approx(100.0)
        assert lt._trailing_activated is True

        lt.rolling_df_5m = _bar_frame(104.5, 101.0, "2026-07-23 07:00:00")
        lt._check_trailing_stop()
        assert lt.exec_client.modify_order.call_count == 2, (
            "a multi-rung ladder must keep ratcheting after the first rung "
            "(the old one-shot latch would have frozen it)"
        )
        assert lt._tracked_sl_price == pytest.approx(102.0)
        assert lt._trailing_activated is True

    def test_gap_through_both_rungs_single_modify(self):
        """A bar gapping through BOTH activations locks the highest rung in
        ONE modify (engine parity: multiple rungs consumed in one bar)."""
        lt = _ladder_stub(ladder_long=LADDER_2RUNG,
                          tracked_sl=94.0, aux_price=94.0,
                          bar_high=104.5, bar_low=100.5)
        lt._check_trailing_stop()
        assert lt.exec_client.modify_order.call_count == 1
        assert lt._tracked_sl_price == pytest.approx(102.0)
        assert lt._trailing_activated is True

    def test_no_rung_activated_no_transmit(self):
        """Below rung 1's activation nothing happens (no modify, no latch)."""
        lt = _ladder_stub(ladder_long=LADDER_2RUNG,
                          tracked_sl=94.0, aux_price=94.0,
                          bar_high=101.5, bar_low=100.5)
        lt._check_trailing_stop()
        lt.exec_client.modify_order.assert_not_called()
        assert lt._trailing_activated is False
        assert lt._tracked_sl_price == pytest.approx(94.0)

    def test_current_rung_resting_noop_skip_relatches(self, caplog):
        """Tracked SL already AT the computed rung lock -> no-op skip guard
        fires (no transmit) and re-latches _trailing_activated — the
        'current rung already resting' case."""
        lt = _ladder_stub(ladder_long=LADDER_2RUNG,
                          tracked_sl=100.0, aux_price=100.0,
                          bar_high=102.5, bar_low=100.5)
        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            lt._check_trailing_stop()
        lt.exec_client.modify_order.assert_not_called()
        assert lt._trailing_activated is True
        assert any("no-op modify skipped" in r.getMessage()
                   for r in caplog.records)

    def test_never_loosen_tracked_tighter_no_transmit(self):
        """Tracked SL strictly tighter than the computed rung lock -> NO
        transmit (never-loosen fence). Latch stays False here: per the
        blueprint, re-latching happens only via a successful modify or the
        half-tick skip guard (known inherited restart limitation)."""
        lt = _ladder_stub(ladder_long=LADDER_2RUNG,
                          tracked_sl=102.5, aux_price=102.5,
                          bar_high=104.5, bar_low=100.5)
        lt._check_trailing_stop()
        lt.exec_client.modify_order.assert_not_called()
        assert lt._tracked_sl_price == pytest.approx(102.5)
        assert lt._trailing_activated is False


# ---------------------------------------------------------------------------
# Multi-rung ladder: short side mirror
# ---------------------------------------------------------------------------


class TestLadderShort:
    # Short mirror (entry=100, ATR=2): rung 1 activation 98.0 -> lock SL
    # 100.0 (BE); rung 2 activation 96.0 -> lock SL 98.0.
    def test_rung1_then_rung2_two_modifies(self):
        lt = _ladder_stub(side=-1, ladder_short=LADDER_2RUNG,
                          tracked_sl=106.0, aux_price=106.0,
                          bar_high=99.5, bar_low=97.5)
        lt._check_trailing_stop()
        assert lt.exec_client.modify_order.call_count == 1
        assert lt._tracked_sl_price == pytest.approx(100.0)
        assert lt._trailing_activated is True

        lt.rolling_df_5m = _bar_frame(99.0, 95.5, "2026-07-23 07:00:00")
        lt._check_trailing_stop()
        assert lt.exec_client.modify_order.call_count == 2
        assert lt._tracked_sl_price == pytest.approx(98.0)

    def test_never_loosen_short(self):
        """Short never-loosen: tracked SL already BELOW (tighter than) the
        computed lock -> no transmit."""
        lt = _ladder_stub(side=-1, ladder_short=LADDER_2RUNG,
                          tracked_sl=97.5, aux_price=97.5,
                          bar_high=99.0, bar_low=95.5)
        lt._check_trailing_stop()
        lt.exec_client.modify_order.assert_not_called()
        assert lt._tracked_sl_price == pytest.approx(97.5)


# ---------------------------------------------------------------------------
# Restart mid-trade (stateless design: tracked SL is the only memory)
# ---------------------------------------------------------------------------


class TestRestartMidTrade:
    def test_recross_rung2_after_restart_modifies(self):
        """Fresh process, tracked SL = rung-1 lock (ledger-restored),
        extremes reset; price re-crosses rung 2 -> modify to rung-2 lock."""
        lt = _ladder_stub(ladder_long=LADDER_2RUNG,
                          tracked_sl=100.0, aux_price=100.0,
                          bar_high=104.5, bar_low=101.0)
        lt._check_trailing_stop()
        assert lt.exec_client.modify_order.call_count == 1
        assert lt._tracked_sl_price == pytest.approx(102.0)
        assert lt._trailing_activated is True

    def test_recross_only_rung1_after_restart_never_loosens(self):
        """Fresh process, tracked SL = rung-2 lock (ledger-restored),
        extremes reset; price re-crosses ONLY rung 1 -> computed target is
        LOOSER than the resting rung-2 lock -> NO modify."""
        lt = _ladder_stub(ladder_long=LADDER_2RUNG,
                          tracked_sl=102.0, aux_price=102.0,
                          bar_high=102.5, bar_low=101.0)
        lt._check_trailing_stop()
        lt.exec_client.modify_order.assert_not_called()
        assert lt._tracked_sl_price == pytest.approx(102.0)


# ---------------------------------------------------------------------------
# Startup guard removal: multi-rung configs construct a LiveTrader
# (full-init seam mirrors tests/test_oca_protective_legs.py::_build_full_trader)
# ---------------------------------------------------------------------------


class DummyStrategy:
    """Minimal strategy stub mirroring tests/test_oca_protective_legs.py."""

    def __init__(self, feature_names=None, config=None):
        self.feature_names = (
            feature_names if feature_names is not None else ["MACD"]
        )
        self.name = "DummyStrategy"
        self.direction = "LONG"
        self.config = config if config is not None else {}


def _ladder_cfg(**extra) -> dict:
    cfg = {
        "nickname": "CL_ladder_phase3",
        "execution_symbol": "CL",
        "bar_size": "1h",
        "long": {
            "tp_atr_mult": 3.0,
            "trailing_atr_mult": 1.0,
            "trailing_sl_atr_offset": 0.0,
            "trailing_ladder": [
                {"activation_atr": 1.0, "lock_atr": 0.0},
                {"activation_atr": 2.0, "lock_atr": 1.0},
            ],
        },
    }
    cfg.update(extra)
    return cfg


def _build_full_trader(cfg: dict, tmp_path) -> LiveTrader:
    with patch("src.live_execution.live_trader.DataManager"), patch(
        "pathlib.Path.exists", return_value=True
    ):
        trader = LiveTrader(
            data_client=MagicMock(),
            exec_client=MagicMock(),
            strategy=DummyStrategy(config=cfg),
            db_path=str(tmp_path / "telemetry.db"),
            dry_run=True,
        )
    return trader


class TestGuardRemoved:
    def test_two_rung_config_constructs_and_wires_ladders(self, tmp_path):
        """Re-adjudication of the BACKTEST-ONLY GUARD (ticket
        trailing-stop-ladder_07132026_1745, removed by
        live-trailing-ladder-phase3_07232026_0035): a multi-rung config now
        constructs a LiveTrader without raising, and the per-side ladders
        are wired from StrategyConfig. No test pinned the guard's
        RuntimeError directly (only a precedent comment in
        tests/test_oca_protective_legs.py, updated with this ticket id)."""
        trader = _build_full_trader(_ladder_cfg(), tmp_path)
        assert trader._trailing_ladder_long == ((1.0, 0.0), (2.0, 1.0))
        assert trader._trailing_ladder_short is None

    def test_ladderless_config_wires_none(self, tmp_path):
        """Fleet-shaped ladder-less config: both per-side ladders None (the
        legacy single-rung path stays selected)."""
        trader = _build_full_trader(
            {"nickname": "CL_no_ladder", "execution_symbol": "CL",
             "bar_size": "1h"},
            tmp_path,
        )
        assert trader._trailing_ladder_long is None
        assert trader._trailing_ladder_short is None

    def test_conflicting_tier_trailing_refuses_to_start(self, tmp_path):
        """Engine parity (backtest_engine._open_position raises when a
        per-order trailing override conflicts with rung 1): tiers are the
        only live source of per-trade trailing_atr_mult overrides, so the
        same inconsistency is refused loudly at construction."""
        cfg = _ladder_cfg()
        cfg["long"]["tiers"] = [
            {"min_prob": 0.5, "lots": 1, "trailing_atr_mult": 1.5},
        ]
        with pytest.raises(ValueError) as excinfo:
            _build_full_trader(cfg, tmp_path)
        message = str(excinfo.value)
        assert "trailing_ladder" in message
        assert "1.5" in message
