# TDD Status — t3-tick-order-pricing_07042026_1954

## PHASE: Red
**Agent:** TDD-Tester | **Date:** 2026-07-04 | **Baseline:** branch `development`, HEAD `f02ec5e` (no worktree)

## Deliverable
`tests/test_tick_order_pricing.py` — 76 tests, Strict-Lock FINALIZED. Ghost-imports
`round_to_tick` + `_tick_grid` from `src.core.instrument_master` (audit §3.1 helper,
verbatim). Covers the audit §6 18-item list, blueprint test requirements, and
impact_review REC-1/REC-2; R1/R2 pins included per manager ruling.

### Class map
- **TestRoundToTickCLParity** (6) — adversarial x.xx5/negative/-0.0/large bitwise pins
  vs `round(x, 2)`; seeded 100k uniform(±200) + full half-cent lattice, zero bitwise
  deviations; GC 0.10 ≡ `round(x,1)` and NG 0.001 ≡ `round(x,3)` sweeps; REC-1
  composition pin `round_to_tick(x ± 2*0.01, 0.01)` ≡ `round(x ± 0.02, 2)` (50k+ both sides).
- **TestRoundToTickGeneralGrids** (18) — ES/ZC 0.25, GC 0.10, SI 0.005, HG 0.0005 spot
  pins incl. nearest semantics (6000.12→6000.00, 6000.13→6000.25) and half-even at exact
  .5 quotients; on-grid via tolerance-free exact-Decimal remainder; idempotence + on-grid
  sweeps over every registry tick; `_tick_grid` classification table.
- **TestRoundToTickErrors** (10) — NaN/±inf price, tick ∈ {0, <0, -0.0, NaN, inf}, None
  price/tick all raise. No silent defaults.
- **TestMarketableLimitTickAware** (14) — S4/S5/S2 marketable-limit: ES buffer 2*0.25 &
  0.25-grid (incl. off-grid base → nearest); CL bit-identical to legacy
  `round(x ± 0.02, 2)` incl. off-grid 72.505 and a seeded composition sweep through the
  REAL `place_entry_order` (160 calls); GC/NG entry spots; **R1** adaptive-exit pins
  (CL on-grid identity, CL/ES off-grid snap); unknown symbol raises w/ no order;
  `_CL_TICK_SIZE` gone (class + module).
