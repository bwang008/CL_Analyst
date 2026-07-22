"""Read-only IBKR broker audit for the hourly fleet monitor.

Connects to IB Gateway with a dedicated unused clientId in READ-ONLY mode and
verifies that every open position has a resting protective STOP on its exact
contract — BROKER TRUTH that the DB-only ``fleet_health`` cannot provide
(``fleet_health`` only checks whether the ledger row *carries* an sl_order_id;
it never confirms that order is actually resting at the broker, so a silently
cancelled/rejected stop reads as "protected" — a false negative).

Read-only by construction: only ``reqPositions`` / ``reqAllOpenOrders`` are
issued; this module NEVER places or cancels an order, and connects with
``readonly=True`` so ib_insync itself refuses any order op. It uses a dedicated
clientId (default 626) that no fleet child uses, so it cannot collide with /
disconnect a live child. Always exits 0 (monitor contract: a broker outage must
never break the hourly run).

The Master API Client ID setting is NOT required and MUST NOT be used:
``reqAllOpenOrders`` + ``reqPositions`` return ALL clients' orders/positions
regardless (verified 2026-07-08 from a plain non-master client — saw all fleet
orders across 3001/4001/5001). Designating this id as master caused cross-client
EXECUTION leakage into the fleet children's ``get_executions``, which the hourly
ledger-repair then mis-matched by colliding order id (GC trade_27 got an MES
exit price) — so the operator's Gateway must have NO Master API Client ID set.

Operator setup (2026-07-08): IB Gateway :4002, NO Master API Client ID,
127.0.0.1 in Trusted IPs. Run: ``conda run -n trader python -m
src.live_execution.broker_audit``.
"""
from __future__ import annotations

import argparse
import sys

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 4002
_DEFAULT_CLIENT_ID = 626
_RESTING_STATUSES = ("Submitted", "PreSubmitted", "PendingSubmit")
_STOP_TYPES = ("STP", "STP LMT")


def analyze(positions: list[dict], orders: list[dict]) -> tuple[list[dict], list[tuple]]:
    """Pure broker-truth naked-leg cross-check (no I/O — unit-testable).

    A position is protected iff a resting STOP order rests on its EXACT contract
    (matched on symbol + lastTradeDateOrContractMonth, so a stop left on an old
    contract after a reconfiguration does not count as protection).

    Args:
        positions: dicts with keys ``symbol``, ``expiry``, ``position``.
        orders:    dicts with keys ``symbol``, ``expiry``, ``order_type``,
                   ``status``, ``order_id``; OPTIONAL ``oca_group`` /
                   ``oca_type`` (ticket oco-leg-race-audit_07212026_1935 —
                   reporting only, never part of naked detection).
    Returns:
        (findings, checked) — ``findings`` is the list of naked positions;
        ``checked`` is [(position, resting_stops), ...] for every non-zero
        position (for reporting). Each resting-stop entry is the bare
        order_id when the order dict lacks an ``oca_group`` key (backward
        compat), or the tuple ``(order_id, oca_group)`` when it carries one.
    """
    stops: dict = {}
    for o in orders:
        if o.get("order_type") in _STOP_TYPES and o.get("status") in _RESTING_STATUSES:
            if "oca_group" in o:
                entry = (o["order_id"], o["oca_group"])
            else:
                entry = o["order_id"]
            stops.setdefault((o["symbol"], o["expiry"]), []).append(entry)

    findings: list[dict] = []
    checked: list[tuple] = []
    for p in positions:
        if not p.get("position"):
            continue
        resting = stops.get((p["symbol"], p["expiry"]), [])
        checked.append((p, resting))
        if not resting:
            findings.append({
                "symbol": p["symbol"], "expiry": p["expiry"],
                "position": p["position"],
                "detail": (f"{p['symbol']} {p['expiry']} pos={p['position']:+g} "
                           f"has NO resting stop on the broker — NAKED"),
            })
    return findings, checked


def format_checked_line(position: dict, resting_stops: list) -> str:
    """Render the BROKER_OK report line for one checked position (pure).

    Each resting-stop entry renders ``id=<id> oca=<group>`` when it is an
    ``(order_id, oca_group)`` tuple with a truthy group, and exactly
    ``id=<id> oca=no-oca`` otherwise (legacy bare-id entry, or a tuple
    whose group is falsy — IB returns '' for an order with no group).
    No IB, no I/O, no printing — unit-testable like ``analyze``.
    """
    fragments = []
    for entry in resting_stops:
        if isinstance(entry, tuple):
            order_id, group = entry
        else:
            order_id, group = entry, None
        fragments.append(f"id={order_id} oca={group if group else 'no-oca'}")
    return (f"BROKER_OK: {position['symbol']} {position['expiry']} "
            f"pos={position['position']:+g} resting stops: "
            f"{', '.join(fragments)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only IBKR naked-leg audit.")
    ap.add_argument("--host", default=_DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=_DEFAULT_PORT)
    ap.add_argument("--client-id", type=int, default=_DEFAULT_CLIENT_ID)
    ap.add_argument("--timeout", type=int, default=15)
    args = ap.parse_args(argv)

    try:
        from ib_insync import IB
    except Exception as exc:  # ib_insync absent (e.g. global python) — never crash the monitor
        print(f"BROKER_UNAVAILABLE: ib_insync import failed: {exc}")
        return 0

    ib = IB()
    positions: list[dict] = []
    orders: list[dict] = []
    try:
        try:
            ib.connect(args.host, args.port, clientId=args.client_id,
                       timeout=args.timeout, readonly=True)
        except Exception as exc:
            # Gateway down / port closed / id in use → not a fault of the fleet.
            print(f"BROKER_UNAVAILABLE: cannot connect {args.host}:{args.port} "
                  f"clientId={args.client_id}: {type(exc).__name__}: {exc}")
            return 0

        ib.reqPositions()
        ib.sleep(1.0)
        positions = [
            {"symbol": p.contract.symbol,
             "expiry": p.contract.lastTradeDateOrContractMonth,
             "position": p.position}
            for p in ib.positions() if p.position != 0
        ]

        ib.reqAllOpenOrders()
        ib.sleep(1.0)
        orders = [
            {"symbol": t.contract.symbol,
             "expiry": t.contract.lastTradeDateOrContractMonth,
             "order_id": t.order.orderId,
             "order_type": t.order.orderType,
             "status": t.orderStatus.status,
             # OCA group membership (reporting only — broker-truth that the
             # protective legs share a bracket group; '' / 0 mean no group).
             "oca_group": (getattr(t.order, "ocaGroup", "") or None),
             "oca_type": (getattr(t.order, "ocaType", 0) or None)}
            for t in ib.openTrades()
        ]
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    findings, checked = analyze(positions, orders)
    for p, resting in checked:
        if resting:
            print(format_checked_line(p, resting))
    for f in findings:
        print(f"BROKER_EVENT: naked-position | {f['symbol']}/{f['expiry']} | {f['detail']}")
    print(f"BROKER_SUMMARY: {len(checked)} open position(s), {len(findings)} naked "
          f"(broker-truth: positions vs resting stop orders)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
