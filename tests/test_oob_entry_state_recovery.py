"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/interfaces/execution_interface.py
                            (D1: NEW ExecutionClient methods
                             cancel_orders_by_ids(order_ids) -> int and
                             get_executions(symbol=None) -> list[dict]),
                            src/live_execution/adapters/ibkr_execution.py
                            (D1: implementations — cancel_orders_by_ids looks
                             up manager.ib.openTrades() and cancels EXACTLY
                             the requested order ids IRRESPECTIVE of contract
                             symbol via manager.ib.cancelOrder(order);
                             get_executions wraps manager.ib.fills() into
                             flat dict records — see RECORD CONTRACT below),
                            src/live_execution/adapters/simulated_execution.py
                            (A4: honest sim-domain implementations or loud
                             NotImplementedError — never fabricated success),
                            src/live_execution/live_trader.py
                            (D1: _recover_inherited_position OOB branch —
                             targeted cancel of the ledger row's exact
                             tp_order_id/sl_order_id THEN the bulk sweep;
                             execution-matched truthful close reasons
                             SL_HIT_OOB / TP_HIT_OOB with exit_price;
                             CLOSED_OOB_UNRECOVERED with NULL exit_price when
                             executions are unavailable; ERROR + Telegram —
                             never log.debug — when a protective order is
                             neither found open nor provably done;
                             A5: recovery-written EXECUTION_FILL/COMMISSION
                             tradebook rows use DETERMINISTIC event_ids
                             (execId / order-id+permId keyed), NOT the
                             timestamp-based _build_event_id;
                             D2: submission stores a PENDING-ENTRY record
                             only (no _entry_price/_atr_at_entry/
                             _position_side/extremes/_position_entry_bar_time
                             at placement); the confirmed-fill entry branch
                             seeds ALL in-position state from the FILL
                             (A3: _entry_price := avg_price, independent of
                             bracket-children success), clears
                             _pending_entry_order_id eagerly, and ERRORs +
                             Telegrams when the stored pending context cannot
                             be resolved; _check_trailing_stop hard-gates on
                             _active_trade_id is not None; NEW zero-arg
                             _clear_pending_entry() used by TTL/rollover/
                             kill-switch — clears ONLY pending state, never
                             fires strategy.on_exit for a never-filled entry;
                             A1: a partially-filled pending order is NOT
                             never-filled — ERROR + Telegram;
                             A2: real-close paths (rollover force-close,
                             kill-switch) still run the full in-position
                             reset incl. strategy.on_exit;
                             A8: bulk symbol-scoped cancel_open_orders call
                             sites unchanged — software-OCA exit, TTL,
                             kill-switch, startup sweep),
                            src/live_execution/strategies/configurable_strategy.py
                            (A7: both SL-flavored cooldown tuples gain
                             SL_HIT_OOB + CLOSED_OOB_UNRECOVERED; TP_HIT_OOB
                             is CONSCIOUSLY EXCLUDED),
                            scripts/trade_reconciler.py
                            (A7: _EXIT_REASON_MAP maps SL_HIT_OOB /
                             TP_HIT_OOB / CLOSED_OOB_UNRECOVERED),
                            src/live_execution/fleet_health.py
                            (D3: check_positions gains ADDITIVE keyword
                             parameter fill_evidence — default None = legacy
                             conservative time-based flagging (A6); evidence
                             present = flag a NULL-fill EXECUTE row only when
                             a matching fill provably exists; close_time
                             fetched and incomplete-close scoped to 48h with
                             NULL close_time still flagging; main() queries
                             tradebook_events EXECUTION_FILL order_ids +
                             active_positions.entry_order_id and ALWAYS
                             passes fill_evidence explicitly, degrading to
                             None on legacy schemas)
Target Class/Function: ExecutionClient.cancel_orders_by_ids,
                       ExecutionClient.get_executions,
                       IBKRExecutionClient.cancel_orders_by_ids,
                       IBKRExecutionClient.get_executions,
                       SimulatedExecution.cancel_orders_by_ids,
                       SimulatedExecution.get_executions,
                       LiveTrader._recover_inherited_position,
                       LiveTrader._on_new_bar (submission block),
                       LiveTrader._on_standard_execution_event,
                       LiveTrader._place_bracket_children_on_fill,
                       LiveTrader._check_trailing_stop,
                       LiveTrader._check_entry_order_ttl,
                       LiveTrader._clear_pending_entry,
                       LiveTrader._check_contract_rollover,
                       LiveTrader._check_naked_position,
                       LiveTrader._cancel_orphaned_orders_on_startup,
                       ConfigurableStrategy cooldown tuples,
                       trade_reconciler._EXIT_REASON_MAP,
                       fleet_health.check_positions, fleet_health.main
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Adapted (killswitch-pending-exit-guard_07202026_1805): MECHANICAL fixture repair
only — _kill_switch_stub() now also declares lt._pending_exit_order_id = None and
lt._time_barrier_exit_attempts = 0, the two attrs _check_naked_position's new
bounded pending-exit guard reads (an object.__new__ stub would otherwise raise
AttributeError once that guard lands). Semantically a no-op: a GENUINE naked
position has no pending TIME BARRIER exit, so the guard skips and the kill switch
still fires. The FENCE test_kill_switch_real_close_still_fires_cooldown_and_bulk_cancel
still asserts the full flatten unchanged (contract 4: genuine-naked still flattens).
No assertion here is loosened.

Ticket: oob-entry-state-recovery_07062026_2335
Phase: RED — every blueprint requirement (D1/D2/D3) and every binding
amendment A1-A8 has at least one test that FAILS on current HEAD. Tests
explicitly marked "FENCE" in their docstrings pass on current code by
design: they pin behavior the fix must PRESERVE (bulk cancel_open_orders
call sites per A8; the A6 legacy-conservative default; TP_HIT_OOB's
conscious absence; the locked tests/test_fleet_health.py pins are mirrored
here on a legacy-schema DB and must keep passing).

LIVE-FLEET SAFETY: every I/O boundary is a MagicMock or a tmp_path DB/log/
queue. No test touches .agents/collab/error_queue, reports/fleet, the real
telemetry DB, a broker, or a process. No stub sets _health_events_enabled
(LiveTrader's opt-in gate defaults False on object.__new__ stubs, so the
production error queue is unreachable by construction).

Conventions (mirroring tests/test_reconnect_recovery_fixes.py and
tests/test_resubscribe_retry.py):
  - LiveTrader / adapter stubs via object.__new__ with ONLY the seams each
    method reads.
  - _brain_symbol/_brain_instrument/_tick_size are read-only properties —
    stubs set _instrument_context = SimpleNamespace(brain_symbol=...,
    brain_instrument=MagicMock(), execution_instrument=...), never the
    property itself.
  - pandas 1.5.3 compatible (no pandas 2.x APIs).
  - A7 tuple tests are SOURCE-LEVEL (inspect.getsource) on purpose: the
    tuples are inline literals inside evaluate(); driving evaluate() would
    require a real model registry (forbidden infra).

FIXED IMPLEMENTATION CONTRACTS (the Coder is bound to these names/shapes):
  1. ExecutionClient.cancel_orders_by_ids(order_ids) -> int  — cancels the
     given order ids irrespective of contract symbol; returns how many were
     found open and cancelled; unknown ids are simply not counted.
     IBKR impl: iterate manager.ib.openTrades(), match order.orderId against
     the requested ids (str/int-robust), cancel via
     manager.ib.cancelOrder(trade.order).
  2. ExecutionClient.get_executions(symbol=None) -> list[dict] — RECORD
     CONTRACT: each record is a dict with AT LEAST the keys
     {"order_id", "perm_id", "exec_id", "price", "qty", "side", "time",
      "symbol", "commission_report"}; commission_report is the broker
     commissionReport object (or an equivalent mapping) when present, else
     None. symbol=None returns all fills; symbol="GC" filters on
     contract.symbol. LiveTrader's OOB recovery consumes EXACTLY this shape
     (the stubs in section D feed these dicts back through exec_client).
  3. Order-id matching (recovery + fill evidence) must be str/int-robust:
     ledger ids are ints, broker events carry str(orderId) — the D-section
     execution records deliberately use str order_ids against int ledger
     ids, and test_check_positions_evidence_match... passes both {8} and
     {"8"} as evidence for an int row.
  4. LiveTrader._clear_pending_entry() is callable with NO arguments and
     clears ONLY _pending_entry_order_id/_pending_entry_bar_time (plus any
     new pending-record fields) — it must NOT touch in-position state and
     must NOT fire strategy.on_exit.
  5. fleet_health.check_positions(active_rows, ledger_rows, now, *,
     fill_evidence=None): fill_evidence is an optional collection of order
     ids proven filled (union of tradebook EXECUTION_FILL order_ids and
     active_positions.entry_order_id values). None (the default) means
     "evidence unavailable" -> legacy conservative time-based flagging (A6).
     main() ALWAYS passes fill_evidence explicitly (None only when the
     schema lacks the evidence sources — legacy DBs must keep working).
  6. Fill-time seeding source: the "fill-time bar" is the LAST row of the
     presence-selected extremes frame (rolling_df_5m if it exists, else
     rolling_df_1h) at the moment the fill callback runs —
     _position_entry_bar_time := that frame's last index value, extremes
     := that bar's High/Low. This matches _check_trailing_stop's
     presence-selection rule (live_trader.py T7/C4 comment).

