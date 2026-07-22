"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
                            (A-7: _last_housekeeping_slot latch initialized
                             None in __init__; _run_hourly_housekeeping()
                             invoked from the event-loop poll body at the
                             same level as the rollover check — anchor: the
                             comment "Runs here (between ib.sleep() calls)"
                             — NOT inside the poll_count % _HEARTBEAT_CYCLES
                             block (poll_count resets on reconnect);
                             _run_hourly_housekeeping SELF-GATES on
                             now_utc.minute >= 15 and (date, hour) !=
                             _last_housekeeping_slot and latches BEFORE the
                             sweep body — same pattern as
                             _check_contract_rollover;
                             A-2: sweep body runs under _ledger_lock, skips
                             silently when disconnected / rolling over /
                             emergency-halted, re-checks is_connected()
                             before EACH broker touch and aborts the
                             remainder on any mid-sweep disconnect; ONLY
                             local-cache primitives (get_open_trades,
                             get_executions, get_cached_position) plus
                             targeted cancel_orders_by_ids allowed inside;
                             A-3: sweep duration measured via
                             lt_module.time (monotonic/time/perf_counter);
                             > ~10s emits a housekeeping-error slow-sweep
                             note; ALL findings of one sweep batch into at
                             most ONE Telegram send; clean sweep sends 0;
                             one summary log line containing
                             "housekeeping";
                             auto-clean: orphan cancel (A-4 live-id
                             exclusion), drift recovery reusing
                             _recover_oob_close -> (reason, price) +
                             _reset_position_state(reason=<truthful>) with
                             the A-5 10-minute activity grace, A-1(b)
                             whitelist ledger repair; detect-only alerts
                             for naked/untracked/ambiguous/unknown-order;
                             _recover_oob_close return type None ->
                             (reason, price) with its startup call site
                             adapted and startup behavior byte-identical;
                             _book_recovered_executions(records) extracted
                             from the A5 EXECUTION_FILL_<execId> /
                             COMMISSION_<execId> booking block, shared by
                             startup recovery and housekeeping),
                            src/live_execution/interfaces/execution_interface.py
                            (A-10: get_open_trades(symbol) — parameter
                             stays REQUIRED, symbol=None now means "ALL
                             symbols on this session"; NEW
                             get_cached_position(symbol) local-cache
                             position read that NEVER routes through
                             ensure_connected),
                            src/live_execution/adapters/ibkr_execution.py
                            (A-10: get_open_trades(None) returns every
                             session order irrespective of contract symbol;
                             get_cached_position reads the ib_insync local
                             portfolio/position cache, never
                             ensure_connected/connect/reqPositions),
                            src/live_execution/adapters/simulated_execution.py
                            (A-10: honest symbol filter — get_open_trades
                             ("GC") returns ONLY GC resting orders,
                             get_open_trades(None) returns all;
                             get_cached_position returns the true sim
                             position, never fabricated),
                            src/live_execution/telemetry.py
                            (A-9: TelemetryDB.repair_closed_position(
                             trade_id, *, exit_price, reason,
                             allow_overwrite_reasons) -> rowcount, UPDATE
                             scoped to trade_id AND status='CLOSED' AND
                             (exit_price IS NULL OR close_reason IN the
                             whitelist) AND this instance's client binding
                             on the shared fleet DB;
                             TelemetryDB.get_recent_closed_positions(
                             hours=48) client-scoped read helper)
Target Class/Function: LiveTrader._run_hourly_housekeeping,
                       LiveTrader._last_housekeeping_slot,
                       LiveTrader._event_loop (housekeeping call site),
                       LiveTrader._book_recovered_executions,
                       LiveTrader._recover_oob_close (return type),
                       ExecutionClient.get_open_trades,
                       ExecutionClient.get_cached_position,
                       IBKRExecutionClient.get_open_trades,
                       IBKRExecutionClient.get_cached_position,
                       SimulatedExecution.get_open_trades,
                       SimulatedExecution.get_cached_position,
                       TelemetryDB.repair_closed_position,
                       TelemetryDB.get_recent_closed_positions
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: hourly-order-housekeeping_07072026_0435
Phase: RED — every blueprint requirement and every binding amendment
A-1(b)..A-10 has at least one test that FAILS on current HEAD. Tests
explicitly marked "FENCE" in their docstrings pass on current code by
design: they pin behavior the feature must PRESERVE (startup
_recover_oob_close observable behavior byte-identical after its
return-type change; bulk cancel_open_orders call sites untouched per
039208d A8; _check_time_barrier's broker-flat branch reason + bulk
cancel — its synthetic bar-price exit_price is DELIBERATELY NOT fenced,
housekeeping repairs those rows under A-1(b); existing
get_open_trades(symbol=<str>) call shapes stay valid).

LIVE-FLEET SAFETY: every I/O boundary is a MagicMock, a purpose-built
strict stub, or a tmp_path DB. No test touches reports/fleet,
.agents/collab/error_queue, C:\\CL_Analyst_Data, the real telemetry DB, a
broker, or a process. No stub sets _health_events_enabled (the opt-in
gate defaults False on object.__new__ stubs, so the production error
queue is unreachable by construction) — health events are asserted by
mocking LiveTrader._emit_health_event and inspecting kinds.

Conventions (mirroring tests/test_oob_entry_state_recovery.py):
  - LiveTrader / adapter stubs via object.__new__ with ONLY the seams
    each method reads.
  - _brain_symbol/_brain_instrument/_tick_size are read-only properties —
    stubs set _instrument_context = SimpleNamespace(brain_symbol=...,
    brain_instrument=MagicMock(), execution_instrument=...), never the
    property itself.
  - pandas 1.5.3 compatible (no pandas 2.x APIs).
  - The __init__ latch test is SOURCE-LEVEL (inspect.getsource) on
    purpose: constructing a real LiveTrader requires a model registry,
    data caches and broker clients (forbidden infra).

FIXED IMPLEMENTATION CONTRACTS (the Coder is bound to these names/shapes):
  1. LiveTrader._run_hourly_housekeeping() — zero-arg, called
     UNCONDITIONALLY on every event-loop poll iteration (at the rollover-
     check level, NOT inside the poll_count % _HEARTBEAT_CYCLES block),
     and SELF-GATING: reads now via lt_module.datetime.now(timezone.utc),
     fires only when now.minute >= 15 and (now.date(), now.hour) !=
     self._last_housekeeping_slot, and sets
     _last_housekeeping_slot = (now.date(), now.hour) BEFORE the sweep
     body (crash must not hot-loop). Before :15, or on the same slot, it
     is a structural no-op (no broker/ledger reads, latch untouched when
     minute < 15). Never raises; any internal failure emits
     _emit_health_event("housekeeping-error", ...) and returns.
  2. Silent-skip gates (NO events, NO broker touches): not
     exec_client.is_connected(), self._rollover_in_progress,
     self._emergency_halt. Latch behavior on a skipped slot is
     deliberately unpinned.
  3. The sweep body runs under self._ledger_lock (with-statement or
     acquire/release — the recording lock accepts both).
  4. Broker access INSIDE the sweep proper is restricted to:
     exec_client.is_connected(), exec_client.get_open_trades(None),
     exec_client.get_executions(...), exec_client.get_cached_position(
     symbol) and exec_client.cancel_orders_by_ids([...]). EVERYTHING
     else on the exec client (get_position, cancel_open_orders,
     place_*, close_position, modify_order, ensure_connected,
     resolve_contract, connect, ...) is FORBIDDEN and will be recorded
     AND raise AssertionError in these tests. The ONE sanctioned
     exception is the reuse of _recover_oob_close for drift recovery —
     it is mocked in every housekeeping stub here, so its internal bulk
     sweep (an A8-fenced startup behavior) never meets the strict
     client.
  5. get_cached_position(symbol) -> int is the NEW cache-read position
     primitive A-2 requires ("position from cached portfolio").
     Interface declares it; IBKR impl reads manager.ib.portfolio() or
     manager.ib.positions() (both stubbed identically here) and NEVER
     calls manager.ensure_connected()/connect(); sim impl returns the
     true sim position.
  6. Mid-sweep disconnect (A-2): is_connected() re-checked before each
     broker touch; on False, the REMAINDER of the sweep is abandoned —
     in the abort test at most ONE broker data touch happens and no
     cancel is issued.
  7. Duration budget (A-3): measured via lt_module.time
     (monotonic/time/perf_counter — the fake clock backs all three);
     duration > ~10s emits ONE housekeeping-error whose detail contains
     the word "slow" (case-insensitive).
  8. Telegram batching (A-3): at most ONE _telegram.send per sweep; a
     finding-free sweep sends ZERO Telegrams and emits ZERO health
     events but still writes one INFO summary log line containing
     "housekeeping" (case-insensitive).
  9. Health-event kinds are EXACTLY: housekeeping-orphan-cancelled,
     housekeeping-drift-detected, housekeeping-ledger-repaired,
     housekeeping-naked-position, housekeeping-untracked-position,
     housekeeping-ambiguous, housekeeping-unknown-order,
     housekeeping-error. Emission goes through
     self._emit_health_event(kind, detail) (mocked here — the existing
     live-gate keeps applying in production).
 10. Orphan auto-clean preconditions (ALL required): _active_trade_id
     is None AND _pending_entry_order_id is None AND
     get_cached_position(...) == 0 AND the resting order id matches a
     recent CLOSED row's tp_order_id/sl_order_id (close_reason
     irrelevant for orphan matching). A-4: an id present in the live
     _tp_order_ids / _sl_order_id / _pending_entry_order_id state is
     NEVER cancelled even when it matches a CLOSED row. Cancel is via
     cancel_orders_by_ids only.
 11. Drift (ledger OPEN row + cached position 0): emits
     housekeeping-drift-detected ALWAYS; A-5 grace — if any
     get_executions record matching the row's entry/tp/sl order ids has
     time within FILL_PRICE_GRACE_MINUTES (10) of now, DETECT ONLY;
     otherwise call self._recover_oob_close(trade_id=..., tp_order_id=...,
     sl_order_id=...) (keyword args, current signature) and then
     self._reset_position_state(reason=<the reason returned>).
     Execution record "time" values arrive as naive-UTC datetimes here.
 12. _recover_oob_close returns (reason, price):
     ("TP_HIT_OOB"/"SL_HIT_OOB", float) on a matched execution,
     ("CLOSED_OOB_UNRECOVERED", None) otherwise. Its startup observable
     behavior (close_position args, targeted-then-bulk cancels,
     deterministic tradebook event ids) is FENCED byte-identical.
 13. Ledger repair (A-1(b)): only rows whose close_reason is in
     {'CLOSED_OOB', 'CLOSED_OOB_UNRECOVERED'} may reach
     telemetry.repair_closed_position, and only with an execution
     matched to that row's OWN tp/sl ids; reason upgrades to
     TP_HIT_OOB/SL_HIT_OOB respectively; allow_overwrite_reasons is
     passed as exactly that two-reason whitelist; rows with any other
     reason (TP_HIT, SL_HIT, TP_HIT_OOB, ...) are NEVER passed to
     repair; no execution match -> row untouched, price never
     synthesized. Matched records are booked via
     self._book_recovered_executions(records) (mocked in sweep stubs;
     tested directly for the deterministic A5 event ids).
 14. _book_recovered_executions(records) is callable with the records
     list as its only positional argument; it writes
     EXECUTION_FILL_<execId> and COMMISSION_<execId> tradebook rows via
     telemetry.log_tradebook_event, byte-stable across wall clocks.
 15. TelemetryDB.repair_closed_position(trade_id, *, exit_price,
     reason, allow_overwrite_reasons) -> int rowcount. UPDATE scope:
     trade_id AND status='CLOSED' AND (exit_price IS NULL OR
     close_reason IN allow_overwrite_reasons) AND the instance's
     client binding. TelemetryDB.get_recent_closed_positions(hours=48)
     -> list[dict] of this client's CLOSED rows with close_time within
     the window (dicts carry at least trade_id, close_reason,
     exit_price, tp_order_id, sl_order_id).
 16. get_open_trades: parameter REQUIRED (no default) in the ABC and
     both adapters; symbol=None -> all symbols; symbol=<str> filters
     honestly in BOTH adapters (the sim currently ignores it — that is
     the red).

