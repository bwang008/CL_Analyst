# Audit — T1 Instrument metadata plumbing
**Ticket:** `t1-instrument-metadata_07042026_1543`
**Auditor:** Ticket-Auditor | **Date:** 2026-07-04 | **Branch:** `development` (post ba12077 / eb6e9d8 fleet-runner merge)
**Source blueprint:** `.agents/collab/tickets/multi-symbol-live-gaps_07042026_1520/blueprint.md` (items M1 + M7; Recommended Architecture 1-3; T1 sketch)

---

## 1. Severity & regression status

- **Severity: MEDIUM/HIGH** (ticket-auditor scale — multi-file structural change: registry schema extension + new module + 2 call-site changes + 1 config migration + test churn). In blueprint terms it fixes **M1** (silent `"CL"` default = latent wrong-instrument trading) and **M7** (registry missing all live fields), and is the enabler for T2-T5.
- **Regression: NO.** This is a longstanding design gap, not a recent break. `strategy_config.get("execution_symbol", "CL")` has been the pattern since the Two-Stream (CL/MCL) work; the registry (`src/core/instrument_master.py`) was extended in ba12077 with cftc_code/volatility_index for 10 symbols but has never carried live-execution fields. Nothing in ba12077 or the fleet-runner merge (eb6e9d8) regressed this — the fleet runner (`src/live_execution/fleet_runner.py`) never reads `execution_symbol` at all (verified by grep), consistent with the blueprint's "fleet runner needs no changes".

## 2. Blueprint claims verified against current code (line numbers re-checked 2026-07-04)

| Claim | Verdict |
|---|---|
| `live_trader.py:276-278` silently defaults `execution_symbol` to `"CL"` | **CONFIRMED** — exact lines, current code: `self._execution_symbol: str = strategy_config.get("execution_symbol", "CL").upper()` |
| Registry lacks exchange/multiplier/months/sessions/etc. (M7) | **CONFIRMED** — current `Instrument` dataclass: symbol, name, tick_size, tick_value, cftc_code, volatility_index, slippage_ticks only. 10 entries (CL, ES, NG, HG, GC, PA, NQ, ZC, ZS, SI). `get_instrument` raises `ValueError: Unknown instrument symbol: {symbol}` on miss (good, keep). |
| `cli.py:167-172` bakes CL seed/cache defaults before config load; `:229-231` cache "always shared" | **CONFIRMED** — `--seed-path` default `_DEFAULT_SEED_PATH` (=`raw/cl-5m_bk.csv`), `--cache-path` default `_DEFAULT_CACHE_PATH`; comment "OHLCV warm-start cache is SHARED — all strategies receive the same CL continuous bars". No instrument resolution anywhere in `cli.py` today. |
| ES config ships `execution_symbol: "CL"` with `E2E_ES_*` experiment_ids | **CONFIRMED** — `configs/strategies/ES01B_Sharpe_E03_07042026.json:16` = `"CL"`; models at `:26/:32` = `E2E_ES_HourSet_01B_{long,short}_logloss`. |
| Real artifacts on disk are `E2E_HourSet_01B_*` (symbol stripped) | **CONFIRMED** — `reports/batch_runs/E2E_HourSet_01B_{long,short}_{logloss,average_precision}` exist; `reports/sweep_es01b_2x1_6h_scout_20260704-0701/registry/production_output/registry/` is EMPTY of `E2E_ES_*` dirs. Stripping logic verified at `gcp/vm_e2e_pipeline.py:651-663`. |
| MCL is a supported execution pattern | **CONFIRMED** — `adapters/ibkr_data_feed.py:93-96,135-138` (`symbol == "MCL"` → `build_mcl_contract`), `ibkr_client.py:62-84,608-616`. Note: **MCL is NOT in INSTRUMENT_REGISTRY** — `get_instrument("MCL")` raises today. T1 must add it or MCL configs break at startup validation. |
| MCL brain-stream nuance | Comment at `live_trader.py:275` says "Brain=CL, Hands=CL or MCL", but the continuous ("brain") subscription (`live_trader.py:2042-2054` → `ibkr_data_feed.py:91-96`) passes `self._execution_symbol`, so an MCL config's brain stream is *MCL-continuous*, not CL-continuous. T1 must not change this behavior — validation only maps MCL→CL for *model-tag comparison*. |
| Circular-import risk for `instrument_context` | **NONE.** `src/core/instrument_master.py` imports only stdlib (`dataclasses`, `typing`) — a pure leaf. `live_trader.py` already reaches `src.core` transitively (`src.features.macro_features:126` does a deferred `from src.core.instrument_master import get_instrument`). A new `src/live_execution/instrument_context.py` importing `src.core.instrument_master` creates no cycle; `cli.py` and `live_trader.py` can import it top-level. |