DELIBERATE SEAM OMISSIONS (contract for the Coder):
  - Recovery stubs do NOT pre-set _health_events_enabled: any new health
    event emission must stay behind the existing getattr(..., False) gate.
  - Recovery/OOB stubs do NOT pre-set any NEW pending-record attribute the
    Coder introduces — new state must be stub-safe via getattr defaults,
    exactly like the existing _enable_5m_stream seams.
  - The TTL/rollover stubs pre-set TODAY'S pre-fill contamination
    (_position_side=1 etc.) on purpose: the no-cooldown assertions must hold
    even against stale in-position junk, proving the fix is structural
    (gate on confirmed fill), not cosmetic (suppressed log lines).
  - _check_entry_order_ttl partial-fill detection (A1) may read either
    evt.filled_qty or evt.raw_event.orderStatus.filled — the stub sets both
    consistently.
"""

from __future__ import annotations

import asyncio  # noqa: F401  (house import parity; async seams unused here)
import inspect
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts.trade_reconciler import _EXIT_REASON_MAP
from src.live_execution import fleet_health as fh
from src.live_execution import live_trader as lt_module
from src.live_execution.adapters import ibkr_execution as ibkr_exec_module
from src.live_execution.adapters.simulated_execution import SimulatedExecution
from src.live_execution.interfaces.execution_interface import (
    ExecutionClient,
    StandardExecutionEvent,
)
from src.live_execution.strategies import configurable_strategy as cs_module


# ===========================================================================
# Shared builders
# ===========================================================================

_BAR_T0 = pd.Timestamp("2026-07-07 14:05:00")
_EXEC_ID_TP = "0000e1b7.6866.01.01"
_EXEC_ID_SL = "0000e1b7.6866.02.01"


def _instrument_ctx(tick=0.01):
    return SimpleNamespace(
        brain_symbol="GC",
        brain_instrument=MagicMock(),
        execution_instrument=SimpleNamespace(tick_size=tick, multiplier=100.0),
    )


def _attach_identity_seams(lt):
    """Seams the real _base_tradebook_fields/_extract_contract_month read."""
    lt._run_id = "live-test-run"
    lt._session_id = "sess-1"
    lt._hostname = "test-host"
    lt._process_id = 4242
    lt._environment = "unit-test"
    lt._front_month_str = "202608"


def _attach_position_state(lt, side=1):
    """Every field the real _reset_position_state touches."""
    lt._position_entry_bar_time = _BAR_T0
    lt._position_bars_held = 4
    lt._trailing_activated = False
    lt._entry_price = 68.0
    lt._atr_at_entry = 0.42
    lt._position_side = side
    lt._highest_high = 68.35
    lt._lowest_low = 67.85
    lt._trade_trailing_atr_mult = None
    lt._trade_max_hold_bars = None
    lt._tp_order_ids = []
    lt._sl_order_id = None
    lt._tracked_tp_price = None
    lt._tracked_sl_price = None


# ---------------------------------------------------------------------------
# D1 — OOB recovery stubs
# ---------------------------------------------------------------------------

def _ledger_open_position(**over):
    pos = {
        "trade_id": "trade_777", "side": "SHORT", "entry_price": 68.90,
        "quantity": 1, "tp_order_id": 201, "sl_order_id": 202,
        "tp_price": 67.90, "sl_price": 69.90, "atr_at_entry": 0.50,
        "entry_bar_time": "2026-07-06T20:00:00",
        "trailing_atr_mult": None, "max_hold_bars": None,
    }
    pos.update(over)
    return pos


def _exec_record(order_id, price, *, side, exec_id, perm_id=9101,
                 symbol="GC", commission_report=None, qty=1):
    """get_executions RECORD CONTRACT (header contract #2).

    order_id is a STRING on purpose against int ledger ids — pins
    str/int-robust matching (contract #3)."""
    return {
        "order_id": order_id, "perm_id": perm_id, "exec_id": exec_id,
        "price": price, "qty": qty, "side": side,
        "time": "2026-07-06T22:10:05", "symbol": symbol,
        "commission_report": commission_report,
    }


def _commission_report(commission=2.25, realized=95.0):
    return SimpleNamespace(commission=commission, realizedPNL=realized,
                           currency="USD")


def _recovery_stub(executions, *, ledger_pos=None, cancel_by_ids=1):
    """LiveTrader stub with only the seams _recover_inherited_position reads
    on the ledger-OPEN / IBKR-flat (OOB) branch.

    NOTE: _health_events_enabled is deliberately NOT set (defaults False
    via getattr) — recovery must never write the production error queue
    from a test stub."""
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt._instrument_context = _instrument_ctx()
    lt.telemetry = MagicMock()
    lt.telemetry.get_open_position.return_value = (
        ledger_pos if ledger_pos is not None else _ledger_open_position()
    )
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = 0  # IBKR flat -> OOB branch
    # reconnect-false-flat-oob: recovery now CONFIRMS a flat read with a
    # settled snapshot before resolving OOB — here the flat is genuine.
    lt.exec_client.get_position_settled.return_value = 0
    lt.exec_client.cancel_orders_by_ids = MagicMock(return_value=cancel_by_ids)
    lt.exec_client.get_executions = MagicMock(return_value=executions)
    lt.exec_client.cancel_open_orders = MagicMock(return_value=0)
    lt._telegram = MagicMock()
    lt._open_orders = {}
    lt.rolling_df_5m = None
    lt.rolling_df_1h = None
    lt._bar_size = "5m"
    # re-adjudicated: cooldown-single-authority-wiring_07222026_1051 — the
    # recovery path's cooldown seeding now reaches the REAL strategy
    # attribute (the old phantom-attr guard silently skipped it here too).
    lt.strategy = MagicMock()
    # trailing-sl-no-cooldown_07222026_2050 (mechanical stub repair): the
    # seed path resolves cooldown_arming from strategy.config, a real dict
    # on every real strategy — absent field = "all" (historical default).
    lt.strategy.config = {}
    lt._position_bars_held = 0
    _attach_identity_seams(lt)
    return lt


def _close_call(lt):
    calls = lt.telemetry.close_position.call_args_list
    assert len(calls) == 1, (
        f"telemetry.close_position must be called exactly once for the OOB "
        f"trade, got {len(calls)} calls"
    )
    c = calls[0]
    trade_id = c.args[0] if c.args else c.kwargs.get("trade_id")
    return trade_id, c.kwargs


def _tradebook_calls(lt, event_type):
    return [c for c in lt.telemetry.log_tradebook_event.call_args_list
            if c.kwargs.get("event_type") == event_type]


def _cancel_by_ids_requested(lt):
    call = lt.exec_client.cancel_orders_by_ids.call_args
    ids = call.args[0] if call.args else call.kwargs.get("order_ids")
    return {int(str(i)) for i in ids}


# ---------------------------------------------------------------------------
# D2 — entry-flow stubs
# ---------------------------------------------------------------------------

def _buy_signal():
    return SimpleNamespace(
        action="BUY", probability=0.81, confidence_pct=81.0,
        signal_label="Buy", skip_reason=None, buy_prob=0.81, sell_prob=0.12,
        lots=1, tp_price=68.9, sl_price=67.4, atr_at_entry=0.42,
        trailing_atr_mult=1.7, max_hold_bars=48,
        cooldown_bars_left_long=None, cooldown_bars_left_short=None,
    )


def _submission_frame():
    idx = pd.DatetimeIndex([_BAR_T0 - pd.Timedelta(minutes=5), _BAR_T0])
    return pd.DataFrame({
        "Open": [67.80, 67.95], "High": [68.10, 68.35],
        "Low": [67.60, 67.85], "Close": [67.90, 68.00],
        "Volume": [900.0, 1000.0],
    }, index=idx)


def _fill_frame():
    """Fill-time frame whose LAST bar is an INSIDE bar (High 68.20 < the
    68.35 accumulated at submission; Low 67.95 > 67.85): re-seeded extremes
    (the requirement) and accumulated extremes (today's bug) diverge."""
    idx = pd.DatetimeIndex([
        _BAR_T0 - pd.Timedelta(minutes=5), _BAR_T0,
        _BAR_T0 + pd.Timedelta(minutes=5),
    ])
    return pd.DataFrame({
        "Open": [67.80, 67.95, 68.00], "High": [68.10, 68.35, 68.20],
        "Low": [67.60, 67.85, 67.95], "Close": [67.90, 68.00, 68.15],
        "Volume": [900.0, 1000.0, 1100.0],
    }, index=idx)


def _child_trades():
    def _t(oid, otype, action, lmt=0.0, aux=0.0):
        return SimpleNamespace(
            order=SimpleNamespace(
                orderId=oid, permId=oid * 10, parentId=901, account="DU111",
                action=action, orderType=otype, tif="GTC", totalQuantity=1,
                lmtPrice=lmt, auxPrice=aux,
            ),
            contract=SimpleNamespace(
                symbol="GC", localSymbol="GCQ6",
                lastTradeDateOrContractMonth="20260828",
            ),
        )
    return [_t(902, "LMT", "SELL", lmt=69.47), _t(903, "STP", "SELL", aux=68.63)]


def _entry_flow_stub(signal=None):
    """Stub with every seam _on_new_bar's live EXECUTE path reads
    (build_live_features is patched at module level by the tests; ATR
    periods exceed the frame length so pandas_ta is never touched)."""
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt._instrument_context = _instrument_ctx()
    lt._emergency_halt = False
    lt._rollover_in_progress = False
    lt._check_entry_order_ttl = MagicMock()
    lt._check_time_barrier = MagicMock(return_value=False)
    lt._check_order_rate_limit = MagicMock(return_value=True)
    lt.data_manager_1h = None
    lt.data_manager_5m = None
    lt._needs_macro = False
    lt._lean_features = False
    lt.feature_names = ["F1"]
    lt._front_month_last_close = 68.0
    lt._atr_period = 999
    lt._atr_period_long = 999
    lt._atr_period_short = 999
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = 0
    lt._open_orders = {}
    lt._max_position_size = 5
    strategy = MagicMock()
    strategy.evaluate.return_value = signal or _buy_signal()
    strategy.name = "MockStrategy"
    strategy.direction = "LONG"
    lt.strategy = strategy
    lt._data_mute = False
    lt._virtual_ledger = {"5m": 0, "1h": 0}
    lt.telemetry = MagicMock()
    lt._telegram = MagicMock()
    lt.dry_run = False
    lt._front_month_local_symbol = "GCQ6"
    lt.entry_mode = "adaptive"
    lt.adaptive_priority = "Normal"
    lt._last_decision_context_by_order_id = {}
    lt._entry_order_ids = set()
    lt._processed_exit_order_ids = set()
    lt._processed_entry_order_ids = set()
    lt._active_trade_id = None
    lt._pending_entry_order_id = None
    lt._pending_entry_bar_time = None
    lt._position_entry_bar_time = None
    lt._position_bars_held = 0
    lt._trailing_activated = False
    lt._entry_price = None
    lt._atr_at_entry = None
    lt._position_side = 0
    lt._highest_high = 0.0
    lt._lowest_low = float("inf")
    lt._trade_trailing_atr_mult = None
    lt._trade_max_hold_bars = None
    lt._tp_order_ids = []
    lt._sl_order_id = None
    lt._tracked_tp_price = None
    lt._tracked_sl_price = None
    lt._trailing_atr_mult = 2.0
    lt._trailing_sl_atr_offset_long = 0.5
    lt._trailing_sl_atr_offset_short = 0.5
    lt._bar_size = "5m"
    lt.rolling_df_5m = None
    lt.rolling_df_1h = None
    _attach_identity_seams(lt)

    parent_order = SimpleNamespace(
        orderId=901, orderType="LMT", algoStrategy="Adaptive", permId=555,
        parentId=0, account="DU111", totalQuantity=1, lmtPrice=68.0,
        auxPrice=0.0, action="BUY", tif="GTC",
    )
    lt.exec_client.place_bracket_order.return_value = SimpleNamespace(
        order=parent_order,
        contract=SimpleNamespace(symbol="GC", localSymbol="GCQ6",
                                 lastTradeDateOrContractMonth="20260828"),
    )
    lt.exec_client.place_child_orders.return_value = _child_trades()
    return lt


def _run_submission(lt, frame=None):
    features = pd.DataFrame([{"F1": 1.0}])
    with patch.object(lt_module, "build_live_features", return_value=features):
        lt._on_new_bar(_BAR_T0, frame if frame is not None else _submission_frame(), "5m")
    # completion sentinel: the EXECUTE block ran to its entry registration
    assert lt._pending_entry_order_id == 901, (
        "test harness sanity: the submission path did not complete "
        "(entry order was never placed/tracked)"
    )
    assert "901" in lt._entry_order_ids


def _fill_event(order_id="901", price=69.05, action="BUY", qty=1):
    raw_order = SimpleNamespace(action=action, permId=555, parentId=0,
                                account="DU111")
    raw_trade = SimpleNamespace(
        order=raw_order,
        contract=SimpleNamespace(symbol="GC", localSymbol="GCQ6",
                                 lastTradeDateOrContractMonth="20260828"),
    )
    return StandardExecutionEvent(
        order_id=order_id, symbol="GC", status="Filled", filled_qty=qty,
        remaining_qty=0, avg_price=price, raw_event=raw_trade,
    )


# ---------------------------------------------------------------------------
# D2 — trailing / TTL / rollover / kill-switch stubs
# ---------------------------------------------------------------------------

def _trailing_stub(active_trade_id):
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt._instrument_context = _instrument_ctx()
    lt._trailing_activated = False
    lt._entry_price = 68.0
    lt._atr_at_entry = 0.5
    lt._position_side = 1
    lt._highest_high = 68.0
    lt._lowest_low = 67.5
    lt._position_bars_held = 3
    lt._trade_trailing_atr_mult = None
    lt._trailing_atr_mult = 2.0
    lt._trailing_sl_atr_offset_long = 0.5
    lt._trailing_sl_atr_offset_short = 0.5
    # Mechanical stub repair (live-trailing-ladder-phase3_07232026_0035):
    # _check_trailing_stop now resolves per-side ladders; None = the legacy
    # single-rung path this suite pins.
    lt._trailing_ladder_long = None
    lt._trailing_ladder_short = None
    idx = pd.DatetimeIndex([pd.Timestamp("2026-07-07 14:00:00")])
    # trigger bar: High 70.0 >= 68.0 + 2.0 * 0.5 = 69.0
    lt.rolling_df_5m = pd.DataFrame({
        "Open": [69.8], "High": [70.0], "Low": [69.5], "Close": [69.9],
        "Volume": [500.0],
    }, index=idx)
    lt.rolling_df_1h = None
    lt._open_orders = {}
    lt.exec_client = MagicMock()
    lt.telemetry = MagicMock()
    lt._active_trade_id = active_trade_id
    lt._sl_order_id = None
    lt._tracked_sl_price = None
    return lt


def _pending_evt(filled_qty=0):
    raw = SimpleNamespace(
        order=SimpleNamespace(orderId=901, action="BUY", totalQuantity=1,
                              parentId=0),
        orderStatus=SimpleNamespace(status="Submitted", filled=filled_qty,
                                    remaining=1 - min(filled_qty, 1),
                                    avgFillPrice=68.9 if filled_qty else 0.0),
    )
    return StandardExecutionEvent(
        order_id="901", symbol="GC", status="Submitted",
        filled_qty=filled_qty, remaining_qty=1 - min(filled_qty, 1),
        avg_price=68.9 if filled_qty else 0.0, raw_event=raw,
    )


def _ttl_stub(filled_qty=0):
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt._instrument_context = _instrument_ctx()
    lt._pending_entry_order_id = 901
    lt._pending_entry_bar_time = pd.Timestamp("2026-07-07 14:00:00")
    lt._open_orders = {"901": _pending_evt(filled_qty)}
    lt.exec_client = MagicMock()
    lt.exec_client.cancel_open_orders.return_value = 1
    strategy = MagicMock()
    lt.strategy = strategy
    lt._telegram = MagicMock()
    lt.telemetry = MagicMock()
    lt._active_trade_id = None
    # today's pre-fill contamination ON PURPOSE (see seam omissions)
    _attach_position_state(lt, side=1)
    _attach_identity_seams(lt)
    return lt


def _rollover_stub(position, pending_order_id=None):
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt._instrument_context = _instrument_ctx()
    lt._last_rollover_check_date = None
    lt._rollover_in_progress = False
    lt.data_client = MagicMock()
    lt.data_client.get_front_month_contract.return_value = ("GCZ6", "202612")
    lt._front_month_local_symbol = "GCQ6"
    lt._front_month_str = "202608"
    lt._front_month_last_close = 68.0
    lt._front_month_bars = None
    lt._subscribe_front_month = MagicMock()
    lt.data_manager_5m = None
    lt.data_manager_1h = None
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = position
    lt.exec_client.cancel_open_orders.return_value = 1
    lt._telegram = MagicMock()
    strategy = MagicMock()
    lt.strategy = strategy
    lt.telemetry = MagicMock()
    lt._pending_entry_order_id = pending_order_id
    lt._pending_entry_bar_time = (
        pd.Timestamp("2026-07-07 14:00:00") if pending_order_id else None
    )
    lt._open_orders = (
        {str(pending_order_id): _pending_evt(0)} if pending_order_id else {}
    )
    lt._active_trade_id = "trade_9" if position else None
    _attach_position_state(lt, side=1 if position else 0)
    _attach_identity_seams(lt)
    return lt


def _kill_switch_stub():
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt._instrument_context = _instrument_ctx()
    lt._active_trade_id = "trade_9"
    lt._pending_entry_order_id = None
    lt._pending_entry_bar_time = None
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = 2
    lt.exec_client.cancel_open_orders.return_value = 1
    lt.exec_client.close_position.return_value = SimpleNamespace(
        order=SimpleNamespace(orderId=77),
    )
    idx = pd.DatetimeIndex([pd.Timestamp("2026-07-07 14:00:00")])
    lt.rolling_df_5m = pd.DataFrame({
        "Open": [68.0], "High": [68.2], "Low": [67.8], "Close": [68.1],
        "Volume": [400.0],
    }, index=idx)
    lt.rolling_df_1h = None
    lt.telemetry = MagicMock()
    lt._telegram = MagicMock()
    strategy = MagicMock()
    lt.strategy = strategy
    lt._processed_exit_order_ids = set()
    _attach_position_state(lt, side=1)
    lt._sl_order_id = None  # the naked-position trigger
    # killswitch-pending-exit-guard_07202026_1805: _check_naked_position gained a
    # bounded pending-exit guard that reads these two attrs immediately after the
    # _sl_order_id guard. A GENUINE naked position has no pending TIME BARRIER
    # exit, so the guard is a no-op here (skips -> the kill switch still fires);
    # this object.__new__ stub must still declare them or the guard raises
    # AttributeError.
    lt._pending_exit_order_id = None
    lt._time_barrier_exit_attempts = 0
    # re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)
    # — mechanical fixture repair only: the widened kill-switch guard reads
    # _retiring_leg_ids, and the new cancel-confirm reads the bounded counter
    # and re-scans get_open_trades (clear book here -> the genuine-naked
    # FENCE below still flattens in a single tick).
    lt._retiring_leg_ids = []
    lt._kill_switch_cancel_confirm_attempts = 0
    lt.exec_client.get_open_trades.return_value = []
    _attach_identity_seams(lt)
    return lt


def _oca_exit_stub():
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt._instrument_context = _instrument_ctx()
    lt._open_orders = {}
    lt.telemetry = MagicMock()
    lt._telegram = MagicMock()
    lt.exec_client = MagicMock()
    lt.exec_client.cancel_open_orders.return_value = 1
    lt._processed_exit_order_ids = set()
    lt._processed_entry_order_ids = set()
    lt._entry_order_ids = set()
    lt._last_decision_context_by_order_id = {}
    lt._active_trade_id = "trade_901"
    strategy = MagicMock()
    lt.strategy = strategy
    _attach_position_state(lt, side=1)
    lt._position_bars_held = 6
    lt._tp_order_ids = [902]
    lt._sl_order_id = 903
    _attach_identity_seams(lt)
    return lt


# ---------------------------------------------------------------------------
# D1 — IBKR adapter stubs
# ---------------------------------------------------------------------------

def _ibkr_stub(open_trades=(), fills=()):
    client = object.__new__(ibkr_exec_module.IBKRExecutionClient)
    client.manager = MagicMock()
    client.manager.ib.openTrades.return_value = list(open_trades)
    client.manager.ib.fills.return_value = list(fills)
    # alternate seam if the impl refreshes via reqExecutions
    client.manager.ib.reqExecutions.return_value = list(fills)
    client._cached_contracts = {}
    client._order_callbacks = []
    client._commission_callbacks = []
    return client


def _open_trade(order_id, symbol, status="Submitted"):
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id),
        contract=SimpleNamespace(symbol=symbol, localSymbol=f"{symbol}X6"),
        orderStatus=SimpleNamespace(status=status, filled=0, remaining=1,
                                    avgFillPrice=0.0),
    )