DELIBERATE SEAM OMISSIONS (contract for the Coder):
  - Housekeeping stubs do NOT set _health_events_enabled, rolling_df_5m/
    rolling_df_1h, data managers or any bar state: the sweep must be a
    pure broker/ledger reconciler needing NO bar data. Any NEW attribute
    the Coder introduces must be stub-safe via getattr defaults.
  - _recover_oob_close, _reset_position_state and
    _book_recovered_executions are MagicMocks on every housekeeping stub
    (their real behavior has its own direct tests) — the sweep must call
    THROUGH these seams, not inline copies of their logic.
  - Latch behavior when a slot is SKIPPED (disconnect/rollover/halt) is
    unpinned; the Coder may latch-and-skip or leave the slot retryable.
  - The DB-layer cell "exit_price IS NULL AND close_reason NOT
    whitelisted" is deliberately untested at the TelemetryDB layer: the
    sweep-level whitelist (contract 13) guarantees such rows never reach
    repair_closed_position.
  - The strict client's is_connected flip fires after the FIRST broker
    data touch; implementations that batch all reads before any action
    still satisfy the abort test because the cancel (the only
    wire-touching action) must be preceded by a re-check.
"""

from __future__ import annotations

import inspect
import logging
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.live_execution import live_trader as lt_module
from src.live_execution.adapters import ibkr_execution as ibkr_exec_module
from src.live_execution.adapters.simulated_execution import SimulatedExecution
from src.live_execution.fleet_health import FILL_PRICE_GRACE_MINUTES
from src.live_execution.interfaces.execution_interface import (
    ExecutionClient,
    StandardExecutionEvent,
)
from src.live_execution.telemetry import TelemetryDB


# ===========================================================================
# Shared constants / builders
# ===========================================================================

_ALLOWED_KINDS = frozenset({
    "housekeeping-orphan-cancelled",
    "housekeeping-drift-detected",
    "housekeeping-ledger-repaired",
    "housekeeping-naked-position",
    "housekeeping-untracked-position",
    "housekeeping-ambiguous",
    "housekeeping-unknown-order",
    "housekeeping-protective-leg-healed",
    "housekeeping-ledger-persist-failed",
    "position-flat-unconfirmed",
    # oca-stage2-residual-detection_07222026_0141 (additive registry update):
    "oca-race-reversal",
    "rearm-sign-mismatch",
    "protective-leg-partial-fill",
    "housekeeping-error",
})

_OVERWRITE_WHITELIST = {"CLOSED_OOB", "CLOSED_OOB_UNRECOVERED"}

# Frozen "now" for sweeps: 14:20 UTC — past the :15 gate.
_SWEEP_TIME = datetime(2026, 7, 7, 14, 20, 0)
_BEFORE_GATE = datetime(2026, 7, 7, 14, 10, 0)
_NEXT_HOUR = datetime(2026, 7, 7, 15, 20, 0)

_EXEC_ID_TP = "0000e1b7.6866.01.01"
_EXEC_ID_SL = "0000e1b7.6866.02.01"


def _frozen_datetime(now_naive_utc):
    """A real datetime subclass whose now()/utcnow() are frozen.

    Subclassing keeps fromisoformat/strptime/arithmetic fully functional
    for any timestamp parsing the sweep does.
    """
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return now_naive_utc
            return now_naive_utc.replace(tzinfo=tz)

        @classmethod
        def utcnow(cls):
            return now_naive_utc

    return _Frozen


class _FakeTime:
    """lt_module.time replacement for the A-3 budget test.

    First clock read returns 0.0; every later read returns `elapsed` —
    whichever of monotonic()/time()/perf_counter() the implementation
    uses, the measured sweep duration is `elapsed` seconds.
    """

    def __init__(self, elapsed=30.0):
        self._elapsed = elapsed
        self._reads = 0

    def _read(self):
        self._reads += 1
        return 0.0 if self._reads == 1 else self._elapsed

    def monotonic(self):
        return self._read()

    def time(self):
        return self._read()

    def perf_counter(self):
        return self._read()

    def sleep(self, *_a, **_k):
        pass


class _RecordingLock:
    """_ledger_lock stand-in counting entries (with-statement or acquire)."""

    def __init__(self):
        self.entries = 0
        self._lock = threading.Lock()

    def __enter__(self):
        self.entries += 1
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False

    def acquire(self, *a, **k):
        self.entries += 1
        return self._lock.acquire()

    def release(self):
        self._lock.release()


class _StrictExecClient:
    """A-2 enforcement double: ONLY the sweep-legal primitives exist.

    Any other attribute access yields a recorder that raises
    AssertionError when called; even if the sweep's never-raises wrapper
    swallows the AssertionError, `forbidden_calls` keeps the evidence.
    """

    _ALLOWED = ("is_connected", "get_open_trades", "get_executions",
                "get_cached_position", "cancel_orders_by_ids")

    def __init__(self, *, position=0, resting=(), executions=(),
                 connected=True, disconnect_after_first_touch=False):
        self.position = position
        self.resting = list(resting)
        self.executions = list(executions)
        self.connected = connected
        self.disconnect_after = disconnect_after_first_touch
        self.touches = []            # ordered broker data/cancel touches
        self.cancel_requests = []    # list of int-id lists per cancel call
        self.forbidden_calls = []

    # -- permitted primitives ------------------------------------------
    def is_connected(self):
        if self.disconnect_after and self.touches:
            return False
        return self.connected

    def get_cached_position(self, symbol=None):
        self.touches.append("get_cached_position")
        return self.position

    def get_open_trades(self, symbol):
        self.touches.append("get_open_trades")
        if symbol is None:
            return list(self.resting)
        return [e for e in self.resting if e.symbol == symbol]

    def get_executions(self, symbol=None):
        self.touches.append("get_executions")
        if symbol is None:
            return list(self.executions)
        return [r for r in self.executions if r.get("symbol") == symbol]

    def cancel_orders_by_ids(self, order_ids):
        self.touches.append("cancel_orders_by_ids")
        ids = [int(str(i)) for i in order_ids]
        self.cancel_requests.append(ids)
        return len(ids)

    # -- everything else is forbidden ----------------------------------
    def __getattr__(self, name):
        client = self

        def _forbidden(*a, **k):
            client.forbidden_calls.append(name)
            raise AssertionError(
                f"housekeeping sweep touched FORBIDDEN exec primitive "
                f"{name!r} (A-2: only local-cache reads + targeted cancel)"
            )
        return _forbidden

    def cancelled_ids(self):
        return {i for req in self.cancel_requests for i in req}


class _RaisingExecClient(_StrictExecClient):
    """Every data primitive explodes — the never-raises / latch fixture."""

    def __init__(self):
        super().__init__()
        self.boom_calls = 0

    def _boom(self, name):
        self.boom_calls += 1
        raise RuntimeError(f"IBKR cache exploded in {name}")

    def get_cached_position(self, symbol=None):
        self._boom("get_cached_position")

    def get_open_trades(self, symbol):
        self._boom("get_open_trades")

    def get_executions(self, symbol=None):
        self._boom("get_executions")


def _instrument_ctx(tick=0.01):
    return SimpleNamespace(
        brain_symbol="GC",
        brain_instrument=MagicMock(),
        execution_instrument=SimpleNamespace(tick_size=tick, multiplier=100.0),
    )


def _attach_identity_seams(lt):
    """Seams the real _base_tradebook_fields reads."""
    lt._run_id = "live-test-run"
    lt._session_id = "sess-1"
    lt._hostname = "test-host"
    lt._process_id = 4242
    lt._environment = "unit-test"
    lt._front_month_str = "202608"


def _resting_evt(order_id, *, order_type="LMT", symbol="GC", action="SELL",
                 status="Submitted", price=0.0):
    raw = SimpleNamespace(
        order=SimpleNamespace(
            orderId=order_id, orderType=order_type, action=action,
            parentId=0, totalQuantity=1,
            lmtPrice=price if order_type == "LMT" else 0.0,
            auxPrice=price if order_type == "STP" else 0.0,
        ),
        contract=SimpleNamespace(symbol=symbol, localSymbol=f"{symbol}Q6"),
    )
    return StandardExecutionEvent(
        order_id=str(order_id), symbol=symbol, status=status,
        filled_qty=0, remaining_qty=1, avg_price=0.0, raw_event=raw,
    )


def _pending_evt(filled_qty=0):
    raw = SimpleNamespace(
        order=SimpleNamespace(orderId=901, action="BUY", totalQuantity=2,
                              parentId=0),
        orderStatus=SimpleNamespace(status="Submitted", filled=filled_qty,
                                    remaining=2 - filled_qty,
                                    avgFillPrice=68.9 if filled_qty else 0.0),
    )
    return StandardExecutionEvent(
        order_id="901", symbol="GC", status="Submitted",
        filled_qty=filled_qty, remaining_qty=2 - filled_qty,
        avg_price=68.9 if filled_qty else 0.0, raw_event=raw,
    )


def _exec_record(order_id, price, *, side, exec_id, time=None, perm_id=9101,
                 symbol="GC", commission_report=None, qty=1):
    """get_executions RECORD CONTRACT (039208d) — order_id str on purpose
    against int ledger ids (str/int-robust matching)."""
    return {
        "order_id": str(order_id), "perm_id": perm_id, "exec_id": exec_id,
        "price": price, "qty": qty, "side": side,
        "time": time if time is not None
        else _SWEEP_TIME - timedelta(hours=2),
        "symbol": symbol,
        "commission_report": commission_report,
    }


def _commission_report(commission=2.25, realized=95.0):
    return SimpleNamespace(commission=commission, realizedPNL=realized,
                           currency="USD")


def _ledger_open_row(**over):
    base_t = (_SWEEP_TIME - timedelta(hours=5)).isoformat()
    row = {
        "trade_id": "trade_9", "status": "OPEN", "side": "SHORT",
        "quantity": 1, "entry_price": 68.90, "entry_order_id": 200,
        "tp_order_id": 201, "sl_order_id": 202, "tp_price": 67.90,
        "sl_price": 69.90, "atr_at_entry": 0.5, "entry_time": base_t,
        "entry_bar_time": base_t, "close_time": None, "close_reason": None,
        "exit_price": None, "bars_held": None, "symbol": "GC",
        "client_id": 4000,
    }
    row.update(over)
    return row


def _closed_row(trade_id, *, close_reason, exit_price, tp_order_id,
                sl_order_id, hours_ago=1, **over):
    row = _ledger_open_row(
        trade_id=trade_id, status="CLOSED", close_reason=close_reason,
        exit_price=exit_price, tp_order_id=tp_order_id,
        sl_order_id=sl_order_id,
        close_time=(_SWEEP_TIME - timedelta(hours=hours_ago)).isoformat(),
    )
    row.update(over)
    return row


def _housekeeping_stub(*, exec_client=None, position=0, resting=(),
                       executions=(), open_position=None, recent_closed=(),
                       active_trade_id=None, pending_entry=None,
                       pending_filled_qty=0, tp_order_ids=(),
                       sl_order_id=None, rollover=False, halted=False,
                       recover_return=("SL_HIT_OOB", 69.90)):
    """LiveTrader stub with only the seams _run_hourly_housekeeping reads.

    NOTE: _health_events_enabled is deliberately NOT set — health events
    are observed by mocking _emit_health_event, never by enabling the
    production queue path.
    """
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt._instrument_context = _instrument_ctx()
    lt._ledger_lock = threading.Lock()
    lt._last_housekeeping_slot = None
    lt._rollover_in_progress = rollover
    lt._emergency_halt = halted
    lt._running = True
    lt.dry_run = False
    lt._active_trade_id = active_trade_id
    lt._pending_entry_order_id = pending_entry
    lt._pending_entry_bar_time = (
        pd.Timestamp("2026-07-07 14:00:00") if pending_entry else None
    )
    lt._open_orders = (
        {str(pending_entry): _pending_evt(pending_filled_qty)}
        if pending_entry else {}
    )
    lt._tp_order_ids = list(tp_order_ids)
    lt._sl_order_id = sl_order_id
    lt._tracked_tp_price = None
    lt._tracked_sl_price = None
    lt.exec_client = exec_client if exec_client is not None else (
        _StrictExecClient(position=position, resting=resting,
                          executions=executions)
    )
    lt.telemetry = MagicMock()
    lt.telemetry.client_id = 4000
    lt.telemetry.get_open_position.return_value = open_position
    lt.telemetry.get_recent_closed_positions.return_value = list(recent_closed)
    lt.telemetry.repair_closed_position.return_value = 1
    lt._telegram = MagicMock()
    lt._emit_health_event = MagicMock()
    lt._recover_oob_close = MagicMock(return_value=recover_return)
    lt._reset_position_state = MagicMock()
    lt._book_recovered_executions = MagicMock()
    lt.strategy = MagicMock()
    _attach_identity_seams(lt)
    return lt


def _run_sweep(lt, now=_SWEEP_TIME):
    with patch.object(lt_module, "datetime", _frozen_datetime(now)):
        lt._run_hourly_housekeeping()


def _events(lt):
    out = []
    for c in lt._emit_health_event.call_args_list:
        kind = c.args[0] if c.args else c.kwargs.get("kind")
        detail = c.args[1] if len(c.args) > 1 else c.kwargs.get("detail", "")
        out.append((kind, str(detail)))
    return out


def _kinds(lt):
    return [k for k, _ in _events(lt)]


def _repair_calls(lt):
    out = []
    for c in lt.telemetry.repair_closed_position.call_args_list:
        trade_id = c.args[0] if c.args else c.kwargs.get("trade_id")
        out.append((trade_id, c.kwargs))
    return out


# ===========================================================================
# A — Scheduling: gate / latch / poll placement (A-7)
# ===========================================================================

class TestHousekeepingSchedulingA7:

    def test_init_seeds_last_housekeeping_slot_none(self):
        """A-7: __init__ must initialize the latch to None (source-level:
        constructing a real LiveTrader needs forbidden infra)."""
        src = inspect.getsource(lt_module.LiveTrader.__init__)
        assert re.search(r"self\._last_housekeeping_slot\s*=\s*None", src), (
            "LiveTrader.__init__ does not initialize "
            "_last_housekeeping_slot = None — the hourly housekeeping "
            "latch does not exist"
        )

    def test_event_loop_calls_housekeeping_outside_heartbeat_gate(self):
        """A-7 poll-count independence: with only 3 poll iterations
        (poll_count never reaches _HEARTBEAT_CYCLES=60 — exactly the
        reconnect-reset regime, since poll_count zeroes on every
        reconnect), _run_hourly_housekeeping must still have been invoked
        while _log_heartbeat provably never ran."""
        lt = object.__new__(lt_module.LiveTrader)
        lt._running = True
        lt._needs_restart = False
        lt._restart_count = 0
        lt.data_client = MagicMock()
        lt.exec_client = MagicMock()
        lt._telegram = MagicMock()
        lt._reconnect = MagicMock(return_value=True)
        lt._check_contract_rollover = MagicMock()
        lt._check_stale_bars = MagicMock(return_value=False)
        lt._log_heartbeat = MagicMock()
        lt._run_hourly_housekeeping = MagicMock()

        polls = {"n": 0}

        def _sleep(_interval):
            polls["n"] += 1
            if polls["n"] >= 3:
                lt._running = False

        lt.data_client.sleep = _sleep

        with patch.object(lt_module, "datetime",
                          _frozen_datetime(_SWEEP_TIME)):
            lt._event_loop()

        assert lt._log_heartbeat.call_count == 0, (
            "harness sanity: 3 iterations must never reach the "
            "poll_count %% _HEARTBEAT_CYCLES block"
        )
        assert lt._run_hourly_housekeeping.call_count >= 1, (
            "the event loop never invoked _run_hourly_housekeeping outside "
            "the heartbeat block — hiding it behind "
            "poll_count %% _HEARTBEAT_CYCLES delays housekeeping "
            "indefinitely under reconnect flapping (poll_count resets)"
        )

    def test_gate_does_not_fire_before_minute_15(self):
        """A-7: before :15 the method is a structural no-op — no broker
        touches, no events, latch NOT consumed (latching early would skip
        the real :15 sweep of the same hour)."""
        lt = _housekeeping_stub()

        _run_sweep(lt, now=_BEFORE_GATE)

        assert lt.exec_client.touches == [], (
            f"housekeeping ran before :15 (touches="
            f"{lt.exec_client.touches})"
        )
        assert _events(lt) == []
        assert lt._last_housekeeping_slot is None, (
            "the latch must not be consumed before the :15 gate"
        )

    def test_gate_fires_and_latches_at_minute_15_plus(self):
        """A-7: at :20 the sweep runs (broker cache read happens) and the
        latch records the (date, hour) slot."""
        lt = _housekeeping_stub()

        _run_sweep(lt, now=_SWEEP_TIME)

        assert len(lt.exec_client.touches) > 0, (
            "the sweep never touched the broker caches — it did not run"
        )
        assert lt._last_housekeeping_slot == (date(2026, 7, 7), 14), (
            f"latch must be the (date, hour) slot tuple; got "
            f"{lt._last_housekeeping_slot!r}"
        )

    def test_same_slot_noop_and_next_hour_refires(self):
        """A-7 latch: a second poll in the same hour is a no-op; the next
        hour's :15+ poll sweeps again."""
        lt = _housekeeping_stub()

        _run_sweep(lt, now=_SWEEP_TIME)
        touches_after_first = len(lt.exec_client.touches)
        _run_sweep(lt, now=datetime(2026, 7, 7, 14, 45, 0))
        assert len(lt.exec_client.touches) == touches_after_first, (
            "the same (date, hour) slot swept twice — the latch is broken "
            "(every 5s poll would re-sweep)"
        )

        _run_sweep(lt, now=_NEXT_HOUR)
        assert len(lt.exec_client.touches) > touches_after_first, (
            "the next hour's slot did not re-fire the sweep"
        )
        assert lt._last_housekeeping_slot == (date(2026, 7, 7), 15)

    def test_crash_latches_before_sweep_body_no_hot_loop(self):
        """A-7: the latch is set BEFORE the sweep body, so a crashing sweep
        runs ONCE per slot — not on every 5s poll."""
        client = _RaisingExecClient()
        lt = _housekeeping_stub(exec_client=client)

        _run_sweep(lt, now=_SWEEP_TIME)   # must not raise (contract 1)
        assert client.boom_calls >= 1, (
            "harness sanity: the sweep body never reached the exploding "
            "primitive"
        )
        booms_first = client.boom_calls
        events_first = len(_events(lt))
        assert lt._last_housekeeping_slot == (date(2026, 7, 7), 14), (
            "a crashed sweep must still consume its slot (latch BEFORE "
            "body) — otherwise the crash hot-loops every poll"
        )

        _run_sweep(lt, now=datetime(2026, 7, 7, 14, 25, 0))
        assert client.boom_calls == booms_first, (
            "the crashed slot was retried on the next poll — hot loop"
        )
        assert len(_events(lt)) == events_first, (
            "a latched slot must not emit further events"
        )


