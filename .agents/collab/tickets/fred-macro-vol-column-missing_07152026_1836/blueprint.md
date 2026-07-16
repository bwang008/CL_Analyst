# Ticket Resolution Blueprint — fred-macro-vol-column-missing_07152026_1836
**Ticket Directory:** `.agents/collab/tickets/fred-macro-vol-column-missing_07152026_1836/`

## Bug Summary

The GC live child crashed at the 18:00 bar with `ValueError: FRED macro file ...fred_macro_data_gc.csv is missing required column 'GVZ'`, raised inside an eventkit bar-update callback. SI separately logged `Missing feature columns: {'MACRO_FED_FUNDS'}` and skipped feature generation.

**Root cause (producer-side, confirmed independently by both agents):** `scripts/download_macro_data.py` silently drops a FRED series that fails to fetch and writes the resulting *partial* file anyway, overwriting the known-good one.

- `:126-127` — per-series `except Exception: log.error(...)`, no re-raise, no failure tracking → returns a partial dict.
- `:105-107` — empty response → `log.warning` + `continue`.
- `:134-136` — `save_fred_data` guards only the *fully-empty* dict (`if not data`), never a partial one.
- `:155` — `merged.to_csv(...)` overwrites with no schema validation, non-atomically, with no backup and no retries.
- `:599-612` — `main()` re-implements the writer inline (`if fred_data:` is truthy on any single series), leaving `save_fred_data` dead in the CLI path and skipping its ffill.

**The live trader corrupted its own input.** `save_fred_data` is reachable via `macro_features.py:316-317` `refresh_if_stale()`, called from `live_trader.py:929-931` (startup) and `:5460-5462` (heartbeat). The real corrupt file carries `save_fred_data`'s ffill signature — which the inline CLI writer lacks — proving the trader wrote it, not an operator CLI run. Reproduction: simulating a single `GVZCLS` fetch failure produced a file **byte-identical (401053 bytes)** to the real corrupt `fred_macro_data_gc.csv`.

All six FRED series (`GVZCLS`, `OVXCLS`, `VIXCLS`, `FEDFUNDS`, `DTWEXBGS`, `T10Y2Y`) were probed live and are healthy. The losses were **transient fetch failures**, hence random-per-series (GC lost GVZ, SI lost FED_FUNDS).

**Not a recent regression.** `git blame` confirms the defect dates to `483d17d` (2026-03-22). `669b9ce` (T4, 2026-07-04) only *added* the `macro_features.py:588` hard-raise that **exposed** it — pre-T4, GC would have silently no-traded exactly as SI does now. Nothing to revert.

**Exposure is once per child per day, not hourly.** `refresh_if_stale:288` gates on a 7PM-ET mtime cutoff; the hourly heartbeat only *checks*. (Corroborated by the observed file mtimes 16:01–17:00 PT = just past 19:00 ET.)

**Non-findings, ruled out:** GC/ES identical byte-size is coincidence — both legitimately hold the same 4 columns, and CL/NG are byte-identical too while both legitimately map to `OVXCLS`. No cross-symbol copy path exists; paths derive from `instrument.symbol.lower()`. FED_FUNDS is unconditional in `base_series` (`:88-93`) for every symbol.

### Blast radius

- **GC child is a zombie.** `eventkit/event.py:213-218` catches all handler exceptions and only logs, so the process lives while `_on_new_bar` aborts at feature-build every 1H bar indefinitely: no inference, no entries, no signal/EOD exits. Resting SL/TP OCA orders still bound risk and `_check_trailing_stop` (`:4427`) still runs before the crash point, but the brain no longer manages the position.
- **Invisible to the operator.** Bars keep arriving, the heartbeat keeps logging alive, the bar-arrival watchdog never fires, `_emit_health_event` isn't wired to the bar path, and the error queue's `extract_traceback` reads child stderr **on exit** — a zombie that never exits files nothing.
- **SI = silent no-trade** (`macro_features.py:627` `if "FED_FUNDS" in df.columns:` — a silent-null-default in disguise).
- **Training is also poisoned, partially.** `data_processor.py:3400` reads the same per-symbol CSVs. GC is protected by the `:588` vol raise (loud crash), **but SI's missing FED_FUNDS is unguarded** → any training/backtest/regenerate run for SI from this tree right now silently trains on a reduced feature set, and propagates corrupt → parquet → GCS → every sweep VM. This confirms the producer is the correct layer to fix and raises F5's urgency.

`macro_features.py:588`'s hard-raise is **correct and must be kept** — it is the messenger, and the only reason this surfaced. Weakening it to a warn-skip would turn GC into SI (a silent no-trade) and would also break the existing test at `tests/test_macro_vol_parameterization.py:638`.

