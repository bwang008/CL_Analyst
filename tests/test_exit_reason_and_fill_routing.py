"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._check_time_barrier, LiveTrader._on_standard_execution_event
Secondary Targets: src/live_execution/strategies/configurable_strategy.py (cooldown flavor
                   vocabulary), scripts/livetest_engine.py (duplicate child placement removal)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: exit-fill-routing-cooldown_07032026_0930

Defects D and E from the 2026-07-03 ledger-parity replay (see
.agents/collab/tickets/exit-fill-routing-cooldown_07032026_0930/blueprint.md):

D — exit-reason vocabulary gap:
  * The time-barrier exit must call _reset_position_state(reason="TIME_BARRIER")
    (not the default "CLOSED"), so ConfigurableStrategy applies sl_cooldown_bars
    exactly like the backtest does for TIME_BARRIER exits.
  * The out-of-band close path must pass reason="CLOSED_OOB".
  * ConfigurableStrategy must treat "CLOSED"/"CLOSED_OOB" as SL-flavored
    (conservative: unknown closes get the longer cooldown).

E — fill misrouting:
  * _on_standard_execution_event must NOT treat an unrecognized fill as an
    entry (today the else-branch places bracket children around an exit fill,
    because decision context is also stored under child order IDs).
    Entries must be identified via an explicit registry populated at order
    submission (self._entry_order_ids).
  * scripts/livetest_engine.py must not call trader._place_bracket_children_on_fill
    itself — live_trader's entry branch already places children on fill; the
    harness duplicate overwrote _tp_order_ids/_sl_order_id and orphaned the
    first child set.

Deterministic, no real I/O: LiveTrader/ConfigurableStrategy built via
object.__new__ stub pattern (see tests/test_live_trader_bugs.py,
tests/test_parity_cooldown_single_authority.py).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution.live_trader import LiveTrader
from src.live_execution.interfaces.execution_interface import StandardExecutionEvent
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trader() -> LiveTrader:
    """Minimal LiveTrader stub with the attributes the tested paths read."""
    t = LiveTrader.__new__(LiveTrader)
    t.exec_client = MagicMock()
    t.telemetry = MagicMock()
    t._telegram = MagicMock()
    t._execution_symbol = "CL"
    t._front_month_str = "202607"
    t._exit_mode = "MKT"
    t._open_orders = {}
    t._processed_exit_order_ids = set()
    t._processed_entry_order_ids = set()
    t._last_decision_context_by_order_id = {}
    t._tp_order_ids = []
    t._sl_order_id = None
    t._active_trade_id = None
    t._position_side = 0
    t._position_bars_held = 0
    t._position_entry_bar_time = None
    t._trade_max_hold_bars = None
    t._max_hold_bars = 240
    t._atr_at_entry = 0.5
    t._trade_trailing_atr_mult = 1.0
    t._pending_entry_order_id = None
    t._build_event_id = MagicMock(return_value="evt-test")
    t._base_tradebook_fields = MagicMock(return_value={})
    return t


def _filled_event(order_id: str, action: str = "SELL", price: float = 88.26) -> StandardExecutionEvent:
    raw_order = MagicMock()
    raw_order.action = action
    raw_order.permId = None
    raw_order.parentId = None
    raw_order.account = None
    raw = MagicMock()
    raw.order = raw_order
    raw.contract = MagicMock(symbol="CL")
    return StandardExecutionEvent(
        order_id=order_id,
        symbol="CL",
        status="Filled",
        filled_qty=1,
        remaining_qty=0,
        avg_price=price,
        raw_event=raw,
    )


