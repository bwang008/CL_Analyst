"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
                            (R1/R2a: [TRADE] EXIT FILLED operator line in the
                             TP/SL branch of _on_standard_execution_event,
                             ~7235-7268;
                             R2b: INFO line in _on_commission_event, 1313-1335;
                             R3: Telegram send failure + False return logged;
                             R4/M3: ledger-close failure -> ERROR + health
                             event "exit-ledger-close-failed" + reset still
                             reached;
                             R5: no `or "unknown"` trade-id fabrication;
                             R6/M4: ledger-close failures raised DEBUG->ERROR in
                             _book_time_barrier_flat (~2580),
                             _book_retired_leg_close (~2634),
                             _flatten_book_and_reset (~7012);
                             R8: rowcount-0 WARNING;
                             A1: _book_time_barrier_flat success INFO line)
Secondary Targets: src/live_execution/utils/telegram_alert.py (R7: timeout and
                            catch-all send failures raised DEBUG->WARNING)
                   src/live_execution/telemetry.py (R8: close_position returns
                            the UPDATE rowcount)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: exit-fill-observability_08112026_1749

Phase: RED. Reference incident 2026-08-11 01:08:50 — a CL take-profit filled
and the ONLY operator-visible record was `[OCA] cancelled 0 resting protective
order(s) after TP_HIT`. No fill price, qty, side, entry, bars held, PnL or
trade id; no Telegram; heartbeat realized PnL moved -7,403.28 -> -3,958.00 with
no narrative in between (blueprint.md, this ticket folder).

Assertion style: SUBSTANCE, not punctuation. The Coder owns the exact wording
and separators; these tests check that the named fields and their values reach
the record, that the level is right, and — critically (M1) — that
_reset_position_state is still reached when the new statements blow up.

Deterministic, no real I/O: LiveTrader built via the object.__new__ stub
pattern from tests/test_exit_reason_and_fill_routing.py and
tests/test_commission_capture.py; the only real object is a tmp_path
TelemetryDB for the R8 rowcount contract.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pandas as pd
import pytest

from src.live_execution.live_trader import LiveTrader
from src.live_execution.interfaces.execution_interface import (
    StandardCommissionEvent,
    StandardExecutionEvent,
)
from src.live_execution.telemetry import TelemetryDB
from src.live_execution.utils import telegram_alert as tg_module


# ---------------------------------------------------------------------------
# Incident-shaped constants (trade_212, CL, 2026-08-11 01:08:50)
# ---------------------------------------------------------------------------

TRADE_ID = "trade_212"
TP_ORDER_ID = "213"
SL_ORDER_ID = "214"
ENTRY_PRICE = 80.63
EXIT_PRICE = 84.08
QTY = 3
BARS_HELD = 17
# CL multiplier = 1000 (src/core/instrument_master.py) ->
# (84.08 - 80.63) * +1 * 3 * 1000 = 10350.00
GROSS_EST = (EXIT_PRICE - ENTRY_PRICE) * 1 * QTY * 1000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_exit_trader(*, is_tp: bool = True) -> LiveTrader:
    """LiveTrader stub carrying every attribute the TP/SL exit branch reads.

    Extends the _make_trader stub in tests/test_exit_reason_and_fill_routing.py
    with the position-context attributes the EXIT FILLED line sources
    (_entry_price, _trailing_activated) and the health-event seam.
    """
    t = LiveTrader.__new__(LiveTrader)
    t.exec_client = MagicMock()
    t.exec_client.cancel_open_orders.return_value = 0  # native OCA already swept
    t.telemetry = MagicMock()
    # M5: an unconfigured MagicMock return makes `rowcount == 0` vacuously
    # False and the rowcount test tautological. Always configure explicitly.
    t.telemetry.close_position.return_value = 1
    t._telegram = MagicMock()
    t._telegram.send.return_value = True
    t._execution_symbol = "CL"
    t._front_month_str = "202609"
    t._exit_mode = "MKT"
    t._open_orders = {}
    t._processed_exit_order_ids = set()
    t._processed_entry_order_ids = set()
    t._last_decision_context_by_order_id = {}
    t._entry_order_ids = set()
    t._retiring_leg_ids = []
    t._tp_order_ids = [int(TP_ORDER_ID)] if is_tp else []
    t._sl_order_id = int(SL_ORDER_ID) if not is_tp else None
    t._active_trade_id = TRADE_ID
    t._position_side = 1
    t._position_bars_held = BARS_HELD
    t._position_entry_bar_time = None
    t._entry_price = ENTRY_PRICE
    t._trailing_activated = False
    t._atr_at_entry = 0.5
    t._trade_trailing_atr_mult = None
    t._trade_max_hold_bars = None
    t._max_hold_bars = 240
    t._pending_entry_order_id = None
    t._pending_exit_order_id = None
    t._time_barrier_exit_attempts = 0
    t._build_event_id = MagicMock(return_value="evt-test")
    t._base_tradebook_fields = MagicMock(return_value={})
    # Seams the new observability code drives; mocked so the tests can assert
    # on them without touching real state or the fleet error queue.
    t._reset_position_state = MagicMock()
    t._emit_health_event = MagicMock()
    return t


