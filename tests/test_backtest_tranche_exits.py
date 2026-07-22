"""
TDD-TESTER AUTHORIZATION
Target Implementation File: agent/backtest_engine.py, src/live_execution/strategies/execution_models.py
Target Class/Function: allocate_tranche_lots, BacktestEngine (multi-tranche tiered_exits)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket ID: backtest-tranche-exits_07222026_1608
Blueprint: .agents/collab/tickets/backtest-tranche-exits_07222026_1608/blueprint.md

Contract under test (blueprint S1-S5):
- Group 1:  shared allocator allocate_tranche_lots() == LIVE allocator
            (live_trader.py _place_bracket_children_on_fill lot loop:
            non-last rung = min(max(1, int(round(lots*pct))), remaining);
            last rung = remainder; zero-lot rungs skipped).
- Groups 2-8: engine books ONE TradeRecord per position with per-tranche
            economics, final-event exit_reason, lots-weighted exit
            price/fill, and a tranche_exits list of
            {lots, exit_dt, exit_price, exit_fill, reason} dicts.
- Group 9:  config-time validation (qty_pct sum, strictly increasing
            tp_atr_mult, multi-rung + concurrent refusal).
- Group 10: identity fences — absent / single-rung tiered_exits must stay
            byte-identical to the pre-change engine (golden literals).

Fixture style mirrors tests/test_trailing_ladder.py and
tests/test_backtest_engine.py (synthetic flat OHLCV, ATR_14 pre-stamped so
ATR-at-entry is exactly 0.02, TieredEnsembleStrategy configs driven through
BacktestEngine.from_config with prob_Buy signals).

Economics used throughout (chosen for clean hand-computation):
    slippage_per_side=0.01, commission_per_side=2.5, contract_multiplier=1000
    entry close 65.0 -> entry fill 65.01 (Buy pays +slippage)
    long exit fills are Sell: fill = price - 0.01
    2-rung ladder [(0.5, 2.0), (0.5, 4.0)]:
        TP1 = round(65.01 + 2.0*0.02, 2) = 65.05
        TP2 = round(65.01 + 4.0*0.02, 2) = 65.09
    sl_atr_mult=1.5 -> SL = round(65.01 - 1.5*0.02, 2) = 64.98
    tier lots=3 -> live allocator [2, 1]
"""

from __future__ import annotations

import pandas as pd
import pytest

from agent.backtest_engine import BacktestEngine, ExitReason


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ATR = 0.02
SLIP = 0.01
COMM = 2.5
MULT = 1000.0

ENTRY_BAR = 20
ENTRY_PRICE = 65.0
ENTRY_FILL = 65.01          # 65.0 + slippage (Buy)

RUNGS_2 = [(0.5, 2.0), (0.5, 4.0)]   # (qty_pct, tp_atr_mult)
TP1 = 65.05                 # round(65.01 + 2.0*0.02, 2)
TP2 = 65.09                 # round(65.01 + 4.0*0.02, 2)
SL_PRICE = 64.98            # round(65.01 - 1.5*0.02, 2)

# Unified trade-ledger schema — MUST stay unchanged (no tranche column).
LEDGER_COLUMNS = [
    "entry_time", "signal_side", "entry_price", "entry_fill",
    "initial_tp_price", "initial_sl_price", "exit_time", "exit_price",
    "exit_fill", "exit_reason", "atr_at_entry", "duration_bars", "lots",
    "gross_pnl_dollars", "commission_dollars", "net_pnl_dollars",
]

TRANCHE_KEYS = {"lots", "exit_dt", "exit_price", "exit_fill", "reason"}


def _sell_fill(price: float) -> float:
    """Long-exit fill: Sell order pays slippage downward."""
    return price - SLIP


def _dollars(x: float):
    return pytest.approx(x, abs=1e-6)


def _price(x: float):
    return pytest.approx(x, abs=1e-9)


def _reason_str(r) -> str:
    """Normalize a tranche reason (ExitReason enum or its string value)."""
    return r.value if isinstance(r, ExitReason) else str(r)


def _allocate(total_lots: int, qty_pcts: list) -> list:
    """Ghost import — Coder implements this in execution_models.py."""
    from src.live_execution.strategies.execution_models import (
        allocate_tranche_lots,
    )
    return allocate_tranche_lots(total_lots, qty_pcts)


# ---------------------------------------------------------------------------
# Fixture helpers (mirror tests/test_trailing_ladder.py conventions)
# ---------------------------------------------------------------------------


def _make_ohlcv(prices, highs, lows, opens, index=None) -> pd.DataFrame:
    """Synthetic OHLCV with ATR_14 pre-stamped to a constant 0.02.

    Stamping ATR_14 takes the engine's attach_atr_cache fast path so
    ATR-at-entry is EXACTLY 0.02 (no rolling warm-up dependency).
    """
    n = len(prices)
    idx = index if index is not None else pd.date_range(
        "2026-01-01", periods=n, freq="5min"
    )
    return pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": prices,
            "Volume": [100.0] * n,
            "ATR_14": [ATR] * n,
        },
        index=idx,
    )


