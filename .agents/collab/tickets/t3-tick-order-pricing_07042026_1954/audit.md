# Audit — t3-tick-order-pricing_07042026_1954
**T3 — Tick-size order pricing (kills rejected/naked-stop orders) + `close_cl_position*` NYMEX exit injection (B5 remainder routed from T2 §7 Q2b/§8).**
Auditor pass 2026-07-04. Baseline: branch `development`, HEAD `f02ec5e` (T1 + T2 merged). Read-only — no source modified.

## 0. Executive summary

Order prices in the live path are quantized with hardcoded CL assumptions: `_CL_TICK_SIZE = 0.01` drives the marketable-limit buffer (2 CL ticks = $0.02) in three `ibkr_client.py` methods, and `round(price, 2)` cent-rounding is applied to every TP/SL/trailing price in `live_trader.py`. For any instrument whose tick is not a divisor-compatible cent grid (ES/NQ/ZC/ZS 0.25, GC 0.10), the resulting prices are invalid increments → IBKR **Error 110** rejection. The lethal case: the entry (Phase 1) fills, then the TP/SL children (Phase 2, `_place_bracket_children_on_fill`) are rejected → **naked position with no stop**. Independently, `close_cl_position` / `close_cl_position_market` inject `pos.contract.exchange = "NYMEX"` at exit time → time-barrier/rollover/kill-switch exits are submitted on a mis-exchanged contract for CME/CBOT/COMEX symbols → **rejected exit, stuck position**.

Proposed fix: one pure helper `round_to_tick(price, tick_size)` in `src/core/instrument_master.py` with a **power-of-ten fast path that is literally `round(price, n)`** (CL byte-identity by construction — see §3 for why this is mandatory, not optional), tick resolved from the registry via the contract/position symbol already in hand (zero public signature changes), and exit-exchange injection replaced by `get_instrument(pos.contract.symbol).exchange`.

**Severity: HIGH** (MEDIUM/HIGH bucket per workflow — multi-line structural change on the live order path across 2 source files + 1 new helper). **Regression: NO** — pre-existing CL-only design (present since the original live-infra commit `587cef7`); T1/T2 deliberately left these sites untouched (T2 audit §5.7, §7 Q2b).

---

## 1. Price-quantization site census (deliverable 1)

Every `round(` in `ibkr_client.py`, `live_trader.py`, `ibkr_execution.py`, `adapters/`, `strategies/` was enumerated at HEAD `f02ec5e` and classified.

### 1a. ORDER-PRICE sites — T3 scope (prices transmitted to IBKR)

| # | File:Line | Code today | Role |
|---|-----------|------------|------|
| S1 | `src/live_execution/ibkr_client.py:984` | `_CL_TICK_SIZE = 0.01` class attr | tick constant (no consumers outside this file — verified repo-wide) |
| S2 | `ibkr_client.py:551-555` | `tick2 = 2*self._CL_TICK_SIZE; lmt_price = round(current_price ± tick2, 2)` | `close_cl_position` marketable-limit **exit** (time-barrier exit path via adapter `close_position`) |
| S3 | `ibkr_client.py:564` | `LimitOrder(action, qty, current_price)` (unrounded) | `close_cl_position` **adaptive exit** limit price |
| S4 | `ibkr_client.py:1133-1152` | `tick2 = 2*self._CL_TICK_SIZE; ml_price = round(limit_price ± tick2, 2)` | `place_bracket_order` marketable-limit entry |
| S5 | `ibkr_client.py:1227-1237` | same formula | `place_entry_order` marketable-limit entry (the live two-phase Phase-1 path) |
| S6 | `src/live_execution/live_trader.py:1093` | `new_sl = round(new_sl, 2)` | trailing-stop SL price (writes `raw_order.auxPrice`) |
| S7 | `live_trader.py:1655,1663,1666,1668,1676,1679` | `round(fill_price ± offset, 2)` ×4 + tiered `round(fill_price ± off, 2)` ×2 | `_place_bracket_children_on_fill` TP list + SL — **the naked-stop site** |
| S8 | `live_trader.py:1513-1519` | ledger-stored `tp_price`/`sl_price` passed through unrounded | `_recover_inherited_position` re-places TP/SL children |

Exit-**exchange** sites (same methods, all exit modes):