## Target Files

- `scripts/download_macro_data.py` (F1, F2, F3)
- `tests/` — new coverage for `download_fred_data` / `save_fred_data` (currently **zero**)

**Single-component ticket.** `src/live_execution/live_trader.py` is **NOT** in scope — F4 was deferred by
the operator at the human gate (see below). `src/features/macro_features.py` is **read-only** here: F2
imports `_FRED_BASE_COLS` (`:57`) and `vol_label_for` from it, and the `:588` hard-raise stays untouched.

## Required Changes

### F1 — Fail loudly on any missing series (`download_macro_data.py:101-129`)
Track per-series failures, **including empty responses** (`:105-107`), not just exceptions. Add a bounded retry (~3 attempts, short backoff) since the observed failure mode is transient. If any series still fails after retries, `raise RuntimeError` naming exactly which series failed. **No partial dict may escape the function.**

F1 emits a health event / Telegram notification on repeated refresh failure. Rationale: the live catch-all at `live_trader.py:5516-5517` is log-only, so a multi-day FRED outage would otherwise stay invisible until the staleness gates fire.

**Verified live-safety of F1's raise (this makes live strictly better, not more fragile):**
- At the **heartbeat**: the `RuntimeError` is not a `StaleDataException`, so it falls through to the pre-existing catch-all at `live_trader.py:5516-5517` → `log.error`, child continues, **file untouched**.
- At **startup** (`:928-958`, catches `StaleDataException` only): it aborts the child — but this is **not novel**. The missing-`FRED_API_KEY` `ValueError` (`:306`) and the COT re-raise (`:354`) already abort startup identically, and `fleet_runner.py:441-460` documents this as approved policy.
- Either path, **the known-good file survives.**

### F2 — Validate the schema before overwriting (`save_fred_data:132-155`)
Validate the produced frame's columns against the required set before writing; **raise instead of overwriting** a known-good file.

- **Import `_FRED_BASE_COLS` from `macro_features.py:57`** — do **not** restate the column literal. Its docstring already declares it the producer/consumer contract; restating would mint a *third* copy of the schema and re-create the very drift this fix exists to prevent.
- Required set = `_FRED_BASE_COLS | {vol_label_for(instrument)}`. Reusing the consumer's own `vol_label_for` makes producer and consumer agree **by construction**.
- **No circular import risk** — `macro_features` imports this script only lazily at `:312`/`:345`; both load orders verified.
- Write **atomically**: `to_csv(tmp)` + `os.replace`.

### F3 — Delete the inline duplicate writer (`main():598-612`)
Delete the inline writer and call `save_fred_data(...)` instead. Pass `instrument=instrument` explicitly and drop the `download_fred_data.instrument` global-attribute hack (`:566`) — same bug class, since any caller forgetting it silently gets no vol column. Net **deletion**, ~13 lines.

**Scope statement (required):** the COT twin at `main():617-620` has the identical dead-writer pattern — and writes **without `index=False`**, unlike `save_cot_data:521`, which is a real latent bug — while `_download_cot_zip:204-209` silently drops individual years. This is **explicitly out of scope**; F3 deletes the FRED inline writer and leaves its COT twin two lines below. **File a follow-up ticket** so the asymmetry is deliberate and recorded, not an oversight.

### F5 — Ops, do first (ordering matters) — ✅ DONE 2026-07-15 18:58 (operator-authorized)
Regenerate the GC and SI files. All six series verified healthy.