def _flat_path(n: int = 40):
    """Flat 65.0 tape: never touches TP1 (65.05) or SL (64.98)."""
    return [65.0] * n, [65.01] * n, [64.99] * n, [65.0] * n


def _prob_signals(ohlcv: pd.DataFrame, probs: dict) -> pd.DataFrame:
    """Full-length prob_Buy column with values at the given bar indices."""
    col = [0.0] * len(ohlcv)
    for i, p in probs.items():
        col[i] = p
    return pd.DataFrame({"prob_Buy": col}, index=ohlcv.index)


def _tranche_cfg(rungs, *, lots=3, sl=1.5, max_hold=288,
                 trailing=None, trailing_offset=None, extra_top=None) -> dict:
    """Production-shaped TieredEnsembleStrategy config with a long-side
    tiered_exits ladder (same shape as configs/strategies/ensemble_*.json:
    side tp_atr_mult == tiers[0].tp_atr_mult == tiered_exits[0].tp_atr_mult).
    """
    rung1_tp = rungs[0][1]
    side = {
        "tp_atr_mult": rung1_tp,
        "sl_atr_mult": sl,
        "tiered_exits": [
            {"qty_pct": p, "tp_atr_mult": m} for (p, m) in rungs
        ],
        "tiers": [
            {"min_prob": 0.60, "lots": lots, "tp_atr_mult": rung1_tp,
             "sl_atr_mult": sl, "max_hold_bars": max_hold, "label": "t1"},
        ],
        "max_hold_bars": max_hold,
        "cooldown_bars": 0,
        "consecutive_signal_threshold": 0,
    }
    if trailing is not None:
        side["trailing_atr_mult"] = trailing
        side["trailing_sl_atr_offset"] = trailing_offset
        side["tiers"][0]["trailing_atr_mult"] = trailing
    cfg = {
        "nickname": "TrancheTest",
        "execution_class": "TieredEnsembleStrategy",
        "long": side,
        "short": {},
        "tp_atr_mult": rung1_tp,
        "sl_atr_mult": sl,
        "max_hold_bars": max_hold,
    }
    if trailing is not None:
        cfg["trailing_atr_mult"] = trailing
        cfg["trailing_sl_atr_offset"] = trailing_offset
    if extra_top:
        cfg.update(extra_top)
    return cfg


def _no_rungs_cfg(*, lots=2, tp=2.0, sl=1.5, max_hold=288) -> dict:
    """Same shape WITHOUT tiered_exits (identity-fence config (a))."""
    return {
        "nickname": "TrancheTest",
        "execution_class": "TieredEnsembleStrategy",
        "long": {
            "tp_atr_mult": tp,
            "sl_atr_mult": sl,
            "tiers": [
                {"min_prob": 0.60, "lots": lots, "tp_atr_mult": tp,
                 "sl_atr_mult": sl, "max_hold_bars": max_hold, "label": "t1"},
            ],
            "max_hold_bars": max_hold,
            "cooldown_bars": 0,
            "consecutive_signal_threshold": 0,
        },
        "short": {},
        "tp_atr_mult": tp,
        "sl_atr_mult": sl,
        "max_hold_bars": max_hold,
    }


def _engine(cfg: dict) -> BacktestEngine:
    return BacktestEngine.from_config(
        cfg,
        commission_per_side=COMM,
        slippage_per_side=SLIP,
        contract_multiplier=MULT,
    )


def _run(cfg: dict, ohlcv: pd.DataFrame, probs=None):
    signals = _prob_signals(ohlcv, probs if probs is not None else {ENTRY_BAR: 0.70})
    return _engine(cfg).run(signals, ohlcv)


# ---------------------------------------------------------------------------
# Group 1 — Allocator truth table (live-equivalence pins)
# ---------------------------------------------------------------------------


# Hand-traced from the LIVE allocator loop (live_trader.py
# _place_bracket_children_on_fill): non-last rung
# min(max(1, int(round(lots*pct))), remaining); last rung = remainder;
# zero-lot rungs skipped.  int(round()) is Python banker's rounding —
# hence lots=5 x [0.5,0.5] -> [2, 3] (round(2.5) == 2), NOT [3, 2].
# Table verified against a replica of the live loop on 2026-07-22.
ALLOCATOR_TRUTH_TABLE = [
    (1, [1.0], [1]),
    (2, [1.0], [2]),
    (3, [1.0], [3]),
    (4, [1.0], [4]),
    (5, [1.0], [5]),
    (1, [0.5, 0.5], [1]),
    (2, [0.5, 0.5], [1, 1]),
    (3, [0.5, 0.5], [2, 1]),          # round(1.5) == 2 (banker's)
    (4, [0.5, 0.5], [2, 2]),
    (5, [0.5, 0.5], [2, 3]),          # round(2.5) == 2 (banker's)
    (1, [0.33, 0.33, 0.34], [1]),
    (2, [0.33, 0.33, 0.34], [1, 1]),
    (3, [0.33, 0.33, 0.34], [1, 1, 1]),
    (4, [0.33, 0.33, 0.34], [1, 1, 2]),
    (5, [0.33, 0.33, 0.34], [2, 2, 1]),
    (1, [0.6, 0.4], [1]),
    (2, [0.6, 0.4], [1, 1]),
    (3, [0.6, 0.4], [2, 1]),
    (4, [0.6, 0.4], [2, 2]),
    (5, [0.6, 0.4], [3, 2]),
]