def _exit_fill_event(order_id: str, action: str = "SELL") -> StandardExecutionEvent:
    raw_order = SimpleNamespace(
        action=action, permId=None, parentId=None, account=None,
    )
    raw = SimpleNamespace(
        order=raw_order, contract=SimpleNamespace(symbol="CL"),
    )
    return StandardExecutionEvent(
        order_id=order_id,
        symbol="CL",
        status="Filled",
        filled_qty=QTY,
        remaining_qty=0,
        avg_price=EXIT_PRICE,
        raw_event=raw,
    )


def _msgs(caplog, min_level: int = logging.NOTSET) -> list:
    return [
        r.getMessage() for r in caplog.records if r.levelno >= min_level
    ]


def _exit_record(caplog):
    """The single [TRADE] EXIT FILLED operator line (R1)."""
    hits = [
        r for r in caplog.records
        if "EXIT FILLED" in r.getMessage().upper()
    ]
    assert hits, (
        "R1: no EXIT FILLED operator line was emitted for the exit fill — this "
        "is the 2026-08-11 blindness. Records seen: "
        + repr(_msgs(caplog))
    )
    assert len(hits) == 1, (
        f"exactly ONE EXIT FILLED line per exit fill expected, got {len(hits)}"
    )
    return hits[0]


def _field(msg: str, name: str):
    """Extract `name[=|:] value` from an operator line, tolerant of the
    Coder's choice of separator/ordering. Returns None when absent."""
    m = re.search(
        rf"(?<![\w]){re.escape(name)}\w*\s*[=:]\s*([^\s,;|)\]]+)",
        msg,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def _commission_event(commission=2.36, realized=3445.28) -> StandardCommissionEvent:
    return StandardCommissionEvent(
        order_id=TP_ORDER_ID,
        exec_id="0000e1a7.686e1",
        symbol="CL",
        commission=commission,
        realized_pnl=realized,
        currency="USD",
    )


def _make_commission_trader() -> LiveTrader:
    lt = LiveTrader.__new__(LiveTrader)
    lt.telemetry = MagicMock()
    lt._utc_iso_now = lambda: "2026-08-11T08:08:51.480000"
    return lt


# ===========================================================================
# R1 / R2a — the EXIT FILLED operator line
# ===========================================================================


class TestExitFilledLine:

    def test_tp_fill_emits_exit_filled_info_line(self, caplog):
        """(Req 1) A take-profit fill must emit ONE INFO line carrying order
        id, reason, fill, qty, entry, bars_held, trade_id, gross_est and
        oca_cancelled. Today the branch's only success-path log is the
        `[OCA] cancelled ...` line."""
        t = _make_exit_trader(is_tp=True)

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        rec = _exit_record(caplog)
        msg = rec.getMessage()
        assert rec.levelno == logging.INFO, (
            f"EXIT FILLED must be INFO (the fleet handler's level), got "
            f"{rec.levelname}"
        )
        assert TP_ORDER_ID in msg, f"order id missing: {msg!r}"
        assert "TP_HIT" in msg, f"exit reason missing: {msg!r}"
        assert "84.08" in msg, f"fill price missing: {msg!r}"
        assert "80.63" in msg, f"entry price missing: {msg!r}"
        assert TRADE_ID in msg, f"trade id missing: {msg!r}"
        assert _field(msg, "qty") in {"3", "3.0", "3.00"}, (
            f"qty field missing/wrong: {msg!r}"
        )
        assert _field(msg, "bars_held") == str(BARS_HELD), (
            f"bars_held field missing/wrong: {msg!r}"
        )
        assert _field(msg, "oca_cancelled") == "0", (
            f"oca_cancelled must be recorded as a FIELD (finding 6: 0 is "
            f"benign, the broker already swept the sibling): {msg!r}"
        )
        gross = _field(msg, "gross_est")
        assert gross is not None, f"gross_est field missing: {msg!r}"
        assert re.search(r"10[,]?350", gross), (
            f"gross_est must be (84.08-80.63)*1*3*1000 = {GROSS_EST:.2f}, got "
            f"{gross!r} in {msg!r}"
        )

    def test_sl_fill_emits_identical_exit_filled_line(self, caplog):
        """(Req 2) TP and SL take the IDENTICAL branch (live_trader.py:7235).
        The operator's impression that SL hits are chattier is ib_insync's
        cancelOrder noise, not ours — so the SL line must carry the same
        fields."""
        t = _make_exit_trader(is_tp=False)

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(SL_ORDER_ID))

        rec = _exit_record(caplog)
        msg = rec.getMessage()
        assert rec.levelno == logging.INFO
        assert SL_ORDER_ID in msg, f"order id missing: {msg!r}"
        assert "SL_HIT" in msg, f"exit reason missing: {msg!r}"
        assert "84.08" in msg, f"fill price missing: {msg!r}"
        assert "80.63" in msg, f"entry price missing: {msg!r}"
        assert TRADE_ID in msg, f"trade id missing: {msg!r}"
        assert _field(msg, "qty") in {"3", "3.0", "3.00"}
        assert _field(msg, "bars_held") == str(BARS_HELD)
        assert _field(msg, "oca_cancelled") == "0"
        assert _field(msg, "gross_est") is not None

    def test_exit_filled_line_carries_position_context(self, caplog):
        """R1's remaining table columns: symbol, action, side and the trailing
        latch (which distinguishes a trailed stop from the original SL)."""
        t = _make_exit_trader(is_tp=False)
        t._trailing_activated = True

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(
                _exit_fill_event(SL_ORDER_ID, action="SELL")
            )

        msg = _exit_record(caplog).getMessage()
        assert "CL" in msg, f"execution symbol missing: {msg!r}"
        assert "SELL" in msg, f"fill action missing: {msg!r}"
        assert "LONG" in msg.upper(), f"position side missing: {msg!r}"
        assert _field(msg, "trailing") is not None, (
            f"trailing latch missing — a trailed stop must be "
            f"distinguishable from the original SL: {msg!r}"
        )

    def test_gross_est_is_na_when_entry_price_unknown(self, caplog):
        """(Req 3 / M2) `_entry_price is None` must render n/a. A 0.0 here is
        a fabricated number on a money line."""
        t = _make_exit_trader(is_tp=True)
        t._entry_price = None

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        msg = _exit_record(caplog).getMessage()
        gross = _field(msg, "gross_est")
        assert gross is not None, f"gross_est field missing: {msg!r}"
        assert gross.lower().strip("$") in {"n/a", "na"}, (
            f"gross_est must be n/a when the entry price is unknown, got "
            f"{gross!r} in {msg!r}"
        )
        assert not re.fullmatch(r"\$?-?0(\.0+)?", gross), (
            f"gross_est rendered a fabricated 0.0: {msg!r}"
        )
        entry = _field(msg, "entry")
        assert entry is None or not re.fullmatch(r"-?0(\.0+)?", entry), (
            f"entry price rendered a fabricated 0.0 instead of n/a: {msg!r}"
        )
        t._reset_position_state.assert_called_once_with(reason="TP_HIT")

    @pytest.mark.parametrize("raise_at", ["property", "multiplier"])
    def test_degenerate_exit_line_inputs_still_reset_state(self, caplog, raise_at):
        """(Req 10 / A2 / M1 — CRITICAL SAFETY) Every new statement in the
        exit branch must be individually exception-isolated.

        ibkr_execution.py:112-113 dispatches order-status callbacks with NO
        exception isolation, so anything escaping this branch strands
        _position_side / _active_trade_id / TP-SL tracking in memory while the
        position is flat broker-side — the phantom-position class this fleet
        has been burned by. Degenerate inputs must degrade the line, never the
        reset.
        """
        t = _make_exit_trader(is_tp=True)
        t._entry_price = None
        t._position_side = 0

        if raise_at == "property":
            # get_instrument raises on an unknown symbol / missing seam
            # (live_trader.py:4525-4539).
            prop = PropertyMock(side_effect=ValueError("no such instrument"))
        else:
            class _BoomInstrument:
                @property
                def multiplier(self):
                    raise ValueError("multiplier unavailable")
            prop = PropertyMock(return_value=_BoomInstrument())

        with patch.object(LiveTrader, "_execution_instrument", prop):
            with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
                # Must NOT raise — an escaping exception breaks the loop.
                t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        t._reset_position_state.assert_called_once_with(reason="TP_HIT")
        # M1: degrade to a minimal orderId/reason/fill line, never nothing.
        msg = _exit_record(caplog).getMessage()
        assert TP_ORDER_ID in msg
        assert "TP_HIT" in msg
        assert "84.08" in msg