## 3. Open design questions — investigated

### 3a. Is experiment_id a reliable source for the expected symbol? **No — only opportunistically.**
Full fleet census of `models.*.experiment_id` (all 20 configs in `configs/strategies/`):

| Shape | Configs | Symbol derivable? |
|---|---|---|
| `E2E_CL_HourSet_14B_*` | HS14B (the live prod config) | YES → CL |
| `E2E_ES_HourSet_01B_*` | ES01B | YES → ES (and mismatches its `execution_symbol:"CL"` — the exact shipped bug) |
| `E2E_HourSet_{08,09,11,13A}_*` | 8 configs | NO (no symbol token) |
| `_long_logloss` (degenerate) | HS13B | NO |
| `EXP-025_S_Ultimate_OOS`, `sweep_h4s01_*` | ensemble2_opt, FourHour_Canary | NO (legacy) |

Additionally, **post-T6 the generator will emit symbol-stripped tags** (`E2E_HourSet_01B_*`, matching `vm_e2e_pipeline.py:658-659` which strips the `{symbol}_` prefix), so future correct configs will carry *no* symbol in experiment_id. Conclusion: validation must be **opportunistic** — parse the token after `E2E_`; if it is a registry symbol, hard-enforce match against the brain symbol; otherwise skip with an INFO log. This catches the ES01B bug today with zero false positives across all 19 CL configs (verified against the census above). For the long term, T6 should emit an explicit `models.*.symbol` field which this validator hard-enforces whenever present (hook included in the design below).

### 3b. Which configs lack `execution_symbol`? **Exactly one: `configs/strategies/ensemble2_opt.json`.**
The other 19 all carry `"execution_symbol": "CL"` (grep-verified). Decision: **required-with-clear-error + one-line migration** of `ensemble2_opt.json` (add `"execution_symbol": "CL"`), not grandfathering. Justification: grandfathering would re-introduce the silent default under a different name (violates the no-silent-null-defaults house rule); the migration is byte-minimal, behavior-identical (the default was CL anyway), and leaves the codebase with a single unconditional rule.

### 3c. MCL / micro pattern. Supported today via `execution_symbol:"MCL"`; brain stream subscribes MCL-continuous, models are CL-trained. T1 handles it by (i) adding micro contracts as **first-class registry entries** with a `micro_of` parent pointer, and (ii) comparing model tags against the *brain* symbol (`micro_of or execution_symbol`). MCL + `E2E_CL_*` passes; MCL + `E2E_ES_*` raises.

### 3d. Where does instrument_context live? `src/live_execution/instrument_context.py` as the blueprint suggests — no import cycle (see §2). It imports only `src.core.instrument_master` + stdlib; `cli.py` and `live_trader.py` import it.