| # | File:Line | Code today |
|---|-----------|------------|
| X1 | `ibkr_client.py:504` | `pos.contract.exchange = "NYMEX"` in `close_cl_position_market` |
| X2 | `ibkr_client.py:545` | `pos.contract.exchange = "NYMEX"` in `close_cl_position` |

`close_cl_position` is reached live via `adapters/ibkr_execution.py:156` (`close_position`) from: time-barrier exit (`live_trader.py:1258`), rollover force-close (`:2203`), naked-position kill switch (`:3906`), shutdown flatten (`:4035-4053` — cancel only, no close), i.e. every non-bracket exit. `close_cl_position_market` has **zero live callers** (verified repo-wide; only a test comment) but is public API and gets the identical 1-line fix.

### 1b. Signal-level rounding — upstream of the order path, NOT changed (analyzed)

- `src/live_execution/strategies/configurable_strategy.py:561-565` — `tp_price/sl_price = round(current_price ± mult*side_atr, 2)`; `:549` — tiered `offset_amount = round(mult*side_atr, 4)`. These never reach IBKR directly: `live_trader.py:3244-3245` converts them to **offsets** (`abs(signal.tp_price − current_price)`) and the actual order prices are recomputed at S7 from the fill. With S7 tick-snapped, final prices are tick-valid regardless of cent-quantized offsets (worst effect: the offset embeds ≤½-cent quantization noise, then final snap dominates). Migrating these would require threading the instrument into `ConfigurableStrategy` (constructor churn, `livetest_engine.py` + test fixtures) for zero Error-110 benefit → **out of T3**; candidate for T6 cosmetics.
- `signal.tp_price/sl_price` are otherwise consumed only by telemetry/Telegram/dry-run logging.

### 1c. Telemetry / display rounding — out of scope

`data_manager.py:660,670` (roll-ratio metadata, 6dp), `src/data/databento_data_builder.py:483` (`sample_price`, 4dp), all `%.2f` log/Telegram format strings (lossy for NG 0.001 display — cosmetic, T6/m3). `live_trader.py:1660,1673` `int(round(lots*pct))` is **lot** sizing, not a price — unchanged.

### 1d. Backtest-side — out of scope, parity implications ANALYZED (hard constraint)

`agent/backtest_engine.py:663-669` and `:793-797` round TP/SL to the cent grid from the slippage-adjusted fill — this **is** the B(a) parity semantic the live S7 mirrors, and the HS14B ledger parity gate (15/15 exact-cent, $0.00 delta — T1 C3 / T2 C7) pins it. Consequences:
1. **CL:** live must keep producing exactly `round(x, 2)` at S2/S4/S5/S6/S7. The proposed helper does (§3). The parity gate re-run after T3 is the enforcement.
2. **Non-CL:** the backtest engine will keep computing cent-grid TP/SL (e.g. ES TP 6016.03) while live snaps to the instrument grid (6016.00). Bounded, structural divergence of **≤ ½ tick per barrier** (ES: 0.125 pts = $6.25/contract; GC: 0.05 = $5; ZC: 0.125¢ = $6.25). This is an accepted consequence of the "NO backtest engine changes" scope guard — the true fix (instrument-aware backtest rounding) is a separate training-side ticket that must move the parity gate deliberately. Documented here so non-CL parity gates are specified with a ½-tick price tolerance, not exact-cent.
3. Backtest trailing (`backtest_engine.py:740-753`) applies **no** rounding while live S6 rounds — pre-existing divergence, outside the gate (gate runs `--disable-trailing`). T3 keeps CL S6 byte-identical; not our problem to reconcile.
4. The parity harness (`scripts/livetest_engine.py` + `SimulatedExecution`) drives the **real** `LiveTrader`, so S6/S7 changes are inside the gate's blast radius; `ibkr_client.py` (S2-S5) is **bypassed** by the harness → its CL byte-identity is enforced by unit pins (§6), not the gate.

---

## 2. Numeric parity analysis — the load-bearing finding (deliverable 2, "verify parity for negative/half cases")

The ticket's working assumption — *"`round(x,2)` on a 0.01 grid equals round-half-even to nearest tick"* — is **FALSE at the float level**, and a naive `round_to_tick(p, t) = round(p/t)*t` would violate the CL byte-identity hard constraint.

