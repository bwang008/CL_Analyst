# Ticket Resolution Blueprint — t3-tick-order-pricing_07042026_1954
**Ticket Directory:** `.agents/collab/tickets/t3-tick-order-pricing_07042026_1954/`

## Requirement Summary
T3 of the multi-symbol live-gaps program: order prices are computed on a hardcoded CL
cent grid (`_CL_TICK_SIZE = 0.01`, scattered `round(price, 2)`), producing invalid
increments (IBKR Error 110) for ES/GC/ZC/etc — a rejected TP/SL child after a filled
entry is a NAKED POSITION. Also `close_cl_position*` inject `exchange="NYMEX"` at exit
(stuck positions on CME/CBOT/COMEX). Reviewer verdict: APPROVE (no human authorization
needed). Full design: `audit.md` (§3 design, §5 site census S1–S7, 18-item test list);
verification: `impact_review.md`. This document governs on conflict.

## CRITICAL implementation constraint (the float-precision finding)
Naive `round(price/tick)*tick` mismatches legacy `round(price, 2)` BITWISE on ~1.4–1.9%
of inputs (half-cent cases, -0.0 sign flips). `round_to_tick` MUST route power-of-ten
ticks (0.01, 0.10, 0.001, and generally 10^-n) through `round(price, n)` directly —
CL byte-identity by construction. General branch (0.25, 0.005, 0.0005) uses the
quotient formulation (verified grid-valid + idempotent). Use the audit §3.1 helper
verbatim — both the auditor and reviewer validated that exact transcription.

## Target Files
- `src/core/instrument_master.py` — APPEND-ONLY: `round_to_tick(price, tick_size)`
  (+ cached `_tick_grid` internals per audit §3.1). Raises on non-finite price or
  invalid/non-positive tick. Stays a pure stdlib leaf.
- `src/live_execution/ibkr_client.py` — delete `_CL_TICK_SIZE` (verified zero external
  consumers); marketable-limit branches in `place_bracket_order`, `place_entry_order`,
  `close_cl_position` resolve `tick = get_instrument(<contract|pos.contract>.symbol).tick_size`
  internally (ZERO signature changes), buffer = `2*tick`, snap via `round_to_tick`;
  NYMEX injection in `close_cl_position` AND `close_cl_position_market` replaced with
  `pos.contract.exchange = get_instrument(pos.contract.symbol).exchange`.
- `src/live_execution/live_trader.py` — `_tick_size` property from InstrumentContext
  (execution instrument; `__new__`-seam fallback mirroring T2's `_brain_symbol`, raises
  when unresolvable); trailing SL site (S6, ~:1093) and the six child-price sites
  (S7, ~:1655-1679) snap via `round_to_tick`; R1 adaptive-exit snap; R2 recovery
  re-place snap (both identity for legitimate inputs).
- `tests/test_bracket_order.py` — mechanical churn ONLY: 4 marketable-limit tests gain
  `contract.symbol = "CL"`.

## Rounding semantics
Nearest / half-even everywhere (the only rule satisfying CL byte-identity). No
side-aware floor/ceil. Entry-price snapping for adaptive/market entries stays DEFERRED
(fail-safe: rejected entry = no position). configurable_strategy signal-level cent
rounding NOT migrated (upstream offsets; final prices snap at child sites).

## Test requirements (audit 18-item list + reviewer RECs, binding)
- CL bitwise regression pins: adversarial x.xx5 / negative / large values + seeded
  100k sweep — `round_to_tick(x, 0.01) == round(x, 2)` exactly.
- REC-1: composition pin — seeded sweep `round_to_tick(x ± 2*0.01, 0.01)` bit-equal to
  legacy `round(x ± 0.02, 2)` (closes gate-invisibility of the marketable-limit sites).
- REC-2: trailing site (S6) CL branch pinned via seeded sweep (S6 is NOT covered by the
  parity gate, which runs --disable-trailing).
- ES/GC/ZC/NG/SI grid validity + idempotence; ES naked-stop scenario: after a filled
  ES entry, TP and SL child prices are tick-valid (0.25 grid).
- Exit-exchange table: CL→NYMEX, MCL→NYMEX, ES→CME, GC→COMEX, ZC→CBOT via
  pos.contract.symbol; unregistered position symbols are SKIPPED (same as today —
  filter semantics unchanged).
- No-silent-default raises: non-finite price, invalid tick, unknown symbol.
- R1/R2 pins (#7/#18 in audit list).

## Scope guards
NO backtest_engine changes; NO macro (T4); NO watchdog/rollover (T5); NO generator (T6);
NO fleet_runner; the Q3 modify_order bug belongs to
`live-trailing-modify-order-dead_07042026_2012` — do NOT fix it here (do not remove the
hasattr guard in this ticket).

## Verification
- Full fast suite green (baseline 1023 + new).
- BLOCKING: HS14B ledger parity gate re-run (`setup --disable-trailing`, 2200/336) →
  PARITY: PASS before commit. Documented accepted consequence: non-CL live-vs-backtest
  TP/SL may diverge ≤½ tick (backtest stays cent-grid; out of scope here).