class TestAllocatorTruthTable:
    """allocate_tranche_lots must reproduce the LIVE allocator exactly."""

    @pytest.mark.parametrize("lots,pcts,expected", ALLOCATOR_TRUTH_TABLE)
    def test_matches_live_allocator(self, lots, pcts, expected) -> None:
        result = _allocate(lots, pcts)
        assert result == expected
        # Conservation invariants: every lot allocated, no zero tranches.
        assert sum(result) == lots
        assert all(t >= 1 for t in result)

    def test_zero_lot_rungs_are_skipped(self) -> None:
        """lots=1 x [0.5, 0.5]: rung1 takes the only lot, rung2 (0) skipped."""
        assert _allocate(1, [0.5, 0.5]) == [1]
        assert len(_allocate(1, [0.5, 0.5])) == 1

    def test_third_rung_skipped_when_exhausted(self) -> None:
        """lots=2 x [0.33,0.33,0.34]: rungs 1+2 take 1 each, rung3 skipped."""
        assert _allocate(2, [0.33, 0.33, 0.34]) == [1, 1]

    def test_last_rung_takes_remainder(self) -> None:
        """Small early pcts leave the bulk on the last rung."""
        assert _allocate(7, [0.2, 0.2, 0.6]) == [1, 1, 5]


# ---------------------------------------------------------------------------
# Group 2 — Two-rung long: rung1 fills, remainder later stopped
# ---------------------------------------------------------------------------


def _group2_fixture() -> pd.DataFrame:
    """Entry bar 20; bar 25 high 65.06 fills rung1 (2 lots @ TP1 65.05);
    bar 30 low 64.97 stops the 1-lot remainder at SL 64.98."""
    prices, highs, lows, opens = _flat_path()
    highs[25] = 65.06
    lows[30] = 64.97
    return _make_ohlcv(prices, highs, lows, opens)


class TestTwoRungPartialThenStop:
    def test_one_record_final_reason_sl_tranche_economics(self) -> None:
        ohlcv = _group2_fixture()
        result = _run(_tranche_cfg(RUNGS_2, lots=3), ohlcv)

        assert result.trade_count == 1
        t = result.trades[0]

        # Final closing event drives the record identity.
        assert t.exit_reason == ExitReason.SL
        assert t.exit_dt == ohlcv.index[30]
        assert t.duration_bars == 10
        assert t.side == 1
        assert t.lots == 3
        assert t.entry_dt == ohlcv.index[ENTRY_BAR]
        assert t.entry_price == _price(ENTRY_PRICE)
        assert t.entry_fill == _price(ENTRY_FILL)
        assert t.atr_at_entry == _price(ATR)

        # Gross = sum of per-tranche PnLs (S3):
        #   rung1: (65.04 - 65.01) * 1000 * 2 = +60
        #   SL:    (64.97 - 65.01) * 1000 * 1 = -40
        expected_gross = (
            (_sell_fill(TP1) - ENTRY_FILL) * MULT * 2
            + (_sell_fill(SL_PRICE) - ENTRY_FILL) * MULT * 1
        )
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ +20
        assert t.commission_dollars == 2 * COMM * 3              # 15.0 total
        assert t.net_pnl_dollars == _dollars(expected_gross - 15.0)

        # Lots-weighted exit price (pre-slippage) and fill (post-slippage).
        assert t.exit_price == _price((2 * TP1 + 1 * SL_PRICE) / 3)
        assert t.exit_fill == _price(
            (2 * _sell_fill(TP1) + 1 * _sell_fill(SL_PRICE)) / 3
        )

    def test_tranche_exits_two_entries(self) -> None:
        ohlcv = _group2_fixture()
        result = _run(_tranche_cfg(RUNGS_2, lots=3), ohlcv)
        t = result.trades[0]

        tranches = t.tranche_exits
        assert isinstance(tranches, list)
        assert len(tranches) == 2

        first, second = tranches
        assert set(first.keys()) == TRANCHE_KEYS
        assert set(second.keys()) == TRANCHE_KEYS

        assert first["lots"] == 2
        assert first["exit_dt"] == ohlcv.index[25]
        assert first["exit_price"] == _price(TP1)
        assert first["exit_fill"] == _price(_sell_fill(TP1))
        assert _reason_str(first["reason"]) == "TP"

        assert second["lots"] == 1
        assert second["exit_dt"] == ohlcv.index[30]
        assert second["exit_price"] == _price(SL_PRICE)
        assert second["exit_fill"] == _price(_sell_fill(SL_PRICE))
        assert _reason_str(second["reason"]) == "SL"

    def test_on_exit_fires_once_with_final_reason(self) -> None:
        """S3: on_exit/cooldown fire ONCE, at the final close, final reason."""
        ohlcv = _group2_fixture()
        engine = _engine(_tranche_cfg(RUNGS_2, lots=3))
        calls = []
        engine._execution_strategy.on_exit = (
            lambda side, reason, bars_held: calls.append(
                (side, reason, bars_held)
            )
        )
        engine.run(_prob_signals(ohlcv, {ENTRY_BAR: 0.70}), ohlcv)
        assert calls == [(1, ExitReason.SL, 10)]