# ===========================================================================
# B — Sweep guardrails (A-2 / A-3 / never-raises)
# ===========================================================================

class TestSweepGuardrails:

    @pytest.mark.parametrize("state", ["disconnected", "rollover", "halted"])
    def test_skips_silently_when_unsafe(self, state):
        """A-2: disconnected / _rollover_in_progress / _emergency_halt →
        skip silently: no broker touches, no events, no Telegram."""
        lt = _housekeeping_stub(
            exec_client=(_StrictExecClient(connected=False)
                         if state == "disconnected" else None),
            rollover=(state == "rollover"),
            halted=(state == "halted"),
        )

        _run_sweep(lt)

        assert lt.exec_client.touches == [], (
            f"{state}: the sweep touched the broker while unsafe"
        )
        assert _events(lt) == [], f"{state}: skip must be SILENT (no event)"
        assert not lt._telegram.send.called

    def test_sweep_runs_under_ledger_lock(self):
        """The sweep body must hold _ledger_lock (it reads/repairs the same
        ledger state the bar callbacks mutate)."""
        lt = _housekeeping_stub()
        lock = _RecordingLock()
        lt._ledger_lock = lock

        _run_sweep(lt)

        assert lock.entries >= 1, (
            "_run_hourly_housekeeping never acquired _ledger_lock"
        )

    def test_mid_sweep_disconnect_aborts_remaining_touches(self):
        """A-2: is_connected() re-checked before EACH broker touch; a
        disconnect after the first data read abandons the sweep — the
        pending orphan cancel and ledger repair must NOT happen."""
        client = _StrictExecClient(
            position=0,
            resting=[_resting_evt(202, order_type="STP")],
            executions=[_exec_record("201", 67.90, side="BOT",
                                     exec_id=_EXEC_ID_TP)],
            disconnect_after_first_touch=True,
        )
        lt = _housekeeping_stub(
            exec_client=client,
            recent_closed=[
                _closed_row("trade_9", close_reason="SL_HIT", exit_price=69.9,
                            tp_order_id=201, sl_order_id=202),
                _closed_row("trade_8", close_reason="CLOSED_OOB",
                            exit_price=68.05, tp_order_id=201,
                            sl_order_id=202),
            ],
        )

        _run_sweep(lt)   # must not raise

        assert client.cancel_requests == [], (
            "an orphan was CANCELLED after a mid-sweep disconnect — the "
            "pre-touch is_connected() re-check is missing (A-2 deadlock "
            "guard)"
        )
        assert len(client.touches) <= 1, (
            f"broker touched {len(client.touches)} times after the "
            f"disconnect flip ({client.touches}) — the remainder of the "
            f"sweep must be aborted"
        )

    def test_sweep_never_calls_forbidden_primitives(self):
        """A-2: an action-heavy sweep (orphan cancel + ledger repair) must
        complete using ONLY the local-cache primitives + targeted cancel.
        get_position / cancel_open_orders / place_* / close_position /
        modify_order / ensure_connected raise AND are recorded."""
        lt = _housekeeping_stub(
            position=0,
            resting=[_resting_evt(202, order_type="STP")],
            executions=[_exec_record("201", 67.90, side="BOT",
                                     exec_id=_EXEC_ID_TP,
                                     commission_report=_commission_report())],
            recent_closed=[
                _closed_row("trade_9", close_reason="SL_HIT", exit_price=69.9,
                            tp_order_id=201, sl_order_id=202),
                _closed_row("trade_8", close_reason="CLOSED_OOB",
                            exit_price=68.05, tp_order_id=201,
                            sl_order_id=202),
            ],
        )

        _run_sweep(lt)

        assert lt.exec_client.forbidden_calls == [], (
            f"the sweep touched forbidden exec primitives "
            f"{lt.exec_client.forbidden_calls} — ensure_connected-routed "
            f"calls under _ledger_lock re-enter the event loop and "
            f"deadlock (A-2)"
        )
        assert lt.exec_client.cancel_requests, (
            "harness sanity: the orphan was expected to be cancelled via "
            "cancel_orders_by_ids in this scenario"
        )

    def test_never_raises_and_emits_housekeeping_error(self):
        """Never-raises contract: a throwing exec client must not
        propagate; the failure surfaces as a housekeeping-error event."""
        lt = _housekeeping_stub(exec_client=_RaisingExecClient())

        _run_sweep(lt)   # raising here fails the test naturally

        assert "housekeeping-error" in _kinds(lt), (
            f"a sweep failure must emit housekeeping-error; got kinds="
            f"{_kinds(lt)}"
        )

    def test_slow_sweep_emits_budget_note(self):
        """A-3: a sweep measured over ~10s emits a housekeeping-error
        slow-sweep note (detail contains 'slow'). Clean stub: the budget
        note is the ONLY legitimate event."""
        lt = _housekeeping_stub()

        with patch.object(lt_module, "time", _FakeTime(elapsed=30.0)):
            _run_sweep(lt)

        slow = [(k, d) for k, d in _events(lt) if k == "housekeeping-error"]
        assert slow, (
            f"a 30s sweep emitted no housekeeping-error budget note; "
            f"kinds={_kinds(lt)}"
        )
        assert any("slow" in d.lower() for _, d in slow), (
            f"the budget note must identify itself as a slow sweep; "
            f"details={[d for _, d in slow]!r}"
        )

    def test_multiple_findings_one_batched_telegram(self):
        """A-3: ALL findings of one sweep batch into at most ONE Telegram
        send (naked + untracked here)."""
        lt = _housekeeping_stub(position=2, open_position=None, resting=[])

        _run_sweep(lt)

        kinds = set(_kinds(lt))
        assert {"housekeeping-naked-position",
                "housekeeping-untracked-position"} <= kinds, (
            f"harness sanity: both detect findings expected; got {kinds}"
        )
        assert lt._telegram.send.call_count == 1, (
            f"{lt._telegram.send.call_count} Telegram sends for one sweep "
            f"— findings must batch into exactly ONE message (A-3)"
        )

    def test_clean_sweep_is_silent_but_logged(self, caplog):
        """A-3/A-7: a finding-free sweep emits NO health events, sends NO
        Telegram, still advances the latch and writes one INFO summary
        line containing 'housekeeping'."""
        lt = _housekeeping_stub(
            recent_closed=[_closed_row("trade_5", close_reason="TP_HIT",
                                       exit_price=67.10, tp_order_id=301,
                                       sl_order_id=302)],
        )

        with caplog.at_level(logging.INFO):
            _run_sweep(lt)

        assert _events(lt) == [], (
            f"a clean sweep must emit nothing; got {_events(lt)}"
        )
        assert not lt._telegram.send.called, (
            "a clean hourly sweep must not Telegram (24 messages/day of "
            "nothing)"
        )
        assert lt._last_housekeeping_slot == (date(2026, 7, 7), 14)
        summary = [r for r in caplog.records
                   if "housekeeping" in r.getMessage().lower()
                   and r.levelno >= logging.INFO]
        assert summary, (
            "the sweep must write one INFO summary log line mentioning "
            "housekeeping"
        )

    def test_emitted_kinds_stay_in_the_contract_set(self):
        """Kind vocabulary: across action, detect and error sweeps, every
        emitted kind must be one of the eight contract kinds."""
        stubs = [
            _housekeeping_stub(
                resting=[_resting_evt(202, order_type="STP")],
                executions=[_exec_record("201", 67.90, side="BOT",
                                         exec_id=_EXEC_ID_TP)],
                recent_closed=[
                    _closed_row("trade_8", close_reason="CLOSED_OOB",
                                exit_price=68.05, tp_order_id=201,
                                sl_order_id=202),
                ],
            ),
            _housekeeping_stub(position=2, open_position=None),
            _housekeeping_stub(exec_client=_RaisingExecClient()),
        ]
        seen = set()
        for lt in stubs:
            _run_sweep(lt)
            seen |= set(_kinds(lt))

        assert seen, "harness sanity: at least one event expected"
        assert seen <= _ALLOWED_KINDS, (
            f"kinds outside the contract vocabulary: "
            f"{seen - _ALLOWED_KINDS} (SKILL.md triage routes on exact "
            f"kind strings)"
        )