# ===========================================================================
# R4 / R5 / R8 / M3 / M5 — ledger-close visibility on the exit branch
# ===========================================================================


class TestExitLedgerCloseVisibility:

    @staticmethod
    def _health_kinds(mock_emit) -> list:
        kinds = []
        for call in mock_emit.call_args_list:
            if call.args:
                kinds.append(call.args[0])
            elif "kind" in call.kwargs:
                kinds.append(call.kwargs["kind"])
        return kinds

    def test_ledger_close_exception_is_loud_and_still_resets(self, caplog):
        """(Req 4 / R4 / M3) A silent failure here leaves the active_positions
        row OPEN forever, and get_open_position + restart-recovery +
        kill-switch all read that ledger. It must log ERROR, emit the
        exit-ledger-close-failed health event, STILL reset in-memory state
        (the position IS flat broker-side) and NEVER re-raise."""
        t = _make_exit_trader(is_tp=True)
        t.telemetry.close_position.side_effect = sqlite3.OperationalError(
            "database is locked"
        )

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        errors = [
            r for r in caplog.records
            if r.levelno >= logging.ERROR and TRADE_ID in r.getMessage()
        ]
        assert errors, (
            "R4: ledger-close failure must log at ERROR naming the trade id; "
            f"records seen: {_msgs(caplog)!r}"
        )
        assert self._health_kinds(t._emit_health_event) == [
            "exit-ledger-close-failed"
        ], (
            "R4: _emit_health_event('exit-ledger-close-failed', detail) must "
            f"fire exactly once; got {t._emit_health_event.call_args_list!r}"
        )
        t._reset_position_state.assert_called_once_with(reason="TP_HIT")

    def test_ledger_close_failure_telegram_is_capped_at_two_sends(self, caplog):
        """(M3) The branch may send at most TWO Telegrams total — each costs up
        to 3s and a Markdown-400 retry doubles that."""
        t = _make_exit_trader(is_tp=True)
        t.telemetry.close_position.side_effect = RuntimeError("db gone")

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        assert t._telegram.send.call_count <= 2, (
            f"M3 caps the exit branch at two Telegram sends, got "
            f"{t._telegram.send.call_count}"
        )
        assert t._telegram.send.call_count == 2, (
            "R4 requires a CRITICAL Telegram in addition to the routine "
            "POSITION CLOSED alert when the ledger close fails"
        )

    def test_rowcount_zero_warns_without_telegram_or_health_event(self, caplog):
        """(Req 5 / R8 / M5) rowcount 0 = the UPDATE matched nothing (row
        already CLOSED by another path, or the shared-fleet-DB _client_scope
        binding did not match). R4 is structurally blind to it because
        close_position raises nothing. WARNING only — no Telegram, no health
        event."""
        t = _make_exit_trader(is_tp=True)
        t.telemetry.close_position.return_value = 0

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        warns = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and TRADE_ID in r.getMessage()
            and "ledger" in r.getMessage().lower()
        ]
        assert warns, (
            "R8: a rowcount-0 ledger close must WARN naming the trade id; "
            f"records seen: {_msgs(caplog)!r}"
        )
        assert not any(
            r.levelno >= logging.ERROR for r in caplog.records
        ), "M5: rowcount 0 is not an error-level event"
        assert self._health_kinds(t._emit_health_event) == [], (
            "M5: rowcount 0 must NOT emit a health event"
        )
        assert t._telegram.send.call_count == 1, (
            "M5: rowcount 0 must NOT send an extra Telegram (only the routine "
            "POSITION CLOSED alert)"
        )

    def test_rowcount_one_is_silent(self, caplog):
        """The happy path must not warn — R8's check is for the 0 case only."""
        t = _make_exit_trader(is_tp=True)
        t.telemetry.close_position.return_value = 1

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        assert not [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ], f"clean exit must emit no warnings: {_msgs(caplog, logging.WARNING)!r}"

    def test_missing_trade_id_never_fabricates_unknown(self, caplog):
        """(Req 7 / R5) close_position is
        `UPDATE ... WHERE trade_id='unknown' AND status='OPEN'` — it matches
        nothing, commits cleanly and raises nothing, so even R4's logging is
        structurally blind to it. Project rule: no silent null defaults."""
        t = _make_exit_trader(is_tp=True)
        t._active_trade_id = None

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        assert "unknown" not in str(t.telemetry.close_position.call_args_list), (
            "R5: the fabricated 'unknown' trade id must never reach the "
            f"ledger; got {t.telemetry.close_position.call_args_list!r}"
        )
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, (
            "R5: a missing _active_trade_id must log an ERROR stating the exit "
            f"cannot be booked to any ledger row; records: {_msgs(caplog)!r}"
        )
        t._reset_position_state.assert_called_once_with(reason="TP_HIT")