Empirical check (seeded, 1.2M+ samples incl. adversarial x.xx5/negatives/ES-magnitude values; scratchpad script, results reproduced in the TDD pins):
- `round(round(p/0.01)*0.01, 2)` vs `round(p, 2)`: **16,865 bitwise mismatches** (~1.4%). Examples: `2.675 → 2.68 vs 2.67`, `2.665 → 2.66 vs 2.67`, `65.025 → 65.02 vs 65.03`, `0.005 → 0.0 vs 0.01`, `-0.0024 → 0.0 vs -0.0`. Mechanism: `round(p, 2)` rounds half-even on the **exact decimal expansion of the double** (CPython correctly-rounded dtoa), while `p/0.01` perturbs the value through division by the inexact double `0.01`, frequently landing exactly on a representable `.5` quotient that half-evens the other way.
- Therefore the helper MUST route power-of-ten ticks through `round(price, n)` itself — identity with legacy CL code **by construction**, not by measurement.
- `2 * 0.01` is bit-identical to the literal `0.02` (doubling is exact), and `get_instrument("CL").tick_size` is the same source-literal `0.01` as `_CL_TICK_SIZE` — so `2 * tick_size` reproduces today's `tick2` exactly.
- General branch verified on non-power-of-ten grids: 0 off-grid outputs in 600k samples on 0.25/0.005 grids (0.25 is a binary power → division/multiplication exact; 0.005 canonicalized by the outer `round(_, 3)`); idempotent on all registry ticks.
- Blueprint spot cases: ES `6012.50+3.47 → 6016.00`, `6012.50−2.31 → 6010.25`; GC `2341.13 → 2341.1`; ZC `450.37 → 450.25`; CL negatives (`-37.635 → round(-37.635, 2)`) all bit-exact vs the reference rule.

### Rounding-direction semantics (nearest vs side-aware floor/ceil)

- **IBKR requires** prices on the `minPriceIncrement` grid (else Error 110); it imposes **no direction**.
- **Backtest B(a)** uses nearest (half-even on decimal expansion) to cents.
- **Current CL live** uses nearest everywhere. Hard constraint (1) ⇒ the ONLY rule that keeps CL byte-identical is nearest/half-even, i.e. the generalization of `round(x, 2)`.
- Side-aware conservative rounding (SL toward entry, TP away, marketable buffer away from the market) was analyzed and **REJECTED**: (a) it changes CL outputs for any off-grid input → violates constraint (1); (b) it systematically biases ½ tick away from the backtest's nearest-rounding model → worse live/backtest parity, not better; (c) marketability impact of nearest is bounded by ½ tick out of a 2-tick buffer, and in practice bar closes/fills arrive **on-grid**, where nearest is exact and the direction question is moot.
- **Deviation from the blueprint example**: B4 testability says "BUY on ES at 6000.10 → limit 6000.75", which implies ceil; under nearest, `6000.10 + 0.50 = 6000.60 → 6000.50`. We deliberately choose nearest (reasons above). With on-grid inputs (`6000.25 + 0.50 = 6000.75`) the blueprint's number is reproduced exactly.

---

## 3. Design (deliverable 2/5 — exact localized changes)

### 3.1 Helper — `src/core/instrument_master.py` (append; no existing lines touched)

```python
import math
from decimal import Decimal
from functools import lru_cache

@lru_cache(maxsize=None)
def _tick_grid(tick_size: float) -> tuple[int, bool]:
    """(decimals, is_power_of_ten) for a tick size, via its shortest repr."""
    if not (isinstance(tick_size, float) and math.isfinite(tick_size) and tick_size > 0):
        raise ValueError(f"Invalid tick_size {tick_size!r}: must be a finite float > 0")
    d = Decimal(str(tick_size)).normalize()
    return max(0, -d.as_tuple().exponent), d.as_tuple().digits == (1,)

def round_to_tick(price: float, tick_size: float) -> float:
    """Snap a price to the instrument tick grid, round-half-even to nearest.

    Power-of-ten ticks (0.01, 0.1, 0.001) use round(price, n) — BIT-IDENTICAL
    to the legacy CL cent rounding by construction (hard constraint: a naive
    round(price/tick)*tick mismatches round(price, 2) on ~1.4% of inputs).
    Other ticks (0.25, 0.005) round the tick count and reconstruct the
    canonical n-decimal double. Raises on non-finite price / invalid tick.
    """
    if not math.isfinite(price):
        raise ValueError(f"Cannot round non-finite price {price!r} to tick")
    nd, pow10 = _tick_grid(tick_size)
    if pow10:
        return round(price, nd)
    return round(round(price / tick_size) * tick_size, nd)
```