### 3e. Test-compat blast radius. Direct `LiveTrader(...)` constructions with a config lacking `execution_symbol` exist only in `tests/test_live_macro_refresh.py` (`DummyStrategy.config = {}`, 3 constructions) — must gain `{"execution_symbol": "CL"}`. All other live-trader tests bypass `__init__` and set `trader._execution_symbol = "CL"` directly (test_cooldown, test_reconnection, test_trailing_stop_*, test_live_trader_bugs, test_account_summary, test_exit_*) — unaffected. `tests/test_schemas.py:9-16` covers registry get/raise — unaffected; extend it.

## 4. Proposed implementation (localized; no refactor of consumers)

### 4.1 `src/core/instrument_master.py` — extend `Instrument` + registry

New fields inserted **before** `slippage_ticks: int = 1` (dataclass ordering: non-default fields cannot follow defaulted ones). All new fields are **required** (no defaults) except the two that are legitimately absent for some symbols (`micro_of`) — construction of every entry supplies every field, so nothing can be silently missing at runtime.

```python
@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    tick_size: float
    tick_value: float            # USD per tick (existing)
    cftc_code: str
    volatility_index: str        # FRED series, training side (existing)
    exchange: str                # IBKR exchange string: NYMEX/CME/COMEX/CBOT
    multiplier: int              # IB contract multiplier (units per quoted price point)
    quote_unit_usd: float        # USD per quoted price unit (1.0; 0.01 for grains quoted in cents)
    active_months: str           # MGL month codes, e.g. "HMUZ", "FGHJKMNQUVXZ"
    roll_reference: str          # "LTD" (last trade) or "FND" (first notice, physically delivered)
    roll_buffer_days: int        # calendar days before roll_reference to roll
    session_hours_ct: tuple      # ((open, close), ...) America/Chicago; wraps midnight; approximate — T5 uses IB tradingHours as authority
    bars_per_day_5m: int         # conservative provisioning floor (CL pinned to legacy 288)
    bars_per_day_1h: int         # conservative provisioning floor (CL pinned to legacy 24)
    live_vol_index: str          # IBKR CBOE index symbol for daily-close fetch ("VIX"/"OVX"/"GVZ")
    micro_of: Optional[str] = None   # parent symbol if this IS a micro (MCL→"CL"); None otherwise
    slippage_ticks: int = 1
```

Invariant (enforced by test): `tick_value == tick_size * multiplier * quote_unit_usd` for every entry.

**Per-symbol values** (verified against CME Group contract specs — NYMEX WTI [CL], Micro WTI [MCL], E-mini S&P 500 [ES], Micro E-mini S&P [MES], E-mini Nasdaq-100 [NQ], Micro E-mini Nasdaq [MNQ], Henry Hub NG, COMEX Gold [GC], Micro Gold [MGC], COMEX Silver [SI], Micro Silver 1000-oz [SIL], CBOT Corn [ZC], CBOT Soybeans [ZS], COMEX Copper [HG], NYMEX Palladium [PA]):

