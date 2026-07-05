# T5 Audit — Hours/Watchdog/Rollover Correctness
**Ticket:** `t5-hours-watchdog-rollover_07042026_2305`
**Auditor:** Ticket-Auditor | 2026-07-04T23:21 PT | HEAD `7a861bb` (branch `development`, T1-T4 + modify_order fix merged)
**Mode:** READ-ONLY RCA + design. No source modified.

---

## 1. Verification of problem areas at HEAD (exact sites)

All blueprint line references re-verified and re-anchored at `7a861bb`.

| # | Site (HEAD) | Finding |
|---|---|---|
| 1a | `src/live_execution/live_trader.py:3928-3959` `_get_market_status(utc_now)` | **CONFIRMED** — `@staticmethod`, CL-only calendar hardcoded in ET via pytz (`America/New_York`): Sat → weekend; Sun `hour<18` → weekend; Fri `hour>=17` → weekend; Mon-Thu `hour==17` → daily halt; else `"OPEN"`. Exact strings: `"OPEN"`, `"CLOSED (weekend — opens Sun 6pm ET)"`, `"CLOSED (daily halt 5-6pm ET)"`. No instrument parameter anywhere. |
| 1b | Consumers of 1a | Exactly two: `_log_heartbeat` `:3762-3763` (prints `market=%s` at `:3795` — the heartbeat line from the ticket) and `_check_stale_bars` `:3891-3894` (gate `!= "OPEN"` → return False). The hourly Telegram heartbeat (`_build_heartbeat_payload` `:522-598`) does NOT include market status (only a static `"Market Closed / No Data"` when inference never ran). One test seam: `tests/test_live_macro_refresh.py:80` assigns `trader._get_market_status = MagicMock(return_value="OPEN")` (instance-attribute shadowing — survives a staticmethod→method conversion). |
| 2 | `live_trader.py:138` `_STALE_BAR_THRESHOLD_MINUTES = 15`; `_check_stale_bars` `:3874-3926` | **CONFIRMED** — fixed 15-min threshold measured from `_last_bar_time_5m` (bar OPEN timestamp of the last COMPLETED 5m bar, set at `:2770` / warm-start `:1951`). Trigger path: event loop `:3699-3720` → `_reconnect()` (15 attempts, exp backoff `:3515-3627`) → full-restart escalation. Fires Telegram `:3911-3915` and force-disconnects both clients `:3920-3925`. With a CL calendar, a ZC instance during the 13:20→19:00 CT halt reads `OPEN` + stale>15 → **reconnect every ~15 min for ~5.7 h/day + 45 min morning halt** (M4 confirmed). |
| 2b | Watchdog cadence | `_event_loop` `:3633-3748`: poll 5 s (`_POLL_INTERVAL` `:121`), heartbeat+rollover+watchdog every 60 cycles ≈ 5 min (`_HEARTBEAT_CYCLES` `:3640`). |
| 2c | **NEW FINDING (pre-existing CL defect, dormant)** | The watchdog has a reopen false-positive: at daily reopen (Mon-Thu 18:00 ET) `_last_bar_time_5m` is the 16:55 ET bar → stale ≈ 65 min while status flips to OPEN; the first completed bar lands ~18:05. The 18:00→18:05 window always contains one 5-min watchdog tick → a **spurious disconnect/reconnect (+Telegram) at virtually every CL daily reopen and Sunday open**. Self-healing (reconnect succeeds, backfills). Same math applies on CME holidays (calendar says OPEN all day → repeated cycles). Pre-existing, NOT introduced by T1-T4; for grains it would fire after BOTH daily halts. See §9-Q1. |
| 3a | `src/live_execution/ibkr_client.py:657` `_EXPIRY_BUFFER_DAYS = 6` (class attr); `get_front_month_contract` `:659-725`, async `:727-774` | **CONFIRMED** — selection = sort ALL `reqContractDetails` results by `lastTradeDateOrContractMonth` (`:693`), keep those with LTD-string `>= (utcnow+6d)` (`:695-702`), pick first; fallback `details[0]` (`:704-713`). No active-month filter → GC/SI serial months selectable (B8a); one buffer for all instruments (ES needs 8; FND-referenced physicals need FND-anchored buffer — B8b/c). Registry `active_months`/`roll_reference`/`roll_buffer_days` (T1, `instrument_master.py:15-17`) consumed by **nothing** (confirmed: only tests reference them). `symbol: str = "CL"` silent default remains at `:660`/`:728` (all 5 src callers pass `symbol=` explicitly — adapter `:127,171,220`, exec `:73`, live_trader `:682,2271`). `month_str = LTD[:6]` (`:716`) — note: for CL this is the LTD month (delivery month −1); it feeds `_front_month_str` → telemetry `contract_month` + logs, so it is PINNED (see §5). Test pin `tests/test_build_future_contract.py:390` asserts `_EXPIRY_BUFFER_DAYS == 6` and self-documents "T5 changes the source deliberately". |
| 3b | Front-month consumers (flow) | `start()` Step 4 `:680-696` (exec symbol) → `_front_month_local_symbol/_str`; Step 4b `:702-708` `exec_client.resolve_contract` (`adapters/ibkr_execution.py:65-85`, same manager call — orders use this cached contract); Step 6 `:713-721` → `DataManager.front_month_id`; hands-stream subscribe `adapters/ibkr_data_feed.py:127,171`; daily `_check_contract_rollover` `:2243-2398` (UTC-day gate `:2263-2266`; force-close via T3's registry-exchange `close_position`; DM update `:2365-2368`; hands resubscribe); reconnect path `_resubscribe_and_backfill` `:2678` re-checks rollover. |
| 4a | `src/live_execution/data_manager.py:133` `_ROLL_PRICE_TOLERANCE = 0.01` | **CONFIRMED** — comment at `:132` says "price difference ($)" but it is used as a RATIO band at `:271` (cache adjust), `:654` (roll-history append), `:947` (full-ledger scale). 1% swallows ES quarterly (~0.2-0.7%) and GC (~0.5-1%) gaps → silent roll-skip → price seam in every rolling feature (M6 confirmed). |
| 4b | `data_manager.py:170,196` `bars_per_day` ctor param | **DEAD PARAMETER** — stored, never read anywhere in the module (grep-verified). live_trader passes literals `288` (`:373`) / `24` (`:423`). Module constant `_BARS_PER_DAY = 288` (`:121`) also dead; `_MAX_IB_REQUEST_DAYS` (`:127`) dead (chunking was replaced by single NOW-anchored requests `:569-573`, `:1053-1057`). `tests/test_symbol_data_paths.py:678,688` pin the CL kwargs 288/24. |
| 4c | `data_manager.py:118` `_SEED_LOOKBACK_DAYS = 280` (used `:470,:476`) + `live_trader.py:2002-2014` 4320-bar RAISE | **CONFIRMED** — 280 calendar days is derived from CL's 24 h/23 h day (comment `:110-117`: 4320 ÷ ~115 bars/wk ≈ 263 d + buffer). ZC/ZS at 16 bars/day: 280 d ≈ 40 wk × 80 bars ≈ 3,200 bars < 4,320 → **guaranteed startup RuntimeError** `"CACHE VALIDATION FAILED"` (M5). The raise message `:2010` hardcodes `warm_start_cache_1h.parquet` — wrong filename for non-CL (T2 caches are `warm_start_cache_{SYM}_1h.parquet`). Reconnect gap backfills (`:2546-2548`, `:2609-2611`) and DataManager backfills are calendar-day-based → session-agnostic → safe for grains (no change needed). |
| 4d | T2-deferred front_month_id cross-talk (C2) | **CONFIRMED** — comment block `live_trader.py:356-364` + shared per-brain file `:370/:420` (`derive_data_paths` → CL legacy `.roll_metadata.json`, `data_manager.py:90-98`). `DataManager.front_month_id` (`:194`, "e.g. CLJ6") holds the EXECUTION localSymbol; `_save_roll_metadata` `:667` writes it to `last_front_month`; `_detect_rollover` `:720-745` compares raw strings. Concurrent CL+MCL instances (both brain=CL → same file) ping-pong `CLQ6` ↔ `MCLQ6`: every restart flags a phantom "ROLLOVER" (`:742-744`), computes a ~1.0 ratio (no adjustment — within tolerance), and **spams a rollover cache backup per restart** (`initialize` Step 6 `:298-301` backs up on `_roll_detected` regardless of ratio) plus WARNING logs. Detection noise, not misdata — as T2's impact review classified. |
| 5 | Session-hours source | Registry `session_hours_ct` (`instrument_master.py:18-19`, `_GLOBEX_SESSION :27`, `_GRAINS_SESSION :29`) exists and is consumed by nothing; T1 comment says "T5 uses IB tradingHours as authority" — decision made in §4e below: **registry-driven now**, IB tradingHours rejected for this ticket. |

## 2. End-to-end flow map (who calls what, when)

```
start() [:624]
 ├─ connect data+exec [:646-648]
 ├─ Step 4  get_front_month_contract(execution_symbol)  [:680-696]  ── IBKRConnectionManager :659
 ├─ Step 4b exec_client.resolve_contract(execution_symbol) [:702]    ── ibkr_execution :65 (same selection)
 ├─ Step 6  DataManager.front_month_id ← localSymbol [:713-721]
 ├─ Step 8  _warm_start [:1935] ─ DataManager.initialize [:208]
 │            ├─ _load_roll_metadata → restore ratios [:216-222]
 │            ├─ seed (_seed_from_csv trims _SEED_LOOKBACK_DAYS=280) [:415-481]
 │            ├─ _detect_rollover (front_month_id vs file last_front_month) [:263-278,:720]
 │            │    └─ _compute_roll_ratio ("3 D" overlap median) [:747] → |r−1|>0.01 → _apply_roll_to_cache [:804]
 │            ├─ _backfill (calendar-day duration, NOW-anchored) [:546]
 │            ├─ _update_training_ledger (scales ENTIRE ledger by ratio when roll+ratio>tol) [:914-964]
 │            ├─ _save_roll_metadata (last_front_month ← front_month_id) [:641-680]
 │            └─ backup-on-roll [:297-301]
 │          then 1h-cadence guard [:1969-1995] and 4320-bar RAISE [:2002-2014]
 ├─ subscribe brain (5m/1h continuous, brain_symbol) + hands (front-month, exec symbol) [:772-777]
 └─ _event_loop [:3633]  (poll 5 s)
      every 60 polls (≈5 min):
        ├─ _log_heartbeat → _get_market_status(now) → "HEARTBEAT ... market=%s" [:3750-3803]
        ├─ _check_contract_rollover (once per UTC day) [:2243]
        │     front-month changed → force-close position → resubscribe hands → DM.front_month_id ← new
        └─ _check_stale_bars [:3874]: status=="OPEN" AND now−last_bar_5m ≥ 15 min
              → telegram + disconnect → _reconnect [:3515] (15 attempts) → _resubscribe_and_backfill [:2668]
                   └─ _check_contract_rollover again [:2678]; gap backfill (calendar-day durations) [:2523]
      async error path: 1101/2104/2106 → _deferred_resubscribe [:2446] → _backfill_reconnect_gap_async
```

## 3. Parity/livetest coverage of this logic

`scripts/livetest_engine.py` `_bootstrap_trader` (`:173-264`) **bypasses `start()` and `_event_loop` entirely**: it sets `_front_month_local_symbol="CLZ9"` manually, replaces `data_manager_1h` with `_MockDataManager`, and drives bars by firing `updateEvent` directly. Therefore:
- `_get_market_status`, `_check_stale_bars`, `_check_contract_rollover`, `DataManager.initialize` (roll detection/tolerance/seed-lookback) **never execute in the parity gate**.
- The gate DOES execute `LiveTrader.__init__` (instrument resolution, DataManager construction args, 1h seed/cache existence check `:399-411`) and the bar/order callbacks.
- `SimulatedDataFeed` (`adapters/simulated_data_feed.py`) keeps its own `get_front_month_contract(symbol="CL")` mock (`:205-207`) — untouched by design (T2 C6 precedent).

**Implication:** CL byte-identity for the watchdog/calendar/rollover semantics cannot ride on the parity gate; it must be pinned by dedicated frozen-clock unit tests (§7). The parity gate remains mandatory post-green because `__init__`/DataManager constructor args change (§5).

## 4. Design (localized; no refactor of stable modules)

### 4a. Market-status determination — NEW leaf module `src/live_execution/session_calendar.py`
Two explicit calendars dispatched on the registry `session_hours_ct` tuple; **no generic segment engine** (only two shapes exist; a table-driven engine adds CL-pin risk for zero current benefit):

```python
def market_status(instrument: Instrument, utc_now: datetime) -> str
def session_open_anchor(instrument: Instrument, utc_now: datetime) -> Optional[datetime]  # tz-naive UTC
```
- `session_hours_ct == _GLOBEX_SESSION` → `_globex_market_status(utc_now)`: the CURRENT `_get_market_status` body moved **verbatim** (pytz ET, same branch order, same three strings) → CL/MCL/ES/NQ/GC/SI/… byte-identical by construction. `session_open_anchor` → `None` (legacy stale semantics preserved).
- `session_hours_ct == _GRAINS_SESSION` → `_grains_market_status(utc_now)` evaluated in `America/Chicago`:
  - Sat → `"CLOSED (weekend — opens Sun 7pm CT)"`; Sun `< 19:00` → same; Fri `>= 13:20` → same.
  - Mon-Fri `[07:45, 08:30)` → `"CLOSED (daily halt — reopens 8:30am CT)"`.
  - Mon-Thu `[13:20, 19:00)` → `"CLOSED (daily halt — reopens 7pm CT)"`.
  - else `"OPEN"` (incl. Mon-Fri 00:00-07:45 overnight tail and Sun/Mon-Thu ≥19:00).
  - `session_open_anchor` returns the most recent session-open instant (08:30 CT, 19:00 CT, or Sun 19:00 CT) as tz-naive UTC.
- Any other tuple → `ValueError("Unsupported session_hours_ct {…!r} for {symbol}: no calendar implementation. Supported: GLOBEX (('17:00','16:00'),) and GRAINS (('19:00','07:45'),('08:30','13:20')).")` — no silent default.
- Known accepted limits (parity with today's CL): no exchange-holiday awareness, no early closes. Documented in the module docstring.

`live_trader.py`: `_get_market_status` converts from `@staticmethod` to an instance method delegating to `market_status(self._brain_instrument, utc_now)` (bars are BRAIN-stream; `_brain_instrument` seam from T4 `:2153-2174` handles `object.__new__` test stubs). The existing instance-attribute mock seam (`test_live_macro_refresh.py:80`) keeps working.

### 4b. Stale-bar watchdog — session-aware gate + grains reopen grace
- `_STALE_BAR_THRESHOLD_MINUTES = 15` **unchanged for all instruments**: once status is session-aware, the expected bar gap is bar-size-driven (5m everywhere), not instrument-driven. (Deviation from the sketch's "expected-bar-gap × margin" — see §8-2.)
- `_check_stale_bars` change (only lines touched):
  ```python
  anchor = session_open_anchor(self._brain_instrument, now)   # None for GLOBEX instruments
  reference = last_bar_time if anchor is None or anchor <= last_bar_time else anchor
  minutes_stale = (now - reference).total_seconds() / 60
  ```
  Grains: after each halt the stale clock restarts at the session open → no guaranteed false trigger at 08:30/19:00 CT reopen. GLOBEX (incl. CL): `anchor is None` → arithmetic bit-identical to today, **including the pre-existing reopen false-positive (§1-2c), which is deliberately pinned, not fixed** (Q1).
- Log/Telegram strings in the trigger path unchanged.

### 4c. Front-month selection — active months + per-instrument roll reference/buffer
`ibkr_client.py`: extract one pure, IB-free helper used by BOTH sync and async methods:

```python
_MONTH_CODES = "FGHJKMNQUVXZ"           # index = month-1
def _select_front_month(details: list, instrument: Instrument, now_utc: datetime) -> ContractDetails
def _first_notice_proxy(contract_month: str) -> date   # last Mon-Fri of the preceding month
```
Selection (replaces `:693-713` body in both methods):
1. Sort by `lastTradeDateOrContractMonth` (unchanged).
2. **Active-month filter** — SKIPPED ENTIRELY when `set(instrument.active_months) == set("FGHJKMNQUVXZ")` (CL/MCL/NG: `contractMonth` never touched → byte-identical). Otherwise: month code from `ContractDetails.contractMonth` (`"YYYYMM"` → `_MONTH_CODES[mm-1]`); a detail missing/blank `contractMonth` on a restricted instrument → `ValueError("Contract details for {symbol} ({localSymbol}) missing contractMonth — cannot apply active-month filter '{active_months}'.")` — no silent pass-through.
3. **Buffer filter** by `instrument.roll_reference`:
   - `"LTD"`: keep the legacy STRING comparison verbatim — `d.contract.lastTradeDateOrContractMonth >= (now_utc + timedelta(days=instrument.roll_buffer_days)).strftime("%Y%m%d")`. CL buffer=6 → bit-identical; ES/NQ buffer=8 → volume-roll ~8 days pre-expiry (B8c).
   - `"FND"`: eligible iff `_first_notice_proxy(contractMonth) >= (now_utc + timedelta(days=instrument.roll_buffer_days)).date()`. Proxy = last weekday of the month preceding delivery (GC/SI/HG/ZC/ZS spec); no holiday calendar — `roll_buffer_days=3` absorbs the ≤2-day holiday shift (documented). Avoids IBKR near-FND force-liquidation (B8b).
   - Any other value → `ValueError` (no silent default).
4. Fallback when the buffer filter empties: first ACTIVE-month detail (warning, as today); if the active-month filter itself empties → `RuntimeError("No {symbol} contract in an active month ('{active_months}') among {n} contract details from IBKR — registry/venue mismatch.")` (loud; a silent serial-month pick is exactly bug B8a). CL fallback = `details[0]` as today.
5. Return contract; `month_str` stays `lastTradeDateOrContractMonth[:6]` — **unchanged semantics** (CL telemetry `contract_month`/log pin; see Q5).
- `_EXPIRY_BUFFER_DAYS` class attr **deleted** (source of truth = registry `roll_buffer_days`; the T2-era pin test `test_build_future_contract.py:390` is updated — its own comment anticipates this).
- The silent `symbol: str = "CL"` defaults on `get_front_month_contract(_async)` (`:660,:728`) **removed** (all 5 src callers already pass `symbol=`; `SimulatedDataFeed` untouched).
- Log line format unchanged (`buffer=%dd` now prints the registry value — 6 for CL, byte-identical).

### 4d. Roll-metadata per-execution-symbol normalization (T2 C2 deferral)
No identifier parsing/rewriting (localSymbol formats differ per venue — CBOT grains are not `ZCZ6`-shaped). Namespace the file instead:
- `DataManager.__init__` gains keyword-only `execution_symbol: Optional[str] = None` → `self.execution_symbol = execution_symbol or symbol`. This is the same **structural derivation** pattern as T2's `_brain_symbol` (outright: exec==brain; only live_trader constructs micro configs and it passes explicitly) — NOT a silent default; zero churn in the ~16 existing test constructions.
- `_save_roll_metadata`: additionally write `meta["last_front_month_by_symbol"][self.execution_symbol] = self.front_month_id`, merging (not replacing) other symbols' entries; legacy `last_front_month` still written exactly as today (CL-only fleets: file gains one redundant key, behavior unchanged).
- `_detect_rollover` read order: (1) `last_front_month_by_symbol[self.execution_symbol]` if present; (2) else legacy `last_front_month` **only if** `last_front_month.startswith(self.execution_symbol)` (ownership check — `"CLQ6".startswith("MCL")` False, `"MCLQ6".startswith("CL")` False → no cross-reads); (3) else first-run path (return False, as `:729-734` today).
  - CL restart on an existing legacy file: by_symbol absent → legacy `"CLQ6"` startswith `"CL"` → identical comparison to today.
  - MCL first run against a CL-written file: ownership check fails → first-run (today: phantom roll + backup spam). Ping-pong dead from the first run of the new code.
- `roll_history` entries become per-execution-symbol consistent (from/to both from the same namespace). Known residual: concurrent same-second startups still last-writer-wins on the whole JSON (startup-only write, tiny window) — documented, no file locking added.
- live_trader `:365-374`/`:415-424`: pass `execution_symbol=self._instrument_context.execution_symbol`; the C2 comment block `:356-364` is replaced by a short "resolved in T5" note.

### 4e. Seed-lookback + bars_per_day via registry
- Decision (source-of-truth question from the ticket): **registry-driven session/bars facts, not IB `contractDetails.tradingHours`.** Justification: deterministic frozen-clock testability; no runtime/order-of-operations dependency (status is consulted before/independently of any qualified contract); parity-safe; IB hours strings (`"20260706:1700-20260707:1600;…"`) would require a parser + fallback when absent/misformatted — a silent-default hazard the house rule forbids; IB's one advantage (holiday awareness) is a behavior CL never had (§1-2c). Hybrid observability (log-only divergence check at qualification) explicitly deferred (ops/T7 flavor, out of scope).
- `data_manager.py`:
  - `REQUIRED_1H_BARS = 4320` (module constant; live_trader's `_min_required` dict imports/references it — value unchanged).
  - `def derive_seed_lookback_days(bars_per_day_1h: int) -> int: return ceil(ceil(REQUIRED_1H_BARS / bars_per_day_1h) * 7 / 5) + 28` — raises on `bars_per_day_1h <= 0`. **CL: ceil(4320/24)=180 trading days → ×7/5 = 252 → +28 = 280 — reproduces the legacy constant EXACTLY** (the 28-day buffer is the legacy 280−252 holiday margin, now explicit). ZC/ZS(16) → 406; ES/GC/…(23) → 292. ZC sanity: 406 d ≈ 277 trading days × 16 ≈ 4,430 bars ≥ 4,320 (holiday-adjusted) — and the 4320 RAISE still guards the floor.
  - `_SEED_LOOKBACK_DAYS` module constant → `self.seed_lookback_days = derive_seed_lookback_days(get_instrument(symbol).bars_per_day_1h)` used at `:470/:476` (both 5m and 1h managers — a day-window is bar-size independent, exactly like the legacy shared constant).
  - `_ROLL_PRICE_TOLERANCE` module constant → `self.roll_ratio_tolerance = get_instrument(symbol).roll_ratio_tolerance` at the three use sites; the misleading "$" comment dies with the constant.
  - Delete dead `_BARS_PER_DAY` and `_MAX_IB_REQUEST_DAYS`; keep the `bars_per_day` ctor param (pinned by tests) but live_trader now feeds it from the registry.
- `instrument_master.py`: append field `roll_ratio_tolerance: float` (inserted before the defaulted `micro_of`/`slippage_ticks`; all 15 entries updated explicitly — no dataclass default, per house rule). Values: **CL/MCL `0.01` (legacy pin)**; all others `0.001` (10 bps — below ES ~20-70 bps and GC ~50-100 bps real roll gaps; overlap-median noise on identical timestamps is ~0, so 10 bps cannot misfire) — needs sign-off (Q2).
- `live_trader.py` `:373/:423`: `bars_per_day=self._instrument_context.brain_instrument.bars_per_day_5m` / `.bars_per_day_1h` (CL: 288/24 → the `test_symbol_data_paths.py:678,688` pins stay green; ES assertions updated 276/23 — mechanical).
- 4320 RAISE message `:2007-2011`: `f"Delete {self.data_manager_1h.cache_path.name} to trigger reseed."` — CL cache name IS `warm_start_cache_1h.parquet` → **byte-identical CL text**; ZC names its real cache. Message remains a hard RuntimeError (a short ZC seed must still crash with actionable text, per M5's "or raises with an actionable message" option — the derived 406-day lookback makes a compliant seed pass, ops supplies the seed depth in T7).

### Files touched (complete list)
| File | Change |
|---|---|
| `src/live_execution/session_calendar.py` | **NEW** leaf module (stdlib + pytz + instrument_master only) |
| `src/live_execution/live_trader.py` | `_get_market_status` → instance delegate; `_check_stale_bars` grace anchor; DM ctor args (execution_symbol, registry bars_per_day); 4320 message cache-name; C2 comment swap |
| `src/live_execution/ibkr_client.py` | `_select_front_month` + `_first_notice_proxy` helpers; both front-month methods routed through them; `_EXPIRY_BUFFER_DAYS` deleted; CL symbol defaults removed |
| `src/live_execution/data_manager.py` | instance-derived seed lookback + roll tolerance; roll-metadata namespace; dead constants removed |
| `src/core/instrument_master.py` | `roll_ratio_tolerance` field (15 entries) |
| Tests | new `tests/test_session_calendar.py`, `tests/test_front_month_selection.py`, extensions to `tests/test_rollover.py`/`test_symbol_data_paths.py`; pin update in `test_build_future_contract.py:390` |

Out of scope honored: NO generator/`batch_post_optimizer` (T6), NO `fleet_runner`, NO backtest engine, NO macro (T4 done), NO ops/data work (T7), NO `SimulatedDataFeed` edits, NO ExecutionGuard changes (config-driven, m5).

## 5. Hard-constraint compliance (CL byte-identity)

| Pinned surface | Mechanism |
|---|---|
| Heartbeat `market=%s` strings | GLOBEX calendar body moved verbatim; pin tests assert the three exact strings at frozen instants + a minute-by-minute equivalence sweep vs a frozen legacy copy across a DST-straddling year |
| Watchdog threshold + arithmetic | 15 min unchanged; `anchor=None` for GLOBEX → identical expression; reopen false-positive explicitly pinned (test 8) |
| Front-month selection | all-12-months short-circuit (no `contractMonth` reads); LTD string-compare + buffer 6 verbatim; fallback `details[0]`; `month_str=LTD[:6]`; log format unchanged |
| Rollover behavior | CL tolerance 0.01 via registry; metadata legacy field written unchanged; legacy-file read path identical for CL |
| Seed lookback | formula reproduces 280 exactly (pinned) |
| Telemetry/Telegram | no string changes on any CL path |
| Parity gate | `__init__`/DataManager arg changes → **HS14B ledger parity gate re-run mandatory post-green** (T1 C3 / T2 C7 convention); watchdog/calendar are NOT covered by the gate (§3) — unit pins are the regression fence |

## 6. Severity + regression classification

- **Severity: MEDIUM/HIGH** (multi-file, structural logic — routed for impact review + human authorization per T2 multi-component precedent). Business justification: M4 (grain reconnect storms ~6 h/day + Telegram flood), M5 (guaranteed ZC/ZS startup crash), M6 (silent ES/GC roll-skip → feature seams → model misdata), B8 (GC/SI illiquid serial-month selection + FND force-liquidation risk) — all launch-blocking for the ZC canary (memory: ZC stood up 2026-07-04) and every non-CL symbol.
- **Regression: NO.** All confirmed defects are latent CL-shaped assumptions predating T1 (git history: watchdog/calendar/rollover bodies untouched by T1-T4). Two pre-existing dormant CL defects newly documented, neither fixed here without authorization: (i) reopen/holiday watchdog false-positive (§1-2c, Q1); (ii) `bars_per_day` dead parameter (cosmetic, now fed real values).

## 7. TDD test list (Strict-Lock; frozen clocks via injected `utc_now` — pure functions need no monkeypatching)

**Session calendar (`tests/test_session_calendar.py`):**
1. CL pins (exact strings): Sat 12:00 ET → weekend; **Sun 17:30 ET → `"CLOSED (weekend — opens Sun 6pm ET)"`** (ticket case); Sun 18:00 ET → OPEN; Mon 17:30 ET → `"CLOSED (daily halt 5-6pm ET)"`; Fri 16:59 ET → OPEN; Fri 17:00 ET → weekend.
2. CL equivalence sweep: generic entrypoint vs frozen verbatim legacy copy, every 5-min instant over 2026 (incl. both DST transitions) — string-equal.
3. ES/GC/SI dispatch to GLOBEX calendar (same statuses as CL at same instants).
4. ZC/ZS grains: **Tue 15:00 CT → CLOSED daily-halt** (ticket case); Tue 08:00 CT → CLOSED halt; Tue 08:30 CT → OPEN; Tue 13:20 CT → CLOSED; Tue 20:00 CT → OPEN; Wed 03:00 CT → OPEN; Fri 07:00 CT → OPEN; Fri 13:20 CT → weekend; Sun 18:30 CT → weekend; Sun 19:00 CT → OPEN; Sat → weekend; repeated in Jan (CST) and Jul (CDT).
5. `session_open_anchor`: GLOBEX → None always; ZC Tue 19:02 CT → that day 19:00 CT (UTC-naive); Tue 09:00 CT → 08:30 CT; Mon 03:00 CT → Sun 19:00 CT.
6. Unknown session tuple → ValueError naming the symbol and supported shapes.

**Watchdog (`LiveTrader` with mocked clients, frozen `datetime`):**
7. ZC Tue 15:00 CT, last bar 13:15 CT (stale 105 min) → `_check_stale_bars() is False`; no telegram, no disconnect (ticket case).
8. CL pins: Mon 12:00 ET stale 16 min → True (disconnect+telegram); stale 10 min → False; **Mon 18:03 ET last bar 16:55 ET → True** (pre-existing reopen behavior PINNED — flips only under Q1 authorization).
9. ZC grace: Tue 19:02 CT last bar 13:15 → False; Tue 19:20 CT last bar 13:15 → True (open 20 min, no bars).
10. `_STALE_BAR_THRESHOLD_MINUTES == 15` pin; `_last_bar_time_5m is None` → False (unchanged).

**Front month (`tests/test_front_month_selection.py`, mocked ContractDetails):**
11. **GC serial filtering** (ticket case): details [F7 serial, G7, H7 serial, J7] → G7 picked; serials never selected even when nearest-expiry.
12. GC FND buffer: now ≥ FND(G7)−3d → G7 skipped → J7 (not serial H7).
13. `_first_notice_proxy`: month starting Mon/weekend cases (e.g. 202603 → 2026-02-27 Fri; 202612 → 2026-11-30 Mon).
14. **ES quarterly** (ticket case): ESU6 LTD 20260918 + ESZ6: now=2026-09-10 → ESU6; 2026-09-11 → ESZ6 (8-day buffer).
15. Restricted instrument, detail missing `contractMonth` → ValueError; all details inactive months → RuntimeError.
16. CL pins: all-12 short-circuit (details WITHOUT `contractMonth` still work); LTD==cutoff-date boundary still eligible (string `>=`); all-near-expiry fallback → `details[0]` + warning; `month_str == LTD[:6]`; sync and async both route through `_select_front_month`; `get_front_month_contract()` without symbol → TypeError; `_EXPIRY_BUFFER_DAYS` attr gone (replaces `test_build_future_contract.py:390` pin per its own comment).

**Roll metadata (extend `tests/test_rollover.py`):**
17. CL legacy file (`last_front_month="CLQ6"`, no by_symbol) + fm="CLQ6" → no roll; after save file has legacy field unchanged AND `by_symbol == {"CL": "CLQ6"}`.
18. Cross-talk kill: by_symbol {"CL":"CLQ6","MCL":"MCLQ6"} → CL fm="CLQ6" no roll; MCL fm="MCLQ6" no roll; CL fm="CLU6" → roll detected.
19. Migration: legacy-only "CLQ6" + MCL fm="MCLQ6" → first-run (False), no backup spam; MCL entry written on save.
20. `execution_symbol=None` → equals `symbol` (outright); explicit value wins.

**Tolerance/lookback (extend `test_rollover.py`/`test_data_manager_ratio.py` + registry tests):**
21. Registry: CL and MCL `roll_ratio_tolerance == 0.01` (pin); every entry `0 < tol < 0.02`; ES/GC == 0.001.
22. **ES ratio 1.004 IS applied** (ticket case: cache adjusted, ledger scaled, roll_history appended); CL 1.004 skipped (pin); CL 1.02 applied (pin).
23. `derive_seed_lookback_days`: **24 → 280 (CL pin)**; 16 → 406; 23 → 292; 0/−1 → raise.
24. ZC DataManager seeds from a synthetic 450-day 1h parquet → trimmed window ≈ last 406 days; CL → 280 (pin).
25. 4320 RAISE: CL message contains `"Delete warm_start_cache_1h.parquet to trigger reseed."` byte-identical; ZC message names `warm_start_cache_ZC_1h.parquet`.

**LiveTrader integration (extend `test_symbol_data_paths.py`):**
26. CL DM kwargs pins stay green (288/24, paths, roll metadata — existing tests 662-688 untouched); ES kwargs 276/23 + `execution_symbol="ES"`; ZC kwargs 200/16; MCL config → DM `symbol="CL"`, `execution_symbol="MCL"`.
27. `trader._get_market_status` instance-mock seam still effective (existing `test_live_macro_refresh.py` stays green).

**Post-green (manager, not coder):** HS14B ledger parity gate re-run (`setup --disable-trailing`, 2200 warmup + 336 replay) — `__init__`/DataManager spine changed; any non-PASS is a T5 regression.

## 8. Deviations from the T5 blueprint sketch (justified)

1. **Registry-driven sessions, not IB `contractDetails.tradingHours`** (blueprint arch item 8 suggested IB). Deterministic frozen-clock tests, no runtime dependency, no parser/fallback silent-default hazard, CL pin trivially provable. IB-hours divergence logging deferred as ops observability.
2. **Watchdog threshold NOT per-instrument** (sketch: "threshold derived from the instrument's session map"). With session-aware status + grains open-anchor, the expected bar gap while OPEN is 5m for every registry symbol → 15 min stands globally; CL pin free. The session map is consumed by status + anchor instead of by a retuned threshold.
3. **Seed lookback stays in days, derived per instrument** (blueprint M5 offered "lookback in bars"). The day-based trim in `_seed_from_csv` is shared 5m/1h machinery; a formula reproducing CL's 280 exactly gives the fix with zero semantic rewrite.
4. **Roll tolerance as a per-instrument ratio (registry field), not ticks** (blueprint M6 said "ticks or bps"). The comparison is already ratio-space; ticks would need a price denominator that doesn't exist at the call sites.
5. **Two explicit calendars, not a generic session-segment engine.** Only two session shapes exist in the registry; unknown shapes raise. A generic engine is more code between CL and its pin.
6. **front_month_id normalization by per-symbol namespace + ownership check, not identifier rewriting** (T2 audit had floated normalizing the stored identifier). localSymbol formats are venue-dependent (CBOT grains are not `ZCZ6`-shaped); parsing is fragile. Namespacing kills the ping-pong with zero change to CL file semantics and no format assumptions.
7. **`month_str` (delivery-month display) NOT corrected to `contractMonth`** — it is CL-pinned via telemetry `contract_month` and logs (Q5).

## 9. Open questions requiring human authorization

- **Q1 — CL reopen/holiday watchdog false-positive (pre-existing, §1-2c).** The grains grace-anchor mechanism would fix it for CL with one change (GLOBEX `session_open_anchor` returning 18:00 ET/Sun-open instead of None), eliminating the near-daily spurious reconnect+Telegram at reopen — but that changes pinned CL behavior. Default in this design: **pinned as-is** (test 8). Authorize separately if wanted.
- **Q2 — `roll_ratio_tolerance` values.** New required registry field; CL/MCL keep 0.01 (pin). Proposed 0.001 (10 bps) for all others — below every real roll gap (ES 20-70 bps, GC 50-100 bps), far above overlap-median noise (~0). Approve the number (or supply per-symbol values).
- **Q3 — multi-component approval.** 4 src files + 1 new module + registry field + mechanical test churn (1 pin update in `test_build_future_contract.py`, ES-assertion updates in `test_symbol_data_paths.py`) — same shape T2 required HUMAN AUTHORIZED for.
- **Q4 — ES session fidelity.** Gap-table M4 mentions a 16:15-16:30 CT equity halt; current CME equity-index hours have only the 16:00-17:00 CT maintenance halt, which `_GLOBEX_SESSION` models. Registry kept as-is (T1-verified); confirm no ES-specific halt segment is wanted (would be registry data only — the calendar dispatch already supports adding one later).
- **Q5 — `month_str` stays `LTD[:6]`.** Semantically the LTD month, not the delivery month, for CL/NG (e.g. CLQ6 → "202607"). Kept for CL telemetry/log byte-identity; correcting it is a separate, explicitly-authorized cosmetic change (would touch telemetry history comparability).