def _ib_fill(order_id, symbol, price, side, exec_id, perm_id,
             commission_report=None, shares=1):
    execution = SimpleNamespace(
        orderId=order_id, permId=perm_id, execId=exec_id, price=price,
        shares=shares, cumQty=shares, side=side,
        time=datetime(2026, 7, 6, 22, 10, 5),
    )
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, localSymbol=f"{symbol}X6"),
        execution=execution,
        commissionReport=commission_report,
        time=execution.time,
    )


_REQUIRED_EXEC_KEYS = {"order_id", "perm_id", "exec_id", "price", "qty",
                       "side", "time", "symbol", "commission_report"}


# ===========================================================================
# A — ExecutionClient interface contract (D1)
# ===========================================================================

class TestExecutionClientInterface:

    def test_interface_declares_targeted_cancel_and_get_executions(self):
        """D1: the ABC must expose both new methods so every adapter and
        the LiveTrader recovery path share one contract."""
        assert hasattr(ExecutionClient, "cancel_orders_by_ids"), (
            "ExecutionClient.cancel_orders_by_ids(order_ids) is missing — "
            "the OOB recovery branch has no symbol-blind targeted cancel "
            "primitive (2026-07-06 MGC->GC orphaned-GTC incident)"
        )
        assert hasattr(ExecutionClient, "get_executions"), (
            "ExecutionClient.get_executions(symbol=None) is missing — "
            "recovery cannot consult broker executions for the true OOB "
            "exit price/reason"
        )
        sig = inspect.signature(ExecutionClient.get_executions)
        assert "symbol" in sig.parameters, (
            "get_executions must take a symbol parameter"
        )
        assert sig.parameters["symbol"].default is None, (
            "get_executions(symbol=...) must default to None (all fills)"
        )