# ===========================================================================
# C — Orphan auto-clean (action (a), A-4)
# ===========================================================================

class TestOrphanCancel:

    def test_orphaned_protective_order_cancelled_targeted(self):
        """(a): flat everywhere + resting order matching a recent CLOSED
        row's sl_order_id → targeted cancel of exactly that id + the
        housekeeping-orphan-cancelled event. Reason SL_HIT on the CLOSED
        row on purpose: orphan matching is id-based, not reason-based."""
        lt = _housekeeping_stub(
            resting=[_resting_evt(202, order_type="STP")],
            recent_closed=[_closed_row("trade_9", close_reason="SL_HIT",
                                       exit_price=69.90, tp_order_id=201,
                                       sl_order_id=202)],
        )

        _run_sweep(lt)

        assert lt.exec_client.cancelled_ids() == {202}, (
            f"expected the orphaned protective order 202 (and ONLY it) to "
            f"be cancelled via cancel_orders_by_ids; got "
            f"{lt.exec_client.cancel_requests} — this is the mid-session "
            f"orphan that today rests until the next restart"
        )
        assert "housekeeping-orphan-cancelled" in _kinds(lt)

    def test_live_bracket_ids_never_cancelled_a4(self):
        """A-4: an id in the live _tp_order_ids state is NEVER cancelled
        even when it matches a CLOSED row (TWS id-sequence reuse); the
        non-live sibling still is."""
        lt = _housekeeping_stub(
            resting=[_resting_evt(201, order_type="LMT"),
                     _resting_evt(202, order_type="STP")],
            recent_closed=[_closed_row("trade_9", close_reason="SL_HIT",
                                       exit_price=69.90, tp_order_id=201,
                                       sl_order_id=202)],
            tp_order_ids=[201],   # stale-but-LIVE tracked id
        )

        _run_sweep(lt)

        cancelled = lt.exec_client.cancelled_ids()
        assert 201 not in cancelled, (
            "order 201 is tracked in the LIVE _tp_order_ids state and was "
            "cancelled anyway — A-4 exists precisely for broker id reuse "
            "after TWS id-sequence resets"
        )
        assert cancelled == {202}, (
            f"the non-live orphan 202 must still be cancelled; got "
            f"{cancelled}"
        )

    def test_pending_entry_blocks_orphan_cancel(self):
        """(a) preconditions: a pending entry means the orphan action is
        skipped wholesale — nothing is cancelled."""
        lt = _housekeeping_stub(
            pending_entry=901,
            resting=[_resting_evt(901), _resting_evt(202, order_type="STP")],
            recent_closed=[_closed_row("trade_9", close_reason="SL_HIT",
                                       exit_price=69.90, tp_order_id=201,
                                       sl_order_id=202)],
        )

        _run_sweep(lt)

        assert lt.exec_client.cancel_requests == [], (
            f"orphan cancel ran with a pending entry in flight; requested "
            f"{lt.exec_client.cancel_requests}"
        )

    @pytest.mark.parametrize("case", ["broker-position", "active-trade"])
    def test_nonflat_state_blocks_orphan_cancel(self, case):
        """(a) preconditions: cached broker position != 0 OR an active
        trade id → no cancel, ever."""
        lt = _housekeeping_stub(
            position=1 if case == "broker-position" else 0,
            active_trade_id=("trade_9" if case == "active-trade" else None),
            open_position=(_ledger_open_row()
                           if case == "active-trade" else None),
            resting=[_resting_evt(202, order_type="STP")],
            recent_closed=[_closed_row("trade_7", close_reason="TP_HIT",
                                       exit_price=67.1, tp_order_id=201,
                                       sl_order_id=202)],
        )

        _run_sweep(lt)

        assert lt.exec_client.cancel_requests == [], (
            f"{case}: orphan cancel fired while not provably flat"
        )

    def test_second_sweep_after_cleanup_is_noop(self):
        """Idempotency: after sweep 1 cancels the orphan and repairs the
        row, the NEXT hour's sweep (post-action broker/ledger state) takes
        no action and emits no action events."""
        lt = _housekeeping_stub(
            resting=[_resting_evt(202, order_type="STP")],
            executions=[_exec_record("201", 67.90, side="BOT",
                                     exec_id=_EXEC_ID_TP)],
            recent_closed=[
                _closed_row("trade_9", close_reason="SL_HIT", exit_price=69.9,
                            tp_order_id=211, sl_order_id=202),
                _closed_row("trade_8", close_reason="CLOSED_OOB",
                            exit_price=68.05, tp_order_id=201,
                            sl_order_id=212),
            ],
        )

        _run_sweep(lt, now=_SWEEP_TIME)
        assert lt.exec_client.cancelled_ids() == {202}, "harness sanity"
        assert len(_repair_calls(lt)) == 1, "harness sanity"
        cancels_after_first = len(lt.exec_client.cancel_requests)
        events_after_first = len(_events(lt))

        # Post-action world: order gone from the broker, row repaired.
        lt.exec_client.resting = []
        lt.telemetry.get_recent_closed_positions.return_value = [
            _closed_row("trade_9", close_reason="SL_HIT", exit_price=69.9,
                        tp_order_id=211, sl_order_id=202),
            _closed_row("trade_8", close_reason="TP_HIT_OOB",
                        exit_price=67.90, tp_order_id=201, sl_order_id=212),
        ]
        lt.telemetry.repair_closed_position.return_value = 0

        _run_sweep(lt, now=_NEXT_HOUR)

        assert len(lt.exec_client.cancel_requests) == cancels_after_first, (
            "the second sweep re-cancelled — auto-clean is not idempotent"
        )
        second_kinds = [k for k, _ in _events(lt)[events_after_first:]]
        assert "housekeeping-orphan-cancelled" not in second_kinds
        assert "housekeeping-ledger-repaired" not in second_kinds, (
            f"the second sweep re-reported actions on an already-clean "
            f"state: {second_kinds}"
        )