# ---------------------------------------------------------------------------
# Group 3 — Ladder completion (rungs fill on different bars)
# ---------------------------------------------------------------------------


class TestLadderCompletion:
    def test_two_rung_completion_weighted_averages(self) -> None:
        """rung1 bar 25 (65.06), rung2 bar 28 (65.10 >= TP2 65.09) -> TP."""
        prices, highs, lows, opens = _flat_path()
        highs[25] = 65.06
        highs[28] = 65.10
        ohlcv = _make_ohlcv(prices, highs, lows, opens)

        result = _run(_tranche_cfg(RUNGS_2, lots=2), ohlcv)  # alloc [1, 1]
        assert result.trade_count == 1
        t = result.trades[0]

        assert t.exit_reason == ExitReason.TP
        assert t.exit_dt == ohlcv.index[28]
        assert t.duration_bars == 8
        assert t.lots == 2

        expected_gross = (
            (_sell_fill(TP1) - ENTRY_FILL) * MULT
            + (_sell_fill(TP2) - ENTRY_FILL) * MULT
        )
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ +100
        assert t.commission_dollars == 2 * COMM * 2              # 10.0
        assert t.net_pnl_dollars == _dollars(expected_gross - 10.0)

        assert t.exit_price == _price((TP1 + TP2) / 2)           # 65.07
        assert t.exit_fill == _price(
            (_sell_fill(TP1) + _sell_fill(TP2)) / 2
        )

        tranches = t.tranche_exits
        assert len(tranches) == 2
        assert tranches[0]["exit_dt"] == ohlcv.index[25]
        assert tranches[1]["exit_dt"] == ohlcv.index[28]
        assert [x["lots"] for x in tranches] == [1, 1]
        assert [_reason_str(x["reason"]) for x in tranches] == ["TP", "TP"]

    def test_three_rung_completion(self) -> None:
        """3-rung ladder [(0.4,1.0),(0.3,2.0),(0.3,3.0)], lots=5 -> [2,2,1].

        TPs: 65.03 / 65.05 / 65.07 (entry fill 65.01, ATR 0.02).
        Bars 24 / 26 / 28 fill rungs 1 / 2 / 3 respectively.
        """
        rungs = [(0.4, 1.0), (0.3, 2.0), (0.3, 3.0)]
        tp_a, tp_b, tp_c = 65.03, 65.05, 65.07
        prices, highs, lows, opens = _flat_path()
        highs[24] = 65.035
        highs[26] = 65.055
        highs[28] = 65.075
        ohlcv = _make_ohlcv(prices, highs, lows, opens)

        result = _run(_tranche_cfg(rungs, lots=5), ohlcv)
        assert result.trade_count == 1
        t = result.trades[0]

        assert t.exit_reason == ExitReason.TP
        assert t.exit_dt == ohlcv.index[28]
        assert t.duration_bars == 8
        assert t.lots == 5

        expected_gross = (
            (_sell_fill(tp_a) - ENTRY_FILL) * MULT * 2
            + (_sell_fill(tp_b) - ENTRY_FILL) * MULT * 2
            + (_sell_fill(tp_c) - ENTRY_FILL) * MULT * 1
        )
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ +130
        assert t.commission_dollars == 2 * COMM * 5              # 25.0
        assert t.net_pnl_dollars == _dollars(expected_gross - 25.0)
        assert t.exit_price == _price((2 * tp_a + 2 * tp_b + tp_c) / 5)

        tranches = t.tranche_exits
        assert [x["lots"] for x in tranches] == [2, 2, 1]
        assert [x["exit_dt"] for x in tranches] == [
            ohlcv.index[24], ohlcv.index[26], ohlcv.index[28]
        ]


# ---------------------------------------------------------------------------
# Group 4 — Same-bar pessimism: SL beats rungs breached on the same bar
# ---------------------------------------------------------------------------


