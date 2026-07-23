"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._reset_position_state, LiveTrader._seed_restart_cooldown
Secondary Targets: scripts/livetest_engine.py (deletion of the _strategy alias patch)
Status: FINALIZED
Ticket: cooldown-single-authority-wiring_07222026_1051

ROOT CAUSE (live incident 2026-07-22, SIL): LiveTrader.__init__ stores the
strategy as ``self.strategy`` (live_trader.py:384), but the two cooldown-arming
sites read the phantom attribute ``self._strategy``, which has NEVER been
assigned in production (introduced already-broken in cafac9e, 2026-06-18):

  * _reset_position_state guarded on ``hasattr(self, '_strategy')`` -> always
    False -> strategy.on_exit() silently skipped on EVERY TP_HIT / SL_HIT /
    TIME_BARRIER / OOB close -> ConfigurableStrategy's cooldown gate never
    armed -> SIL re-shorted on the very next 1h bar after a stop-out that
    should have started an 11-bar lockout.
  * _seed_restart_cooldown read ``getattr(self, "_strategy", None)`` -> always
    None -> the entire restart-cooldown-recovery fix (ticket
    cooldown-not-restored-on-restart_07082026_0230) was inert in production.

The bug was masked three ways: unit stubs hand-set ``lt._strategy``; the
livetest parity harness aliases ``trader._strategy = trader.strategy``
(scripts/livetest_engine.py "PARITY FIX"); and the silent hasattr/getattr
guards never raised.

Required behavior:
  1. _reset_position_state must call ``self.strategy.on_exit(side, reason,
     bars_held)`` whenever a tracked position side exists (_position_side != 0)
     - via the REAL production attribute, no phantom alias, no silent guard.
  2. _reset_position_state must NOT call on_exit when _position_side == 0
     (never-filled entries are not trades - D2.4).
  3. _seed_restart_cooldown must reach the strategy via ``self.strategy``.
  4. The tracked-SL-fill path (_on_standard_execution_event) must arm the
     strategy cooldown end to end through the real _reset_position_state.
  5. The phantom ``self._strategy`` attribute must not appear in
     live_trader.py, and the livetest harness alias patch must be deleted
     (production wiring makes it redundant).

Deterministic, no real I/O: LiveTrader built via the object.__new__ stub
pattern (see tests/test_exit_reason_and_fill_routing.py). Stubs set ONLY the
real production attribute ``strategy`` - hand-setting ``_strategy`` here would
mask the exact bug this ticket fixes.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

from src.live_execution.live_trader import LiveTrader
from src.live_execution.interfaces.execution_interface import StandardExecutionEvent


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trader() -> LiveTrader:
    """Minimal LiveTrader stub for the reset/seed/fill-routing paths.

    Deliberately sets ONLY the real production attribute ``strategy``
    (never ``_strategy``).
    """
    t = LiveTrader.__new__(LiveTrader)
    t.exec_client = MagicMock()
    t.telemetry = MagicMock()
    t._telegram = MagicMock()
    t.strategy = MagicMock()
    # trailing-sl-no-cooldown_07222026_2050 (mechanical stub repair): the
    # seed path resolves cooldown_arming from strategy.config, a real dict
    # on every real strategy — absent field = "all" (historical default).
    t.strategy.config = {}
    t._execution_symbol = "SIL"
    t._front_month_str = "202609"
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
    t._pending_exit_order_id = None
    t._retiring_leg_ids = []
    t._retiring_sl_id = None
    t._time_barrier_exit_attempts = 0
    t._kill_switch_cancel_confirm_attempts = 0
    t._entry_price = None
    t._highest_high = 0.0
    t._lowest_low = float("inf")
    t._trailing_activated = False
    t._trade_trailing_atr_mult = None
    t._build_event_id = MagicMock(return_value="evt-test")
    t._base_tradebook_fields = MagicMock(return_value={})
    return t


def _filled_event(
    order_id: str, action: str = "BUY", price: float = 60.23
) -> StandardExecutionEvent:
    raw_order = MagicMock()
    raw_order.action = action
    raw_order.permId = None
    raw_order.parentId = None
    raw_order.account = None
    raw = MagicMock()
    raw.order = raw_order
    raw.contract = MagicMock(symbol="SI")
    return StandardExecutionEvent(
        order_id=order_id,
        symbol="SIL",
        status="Filled",
        filled_qty=1,
        remaining_qty=0,
        avg_price=price,
        raw_event=raw,
    )