# ===========================================================================
# D — Drift recovery (action (b), A-5)
# ===========================================================================

class TestDriftRecovery:

    def test_drift_recovers_truthfully_and_resets_with_returned_reason(self):
        """(b): ledger OPEN + cached broker flat + NO recent activity →
        housekeeping-drift-detected, _recover_oob_close(trade/tp/sl kwargs)
        and _reset_position_state(reason=<the recovered reason>) for
        cooldown parity with the legacy path."""
        stale = _SWEEP_TIME - timedelta(
            minutes=FILL_PRICE_GRACE_MINUTES + 15)
        lt = _housekeeping_stub(
            position=0,
            active_trade_id="trade_9",
            open_position=_ledger_open_row(),
            executions=[_exec_record("202", 69.90, side="BOT",
                                     exec_id=_EXEC_ID_SL, time=stale)],
            recover_return=("SL_HIT_OOB", 69.90),
        )

        _run_sweep(lt)

        assert "housekeeping-drift-detected" in _kinds(lt)
        assert lt._recover_oob_close.called, (
            "drift must be resolved through the shared _recover_oob_close "
            "path (truthful reason/price), not an inline copy"
        )
        kw = lt._recover_oob_close.call_args.kwargs
        assert kw.get("trade_id") == "trade_9"
        assert kw.get("tp_order_id") == 201
        assert kw.get("sl_order_id") == 202
        assert lt._reset_position_state.called, (
            "the legacy broker-flat path resets position state (cooldown "
            "parity) — housekeeping must too"
        )
        reset = lt._reset_position_state.call_args
        reason = (reset.kwargs.get("reason")
                  if reset.kwargs.get("reason") is not None
                  else (reset.args[0] if reset.args else None))
        assert reason == "SL_HIT_OOB", (
            f"_reset_position_state must receive the TRUTHFUL recovered "
            f"reason (SL_HIT_OOB), got {reason!r}"
        )

    def test_recent_activity_grace_skips_action(self):
        """A-5: fill/order activity within FILL_PRICE_GRACE_MINUTES of now
        (a fill callback may be in flight) → detect-only note, NO
        _recover_oob_close, NO reset."""
        fresh = _SWEEP_TIME - timedelta(minutes=3)
        assert 3 < FILL_PRICE_GRACE_MINUTES, "harness sanity"
        lt = _housekeeping_stub(
            position=0,
            active_trade_id="trade_9",
            open_position=_ledger_open_row(),
            executions=[_exec_record("202", 69.90, side="BOT",
                                     exec_id=_EXEC_ID_SL, time=fresh)],
        )

        _run_sweep(lt)

        assert not lt._recover_oob_close.called, (
            "drift acted on a trade with fill activity 3 minutes old — "
            "the in-flight fill callback will double-close (A-5 grace)"
        )
        assert not lt._reset_position_state.called
        assert "housekeeping-drift-detected" in _kinds(lt), (
            "the graced drift must still be NOTED (detect-only)"
        )


# ===========================================================================
# E — Ledger repair (action (c), A-1(b))
# ===========================================================================

class TestLedgerRepairA1b:

    def test_whitelist_exactness(self):
        """A-1(b): of three recent CLOSED rows —
        synthetic-price CLOSED_OOB with a proven own-bracket fill (REPAIR),
        TP_HIT with a matching fill (NEVER touched),
        CLOSED_OOB_UNRECOVERED with no fill match (untouched, never
        synthesized) — exactly ONE repair happens, with the proven price,
        the upgraded reason and the exact two-reason whitelist."""
        lt = _housekeeping_stub(
            executions=[
                _exec_record("201", 67.90, side="BOT", exec_id=_EXEC_ID_TP,
                             commission_report=_commission_report()),
                _exec_record("301", 67.15, side="BOT",
                             exec_id="0000ffff.0001.01"),
            ],
            recent_closed=[
                _closed_row("trade_8", close_reason="CLOSED_OOB",
                            exit_price=68.05,   # synthetic bar price
                            tp_order_id=201, sl_order_id=212),
                _closed_row("trade_5", close_reason="TP_HIT",
                            exit_price=67.10, tp_order_id=301,
                            sl_order_id=302),
                _closed_row("trade_6", close_reason="CLOSED_OOB_UNRECOVERED",
                            exit_price=None, tp_order_id=401,
                            sl_order_id=402),
            ],
        )

        _run_sweep(lt)

        repairs = _repair_calls(lt)
        assert [t for t, _ in repairs] == ["trade_8"], (
            f"exactly the synthetic CLOSED_OOB row may be repaired; got "
            f"repairs for {[t for t, _ in repairs]} (TP_HIT rows are NEVER "
            f"touched; no-match rows are NEVER synthesized)"
        )
        kw = repairs[0][1]
        assert kw.get("exit_price") == pytest.approx(67.90), (
            f"repair must write the PROVEN fill price 67.90, got "
            f"{kw.get('exit_price')!r}"
        )
        assert kw.get("reason") == "TP_HIT_OOB", (
            f"a fill on the row's own TP id upgrades the reason to "
            f"TP_HIT_OOB, got {kw.get('reason')!r}"
        )
        assert set(kw.get("allow_overwrite_reasons") or ()) == (
            _OVERWRITE_WHITELIST
        ), (
            f"allow_overwrite_reasons must be exactly "
            f"{_OVERWRITE_WHITELIST}, got "
            f"{kw.get('allow_overwrite_reasons')!r}"
        )
        repaired_kinds = [k for k in _kinds(lt)
                          if k == "housekeeping-ledger-repaired"]
        assert len(repaired_kinds) == 1
        assert lt._book_recovered_executions.called, (
            "the matched execution must be booked through the shared "
            "_book_recovered_executions helper (A5-idempotent tradebook "
            "rows)"
        )
        booked = lt._book_recovered_executions.call_args
        flat = list(booked.args) + list(booked.kwargs.values())
        recs = []
        for item in flat:
            if isinstance(item, dict):
                recs.append(item)
            elif isinstance(item, (list, tuple)):
                recs.extend(r for r in item if isinstance(r, dict))
        assert any(r.get("exec_id") == _EXEC_ID_TP for r in recs), (
            f"_book_recovered_executions must receive the matched "
            f"execution record; got {recs!r}"
        )

    def test_sl_execution_upgrades_reason_to_sl_hit_oob(self):
        """A-1(b): a NULL-price CLOSED_OOB row whose own SL id matches a
        fill repairs to SL_HIT_OOB at the fill price (the trade_8
        permanent-NULL class)."""
        lt = _housekeeping_stub(
            executions=[_exec_record("202", 69.90, side="BOT",
                                     exec_id=_EXEC_ID_SL)],
            recent_closed=[_closed_row("trade_7", close_reason="CLOSED_OOB",
                                       exit_price=None, tp_order_id=201,
                                       sl_order_id=202)],
        )

        _run_sweep(lt)

        repairs = _repair_calls(lt)
        assert [t for t, _ in repairs] == ["trade_7"]
        kw = repairs[0][1]
        assert kw.get("reason") == "SL_HIT_OOB"
        assert kw.get("exit_price") == pytest.approx(69.90)


# ===========================================================================
# F — Detect-and-alert ONLY (never act)
# ===========================================================================

