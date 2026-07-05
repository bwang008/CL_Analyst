# Impact Review — t3-tick-order-pricing_07042026_1954

**Reviewer:** Ticket-Impact-Reviewer | **Date:** 2026-07-04 | **Baseline:** branch `development`, HEAD `f02ec5e` (verified clean source tree). Read-only pass — no source modified; independent verification script run from scratchpad (not the repo).

## VERDICT: APPROVE

Approved as designed, subject to the manager rulings already given (R1/R2 adopted, entry-snapping deferred, Q3 modify_order OUT — separate ticket `live-trailing-modify-order-dead_07042026_2012` observed already minted) and the audit's own blocking post-green condition (HS14B parity gate re-run). Two non-blocking recommendations below (REC-1, REC-2).

---

## 1. Constraint evaluation (workflow rules)

| Rule | Triggered? | Disposition |
|---|---|---|
| **Interface Rule** | **NO** | Zero public signature changes, verified: S4/S5 resolve tick internally from `contract.symbol`; `adapters/ibkr_execution.py` and `interfaces/execution_interface.py` untouched; `round_to_tick` is a new additive pure function; `_tick_size` is a private property. Deleting `_CL_TICK_SIZE` is signature surface only in theory — repo-wide grep confirms **zero consumers outside `ibkr_client.py`** (only .md docs elsewhere; not one test references it). |
| **Base Class Rule** | **Technically yes** (`src/core/instrument_master.py` is a widely-imported core utility) | Approved under the Business Justification exception: the change is **append-only** (new imports + one cached classifier + one pure function; no existing line modified, no existing consumer affected), the file is the single source of truth for `tick_size`, and the alternative (`src/live_execution/pricing.py`) was considered and reasonably rejected (unreachable from the future backtest-side ticket). Justification is strong and specific. |
| **Refactor Veto** | **NO** | Not a multi-component refactor: one appended helper + mechanical substitution at 8 sites across 2 files + a 2-line exchange fix. No module boundaries move, no component is rewritten, no interfaces change. Human authorization not required beyond the manager rulings already issued. |

## 2. Independent verification results

