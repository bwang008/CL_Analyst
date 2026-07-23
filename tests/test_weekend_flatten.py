"""Tests for the optional weekend-carry flatten overlay (backtester only).

The overlay flattens a still-open, profitable position on the last bar before a
weekend/holiday market gap.  It is DEFAULT-OFF: when the ``weekend_flatten``
config block is absent (or ``enabled: false``) the engine must behave exactly as
it did before this feature existed.

Design under test (agent/backtest_engine.py, src/live_execution/strategy_config.py):
  - Trigger only on precomputed "flatten bars" (last bar before a >= min_gap_hours
    gap), and only when unrealized PnL >= profit_atr_mult x ATR-at-entry.
  - Precedence: TP/SL (intrabar) and the time barrier are evaluated FIRST, so the
    overlay only fires when the position would otherwise have survived the bar —
    keeping attribution clean and existing exits byte-identical.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from agent.backtest_engine import BacktestEngine, ExitReason, TradeState, _OpenPosition
from src.live_execution.strategy_config import (
    WeekendFlattenConfig,
    parse_eod_flatten,
    parse_weekend_flatten,
)


ATR = 0.5537
TICK = 0.01
ENTRY_PRICE = 70.003
ENTRY_DT = pd.Timestamp("2026-05-29 06:00:00")  # a Friday
FLATTEN_DT = pd.Timestamp("2026-05-29 16:00:00")  # last Friday bar before weekend


def _make_engine(*, enabled: bool, profit_atr_mult: float = 1.0,
                 max_horizon: int = 240, **overrides) -> BacktestEngine:
    wf = WeekendFlattenConfig(
        enabled=enabled, profit_atr_mult=profit_atr_mult, min_gap_hours=40.0
    )
    kwargs = dict(
        tp_atr_mult=2.0,
        sl_atr_mult=1.0,
        slippage_per_side=TICK,
        commission_per_side=2.50,
        contract_multiplier=1000.0,
        max_horizon=max_horizon,
        weekend_flatten=wf,
    )
    kwargs.update(overrides)
    engine = BacktestEngine(**kwargs)
    engine._reset_state()
    return engine


def _bar(close: float) -> SimpleNamespace:
    return SimpleNamespace(
        exec_Close=close, exec_High=close + 0.05, exec_Low=close - 0.05,
    )


def _enter_long(engine: BacktestEngine) -> None:
    engine._on_flat(ENTRY_DT, _bar(ENTRY_PRICE), signal_side=1, atr=ATR)
    assert engine._state == TradeState.IN_POSITION


# ---------------------------------------------------------------------------
# config parsing — default off, crash-if-half-configured
# ---------------------------------------------------------------------------


class TestParseWeekendFlatten:
    def test_absent_block_returns_none(self):
        assert parse_weekend_flatten({"nickname": "x"}) is None

    def test_disabled_block_is_inert(self):
        wf = parse_weekend_flatten({"weekend_flatten": {"enabled": False}})
        assert wf is not None and wf.enabled is False

    def test_enabled_requires_profit_atr_mult(self):
        with pytest.raises(ValueError, match="profit_atr_mult"):
            parse_weekend_flatten({"weekend_flatten": {"enabled": True}})

    def test_enabled_parses_fields(self):
        wf = parse_weekend_flatten(
            {"weekend_flatten": {"enabled": True, "profit_atr_mult": 1.5,
                                 "min_gap_hours": 36.0}}
        )
        assert (wf.enabled, wf.profit_atr_mult, wf.min_gap_hours) == (True, 1.5, 36.0)

    def test_non_dict_block_raises(self):
        with pytest.raises(ValueError):
            parse_weekend_flatten({"weekend_flatten": "yes"})


# ---------------------------------------------------------------------------
# single-position path (_on_in_position) — trigger + precedence
# ---------------------------------------------------------------------------


class TestSinglePositionFlatten:
    def test_flatten_fires_on_profitable_flatten_bar(self):
        engine = _make_engine(enabled=True, profit_atr_mult=1.0)
        engine._flatten_bars = {FLATTEN_DT}
        _enter_long(engine)

        entry_fill, atr = engine._entry_fill, engine._atr_at_entry
        bar_open = entry_fill + 1.4 * atr  # >= 1.0 ATR in favor
        # keep the bar strictly inside the TP/SL rails so no barrier fills
        high = min(bar_open + 0.02, engine._tp_price - 0.02)
        low = max(bar_open - 0.02, engine._sl_price + 0.02)

        engine._on_in_position(FLATTEN_DT, bar_open, high, low)

        assert engine._state == TradeState.FLAT
        trade = engine._trades[-1]
        assert trade.exit_reason == ExitReason.WEEKEND_FLATTEN
        assert trade.exit_price == pytest.approx(bar_open, abs=1e-9)

    def test_no_flatten_when_below_profit_threshold(self):
        engine = _make_engine(enabled=True, profit_atr_mult=1.0)
        engine._flatten_bars = {FLATTEN_DT}
        _enter_long(engine)

        entry_fill, atr = engine._entry_fill, engine._atr_at_entry
        bar_open = entry_fill + 0.3 * atr  # profitable but < 1.0 ATR
        engine._on_in_position(FLATTEN_DT, bar_open, bar_open + 0.02, bar_open - 0.02)

        assert engine._state == TradeState.IN_POSITION
        assert engine._trades == []

    def test_no_flatten_on_non_flatten_bar(self):
        engine = _make_engine(enabled=True, profit_atr_mult=1.0)
        engine._flatten_bars = {FLATTEN_DT}
        _enter_long(engine)

        entry_fill, atr = engine._entry_fill, engine._atr_at_entry
        bar_open = entry_fill + 1.4 * atr  # very profitable...
        other_dt = pd.Timestamp("2026-05-27 12:00:00")  # ...but not a flatten bar
        engine._on_in_position(other_dt, bar_open, bar_open + 0.02, bar_open - 0.02)

        assert engine._state == TradeState.IN_POSITION
        assert engine._trades == []

    def test_profit_zero_flattens_any_non_loser(self):
        engine = _make_engine(enabled=True, profit_atr_mult=0.0)
        engine._flatten_bars = {FLATTEN_DT}
        _enter_long(engine)

        entry_fill = engine._entry_fill
        bar_open = entry_fill + 0.01  # barely in the green
        engine._on_in_position(FLATTEN_DT, bar_open, bar_open + 0.02, bar_open - 0.02)

        assert engine._trades[-1].exit_reason == ExitReason.WEEKEND_FLATTEN

    def test_tp_beats_weekend_flatten_same_bar(self):
        engine = _make_engine(enabled=True, profit_atr_mult=0.0)
        engine._flatten_bars = {FLATTEN_DT}
        _enter_long(engine)

        # bar high pierces TP AND it is a profitable flatten bar -> TP wins
        engine._on_in_position(FLATTEN_DT, engine._entry_fill + 0.1,
                               engine._tp_price + 0.10, engine._entry_fill)

        assert engine._trades[-1].exit_reason == ExitReason.TP

    def test_time_barrier_beats_weekend_flatten_same_bar(self):
        """When the time barrier and the flatten bar coincide, TIME_BARRIER wins
        (checked first) so pre-existing exits keep their label and PnL."""
        engine = _make_engine(enabled=True, profit_atr_mult=0.0, max_horizon=1)
        engine._flatten_bars = {FLATTEN_DT}
        _enter_long(engine)

        entry_fill, atr = engine._entry_fill, engine._atr_at_entry
        prof = entry_fill + 1.4 * atr
        # bar 1: survives (bars_held=1, not > horizon 1), non-flatten bar
        engine._on_in_position(pd.Timestamp("2026-05-29 15:00:00"),
                               entry_fill + 0.05, entry_fill + 0.07, entry_fill + 0.03)
        assert engine._state == TradeState.IN_POSITION
        # bar 2: bars_held=2 > 1 -> TIME_BARRIER, even though profitable flatten bar
        engine._on_in_position(FLATTEN_DT, prof, prof + 0.02, prof - 0.02)

        assert engine._trades[-1].exit_reason == ExitReason.TIME_BARRIER

    def test_disabled_never_flattens(self):
        engine = _make_engine(enabled=False)
        engine._flatten_bars = {FLATTEN_DT}  # even if a set were present
        _enter_long(engine)

        entry_fill, atr = engine._entry_fill, engine._atr_at_entry
        bar_open = entry_fill + 3.0 * atr
        # keep inside rails so nothing else fires either
        high = min(bar_open + 0.02, engine._tp_price - 0.02)
        engine._on_in_position(FLATTEN_DT, bar_open, high, bar_open - 0.02)

        assert engine._state == TradeState.IN_POSITION
        assert engine._trades == []


# ---------------------------------------------------------------------------
# concurrent path (_check_position) — trigger fires there too
# ---------------------------------------------------------------------------


class TestConcurrentFlatten:
    def _pos(self, entry_fill: float) -> _OpenPosition:
        return _OpenPosition(
            entry_dt=ENTRY_DT,
            entry_price=entry_fill,
            entry_fill=entry_fill,
            atr_at_entry=ATR,
            side=1,
            tp_price=entry_fill + 100.0,   # far -> no TP
            sl_price=entry_fill - 100.0,   # far -> no SL
            original_sl_price=entry_fill - 100.0,
            bars_held=0,
        )

    def test_check_position_flattens_profitable_winner(self):
        engine = _make_engine(enabled=True, profit_atr_mult=1.0)
        engine._flatten_bars = {FLATTEN_DT}
        pos = self._pos(70.0)
        bar_open = pos.entry_fill + 1.4 * ATR

        rec = engine._check_position(pos, FLATTEN_DT, bar_open, bar_open + 0.02,
                                     bar_open - 0.02)

        assert rec is not None
        assert rec.exit_reason == ExitReason.WEEKEND_FLATTEN
        assert rec.exit_price == pytest.approx(bar_open, abs=1e-9)

    def test_check_position_no_flatten_when_disabled(self):
        engine = _make_engine(enabled=False)
        engine._flatten_bars = {FLATTEN_DT}
        pos = self._pos(70.0)
        bar_open = pos.entry_fill + 3.0 * ATR

        rec = engine._check_position(pos, FLATTEN_DT, bar_open, bar_open + 0.02,
                                     bar_open - 0.02)
        assert rec is None


# ---------------------------------------------------------------------------
# run()-level integration: gap detection + default-off byte-identical
# ---------------------------------------------------------------------------


def _weekend_gap_data():
    """20 contiguous Friday hourly bars, a weekend gap, then 20 Monday bars.

    A gentle uptrend keeps a long entered mid-Friday in the green with wide
    brackets so only the overlay (or nothing) can close it before the data ends.
    """
    fri = pd.date_range("2026-05-29 00:00", periods=20, freq="h")   # Friday
    mon = pd.date_range("2026-06-01 00:00", periods=20, freq="h")   # Monday
    idx = fri.append(mon)
    n = len(idx)
    close = 100.0 + 0.10 * np.arange(n)
    ohlcv = pd.DataFrame(
        {
            "Open": close - 0.05,
            "High": close + 0.20,
            "Low": close - 0.20,
            "Close": close,
            "Volume": 1000.0,
        },
        index=idx,
    )
    # single long signal at bar 15 (Fri 15:00) — ATR_14 is established by then
    signals = pd.DataFrame({"side": [1]}, index=[fri[15]])
    return signals, ohlcv


def _run(engine, signals, ohlcv):
    return engine.run(signals, ohlcv, label="t")


class TestRunLevelIntegration:
    def test_flatten_bars_are_only_pre_weekend_bars(self):
        signals, ohlcv = _weekend_gap_data()
        engine = _make_engine(enabled=True, profit_atr_mult=0.0,
                              tp_atr_mult=10.0, sl_atr_mult=10.0, max_horizon=500)
        _run(engine, signals, ohlcv)
        # The only >=40h gap is Fri 19:00 -> Mon 00:00, so the sole flatten bar
        # is the last Friday bar. Daily 1h steps must never be flagged.
        assert engine._flatten_bars == {pd.Timestamp("2026-05-29 19:00:00")}

    def test_none_vs_disabled_byte_identical(self):
        signals, ohlcv = _weekend_gap_data()
        eng_none = BacktestEngine(
            tp_atr_mult=10.0, sl_atr_mult=10.0, slippage_per_side=TICK,
            contract_multiplier=1000.0, max_horizon=500, weekend_flatten=None,
        )
        eng_dis = _make_engine(enabled=False, tp_atr_mult=10.0, sl_atr_mult=10.0,
                               max_horizon=500)
        r_none = _run(eng_none, signals, ohlcv)
        r_dis = _run(eng_dis, signals, ohlcv)
        pd.testing.assert_frame_equal(r_none.to_dataframe(), r_dis.to_dataframe())

    def test_enabled_produces_weekend_flatten_exit(self):
        signals, ohlcv = _weekend_gap_data()
        eng_dis = _make_engine(enabled=False, tp_atr_mult=10.0, sl_atr_mult=10.0,
                               max_horizon=500)
        eng_on = _make_engine(enabled=True, profit_atr_mult=0.0, tp_atr_mult=10.0,
                              sl_atr_mult=10.0, max_horizon=500)
        r_dis = _run(eng_dis, signals, ohlcv)
        r_on = _run(eng_on, signals, ohlcv)

        # Disabled: the long never closes (wide brackets, no barrier) -> 0 trades.
        assert r_dis.trade_count == 0
        # Enabled: exactly one WEEKEND_FLATTEN at the last Friday bar.
        assert r_on.trade_count == 1
        t = r_on.trades[0]
        assert t.exit_reason == ExitReason.WEEKEND_FLATTEN
        assert t.exit_dt == pd.Timestamp("2026-05-29 19:00:00")

    def test_no_gap_data_is_inert_even_when_enabled(self):
        """Contiguous data (no >=40h gap) has no flatten bars, so an enabled
        overlay is a no-op and matches the disabled run byte-for-byte."""
        idx = pd.date_range("2026-05-26 00:00", periods=40, freq="h")  # Tue..Wed, no weekend
        close = 100.0 + 0.10 * np.arange(len(idx))
        ohlcv = pd.DataFrame(
            {"Open": close - 0.05, "High": close + 0.20, "Low": close - 0.20,
             "Close": close, "Volume": 1000.0},
            index=idx,
        )
        signals = pd.DataFrame({"side": [1]}, index=[idx[15]])

        eng_dis = _make_engine(enabled=False, max_horizon=500)
        eng_on = _make_engine(enabled=True, profit_atr_mult=0.0, max_horizon=500)
        r_dis = _run(eng_dis, signals, ohlcv)
        r_on = _run(eng_on, signals, ohlcv)

        assert eng_on._flatten_bars == set()
        pd.testing.assert_frame_equal(r_dis.to_dataframe(), r_on.to_dataframe())


# ===========================================================================
# EOD flatten trigger (eod_flatten) — Phase 1 of
# exit-triggers-eod-oppsignal_07072026_1924
# ===========================================================================


def _flatten_cfg(profit_atr_mult: float, min_gap_hours: float) -> WeekendFlattenConfig:
    return WeekendFlattenConfig(
        enabled=True, profit_atr_mult=profit_atr_mult, min_gap_hours=min_gap_hours
    )


def _make_engine2(*, weekend=None, eod=None, **overrides) -> BacktestEngine:
    """Engine with independent weekend/eod trigger configs (None = off).

    Brackets are extremely wide (50xATR) so a multi-day synthetic uptrend
    cannot hit TP — only the flatten triggers (or nothing) close positions.
    """
    kwargs = dict(
        tp_atr_mult=50.0,
        sl_atr_mult=50.0,
        slippage_per_side=TICK,
        commission_per_side=2.50,
        contract_multiplier=1000.0,
        max_horizon=500,
        weekend_flatten=weekend,
        eod_flatten=eod,
    )
    kwargs.update(overrides)
    engine = BacktestEngine(**kwargs)
    engine._reset_state()
    return engine


class TestParseEodFlatten:
    def test_absent_block_returns_none(self):
        assert parse_eod_flatten({"nickname": "x"}) is None

    def test_enabled_requires_profit_atr_mult(self):
        with pytest.raises(ValueError, match="profit_atr_mult"):
            parse_eod_flatten({"eod_flatten": {"enabled": True}})

    def test_default_min_gap_is_two_hours(self):
        ef = parse_eod_flatten(
            {"eod_flatten": {"enabled": True, "profit_atr_mult": 1.0}}
        )
        assert ef.min_gap_hours == 2.0

    def test_disabled_block_is_inert(self):
        ef = parse_eod_flatten({"eod_flatten": {"enabled": False}})
        assert ef is not None and ef.enabled is False

    def test_weekend_parse_unaffected(self):
        """eod_flatten block must not leak into parse_weekend_flatten."""
        cfg = {"eod_flatten": {"enabled": True, "profit_atr_mult": 1.0}}
        assert parse_weekend_flatten(cfg) is None


class TestEodUnitTriggers:
    def test_eod_fires_on_eod_bar_for_winner(self):
        engine = _make_engine2(eod=_flatten_cfg(1.0, 2.0),
                               tp_atr_mult=2.0, sl_atr_mult=1.0)
        engine._eod_flatten_bars = {FLATTEN_DT}
        _enter_long(engine)

        entry_fill, atr = engine._entry_fill, engine._atr_at_entry
        bar_open = entry_fill + 1.4 * atr
        high = min(bar_open + 0.02, engine._tp_price - 0.02)
        low = max(bar_open - 0.02, engine._sl_price + 0.02)
        engine._on_in_position(FLATTEN_DT, bar_open, high, low)

        trade = engine._trades[-1]
        assert trade.exit_reason == ExitReason.EOD_FLATTEN
        assert trade.exit_price == pytest.approx(bar_open, abs=1e-9)

    def test_eod_does_not_fire_on_weekend_set(self):
        """The weekend and EOD bar-sets are independent: a bar in the WEEKEND
        set must not trigger an EOD-only engine."""
        engine = _make_engine2(eod=_flatten_cfg(0.0, 2.0))
        engine._flatten_bars = {FLATTEN_DT}   # weekend set only
        _enter_long(engine)

        entry_fill, atr = engine._entry_fill, engine._atr_at_entry
        bar_open = entry_fill + 2.0 * atr
        engine._on_in_position(FLATTEN_DT, bar_open, bar_open + 0.02, bar_open - 0.02)

        assert engine._state == TradeState.IN_POSITION
        assert engine._trades == []

    def test_tp_beats_eod_same_bar(self):
        engine = _make_engine2(eod=_flatten_cfg(0.0, 2.0),
                               tp_atr_mult=2.0, sl_atr_mult=1.0)
        engine._eod_flatten_bars = {FLATTEN_DT}
        _enter_long(engine)

        engine._on_in_position(FLATTEN_DT, engine._entry_fill + 0.1,
                               engine._tp_price + 0.10, engine._entry_fill)

        assert engine._trades[-1].exit_reason == ExitReason.TP

    def test_eod_close_does_not_reset_cooldown_counter(self):
        """re-adjudicated: trailing-sl-no-cooldown_07222026_2050 — only an
        ORIGINAL SL arms the cooldown. An EOD_FLATTEN close (a profitable
        winner flattened before the halt) must leave last_exit_bars_ago
        untouched, exactly like TP/TRAILING_BE/TIME_BARRIER."""
        engine = _make_engine2(eod=_flatten_cfg(0.0, 2.0))
        engine._eod_flatten_bars = {FLATTEN_DT}
        _enter_long(engine)

        bar_open = engine._entry_fill + 1.0
        engine._on_in_position(FLATTEN_DT, bar_open, bar_open + 0.02, bar_open - 0.02)

        assert engine._trades[-1].exit_reason == ExitReason.EOD_FLATTEN
        assert engine._engine_state.last_exit_bars_ago_long == 9999, (
            "EOD_FLATTEN must NOT arm the cooldown under the "
            "only-original-SL rule"
        )

    def test_concurrent_check_position_fires_eod(self):
        engine = _make_engine2(eod=_flatten_cfg(1.0, 2.0))
        engine._eod_flatten_bars = {FLATTEN_DT}
        pos = _OpenPosition(
            entry_dt=ENTRY_DT, entry_price=70.0, entry_fill=70.0,
            atr_at_entry=ATR, side=1, tp_price=170.0, sl_price=-30.0,
            original_sl_price=-30.0,
        )
        bar_open = 70.0 + 1.4 * ATR
        rec = engine._check_position(pos, FLATTEN_DT, bar_open,
                                     bar_open + 0.02, bar_open - 0.02)
        assert rec is not None and rec.exit_reason == ExitReason.EOD_FLATTEN


def _week_gap_data():
    """Mon–Fri with a daily 4h halt (15:00→19:00) plus a real weekend gap.

    Bars: each day 00:00–15:00 then 19:00–23:00; Fri 23:00 → Mon 00:00 is the
    weekend gap (49h). Gentle uptrend; wide brackets in _make_engine2 keep the
    position open so only flatten triggers can close it.
    Signals: long Tue 10:00 (past ATR_14 warm-up; hits Tue's EOD bar first)
    and long Fri 20:00 (hits the weekend bar first).
    """
    days = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]
    ts: list = []
    for d in days:
        ts += list(pd.date_range(f"{d} 00:00", f"{d} 15:00", freq="h"))
        ts += list(pd.date_range(f"{d} 19:00", f"{d} 23:00", freq="h"))
    ts += list(pd.date_range("2026-06-08 00:00", periods=8, freq="h"))  # Monday
    idx = pd.DatetimeIndex(ts)
    n = len(idx)
    close = 100.0 + 0.05 * np.arange(n)
    ohlcv = pd.DataFrame(
        {"Open": close - 0.02, "High": close + 0.10, "Low": close - 0.10,
         "Close": close, "Volume": 1000.0},
        index=idx,
    )
    signals = pd.DataFrame(
        {"side": [1, 1]},
        index=[pd.Timestamp("2026-06-02 10:00:00"),
               pd.Timestamp("2026-06-05 20:00:00")],
    )
    return signals, ohlcv


EOD_EXIT_DT = pd.Timestamp("2026-06-02 15:00:00")      # Tuesday pre-halt bar
WKD_EXIT_DT = pd.Timestamp("2026-06-05 23:00:00")      # Friday pre-weekend bar


class TestEodRunLevel:
    def test_eod_only_flattens_daily_halt_not_weekend(self):
        signals, ohlcv = _week_gap_data()
        engine = _make_engine2(eod=_flatten_cfg(0.0, 2.0))
        result = _run(engine, signals, ohlcv)

        # Trade 1 flattens at Tuesday's pre-halt bar. Trade 2 (entered Friday
        # evening) must NOT be flattened at the weekend bar by the EOD trigger
        # (band exclusivity) — it stays open, so only 1 closed trade.
        assert result.trade_count == 1
        t = result.trades[0]
        assert t.exit_reason == ExitReason.EOD_FLATTEN
        assert t.exit_dt == EOD_EXIT_DT
        assert WKD_EXIT_DT not in engine._eod_flatten_bars
        assert EOD_EXIT_DT in engine._eod_flatten_bars

    def test_weekend_only_ignores_daily_halts(self):
        signals, ohlcv = _week_gap_data()
        engine = _make_engine2(weekend=_flatten_cfg(0.0, 40.0))
        result = _run(engine, signals, ohlcv)

        # Trade 1 survives every daily halt and flattens at the weekend bar.
        assert result.trade_count == 1
        t = result.trades[0]
        assert t.exit_reason == ExitReason.WEEKEND_FLATTEN
        assert t.exit_dt == WKD_EXIT_DT

    def test_both_triggers_label_their_own_bars(self):
        signals, ohlcv = _week_gap_data()
        engine = _make_engine2(weekend=_flatten_cfg(0.0, 40.0),
                               eod=_flatten_cfg(0.0, 2.0))
        result = _run(engine, signals, ohlcv)

        assert result.trade_count == 2
        assert result.trades[0].exit_reason == ExitReason.EOD_FLATTEN
        assert result.trades[0].exit_dt == EOD_EXIT_DT
        assert result.trades[1].exit_reason == ExitReason.WEEKEND_FLATTEN
        assert result.trades[1].exit_dt == WKD_EXIT_DT

    def test_eod_absent_vs_disabled_byte_identical(self):
        signals, ohlcv = _week_gap_data()
        eng_none = _make_engine2()  # both triggers off
        eng_dis = _make_engine2(
            eod=WeekendFlattenConfig(enabled=False, profit_atr_mult=0.0,
                                     min_gap_hours=2.0)
        )
        r_none = _run(eng_none, signals, ohlcv)
        r_dis = _run(eng_dis, signals, ohlcv)
        pd.testing.assert_frame_equal(r_none.to_dataframe(), r_dis.to_dataframe())

    def test_eod_min_gap_at_or_above_weekend_threshold_raises(self):
        signals, ohlcv = _week_gap_data()
        engine = _make_engine2(weekend=_flatten_cfg(0.0, 40.0),
                               eod=_flatten_cfg(0.0, 40.0))
        with pytest.raises(ValueError, match="min_gap_hours"):
            _run(engine, signals, ohlcv)