class TestSameBarPessimism:
    def test_sl_beats_unfilled_rung_after_partial(self) -> None:
        """rung1 filled bar 25; bar 27 breaches BOTH rung2 and SL ->
        the whole 1-lot remainder closes at the SL gap price, rung2 void."""
        prices, highs, lows, opens = _flat_path()
        highs[25] = 65.06
        highs[27] = 65.10      # >= TP2 65.09
        lows[27] = 64.97       # <= SL 64.98 — SL wins
        ohlcv = _make_ohlcv(prices, highs, lows, opens)

        result = _run(_tranche_cfg(RUNGS_2, lots=3), ohlcv)
        assert result.trade_count == 1
        t = result.trades[0]

        assert t.exit_reason == ExitReason.SL
        assert t.exit_dt == ohlcv.index[27]
        assert t.duration_bars == 7

        expected_gross = (
            (_sell_fill(TP1) - ENTRY_FILL) * MULT * 2
            + (_sell_fill(SL_PRICE) - ENTRY_FILL) * MULT * 1
        )
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ +20
        assert t.net_pnl_dollars == _dollars(expected_gross - 15.0)
        assert t.exit_price == _price((2 * TP1 + SL_PRICE) / 3)

        tranches = t.tranche_exits
        assert len(tranches) == 2                    # rung2 VOID — no entry
        assert tranches[1]["lots"] == 1
        assert tranches[1]["exit_price"] == _price(SL_PRICE)
        assert _reason_str(tranches[1]["reason"]) == "SL"

    def test_sl_beats_rungs_no_prior_fill(self) -> None:
        """No prior fill; one bar breaches both rungs AND the SL -> the whole
        position closes at the SL gap price (identical totals to the current
        single-path pessimism — this pins the convention post-change too).
        """
        prices, highs, lows, opens = _flat_path()
        highs[25] = 65.10
        lows[25] = 64.90
        ohlcv = _make_ohlcv(prices, highs, lows, opens)

        result = _run(_tranche_cfg(RUNGS_2, lots=3), ohlcv)
        assert result.trade_count == 1
        t = result.trades[0]

        assert t.exit_reason == ExitReason.SL
        assert t.exit_dt == ohlcv.index[25]
        assert t.duration_bars == 5
        assert t.lots == 3
        assert t.exit_price == _price(SL_PRICE)
        assert t.exit_fill == _price(_sell_fill(SL_PRICE))
        expected_gross = (_sell_fill(SL_PRICE) - ENTRY_FILL) * MULT * 3
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ -120
        assert t.net_pnl_dollars == _dollars(expected_gross - 15.0)
        # tranche_exits shape for an all-at-once close of a multi-tranche
        # position is NOT pinned here (blueprint leaves it to S3 totals).


# ---------------------------------------------------------------------------
# Group 5 — Gap-open fills: each rung fills at max(open, its TP)
# ---------------------------------------------------------------------------


class TestGapOpenFills:
    def test_gap_through_both_rungs_fills_both_at_open(self) -> None:
        """Bar 25 opens at 65.12 > TP2 > TP1: BOTH rungs fill at the open."""
        prices, highs, lows, opens = _flat_path()
        opens[25] = 65.12
        highs[25] = 65.13
        lows[25] = 65.07
        prices[25] = 65.10
        ohlcv = _make_ohlcv(prices, highs, lows, opens)

        result = _run(_tranche_cfg(RUNGS_2, lots=3), ohlcv)
        assert result.trade_count == 1
        t = result.trades[0]

        # tranche_exits FIRST — totals coincide with the old single path on
        # this fixture, the per-tranche split is the new contract.
        tranches = t.tranche_exits
        assert len(tranches) == 2
        assert [x["lots"] for x in tranches] == [2, 1]
        assert tranches[0]["exit_price"] == _price(65.12)
        assert tranches[1]["exit_price"] == _price(65.12)
        assert [_reason_str(x["reason"]) for x in tranches] == ["TP", "TP"]

        assert t.exit_reason == ExitReason.TP
        assert t.exit_dt == ohlcv.index[25]
        assert t.exit_price == _price(65.12)
        assert t.exit_fill == _price(_sell_fill(65.12))
        expected_gross = (_sell_fill(65.12) - ENTRY_FILL) * MULT * 3
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ +300
        assert t.net_pnl_dollars == _dollars(expected_gross - 15.0)

    def test_gap_between_rungs_each_rung_own_price(self) -> None:
        """Bar 25 opens at 65.07 (past TP1, below TP2), high 65.10:
        rung1 fills at the OPEN (65.07), rung2 at its own TP (65.09)."""
        prices, highs, lows, opens = _flat_path()
        opens[25] = 65.07
        highs[25] = 65.10
        lows[25] = 65.02
        prices[25] = 65.08
        ohlcv = _make_ohlcv(prices, highs, lows, opens)

        result = _run(_tranche_cfg(RUNGS_2, lots=3), ohlcv)
        assert result.trade_count == 1
        t = result.trades[0]

        assert t.exit_reason == ExitReason.TP
        assert t.exit_dt == ohlcv.index[25]

        expected_gross = (
            (_sell_fill(65.07) - ENTRY_FILL) * MULT * 2   # rung1 @ open
            + (_sell_fill(TP2) - ENTRY_FILL) * MULT * 1   # rung2 @ its TP
        )
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ +170
        assert t.net_pnl_dollars == _dollars(expected_gross - 15.0)
        assert t.exit_price == _price((2 * 65.07 + TP2) / 3)

        tranches = t.tranche_exits
        assert tranches[0]["exit_price"] == _price(65.07)
        assert tranches[1]["exit_price"] == _price(TP2)


# ---------------------------------------------------------------------------
# Group 6 — A rung fill consumes the bar: time barrier deferred
# ---------------------------------------------------------------------------