**Location justification:** `instrument_master.py` is the pure-stdlib leaf that already owns `tick_size` (single source of truth) and is already imported by `ibkr_client.py`, `live_trader.py` (via `instrument_context`), and the training side — so the same function is importable by a future backtest tick-awareness ticket without touching live packages. A new `src/live_execution/pricing.py` was considered and rejected: ~20 lines don't justify a module, and it would be unreachable from the backtest/generator side without a live import. Registry invariant tests already live next door (`tests/test_instrument_master_live_fields.py`).

**No silent defaults:** invalid tick, non-finite price, unknown symbol (`get_instrument`) all raise `ValueError`.

### 3.2 `ibkr_client.py` — tick + exchange from the contract/position in hand (no signature changes)

- **S1**: delete `_CL_TICK_SIZE` (verified: no consumers anywhere outside this file — do NOT defer to T6, dead constants invite reuse).
- **S4/S5** (`place_bracket_order` / `place_entry_order`, marketable_limit branch only):
  ```python
  tick = get_instrument(contract.symbol).tick_size   # raises for unknown symbol
  buf = 2 * tick                                     # 2 instrument ticks (was 2 CL ticks)
  ml_price = round_to_tick(limit_price + buf, tick)  # BUY; − for SELL
  ```
  Log lines keep shape, `%.2f` → keep (display); buffer value logged is now instrument-true.