# ===========================================================================
# B — IBKR adapter implementations (D1)
# ===========================================================================

class TestIBKRTargetedCancel:

    def test_cancels_exact_ids_across_contract_symbols(self):
        """The 2026-07-06 incident pin: order 102 rests on the OLD contract
        symbol (MGC) after an instance reconfiguration — the targeted
        primitive must cancel it anyway. Symbol-scoped filtering here is
        exactly the silent-miss bug."""
        trades = [
            _open_trade(101, "GC"),
            _open_trade(102, "MGC"),   # old-contract orphan
            _open_trade(999, "GC"),    # NOT requested — must survive
        ]
        client = _ibkr_stub(open_trades=trades)

        cancelled = client.cancel_orders_by_ids([101, 102])

        cancel_calls = client.manager.ib.cancelOrder.call_args_list
        cancelled_ids = {c.args[0].orderId for c in cancel_calls}
        assert cancelled_ids == {101, 102}, (
            f"cancel_orders_by_ids must cancel exactly the requested ids "
            f"irrespective of contract symbol; cancelled {cancelled_ids}"
        )
        assert cancelled == 2, "must return the number of orders cancelled"

    def test_unknown_ids_cancel_nothing_and_return_zero(self):
        client = _ibkr_stub(open_trades=[_open_trade(101, "GC")])
        cancelled = client.cancel_orders_by_ids([424242])
        assert cancelled == 0
        assert not client.manager.ib.cancelOrder.called, (
            "an id with no open trade must not trigger any cancel"
        )


class TestIBKRGetExecutions:

    def test_returns_flat_fill_records_with_contract_keys(self):
        cr = _commission_report()
        fills = [_ib_fill(201, "GC", 67.90, "BOT", _EXEC_ID_TP, 9101,
                          commission_report=cr)]
        client = _ibkr_stub(fills=fills)

        recs = client.get_executions()

        assert len(recs) == 1
        rec = recs[0]
        missing = _REQUIRED_EXEC_KEYS - set(rec.keys())
        assert not missing, (
            f"get_executions record missing contract keys: {missing} "
            f"(RECORD CONTRACT in the module header)"
        )
        assert str(rec["order_id"]) == "201"
        assert rec["perm_id"] == 9101
        assert rec["exec_id"] == _EXEC_ID_TP
        assert rec["price"] == pytest.approx(67.90)
        assert float(rec["qty"]) == pytest.approx(1.0)
        assert rec["side"] == "BOT"
        assert rec["symbol"] == "GC"
        got_cr = rec["commission_report"]
        assert got_cr is not None, "commissionReport present must be carried"
        cr_commission = (got_cr.get("commission") if isinstance(got_cr, dict)
                         else getattr(got_cr, "commission", None))
        assert cr_commission == pytest.approx(2.25)

    def test_symbol_filter_and_default_all(self):
        fills = [
            _ib_fill(201, "GC", 67.90, "BOT", _EXEC_ID_TP, 9101),
            _ib_fill(88, "MGC", 67.95, "SLD", "0000ffff.0001.01", 9102),
        ]
        client = _ibkr_stub(fills=fills)

        all_recs = client.get_executions()
        assert len(all_recs) == 2, "symbol=None must return every fill"

        gc_recs = client.get_executions(symbol="GC")
        assert len(gc_recs) == 1
        assert gc_recs[0]["symbol"] == "GC"


# ===========================================================================
# C — SimulatedExecution (A4)
# ===========================================================================

class TestSimulatedExecutionA4:

    def test_sim_implements_both_targeted_primitives(self):
        sim = SimulatedExecution()
        assert hasattr(sim, "cancel_orders_by_ids"), (
            "A4: SimulatedExecution must implement cancel_orders_by_ids"
        )
        assert hasattr(sim, "get_executions"), (
            "A4: SimulatedExecution must implement get_executions"
        )

    def test_sim_never_fabricates_success(self):
        """A4: honest for the sim domain OR loud NotImplementedError.
        A nonexistent order id must never report a successful cancel; an
        empty fill history must never invent executions."""
        sim = SimulatedExecution()
        try:
            result = sim.cancel_orders_by_ids([424242])
        except NotImplementedError:
            pass  # loud refusal is allowed
        else:
            assert result == 0, (
                f"no order 424242 exists in the sim — a cancel count of "
                f"{result!r} is fabricated success"
            )
        try:
            fills = sim.get_executions()
        except NotImplementedError:
            pass
        else:
            assert fills == [], (
                f"a fresh sim has no executions — got {fills!r}"
            )


# ===========================================================================
# D — OOB recovery branch (D1 + A5)
# ===========================================================================