### V-A. Float-precision claim (the crux) — **REPRODUCED, auditor is RIGHT**
Reproduced from scratch with a **different RNG seed** (987654321 vs auditor's 20260704), 880,019 samples (uniform ±150, uniform ±8000 for ES/GC magnitude, every half-cent in [−200, 200], the adversarial spot set):
- Naive `round(round(x/0.01)*0.01, 2)` vs legacy `round(x, 2)`: **16,480 bitwise mismatches (1.87%)** — rate is sample-mix dependent; same order as the auditor's ~1.4%. All five auditor examples confirmed exactly (`2.675→2.68 vs 2.67`, `2.665→2.66 vs 2.67`, `65.025→65.02 vs 65.03`, `0.005→0.0 vs 0.01`, `-0.0024` sign-of-zero).
- The proposed helper (transcribed verbatim from audit §3.1): **0/880,019 bitwise deviations** from `round(x, 2)` at tick 0.01; same for 0.10 vs `round(x,1)` and 0.001 vs `round(x,3)`.
- **Composition identity at the marketable-limit sites**: `round_to_tick(x ± 2*0.01, 0.01)` bit-equal to legacy `round(x ± 0.02, 2)` over all 880k samples both sides (0 mismatches); `2*0.01` bit-identical to `0.02`.
- General branch: 0 off-grid outputs and full idempotence on 0.25 / 0.005 / **0.0005** (HG — I extended the auditor's 0.25/0.005 evidence to the fourth registry grid); `_tick_grid` classification matches audit test #6 for all six registry ticks; all ES/GC/ZC/SI/NG/CL-negative spot cases pass; all validation raises confirmed.

Conclusion: the mandatory power-of-ten fast path is correctly identified as load-bearing. Any naive implementation would violate the CL byte-identity hard constraint; this one holds it by construction. This was the REJECT trigger and it did not fire.

### V-B. `_CL_TICK_SIZE` consumer census — **CONFIRMED**
Repo-wide grep: definition + 3 use sites in `src/live_execution/ibkr_client.py` (:551, :984, :1133, :1227) and .md documents only. No tests, no scripts, no other modules. Deletion is safe.

### V-C. Site-census completeness — **CONFIRMED, nothing missed**
Independent sweep of `round(`, `np.round`, `floor(`, `ceil(`, `quantize`, `Decimal` across `src/live_execution/` (incl. `adapters/`, `strategies/`):
- `strategies/execution_models.py`: **zero rounding of any kind** — correctly absent from the census.
- `live_trader.py`: exactly S6 (:1093) + the six S7 price sites (:1655, :1663, :1666, :1668, :1676, :1679); :1660/:1673 are `int(round(lots*pct))` lot sizing — correctly excluded; every other `.2f` hit is a log/Telegram format string (display).
- S8 recovery (:1513-1519) passes ledger `tp_price`/`sl_price` through unrounded — R2's target confirmed.
- `configurable_strategy.py` :549/:561-565: confirmed upstream-of-order-path — `live_trader.py:3244-3245` converts signal prices to **offsets** and S7 recomputes final prices from the fill; `place_child_orders` (ibkr_client :1294-1317) applies no further rounding, so S7 is the single quantization point for children. Non-migration justified.
- `data_manager.py` :660/:670 — roll-ratio metadata, correctly telemetry.
- The unrounded full-bracket path (`ib.bracketOrder` tp/sl at :1101-1106): the only live caller of `exec_client.place_bracket_order` is `live_trader.py:3254`, which passes no tp/sl → routes to `place_entry_order`. Claim "no live caller" confirmed.

### V-D. Exit-exchange fix — **SOUND**
- `pos.contract.symbol` is **already load-bearing** in both close methods today (loop filter `pos.contract.symbol != symbol` at :498/:539) — if it were unreliable, today's code would already fail to match positions. The fix reads the same attribute.
- Registry covers all 15 symbols incl. all five micros (MCL/MES/MGC/MNQ/SIL), each with `exchange` and `tick_size`; `get_instrument` raises `ValueError("Unknown instrument symbol: …")`.
- Unknown-symbol raise is effectively unreachable: the filter only passes positions whose symbol equals the requested `symbol`, which is `self._execution_symbol` — registry-validated at trader startup (InstrumentContext construction). A held position in an *unregistered* symbol is skipped by the pre-existing filter exactly as today — the fix introduces **no new stuck-close mode**. If reached via direct API misuse, the raise happens BEFORE `placeOrder` (no mis-routed order), and two of three live call sites wrap in try/except with loud actionable logs (rollover :2196-2210 `log.error`; kill-switch :3891-3922 `log.exception` "MANUAL INTERVENTION REQUIRED"). The time-barrier site (:1258) does not wrap, but cannot see an unknown symbol for the reason above. Acceptable.
- `close_cl_position_market`: zero live callers confirmed (only a docstring mention in `test_cooldown.py:170`); fixing it anyway is 1 line on a public API — agreed.

### V-E. Backtest-parity surface — **CONFIRMED**
- `agent/backtest_engine.py` untouched by the proposal; independently confirmed `agent/` has **zero** references to `tick_size`/`get_instrument`/`round_to_tick` — no hidden coupling. Cent-grid rounding at :663-669 verified present as described.
- S7 **is** inside the parity gate's blast radius: `SimulatedExecution` fires `_order_callbacks` (:548/:699/:744) → real `LiveTrader._on_order_status` → real `_place_bracket_children_on_fill`. (The harness-source assertion in `test_exit_reason_and_fill_routing.py:290-296` only bans *direct* harness calls; the callback route is the one exercised.)
- **Nuance (REC-2)**: audit §1d.4 says "S6/S7 changes are inside the gate's blast radius", but §1d.3/§5.3 correctly note the gate runs `--disable-trailing` — so **S6 is NOT actually exercised by the gate** and rests entirely on unit pin #16. The test plan already covers this; pin #16 should include a seeded sweep, not just spot values.
- Gate-invisible S2/S4/S5 unit bit-pins: **adequate**. The helper-level 100k sweep (test #1) proves `round_to_tick(_, 0.01) ≡ round(_, 2)` universally; `2*tick == 0.02` is bitwise exact; my V6 composition sweep confirms the assembled expression is bit-identical over 880k samples. REC-1 asks for that composition sweep to be pinned in the suite.
- Non-CL ≤½-tick backtest/live divergence: correctly scoped out (backtest tick-awareness is a training-side ticket that must move the gate deliberately); the instruction to spec non-CL parity gates with ½-tick tolerance is the right operational note.

### V-F. Test churn census — **COMPLETE as claimed**
Six test files touch the changed paths; churn verified file-by-file:
- `tests/test_bracket_order.py` — the ONLY file needing churn: the 4 `TestMarketableLimitOrder` entry tests pass bare `MagicMock()` contracts and will hit `get_instrument(contract.symbol)` → need `contract.symbol = "CL"` (the audit's "MagicMock symbols now raise, which is itself asserted" is correct — a MagicMock key is not in the registry → ValueError). The close fixture **already** sets `pos.contract.symbol = "CL"` (:326), so all 6 `TestClosePositionModes` tests pass unchanged even though the exchange fix now calls `get_instrument` in every exit mode. Adaptive/market entry tests don't reach the ml branch → no churn.
- `tests/test_live_trader_bugs.py` and `tests/test_exit_reason_and_fill_routing.py` — all `LiveTrader.__new__` stubs already set `_execution_symbol = "CL"` → the `_tick_size` fallback resolves to 0.01 → CL byte-identity keeps every existing assertion green with **zero churn** (incl. the real-method `test_place_bracket_children_on_fill` at :255-283).
- `tests/test_ibkr_adapters.py` — patches `IBKRConnectionManager` wholesale (`_make_client_and_manager`), zero churn. `tests/test_cooldown.py` — mocks `exec_client`, zero churn. `tests/test_simulated_execution.py` — SimulatedExecution unchanged, zero churn.

## 3. Recommendations (non-blocking)

- **REC-1**: Add the composition property pin to the TDD suite (e.g. in `test_round_to_tick.py`): seeded sweep asserting `struct.pack('<d', round_to_tick(x + 2*0.01, 0.01)) == struct.pack('<d', round(x + 0.02, 2))` (and `−` side). This pins the *assembled* S2/S4/S5 expression, not just the helper, closing the gate-invisibility gap at the property level rather than only via spot pins #7-#9.
- **REC-2**: Ensure trailing pin #16's CL branch is a seeded sweep (S6 is unit-pin-only coverage; the gate's `--disable-trailing` means no end-to-end enforcement — align §1d.4's wording during TDD).

## 4. Conditions carried forward (already in the plan / manager rulings)

1. Post-green **HS14B ledger parity gate re-run** (manager-run, blocking): `PARITY: PASS`, 15=15, 15/15 exact-cent, $0.00 delta.
2. R1/R2 adopted per manager ruling (pins #7-adaptive and #18 required).
3. Entry-price snapping stays deferred and documented (§3.4); revisit only with human ACK.
4. Q3 trailing `modify_order` dead-path stays OUT — tracked in `live-trailing-modify-order-dead_07042026_2012`.

## 5. Evidence

- Verification script: `scratchpad/verify_tick_rounding.py` (session scratchpad, seed 987654321, 880,019 samples) — all 40 checks PASS; not committed to the repo per read-only mandate.
- Greps at HEAD `f02ec5e`: `_CL_TICK_SIZE`, `round(|np.round|floor(|ceil(|quantize|Decimal` over `src/live_execution/`, `place_bracket_order|place_entry_order` over `src/`, `tick_size|get_instrument|round_to_tick` over `agent/`, close-position caller census over repo.
- Files read in full or at cited ranges: `src/core/instrument_master.py`, `src/live_execution/ibkr_client.py` (:480-620, :975-1320), `src/live_execution/live_trader.py` (:1060-1140, :1245-1300, :1490-1560, :1630-1700, :2062-2094, :2190-2220, :3230-3270, :3890-3934), `src/live_execution/adapters/ibkr_execution.py`, `agent/backtest_engine.py` (:655-675), `tests/test_bracket_order.py` (full), `tests/test_ibkr_adapters.py`, `tests/test_cooldown.py`, `tests/test_live_trader_bugs.py`, `tests/test_exit_reason_and_fill_routing.py` (targeted).