class TestDetectOnly:

    def test_naked_position_alert_never_places_protection(self):
        """Naked (position != 0, no resting SL): alert only — placing
        protection is order-routing semantics (human gate) and interacts
        with the 10-orders/60s halt."""
        lt = _housekeeping_stub(
            position=2,
            active_trade_id="trade_9",
            open_position=_ledger_open_row(side="LONG"),
        )

        _run_sweep(lt)

        assert "housekeeping-naked-position" in _kinds(lt), (
            f"a position with no resting SL must alert; kinds={_kinds(lt)}"
        )
        assert lt.exec_client.forbidden_calls == [], (
            f"the naked-position path CALLED {lt.exec_client.forbidden_calls} "
            f"— housekeeping must never place/modify orders"
        )
        assert lt.exec_client.cancel_requests == []

    def test_resting_sl_suppresses_naked_alert(self):
        """A protected position (STP resting) is not naked."""
        lt = _housekeeping_stub(
            position=2,
            active_trade_id="trade_9",
            open_position=_ledger_open_row(side="LONG"),
            resting=[_resting_evt(202, order_type="STP", action="SELL")],
        )

        _run_sweep(lt)

        assert "housekeeping-naked-position" not in _kinds(lt), (
            "a position protected by a resting STP order was flagged naked"
        )

    def test_untracked_position_alert_never_closes(self):
        """Untracked (broker position != 0, ledger flat): alert only."""
        lt = _housekeeping_stub(
            position=2,
            open_position=None,
            resting=[_resting_evt(202, order_type="STP", action="SELL")],
        )

        _run_sweep(lt)

        assert "housekeeping-untracked-position" in _kinds(lt)
        assert lt.exec_client.forbidden_calls == [], (
            f"untracked-position handling touched "
            f"{lt.exec_client.forbidden_calls} — never auto-close a "
            f"position the ledger does not own"
        )
        assert lt.exec_client.cancel_requests == []

    def test_partial_fill_pending_entry_is_ambiguous(self):
        """Pending-entry partial fill (A1-039208d detection reused):
        contracts exist broker-side — alert, never cancel."""
        lt = _housekeeping_stub(
            position=1,
            pending_entry=901,
            pending_filled_qty=1,
            resting=[_resting_evt(901, order_type="LMT", action="BUY")],
        )

        _run_sweep(lt)

        assert "housekeeping-ambiguous" in _kinds(lt), (
            f"a partially-filled pending entry must raise "
            f"housekeeping-ambiguous; kinds={_kinds(lt)}"
        )
        assert lt.exec_client.cancel_requests == [], (
            "the ambiguous path cancelled orders — a partial fill means "
            "contracts EXIST broker-side"
        )

    def test_unknown_order_alert_excludes_pending_entry_a6(self):
        """A-6: while ledger-flat, a resting order matching neither recent
        CLOSED brackets nor the pending entry id is unknown-bot-origin —
        alert only. The legitimate resting ENTRY order is NOT flagged."""
        lt = _housekeeping_stub(
            position=0,
            pending_entry=901,
            resting=[_resting_evt(901, order_type="LMT", action="BUY"),
                     _resting_evt(555, order_type="LMT", action="SELL")],
            recent_closed=[_closed_row("trade_9", close_reason="SL_HIT",
                                       exit_price=69.9, tp_order_id=201,
                                       sl_order_id=202)],
        )

        _run_sweep(lt)

        unknown = [(k, d) for k, d in _events(lt)
                   if k == "housekeeping-unknown-order"]
        assert len(unknown) == 1, (
            f"exactly ONE unknown-order alert expected (order 555); got "
            f"{unknown!r} — entry orders legitimately rest while flat"
        )
        assert "555" in unknown[0][1], (
            f"the alert must identify the unknown order id 555; detail="
            f"{unknown[0][1]!r}"
        )
        assert "901" not in unknown[0][1], (
            f"the pending entry 901 was reported unknown; detail="
            f"{unknown[0][1]!r}"
        )
        assert lt.exec_client.cancel_requests == [], (
            "unknown-order is detect-only (A-6) — never cancel a possibly "
            "human-placed order"
        )


# ===========================================================================
# G — Shared booking helper (_book_recovered_executions)
# ===========================================================================

class TestBookRecoveredExecutions:

    def _booking_stub(self):
        lt = object.__new__(lt_module.LiveTrader)
        lt._execution_symbol = "GC"
        lt._instrument_context = _instrument_ctx()
        lt.telemetry = MagicMock()
        _attach_identity_seams(lt)
        return lt

    def _tradebook_calls(self, lt, event_type):
        return [c for c in lt.telemetry.log_tradebook_event.call_args_list
                if c.kwargs.get("event_type") == event_type]

    def test_helper_books_deterministic_fill_and_commission_rows(self):
        """The extracted helper writes EXECUTION_FILL_<execId> and
        COMMISSION_<execId> rows byte-stable across wall clocks (A5:
        INSERT OR IGNORE dedupe across restarts AND hourly sweeps)."""
        def _run(now_iso):
            lt = self._booking_stub()
            lt._utc_iso_now = lambda: now_iso
            rec = _exec_record("201", 67.90, side="BOT",
                               exec_id=_EXEC_ID_TP,
                               commission_report=_commission_report())
            lt._book_recovered_executions([rec])
            fills = self._tradebook_calls(lt, "EXECUTION_FILL")
            assert fills, (
                "_book_recovered_executions wrote no EXECUTION_FILL row"
            )
            assert str(fills[0].kwargs.get("order_id")) == "201"
            commissions = self._tradebook_calls(lt, "COMMISSION")
            assert commissions, (
                "a commission_report-bearing record must produce a "
                "COMMISSION row"
            )
            assert commissions[0].kwargs.get("event_id") == (
                f"COMMISSION_{_EXEC_ID_TP}"
            )
            return fills[0].kwargs.get("event_id")

        id_first = _run("2026-07-07T14:20:00")
        id_second = _run("2026-07-07T21:45:00")
        assert id_first == f"EXECUTION_FILL_{_EXEC_ID_TP}", (
            f"EXECUTION_FILL event ids must stay execId-keyed; got "
            f"{id_first!r}"
        )
        assert id_first == id_second, (
            f"booking event_id changed with the wall clock ({id_first!r} "
            f"!= {id_second!r}) — hourly sweeps would duplicate rows"
        )


# ===========================================================================
# H — Startup recovery: return-type change + byte-identical FENCEs
# ===========================================================================

def _recovery_stub(executions, *, ledger_pos=None, cancel_by_ids=1):
    """Startup-path stub (039208d seams): telemetry ledger row + flat IBKR.

    NOTE: this is the STARTUP path — get_position / cancel_open_orders are
    legitimate here (A-2 restricts only the hourly sweep)."""
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "GC"
    lt._instrument_context = _instrument_ctx()
    lt.telemetry = MagicMock()
    lt.telemetry.get_open_position.return_value = (
        ledger_pos if ledger_pos is not None else _ledger_open_row(
            trade_id="trade_777")
    )
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = 0   # IBKR flat -> OOB branch
    lt.exec_client.get_position_settled.return_value = 0  # settled CONFIRMS flat
    lt.exec_client.cancel_orders_by_ids = MagicMock(return_value=cancel_by_ids)
    lt.exec_client.get_executions = MagicMock(return_value=executions)
    lt.exec_client.cancel_open_orders = MagicMock(return_value=0)
    lt._telegram = MagicMock()
    lt._open_orders = {}
    lt.rolling_df_5m = None
    lt.rolling_df_1h = None
    lt._bar_size = "5m"
    lt.strategy = MagicMock()
    lt._position_bars_held = 0
    lt.telemetry.get_recent_closed_positions.return_value = []
    _attach_identity_seams(lt)
    return lt