def _make_strategy(config: dict) -> ConfigurableStrategy:
    """ConfigurableStrategy stub (pattern from test_parity_cooldown_single_authority)."""
    strat = object.__new__(ConfigurableStrategy)
    strat.config = config
    strat._nickname = "ExitReasonTest"
    strat._direction = "BOTH"
    strat.allow_concurrent = False
    strat._is_tiered = True
    strat._is_ensemble = False
    strat._long_learner = object()
    strat._short_learner = object()
    long_sentinel = strat._long_learner
    strat._run_inference = (
        lambda learner, features: 0.90 if learner is long_sentinel else 0.80
    )
    strat._execution_guard = None
    strat._last_exit_bars_ago_long = 9999
    strat._last_exit_bars_ago_short = 9999
    strat._last_exit_reason_long = ""
    strat._last_exit_reason_short = ""
    strat.exit_mode = "SINGLE"
    strat.tp_atr_mult = 2.0
    strat.sl_atr_mult = 1.0
    strat._long_tiered_exits = None
    strat._short_tiered_exits = None
    strat.base_quantity = 1
    exec_strategy = MagicMock()
    from src.live_execution.strategies.execution_models import Order
    exec_strategy.on_bar.return_value = [Order(action="HOLD", side=0, lots=0, reason="no_signal")]
    strat._exec_strategy = exec_strategy
    return strat


CFG_SL7 = {
    "nickname": "sl7",
    "sl_cooldown_bars": 7,
    "tp_cooldown_bars": 0,
    "long": {"cooldown_bars": 1},
    "short": {"cooldown_bars": 1},
}


# ---------------------------------------------------------------------------
# D — exit-reason vocabulary
# ---------------------------------------------------------------------------


class TestExitReasonVocabulary:
    def test_time_barrier_exit_passes_time_barrier_reason(self):
        """The time-barrier exit must reset with reason="TIME_BARRIER" so the
        strategy applies sl_cooldown_bars — matching the backtest's flavor
        (TIME_BARRIER exits are SL-flavored there)."""
        t = _make_trader()
        t.exec_client.get_position.return_value = 1
        t.exec_client.cancel_open_orders.return_value = 2
        t.exec_client.close_position.return_value = MagicMock()
        t._active_trade_id = "trade_1"
        t._position_entry_bar_time = pd.Timestamp("2026-06-01 00:00:00")
        t._position_bars_held = 6
        t._trade_max_hold_bars = 6
        t._reset_position_state = MagicMock()

        exited = t._check_time_barrier(
            bar_time=pd.Timestamp("2026-06-01 07:00:00"),
            current_price=90.0,
            atr_value=0.9,
        )

        assert exited is True
        t._reset_position_state.assert_called_once_with(reason="TIME_BARRIER")

    def test_oob_close_passes_closed_oob_reason(self):
        """The out-of-band close path must pass reason="CLOSED_OOB" (its
        telemetry already uses CLOSED_OOB) instead of the bare default."""
        t = _make_trader()
        t.exec_client.get_position.return_value = 0
        t.exec_client.get_position_settled.return_value = 0  # settled CONFIRMS flat
        t.exec_client.cancel_open_orders.return_value = 4
        t._active_trade_id = "trade_1015"
        t._position_bars_held = 6
        t._reset_position_state = MagicMock()

        exited = t._check_time_barrier(
            bar_time=pd.Timestamp("2026-05-26 13:00:00"),
            current_price=94.2,
            atr_value=1.0,
        )

        assert exited is False
        t._reset_position_state.assert_called_once_with(reason="CLOSED_OOB")

    @pytest.mark.parametrize("reason", ["CLOSED", "CLOSED_OOB"])
    def test_closed_reasons_get_sl_flavored_cooldown(self, reason):
        """After on_exit with a CLOSED-family reason, the side must be gated by
        sl_cooldown_bars (7), not tp_cooldown_bars (0). First evaluate() after
        the exit reads counter 1 <= 7 → buy side zeroed."""
        strat = _make_strategy(CFG_SL7)
        strat.on_exit(1, reason, 5)

        sig = strat.evaluate(pd.DataFrame(), 70.0, 0.5, 0)

        assert sig.buy_prob == 0.0, (
            f"reason={reason!r} must be SL-flavored (sl_cooldown_bars=7); "
            f"got buy_prob={sig.buy_prob} — it fell through to tp_cooldown_bars=0"
        )