**EXECUTED.** Corrupt originals backed up as evidence to
`<scratchpad>/corrupt_macro_backup/` (`fred_macro_data_gc.csv` 401053 B, `fred_macro_data_si.csv` 326301 B).
Regenerated via `python scripts/download_macro_data.py --symbol {GC,SI} --fred-only` using the
anaconda3 **base** interpreter (matches the live trader's env per the eventkit path in the traceback).
All series fetched cleanly on the first attempt — no retries needed, confirming the transient-failure diagnosis.

Post-fix headers verified across all 8 symbol files:
```
cl   Date,VIX,DXY,YIELD_CURVE,FED_FUNDS,OVX
es   Date,VIX,DXY,YIELD_CURVE,FED_FUNDS
gc   Date,VIX,DXY,YIELD_CURVE,FED_FUNDS,GVZ   <- GVZ restored
ng   Date,VIX,DXY,YIELD_CURVE,FED_FUNDS,OVX
si   Date,VIX,DXY,YIELD_CURVE,FED_FUNDS       <- FED_FUNDS restored
nq   Date,VIX,DXY,YIELD_CURVE,FED_FUNDS
zc   Date,VIX,DXY,YIELD_CURVE,FED_FUNDS
zs   Date,VIX,DXY,YIELD_CURVE,FED_FUNDS
```
All match the required set (`_FRED_BASE_COLS` + vol column where vol ≠ VIXCLS). GC 13148 rows, GVZ to 2026-07-14.

**Note for F3's implementer:** the CLI path writes **without** ffill (the inline writer skips it), so the
regenerated files have trailing blanks for DXY/FED_FUNDS on recent dates. This is correct and harmless —
the consumer ffills itself at `macro_features.py:550`. Do not "fix" it by adding ffill expectations to tests;
F3 routing `main()` through `save_fred_data` will add the ffill as a side effect, which is fine either way.

**Ordering:** regenerating while children run would race the buggy non-atomic writer. The 7PM-ET cutoff means the buggy writer **cannot fire again until ~19:00 ET tomorrow**, which buys a ~24h window. Therefore: **F5 now**, land F1/F2/F3 inside that window, then restart children onto fixed code.

**The zombie self-heals without a restart** — `feature_pipeline.py:282` constructs a fresh `MacroFeatureEngine` per bar with no cross-bar cache, so GC resumes on the next 1H bar once the file is fixed.

### Testing (mandatory — coverage is currently zero)
`download_fred_data` / `save_fred_data` have no tests. F1/F2 must ship with them; both are directly unit-testable by injecting a failing/empty series and asserting **(a)** the raise, and **(b)** that the existing file is **not mutated**. The Auditor's byte-identical reproduction is the obvious first test.

---

## F4 — ❌ DEFERRED BY OPERATOR 2026-07-15 18:56 — OUT OF SCOPE FOR THIS TICKET

**Operator decision at the human gate: defer F4 to its own ticket.** F1+F2 alone stop this bug; F4 is
defense-in-depth against a different failure class (feature failures generally going invisible to the
operator) and carries the `_data_mute` defect below, which needs its own design pass.

**TDD-Manager: do NOT implement F4. Do not touch `src/live_execution/live_trader.py` in this ticket.**
The detail below is retained verbatim so the follow-up ticket can be raised from it without re-deriving.

**Proposal:** guard the `_on_new_bar` dispatch (`live_trader.py:4429-4449`) so a feature failure can't be absorbed by eventkit — log CRITICAL, `_emit_health_event("bar-processing-failed", ...)` so the error queue finally sees it, Telegram alert, and set the existing `_data_mute`. Additionally make the heartbeat's post-refresh validation **unconditional** (`:5465`, currently gated on `if self._data_mute:` — a 1-line change that would have caught the 17:00 corruption at 17:00 instead of 18:00).

**Why it's gated:** it touches the live trader's bar-event dispatch — the hot path managing real open positions — and changes risk posture when the trader is blind. F1+F2 alone stop the bug; F4 is defense-in-depth against a different failure class, so gating it leaves nothing unfixed.

**Defect that must be corrected first if authorized:** `_data_mute` is cleared **only** at `:5475`, inside the `if _needs_macro` gate at `:5454`. F4 setting `_data_mute` on a generic feature failure in a **non-macro** model would be a **permanent silent no-trade, unclearable without a restart** — precisely the failure mode F4 exists to prevent. There is also a set/clear evidence mismatch (an ATR failure would be cleared by an unrelated FRED check). F4 must gate on `_needs_macro` or carry its own clear-on-next-successful-build path.

**Mute-not-flatten is the right call** (both agents agree; verified): `:4774` blocks only `BUY/SELL/ENTER/SHORT`, leaving exits and resting SL/TP untouched. Flattening on a *data* error would be a trading decision made on no signal, while resting SL/TP already bound the risk; muting entries is reversible and lets the operator decide.

F4 is well-scoped in principle — `_on_new_bar:4517` **already** has the exact catch→mute→alert→return pattern, and F4 extends it in place rather than inventing a mechanism.

## Follow-up tickets to file (out of scope here)
1. **COT twin** — `main():617-620` dead inline writer + missing `index=False`; `_download_cot_zip:204-209` silently drops years.
2. **SI FED_FUNDS silent skip** — `macro_features.py:627` `if "FED_FUNDS" in df.columns:` and the `:601-603` warn-continue for DXY/YIELD_CURVE are silent-null-defaults. Do **not** fix by adding base-column raises to `_build_fred_features` — that is consumer-side creep into a core utility, and F2's producer gate makes it unnecessary.
3. **Fleet preflight header validation** — `fleet_runner.py:441-460` checks the CSVs *exist* but never validates the header, so the corrupt GC file sailed through. F2's schema check is reusable there.