# ===========================================================================
# R3 — Telegram delivery is no longer invisible
# ===========================================================================


class TestExitTelegramVisibility:

    @staticmethod
    def _delivery_warnings(caplog) -> list:
        return [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and (
                "telegram" in r.getMessage().lower()
                or "alert" in r.getMessage().lower()
            )
        ]

    def test_send_returning_false_is_warned(self, caplog):
        """(Req 6 / R3) send() returns False on failure and the exit site
        discards it. This is the single change that would have made the 01:08
        incident self-explaining."""
        t = _make_exit_trader(is_tp=True)
        t._telegram.send.return_value = False

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        warns = self._delivery_warnings(caplog)
        assert warns, (
            "R3: a False return from _telegram.send must WARN that the alert "
            f"was NOT delivered; records: {_msgs(caplog)!r}"
        )
        assert any(TRADE_ID in r.getMessage() for r in warns), (
            f"the undelivered-alert warning must name the trade: "
            f"{[r.getMessage() for r in warns]!r}"
        )
        # The fill IS booked regardless.
        t.telemetry.close_position.assert_called_once()
        t._reset_position_state.assert_called_once_with(reason="TP_HIT")

    def test_send_raising_is_warned_and_fill_handling_completes(self, caplog):
        """(Req 6 / R3) Keep the except — to_ascii() and datetime.now(tz) sit
        OUTSIDE send()'s internal try (telegram_alert.py:114,119) — but log it
        at WARNING with exc_info instead of `pass`."""
        t = _make_exit_trader(is_tp=True)
        t._telegram.send.side_effect = RuntimeError("tz database missing")

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        assert self._delivery_warnings(caplog), (
            "R3: `except Exception: pass` around the exit Telegram must become "
            f"a WARNING; records: {_msgs(caplog)!r}"
        )
        t.telemetry.close_position.assert_called_once()
        t._reset_position_state.assert_called_once_with(reason="TP_HIT")