class TestOOBRecoveryBranch:

    def test_targeted_cancel_of_ledger_bracket_ids_then_bulk_sweep(self):
        """D1.3(a): the ledger row's exact tp/sl order ids are cancelled via
        the symbol-blind primitive FIRST; the existing symbol-scoped sweep
        still runs afterwards as belt-and-braces (A8: bulk primitive itself
        untouched)."""
        executions = [_exec_record("201", 67.90, side="BOT",
                                   exec_id=_EXEC_ID_TP,
                                   commission_report=_commission_report())]
        lt = _recovery_stub(executions)

        lt._recover_inherited_position()

        assert lt.exec_client.cancel_orders_by_ids.called, (
            "OOB recovery never used the targeted cancel primitive — the "
            "symbol-scoped sweep silently missed the old-contract GTC TP "
            "on 2026-07-06 (naked-short trap)"
        )
        requested = _cancel_by_ids_requested(lt)
        assert 202 in requested, (
            f"the resting SL leg (ledger sl_order_id=202) must be in the "
            f"targeted cancel; requested={requested}"
        )
        assert requested <= {201, 202}, (
            f"targeted cancel must only touch the ledger row's own bracket "
            f"ids; requested={requested}"
        )
        # belt-and-braces bulk sweep preserved
        assert lt.exec_client.cancel_open_orders.called
        bulk = lt.exec_client.cancel_open_orders.call_args
        bulk_symbol = bulk.kwargs.get("symbol",
                                      bulk.args[0] if bulk.args else None)
        assert bulk_symbol == "GC"

    def test_tp_leg_execution_recovers_truthful_reason_and_exit_price(self):
        """D1.3(b): a broker execution matching the TP order id -> the close
        is TP_HIT_OOB WITH the real exit price, not generic CLOSED_OOB with
        a permanently NULL price."""
        executions = [_exec_record("201", 67.90, side="BOT",
                                   exec_id=_EXEC_ID_TP,
                                   commission_report=_commission_report())]
        lt = _recovery_stub(executions)

        lt._recover_inherited_position()

        trade_id, kwargs = _close_call(lt)
        assert trade_id == "trade_777"
        assert kwargs.get("reason") == "TP_HIT_OOB", (
            f"expected truthful reason TP_HIT_OOB, got "
            f"{kwargs.get('reason')!r}"
        )
        assert kwargs.get("exit_price") == pytest.approx(67.90), (
            f"exit_price must be recovered from the matched execution, got "
            f"{kwargs.get('exit_price')!r}"
        )

    def test_sl_leg_execution_recovers_truthful_reason_and_exit_price(self):
        executions = [_exec_record("202", 69.90, side="BOT",
                                   exec_id=_EXEC_ID_SL,
                                   commission_report=_commission_report(
                                       realized=-102.0))]
        lt = _recovery_stub(executions)

        lt._recover_inherited_position()

        trade_id, kwargs = _close_call(lt)
        assert trade_id == "trade_777"
        assert kwargs.get("reason") == "SL_HIT_OOB"
        assert kwargs.get("exit_price") == pytest.approx(69.90)

    def test_recovered_fill_rows_use_deterministic_event_ids(self):
        """A5: recovery-written EXECUTION_FILL + COMMISSION tradebook rows
        must dedupe across restarts — event_ids keyed on execId /
        order-id+permId, NOT wall-clock time. Two recovery runs at different
        wall clocks must produce byte-identical event_ids."""
        def _run(now_iso):
            executions = [_exec_record(
                "201", 67.90, side="BOT", exec_id=_EXEC_ID_TP,
                commission_report=_commission_report())]
            lt = _recovery_stub(executions)
            lt._utc_iso_now = lambda: now_iso  # shadow the clock
            lt._recover_inherited_position()
            fill_calls = _tradebook_calls(lt, "EXECUTION_FILL")
            assert fill_calls, (
                "OOB recovery must write an EXECUTION_FILL tradebook row "
                "for the recovered fill"
            )
            fill_kwargs = fill_calls[0].kwargs
            assert str(fill_kwargs.get("order_id")) == "201"
            commission_calls = _tradebook_calls(lt, "COMMISSION")
            assert commission_calls, (
                "OOB recovery must write a COMMISSION tradebook row when "
                "the execution carries a commissionReport"
            )
            assert commission_calls[0].kwargs.get("event_id") == (
                f"COMMISSION_{_EXEC_ID_TP}"
            ), "COMMISSION rows must reuse the COMMISSION_<execId> pattern"
            return fill_kwargs.get("event_id")

        id_first = _run("2026-07-07T01:00:00")
        id_second = _run("2026-07-07T09:30:00")
        assert id_first == id_second, (
            f"EXECUTION_FILL event_id changed with the wall clock "
            f"({id_first!r} != {id_second!r}) — timestamp-keyed "
            f"_build_event_id defeats INSERT OR IGNORE idempotency (A5)"
        )

    def test_no_executions_closes_unrecovered_with_null_exit_price(self):
        """D1.3(c): day-boundary case — no fabricated prices. Reason must be
        the explicit CLOSED_OOB_UNRECOVERED with exit_price left NULL."""
        lt = _recovery_stub(executions=[], cancel_by_ids=2)

        lt._recover_inherited_position()

        trade_id, kwargs = _close_call(lt)
        assert trade_id == "trade_777"
        assert kwargs.get("reason") == "CLOSED_OOB_UNRECOVERED", (
            f"expected explicit CLOSED_OOB_UNRECOVERED, got "
            f"{kwargs.get('reason')!r}"
        )
        assert kwargs.get("exit_price") is None, (
            f"no execution evidence -> exit_price must stay NULL, got "
            f"{kwargs.get('exit_price')!r}"
        )

    def test_unaccounted_protective_order_logs_error_and_telegrams(self, caplog):
        """D1.3(a): a protective order neither found open (targeted cancel
        matched nothing) nor provably done (no execution) is a live orphan
        hazard — ERROR + Telegram, NEVER the current log.debug swallow."""
        lt = _recovery_stub(executions=[], cancel_by_ids=0)
        lt.exec_client.cancel_open_orders.return_value = 0

        with caplog.at_level(logging.DEBUG):
            lt._recover_inherited_position()

        error_records = [r for r in caplog.records
                         if r.levelno >= logging.ERROR]
        assert error_records, (
            "an unaccounted protective order must be logged at ERROR "
            "(the 2026-07-06 orphaned GTC TP was silent by construction: "
            "success logged only if cancelled > 0, except -> log.debug)"
        )
        assert lt._telegram.send.called, (
            "an unaccounted protective order must attempt a Telegram alert"
        )


# ===========================================================================
# E — startup sweep FENCE (A8)
# ===========================================================================

class TestStartupSweepFence:

    def test_startup_sweep_still_uses_bulk_symbol_cancel(self):
        """FENCE (passes today): _cancel_orphaned_orders_on_startup keeps
        the bulk symbol-scoped cancel for the flat-and-untracked sweep (A8:
        do NOT modify cancel_open_orders call sites)."""
        lt = object.__new__(lt_module.LiveTrader)
        lt._execution_symbol = "GC"
        lt._active_trade_id = None
        lt._pending_entry_order_id = None
        lt.exec_client = MagicMock()
        lt.exec_client.get_position.return_value = 0
        lt.exec_client.get_position_settled.return_value = 0  # settled CONFIRMS flat
        lt.exec_client.cancel_open_orders.return_value = 1
        evt = SimpleNamespace(symbol="GC", order_id="55", status="Submitted")
        lt._open_orders = {"55": evt}

        lt._cancel_orphaned_orders_on_startup()

        lt.exec_client.cancel_open_orders.assert_called_once_with(symbol="GC")


# ===========================================================================
# F — D2: pending-entry vs in-position state split
# ===========================================================================

class TestSubmissionStoresPendingOnly:

    def test_submission_does_not_set_in_position_state(self):
        """D2.1: order PLACEMENT must store a pending-entry record only.
        _entry_price/_atr_at_entry/_position_side/extremes/
        _position_entry_bar_time belong to the FILL, not the submission —
        pre-fill state is what re-fires the trailing warning every bar
        (NG order 19, 2026-07-06) and poisons trailing math."""
        lt = _entry_flow_stub()
        _run_submission(lt)

        # pending record present (unchanged behavior)
        assert lt._pending_entry_order_id == 901
        assert lt._pending_entry_bar_time == _BAR_T0

        # in-position state must NOT exist yet
        assert lt._entry_price is None, (
            f"submission set _entry_price={lt._entry_price!r} — trailing "
            f"math would run off the submission price on every trade"
        )
        assert lt._atr_at_entry is None, (
            "submission must not set _atr_at_entry (carry it in the "
            "pending decision context instead)"
        )
        assert lt._position_side == 0, (
            f"submission set _position_side={lt._position_side} before any "
            f"fill — this is the pre-fill state machine bug"
        )
        assert lt._highest_high == 0.0 and lt._lowest_low == float("inf"), (
            "extremes must not accumulate pre-fill"
        )
        assert lt._position_entry_bar_time is None, (
            "submission must not set _position_entry_bar_time — TTL'd "
            "never-filled entries otherwise look like held positions"
        )