# ---------------------------------------------------------------------------
# E — fill routing
# ---------------------------------------------------------------------------


class TestFillRouting:
    def test_unrecognized_fill_is_not_treated_as_entry(self, caplog):
        """A Filled event whose order id is neither a registered entry nor a
        tracked TP/SL order must NOT enter the entry branch — even when
        decision context exists under that id (context is also stored under
        child order ids for telemetry, live_trader.py:1742)."""
        t = _make_trader()
        t._entry_order_ids = {"100"}
        # Simulate an orphaned SL child: context present (parent's ctx copied
        # under the child id), but the id is not in _tp_order_ids/_sl_order_id.
        t._last_decision_context_by_order_id["555"] = {
            "tp_offset": 2.3, "sl_offset": 2.3, "entry_action": "BUY", "lots": 1,
        }
        t._place_bracket_children_on_fill = MagicMock()

        with caplog.at_level(logging.ERROR):
            t._on_standard_execution_event(_filled_event("555", action="SELL"))

        t._place_bracket_children_on_fill.assert_not_called()
        assert t._active_trade_id is None, (
            "unrecognized fill was booked as a new entry (misrouting)"
        )
        assert any("UNRECOGNIZED FILL" in rec.message for rec in caplog.records), (
            "expected a loud [TRADE] UNRECOGNIZED FILL error log"
        )

    def test_registered_entry_fill_still_processed_as_entry(self):
        """A fill for an order id registered at submission must run the entry
        branch: trade id booked and bracket children placed."""
        t = _make_trader()
        t._entry_order_ids = {"100"}
        t._last_decision_context_by_order_id[100] = {
            "tp_offset": 2.5, "sl_offset": 2.5, "entry_action": "BUY", "lots": 1,
        }
        t._place_bracket_children_on_fill = MagicMock()

        t._on_standard_execution_event(_filled_event("100", action="BUY", price=89.50))

        assert t._active_trade_id == "trade_100"
        t._place_bracket_children_on_fill.assert_called_once()

    def test_sl_fill_still_routes_to_exit_path(self):
        """Regression guard: a tracked SL fill keeps routing to the exit branch
        (software OCA + _reset_position_state with the real reason)."""
        t = _make_trader()
        t._entry_order_ids = {"100"}
        t._sl_order_id = 777
        t._active_trade_id = "trade_100"
        t._reset_position_state = MagicMock()

        t._on_standard_execution_event(_filled_event("777", action="SELL"))

        t.exec_client.cancel_open_orders.assert_called_once_with(symbol="CL")
        t._reset_position_state.assert_called_once_with(reason="SL_HIT")

    def test_entry_submission_registers_order_id_source_scan(self):
        """live_trader.py must maintain the _entry_order_ids registry: initialized
        and populated at entry-order submission (next to the decision-context store)."""
        src = (REPO_ROOT / "src" / "live_execution" / "live_trader.py").read_text(
            encoding="utf-8", errors="replace"
        )
        assert re.search(r"self\._entry_order_ids\s*(?::\s*[Ss]et\S*\s*)?=\s*set\(\)", src), (
            "self._entry_order_ids must be initialized to a set()"
        )
        assert re.search(r"self\._entry_order_ids\.add\(", src), (
            "entry submission must register the order id in self._entry_order_ids"
        )

    def test_livetest_engine_has_no_duplicate_child_placement(self):
        """The harness must not call trader._place_bracket_children_on_fill —
        live_trader's entry branch places children on fill; the harness
        duplicate orphaned the first child set (root cause of misrouted fills)."""
        src = (REPO_ROOT / "scripts" / "livetest_engine.py").read_text(
            encoding="utf-8", errors="replace"
        )
        assert "._place_bracket_children_on_fill(" not in src, (
            "scripts/livetest_engine.py still places bracket children itself "
            "(duplicate of live_trader.py:4047)"
        )