# ===========================================================================
# R2b — the commission line (the "realized moved with no narrative" answer)
# ===========================================================================


class TestCommissionObservability:

    def test_commission_event_logs_info_with_commission_and_realized(self, caplog):
        """(Req 8 / R2b) tradebook_events showed
        `COMMISSION 08:08:51.480 order 213 commission 2.36 realized_pnl
        3445.28` while the operator log said nothing. The heartbeat's `real=`
        is a SUM over that column (live_trader.py:6603)."""
        lt = _make_commission_trader()

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            lt._on_commission_event(_commission_event())

        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert infos, (
            "R2b: _on_commission_event logs only on failure today; it must "
            f"emit an INFO line. Records: {_msgs(caplog)!r}"
        )
        hits = [
            r for r in infos
            if "2.36" in r.getMessage() and "3445.28" in r.getMessage()
        ]
        assert hits, (
            "R2b: the COMMISSION INFO line must carry commission and "
            f"realized_pnl; got {[r.getMessage() for r in infos]!r}"
        )
        msg = hits[0].getMessage()
        assert TP_ORDER_ID in msg, f"order id missing: {msg!r}"
        assert "0000e1a7.686e1" in msg, f"exec id missing: {msg!r}"

    def test_commission_line_survives_null_realized_pnl(self, caplog):
        """Entry fills carry IBKR's DBL_MAX sentinel, mapped to None by the
        adapter — the new line must render it, not crash."""
        lt = _make_commission_trader()

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            lt._on_commission_event(_commission_event(realized=None))

        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("2.36" in r.getMessage() for r in infos), (
            f"commission INFO line missing: {_msgs(caplog)!r}"
        )
        # No crash, and no fabricated 0.0 standing in for an absent figure.
        msg = next(r.getMessage() for r in infos if "2.36" in r.getMessage())
        realized = _field(msg, "realized_pnl")
        assert realized is None or not re.fullmatch(r"-?0(\.0+)?", realized), (
            f"an absent realized_pnl must not render as 0.0: {msg!r}"
        )