class TestFillSeedsInPositionState:

    def test_fill_seeds_all_state_from_the_fill(self):
        """D2.2 + A3: the confirmed-fill branch seeds ALL in-position state
        from the FILL: _entry_price := avg_price, extremes := fill-time bar
        H/L (re-seeded, not accumulated from submission),
        _position_entry_bar_time := fill bar, side from the fill action,
        ATR + overrides from the stored pending context; and
        _pending_entry_order_id is cleared eagerly."""
        lt = _entry_flow_stub()
        _run_submission(lt)

        fill_frame = _fill_frame()  # last bar is an INSIDE bar
        lt.rolling_df_5m = fill_frame
        lt._on_standard_execution_event(_fill_event(price=69.05))

        assert lt._pending_entry_order_id is None, (
            "the entry fill must clear _pending_entry_order_id eagerly "
            "(D2.2) — a stale pending id re-arms TTL against a live trade"
        )
        assert lt._active_trade_id == "trade_901"
        assert lt._entry_price == pytest.approx(69.05), (
            f"A3: _entry_price must be re-seeded from the fill avg_price "
            f"(69.05), got {lt._entry_price!r}"
        )
        assert lt._position_side == 1
        assert lt._atr_at_entry == pytest.approx(0.42), (
            "ATR at signal must flow from the stored pending context to "
            "fill-time state"
        )
        assert lt._trade_trailing_atr_mult == pytest.approx(1.7), (
            "per-trade trailing override must flow from the stored pending "
            "context to fill-time state"
        )
        assert lt._highest_high == pytest.approx(68.20), (
            f"extremes must be RE-SEEDED from the fill-time bar (High "
            f"68.20), not accumulated from submission (68.35); got "
            f"{lt._highest_high!r}"
        )
        assert lt._lowest_low == pytest.approx(67.95), (
            f"extremes must be RE-SEEDED from the fill-time bar (Low "
            f"67.95), not accumulated from submission (67.85); got "
            f"{lt._lowest_low!r}"
        )
        assert lt._position_entry_bar_time == fill_frame.index[-1], (
            f"_position_entry_bar_time must be the fill-time bar "
            f"({fill_frame.index[-1]}), not the submission bar; got "
            f"{lt._position_entry_bar_time!r}"
        )

    def test_a3_entry_price_seeding_survives_child_placement_failure(self):
        """A3 red anchor: today _entry_price := fill only happens INSIDE
        _place_bracket_children_on_fill AFTER place_child_orders succeeds.
        The position exists broker-side the moment the entry fills — state
        seeding must not be contingent on bracket-children success (the
        kill-switch needs truthful state to protect a naked position)."""
        lt = _entry_flow_stub()
        _run_submission(lt)
        lt.rolling_df_5m = _fill_frame()
        lt.exec_client.place_child_orders.side_effect = RuntimeError(
            "bracket children rejected"
        )

        lt._on_standard_execution_event(_fill_event(price=69.05))

        assert lt._entry_price == pytest.approx(69.05), (
            f"A3: fill-time _entry_price seeding must survive child-order "
            f"failure; got {lt._entry_price!r} (submission price 68.0 "
            f"means the re-seed is still gated on bracket success)"
        )
        assert lt._position_side == 1
        assert lt._active_trade_id == "trade_901"

    def test_unresolvable_pending_context_errors_and_telegrams(self, caplog):
        """D2.2: a recognized entry fill whose stored context cannot be
        resolved is a raise-loudly condition — ERROR + Telegram, no
        defaults (today: a WARNING and a silently naked position)."""
        lt = _entry_flow_stub()
        lt._entry_order_ids.add("901")     # recognized entry...
        lt._last_decision_context_by_order_id = {}  # ...but no context
        lt.rolling_df_5m = _fill_frame()

        with caplog.at_level(logging.DEBUG):
            lt._on_standard_execution_event(_fill_event(price=69.05))

        error_records = [r for r in caplog.records
                         if r.levelno >= logging.ERROR]
        assert error_records, (
            "an unresolvable pending context for a recognized entry fill "
            "must log at ERROR (no-silent-defaults rule)"
        )
        assert lt._telegram.send.called, (
            "an unresolvable pending context must attempt a Telegram alert"
        )


class TestTrailingHardGate:

    def test_no_confirmed_trade_is_a_structural_noop(self, caplog):
        """D2.3: _check_trailing_stop must hard-gate on
        _active_trade_id is not None (fill-time-only today). With stale
        pre-fill junk present and NO confirmed trade, the method must do
        NOTHING: no extremes mutation, no '_sl_order_id is None' warning
        (the NG order-19 repeater), no order modification — on every call."""
        lt = _trailing_stub(active_trade_id=None)

        with caplog.at_level(logging.DEBUG):
            lt._check_trailing_stop()
            lt._check_trailing_stop()  # the repeater: bar after bar

        trailing_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "TRAILING STOP" in r.getMessage()
        ]
        assert not trailing_warnings, (
            f"pre-fill trailing evaluation still fires: {trailing_warnings[0].getMessage()!r} "
            f"— the warning must become structurally impossible, not "
            f"suppressed"
        )
        assert lt._highest_high == pytest.approx(68.0), (
            f"extremes mutated without a confirmed trade "
            f"(_highest_high={lt._highest_high!r}) — the hard gate must "
            f"precede the extremes update"
        )
        assert not lt.exec_client.modify_order.called
        assert lt._trailing_activated is False

    def test_confirmed_position_trailing_still_fires(self):
        """FENCE (passes today): with a confirmed trade and a tracked SL
        order, the trigger still modifies the SL and latches — the hard
        gate must not lobotomize live trailing."""
        lt = _trailing_stub(active_trade_id="trade_9")
        lt._sl_order_id = 903
        raw_order = SimpleNamespace(auxPrice=67.5)
        evt = SimpleNamespace(symbol="GC", order_id=903,
                              raw_event=SimpleNamespace(order=raw_order))
        lt._open_orders = {903: evt}

        lt._check_trailing_stop()

        lt.exec_client.modify_order.assert_called_once()
        assert lt._trailing_activated is True
        assert lt._tracked_sl_price == pytest.approx(68.25)  # 68 + 0.5*0.5
        assert raw_order.auxPrice == pytest.approx(68.25)
        assert lt.telemetry.update_position_sl.called


class TestClearPendingEntry:

    def test_clear_pending_entry_clears_only_pending_state(self):
        """D2.4: NEW zero-arg _clear_pending_entry() — clears ONLY the
        pending record; never fires strategy.on_exit; leaves in-position
        state alone (rollover/kill-switch real closes still need it)."""
        assert hasattr(lt_module.LiveTrader, "_clear_pending_entry"), (
            "LiveTrader._clear_pending_entry() does not exist — TTL/"
            "rollover/kill-switch have no cooldown-free pending-clear path"
        )
        lt = _ttl_stub(filled_qty=0)
        lt._active_trade_id = "trade_9"   # unrelated live trade state
        lt._entry_price = 68.0

        lt._clear_pending_entry()

        assert lt._pending_entry_order_id is None
        assert lt._pending_entry_bar_time is None
        assert not lt.strategy.on_exit.called, (
            "_clear_pending_entry must NEVER fire strategy.on_exit — no "
            "fill ever occurred"
        )
        assert lt._active_trade_id == "trade_9", (
            "_clear_pending_entry must clear ONLY pending state"
        )
        assert lt._entry_price == pytest.approx(68.0), (
            "_clear_pending_entry must not touch in-position fields"
        )


# ===========================================================================
# G — TTL / rollover / kill-switch (D2.4, A1, A2, A8)
# ===========================================================================

class TestEntryCancellationPaths:

    def test_ttl_cancel_of_never_filled_entry_skips_cooldown(self):
        """D2.4: TTL-cancelling an entry that NEVER filled must not fire
        strategy.on_exit (today: _reset_position_state() default reason
        'CLOSED' applies an SL-flavored cooldown for a trade that never
        existed). The bulk symbol-scoped cancel stays (A8)."""
        lt = _ttl_stub(filled_qty=0)

        lt._check_entry_order_ttl(pd.Timestamp("2026-07-07 15:00:00"))

        assert not lt.strategy.on_exit.called, (
            "TTL cancel of a never-filled entry fired strategy.on_exit — "
            "a phantom SL cooldown now suppresses real signals"
        )
        assert lt._pending_entry_order_id is None
        assert lt._pending_entry_bar_time is None
        # A8 FENCE: entry cancellation still goes through the bulk primitive
        lt.exec_client.cancel_open_orders.assert_called_once_with(symbol="GC")

    def test_ttl_partial_fill_is_not_never_filled(self, caplog):
        """A1: orderStatus.filled > 0 on the pending order means contracts
        EXIST broker-side — silently cancelling and resetting hides a live
        position. Must log ERROR + Telegram and route to in-position
        handling, not the never-filled clear."""
        lt = _ttl_stub(filled_qty=1)

        with caplog.at_level(logging.DEBUG):
            lt._check_entry_order_ttl(pd.Timestamp("2026-07-07 15:00:00"))

        error_records = [r for r in caplog.records
                         if r.levelno >= logging.ERROR]
        assert error_records, (
            "a partially-filled pending entry hit the TTL path without an "
            "ERROR — the position exists broker-side (A1)"
        )
        assert lt._telegram.send.called, (
            "a partial fill on the TTL path must attempt a Telegram alert"
        )

    def test_rollover_real_close_still_fires_cooldown(self):
        """FENCE (passes today) — A2: rollover force-close of a FILLED
        position is a REAL close: full in-position reset including
        strategy.on_exit(reason='ROLLOVER'), and the bulk cancel sweep
        (A8) is preserved."""
        lt = _rollover_stub(position=1)

        lt._check_contract_rollover()

        assert lt.strategy.on_exit.called, (
            "rollover force-close must still run the full in-position "
            "reset (strategy.on_exit + cooldown) — A2"
        )
        reason = lt.strategy.on_exit.call_args.args[1]
        assert reason == "ROLLOVER"
        assert lt.exec_client.cancel_open_orders.called  # A8
        assert lt.exec_client.close_position.called

    def test_rollover_flat_with_pending_clears_pending_without_cooldown(self):
        """D2.4/A2: rollover is an entry-cancellation path — a pending
        never-filled entry resting on the EXPIRING contract must be
        cleared (it is the same old-contract-orphan class as D1), and no
        cooldown may fire because no fill ever existed. Today the flat
        branch leaves the pending record (and its resting order) behind."""
        lt = _rollover_stub(position=0, pending_order_id=901)

        lt._check_contract_rollover()

        assert lt._pending_entry_order_id is None, (
            "rollover left _pending_entry_order_id=901 tracked against an "
            "order resting on the EXPIRED contract — TTL's symbol-scoped "
            "cancel will silently miss it after the symbol updates"
        )
        assert lt._pending_entry_bar_time is None
        assert not lt.strategy.on_exit.called, (
            "no fill ever occurred — the rollover pending-clear must not "
            "fire cooldowns (A2 scoping)"
        )

    def test_kill_switch_real_close_still_fires_cooldown_and_bulk_cancel(self):
        """FENCE (passes today) — A2 + A8: the naked-position kill switch
        is a REAL-close path: full reset incl. strategy.on_exit
        (reason='NAKED_POSITION_KILL_SWITCH'), bulk symbol cancel, market
        flatten."""
        lt = _kill_switch_stub()

        lt._check_naked_position()

        assert lt.strategy.on_exit.called
        reason = lt.strategy.on_exit.call_args.args[1]
        assert reason == "NAKED_POSITION_KILL_SWITCH"
        # re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)
        # _check_naked_position now transmits its own bulk cancel and re-scans
        # the book (cancel-confirm) BEFORE the flatten helper's idempotent
        # cancel — the A8 fence keeps every call bulk + symbol-scoped without
        # pinning the (now 2-call) count; the Stage-4 suite
        # (tests/test_oca_exit_ordering.py) pins the cancel-confirm ordering.
        assert lt.exec_client.cancel_open_orders.called
        for _c in lt.exec_client.cancel_open_orders.call_args_list:
            _sym = _c.kwargs.get("symbol", _c.args[0] if _c.args else None)
            assert _sym == "GC", (
                f"kill-switch cancels must stay bulk symbol-scoped (A8); "
                f"got {_c!r}"
            )
        close_kwargs = lt.exec_client.close_position.call_args.kwargs
        assert close_kwargs.get("exit_mode") == "market"
        assert lt._pending_entry_order_id is None


