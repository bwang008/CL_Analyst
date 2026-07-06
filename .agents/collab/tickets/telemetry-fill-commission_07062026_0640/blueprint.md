# Ticket Resolution Blueprint — telemetry-fill-commission_07062026_0640
**Ticket Directory:** `.agents/collab/tickets/telemetry-fill-commission_07062026_0640/`

## Bug Summary
User-reported: "execution price and tracking pnl unrealized and realized
don't seem to be consistently lining up." Audit of fleet_telemetry.db +
code confirmed three defects:
1. `trade_ledger.fill_price` is NULL on every row —
   `TelemetryDB.update_fill` (telemetry.py:638) exists but is never called.
2. Commissions and per-fill realized PnL are never captured: no
   `commissionReport` handler exists anywhere; `tradebook_events.commission`
   and `.realized_pnl` are always NULL; zero COMMISSION events recorded.
   (Verified: GC trade_5 gross −$183.00 vs IBKR real_pnl −$184.94 — the
   $1.94 round-trip commission is invisible to our records.)
3. The `[PNL]` log line (live_trader.py:3352-3373) mixes three sources:
   IBKR live unrealizedPnL, IBKR avgCost (which INCLUDES commission) as
   entryPrice, and OUR last bar close as mktPrice, all formatted `%.2f`
   (half a tick on NG = $50 hidden). Lines can look sign-contradictory
   while being IBKR-internally correct.

## Target Files
- `src/live_execution/interfaces/execution_interface.py`
- `src/live_execution/adapters/ibkr_execution.py`
- `src/live_execution/adapters/simulated_execution.py`
- `src/live_execution/ibkr_client.py`
- `src/live_execution/live_trader.py`

## Required Changes

### R1 — commission bridge (interface + adapters)
- `execution_interface.py`: new frozen dataclass `StandardCommissionEvent`
  (order_id: str, exec_id: str, symbol: str, commission: float,
  realized_pnl: Optional[float], currency: str, raw_event: Any = None) and
  an ExecutionClient method `register_commission_callback(cb)` (base: no-op
  default so non-broker adapters keep working).
- `ibkr_execution.py`: maintain `self._commission_callbacks` (init in
  `__init__`), implement `register_commission_callback`, subscribe
  `self.manager.ib.commissionReportEvent += self._on_commission_report`
  in `__init__` (exec-client session only — the data client would duplicate
  events). `_on_commission_report(trade, fill, report)` builds the
  StandardCommissionEvent (order_id from trade.order.orderId, exec_id from
  fill.execution.execId, symbol from trade.contract.symbol, commission /
  realizedPNL / currency from report; IBKR sentinel realizedPNL values of
  1.7976931348623157e+308 map to None) and dispatches to callbacks; a
  callback exception must be caught and logged, never propagated into
  ib_insync's event loop.
- `simulated_execution.py`: `register_commission_callback` accepted (no-op
  storage is fine).

### R2 — LiveTrader wiring (live_trader.py)
- Where order-status callbacks are registered, also
  `exec_client.register_commission_callback(self._on_commission_event)`
  (guard with hasattr for adapters predating the interface method).
- New `_on_commission_event(evt)`: writes a tradebook row via
  `self.telemetry.log_tradebook_event(event_id=f"COMMISSION_{evt.exec_id}",
  event_type="COMMISSION", event_timestamp_utc=<utc iso now>,
  order_id=evt.order_id, broker_execution_id=evt.exec_id,
  symbol=evt.symbol, commission=evt.commission,
  realized_pnl=evt.realized_pnl)`. Deterministic event_id makes the
  INSERT OR IGNORE dedupe across the two IB sessions / restarts. Wrap in
  try/except + log (telemetry failure never affects trading).
- `_on_standard_execution_event` Filled branch: pass
  `avg_fill_price=avg_price` to the existing EXECUTION_FILL
  log_tradebook_event call (currently only last_fill_price).
- Entry-fill branch (after open_position): call
  `self.telemetry.update_fill(<int order_id when castable, else raw>,
  avg_price)` in its own try/except — this finally stamps
  trade_ledger.fill_price.

### R3 — [PNL] single-source display (ibkr_client.py + live_trader.py)
- `get_account_summary` additionally returns
  `"cl_market_price": float(item.marketPrice)` (default 0.0; docstring
  updated). `simulated_execution.get_account_summary` returns the key too.
- New pure module function in live_trader.py:
  `_price_decimals(tick_size) -> int` (decimals needed to render one tick:
  0.001→3, 0.01→2, 0.1→1, 0.25→2, 1→0; max(0, -Decimal(str(tick)).exponent)).
- The `[PNL]` block: `entry_price` prefers `self._entry_price` (our actual
  fill) and falls back to avgCost/multiplier ONLY when `_entry_price` is
  None (recovery edge); `mkt` prefers `cl_market_price` when > 0 (IBKR live
  mark) and falls back to the bar close; the line states the mkt source
  (`mkt=<x> (IBKR)` vs `mkt=<x> (bar)`); entry/mkt/TP/SL formatted with
  `_price_decimals(self._tick_size)` instead of hardcoded `%.2f`.
  unrealizedPnL stays IBKR's number (authoritative).

## Test Contract (Strict-Locked)
`tests/test_commission_capture.py` — names `StandardCommissionEvent`,
`register_commission_callback`, `_on_commission_report`,
`_on_commission_event`, `_price_decimals`, `cl_market_price` are contract.