class TestStartupRecovery:

    def test_recover_oob_close_returns_reason_price_tuple(self):
        """RED: _recover_oob_close must return (reason, price) so the
        housekeeping drift branch can reset with the truthful reason.
        Matched TP fill -> ("TP_HIT_OOB", price); nothing -> ("
        CLOSED_OOB_UNRECOVERED", None)."""
        lt = _recovery_stub([_exec_record("201", 67.90, side="BOT",
                                          exec_id=_EXEC_ID_TP)])
        result = lt._recover_oob_close(trade_id="trade_777",
                                       tp_order_id=201, sl_order_id=202)
        assert result == ("TP_HIT_OOB", pytest.approx(67.90)), (
            f"_recover_oob_close must return the (reason, price) tuple, "
            f"got {result!r}"
        )

        lt2 = _recovery_stub([], cancel_by_ids=2)
        result2 = lt2._recover_oob_close(trade_id="trade_777",
                                         tp_order_id=201, sl_order_id=202)
        assert result2 == ("CLOSED_OOB_UNRECOVERED", None), (
            f"unrecovered OOB close must return "
            f"('CLOSED_OOB_UNRECOVERED', None), got {result2!r}"
        )

    def test_startup_oob_tp_leg_byte_identical(self):
        """FENCE (passes today): after the return-type refactor the startup
        flow must stay byte-identical — truthful close, targeted-then-bulk
        cancels, deterministic tradebook ids."""
        lt = _recovery_stub([_exec_record("201", 67.90, side="BOT",
                                          exec_id=_EXEC_ID_TP,
                                          commission_report=_commission_report())])

        lt._recover_inherited_position()

        close_calls = lt.telemetry.close_position.call_args_list
        assert len(close_calls) == 1
        c = close_calls[0]
        trade_id = c.args[0] if c.args else c.kwargs.get("trade_id")
        assert trade_id == "trade_777"
        assert c.kwargs.get("reason") == "TP_HIT_OOB"
        assert c.kwargs.get("exit_price") == pytest.approx(67.90)

        # targeted cancel of the ledger row's own ids, then the bulk sweep
        cbi = lt.exec_client.cancel_orders_by_ids.call_args
        ids = {int(str(i))
               for i in (cbi.args[0] if cbi.args
                         else cbi.kwargs.get("order_ids"))}
        assert 202 in ids and ids <= {201, 202}
        bulk = lt.exec_client.cancel_open_orders.call_args
        bulk_symbol = bulk.kwargs.get("symbol",
                                      bulk.args[0] if bulk.args else None)
        assert bulk_symbol == "GC", (
            "A8 FENCE: the belt-and-braces bulk symbol-scoped sweep must "
            "survive the refactor"
        )

        fills = [c for c in lt.telemetry.log_tradebook_event.call_args_list
                 if c.kwargs.get("event_type") == "EXECUTION_FILL"]
        assert fills and fills[0].kwargs.get("event_id") == (
            f"EXECUTION_FILL_{_EXEC_ID_TP}"
        )
        commissions = [
            c for c in lt.telemetry.log_tradebook_event.call_args_list
            if c.kwargs.get("event_type") == "COMMISSION"
        ]
        assert commissions and commissions[0].kwargs.get("event_id") == (
            f"COMMISSION_{_EXEC_ID_TP}"
        )

        # cooldown-not-restored-on-restart_07082026_0230: the startup OOB
        # branch now ARMS the strategy re-entry cooldown with the truthful
        # recovered reason and the LEDGER side (SHORT → -1). (The three fences
        # above are unchanged; this is the added authorized behavior.)
        assert lt.strategy.on_exit.called, (
            "startup OOB recovery must arm the strategy cooldown"
        )
        args = lt.strategy.on_exit.call_args.args
        assert args[0] == -1 and args[1] == "TP_HIT_OOB", (
            f"cooldown must be armed with (ledger_side=-1, truthful reason), "
            f"got {args!r}"
        )

    def test_startup_oob_unrecovered_null_price_fence(self):
        """FENCE (passes today): no execution evidence → explicit
        CLOSED_OOB_UNRECOVERED with a NULL exit price — never fabricated."""
        lt = _recovery_stub([], cancel_by_ids=2)

        lt._recover_inherited_position()

        c = lt.telemetry.close_position.call_args
        assert c.kwargs.get("reason") == "CLOSED_OOB_UNRECOVERED"
        assert c.kwargs.get("exit_price") is None

    def test_startup_flat_sweep_bulk_cancel_fence(self):
        """FENCE (passes today, A8): _cancel_orphaned_orders_on_startup
        keeps the bulk symbol-scoped cancel."""
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

    def test_time_barrier_broker_flat_branch_fence(self, caplog):
        """FENCE (re-pointed by settle-confirm-event-loop_07202026_0713):
        Direction A supersedes the v1 in-callback broker-flat branch. The
        confirmed-flat out-of-band close is now booked BYTE-FOR-BYTE by the
        idle-loop reconciler's flat-read branch (_book_out_of_band_close),
        NOT inline in _check_time_barrier (which defers the flat read). What
        this fence guards is UNCHANGED, only relocated: CLOSED_OOB close
        reason, bulk symbol cancel, strategy cooldown via
        _reset_position_state -> on_exit. The exit_price is DELIBERATELY NOT
        pinned — the idle reconciler has no bar price and books an explicit
        None (honest-unknown); housekeeping / kill switch own any priced
        flatten (A-1(b))."""
        lt = object.__new__(lt_module.LiveTrader)
        lt._execution_symbol = "GC"
        lt._instrument_context = _instrument_ctx()
        lt.exec_client = MagicMock()
        lt.exec_client.get_position.return_value = 0
        lt.exec_client.get_position_settled.return_value = 0  # settled CONFIRMS flat
        lt.exec_client.cancel_open_orders.return_value = 1
        lt.telemetry = MagicMock()
        lt._telegram = MagicMock()
        strategy = MagicMock()
        lt.strategy = strategy
        lt._active_trade_id = "trade_9"
        lt._position_entry_bar_time = pd.Timestamp("2026-07-07 10:00:00")
        lt._position_bars_held = 4
        lt._trailing_activated = False
        lt._entry_price = 68.0
        lt._atr_at_entry = 0.42
        lt._position_side = 1
        lt._highest_high = 68.35
        lt._lowest_low = 67.85
        lt._trade_trailing_atr_mult = None
        lt._trade_max_hold_bars = None
        lt._tp_order_ids = [201]
        lt._sl_order_id = 202
        lt._tracked_tp_price = None
        lt._tracked_sl_price = None
        # TIME BARRIER submit-and-defer state (settle-confirm-event-loop_07202026_0713):
        # the re-entrancy guard at the top of _check_time_barrier reads this.
        lt._pending_exit_order_id = None
        _attach_identity_seams(lt)

        # In-callback: a flat cache read for a tracked trade DEFERS (no inline
        # confirm/book/reset). The confirmed-flat OOB book runs in the reconciler.
        result = lt._check_time_barrier(
            bar_time=pd.Timestamp("2026-07-07 14:00:00"),
            current_price=68.11, atr_value=0.4,
        )
        assert result is False
        lt.telemetry.close_position.assert_not_called()  # deferred, not booked inline
        lt.exec_client.cancel_open_orders.assert_not_called()

        # Idle-loop reconciler: settled CONFIRMS flat -> book the OOB close
        # (byte-for-byte the relocated broker-flat branch).
        lt._reconcile_pending_position_state()

        c = lt.telemetry.close_position.call_args
        assert c.kwargs.get("reason") == "CLOSED_OOB", (
            "the broker-flat OOB branch must keep writing CLOSED_OOB "
            "(the reason housekeeping's whitelist keys on)"
        )
        lt.exec_client.cancel_open_orders.assert_called_once_with(symbol="GC")
        assert strategy.on_exit.called
        assert strategy.on_exit.call_args.args[1] == "CLOSED_OOB"


# ===========================================================================
# I — Interface widening: get_open_trades(None) == all symbols (A-10)
# ===========================================================================

def _ibkr_stub(open_trades=()):
    client = object.__new__(ibkr_exec_module.IBKRExecutionClient)
    client.manager = MagicMock()
    client.manager.ib.openTrades.return_value = list(open_trades)
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


class TestGetOpenTradesAllSymbolsA10:

    def test_symbol_parameter_stays_required_fence(self):
        """FENCE (passes today): the symbol parameter must stay REQUIRED
        (no default) on the ABC and both adapters — 'all symbols' is an
        EXPLICIT None, never an accident."""
        for owner in (ExecutionClient,
                      ibkr_exec_module.IBKRExecutionClient,
                      SimulatedExecution):
            sig = inspect.signature(owner.get_open_trades)
            assert sig.parameters["symbol"].default is (
                inspect.Parameter.empty
            ), (
                f"{owner.__name__}.get_open_trades grew a default for "
                f"symbol — A-10 keeps it required"
            )

    def test_ibkr_none_returns_every_session_order(self):
        """A-10: symbol=None must return orders on EVERY contract symbol —
        the trade_8 class of old-contract orphans lives on a symbol the
        instance no longer trades."""
        client = _ibkr_stub([_open_trade(101, "GC"),
                             _open_trade(102, "MGC")])

        events = client.get_open_trades(None)

        got = {(e.order_id, e.symbol) for e in events}
        assert got == {("101", "GC"), ("102", "MGC")}, (
            f"get_open_trades(None) must return ALL session orders "
            f"irrespective of contract symbol; got {got} (today the "
            f"symbol filter drops everything)"
        )

    def test_ibkr_symbol_scoped_call_fence(self):
        """FENCE (passes today): the existing symbol-scoped call shape
        keeps filtering — startup recovery call sites stay valid."""
        client = _ibkr_stub([_open_trade(101, "GC"),
                             _open_trade(102, "MGC")])

        events = client.get_open_trades(symbol="GC")

        assert {(e.order_id, e.symbol) for e in events} == {("101", "GC")}

    def test_sim_honest_symbol_filter_and_none_means_all(self):
        """A-10 sim honesty (039208d A4 precedent): symbol='GC' returns
        ONLY GC resting orders (today the sim ignores the parameter);
        symbol=None returns everything."""
        sim = SimulatedExecution()
        sim.place_child_orders(symbol="GC", parent_order_id=1, action="SELL",
                               quantity=1, tp_price=68.9, sl_price=67.4)
        sim.place_child_orders(symbol="ES", parent_order_id=2, action="SELL",
                               quantity=1, tp_price=5000.0, sl_price=4900.0)

        gc_events = sim.get_open_trades("GC")
        assert {e.symbol for e in gc_events} == {"GC"}, (
            f"the sim returned foreign-symbol orders for symbol='GC': "
            f"{[(e.order_id, e.symbol) for e in gc_events]} — fabricated "
            f"scope is the A4 dishonesty class"
        )
        assert len(gc_events) == 2

        all_events = sim.get_open_trades(None)
        assert len(all_events) == 4, (
            f"symbol=None must return all resting orders; got "
            f"{len(all_events)}"
        )
        assert {e.symbol for e in all_events} == {"GC", "ES"}


# ===========================================================================
# J — Cached-position primitive (A-2 seam)
# ===========================================================================

class TestCachedPositionPrimitive:

    def test_interface_declares_get_cached_position(self):
        """A-2: the deadlock-safe position read must be a shared
        ExecutionClient primitive (get_position routes through
        ensure_connected → blocking reconnect under _ledger_lock)."""
        assert hasattr(ExecutionClient, "get_cached_position"), (
            "ExecutionClient.get_cached_position(symbol) is missing — the "
            "housekeeping sweep has no cache-read position primitive"
        )

    def test_ibkr_cached_position_never_reconnects(self):
        """A-2: the IBKR impl reads the local ib_insync portfolio/position
        cache and NEVER calls ensure_connected/connect."""
        client = _ibkr_stub()
        items = [
            SimpleNamespace(contract=SimpleNamespace(symbol="GC"),
                            position=2.0),
            SimpleNamespace(contract=SimpleNamespace(symbol="MGC"),
                            position=1.0),
        ]
        client.manager.ib.portfolio.return_value = items
        client.manager.ib.positions.return_value = items

        pos = client.get_cached_position("GC")

        assert pos == 2
        assert client.get_cached_position("ZZ") == 0, (
            "an unheld symbol must read as flat (0), never raise"
        )
        assert not client.manager.ensure_connected.called, (
            "get_cached_position routed through ensure_connected — the "
            "exact re-entrant deadlock A-2 forbids"
        )
        assert not client.manager.connect.called

    def test_sim_cached_position_is_honest(self):
        """A4-style honesty: the sim's cached position is its true
        position — 0 when fresh, the real net position after an entry."""
        sim = SimulatedExecution()
        assert sim.get_cached_position("GC") == 0
        sim.place_bracket_order(symbol="GC", action="BUY", quantity=2)
        assert sim.get_cached_position("GC") == 2, (
            "the sim must report its true net position from the cache "
            "read, never a fabricated flat"
        )


# ===========================================================================
# K — TelemetryDB: repair_closed_position / get_recent_closed_positions (A-9)
# ===========================================================================

def _fleet_bots(tmp_path):
    db = tmp_path / "fleet_telemetry.db"
    bot_a = TelemetryDB(str(db), symbol="GC", client_id=4000)
    bot_b = TelemetryDB(str(db), symbol="NG", client_id=4001)
    return db, bot_a, bot_b


def _seed_closed(bot, trade_id, *, reason, exit_price, close_time=None,
                 entry_order_id=200):
    now = datetime.utcnow()
    bot.open_position(
        trade_id=trade_id, side="SHORT", quantity=1, entry_price=68.90,
        entry_order_id=entry_order_id,
        entry_time=(now - timedelta(hours=6)).isoformat(),
    )
    bot.update_position_brackets(trade_id, tp_order_id=201, sl_order_id=202,
                                 tp_price=67.90, sl_price=69.90)
    bot.close_position(
        trade_id, reason=reason,
        close_time=(close_time or (now - timedelta(hours=1)).isoformat()),
        exit_price=exit_price,
    )