# ---------------------------------------------------------------------------
# 1+2. _reset_position_state wiring
# ---------------------------------------------------------------------------


class TestResetPositionStateOnExitWiring:
    def test_reset_calls_strategy_on_exit_via_real_attribute(self):
        """A tracked SHORT close must notify strategy.on_exit(-1, reason,
        bars_held) through ``self.strategy`` - the production attribute.
        This is the SIL 2026-07-22 incident wiring: the phantom
        ``self._strategy`` guard silently skipped this call."""
        t = _make_trader()
        t._position_side = -1
        t._position_bars_held = 42

        t._reset_position_state(reason="SL_HIT")

        t.strategy.on_exit.assert_called_once_with(-1, "SL_HIT", 42)

    def test_reset_calls_on_exit_for_long_side_too(self):
        t = _make_trader()
        t._position_side = 1
        t._position_bars_held = 7

        t._reset_position_state(reason="TP_HIT")

        t.strategy.on_exit.assert_called_once_with(1, "TP_HIT", 7)

    def test_reset_without_tracked_side_does_not_fire_on_exit(self):
        """_position_side == 0 means no trade existed (e.g. never-filled
        entry cleanup) - no on_exit, no cooldown (D2.4)."""
        t = _make_trader()
        t._position_side = 0

        t._reset_position_state(reason="CLOSED")

        t.strategy.on_exit.assert_not_called()


# ---------------------------------------------------------------------------
# 3. _seed_restart_cooldown wiring
# ---------------------------------------------------------------------------


class TestSeedRestartCooldownWiring:
    def test_seed_reaches_strategy_via_real_attribute(self):
        """close_time=None (exit just happened) must fire on_exit on
        ``self.strategy`` - the getattr(self, "_strategy", None) guard made
        this a silent no-op in production."""
        t = _make_trader()

        t._seed_restart_cooldown(-1, "SL_HIT_OOB", close_time=None)

        t.strategy.on_exit.assert_called_once_with(-1, "SL_HIT_OOB", 0)

    def test_seed_still_validates_side_and_reason(self):
        t = _make_trader()

        t._seed_restart_cooldown(0, "SL_HIT_OOB", close_time=None)
        t._seed_restart_cooldown(1, None, close_time=None)

        t.strategy.on_exit.assert_not_called()


# ---------------------------------------------------------------------------
# 4. End to end: tracked SL fill arms the strategy cooldown
# ---------------------------------------------------------------------------


class TestSlFillArmsCooldownEndToEnd:
    def test_tracked_sl_fill_notifies_strategy_on_exit(self):
        """The exact incident path: entry filled SHORT, tracked SL fills,
        software OCA books SL_HIT - the REAL _reset_position_state must
        notify the strategy so the re-entry cooldown arms."""
        t = _make_trader()
        t._entry_order_ids = {"1078"}
        t._sl_order_id = 1080
        t._active_trade_id = "trade_1078"
        t._position_side = -1
        t._position_bars_held = 1

        t._on_standard_execution_event(_filled_event("1080", action="BUY"))

        t.strategy.on_exit.assert_called_once_with(-1, "SL_HIT", 1)
        # And the close is fully booked (regression guards)
        t.exec_client.cancel_open_orders.assert_called_once_with(symbol="SIL")
        assert t._position_side == 0
        assert t._active_trade_id is None


# ---------------------------------------------------------------------------
# 5. Phantom attribute eradicated (source scans)
# ---------------------------------------------------------------------------


class TestPhantomStrategyAttributeRemoved:
    def test_live_trader_has_no_phantom_strategy_attribute(self):
        """``self._strategy`` must not appear in live_trader.py - it was
        never assigned and every read of it was a silent no-op."""
        src = (REPO_ROOT / "src" / "live_execution" / "live_trader.py").read_text(
            encoding="utf-8", errors="replace"
        )
        hits = re.findall(r"self\._strategy\b", src)
        assert hits == [], (
            f"live_trader.py still references the phantom self._strategy "
            f"({len(hits)} occurrence(s)) - use self.strategy"
        )

    def test_livetest_engine_alias_patch_deleted(self):
        """The harness compensator ``trader._strategy = trader.strategy``
        must be gone - production wiring makes it redundant, and keeping it
        would hide any future regression of this exact bug."""
        src = (REPO_ROOT / "scripts" / "livetest_engine.py").read_text(
            encoding="utf-8", errors="replace"
        )
        assert "._strategy" not in src, (
            "scripts/livetest_engine.py still contains a ._strategy alias/"
            "reference - the PARITY FIX patch must be deleted"
        )
