# TDD Result — oco-leg-race-audit_07212026_1935 (Stage 1)

**Scope:** Blueprint Stage 1 ONLY (human-authorized 2026-07-21 "Proceed"). Stages 2/3/4 remain separate, unauthorized tickets. Stage 2 is the committed immediate follow-up per operator decision.

## Outcome: GREEN
- RED phase: full fast suite `18 failed, 2458 passed, 1 skipped` — all 18 failures were the new OCA tests; zero pre-existing breakage.
- GREEN phase: full fast suite `2476 passed, 1 skipped, 0 failed` (317.9s). Ticket file `tests/test_oca_protective_legs.py`: 26/26 (18 feature tests + 8 permanent pins incl. the Error-201 parentId red sentinel).

## Files changed
- `src/live_execution/ibkr_client.py` — `place_child_orders`: per-bracket `ocaGroup` (`OCA-{localSymbol}-{client_id}-{uuid12}`, never derived from parent_order_id) + `ocaType=2` on SL and every TP (scalar and tiered-list variants); docstring updated (Error-201 history kept verbatim; stale `_on_order_status` reference fixed to `_on_standard_execution_event`; software cancel documented as idempotent belt-and-braces; ocaType=3 canary fallback noted).
- `src/live_execution/live_trader.py` — startup gate in `__init__` after `_max_position_size`: `exit_mode=TIERED` + `max_position_size>1` raises RuntimeError naming this ticket (multi-rung OCA semantics ill-defined + first-rung-fill booked as full close; 1-lot TIERED unaffected — all deployed configs are 1-lot).
- `src/live_execution/adapters/simulated_execution.py` — `_RestingOrder.oca_group` (optional, default None); sim `place_child_orders` stamps one per-call `SIM-OCA-*` tag on records and mock trades (`ocaGroup`/`ocaType=2`); matcher semantics untouched (already models atomic OCA).
- `src/live_execution/broker_audit.py` — read-only reporting: order harvest gains `oca_group`/`oca_type`; `analyze()` emits `(order_id, oca_group)` tuples when present (bare ids otherwise — backward compat); new pure `format_checked_line()` renders `id=<id> oca=<group|no-oca>`; naked detection unchanged.
- `tests/test_oca_protective_legs.py` — NEW (TDD-Tester, Strict-Lock).

## DEPLOY: NOT DEPLOYED — OPERATOR-GATED
Per blueprint + operator reaffirmation: Stage 1 ships ONLY after its own paper-trading canary, as its own operator-gated deploy. It must NOT ride the already-pending 291a9fd/394fa68 restart. Canary checklist: blueprint section 5 item 10 (esp. overnight/off-hours stop states under ocaType=2 with-block; fallback = ocaType=3).