# ===========================================================================
# H — software-OCA exit path FENCE (A8)
# ===========================================================================

class TestSoftwareOCAExitFence:

    def test_sl_fill_exit_path_bulk_cancel_and_reasons_unchanged(self):
        """FENCE (passes today) — A8: the live software-OCA exit path keeps
        the bulk symbol-scoped cancel_open_orders (exactly one bracket is
        live at exit time), reason vocabulary SL_HIT flows to both the
        ledger close and strategy.on_exit."""
        lt = _oca_exit_stub()
        raw_order = SimpleNamespace(action="SELL", permId=9030, parentId=0,
                                    account="DU111")
        evt = StandardExecutionEvent(
            order_id="903", symbol="GC", status="Filled", filled_qty=1,
            remaining_qty=0, avg_price=67.50,
            raw_event=SimpleNamespace(
                order=raw_order,
                contract=SimpleNamespace(symbol="GC", localSymbol="GCQ6"),
            ),
        )

        lt._on_standard_execution_event(evt)

        lt.exec_client.cancel_open_orders.assert_called_once_with(symbol="GC")
        close_kwargs = lt.telemetry.close_position.call_args.kwargs
        assert close_kwargs.get("reason") == "SL_HIT"
        assert close_kwargs.get("exit_price") == pytest.approx(67.50)
        assert lt.strategy.on_exit.called
        assert lt.strategy.on_exit.call_args.args[1] == "SL_HIT"


# ===========================================================================
# I — A7: cooldown vocabulary
# re-adjudicated: cooldown-single-authority-wiring_07222026_1051 — the gate
# is flavor-blind per-side cooldown_bars (every exit reason arms it, matching
# the backtest's TieredEnsemble re-gate), so the SL-flavored vocabulary
# tuples are GONE. A7's concern (an OOB-recovered SL exit falling through to
# the no-cooldown TP flavor) is structurally impossible now; the inverted pin
# below keeps a reason-conditional cooldown from regressing in silently.
# ===========================================================================

def _sl_cooldown_tuple_sites():
    """All parenthesized string-tuple literals in ConfigurableStrategy's
    source that contain the exact element "SL_HIT" — the old flavored
    cooldown tuple sites (must now be absent)."""
    source = inspect.getsource(cs_module)
    candidates = re.findall(r"\(([^()]*?)\)", source, flags=re.DOTALL)
    return [c for c in candidates if '"SL_HIT"' in c]


class TestCooldownVocabularyA7:

    def test_flavored_cooldown_vocabulary_removed(self):
        """The gate must be flavor-blind: no reason-vocabulary tuple may
        condition the cooldown on the exit flavor. All exit reasons arm the
        per-side cooldown_bars equally (see
        tests/test_exit_reason_and_fill_routing.py for the behavioral pin)."""
        sites = _sl_cooldown_tuple_sites()
        assert sites == [], (
            f"flavored cooldown vocabulary tuple(s) reappeared in "
            f"configurable_strategy.py: {sites!r} — the cooldown gate is "
            f"per-side cooldown_bars, any exit reason"
        )

    def test_reconciler_exit_reason_map_covers_oob_vocabulary(self):
        """A7: scripts/trade_reconciler.py _EXIT_REASON_MAP must map all
        three new reasons; the SL/TP OOB flavors normalize to the same
        canonical names as their in-band counterparts."""
        assert "SL_HIT_OOB" in _EXIT_REASON_MAP, (
            "_EXIT_REASON_MAP lacks SL_HIT_OOB"
        )
        assert _EXIT_REASON_MAP["SL_HIT_OOB"] == _EXIT_REASON_MAP["SL_HIT"], (
            "SL_HIT_OOB must normalize like SL_HIT (it IS an SL exit, "
            "recovered out-of-band)"
        )
        assert "TP_HIT_OOB" in _EXIT_REASON_MAP, (
            "_EXIT_REASON_MAP lacks TP_HIT_OOB"
        )
        assert _EXIT_REASON_MAP["TP_HIT_OOB"] == _EXIT_REASON_MAP["TP_HIT"], (
            "TP_HIT_OOB must normalize like TP_HIT"
        )
        assert _EXIT_REASON_MAP.get("CLOSED_OOB_UNRECOVERED"), (
            "_EXIT_REASON_MAP lacks CLOSED_OOB_UNRECOVERED"
        )


# ===========================================================================
# J — D3: fleet_health discriminators (unit)
# ===========================================================================

_NOW = datetime(2026, 7, 7, 4, 0, 0)


def _aged_null_fill_row(order_id=8):
    return {"symbol": "GC", "client_id": 4000, "order_id": order_id,
            "action_taken": "EXECUTE", "fill_price": None,
            "timestamp": "2026-07-07T03:00:00",
            "created_at": "2026-07-07T03:00:06"}  # 60 min before _NOW


def _closed_row(close_time, trade_id="trade_8"):
    return {"symbol": "GC", "client_id": 4000, "trade_id": trade_id,
            "status": "CLOSED", "tp_order_id": 9, "sl_order_id": 10,
            "entry_price": 4150.1, "exit_price": None,
            "close_reason": "SL_HIT", "close_time": close_time}


class TestCheckPositionsFillEvidence:

    def test_legacy_three_positional_call_stays_conservative(self):
        """FENCE (passes today) — A6 default: the existing 3-positional-arg
        call shape (pinned by the Strict-Locked tests/test_fleet_health.py)
        must keep over-flagging aged NULL-fill EXECUTE rows when no
        evidence is supplied."""
        findings = fh.check_positions([], [_aged_null_fill_row()], _NOW)
        assert [f["kind"] for f in findings] == ["missing-fill-price"]

    def test_fill_evidence_none_means_legacy_conservative(self):
        """A6: fill_evidence=None (the default) = evidence unavailable ->
        legacy time-based flagging. Red today: the keyword does not exist."""
        findings = fh.check_positions(
            [], [_aged_null_fill_row()], _NOW, fill_evidence=None)
        assert [f["kind"] for f in findings] == ["missing-fill-price"], (
            "fill_evidence=None must reproduce the legacy conservative "
            "over-flagging exactly (never silently skip)"
        )

    def test_evidence_without_match_not_flagged(self):
        """D3.2: with evidence present and NO fill proven for the row's
        order_id, the NULL fill_price is the LEGITIMATE permanent state of
        a never-filled/TTL-cancelled entry — no finding."""
        findings = fh.check_positions(
            [], [_aged_null_fill_row(order_id=55)], _NOW,
            fill_evidence={8, 77})
        assert findings == [], (
            "a never-filled EXECUTE row (no matching fill evidence) must "
            "not be flagged — this was the D3 false-positive nag"
        )

    @pytest.mark.parametrize("evidence", [{8}, {"8"}],
                             ids=["int-evidence", "str-evidence"])
    def test_evidence_match_flags_update_fill_regression(self, evidence):
        """D3.2: a proven fill (EXECUTION_FILL row or entry_order_id) with
        a still-NULL ledger fill_price is a TRUE update_fill regression —
        must flag. Matching must be str/int-robust (contract #3)."""
        findings = fh.check_positions(
            [], [_aged_null_fill_row(order_id=8)], _NOW,
            fill_evidence=evidence)
        assert [f["kind"] for f in findings] == ["missing-fill-price"], (
            f"order 8 provably filled (evidence={evidence!r}) but "
            f"fill_price is NULL — the regression detector must fire"
        )

    def test_incomplete_close_scoped_to_48h(self):
        """D3.3: incomplete-close must be scoped to closes within 48h —
        adjudicated old rows currently nag hourly forever."""
        old = _closed_row((_NOW - timedelta(days=10)).isoformat(),
                          trade_id="trade_old")
        recent = _closed_row((_NOW - timedelta(hours=1)).isoformat(),
                             trade_id="trade_recent")
        findings = fh.check_positions([old, recent], [], _NOW)
        kinds_who = [(f["kind"], f["detail"]) for f in findings]
        assert len(findings) == 1, (
            f"only the recent (<48h) incomplete close may flag; got "
            f"{kinds_who}"
        )
        assert findings[0]["kind"] == "incomplete-close"
        assert "trade_recent" in findings[0]["detail"]

    def test_null_or_missing_close_time_still_flags(self):
        """FENCE-pin for the fix (passes today): a CLOSED row with NULL —
        or entirely absent — close_time still flags ('surface it'
        precedent; the Strict-Locked test rows carry no close_time key)."""
        null_ct = _closed_row(None, trade_id="trade_null_ct")
        no_key = _closed_row(None, trade_id="trade_no_ct")
        del no_key["close_time"]
        findings = fh.check_positions([null_ct, no_key], [], _NOW)
        assert [f["kind"] for f in findings] == (
            ["incomplete-close", "incomplete-close"]
        )