class TestFillConsumesBar:
    def test_rung_fill_on_barrier_bar_defers_barrier(self) -> None:
        """max_hold=5: barrier would fire on bar 26 (bars_held=6), but rung1
        fills there -> barrier deferred; remainder closes bar 27 at open."""
        prices, highs, lows, opens = _flat_path()
        highs[26] = 65.06
        ohlcv = _make_ohlcv(prices, highs, lows, opens)

        result = _run(_tranche_cfg(RUNGS_2, lots=3, max_hold=5), ohlcv)
        assert result.trade_count == 1
        t = result.trades[0]

        assert t.exit_reason == ExitReason.TIME_BARRIER
        assert t.exit_dt == ohlcv.index[27]
        assert t.duration_bars == 7

        expected_gross = (
            (_sell_fill(TP1) - ENTRY_FILL) * MULT * 2
            + (_sell_fill(65.0) - ENTRY_FILL) * MULT * 1   # bar-27 open
        )
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ +40
        assert t.net_pnl_dollars == _dollars(expected_gross - 15.0)

        tranches = t.tranche_exits
        assert len(tranches) == 2
        assert tranches[0]["exit_dt"] == ohlcv.index[26]
        assert _reason_str(tranches[0]["reason"]) == "TP"
        assert tranches[1]["exit_dt"] == ohlcv.index[27]
        assert tranches[1]["exit_price"] == _price(65.0)
        assert _reason_str(tranches[1]["reason"]) == "TIME_BARRIER"


# ---------------------------------------------------------------------------
# Group 7 — Time barrier / flatten overlays close the remainder at bar open
# ---------------------------------------------------------------------------


class TestOverlaysCloseRemainder:
    def test_time_barrier_closes_remainder_at_open(self) -> None:
        """rung1 fills bar 23; barrier bar 26 (max_hold=5) closes the
        remainder at the bar open with TIME_BARRIER."""
        prices, highs, lows, opens = _flat_path()
        highs[23] = 65.06
        ohlcv = _make_ohlcv(prices, highs, lows, opens)

        result = _run(_tranche_cfg(RUNGS_2, lots=3, max_hold=5), ohlcv)
        assert result.trade_count == 1
        t = result.trades[0]

        assert t.exit_reason == ExitReason.TIME_BARRIER
        assert t.exit_dt == ohlcv.index[26]
        assert t.duration_bars == 6

        expected_gross = (
            (_sell_fill(TP1) - ENTRY_FILL) * MULT * 2
            + (_sell_fill(65.0) - ENTRY_FILL) * MULT * 1
        )
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ +40
        assert t.net_pnl_dollars == _dollars(expected_gross - 15.0)
        assert t.exit_price == _price((2 * TP1 + 65.0) / 3)

    def test_weekend_flatten_closes_remainder_at_open(self) -> None:
        """rung1 fills bar 23; bar 26 is the last bar before a 48h gap and
        the remainder is >= 1 ATR in profit at its open (65.04) -> the
        remainder closes at the open with WEEKEND_FLATTEN."""
        idx = list(pd.date_range("2026-01-05", periods=27, freq="5min"))
        idx += list(pd.date_range(
            idx[-1] + pd.Timedelta(hours=48), periods=5, freq="5min"
        ))
        n = len(idx)
        prices, highs, lows, opens = _flat_path(n)
        highs[23] = 65.06
        opens[26] = 65.04      # unrealized (65.04-65.01)/0.02 = 1.5 ATR >= 1.0
        highs[26] = 65.045     # < TP2 — rung2 does not fill
        lows[26] = 65.0
        prices[26] = 65.02
        ohlcv = _make_ohlcv(prices, highs, lows, opens,
                            index=pd.DatetimeIndex(idx))

        cfg = _tranche_cfg(
            RUNGS_2, lots=3,
            extra_top={"weekend_flatten": {"enabled": True,
                                           "profit_atr_mult": 1.0}},
        )
        result = _run(cfg, ohlcv)
        assert result.trade_count == 1
        t = result.trades[0]

        assert t.exit_reason == ExitReason.WEEKEND_FLATTEN
        assert t.exit_dt == ohlcv.index[26]
        assert t.duration_bars == 6

        expected_gross = (
            (_sell_fill(TP1) - ENTRY_FILL) * MULT * 2
            + (_sell_fill(65.04) - ENTRY_FILL) * MULT * 1
        )
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ +80
        assert t.net_pnl_dollars == _dollars(expected_gross - 15.0)

        tranches = t.tranche_exits
        assert tranches[1]["exit_price"] == _price(65.04)
        assert _reason_str(tranches[1]["reason"]) == "WEEKEND_FLATTEN"


# ---------------------------------------------------------------------------
# Group 8 — Trailing ratchet protects the remainder; TRAILING_BE on its stop
# ---------------------------------------------------------------------------


