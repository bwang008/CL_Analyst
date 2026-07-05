# Ticket Resolution Blueprint — live-trailing-modify-order-dead_07042026_2012
**Ticket Directory:** `.agents/collab/tickets/live-trailing-modify-order-dead_07042026_2012/`

## Bug Summary
Live trailing-stop SL modifications are silently never transmitted to IBKR. Regression
timeline (reviewer-verified): worked as direct `ib.placeOrder` since 8fe9fd1 (2026-03-09);
61bb864 (06-13 modularization) replaced it with `if hasattr(self.exec_client,
"modify_order")` while creating IBKRExecutionClient WITHOUT the method; db32561 (06-17)
added modify_order to the SIMULATED adapter only, masking the break in every parity run.
Lie surface on trigger: cached Order.auxPrice mutated, "modified SL order" logged,
`_trailing_activated` latched (never retries), `_tracked_sl_price` set, phantom SL
persisted via telemetry.update_position_sl + TRAILING_ACTIVATED snapshot — zero broker
interaction. Full detail: `audit.md`; verification + conditions C-1..C-3:
`impact_review.md`. Reviewer verdict: APPROVE (no human authorization; Q2 interface
change manager-ACKed). Q1 (restart live HS14B + phantom-row back-annotation) is a
separate USER decision — not in this ticket.

## Target Files
- `src/live_execution/interfaces/execution_interface.py` — `modify_order(order_id,
  event=None)` becomes `@abstractmethod`; C-2: docstring states SYNC-TRANSMIT failure
  semantics precisely (must raise on transmit/validation failure; not-found may warn +
  no-op, matching live IBKR async behavior).
- `src/live_execution/adapters/ibkr_execution.py` — implement `modify_order`: validate
  event + `event.raw_event` (ib_insync Trade with .order/.contract) + order-id match +
  connection, then `ib.placeOrder(trade.contract, trade.order)` (same order object, same
  session — the ib_insync modify flow; sync, callback-safe, NO qualification calls).
  Raise loudly (ValueError/ConnectionError) on any validation failure.
- `src/live_execution/adapters/simulated_execution.py` — C-1: raise on the
  malformed-event class (missing event/raw_event/order); keep not-found as warn-level
  no-op. Semantics must match the interface docstring exactly.
- `src/live_execution/live_trader.py` (~:1120-1161) — delete the hasattr guard; direct
  call with TRANSMIT-THEN-COMMIT ordering: transmit FIRST inside a targeted try/except
  (required — the existing outer handler at ~:1168 would swallow the failure after the
  auxPrice mutation); on failure restore raw_order.auxPrice, log an ERROR that does NOT
  contain the success substring, commit NOTHING (`_trailing_activated` stays False,
  no _tracked_sl_price, no telemetry, no snapshot) → natural retry next bar; on success
  commit everything as today.

## Required Changes — test list (Tester implements; audit A/I/C series + C-3)
- A1-A5 adapter unit tests (mocked IB): placeOrder called exactly once with the Trade's
  contract+order; raises on missing event / missing raw_event / missing order or
  contract; raises on order-id mismatch; raises when disconnected; pin that NO
  qualification/reqHistoricalData-family call occurs (event-loop safety).
- I1-I2: ExecutionClient cannot be instantiated without modify_order (abstract
  enforcement — a dummy subclass missing it raises TypeError); sim and IBKR signatures
  match the interface.
- C1-C5 call-site tests (established LiveTrader seams): guard removal — an exec client
  whose modify_order raises produces the failure path (no lie: auxPrice restored,
  _trailing_activated False, no telemetry update_position_sl call, no TRAILING_ACTIVATED
  snapshot); success path commits all state exactly as today; retry: after a failed
  transmit, the next bar re-triggers and can succeed; sim-adapter integration
  (modify through the sim client mutates the resting order).
- C-3 Strict-Lock pin: the exact substring "modified SL order" is emitted ONCE on
  success only; the failure log must not contain it.
- C-1 sim tests: malformed event raises; unknown order-id warns + no-ops.

## Hard Constraints
- The `--disable-trailing` parity path is byte-identical (changed region sits entirely
  below the `if not triggered: return` early-out — reviewer-verified).
- No-silent-defaults: no hasattr/duck-typing anywhere in the fix.
- Scope guards: transmit path ONLY — no trailing binding/scheduling changes (5m-harness
  ticket owns that), no escalation-on-repeated-failure (spun-off ticket), no T4-T8 scope.
- All 28 existing MagicMock exec-client doubles stay green (auto-provide modify_order).

## Verification
- Full fast suite green (baseline 1099 + new).
- BLOCKING: HS14B ledger parity gate (`setup --disable-trailing`, 2200/336) → PARITY:
  PASS before commit (LiveTrader changed; gate must stay byte-identical).