def _raw_rows(db_path, trade_id):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT client_id, status, close_reason, exit_price "
        "FROM active_positions WHERE trade_id = ? ORDER BY client_id",
        (trade_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


class TestTelemetryRepairA9:

    def test_repair_fills_null_price_and_returns_rowcount(self, tmp_path):
        """A-9: a NULL-price CLOSED_OOB_UNRECOVERED row (the trade_8 class)
        is repaired with the proven fill; rowcount 1 returned."""
        db, bot_a, _ = _fleet_bots(tmp_path)
        _seed_closed(bot_a, "trade_8", reason="CLOSED_OOB_UNRECOVERED",
                     exit_price=None)

        count = bot_a.repair_closed_position(
            "trade_8", exit_price=67.90, reason="TP_HIT_OOB",
            allow_overwrite_reasons=("CLOSED_OOB", "CLOSED_OOB_UNRECOVERED"),
        )

        assert count == 1, f"expected rowcount 1, got {count!r}"
        rows = _raw_rows(db, "trade_8")
        assert rows[0]["exit_price"] == pytest.approx(67.90)
        assert rows[0]["close_reason"] == "TP_HIT_OOB"
        assert rows[0]["status"] == "CLOSED"

    def test_repair_overwrites_synthetic_price_only_under_whitelist(
            self, tmp_path):
        """A-1(b) at the DB layer: a synthetic-price CLOSED_OOB row IS
        overwritable; a TP_HIT row with a real price is NOT (rowcount 0,
        row byte-identical)."""
        db, bot_a, _ = _fleet_bots(tmp_path)
        _seed_closed(bot_a, "trade_11", reason="CLOSED_OOB",
                     exit_price=68.05)   # synthetic bar price
        _seed_closed(bot_a, "trade_10", reason="TP_HIT", exit_price=67.10)

        whitelist = ("CLOSED_OOB", "CLOSED_OOB_UNRECOVERED")
        assert bot_a.repair_closed_position(
            "trade_11", exit_price=67.90, reason="TP_HIT_OOB",
            allow_overwrite_reasons=whitelist,
        ) == 1
        assert _raw_rows(db, "trade_11")[0]["exit_price"] == (
            pytest.approx(67.90)
        )

        assert bot_a.repair_closed_position(
            "trade_10", exit_price=67.15, reason="TP_HIT_OOB",
            allow_overwrite_reasons=whitelist,
        ) == 0, "a non-whitelisted reason with a real price must not update"
        row = _raw_rows(db, "trade_10")[0]
        assert row["exit_price"] == pytest.approx(67.10)
        assert row["close_reason"] == "TP_HIT"

    def test_repair_scoped_to_client_and_closed_status(self, tmp_path):
        """54dc110 collision precedent: bot B's identical trade_id must be
        untouched by bot A's repair; an OPEN row is never repaired."""
        db, bot_a, bot_b = _fleet_bots(tmp_path)
        _seed_closed(bot_a, "trade_8", reason="CLOSED_OOB", exit_price=None)
        _seed_closed(bot_b, "trade_8", reason="SL_HIT", exit_price=3.10)
        now = datetime.utcnow()
        bot_a.open_position(
            trade_id="trade_12", side="LONG", quantity=1, entry_price=68.0,
            entry_order_id=300,
            entry_time=(now - timedelta(hours=1)).isoformat(),
        )

        count = bot_a.repair_closed_position(
            "trade_8", exit_price=67.90, reason="TP_HIT_OOB",
            allow_overwrite_reasons=("CLOSED_OOB", "CLOSED_OOB_UNRECOVERED"),
        )
        assert count == 1

        rows = _raw_rows(db, "trade_8")
        by_cid = {r["client_id"]: r for r in rows}
        assert by_cid[4000]["exit_price"] == pytest.approx(67.90)
        assert by_cid[4001]["exit_price"] == pytest.approx(3.10), (
            "bot A's repair leaked into bot B's identical trade_id — the "
            "54dc110 fleet-collision class"
        )
        assert by_cid[4001]["close_reason"] == "SL_HIT"

        assert bot_a.repair_closed_position(
            "trade_12", exit_price=1.0, reason="TP_HIT_OOB",
            allow_overwrite_reasons=("CLOSED_OOB",),
        ) == 0, "an OPEN row must never be 'repaired' closed"
        assert _raw_rows(db, "trade_12")[0]["status"] == "OPEN"

    def test_get_recent_closed_positions_window_and_scope(self, tmp_path):
        """A-9: get_recent_closed_positions defaults to a 48h close_time
        window, returns only THIS client's CLOSED rows, and widens with an
        explicit hours=."""
        sig = inspect.signature(TelemetryDB.get_recent_closed_positions)
        assert sig.parameters["hours"].default == 48

        db, bot_a, bot_b = _fleet_bots(tmp_path)
        now = datetime.utcnow()
        _seed_closed(bot_a, "trade_recent", reason="TP_HIT", exit_price=67.9,
                     close_time=(now - timedelta(hours=1)).isoformat())
        _seed_closed(bot_a, "trade_old", reason="SL_HIT", exit_price=69.9,
                     close_time=(now - timedelta(hours=100)).isoformat())
        bot_a.open_position(
            trade_id="trade_live", side="LONG", quantity=1, entry_price=68.0,
            entry_order_id=301,
            entry_time=(now - timedelta(hours=1)).isoformat(),
        )
        _seed_closed(bot_b, "trade_b", reason="TP_HIT", exit_price=3.2,
                     close_time=(now - timedelta(hours=1)).isoformat())

        recent = bot_a.get_recent_closed_positions()
        ids = {r["trade_id"] for r in recent}
        assert ids == {"trade_recent"}, (
            f"default 48h window must return exactly this client's recent "
            f"CLOSED rows; got {ids} (trade_old is 100h stale, trade_live "
            f"is OPEN, trade_b belongs to the fleet-mate)"
        )
        for key in ("trade_id", "close_reason", "exit_price",
                    "tp_order_id", "sl_order_id"):
            assert key in recent[0], (
                f"row dicts must carry {key!r} for the sweep's matching"
            )

        wide = {r["trade_id"]
                for r in bot_a.get_recent_closed_positions(hours=200)}
        assert wide == {"trade_recent", "trade_old"}


# ---------------------------------------------------------------------------
# Protective-leg verify + heal (ticket unprotected-leg-verification_07082026_0315)
# The sweep now AUTO-HEALS a genuinely-missing tracked leg (operator-authorized
# 2026-07-08); healthy positions are never churned; fail-closed guards defer.
# ---------------------------------------------------------------------------

class _HealExecClient(_StrictExecClient):
    """Read-only sweep primitives PLUS the operator-authorized heal's placement
    seams (place_child_orders / cancel_open_orders)."""

    def __init__(self, *, placed_ids=(50, 51), **kw):
        super().__init__(**kw)
        self._placed_ids = tuple(placed_ids)
        self.place_calls = []

    def cancel_open_orders(self, symbol):
        self.touches.append("cancel_open_orders")
        return 0

    def place_child_orders(self, *, symbol, parent_order_id, action,
                           quantity, tp_price, sl_price):
        self.touches.append("place_child_orders")
        self.place_calls.append(
            {"action": action, "quantity": quantity,
             "tp_price": tp_price, "sl_price": sl_price})
        return [SimpleNamespace(order=SimpleNamespace(orderId=oid))
                for oid in self._placed_ids]


def _heal_stub(*, resting, position=1, connected=True, halted=False,
               placed_ids=(50, 51), sl_price=69.90, tp_price=67.90):
    """Housekeeping stub whose exec client permits the heal placement."""
    row = _ledger_open_row(
        trade_id="trade_9", side="LONG", sl_order_id=202, tp_order_id=201,
        sl_price=sl_price, tp_price=tp_price,
    )
    lt = _housekeeping_stub(
        exec_client=_HealExecClient(position=position, resting=resting,
                                    connected=connected, placed_ids=placed_ids),
        open_position=row, active_trade_id="trade_9", halted=halted,
    )
    lt._front_month_local_symbol = "GCQ6"  # non-None → heal may re-place
    return lt


class TestSweepProtectiveLegHeal:
    def test_both_legs_resting_no_heal(self):
        lt = _heal_stub(resting=[_resting_evt(201, order_type="LMT"),
                                 _resting_evt(202, order_type="STP")])
        _run_sweep(lt)
        assert lt.exec_client.place_calls == [], "healthy position must not churn"
        assert "housekeeping-protective-leg-healed" not in _kinds(lt)

    def test_missing_sl_is_healed(self):
        # SL (202) not resting, TP (201) resting → heal re-places the pair.
        lt = _heal_stub(resting=[_resting_evt(201, order_type="LMT")])
        _run_sweep(lt)
        assert len(lt.exec_client.place_calls) == 1, "missing leg must be re-placed"
        assert "housekeeping-protective-leg-healed" in _kinds(lt)
        assert lt._sl_order_id == 51  # registered the new SL id

    def test_empty_broker_cache_defers_heal(self):
        # Position held but broker returns ZERO orders → possible stale cache.
        lt = _heal_stub(resting=[])
        _run_sweep(lt)
        assert lt.exec_client.place_calls == [], "must not churn on empty cache"
        assert "housekeeping-naked-position" in _kinds(lt)

    def test_rate_limit_halt_skips_sweep_no_placement(self):
        # The outer _run_hourly_housekeeping gate skips the whole sweep while
        # halted → fail-closed: nothing is placed.
        lt = _heal_stub(resting=[_resting_evt(201, order_type="LMT")], halted=True)
        _run_sweep(lt)
        assert lt.exec_client.place_calls == [], "must not place while halted"
        assert "housekeeping-protective-leg-healed" not in _kinds(lt)

    def test_disconnect_skips_sweep_no_placement(self):
        # is_connected() gate (outer + per-touch) abandons the sweep → nothing
        # is placed.
        lt = _heal_stub(resting=[_resting_evt(201, order_type="LMT")],
                        connected=False)
        _run_sweep(lt)
        assert lt.exec_client.place_calls == [], "must not place while disconnected"
        assert "housekeeping-protective-leg-healed" not in _kinds(lt)

    def test_cannot_heal_without_ledger_price_is_loud(self):
        # Leg missing but the ledger has no sl_price → refuse to fabricate.
        lt = _heal_stub(resting=[_resting_evt(201, order_type="LMT")],
                        sl_price=None)
        _run_sweep(lt)
        assert lt.exec_client.place_calls == [], "never fabricate a price"
        assert "housekeeping-naked-position" in _kinds(lt)