class TestTrailingRemainder:
    def test_ratchet_after_fill_then_trailing_be_stop(self) -> None:
        """trailing (1.0, 0.5): activation 65.02, lock 65.01 (entry-anchored
        at 65.0 pre-slippage).  Bar 25 fills rung1 (65.06 also crosses the
        activation, but the FILL consumes the bar — no ratchet).  Bar 26 the
        ratchet advances off the running extreme (65.06) -> SL 65.01.  Bar 27
        dips to 65.005 -> the 1-lot remainder stops at 65.01, TRAILING_BE.
        """
        lock_price = ENTRY_PRICE + 0.5 * ATR   # 65.01
        prices, highs, lows, opens = _flat_path()
        opens[25] = 65.0
        highs[25] = 65.06
        lows[25] = 65.0
        # bar 26 stays flat: old SL 64.98 not touched; ratchet arms at close
        opens[27] = 65.02
        highs[27] = 65.03
        lows[27] = 65.005      # <= 65.01 ratcheted stop
        prices[27] = 65.01
        ohlcv = _make_ohlcv(prices, highs, lows, opens)

        result = _run(
            _tranche_cfg(RUNGS_2, lots=3, trailing=1.0, trailing_offset=0.5),
            ohlcv,
        )
        assert result.trade_count == 1
        t = result.trades[0]

        assert t.exit_reason == ExitReason.TRAILING_BE
        assert t.exit_dt == ohlcv.index[27]
        assert t.duration_bars == 7

        expected_gross = (
            (_sell_fill(TP1) - ENTRY_FILL) * MULT * 2
            + (_sell_fill(lock_price) - ENTRY_FILL) * MULT * 1
        )
        assert t.gross_pnl_dollars == _dollars(expected_gross)   # ~ +50
        assert t.net_pnl_dollars == _dollars(expected_gross - 15.0)
        assert t.exit_price == _price((2 * TP1 + lock_price) / 3)

        tranches = t.tranche_exits
        assert len(tranches) == 2
        assert tranches[0]["exit_dt"] == ohlcv.index[25]
        assert _reason_str(tranches[0]["reason"]) == "TP"
        assert tranches[1]["exit_price"] == _price(lock_price)
        assert _reason_str(tranches[1]["reason"]) == "TRAILING_BE"


# ---------------------------------------------------------------------------
# Group 9 — Config-time validation (S4)
# ---------------------------------------------------------------------------


class TestTrancheValidation:
    def test_qty_pct_sum_below_one_raises(self) -> None:
        cfg = _tranche_cfg([(0.5, 2.0), (0.4, 4.0)], lots=3)  # sum 0.9
        with pytest.raises(ValueError, match="qty_pct"):
            _engine(cfg)

    def test_non_increasing_tp_mult_raises(self) -> None:
        cfg = _tranche_cfg([(0.5, 2.0), (0.5, 1.5)], lots=3)
        with pytest.raises(ValueError, match="tp_atr_mult"):
            _engine(cfg)

    def test_equal_tp_mult_raises(self) -> None:
        """'Strictly increasing' — equal rung TPs are also invalid."""
        cfg = _tranche_cfg([(0.5, 2.0), (0.5, 2.0)], lots=3)
        with pytest.raises(ValueError, match="tp_atr_mult"):
            _engine(cfg)

    def test_multi_rung_concurrent_mode_raises(self) -> None:
        cfg = _tranche_cfg(
            RUNGS_2, lots=3,
            extra_top={"allow_concurrent": True, "max_concurrent": 2},
        )
        with pytest.raises(ValueError, match="single-position"):
            _engine(cfg)

    def test_single_rung_no_new_validation(self) -> None:
        """Single-rung tiered_exits (production shape): none of the new
        checks fire, the engine runs, and there is no tranche_exits."""
        cfg = _no_rungs_cfg()
        cfg["long"]["tiered_exits"] = [{"qty_pct": 1.0, "tp_atr_mult": 2.0}]

        prices, highs, lows, opens = _flat_path()
        highs[25] = 65.06
        ohlcv = _make_ohlcv(prices, highs, lows, opens)
        result = _run(cfg, ohlcv)

        assert result.trade_count == 1
        assert getattr(result.trades[0], "tranche_exits", None) is None

    def test_single_rung_concurrent_still_allowed(self) -> None:
        """Identity: single-rung + concurrent config must NOT raise."""
        cfg = _no_rungs_cfg()
        cfg["long"]["tiered_exits"] = [{"qty_pct": 1.0, "tp_atr_mult": 2.0}]
        cfg["allow_concurrent"] = True
        cfg["max_concurrent"] = 2
        _engine(cfg)  # no raise

    def test_absent_tiered_exits_concurrent_still_allowed(self) -> None:
        """Identity: absent tiered_exits + concurrent must NOT raise."""
        cfg = _no_rungs_cfg()
        cfg["allow_concurrent"] = True
        cfg["max_concurrent"] = 2
        _engine(cfg)  # no raise


# ---------------------------------------------------------------------------
# Group 10 — Identity fences (S5) — MUST pass before AND after the change
# ---------------------------------------------------------------------------
#
# Golden literals below were captured by RUNNING the pre-change engine
# (branch development, HEAD 7b5321c, 2026-07-22 — the current engine at
# test-authoring time; the blueprint's reference commit 619d3d2 predates
# unrelated commits that do not touch the engine).  Fixture: flat 65.0 tape,
# ATR_14 stamped 0.02, prob_Buy 0.70 at bars 20 and 30; bar 25 high 65.06
# (TP trade), bar 33 low 64.97 (SL trade); lots=2, tp 2.0, sl 1.5,
# commission 2.5/side, slippage 0.01, multiplier 1000.