| Sym | Exchange | tick_size | tick_value | multiplier | quote_unit | active_months | roll_ref | buffer_d | sessions (CT) | bars 5m/1h | live_vol | micro_of |
|-----|----------|-----------|------------|------------|-----------|---------------|----------|----------|----------------|------------|----------|----------|
| CL  | NYMEX | 0.01 | 10.00 | 1000 | 1.0 | FGHJKMNQUVXZ | LTD | **6** (= current `_EXPIRY_BUFFER_DAYS`, zero-change) | 17:00–16:00 | **288/24** (legacy code constants, zero-change) | OVX | — |
| MCL | NYMEX | 0.01 | 1.00 | 100 | 1.0 | FGHJKMNQUVXZ | LTD | 6 | 17:00–16:00 | 288/24 | OVX | CL |
| ES  | CME | 0.25 | 12.50 | 50 | 1.0 | HMUZ | LTD | 8 (volume-roll Monday ≈ 8 cal days pre 3rd-Friday expiry) | 17:00–16:00 | 276/23 | VIX | — |
| MES | CME | 0.25 | 1.25 | 5 | 1.0 | HMUZ | LTD | 8 | 17:00–16:00 | 276/23 | VIX | ES |
| NQ  | CME | 0.25 | 5.00 | 20 | 1.0 | HMUZ | LTD | 8 | 17:00–16:00 | 276/23 | VIX | — |
| MNQ | CME | 0.25 | 0.50 | 2 | 1.0 | HMUZ | LTD | 8 | 17:00–16:00 | 276/23 | VIX | NQ |
| NG  | NYMEX | 0.001 | 10.00 | 10000 | 1.0 | FGHJKMNQUVXZ | LTD | 6 (LTD = 3 biz days before delivery-month start) | 17:00–16:00 | 276/23 | OVX | — |
| GC  | COMEX | 0.10 | 10.00 | 100 | 1.0 | GJMQVZ (serials listed but illiquid — filter!) | **FND** (last biz day of month before delivery) | 3 | 17:00–16:00 | 276/23 | GVZ | — |
| MGC | COMEX | 0.10 | 1.00 | 10 | 1.0 | GJMQVZ | FND | 3 | 17:00–16:00 | 276/23 | GVZ | GC |
| SI  | COMEX | 0.005 | 25.00 | 5000 | 1.0 | HKNUZ | FND | 3 | 17:00–16:00 | 276/23 | VIX | — |
| SIL | COMEX | 0.005 | 5.00 | 1000 | 1.0 | HKNUZ | FND | 3 | 17:00–16:00 | 276/23 | VIX | SI |
| ZC  | CBOT | 0.25 | 12.50 | 5000 | **0.01** (cents/bu) | HKNUZ | FND | 3 | 19:00–07:45 + 08:30–13:20 (daily halts 07:45–08:30, 13:20–19:00) | 200/16 | VIX | — |
| ZS  | CBOT | 0.25 | 12.50 | 5000 | **0.01** | FHKNQUX | FND | 3 | same as ZC | 200/16 | VIX | — |
| HG  | COMEX | 0.0005 | 12.50 | 25000 | 1.0 | HKNUZ | FND | 3 | 17:00–16:00 | 276/23 | VIX | — |
| PA  | NYMEX | **0.10 / 10.00 — see flag** | | 100 | 1.0 | HMUZ | FND | 3 | 17:00–16:00 | 276/23 | VIX | — |

Notes:
- **bars_per_day semantics matter**: they are conservative *floors* used later (T5) to convert required-bar counts into calendar lookbacks — over-stating them under-provisions the seed. CL is pinned to the legacy 288/24 constants (`data_manager.py:74`, `live_trader.py:335,380`) so T2/T5 wiring is behavior-neutral for CL. Grains floor 16 bars/1h-day (session ≈17.6h but partial-hour bars and holidays argue for the floor).
- **FLAG (needs human ack):** existing PA entry (`tick_size=0.05, tick_value=5.00`, with a self-doubting comment) is **wrong** — NYMEX Palladium is 100 troy oz, tick $0.10 = $10.00. Recommend correcting in T1 since we touch every entry; it changes training-side slippage math for PA only (no PA pipeline exists). If declined, leave values and add a `# KNOWN-WRONG` comment.
- Existing `volatility_index` (FRED) is kept unchanged; `live_vol_index` is the distinct IBKR/CBOE symbol (GVZ vs GVZCLS distinction per blueprint M7). GVZ availability on IBKR as `Index("GVZ","CBOE")` is assumed here and runtime-verified in T4.
- CL entry's pre-existing fields (tick, tick_value, cftc_code, volatility_index, slippage_ticks) are byte-identical — regression-guarded by test.

### 4.2 New `src/live_execution/instrument_context.py`

Imports: `dataclasses`, `typing`, `src.core.instrument_master` only (leaf-safe).