# ===========================================================================
# R6 / M4 — the three DEBUG-buried ledger-close failures
# ===========================================================================


def _make_booking_trader() -> LiveTrader:
    t = LiveTrader.__new__(LiveTrader)
    t.exec_client = MagicMock()
    t.telemetry = MagicMock()
    t.telemetry.close_position.return_value = 1
    t._telegram = MagicMock()
    t._telegram.send.return_value = True
    t._execution_symbol = "CL"
    t._active_trade_id = TRADE_ID
    t._position_bars_held = BARS_HELD
    t._processed_exit_order_ids = set()
    t._retiring_leg_ids = []
    t._retiring_sl_id = None
    t._reset_position_state = MagicMock()
    t._clear_pending_entry = MagicMock()
    t.rolling_df_5m = pd.DataFrame(
        {"Close": [84.08]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-08-11 01:00:00")]),
    )
    return t


class TestLedgerCloseFailuresAreLoud:

    @staticmethod
    def _ledger_errors(caplog) -> list:
        return [
            r for r in caplog.records
            if r.levelno >= logging.ERROR
            and "ledger" in r.getMessage().lower()
        ]

    def test_book_time_barrier_flat_logs_error(self, caplog):
        """R6 site 1 (~2580): today log.debug, invisible under the INFO fleet
        handler, leaving the row OPEN forever."""
        t = _make_booking_trader()
        t._resolve_exit_fill_price = MagicMock(return_value=EXIT_PRICE)
        t.telemetry.close_position.side_effect = RuntimeError("db locked")

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._book_time_barrier_flat(71, "TIME_BARRIER")

        assert self._ledger_errors(caplog), (
            "R6: _book_time_barrier_flat's ledger-close failure must be ERROR, "
            f"not DEBUG; records: {_msgs(caplog)!r}"
        )
        t._reset_position_state.assert_called_once_with(reason="TIME_BARRIER")

    def test_book_retired_leg_close_logs_error(self, caplog):
        """R6 site 2 (~2634)."""
        t = _make_booking_trader()
        t.exec_client.get_executions.return_value = []
        t.telemetry.close_position.side_effect = RuntimeError("db locked")

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._book_retired_leg_close(["66"])

        assert self._ledger_errors(caplog), (
            "R6: _book_retired_leg_close's ledger-close failure must be ERROR, "
            f"not DEBUG; records: {_msgs(caplog)!r}"
        )
        t._reset_position_state.assert_called_once_with(reason="CLOSED_OOB")

    def test_flatten_book_and_reset_logs_error(self, caplog):
        """R6 site 3 / M4 (~7012) — fixing two of three would be arbitrary."""
        t = _make_booking_trader()
        t.exec_client.cancel_open_orders.return_value = 0
        t.exec_client.close_position.return_value = SimpleNamespace(
            order=SimpleNamespace(orderId=99),
        )
        t.telemetry.close_position.side_effect = RuntimeError("db locked")

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._flatten_book_and_reset(
                reason="NAKED_POSITION_KILL_SWITCH",
                telegram_text="[CRITICAL] flatten",
                ledger_trade_id=TRADE_ID,
            )

        assert self._ledger_errors(caplog), (
            "M4: _flatten_book_and_reset's ledger-close failure must be ERROR, "
            f"not DEBUG; records: {_msgs(caplog)!r}"
        )
        t._reset_position_state.assert_called_once_with(
            reason="NAKED_POSITION_KILL_SWITCH"
        )


# ===========================================================================
# A1 — _book_time_barrier_flat's silent success
# ===========================================================================


class TestTimeBarrierBookSuccessLine:

    @staticmethod
    def _book_info(caplog):
        hits = [
            r for r in caplog.records
            if r.levelno == logging.INFO and TRADE_ID in r.getMessage()
        ]
        assert hits, (
            "A1: _book_time_barrier_flat closes the ledger and resets state in "
            "TOTAL silence — blinder than the path the operator reported. "
            f"Records: {_msgs(caplog)!r}"
        )
        return hits[0]

    def test_time_barrier_book_logs_success_line(self, caplog):
        t = _make_booking_trader()
        t._resolve_exit_fill_price = MagicMock(return_value=EXIT_PRICE)

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._book_time_barrier_flat(71, "TIME_BARRIER")

        msg = self._book_info(caplog).getMessage()
        assert "TIME_BARRIER" in msg, f"reason missing: {msg!r}"
        assert "84.08" in msg, f"exit price missing: {msg!r}"
        assert _field(msg, "bars_held") == str(BARS_HELD), (
            f"bars_held missing: {msg!r}"
        )

    def test_time_barrier_book_renders_na_exit_price(self, caplog):
        """_resolve_exit_fill_price returns None when no execution matches —
        an explicit unknown, never a fabricated price."""
        t = _make_booking_trader()
        t._resolve_exit_fill_price = MagicMock(return_value=None)

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._book_time_barrier_flat(71, "ROLLOVER_FORCE_CLOSE")

        msg = self._book_info(caplog).getMessage()
        assert "ROLLOVER_FORCE_CLOSE" in msg
        assert "n/a" in msg.lower(), (
            f"an unknown exit price must render n/a, not 0.0/None: {msg!r}"
        )


# ===========================================================================
# R7 — telegram_alert.py: stop hiding lost alerts
# ===========================================================================


@pytest.mark.skipif(
    tg_module.requests is None, reason="requests not installed"
)
class TestTelegramAlerterFailureVisibility:

    @staticmethod
    def _alerter():
        return tg_module.TelegramAlerter(token="tok", chat_id="chat")

    def test_timeout_logs_warning_and_returns_false(self, caplog):
        """R7: line 157 is DEBUG today and setup_fleet_logging attaches the
        root handler at INFO (fleet_log.py:163) — every timed-out send is a
        LOST operator alert that leaves no trace."""
        alerter = self._alerter()
        timeout = tg_module.requests.exceptions.Timeout

        with patch.object(tg_module.requests, "post", side_effect=timeout()):
            with caplog.at_level(logging.DEBUG, logger="TelegramAlerter"):
                result = alerter.send("EXIT FILLED test")

        assert result is False
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warns, (
            "R7: the timeout path must WARN (the non-200 branch two lines "
            f"above already does); records: {_msgs(caplog)!r}"
        )
        assert any(
            "telegram" in r.getMessage().lower() for r in warns
        ), f"the warning must name Telegram: {[r.getMessage() for r in warns]!r}"

    def test_catch_all_failure_logs_warning_and_returns_false(self, caplog):
        """R7: line 162 catch-all (DNS failure, connection reset, SSL error).
        send()'s "Never raises" contract is untouched."""
        alerter = self._alerter()

        with patch.object(
            tg_module.requests, "post", side_effect=OSError("connection reset")
        ):
            with caplog.at_level(logging.DEBUG, logger="TelegramAlerter"):
                result = alerter.send("EXIT FILLED test")

        assert result is False
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warns, (
            "R7: the catch-all path must WARN; records: "
            f"{_msgs(caplog)!r}"
        )

    def test_failure_warnings_are_ascii(self, caplog):
        """Req 9 / project rule: operator strings are plain ASCII. The current
        timeout message carries an em-dash."""
        alerter = self._alerter()
        timeout = tg_module.requests.exceptions.Timeout

        with patch.object(tg_module.requests, "post", side_effect=timeout()):
            with caplog.at_level(logging.DEBUG, logger="TelegramAlerter"):
                alerter.send("EXIT FILLED test")
        with patch.object(
            tg_module.requests, "post", side_effect=OSError("boom")
        ):
            with caplog.at_level(logging.DEBUG, logger="TelegramAlerter"):
                alerter.send("EXIT FILLED test")

        for rec in caplog.records:
            msg = rec.getMessage()
            assert msg.isascii(), f"non-ASCII operator string: {msg!r}"


# ===========================================================================
# R8 — telemetry.close_position returns the UPDATE rowcount
# ===========================================================================


class TestClosePositionRowcount:
    """R8 contract, against a real tmp_path SQLite ledger;
    see telemetry.py:1194-1214."""

    @pytest.fixture
    def db(self, tmp_path):
        d = TelemetryDB(str(tmp_path / "fleet_telemetry.db"))
        yield d
        d.close()

    @staticmethod
    def _open(db, trade_id=TRADE_ID):
        db.open_position(
            trade_id=trade_id,
            side="LONG",
            quantity=1,
            entry_price=ENTRY_PRICE,
            entry_order_id=212,
            atr_at_entry=0.5,
            entry_time="2026-08-11T07:00:00",
        )

    def test_close_position_returns_one_on_match(self, db):
        self._open(db)
        rc = db.close_position(
            TRADE_ID,
            reason="TP_HIT",
            close_time="2026-08-11T08:08:51",
            bars_held=BARS_HELD,
            exit_price=EXIT_PRICE,
        )
        assert isinstance(rc, int), (
            f"R8: close_position must return the UPDATE rowcount, got {rc!r}"
        )
        assert rc == 1

    def test_close_position_returns_zero_when_nothing_matched(self, db):
        """The class R4 cannot see: the row is already CLOSED (or the
        _client_scope binding did not match) — the UPDATE commits cleanly and
        raises nothing."""
        self._open(db)
        db.close_position(
            TRADE_ID, reason="TP_HIT", close_time="2026-08-11T08:08:51",
        )
        rc = db.close_position(
            TRADE_ID, reason="TP_HIT", close_time="2026-08-11T08:09:00",
        )
        assert rc == 0, (
            f"R8: a second close must report rowcount 0, got {rc!r}"
        )

    def test_close_position_returns_zero_for_unknown_trade_id(self, db):
        rc = db.close_position(
            "unknown", reason="TP_HIT", close_time="2026-08-11T08:08:51",
        )
        assert rc == 0


# ===========================================================================
# Req 9 — every new operator string is ASCII
# ===========================================================================


class TestOperatorStringsAreAscii:

    @pytest.mark.parametrize(
        "scenario",
        ["clean", "ledger_raises", "rowcount_zero", "telegram_false",
         "no_trade_id"],
    )
    def test_exit_branch_strings_are_ascii(self, caplog, scenario):
        """Project rule (07-19): logs and Telegram are plain ASCII, no emoji —
        non-ASCII crashed sinks before. ascii_safe.py is a net, not a license.
        """
        t = _make_exit_trader(is_tp=True)
        if scenario == "ledger_raises":
            t.telemetry.close_position.side_effect = RuntimeError("db locked")
        elif scenario == "rowcount_zero":
            t.telemetry.close_position.return_value = 0
        elif scenario == "telegram_false":
            t._telegram.send.return_value = False
        elif scenario == "no_trade_id":
            t._active_trade_id = None

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._on_standard_execution_event(_exit_fill_event(TP_ORDER_ID))

        # RED anchor: the EXIT FILLED line must exist before it can be checked.
        _exit_record(caplog)
        for rec in caplog.records:
            msg = rec.getMessage()
            assert msg.isascii(), f"non-ASCII log string: {msg!r}"
        for call in t._telegram.send.call_args_list:
            for arg in list(call.args) + list(call.kwargs.values()):
                if isinstance(arg, str):
                    assert arg.isascii(), f"non-ASCII Telegram text: {arg!r}"

    def test_commission_line_is_ascii(self, caplog):
        lt = _make_commission_trader()

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            lt._on_commission_event(_commission_event())

        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert infos, (
            f"R2b INFO line missing (RED anchor): {_msgs(caplog)!r}"
        )
        for rec in caplog.records:
            assert rec.getMessage().isascii(), (
                f"non-ASCII log string: {rec.getMessage()!r}"
            )

    def test_time_barrier_success_line_is_ascii(self, caplog):
        t = _make_booking_trader()
        t._resolve_exit_fill_price = MagicMock(return_value=EXIT_PRICE)

        with caplog.at_level(logging.DEBUG, logger="LiveTrader"):
            t._book_time_barrier_flat(71, "TIME_BARRIER")

        infos = [
            r for r in caplog.records
            if r.levelno == logging.INFO and TRADE_ID in r.getMessage()
        ]
        assert infos, f"A1 INFO line missing (RED anchor): {_msgs(caplog)!r}"
        for rec in caplog.records:
            assert rec.getMessage().isascii(), (
                f"non-ASCII log string: {rec.getMessage()!r}"
            )