def _fence_ohlcv() -> pd.DataFrame:
    prices, highs, lows, opens = _flat_path()
    highs[25] = 65.06
    lows[33] = 64.97
    return _make_ohlcv(prices, highs, lows, opens)


def _fence_cfg_no_tiered_exits() -> dict:
    return _no_rungs_cfg()


def _fence_cfg_single_rung() -> dict:
    cfg = _no_rungs_cfg()
    cfg["long"]["tiered_exits"] = [{"qty_pct": 1.0, "tp_atr_mult": 2.0}]
    return cfg


def _assert_fence_goldens(result) -> None:
    """Exact (bit-identical) golden values from the pre-change engine."""
    assert result.trade_count == 2
    t1, t2 = result.trades

    # Trade 1 — TP at bar 25.
    assert t1.entry_dt == pd.Timestamp("2026-01-01 01:40:00")
    assert t1.exit_dt == pd.Timestamp("2026-01-01 02:05:00")
    assert t1.entry_price == 65.0
    assert t1.entry_fill == 65.01
    assert t1.exit_price == 65.05
    assert t1.exit_fill == 65.03999999999999
    assert t1.side == 1
    assert t1.lots == 2
    assert t1.atr_at_entry == 0.02
    assert t1.exit_reason == ExitReason.TP
    assert t1.duration_bars == 5
    assert t1.gross_pnl_dollars == 59.99999999997385
    assert t1.commission_dollars == 10.0
    assert t1.net_pnl_dollars == 49.99999999997385
    assert t1.initial_tp_price == 65.05
    assert t1.initial_sl_price == 64.98
    assert getattr(t1, "tranche_exits", None) is None

    # Trade 2 — SL at bar 33.
    assert t2.entry_dt == pd.Timestamp("2026-01-01 02:30:00")
    assert t2.exit_dt == pd.Timestamp("2026-01-01 02:45:00")
    assert t2.entry_price == 65.0
    assert t2.entry_fill == 65.01
    assert t2.exit_price == 64.98
    assert t2.exit_fill == 64.97
    assert t2.side == 1
    assert t2.lots == 2
    assert t2.atr_at_entry == 0.02
    assert t2.exit_reason == ExitReason.SL
    assert t2.duration_bars == 3
    assert t2.gross_pnl_dollars == -80.0000000000125
    assert t2.commission_dollars == 10.0
    assert t2.net_pnl_dollars == -90.0000000000125
    assert t2.initial_tp_price == 65.05
    assert t2.initial_sl_price == 64.98
    assert getattr(t2, "tranche_exits", None) is None


class TestIdentityFences:
    def test_absent_tiered_exits_golden(self) -> None:
        result = _run(_fence_cfg_no_tiered_exits(), _fence_ohlcv(),
                      probs={20: 0.70, 30: 0.70})
        _assert_fence_goldens(result)

    def test_single_rung_tiered_exits_golden(self) -> None:
        result = _run(_fence_cfg_single_rung(), _fence_ohlcv(),
                      probs={20: 0.70, 30: 0.70})
        _assert_fence_goldens(result)

    def test_two_rung_config_one_lot_runs_single_path(self) -> None:
        """S1 activation fence: 2 rungs but lots=1 -> allocator yields a
        single tranche -> the EXISTING single-exit path (rung-1 TP), no
        tranche_exits.  Goldens captured pre-change (HEAD 7b5321c)."""
        prices, highs, lows, opens = _flat_path()
        highs[25] = 65.06
        ohlcv = _make_ohlcv(prices, highs, lows, opens)

        result = _run(_tranche_cfg(RUNGS_2, lots=1), ohlcv)
        assert result.trade_count == 1
        t = result.trades[0]
        assert t.exit_reason == ExitReason.TP
        assert t.exit_dt == ohlcv.index[25]
        assert t.lots == 1
        assert t.exit_price == 65.05
        assert t.exit_fill == 65.03999999999999
        assert t.gross_pnl_dollars == 29.999999999986926
        assert t.commission_dollars == 5.0
        assert t.net_pnl_dollars == 24.999999999986926
        assert getattr(t, "tranche_exits", None) is None

    def test_to_dataframe_columns_golden_fence_configs(self) -> None:
        """Ledger schema is frozen for absent AND single-rung configs."""
        for cfg in (_fence_cfg_no_tiered_exits(), _fence_cfg_single_rung()):
            result = _run(cfg, _fence_ohlcv(), probs={20: 0.70, 30: 0.70})
            assert list(result.to_dataframe().columns) == LEDGER_COLUMNS

    def test_to_dataframe_columns_unchanged_for_multi_tranche(self) -> None:
        """S3: tranche_exits is EXCLUDED from to_dataframe() — the unified
        ledger schema gains no column even for multi-tranche runs."""
        result = _run(_tranche_cfg(RUNGS_2, lots=3), _group2_fixture())
        df = result.to_dataframe()
        assert list(df.columns) == LEDGER_COLUMNS