```python
@dataclass(frozen=True)
class InstrumentContext:
    execution_symbol: str
    brain_symbol: str
    execution_instrument: Instrument
    brain_instrument: Instrument

def resolve_instrument_context(strategy_config: Mapping[str, Any]) -> InstrumentContext: ...
def derive_model_symbol(experiment_id: str | None) -> str | None: ...
def validate_models_against_symbol(strategy_config: Mapping[str, Any], brain_symbol: str) -> None: ...
```

**`resolve_instrument_context` rules (exact error messages):**
1. `"execution_symbol" not in strategy_config` (or empty/non-str) →
   `ValueError("Strategy config '{nickname}' is missing required field 'execution_symbol'. Every live strategy config must declare its instrument explicitly (no silent CL default). Add e.g. \"execution_symbol\": \"CL\".")`
   (`nickname = strategy_config.get("nickname", "<unnamed>")` — display-only, allowed to soft-default.)
2. Unknown symbol →
   `ValueError("Strategy config '{nickname}': execution_symbol '{sym}' is not in INSTRUMENT_REGISTRY (known: {sorted keys}).")`
3. `brain_symbol` (optional key): if present it must equal `execution_symbol` or the execution instrument's `micro_of`; else →
   `ValueError("Strategy config '{nickname}': brain_symbol '{b}' is not compatible with execution_symbol '{e}' (expected '{e}'{' or micro parent ' + p if p else ''}).")`
   If absent: `brain_symbol = execution_instrument.micro_of or execution_symbol` (documented structural derivation, not a config default).
4. Calls `validate_models_against_symbol(strategy_config, brain_symbol)`.

**`derive_model_symbol`** (opportunistic, per §3a):
```python
parts = (experiment_id or "").split("_")
if len(parts) >= 2 and parts[0].upper() == "E2E" and parts[1].upper() in INSTRUMENT_REGISTRY:
    return parts[1].upper()
return None
```

**`validate_models_against_symbol`:** for each side in `strategy_config.get("models", {})`:
- If the model entry carries an explicit `"symbol"` field (T6 forward-compat), hard-enforce `symbol.upper() == brain_symbol`.
- Else `tag = derive_model_symbol(entry.get("experiment_id"))`; if `tag is None` → `log.info("models.%s.experiment_id '%s' carries no symbol tag — skipping symbol cross-check", side, exp_id)` and continue.
- Mismatch →
  `ValueError("Strategy config '{nickname}': execution_symbol '{exec}' (brain '{brain}') does not match model symbol '{tag}' declared by models.{side}.experiment_id '{exp_id}'. Refusing to start — this config would trade the wrong instrument.")`

### 4.3 `src/live_execution/live_trader.py:275-278`

Replace the silent default:
```python
from src.live_execution.instrument_context import resolve_instrument_context  # top-level import
...
# Resolve + validate the instrument (raises on missing/unknown/mismatched symbol)
self._instrument_context = resolve_instrument_context(strategy_config)
self._execution_symbol: str = self._instrument_context.execution_symbol
```
`self._execution_symbol` keeps its name and type — the ~45 existing consumers (`live_trader.py:500,628,648,932,...`) are untouched. `self._instrument_context` is stored for T2-T5 to consume; T1 wires nothing else through it (hard scope line).

### 4.4 `src/live_execution/cli.py` — fail fast pre-connect

Immediately after the `--config` strategy is constructed (`cli.py:196-206`), before `DataFeedFactory.create` (`:276`):
```python
from src.live_execution.instrument_context import resolve_instrument_context
ctx = resolve_instrument_context(strategy.config)
log.info("Instrument resolved: execution=%s (%s, tick=%s) brain=%s",
         ctx.execution_symbol, ctx.execution_instrument.exchange,
         ctx.execution_instrument.tick_size, ctx.brain_symbol)
```
Any ValueError propagates and kills the process before any IBKR client exists. (LiveTrader.__init__ re-validates — idempotent, protects direct/test construction.) The legacy `--strategy` path needs nothing: `_STRATEGY_REGISTRY` is empty and `parser.error`s without `--config`. `--seed-path`/`--cache-path` CL defaults are **left alone** (blueprint item 9 belongs to T2 — changing them now without DataManager path derivation would break CL).

