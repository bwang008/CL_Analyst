"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function:
    LiveTrader._flatten_book_and_reset      (R1: NEW un-gated shared flatten
                                             helper extracted from
                                             _check_naked_position's body)
    LiveTrader._reset_position_state        (R2: single-slot recently-closed
                                             leg-id registry snapshot BEFORE
                                             clearing _tp_order_ids /
                                             _sl_order_id; R4: partial-fill
                                             dedupe set cleared here)
    LiveTrader._on_standard_execution_event (R2: residual double-fill
                                             reversal branch ahead of the
                                             UNRECOGNIZED-FILL ignore path;
                                             R4: partial-fill observability
                                             for non-Filled status events on
                                             tracked TP/SL legs)
    LiveTrader._route_retired_time_barrier_exit (R3/D2: settled-sign check ->
                                             flatten on reversal, NEVER
                                             re-arm; settled-sized re-arm on
                                             the sign-matching path)
Secondary (ADDITIVE registry edit, made by the TDD-Tester under explicit
    single-edit authorization): tests/test_hourly_order_housekeeping.py
    _ALLOWED_KINDS gains "oca-race-reversal", "rearm-sign-mismatch",
    "protective-leg-partial-fill" (R5) — nothing else in that file changed.
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: oca-stage2-residual-detection_07222026_0141
Parent: oco-leg-race-audit_07212026_1935 Stage 2 (Stage 1 = 7795e1a).

Phase: RED — against current source these FAIL on the missing features:
  - LiveTrader has no _flatten_book_and_reset attribute -> the R1 contract
    test and both R1 behavioral tests fail (AttributeError / getattr None);
  - the second Filled event for the just-closed bracket's OLD TP id lands in
    the UNRECOGNIZED-FILL ignore branch (live_trader.py:6499-6516): no
    CRITICAL, no oca-race-reversal health event, no OCA_RACE_REVERSAL
    tradebook event, no cached-position read, no flatten -> both R2 reversal
    tests fail;
  - _route_retired_time_barrier_exit branches only on settled==0 vs non-zero:
    a REVERSED settled (-3 against a tracked +1) re-arms via
    _verify_and_heal_protective_legs (the sign-blind D2 defect this suite
    pins as WRONG) and returns False -> the sign-mismatch test fails on
    "heal was called"; the sign-matching path re-arms with the STALE
    current_position (quantity 5, not the settled 2) -> the settled-sizing
    test fails on quantity;
  - non-Filled status events with filled_qty>0 on a tracked leg do nothing
    today -> all partial-fill emission tests fail on the missing warning /
    health event.
Intentionally GREEN today (fences the fix must PRESERVE):
  - registry hygiene: a recognized entry fill clears the registry, an aged
    snapshot does not trip, a Cancelled sibling event does not trip, and a
    totally unknown order id still takes the old UNRECOGNIZED-FILL ignore
    path (no flatten, no reversal events);
  - a full Filled after a partial books the exit normally (TP_HIT);
  - R5 kind membership (the tester's own additive registry edit).

LIVE-FLEET SAFETY: every I/O boundary is a MagicMock or SimpleNamespace —
no broker, no network, no real DB, no data files. No stub sets
_health_events_enabled; health events are asserted by mocking
LiveTrader._emit_health_event and inspecting kinds (the
tests/test_hourly_order_housekeeping.py convention).

Construction mirrors tests/test_exit_reason_and_fill_routing.py (LiveTrader
via object.__new__ with only the seams the tested paths read);
StandardExecutionEvent construction mirrors that file's _filled_event; the
lt_module.time patch for the aged-registry fence mirrors
tests/test_hourly_order_housekeeping.py's A-3 clock convention. The existing
kill-switch/OOB/settle-confirm suites (tests/test_oob_entry_state_recovery.py,
tests/test_time_barrier_exit_fill_confirmation.py) stay green UNMODIFIED —
_check_naked_position's observable behavior is byte-identical after the R1
extraction, so this file deliberately re-pins NONE of its internals.
"""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.live_execution import live_trader as lt_module
from src.live_execution.live_trader import LiveTrader
from src.live_execution.interfaces.execution_interface import (
    StandardExecutionEvent,
)
# Cross-test import is this suite's convention (see
# tests/test_pair_selection_top_n.py, tests/test_shallow_5m_bootstrap.py).
from tests.test_hourly_order_housekeeping import _ALLOWED_KINDS

TICKET_ID = "oca-stage2-residual-detection_07222026_0141"

_NEW_KINDS = frozenset({
    "oca-race-reversal",
    "rearm-sign-mismatch",
    "protective-leg-partial-fill",
})


# ===========================================================================
# Helpers — builders and event constructors
# ===========================================================================


def _make_trader() -> LiveTrader:
    """Minimal FLAT LiveTrader stub with the attributes the tested paths
    read (the tests/test_exit_reason_and_fill_routing.py builder, extended
    with the reversal/flatten seams)."""
    t = LiveTrader.__new__(LiveTrader)
    t.exec_client = MagicMock()
    t.telemetry = MagicMock()
    t._telegram = MagicMock()
    t._emit_health_event = MagicMock()
    t._strategy = None  # real _reset_position_state skips on_exit on None
    t._execution_symbol = "CL"
    t._front_month_str = "202608"
    t._front_month_local_symbol = "CLU6"
    t._exit_mode = "MKT"
    t._open_orders = {}
    t._processed_exit_order_ids = set()
    t._processed_entry_order_ids = set()
    t._last_decision_context_by_order_id = {}
    t._entry_order_ids = set()
    t._tp_order_ids = []
    t._sl_order_id = None
    t._tracked_tp_price = None
    t._tracked_sl_price = None
    t._active_trade_id = None
    t._position_side = 0
    t._position_bars_held = 0
    t._position_entry_bar_time = None
    t._entry_price = None
    t._atr_at_entry = 0.5
    t._trailing_activated = False
    t._highest_high = 0.0
    t._lowest_low = float("inf")
    t._trade_max_hold_bars = None
    t._trade_trailing_atr_mult = None
    t._max_hold_bars = 240
    t._pending_entry_order_id = None
    t._pending_entry_bar_time = None
    t._pending_exit_order_id = None
    t._time_barrier_exit_attempts = 0
    t.rolling_df_5m = None
    t.rolling_df_1h = None
    t._build_event_id = MagicMock(return_value="evt-test")
    t._base_tradebook_fields = MagicMock(return_value={})
    # Broker seams — safe defaults, overridden per test.
    t.exec_client.get_cached_position.return_value = 0
    t.exec_client.cancel_open_orders.return_value = 1
    t.exec_client.close_position.return_value = SimpleNamespace(
        order=SimpleNamespace(orderId=4242)
    )
    return t


def _tracked_trader() -> LiveTrader:
    """A trader HOLDING +1 long trade_100 with a live [TP=65, SL=66]
    bracket — the state every reversal / partial-fill scenario starts in."""
    t = _make_trader()
    t._entry_order_ids = {"100"}
    t._processed_entry_order_ids = {"100"}
    t._active_trade_id = "trade_100"
    t._position_side = 1
    t._position_bars_held = 4
    t._entry_price = 88.00
    t._tp_order_ids = [65]
    t._sl_order_id = 66
    t._tracked_tp_price = 89.50
    t._tracked_sl_price = 87.20
    return t


def _fill_event(order_id: str, *, action: str = "SELL", price: float = 88.26,
                qty: int = 1) -> StandardExecutionEvent:
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
        filled_qty=qty,
        remaining_qty=0,
        avg_price=price,
        raw_event=raw,
    )


def _status_event(order_id: str, *, status: str, filled_qty: int = 0,
                  remaining_qty: int = 1, price: float = 0.0,
                  action: str = "SELL") -> StandardExecutionEvent:
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
        status=status,
        filled_qty=filled_qty,
        remaining_qty=remaining_qty,
        avg_price=price,
        raw_event=raw,
    )


def _health_kinds(t) -> list:
    out = []
    for c in t._emit_health_event.call_args_list:
        out.append(c.args[0] if c.args else c.kwargs.get("kind"))
    return out


def _ledger_close_calls(t) -> list:
    """(trade_id, reason) per telemetry.close_position call — robust to
    positional-or-keyword trade_id (both shapes exist in live_trader)."""
    out = []
    for c in t.telemetry.close_position.call_args_list:
        trade_id = c.kwargs.get(
            "trade_id", c.args[0] if c.args else None,
        )
        reason = c.kwargs.get(
            "reason", c.args[1] if len(c.args) > 1 else None,
        )
        out.append((trade_id, reason))
    return out


def _close_via_sl_fill(t) -> None:
    """Step 1 of every reversal scenario: the tracked SL (66) fills and the
    trade books a normal SL_HIT close through the REAL
    _reset_position_state — the exact moment R2 requires the leg-id
    registry snapshot to be taken (BEFORE the ids are cleared)."""
    t._on_standard_execution_event(
        _fill_event("66", action="SELL", price=87.20)
    )
    # Harness sanity (GREEN today): the close booked and state reset.
    assert t._active_trade_id is None
    assert t._tp_order_ids == [] and t._sl_order_id is None
    assert _ledger_close_calls(t) == [("trade_100", "SL_HIT")]


def _reversal_tradebook_calls(t) -> list:
    return [
        c for c in t.telemetry.log_tradebook_event.call_args_list
        if c.kwargs.get("event_type") == "OCA_RACE_REVERSAL"
    ]


class _Clock:
    """lt_module.time replacement (the housekeeping-suite A-3 convention):
    controllable wall clock for the aged-registry fence."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def perf_counter(self):
        return self.now

    def sleep(self, *_a, **_k):
        pass


# ===========================================================================
# R1 — un-gated shared flatten helper
# ===========================================================================


class TestFlattenHelperContract:
    def test_helper_exists_with_keyword_only_contract(self):
        """LiveTrader._flatten_book_and_reset(*, reason, telegram_text,
        ledger_trade_id) must exist with all three params KEYWORD-ONLY."""
        helper = getattr(LiveTrader, "_flatten_book_and_reset", None)
        assert helper is not None, (
            "LiveTrader has no _flatten_book_and_reset — the un-gated "
            "flatten helper (R1, extracted from _check_naked_position) "
            "does not exist"
        )
        sig = inspect.signature(helper)
        for name in ("reason", "telegram_text", "ledger_trade_id"):
            assert name in sig.parameters, (
                f"_flatten_book_and_reset is missing the {name!r} parameter"
            )
            assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (
                f"_flatten_book_and_reset param {name!r} must be "
                f"KEYWORD-ONLY (contract: (*, reason, telegram_text, "
                f"ledger_trade_id)); got {sig.parameters[name].kind!r}"
            )

    def test_helper_flattens_ungated_with_no_active_trade(self):
        """UN-GATED (the whole point): with _active_trade_id None,
        _sl_order_id None and no pending state, the helper STILL cancels
        open orders, market-flattens, registers the exit oid, resets state
        with the given reason, Telegrams the given text — and SKIPS the
        ledger close because ledger_trade_id is None (the reversal branch
        must never re-close the already-truthfully-closed row).
        _check_naked_position could never do this: its _active_trade_id
        guard makes the same call a silent no-op by construction."""
        t = _make_trader()  # flat: _active_trade_id None, _sl_order_id None
        t._reset_position_state = MagicMock()

        t._flatten_book_and_reset(
            reason="OCA_RACE_REVERSAL",
            telegram_text="test flatten text",
            ledger_trade_id=None,
        )

        assert t.exec_client.cancel_open_orders.called, (
            "the un-gated helper must cancel open orders even when no "
            "trade is tracked"
        )
        assert t.exec_client.close_position.called, (
            "the un-gated helper must flatten even when _active_trade_id "
            "is None — gating it on tracked state recreates the "
            "kill-switch blindness this ticket exists to fix"
        )
        assert (
            t.exec_client.close_position.call_args.kwargs.get("exit_mode")
            == "market"
        ), "the flatten must be a MARKET close (exit_mode='market')"
        t.telemetry.close_position.assert_not_called()
        assert "4242" in {str(x) for x in t._processed_exit_order_ids}, (
            "the flatten's own exit order id must be registered in "
            "_processed_exit_order_ids (else its fill re-enters the "
            "unrecognized-fill machinery)"
        )
        assert t._reset_position_state.called, (
            "the helper must reset position state"
        )
        c = t._reset_position_state.call_args
        reason = c.kwargs.get("reason", c.args[0] if c.args else None)
        assert reason == "OCA_RACE_REVERSAL", (
            f"_reset_position_state must receive the helper's reason; "
            f"got {reason!r}"
        )
        sent = " | ".join(
            str(c) for c in t._telegram.send.call_args_list
        )
        assert "test flatten text" in sent, (
            "the CRITICAL Telegram must carry the caller-provided "
            "telegram_text"
        )

    def test_helper_ledger_close_runs_with_given_trade_id_and_reason(self):
        """ledger_trade_id not None -> telemetry.close_position runs
        exactly once with THAT trade id and the helper's reason."""
        t = _make_trader()
        t._position_bars_held = 7
        t._reset_position_state = MagicMock()

        t._flatten_book_and_reset(
            reason="REVERSED_POSITION_KILL_SWITCH",
            telegram_text="reversed book",
            ledger_trade_id="trade_42",
        )

        assert _ledger_close_calls(t) == [
            ("trade_42", "REVERSED_POSITION_KILL_SWITCH"),
        ], (
            f"expected exactly one ledger close for trade_42 with reason "
            f"REVERSED_POSITION_KILL_SWITCH; got {_ledger_close_calls(t)!r}"
        )


# ===========================================================================
# R2 — residual double-fill (reversal) branch
# ===========================================================================


class TestOcaRaceReversalBranch:
    def test_residual_tp_fill_after_sl_close_trips_reversal_flatten(
        self, caplog,
    ):
        """THE Stage-2 scenario: SL fills and books SL_HIT normally; then a
        second Filled event arrives for the OLD TP id (now untracked —
        _reset_position_state already cleared it). Today that second fill
        is silently ignored while a REVERSED position sits kill-switch-
        blind. Required: CRITICAL log + oca-race-reversal health event +
        Telegram + OCA_RACE_REVERSAL tradebook event; residual read via
        get_cached_position ONLY (in-callback A-2 constraint — never
        get_position, never a settled read); cached residual != 0 ->
        un-gated flatten (market close + cancel) WITHOUT re-closing the
        original SL_HIT ledger row."""
        t = _tracked_trader()
        t.exec_client.get_cached_position.return_value = -1  # reversed book

        _close_via_sl_fill(t)
        sends_before = t._telegram.send.call_count
        cancels_before = t.exec_client.cancel_open_orders.call_count
        caplog.clear()

        with caplog.at_level(logging.INFO):
            t._on_standard_execution_event(
                _fill_event("65", action="SELL", price=89.50, qty=1)
            )

        assert any(
            r.levelno == logging.CRITICAL for r in caplog.records
        ), (
            "a residual double-fill on the just-closed bracket's sibling "
            "leg must log CRITICAL — today it dies in the "
            "UNRECOGNIZED-FILL ignore branch"
        )
        assert "oca-race-reversal" in _health_kinds(t), (
            f"expected an 'oca-race-reversal' health event; got kinds="
            f"{_health_kinds(t)}"
        )
        assert t._telegram.send.call_count > sends_before, (
            "the reversal must send a CRITICAL Telegram"
        )
        reversal_events = _reversal_tradebook_calls(t)
        assert len(reversal_events) == 1, (
            f"expected exactly one OCA_RACE_REVERSAL tradebook event; got "
            f"{len(reversal_events)}"
        )
        vals = {str(v) for v in reversal_events[0].kwargs.values()}
        assert "65" in vals, (
            f"the OCA_RACE_REVERSAL tradebook event must carry the second "
            f"fill's order id 65; kwargs values={vals!r}"
        )
        assert "89.5" in vals, (
            f"the OCA_RACE_REVERSAL tradebook event must carry the second "
            f"fill's price 89.5; kwargs values={vals!r}"
        )
        assert {"1", "1.0"} & vals, (
            f"the OCA_RACE_REVERSAL tradebook event must carry the second "
            f"fill's qty 1; kwargs values={vals!r}"
        )
        # Residual read: cached ONLY (test-pinned A-2 constraint).
        assert t.exec_client.get_cached_position.called, (
            "the residual position must be read via get_cached_position"
        )
        t.exec_client.get_position.assert_not_called()
        t.exec_client.get_position_settled.assert_not_called()
        # Flatten through the un-gated helper: market close + cancel.
        assert t.exec_client.close_position.called, (
            "cached residual -1 != 0 -> the book must be flattened"
        )
        assert (
            t.exec_client.close_position.call_args.kwargs.get("exit_mode")
            == "market"
        )
        assert t.exec_client.cancel_open_orders.call_count > cancels_before, (
            "the flatten must cancel open orders (the helper's step 1)"
        )
        # The original ledger row stays truthfully SL_HIT — no re-close.
        assert _ledger_close_calls(t) == [("trade_100", "SL_HIT")], (
            f"the reversal branch must NOT re-close/overwrite the original "
            f"ledger row (ledger_trade_id=None); ledger closes="
            f"{_ledger_close_calls(t)!r}"
        )

    def test_cached_flat_residual_emits_events_without_flatten_order(
        self, caplog,
    ):
        """Cached residual == 0: the double-fill self-cancelled (e.g. the
        second fill itself flattened). Events still fire — the anomaly is
        real and must page — but NO flatten order is placed."""
        t = _tracked_trader()
        t.exec_client.get_cached_position.return_value = 0

        _close_via_sl_fill(t)
        caplog.clear()

        with caplog.at_level(logging.INFO):
            t._on_standard_execution_event(
                _fill_event("65", action="SELL", price=89.50, qty=1)
            )

        assert "oca-race-reversal" in _health_kinds(t), (
            f"cached-flat variant must still emit the health event; got "
            f"{_health_kinds(t)}"
        )
        assert any(
            r.levelno == logging.CRITICAL for r in caplog.records
        ), "cached-flat variant must still log CRITICAL"
        assert len(_reversal_tradebook_calls(t)) == 1, (
            "cached-flat variant must still write the OCA_RACE_REVERSAL "
            "tradebook event (truthful record)"
        )
        t.exec_client.close_position.assert_not_called()
        assert _ledger_close_calls(t) == [("trade_100", "SL_HIT")]


class TestReversalRegistryHygiene:
    """FENCES (green today, must stay green): the registry must only trip
    on a FRESH snapshot's OWN leg ids for Filled events — everything else
    keeps the old UNRECOGNIZED-FILL ignore path untouched."""

    def test_next_recognized_entry_fill_clears_registry(self, caplog):
        """A recognized entry fill AFTER the close consumes the snapshot:
        a later Filled event for the old leg id must take the plain
        ignore path (no reversal events, no flatten) — the old bracket's
        world ended when a new trade began."""
        t = _tracked_trader()
        t.exec_client.get_cached_position.return_value = -1
        _close_via_sl_fill(t)

        # A new recognized entry fill (registry must be CLEARED by it).
        t._entry_order_ids.add("200")
        t._last_decision_context_by_order_id[200] = {
            "tp_offset": 2.0, "sl_offset": 2.0, "entry_action": "BUY",
            "lots": 1, "atr_at_entry": 0.5,
        }
        t._place_bracket_children_on_fill = MagicMock()
        t._on_standard_execution_event(
            _fill_event("200", action="BUY", price=88.00)
        )
        assert t._active_trade_id == "trade_200"  # harness sanity
        caplog.clear()

        with caplog.at_level(logging.ERROR):
            t._on_standard_execution_event(
                _fill_event("65", action="SELL", price=89.50)
            )

        assert "oca-race-reversal" not in _health_kinds(t), (
            "a leg id from BEFORE the new entry tripped the reversal "
            "branch — the registry must be cleared on a recognized entry "
            "fill"
        )
        t.exec_client.close_position.assert_not_called()
        assert any(
            "UNRECOGNIZED FILL" in r.getMessage() for r in caplog.records
        ), "the old ignore path (loud UNRECOGNIZED FILL error) must remain"
        assert [r for _, r in _ledger_close_calls(t)] == ["SL_HIT"], (
            "no additional ledger close may happen"
        )

    def test_aged_registry_snapshot_does_not_trip(self, caplog):
        """A snapshot far older than any sane bound (24h; blueprint bound
        example is 6h) must be ignored at match time — broker id reuse
        across sessions must not trigger a phantom reversal flatten."""
        t = _tracked_trader()
        t.exec_client.get_cached_position.return_value = -1
        clock = _Clock()

        with patch.object(lt_module, "time", clock):
            _close_via_sl_fill(t)          # snapshot taken at T0
            clock.now += 24 * 3600.0       # age the snapshot 24 hours
            caplog.clear()
            with caplog.at_level(logging.ERROR):
                t._on_standard_execution_event(
                    _fill_event("65", action="SELL", price=89.50)
                )

        assert "oca-race-reversal" not in _health_kinds(t), (
            "an aged registry snapshot tripped the reversal branch — "
            "entries older than the bounded age must be ignored"
        )
        t.exec_client.close_position.assert_not_called()
        assert any(
            "UNRECOGNIZED FILL" in r.getMessage() for r in caplog.records
        )

    def test_sibling_cancelled_status_does_not_trip(self, caplog):
        """The branch keys on status=='Filled' ONLY: the sibling's
        Cancelled event (the OCA group doing its job, or the software-OCA
        bulk cancel echo) must not emit events or flatten."""
        t = _tracked_trader()
        t.exec_client.get_cached_position.return_value = -1
        _close_via_sl_fill(t)
        caplog.clear()

        with caplog.at_level(logging.INFO):
            t._on_standard_execution_event(
                _status_event(
                    "65", status="Cancelled", filled_qty=0, remaining_qty=0,
                )
            )

        assert "oca-race-reversal" not in _health_kinds(t), (
            "a CANCELLED sibling event tripped the reversal branch — it "
            "must key on Filled only"
        )
        t.exec_client.close_position.assert_not_called()
        assert not any(
            r.levelno >= logging.CRITICAL for r in caplog.records
        )
        assert [r for _, r in _ledger_close_calls(t)] == ["SL_HIT"]

    def test_unknown_order_id_keeps_unrecognized_ignore_path(self, caplog):
        """A Filled event for an id in NOBODY's registry (not a leg of the
        just-closed bracket) still takes the old UNRECOGNIZED-FILL ignore
        path even while a fresh snapshot exists."""
        t = _tracked_trader()
        t.exec_client.get_cached_position.return_value = -1
        _close_via_sl_fill(t)
        caplog.clear()

        with caplog.at_level(logging.ERROR):
            t._on_standard_execution_event(
                _fill_event("999", action="SELL", price=42.00)
            )

        assert any(
            "UNRECOGNIZED FILL" in r.getMessage() for r in caplog.records
        ), "the unknown-id fill must keep the loud UNRECOGNIZED FILL log"
        assert "oca-race-reversal" not in _health_kinds(t), (
            "an id outside the snapshot's leg_ids tripped the reversal "
            "branch"
        )
        t.exec_client.close_position.assert_not_called()
        assert [r for _, r in _ledger_close_calls(t)] == ["SL_HIT"]


# ===========================================================================
# R3 — D2: settled-sign check + settled-sized re-arm in
#          _route_retired_time_barrier_exit
# ===========================================================================


def _make_route_trader() -> LiveTrader:
    """A trader mid TIME-BARRIER retirement routing: +1 tracked long, exit
    retired, protective ids already cleared, tracked prices survive for a
    potential re-arm (the tests/test_time_barrier_exit_fill_confirmation.py
    state at the _route_retired_time_barrier_exit call site)."""
    t = _make_trader()
    t._execution_symbol = "NG"
    t._front_month_local_symbol = "NGQ6"
    t._active_trade_id = "trade_71"
    t._position_side = 1
    t._position_bars_held = 6
    t._tp_order_ids = []
    t._sl_order_id = None
    t._tracked_tp_price = 2.96
    t._tracked_sl_price = 2.86
    t._verify_and_heal_protective_legs = MagicMock(return_value="healed")
    t._reset_position_state = MagicMock()
    return t


class TestRearmSignCheckD2:
    def test_reversed_settled_sign_flattens_and_never_rearms(self, caplog):
        """D2: tracked +1 LONG, settled read returns -3 (REVERSED). Today
        the sign-blind branch treats any non-zero settled as 'still open'
        and RE-ARMS protective legs on the OLD side against a reversed
        book (exposure-increasing if filled) — that re-arm call is exactly
        what this test pins as FORBIDDEN. Required: no heal, CRITICAL +
        rearm-sign-mismatch + Telegram, flatten with ledger reason
        REVERSED_POSITION_KILL_SWITCH, return True (trade concluded)."""
        t = _make_route_trader()
        t._confirm_settled_position = MagicMock(return_value=-3)

        with caplog.at_level(logging.INFO):
            result = t._route_retired_time_barrier_exit(71, 1)

        assert not t._verify_and_heal_protective_legs.called, (
            "REVERSED settled position (-3 vs tracked +1) and the route "
            "still re-armed protective legs — the D2 sign-blind re-arm "
            "places same-direction orders against a reversed book"
        )
        assert result is True, (
            f"a reversed-and-flattened trade is CONCLUDED — the route must "
            f"return True; got {result!r}"
        )
        assert "rearm-sign-mismatch" in _health_kinds(t), (
            f"expected a 'rearm-sign-mismatch' health event; got kinds="
            f"{_health_kinds(t)}"
        )
        assert any(
            r.levelno == logging.CRITICAL for r in caplog.records
        ), "a settled sign flip must log CRITICAL"
        assert t._telegram.send.called, (
            "a settled sign flip must send Telegram"
        )
        assert t.exec_client.close_position.called, (
            "the reversed book must be flattened"
        )
        assert (
            t.exec_client.close_position.call_args.kwargs.get("exit_mode")
            == "market"
        )
        assert _ledger_close_calls(t) == [
            ("trade_71", "REVERSED_POSITION_KILL_SWITCH"),
        ], (
            f"the ledger must close trade_71 with reason "
            f"REVERSED_POSITION_KILL_SWITCH exactly once; got "
            f"{_ledger_close_calls(t)!r}"
        )

    def test_same_sign_rearm_sized_from_settled_not_stale_read(self):
        """Sign-matching path: settled +2 while the STALE pre-settled
        current_position argument says +5. The re-arm must be sized from
        the SETTLED value — quantity abs(+2) — not the stale 5 (today
        _rearm_time_barrier_protection(current_position) re-arms at 5).
        Everything else about the still-open path is unchanged: deferred
        (returns False), no flatten, no ledger close."""
        t = _make_route_trader()
        t._confirm_settled_position = MagicMock(return_value=2)

        result = t._route_retired_time_barrier_exit(71, 5)

        assert result is False, (
            "the still-open sign-matching path must keep deferring "
            "(return False)"
        )
        assert t._verify_and_heal_protective_legs.call_count == 1, (
            "the sign-matching path must re-arm exactly once"
        )
        kw = t._verify_and_heal_protective_legs.call_args.kwargs
        assert kw.get("quantity") == 2, (
            f"re-arm must be sized from the SETTLED position "
            f"(abs(+2) == 2), not the stale pre-settled read (5); got "
            f"quantity={kw.get('quantity')!r}"
        )
        assert kw.get("position_side") == 1
        t.telemetry.close_position.assert_not_called()
        t.exec_client.close_position.assert_not_called()


# ===========================================================================
# R4 — partial-fill observability
# ===========================================================================


def _partial_warnings(caplog) -> list:
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "partial" in r.getMessage().lower()
    ]


class TestPartialFillObservability:
    def test_partial_fill_on_tracked_tp_emits_once(self, caplog):
        """status='Submitted', filled_qty=2 on the tracked TP -> exactly
        one log.warning + one protective-leg-partial-fill health event.
        Observability ONLY: no booking, no cancel, no reset, no flatten
        (broker-side ocaType=2 owns the sibling resize)."""
        t = _tracked_trader()
        t._reset_position_state = MagicMock()

        with caplog.at_level(logging.INFO):
            t._on_standard_execution_event(
                _status_event(
                    "65", status="Submitted", filled_qty=2,
                    remaining_qty=1, price=89.50,
                )
            )

        assert len(_partial_warnings(caplog)) == 1, (
            "a partial fill on a tracked protective leg must log exactly "
            "ONE warning — today non-Filled events with filled_qty>0 are "
            "completely invisible"
        )
        assert _health_kinds(t).count("protective-leg-partial-fill") == 1, (
            f"expected exactly one 'protective-leg-partial-fill' health "
            f"event; got kinds={_health_kinds(t)}"
        )
        # NO booking changes, NO cancel, NO state mutation beyond dedupe.
        t.exec_client.cancel_open_orders.assert_not_called()
        t.exec_client.close_position.assert_not_called()
        t.telemetry.close_position.assert_not_called()
        t._reset_position_state.assert_not_called()

    def test_identical_partial_deduped_new_qty_reemits(self, caplog):
        """Dedupe key is (order_id, filled_qty): the identical repeat event
        emits nothing more; a HIGHER filled_qty (the partial growing) is a
        new pair and emits again."""
        t = _tracked_trader()
        t._reset_position_state = MagicMock()

        def evt(q):
            return _status_event(
                "65", status="Submitted", filled_qty=q,
                remaining_qty=3 - q, price=89.50,
            )

        with caplog.at_level(logging.INFO):
            t._on_standard_execution_event(evt(1))
            t._on_standard_execution_event(evt(1))  # identical -> deduped

        assert _health_kinds(t).count("protective-leg-partial-fill") == 1, (
            f"an identical (order_id, filled_qty) repeat must NOT re-emit; "
            f"got kinds={_health_kinds(t)}"
        )
        assert len(_partial_warnings(caplog)) == 1, (
            "an identical repeat event must not log a second warning "
            "(IBKR replays status events on every reconnect)"
        )

        with caplog.at_level(logging.INFO):
            t._on_standard_execution_event(evt(2))  # new pair -> re-emit

        assert _health_kinds(t).count("protective-leg-partial-fill") == 2, (
            "a NEW filled_qty on the same order is a new (order_id, "
            "filled_qty) pair and must emit again"
        )

    def test_full_fill_after_partial_books_exit_normally(self):
        """FENCE (green today, must stay green): after a partial event, the
        eventual full Filled for the tracked TP still books the exit
        normally — TP_HIT close + software-OCA cancel + reset."""
        t = _tracked_trader()
        t._reset_position_state = MagicMock()

        t._on_standard_execution_event(
            _status_event(
                "65", status="Submitted", filled_qty=1,
                remaining_qty=1, price=89.50,
            )
        )
        t._on_standard_execution_event(
            _fill_event("65", action="SELL", price=89.50, qty=2)
        )

        t.exec_client.cancel_open_orders.assert_called_once_with(symbol="CL")
        closes = _ledger_close_calls(t)
        assert closes and closes[-1] == ("trade_100", "TP_HIT"), (
            f"the full fill must book a normal TP_HIT close; got "
            f"{closes!r}"
        )
        t._reset_position_state.assert_called_once_with(reason="TP_HIT")

    def test_dedupe_state_cleared_by_reset_position_state(self, caplog):
        """The dedupe set lives per-trade: after a REAL
        _reset_position_state (trade closed) and broker id reuse on the
        next trade, the same (order_id, filled_qty) pair must emit AGAIN."""
        t = _tracked_trader()  # REAL _reset_position_state

        with caplog.at_level(logging.INFO):
            t._on_standard_execution_event(
                _status_event(
                    "65", status="Submitted", filled_qty=1,
                    remaining_qty=1, price=89.50,
                )
            )
        assert _health_kinds(t).count("protective-leg-partial-fill") == 1, (
            "harness precondition (RED today): the first partial must emit"
        )

        # Close the trade through the REAL reset (SL fills).
        t._on_standard_execution_event(
            _fill_event("66", action="SELL", price=87.20)
        )
        assert t._active_trade_id is None  # harness sanity

        # Next trade; TWS id-sequence reuse hands out order id 65 again.
        t._active_trade_id = "trade_101"
        t._position_side = 1
        t._position_bars_held = 0
        t._tp_order_ids = [65]
        t._sl_order_id = 66
        t._processed_exit_order_ids = set()

        with caplog.at_level(logging.INFO):
            t._on_standard_execution_event(
                _status_event(
                    "65", status="Submitted", filled_qty=1,
                    remaining_qty=1, price=89.50,
                )
            )

        assert _health_kinds(t).count("protective-leg-partial-fill") == 2, (
            "the dedupe set was not cleared by _reset_position_state — a "
            "reused order id on the NEXT trade suppressed a real partial "
            "fill signal"
        )


# ===========================================================================
# R5 — health-kind registry
# ===========================================================================


class TestHealthKindRegistry:
    def test_new_kinds_registered_in_housekeeping_contract_set(self):
        """The three Stage-2 kinds must be members of the housekeeping
        suite's _ALLOWED_KINDS contract vocabulary (SKILL.md triage routes
        on exact kind strings; the additive registry edit mirrors how
        position-flat-unconfirmed was added)."""
        missing = _NEW_KINDS - _ALLOWED_KINDS
        assert not missing, (
            f"health kinds missing from tests/test_hourly_order_"
            f"housekeeping.py::_ALLOWED_KINDS: {sorted(missing)} — the "
            f"registry update is part of R5 ({TICKET_ID})"
        )