- **TestExitExchangeFromRegistry** (13) — X1/X2 table CL→NYMEX (regression pin),
  MCL→NYMEX, ES→CME, GC→COMEX, ZC→CBOT for BOTH close methods, exchange captured AT
  placeOrder time; unregistered position symbol SKIPPED (no order, no raise — blueprint
  filter-semantics ruling, governs over audit item 11's ValueError variant); mixed-book
  skip+close.
- **TestLiveTraderTickSites** (15) — `_tick_size`: full ES init → 0.25, synthetic
  exec-ES/brain-CL ctx → 0.25 (execution not brain), `__new__` seam CL fallback → 0.01,
  unknown → ValueError, nothing set → AttributeError naming `_execution_symbol`;
  **S6** trailing ES → 0.25 grid + **REC-2** 500-sample seeded sweep through the real
  `_check_trailing_stop` bit-identical to legacy `round(entry+offset, 2)`; **S7** ES
  naked-stop scenario (fill 6012.50, offsets 3.47/2.31 → TP 6016.00 / SL 6010.25),
  tiered lot-arithmetic pin ([(2,6013.75),(2,6015.25)]), SELL mirror, GC tiered on 0.10,
  CL bit-identical incl. half-cent fill 65.005; **R2** recovery re-place CL identity +
  ES off-grid ledger row (6016.03, 6010.19) → (6016.00, 6010.25).

### Documented deviations
1. R1 pins live in the ibkr_client-fixture class (the R1 site is
   `close_cl_position`'s adaptive branch), not under TestLiveTraderTickSites.
2. Unregistered-symbol close = SKIP (blueprint governs) — no ValueError variant tested.
3. Q3 hasattr/modify_order guard untouched and unasserted (separate ticket
   `live-trailing-modify-order-dead_07042026_2012`).
4. `tests/test_bracket_order.py` NOT modified (Coder owns the 4 mechanical
   `contract.symbol = "CL"` additions).

## Red proof
`conda run -n trader python -m pytest tests/test_tick_order_pricing.py -v --tb=short --continue-on-collection-errors`
```
tests\test_tick_order_pricing.py:118: in <module>
    from src.core.instrument_master import (  # noqa: E402
E   ImportError: cannot import name '_tick_grid' from 'src.core.instrument_master'
    (C:\Users\bwang\Documents\GitHub\CL_Analyst_Development\src\core\instrument_master.py)
=========================== short test summary info ===========================
ERROR tests/test_tick_order_pricing.py
========================= 1 warning, 1 error in 2.37s =========================
```
Missing-implementation ghost-import failure ONLY — no syntax errors.

### Suite-quality validation (scratchpad-only, repo untouched)
The audit §3.1 helper was injected at runtime via a scratchpad pytest plugin
(`inject_t3.py`, NOT in the repo) to validate the tests themselves:
**48 passed / 28 failed** — every pass is a pure-helper or CL-regression pin
(bit-identity vs today's code confirmed, incl. both 100k+ sweeps and the trailing
sweep); every failure is a desired-behavior Red (ES/GC/ZC/NG grid mismatches, NYMEX
injection, `_CL_TICK_SIZE` present, `_tick_size` missing, R1/R2 off-grid
pass-throughs). Zero fixture crashes, zero test bugs.

## Regression check
`tests/test_instrument_context.py tests/test_instrument_master_live_fields.py
tests/test_symbol_data_paths.py tests/test_build_future_contract.py` → **198 passed** (T1/T2 green).

## Next
Coder implements audit §3 (helper VERBATIM — power-of-ten fast path is mandatory),
S1-S7 + X1/X2 + R1/R2, plus the 4 mechanical fixture additions in
tests/test_bracket_order.py. Post-green: manager-run HS14B ledger parity gate
(`setup --disable-trailing`, 2200/336) must print PARITY: PASS before commit.

---

## PHASE: Green
**Agent:** TDD-Coder | **Date:** 2026-07-04 | **Baseline:** branch `development`, HEAD `f02ec5e` (no worktree, uncommitted per manager instruction)

## Implementation (per blueprint Target Files / audit §3)
1. `src/core/instrument_master.py` — APPEND-ONLY: audit §3.1 `_tick_grid` +
   `round_to_tick` transcribed VERBATIM (power-of-ten ticks route through
   `round(price, n)`; general branch uses the quotient formulation; raises on
   non-finite price / invalid tick). Module stays a pure stdlib leaf (new
   imports `math`/`Decimal`/`lru_cache` appended with the block; no existing
   line touched).
2. `src/live_execution/ibkr_client.py` —
   - S1: `_CL_TICK_SIZE` class attr DELETED (all 4 uses replaced).
   - S4/S5 (`place_bracket_order` / `place_entry_order` marketable-limit
     branches): `tick = get_instrument(contract.symbol).tick_size`,
     `buf = 2 * tick`, snap via `round_to_tick` — resolved INSIDE the ml
     branch only (adaptive/market entries unchanged; unknown symbol raises
     before any placeOrder). ZERO signature changes.
   - S2 + X1/X2 (`close_cl_position`, `close_cl_position_market`): NYMEX
     injection replaced by `inst = get_instrument(pos.contract.symbol);
     pos.contract.exchange = inst.exchange` — resolved AFTER the pre-existing
     `pos.contract.symbol != symbol` filter, so unregistered position symbols
     are SKIPPED exactly as today (blueprint filter-semantics ruling). ml
     branch uses `2 * inst.tick_size` + `round_to_tick`.
   - R1: adaptive-exit branch snaps its limit via
     `round_to_tick(current_price, inst.tick_size)` (identity on-grid).
3. `src/live_execution/live_trader.py` —
   - New `_tick_size` property mirroring the T2 `_brain_symbol` seam pattern:
     `_instrument_context.execution_instrument.tick_size` (execution, NOT
     brain) with `__new__`-seam structural fallback via
     `get_instrument(self._execution_symbol).tick_size`; missing seam raises
     AttributeError, unknown symbol raises ValueError — no silent default.
   - S6 (~:1093): trailing `round(new_sl, 2)` → `round_to_tick(new_sl,
     self._tick_size)`.
   - S7 (six child-price sites in `_place_bracket_children_on_fill`): all →
     `round_to_tick(…, tick)` with `tick = self._tick_size` hoisted once; lot
     arithmetic (`int(round(lots*pct))`) untouched.
   - R2: `_recover_inherited_position` step-5 re-place snaps ledger
     `tp_price`/`sl_price` via `round_to_tick` before `place_child_orders`.
   - Top-level `from src.core.instrument_master import round_to_tick` (leaf,
     no cycle).
4. `tests/test_bracket_order.py` — mechanical churn ONLY: the 4
   `TestMarketableLimitOrder` tests build `contract = MagicMock();
   contract.symbol = "CL"`. Nothing else changed.

## Scope guards honored
Q3 `hasattr(exec_client, "modify_order")` guard untouched; no backtest_engine /
macro / watchdog / rollover / generator / fleet_runner / configurable_strategy
changes; no entry-price snapping for adaptive/market entries (deferred, §3.4).

## Green proof
1. `conda run -n trader python -m pytest tests/test_tick_order_pricing.py -v --tb=short`
   → **76 passed** in 2.67s.
2. `conda run -n trader python -m pytest tests/test_bracket_order.py
   tests/test_instrument_context.py tests/test_instrument_master_live_fields.py
   tests/test_symbol_data_paths.py tests/test_build_future_contract.py -q`
   → **220 passed** in 3.03s.
3. `conda run -n trader python -m pytest tests/ -q --tb=short -m "not slow"`
   → **1099 passed** (baseline 1023 + 76 new), zero failures, in 4:43.

## Deviations
None from the blueprint/audit design. Strict-Lock files untouched.

## Next (manager)
BLOCKING before commit: HS14B ledger parity gate re-run
(`setup --disable-trailing`, 2200 warmup + 336 replay) → PARITY: PASS,
15=15 trades, 15/15 exact-cent, $0.00 delta.