### 4.5 Config migration

`configs/strategies/ensemble2_opt.json`: add `"execution_symbol": "CL"` (the only config missing it; §3b). Behavior-identical.

### 4.6 Explicitly OUT of T1 scope
No changes to `ibkr_client.py`, `ibkr_data_feed.py`, `data_manager.py`, `feature_pipeline.py`, factories, order pricing, sessions/watchdog, or the generator — those are T2-T6 and each consumes the fields added here.

## 5. Zero-behavior-change proof for CL (hard constraint)

1. All 19 CL configs carry `execution_symbol:"CL"` → rule 1/2 pass. `ensemble2_opt.json` migrated in the same commit.
2. Model cross-check census (§3a): HS14B → `E2E_CL_*` → CL == CL ✓; all others → no tag → skipped ✓. No CL config can fail validation.
3. Registry: CL's existing field values unchanged; new fields are additive; `Instrument` is only ever constructed inside the registry literal, so required-field additions can't break external constructors (grep: no `Instrument(` outside `instrument_master.py`).
4. MCL configs: `get_instrument("MCL")` now resolves (new entry) — previously MCL never hit `get_instrument` on the live path at all, so adding the entry changes nothing for running code.
5. Intentional non-CL exception: **the current mis-generated `ES01B_Sharpe_E03_07042026.json` will refuse to start** (exec CL vs model tag ES) — that is the ticket's purpose; the config is regenerated in T6.
6. Test churn limited to `tests/test_live_macro_refresh.py` (add `execution_symbol` to `DummyStrategy.config`); all other live tests bypass `__init__`.

## 6. Test list for the TDD tester

Registry (`tests/test_instrument_master_live_fields.py` or extend `test_schemas.py`):
1. `test_registry_completeness` — every entry: non-empty `exchange` ∈ {NYMEX, CME, COMEX, CBOT}; `active_months` chars ⊆ `FGHJKMNQUVXZ` and non-empty; `roll_reference` ∈ {LTD, FND}; `roll_buffer_days > 0`; `session_hours_ct` non-empty (start, end) HH:MM pairs; `bars_per_day_5m/1h > 0`; `live_vol_index` non-empty.
2. `test_tick_value_invariant` — `tick_value == tick_size * multiplier * quote_unit_usd` (±1e-9) for every entry (grains prove the 0.01 quote factor).
3. `test_micro_entries_consistent` — every `micro_of` target exists; micro's exchange/tick_size/active_months/roll_reference equal parent's.
4. `test_cl_legacy_values_unchanged` — CL: tick 0.01, tick_value 10.0, cftc `067651`, volatility_index `OVXCLS`, slippage_ticks 1, bars_per_day 288/24, roll_buffer_days 6 (regression pin).
5. `test_get_instrument_unknown_raises` — exists (`test_schemas.py:14`), keep; add `get_instrument("MCL")` now succeeds.