- **S2 + X1/X2** (`close_cl_position`, `close_cl_position_market`): once per matched position, replace the injection comment block:
  ```python
  # IBKR positions() returns contracts without exchange — inject the
  # registry exchange for the position's OWN contract symbol (MCL≠CL-safe).
  inst = get_instrument(pos.contract.symbol)
  pos.contract.exchange = inst.exchange
  ```
  and in the marketable_limit branch: `buf = 2 * inst.tick_size; lmt_price = round_to_tick(current_price ± buf, inst.tick_size)`.
  - `pos.contract.symbol` (not the `symbol` argument) is authoritative: for an MCL-hands config the position is MCL → registry MCL → NYMEX; MES → CME. (They are equal today because the loop filters `pos.contract.symbol != symbol`, but the position's own symbol is the semantically correct source and survives future loop changes.)
  - Unknown symbol → `get_instrument` raises `ValueError("Unknown instrument symbol: …")` — loud, no order placed. Unreachable in practice (the `symbol` arg is registry-validated upstream by T1), which is exactly what a fail-fast should be.
  - Alternative considered — re-qualify via the exec adapter's `_cached_contracts`: rejected. `close_cl_position*` lives in the manager (no adapter cache access), the position contract already carries `conId` (sufficient with exchange for routing), and re-qualification is an async IBKR call that is unsafe from ib_insync callbacks (the same reason `resolve_contract` caches at startup).
- **S3 (RECOMMENDED, R1)**: adaptive exit branch: `LimitOrder(action, qty, round_to_tick(current_price, inst.tick_size))`. For every on-grid input (all real bar closes) `round_to_tick` is the identity → byte-identical; for a hypothetical off-grid input, today = rejected **exit** (stuck position), after = valid order. Exit-path robustness at zero parity cost.

### 3.3 `live_trader.py` — tick via the already-resolved InstrumentContext

- New property, mirroring the T2 `_brain_symbol` pattern (`:2066-2077`) so legacy `LiveTrader.__new__` test seams keep working — structural derivation, NOT a silent default (raises if neither context nor symbol exists):
  ```python
  @property
  def _tick_size(self) -> float:
      ctx = getattr(self, "_instrument_context", None)
      if ctx is not None:
          return ctx.execution_instrument.tick_size
      from src.core.instrument_master import get_instrument
      return get_instrument(self._execution_symbol).tick_size  # AttributeError/ValueError if unset/unknown
  ```
  Execution (not brain) instrument: orders are placed on the execution contract. Micros share the parent tick in the registry, so MCL/MES configs are unaffected either way.
- **S6** `:1093`: `new_sl = round_to_tick(new_sl, self._tick_size)`.
- **S7** `:1655-1679`: all six price computations → `round_to_tick(fill_price ± offset, self._tick_size)` (tiered loop included; `int(round(lots*pct))` untouched).
- **S8 (RECOMMENDED, R2)** `:1513-1519`: snap re-placed recovery prices — `tp_price=round_to_tick(tp_price, self._tick_size)`, same for `sl_price` (scalar path only; ledger stores scalars). Identity for every legitimately-stored price (they were tick-rounded at S7 when created); protects the recovery path — the one whose whole job is preventing naked positions — against off-grid ledger rows (pre-T3 non-CL rows can't exist, but manual DB edits can).

### 3.4 Explicitly UNCHANGED (with reasons)

- Adaptive/market **entry** prices (`place_bracket_order:1112-1124` `lmtPrice=limit_price` raw; `place_entry_order` adaptive; `live_trader.py:3254` `limit_price=current_price`): entry rejection is fail-safe (no position → no nakedness); bar closes are on-grid; snapping would be a genuine behavior change for off-grid inputs. Documented; revisit only with human ACK (§8 Q2).
- `configurable_strategy.py` signal-level rounding (§1b), lot rounding, `ib.bracketOrder` full-bracket path (no live caller passes tp/sl through the adapter), `SimulatedExecution` (parity matching engine — must not move), backtest engine (§1d), all telemetry/display rounding.
- `get_cl_position`/`cancel_open_cl_orders`/`get_account_summary` `symbol="CL"` defaults and `cl_*` naming — T6 (m2/m3).
- Interfaces (`execution_interface.py`) and adapters: **zero signature changes** anywhere in T3.

### 3.5 Scope guards honored (deliverable 4)

NO macro/COT/vol (T4), NO watchdog/session/rollover-timing/`roll_buffer_days` consumption (T5 — note `get_front_month_contract` buffer stays 6 days), NO generator (T6), NO `fleet_runner`, NO backtest-engine or parity-harness changes, NO data-path changes (T2 is done). The only touched files: `src/core/instrument_master.py` (append-only), `src/live_execution/ibkr_client.py`, `src/live_execution/live_trader.py`, plus tests.

---

## 4. Severity, regression status, refactor stance (deliverables 4/5)

- **Severity: HIGH** (workflow MEDIUM/HIGH bucket): multi-line changes on the live order path; blueprint classifies the underlying gaps as BLOCKERs (B4, B5-exit-remainder) with naked-position/stuck-position consequences.
- **Regression: NO.** `git log -n 5` on both files shows the pricing sites predate T1/T2 (`587cef7` original live infra); T1 (`fe1ce5e`) and T2 (`f02ec5e`) did not touch them — T2's audit explicitly routed them here. CL behavior today is correct; the defect only manifests for non-CL symbols.
- **Not a refactor.** Localized fix: one new pure function + mechanical substitution at 8 sites + 2-line exchange fix. No module boundaries move. (Refactoring constraint satisfied — first-solution localized.)

---

## 5. Hard constraints restated for the TDD phase

1. **CL byte-identity**: every S2/S4-S7 output for CL must be bit-equal to today's `round(…, 2)` — guaranteed by the power-of-ten fast path (§3.1) plus `2*tick` doubling exactness (§2); pinned by tests below; enforced end-to-end by the HS14B parity gate re-run.
2. **No silent defaults**: unknown symbol → `ValueError` (get_instrument); invalid tick / non-finite price → `ValueError`; `_tick_size` property raises when nothing is resolvable.
3. **Post-green gate (manager-run, blocking)**: HS14B ledger parity gate (`setup --disable-trailing`, 2200 warmup + 336 replay) must print `PARITY: PASS`, 15=15 trades, 15/15 exact-cent, $0.00 delta — same convention as T1 C3 / T2 C7. Covers S6/S7; S2-S5 are gate-invisible (harness bypasses ibkr_client) and rest on the unit pins.

---

## 6. TDD test list (deliverable 5)

New `tests/test_round_to_tick.py` (pure, no mocks):
1. **CL byte-identity pin (THE regression pin)** — for adversarial set `{2.675, 2.665, 1.005, 65.005, 65.015, 65.025, 100.115, 0.005, 0.015, -0.005, -2.675, -37.635, -0.0, 0.0, 6012.375, 19.99999999999999}` ∪ 100k seeded `uniform(-150, 150)` ∪ x.xx5-shaped sweep: `struct.pack('<d', round_to_tick(x, 0.01)) == struct.pack('<d', round(x, 2))` (bit compare, catches -0.0).
2. Power-of-ten grids: `round_to_tick(2341.13, 0.10) == round(2341.13, 1)`; NG `round_to_tick(x, 0.001) == round(x, 3)` (seeded sample).
3. 0.25 grid (ES/NQ/ZC/ZS): `6000.60→6000.50`, `6012.37→6012.25`, on-grid identity `6000.75→6000.75`, negative `-0.30→-0.25`; every output satisfies the on-grid reconstruction check.
4. 0.005 grid (SI): `32.0024→32.0`, `32.0026→32.005`; on-grid outputs (seeded sample).
5. Validation raises: `tick_size` ∈ {0, -0.01, nan, inf} → ValueError; `price` ∈ {nan, inf, -inf} → ValueError.
6. Idempotence + registry sweep: `round_to_tick(round_to_tick(x, t), t) == round_to_tick(x, t)` for every `INSTRUMENT_REGISTRY` tick; `_tick_grid` accepts every registry tick (0.01→(2,T), 0.25→(2,F), 0.10→(1,T), 0.005→(3,F), 0.001→(3,T), 0.0005→(4,F)).

`tests/test_bracket_order.py` (extend; existing CL pins must keep passing with only the mechanical `contract.symbol = "CL"` fixture addition — MagicMock symbols now raise, which is itself asserted):
7. CL regression pins unchanged: ml BUY `65.00→65.02`, SELL `65.00→64.98`; close ml SELL `72.50→72.48`, BUY `72.50→72.52`; adaptive close `lmtPrice == 72.50`; off-grid-base pin: close ml BUY `current_price=72.505` → `lmtPrice == round(72.505 + 0.02, 2)` (legacy expression computed in-test, bit-equal).
8. ES buffer + grid: `place_bracket_order` ml BUY `limit_price=6000.25` → `lmtPrice == 6000.75` (buffer 0.50 = 2×0.25); SELL → `5999.75`; `place_entry_order` GC ml BUY `2341.10 → 2341.30`; NG ml BUY `2.675 → 2.677`; all outputs `% tick == 0` (via on-grid check).
9. ZC close: mock ZC position, ml `current_price=450.25` → SELL `449.75`.
10. Unknown symbol: `place_bracket_order` ml with `contract.symbol="XX"` → `ValueError` mentioning the symbol; no `placeOrder` call.
11. **Exit exchange table** (both `close_cl_position` and `close_cl_position_market`; mock `positions()`): CL→`"NYMEX"` (regression pin), MCL→`"NYMEX"`, ES→`"CME"`, GC→`"COMEX"`, ZC→`"CBOT"`; injected BEFORE `placeOrder`; unknown-symbol position → `ValueError`, `placeOrder` not called.
12. Exit modes regression: market/adaptive/fallback tests (`:335-399`) unchanged post-fixture-churn.

New `tests/test_tick_pricing_live_trader.py` (LiveTrader seams, mocked exec client):
13. **CL child-price pin**: `_place_bracket_children_on_fill` with fill `65.13`, offsets tp `0.47`/sl `0.29` → `place_child_orders` receives TP `65.60`, SL `64.84` — bit-equal to `round(…, 2)`; half-cent case fill `65.005` bit-equal to legacy.
14. **ES naked-stop scenario (THE blocker test)**: ES-context trader, entry fill `6012.50`, cent-derived offsets tp `3.47`/sl `2.31` → children called with TP `6016.00`, SL `6010.25` — both tick-valid on the 0.25 grid → the TP/SL children after a filled ES entry can no longer draw Error 110. Tiered variant `[(0.5, 1.13), (0.5, 2.87)]` → every `(lots, price)` tick-valid, lot arithmetic byte-identical to today.
15. GC tiered TPs land on the 0.10 grid; SELL-side (short) mirror of #14.
16. Trailing: ES entry `6012.50`, offset product off-grid → `modify` path computes `new_sl % 0.25 == 0`; CL trailing bit-equal to `round(new_sl, 2)` (pin).
17. `_tick_size` property: full init (CL config) → `0.01`; `LiveTrader.__new__` seam + `_execution_symbol="ES"` → `0.25`; seam with unknown symbol → raises; seam with nothing set → raises (no silent default).
18. (If R2 adopted) recovery re-place: ledger prices `(65.60, 64.84)` pass through bit-identical for CL; off-grid ES ledger row `(6016.03, 6010.19)` → re-placed as `(6016.00, 6010.25)`.

Mechanical churn budget: `tests/test_bracket_order.py` fixtures set `contract.symbol`/`pos.contract.symbol` (close fixture already does); no other test files are expected to churn (`test_cooldown.py`/`test_ibkr_adapters.py` mock the manager wholesale).
Post-green: **HS14B parity gate re-run** (manager, blocking — §5.3).

---

## 7. Deviations from the T3 blueprint sketch (with justification)

1. **Nearest (half-even) rounding, not away-rounding** — the blueprint's ES example (`6000.10 → 6000.75`) implies ceil-for-BUY. Rejected: constraint (1) mathematically forces nearest for CL, and a uniform rule beats side-aware branching (§2). On-grid inputs reproduce the blueprint's arithmetic exactly.
2. **Power-of-ten fast path is mandatory, not an implementation detail** — new empirical finding (§2): the naive `round(p/t)*t` formulation mismatches `round(p, 2)` bitwise on ~1.4% of inputs (incl. plain half-cents like 2.675). Any implementation that doesn't special-case decimal ticks fails the hard constraint.
3. **No signature changes to ibkr_client order methods** — blueprint arch item 6 implied threading tick as a parameter. Tick/exchange are resolved inside the manager from `contract.symbol`/`pos.contract.symbol` (T2 precedent: exchange resolved inside `ibkr_client` per symbol, T2 audit §8.2). Zero interface/adapter churn; unknown symbols raise at the same depth.
4. **Helper placed in `src/core/instrument_master.py`** — blueprint left the location open ("new round_to_tick helper"); justification §3.1.
5. **Two additive recommendations beyond the sketch**: R1 adaptive-exit snap (S3) and R2 recovery re-place snap (S8) — both identity for all legitimate inputs, both harden exactly the stuck-position/naked-position paths this ticket exists to kill. Flagged for approval rather than smuggled in (§8).
6. **`close_cl_position_market` fixed despite zero live callers** — 1 line, public API, same defect family.
7. **Signal-level cent rounding (`configurable_strategy.py`) deliberately NOT migrated** — analysis §1b; zero Error-110 exposure once S7 snaps; avoids strategy-constructor churn inside the parity surface.

## 8. Open questions requiring human/manager authorization

- **Q1 (recommend YES)**: adopt R1 (adaptive-exit price snap) and R2 (recovery re-place snap)? Both are identity for on-grid/ledger-valid inputs and only change behavior where today's behavior is a rejected exit / naked position. Cost: two extra pins (#7 adaptive, #18).
- **Q2 (recommend NO for T3)**: snap adaptive/marketable **entry** limit prices at `live_trader.py:3254` / adaptive branches? Entry rejection is fail-safe; deferred with documentation (§3.4). Needs explicit ACK only if someone wants belt-and-braces entries now.
- **Q3 — adjacent bug found during audit, OUT of T3 scope, needs its own ticket**: `IBKRExecutionClient` defines **no `modify_order`**, and `live_trader.py:1122` guards with `hasattr(self.exec_client, "modify_order")` — so on the REAL IBKR adapter the trailing-stop SL modification is **silently never transmitted** (only the local `raw_order.auxPrice` mutation happens; ib_insync requires re-`placeOrder` to modify). `SimulatedExecution` HAS `modify_order`, so the parity harness exercises trailing while production silently doesn't. The log line "TRAILING STOP: modified SL order …" is misleading. No tick/rounding aspect → not T3; flagging because it sits 30 lines from S6 and the manager should mint a ticket (suggest alongside T5).

## 9. Verification evidence

- Site census: repo-wide greps for `round(`, `_CL_TICK_SIZE`, `NYMEX`, `close_cl_position*`, `place_*_order` at HEAD; all hits classified in §1; `_CL_TICK_SIZE` and `close_cl_position_market` consumer searches came back empty outside `ibkr_client.py`/tests.
- Numeric claims: scratchpad script (seeded RNG 20260704, 1.2M+ samples + adversarial set) — mismatch counts and spot table reproduced in §2; claims 2 (buffer doubling exactness), 3 (grid validity/idempotence) all green.
- Git history (bounded per guardrail): `git log -n 5` on both target files — pricing sites originate in `587cef7`, untouched by T1/T2.
- T2 handoff honored: `t2-symbol-data-paths_07042026_1815/tdd_result.md` "T3 owns: tick-size order pricing incl. `close_cl_position*` NYMEX injection"; T2 audit §7 Q2b, §8.