# ===========================================================================
# K — D3: fleet_health main() integration (tmp_path DB only)
# ===========================================================================

_LOG_CLEAN = (
    "2026-07-07 03:00:05 [INFO] [GC cid=4000] LiveTrader: [GC] INFERENCE ok\n"
)


def _evidence_db(path, *, ledger_rows=(), active_rows=(), tradebook_rows=()):
    """Telemetry DB with the FULL evidence schema: active_positions carries
    entry_order_id + close_time (as the real TelemetryDB does) and
    tradebook_events exists."""
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE active_positions (
            id INTEGER PRIMARY KEY, symbol TEXT, client_id INTEGER,
            trade_id TEXT, status TEXT, side TEXT, quantity INTEGER,
            entry_price REAL, exit_price REAL, close_reason TEXT,
            tp_order_id INTEGER, sl_order_id INTEGER,
            entry_order_id INTEGER,
            entry_time TEXT, close_time TEXT);
        CREATE TABLE trade_ledger (
            id INTEGER PRIMARY KEY, symbol TEXT, client_id INTEGER,
            timestamp TEXT, action_taken TEXT, order_id INTEGER,
            fill_price REAL, created_at TEXT);
        CREATE TABLE market_bars (
            id INTEGER PRIMARY KEY, symbol TEXT, client_id INTEGER,
            timestamp TEXT);
        CREATE TABLE tradebook_events (
            id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, event_type TEXT,
            event_timestamp_utc TEXT, order_id INTEGER, symbol TEXT,
            client_id INTEGER);
    """)
    now = datetime.utcnow()
    fresh = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        "INSERT INTO market_bars (symbol, client_id, timestamp)"
        " VALUES ('GC', 4000, ?)", (fresh,))
    for r in ledger_rows:
        con.execute(
            "INSERT INTO trade_ledger (symbol, client_id, timestamp,"
            " action_taken, order_id, fill_price, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", r)
    for r in active_rows:
        con.execute(
            "INSERT INTO active_positions (symbol, client_id, trade_id,"
            " status, side, quantity, entry_price, exit_price, close_reason,"
            " tp_order_id, sl_order_id, entry_order_id, entry_time,"
            " close_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            r)
    for r in tradebook_rows:
        con.execute(
            "INSERT INTO tradebook_events (event_id, event_type,"
            " event_timestamp_utc, order_id, symbol, client_id)"
            " VALUES (?, ?, ?, ?, ?, ?)", r)
    con.commit()
    con.close()


def _legacy_db(path):
    """Locked-file-style schema: NO tradebook_events table, NO
    entry_order_id column — mirrors tests/test_fleet_health.py::_make_db
    with naked=True, null_fill=True."""
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE active_positions (
            id INTEGER PRIMARY KEY, symbol TEXT, client_id INTEGER,
            trade_id TEXT, status TEXT, side TEXT, quantity INTEGER,
            entry_price REAL, exit_price REAL, close_reason TEXT,
            tp_order_id INTEGER, sl_order_id INTEGER,
            entry_time TEXT, close_time TEXT);
        CREATE TABLE trade_ledger (
            id INTEGER PRIMARY KEY, symbol TEXT, client_id INTEGER,
            timestamp TEXT, action_taken TEXT, order_id INTEGER,
            fill_price REAL, created_at TEXT);
        CREATE TABLE market_bars (
            id INTEGER PRIMARY KEY, symbol TEXT, client_id INTEGER,
            timestamp TEXT);
    """)
    now = datetime.utcnow()
    old = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        "INSERT INTO active_positions (symbol, client_id, trade_id, status,"
        " side, quantity, entry_price, tp_order_id, sl_order_id, entry_time)"
        " VALUES ('GC', 4000, 'trade_8', 'OPEN', 'LONG', 1, 4150.1, NULL,"
        " 10, ?)", (old,))
    con.execute(
        "INSERT INTO trade_ledger (symbol, client_id, timestamp,"
        " action_taken, order_id, fill_price, created_at)"
        " VALUES ('GC', 4000, ?, 'EXECUTE', 8, NULL, ?)", (old, old))
    con.execute(
        "INSERT INTO market_bars (symbol, client_id, timestamp)"
        " VALUES ('GC', 4000, ?)", (fresh,))
    con.commit()
    con.close()


class TestFleetHealthMainIntegration:

    def _run_main(self, tmp_path, capsys, db_path):
        log_dir = tmp_path / "fleet"
        log_dir.mkdir(exist_ok=True)
        (log_dir / "fleet_20260707.log").write_text(_LOG_CLEAN,
                                                    encoding="utf-8")
        queue = tmp_path / "error_queue"
        queue.mkdir(exist_ok=True)
        rc = fh.main(["--log-dir", str(log_dir), "--db", str(db_path),
                      "--queue-dir", str(queue)])
        out = capsys.readouterr().out
        assert rc == 0
        return out

    def test_main_passes_fill_evidence_explicitly(self, tmp_path, capsys):
        """A6: main() ALWAYS passes fill_evidence explicitly (never relies
        on the parameter default). Red today: check_positions is called
        with three positional args only."""
        db = tmp_path / "fleet_telemetry.db"
        _evidence_db(db)
        with patch.object(fh, "check_positions",
                          return_value=[]) as spy:
            self._run_main(tmp_path, capsys, db)
        assert spy.called, "main() must run the positions check"
        assert "fill_evidence" in spy.call_args.kwargs, (
            "main() must pass fill_evidence explicitly to check_positions "
            "(A6: the default exists only for callers without evidence)"
        )

    def test_main_flags_only_provably_filled_null_fill_rows(self, tmp_path,
                                                            capsys):
        """D3.1+D3.2 end-to-end: order 55 never filled (ORDER_SUBMITTED
        only) -> silent; order 8 has an EXECUTION_FILL tradebook row ->
        flagged; order 77 opened a position (entry_order_id) -> flagged."""
        now = datetime.utcnow()
        old = (now - timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")
        db = tmp_path / "fleet_telemetry.db"
        _evidence_db(
            db,
            ledger_rows=[
                ("GC", 4000, old, "EXECUTE", 55, None, old),  # never filled
                ("GC", 4000, old, "EXECUTE", 8, None, old),   # tradebook fill
                ("GC", 4000, old, "EXECUTE", 77, None, old),  # position fill
            ],
            active_rows=[
                ("GC", 4000, "trade_77", "OPEN", "LONG", 1, 4150.1, None,
                 None, 9, 10, 77, old, None),
            ],
            tradebook_rows=[
                ("EV_SUB_55", "ORDER_SUBMITTED", old, 55, "GC", 4000),
                ("EV_FILL_8", "EXECUTION_FILL", old, 8, "GC", 4000),
            ],
        )
        out = self._run_main(tmp_path, capsys, db)

        assert out.count("HEALTH_EVENT: missing-fill-price") == 2, (
            f"exactly the two provably-filled NULL-fill rows (8, 77) may "
            f"flag; output:\n{out}"
        )
        assert "order_id=55" not in out, (
            "order 55 never filled (ORDER_SUBMITTED is not fill evidence) "
            "— flagging it is the D3 false-positive nag"
        )
        assert "order_id=8" in out
        assert "order_id=77" in out

    def test_main_scopes_incomplete_close_via_fetched_close_time(
            self, tmp_path, capsys):
        """D3.3 end-to-end: main()'s SELECT must fetch close_time; a CLOSED
        row adjudicated 10 days ago stops nagging, a fresh one still
        flags."""
        now = datetime.utcnow()
        old_close = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        recent_close = (now - timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S")
        entry = (now - timedelta(days=11)).strftime("%Y-%m-%d %H:%M:%S")
        db = tmp_path / "fleet_telemetry.db"
        _evidence_db(
            db,
            active_rows=[
                ("GC", 4000, "trade_old", "CLOSED", "LONG", 1, 4150.1, None,
                 "SL_HIT", 9, 10, 5, entry, old_close),
                ("GC", 4000, "trade_recent", "CLOSED", "LONG", 1, 4150.1,
                 None, "SL_HIT", 9, 10, 6, entry, recent_close),
            ],
        )
        out = self._run_main(tmp_path, capsys, db)

        assert out.count("HEALTH_EVENT: incomplete-close") == 1, (
            f"only the <48h close may flag; output:\n{out}"
        )
        assert "trade_recent" in out
        assert "trade_old" not in out

    def test_main_legacy_schema_degrades_to_conservative(self, tmp_path,
                                                         capsys):
        """FENCE (passes today; mirrors the Strict-Locked
        tests/test_fleet_health.py dirty-DB pins): a legacy DB with NO
        tradebook_events table and NO entry_order_id column must not crash
        the positions check — evidence unavailable -> legacy conservative
        flagging (A6): unprotected-position AND missing-fill-price still
        reported."""
        db = tmp_path / "fleet_telemetry.db"
        _legacy_db(db)
        out = self._run_main(tmp_path, capsys, db)

        assert "HEALTH_EVENT: unprotected-position" in out
        assert "HEALTH_EVENT: missing-fill-price" in out, (
            "on a legacy schema (no evidence sources) the checker must "
            "over-flag, never silently skip (A6)"
        )