instrument_context (`tests/test_instrument_context.py`):
6. `test_missing_execution_symbol_raises` — `{}` → ValueError matching `missing required field 'execution_symbol'`.
7. `test_unknown_symbol_raises` — `{"execution_symbol": "XX"}` → ValueError matching `not in INSTRUMENT_REGISTRY`.
8. `test_cl_passthrough` — `{"execution_symbol": "CL"}` → exec CL, brain CL, exchange NYMEX.
9. `test_mcl_brain_maps_to_cl` — exec MCL → brain CL (micro_of derivation).
10. `test_brain_symbol_override` — explicit valid (`MCL`+`CL`) passes; incompatible (`CL`+`ES`) raises.
11. `test_derive_model_symbol_table` — `E2E_CL_HourSet_14B_long_average_precision`→CL; `E2E_ES_HourSet_01B_long_logloss`→ES; `E2E_HourSet_08_long_logloss`→None; `EXP-025_S_Ultimate_OOS`→None; `sweep_h4s01_3x1_96h`→None; `_long_logloss`→None; None/""→None.
12. `test_model_symbol_mismatch_raises` — exec CL + `E2E_ES_*` → ValueError (reproduces the shipped ES01B bug).
13. `test_model_symbol_match_micro` — exec MCL + `E2E_CL_*` → passes.
14. `test_explicit_model_symbol_field_enforced` — `models.long.symbol: "ES"` + exec CL → raises even with tag-free experiment_id (T6 forward-compat).
15. `test_all_shipped_configs` — glob `configs/strategies/*.json`; every config with execution_symbol resolving to brain CL passes `resolve_instrument_context`; assert `ES01B_Sharpe_E03_07042026.json` RAISES as-is (documents intended failure until T6 regeneration).

Integration:
16. `test_live_trader_requires_execution_symbol` — `LiveTrader(strategy=DummyStrategy(config={}), ...)` raises ValueError; with `config={"execution_symbol": "CL"}` constructs (update `tests/test_live_macro_refresh.py` fixtures accordingly).
17. `test_cli_fails_fast_pre_connect` — `cli.main()` with a config missing execution_symbol raises before `DataFeedFactory.create` is called (mock factories, assert not called).

## 7. Deviations from the blueprint's T1 sketch (with justification)

1. **Opportunistic (not mandatory) experiment_id symbol validation** — the fleet's experiment_ids are heterogeneous and post-T6 tags will be symbol-stripped (§3a); mandatory parsing would either break 10+ CL configs or force fake tags. Hard enforcement is provided via the optional explicit `models.*.symbol` field the moment T6 starts writing it.
2. **Micros as first-class registry entries** (MCL/MES/MNQ/MGC/SIL with `micro_of`) rather than a `micro sibling` string on parents — `execution_symbol:"MCL"` must resolve through `get_instrument` for T1 validation and for T2/T3 to read tick/exchange/multiplier; a sibling string on CL alone can't do that.
3. **`ensemble2_opt.json` migrated** (one line) instead of grandfathering — keeps the required-field rule unconditional (no-silent-null-defaults).
4. **CL bars_per_day pinned to legacy 288/24** (not the true 276/23) and `roll_buffer_days=6` (= current `_EXPIRY_BUFFER_DAYS`) so T2/T5 consumption is provably behavior-neutral for CL; documented as provisioning floors.
5. **`cli.py --seed-path/--cache-path` defaults untouched** (blueprint arch item 9 assigns path derivation to T2; touching them in T1 without DataManager changes would break CL).
6. **PA tick correction proposed opportunistically** (registry error found during spec verification; not in blueprint).

## 8. Open questions requiring human authorization

1. **PA tick fix** (0.05/$5.00 → 0.10/$10.00): approve inclusion in T1? Touches training-side metadata for a symbol with no pipeline.
2. **Intended startup failure of current `ES01B_Sharpe_E03_07042026.json`** post-T1 (until T6 regenerates it): confirm acceptable sequencing (T1 may land before T6).
3. **GVZ on IBKR**: registry stores `live_vol_index="GVZ"`; actual fetchability (CBOE index permissions on the data account) is a T4 runtime concern — confirm the account has CBOE index data.
4. **ES afternoon micro-halt** (blueprint mentions a 16:15-16:30 CT pause): session strings in T1 are provisioning-grade; T5 will use IB `contractDetails.tradingHours` as authority. Confirm no one consumes `session_hours_ct` for gating before T5.
5. **Recommend T6 emit `models.*.symbol` explicitly** so validation stops depending on tag parsing — endorse now so T6's spec includes it.
