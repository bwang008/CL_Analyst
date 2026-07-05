# T7 Audit — ES ops runway
**Ticket:** `t7-es-ops-runway_07052026_0214`
**Auditor:** Ticket-Auditor | 2026-07-05 PT | HEAD `03218af` (branch `development`, T1-T6 merged)
**Mode:** READ-ONLY vs source. Goal: everything remaining so
`python -m src.live_execution.cli --config configs/strategies/ES01B_Sharpe_E03_07042026.json --dry-run`
reaches the event loop with ES bars flowing and ZERO CL contract requests.

**Verdict: ONE code piece (equity session calendar, MEDIUM), TWO data-provisioning gaps
(1h seed = trivial copy; 5m seed = NO sanctioned zero-code path exists — needs authorization),
ONE already-de-facto-verified entitlement, ONE runnable canary runbook. No regression found.**

---

## 0. Accumulated preconditions (from T4/T5/T6 tdd_result.md) — status at HEAD

| Precondition | Source | Status (verified this audit) |
|---|---|---|
| Equity session shape (ES/NQ 15:15-15:30 CT halt) | T5 C4 | **OPEN — the T7 code piece** (§1) |
| GVZ IBKR entitlement check | T4 note / T5 | **Scoped OUT for ES** (§4) — GC-only |
| ES 1h seed parquet `ES_raw_1h.parquet` | T6 note | **OPEN — ops** (§2.1); `ES_raw.parquet` verified as the exact artifact |
| ES config resolves ES/ES/CME, model/prediction artifacts on disk | T6 | **DONE** — config re-read this audit: `execution_symbol:"ES"`, `models.*.symbol:"ES"`, both `final_model.pkl` + `ES01B_Sharpe_E03_predictions.csv` verified present |
| ES macro/COT CSVs | T4 | **DONE** — `fred_macro_data_es.csv` (cols `Date,VIX,DXY,YIELD_CURVE,FED_FUNDS`, last row 2026-07-01) + `cftc_cot_es.csv` (canonical `OI,MM_*,Prod_*,Spec_*,*_Net`, last 2026-06-23, TFF-mapped) both in `C:\CL_Analyst_Data\data\raw\macro\` |
| **NEW gap found: 5m seed `raw/es-5m_bk.csv`** | this audit | **OPEN — ops + authorization** (§2.2). Not in the blueprint's m4 list; blocks `_warm_start()` |

---

## 1. Equity session calendar (the CODE piece — standard TDD flow)

### 1.1 Sourced hours (NO new sources introduced)
From T5 `impact_review.md` V4 (independently sourced there: CME Group E-mini S&P 500 specs,
RJO Futures ES specs "3:15-3:30 pm halt", NinjaTrader ES specs, CME trading hours page):

- ES/NQ trade **Sun 17:00 CT → Fri 16:00 CT**, daily maintenance **16:00-17:00 CT Mon-Thu**
  (this IS the GLOBEX calendar's `Mon-Thu hour==17 ET` branch — V4 sweep-verified as modeled).
- ES/NQ additionally have a REAL **daily 15:15-15:30 CT trading halt** the GLOBEX calendar
  does NOT model (reports OPEN). The blueprint's "16:15-16:30 CT" was an ET/CT transcription
  slip of the same halt (V4).
- Day-set: "daily" = every day with an active day session = **Mon-Fri** (Sat is closed;
  Sun's session opens 17:00 CT, after the halt window — structural consequence, not a new
  hours claim). Friday: halt 15:15-15:30, reopen 15:30-16:00, then weekend close at 16:00 CT
  (per the RJO/CME "each trading day" specs V4 cites).
- Consequence documented in `session_calendar.py` C4 block (lines 29-37): unmodeled, the ES
  watchdog has a daily false-positive window ≈15:25-15:35 CT (last 5m bar opens 15:10;
  threshold 15 min) → forced disconnect/reconnect + Telegram spam every trading day.

### 1.2 Registry tuple change (`src/core/instrument_master.py`)
```python
# CME equity index outrights: 17:00-16:00 CT with a 15:15-15:30 CT daily halt
# (Mon-Fri) — T5 impact_review V4 sourced finding, C4 amendment.
_EQUITY_SESSION: Tuple[Tuple[str, str], ...] = (("17:00", "15:15"), ("15:30", "16:00"))
```
- `session_hours_ct=_EQUITY_SESSION` on exactly **4 entries: ES, MES, NQ, MNQ**
  (currently `_GLOBEX_SESSION` at lines 94/113/232/251). Micros MUST change with parents
  (`tests/test_instrument_master_live_fields.py:205` asserts `m.session_hours_ct == p.session_hours_ct`).
- Everything else in the registry untouched (`bars_per_day_1h=23` stays — the 15-min halt
  lives inside the 15:00-16:00 CT hour, no bar-count change; no seed-formula change;
  roll fields untouched).
- CL/MCL/GC/SI/NG/HG/PA/SIL keep `_GLOBEX_SESSION`; ZC/ZS keep `_GRAINS_SESSION` — byte-identical.

### 1.3 Calendar branch (`src/live_execution/session_calendar.py`)
Additive third branch on the existing tuple-equality dispatch (the module docstring itself
says "the dispatch below already supports adding one"). Evaluated in **America/Chicago**
(grains precedent — the halt is CT-defined by CME):

```python
def _equity_market_status(utc_now: datetime) -> str:
    ct_now = utc_now.replace(tzinfo=pytz.utc).astimezone(_CT)
    weekday, hour, minute = ct_now.weekday(), ct_now.hour, ct_now.minute
    if weekday == 5:                                   # Saturday
        return "CLOSED (weekend — opens Sun 5pm CT)"
    if weekday == 6 and hour < 17:                     # Sunday pre-open
        return "CLOSED (weekend — opens Sun 5pm CT)"
    if weekday == 4 and hour >= 16:                    # Friday close
        return "CLOSED (weekend — opens Sun 5pm CT)"
    if 0 <= weekday <= 3 and hour == 16:               # Mon-Thu maintenance 16:00-17:00 CT
        return "CLOSED (daily maintenance 4-5pm CT)"
    if weekday <= 4 and hour == 15 and 15 <= minute < 30:   # Mon-Fri equity halt
        return "CLOSED (equity halt 3:15-3:30pm CT)"
    return "OPEN"
```
String contract (T5 convention): only `"OPEN"` is byte-load-bearing (watchdog gate
`!= "OPEN"`, `live_trader.py:3940`); CLOSED strings are shape-pinned (`startswith("CLOSED")`
+ `weekend`/`halt`/`maintenance` marker) — the CL byte-frozen strings are NOT reused, so the
GLOBEX body stays untouched.

`session_open_anchor` equity branch — mirror of `_grains_session_open_anchor` walk-back:
opens are **Mon-Fri 15:30 CT** and **Sun-Thu 17:00 CT**; return most recent as tz-naive UTC.
This intentionally FIXES both ES reopen false positives (15:30 halt-reopen AND 17:00
maintenance-reopen). No CL semantics change: GLOBEX still returns `None`
(Q1 CL reopen false-positive stays pinned as-is per `cl-watchdog-reopen-grace_07052026_0001`).

Dispatch: two new `if shape == _EQUITY_SESSION:` arms in `market_status` and
`session_open_anchor`; `_unsupported_session_shape` message extended to list the EQUITY tuple.
C4 doc block rewritten: halt is now modeled; drop the "REQUIRED before ES launch" warning.

### 1.4 Watchdog interplay (verified against `live_trader.py:3921-3986` — no live_trader change needed)
- 15:15-15:30 CT: `market_status != "OPEN"` → `_check_stale_bars` returns False → **no storm
  during the halt** (was the C4 defect).
- 15:30-15:45 CT: anchor=15:30 > last-bar 15:10 → reference=15:30 → minutes_stale < 15 →
  no reopen false positive; if bars are genuinely dead, staleness re-arms 15 min after
  reopen — correct detection preserved.
- 16:00-17:00 CT Mon-Thu and weekends: CLOSED (as today via the equivalent ET branch).
- `_get_market_status`/heartbeat consume the same function — heartbeat `market=` strings for
  ES change from OPEN→CLOSED inside the halt (log-only; grep-verified no Telegram payload
  dependency in T5 V1).

### 1.5 Tests (Strict-Lock; frozen-clock; extend `tests/test_session_watchdog_rollover.py`)
New cases (each run at a CST-week AND a CDT-week instant, grains-test pattern `_GRAIN_WEEKS`):
1. ES Tue **15:20 CT** → CLOSED + "halt" marker. 2. ES Tue **15:35 CT** → `"OPEN"` (byte).
3. ES Tue **16:30 CT** → CLOSED + "maintenance" marker. 4. ES Fri 15:35 CT → OPEN;
   ES Fri 16:30 CT → CLOSED weekend. 5. ES Sat noon / Sun 10:00 CT → CLOSED weekend;
   Sun 17:05 CT → OPEN. 6. Watchdog no-storm: frozen `LiveTrader` stub at 15:20 CT with
   last 5m bar 15:10 → `_check_stale_bars() is False`; at 15:36 CT last bar 15:10 →
   False (anchor grace); at 15:50 CT with no bar since 15:10 → True (real staleness detected).
7. Anchor pins: ES anchors == exact tz-naive-UTC of 15:30/17:00/Sun-17:00 CT; MES/MNQ equal ES/NQ.
8. Registry pins: ES/MES/NQ/MNQ == `_EQUITY_SESSION`; **CL/MCL == `_GLOBEX_SESSION` and
   ZC/ZS == `_GRAINS_SESSION` unchanged** (explicit anti-drift pins).
9. Unknown-shape still raises (existing test at `:481-483` uses a fake tuple — stays green).

**Sanctioned pin evolutions (exactly 3 — need TDD-manager sanction, T6 precedent):**
- `test_globex_family_dispatches_to_same_calendar` (`:257-266`): drop ES/NQ from the loop
  (keep CL/MCL/GC/SI).
- `test_es_maintenance_break_modeled_closed` (`:268-274`): ES Mon 17:30 ET now returns the
  equity maintenance string, not the CL `_HALT_STR` byte string → re-pin shape-wise; its own
  docstring says the 15:15-15:30 halt is "C4 documentation-only — deliberately NOT modeled"
  (i.e., written to be evolved).
- `test_session_open_anchor_none_for_globex` (`:276-291`): drop `_ES` from the instrument loop.

All other T5 fences (GLOBEX minute-by-minute DST sweep vs the frozen legacy transcription,
grains cases, Q1 CL pins) remain untouched and MUST stay green. CL/grains byte-identity holds
by construction: dispatch is tuple-equality and CL/ZC tuples don't change.
Post-green: HS14B ledger parity gate re-run per T2/T4/T5/T6 convention (expect $0.00 delta —
this layer is bypassed by the harness, but the convention is mandatory).

**Severity: MEDIUM** (multi-line, 2 src files + tests, but additive along the seam T5
explicitly designed for this; no live_trader/ibkr_client/data_manager edits; not a refactor).
**Regression: NO** — planned completion of T5's C4 condition.
**Business justification:** without it, every ES trading day has a forced
disconnect/reconnect + Telegram alert + backfill churn at ≈15:25-15:35 CT (T5 V4).

---

## 2. Data-provisioning plan (ops — NOT TDD)

### 2.1 The 1h seed — DECISION: **copy `ES_raw.parquet` → `ES_raw_1h.parquet`** (no derivation needed)

Verified schema facts (scratchpad `conda run -n trader` inspection, 2026-07-05):

| Fact | `ES_raw.parquet` (exists) | `CL_raw_1h.parquet` (CL's live 1h seed) |
|---|---|---|
| Columns / dtypes | `[DateTime(datetime64[ns]), Open, High, Low, Close (float64), Volume (int64)]` | identical |
| Cadence | median = mode = **1h** (max gap 4d1h = weekends/holidays) | identical (1h) |
| Range | 2010-06-07 00:00 → **2026-06-30 23:00**, tz-naive **UTC** | 2010-06-07 → 2026-06-12 |
| Dups / NaN | 0 / 0 | 0 / 0 |
| Bars in last 292 cal days (ES seed window) | **4,696 ≥ 4,320** (MACRO_6M floor) | 4,728 |
| Provenance | Databento GLBX batch `GLBX-20260701-NEM5G6FTXC`, `schema=ohlcv-1h`, `symbols=['ES.v.0']`, converted per build-symbol-pipeline Phase 1 step 6 ("mirror CL_raw_1h.parquet", **hourly** — claim VERIFIED) | Databento, same convention |

- The seed loader (`data_manager._seed_from_csv`, `.parquet` branch, `:489-498`) needs exactly
  `DateTime + OHLCV` → satisfied. UTC-naive matches live appends
  (`ib_bars_to_dataframe` targets UTC + `make_naive=True`, `ibkr_client.py:22-23,1443`;
  `_on_bar_update_1h` tz-converts to UTC-naive, `live_trader.py:2847-2849`) and the backfill
  gap math (`pd.Timestamp.now(tz="UTC").tz_localize(None)`, `data_manager.py:603`).
- **Copy, not rename**: `ES_raw.parquet` is the manifests' `execution_data_path` artifact
  (build-symbol-pipeline Phase 4 uploads it) — renaming breaks the training/backtest side.
- **Copy, not `live_config.seed_path_1h` override**: keeps the freshly-T6-validated config
  untouched and lands the file at the T2 naming-authority default
  (`derive_data_paths("ES").seed_1h == processed/ES_raw_1h.parquet`, `data_manager.py:105`).
- Staleness: seed ends 2026-06-30 23:00 UTC; startup backfill issues a single now-anchored
  `"{gap_days} D"` 1h ContFuture request (`data_manager.py:616-640`) — a ~1-week gap is
  trivially within IBKR limits. The 45min-2h30 cache-cadence guard (`live_trader.py:1986-1993`)
  passes (median exactly 1h).

Commands (PowerShell):
```powershell
Copy-Item C:\CL_Analyst_Data\data\processed\ES_raw.parquet C:\CL_Analyst_Data\data\processed\ES_raw_1h.parquet
```
Validation (scratchpad script, must ALL pass before canary): columns exactly
`[DateTime,Open,High,Low,Close,Volume]`; median diff == 1h; 0 NaN; 0 dup timestamps;
`max(DateTime) >= 2026-06-30`; bars in last 292 days ≥ 4,320; last Close in [5000, 10000].

### 2.2 The 5m seed — **BLOCKING GAP, no sanctioned zero-code path; needs human authorization**

What raises, exactly: the 5m DataManager is ALWAYS constructed (`live_trader.py:373-383`) with
seed default `derive_data_paths("ES").seed_5m` → `get_data_path("raw/es-5m_bk.csv")` →
`C:\CL_Analyst_Data\data\raw\es-5m_bk.csv` (verified ABSENT; repo-local `data/raw/` doesn't
even exist). With no `warm_start_cache_ES.parquet` either (verified absent),
`DataManager.initialize()` Step 1 hits the deliberate No-Silent-Bootstrap hard fail
(`data_manager.py:293-308`) → `FileNotFoundError` inside `_warm_start()` (start() Step 8,
`live_trader.py:770`) → **canary dies after IBKR connect, before the event loop**. There is
NO empty-seed bootstrap path (by design — T2 kept the hard fail verbatim), and
`live_config.seed_path_5m` was routed T2→T6 but never implemented (grep: 0 hits) — only the
CLI `--seed-path` flag or the canonical filename work.

Functional requirement for a **1h config** (verified): the 5m stream feeds ONLY
trailing-stop checks, telemetry, and the ledger append (`live_trader.py:2820-2837`);
inference/features/4320-floor are all on the 1h stream. Warm start requires just `len > 0`
(`:1957-1960`) plus real (non-fabricated) data per the house rule. Seed trim window is
last 292 calendar days (`derive_seed_lookback_days(23)`), but a shorter genuine history is
mechanically valid.

Options (both need a small NEW ops script — neither existing tool covers ES 5m):
- **(A) RECOMMENDED — IBKR ContFuture 5m pull (free, ~60-365 days).**
  `scripts/download_ibkr_history.py` proves the mechanics (Strategy A: ContFuture, empty
  endDateTime, `"2 Y"→"1 M"` durations, 5m bars) but is CL-hardcoded (`build_cl_contract`,
  `:95`); `scripts/download_ibkr_multi_history.py` is symbol-generic but 1h-hardcoded
  (`bar_size="1 hour"`, `:195`). A ~40-line ops script using the T2 generic
  `build_future_contract("ES", continuous=True)` + `ib_bars_to_dataframe` (UTC-naive by
  default — matches the seed convention) writing the legacy semicolon format
  `DD/MM/YYYY;HH:MM;O;H;L;C;V` (parser at `data_manager.py:500-514`) to
  `C:\CL_Analyst_Data\data\raw\es-5m_bk.csv`. Uses the paper gateway (4002) → **needs user
  go-ahead for the gateway session**; writing a new script exceeds pure-ops → **flag for
  approval** (script itself is throwaway-grade, can live in scripts/ or scratchpad).
- **(B) Databento upgrade path (COSTS MONEY — MUST be explicitly authorized).**
  Databento GLBX has NO `ohlcv-5m` schema (1s/1m/1h/1d); the route is `ohlcv-1m` for ES.v.0
  → resample to 5m → convert. The builder CLI does NOT expose `--schema`
  (`DEFAULT_SCHEMA="ohlcv-1h"`; `estimate`/`submit` parsers have no schema flag) — the
  library functions do (`get_cost_estimate(schema=...)`, `submit_historical_batch(schema=...)`),
  so this also needs a small script plus a **free cost estimate FIRST**, then explicit
  user spend approval. Recommended only as the post-canary quality upgrade (full history,
  training-source-consistent), not as the canary blocker-remover.

Validation for whichever seed lands: 5m median cadence; 0 NaN/dups; UTC-naive;
last bar within ~1 day of pull time; Close magnitude 5000-10000; ≥30 days coverage (canary
floor; note in the runbook that pre-live-trading a fuller 5m history is preferred).

### 2.3 Explicit non-goals (scope guards)
NO provisioning for any other symbol (ZC/NQ/GC 5m seeds have the same gap — NOT built here);
NO T8 workflow-doc edits; NO fleet_runner changes; NO edits to `configs/strategies/ES01B_*.json`
(T6's surgical patch stands); NO touching CL artifacts
(`warm_start_cache*.parquet`, `cl_continuous_master*`, `.roll_metadata.json`).

---

## 3. Warmup / feature inputs for the 1h ES config (all instrument-driven post-T4 — inventory)

Verified by loading both `final_model.pkl` boosters (LightGBM, 223 features each):
external features = **47**: `MACRO_VIX*` (9), `MACRO_DXY*` (9), `MACRO_YIELD_CURVE*` (10),
`MACRO_FED_FUNDS`, `MACRO_WIDTH_*` (5 — internal, T4 D1-excluded from the external check),
`COT_*` (13). **Zero OVX/GVZ features** → T4's startup `validate_external_macro_features(ES)`
passes; `_needs_macro` is True.

| Input | Status |
|---|---|
| `fred_macro_data_es.csv` | present; columns exactly the model's externals (VIX/DXY/YIELD_CURVE/FED_FUNDS). mtime 2026-07-01 → STALE vs the 7pm-ET rule → startup auto-refresh WILL run → **requires internet + `FRED_API_KEY`** (verified set in `.env`); a missing key or FRED failure is FATAL (only `StaleDataException` is caught at start() Step 7 → SAFETY MUTE; `ValueError`/download errors propagate) |
| `cftc_cot_es.csv` | present (TFF-mapped canonical cols); mtime 2026-07-02 → refresh only if >7 days old at canary time; CFTC download needs no key |
| VIX daily close (IBKR) | T4 fetch list for ES = `["VIX"]` only (`live_trader.py:675-676`); VIX/CBOE already proven working by the running CL fleet (fetches VIX+OVX daily) |
| 1h history ≥ 4,320 bars | seed provides 4,696 (§2.1) + backfill |
| `warmup_bars` | not in the config → default **24** (`live_trader.py:774`) → `_warmup_inference_state(24)` on the ratio-adjusted 1h df; needs exec-side `get_position`/`get_account_summary` (paper, free) |
| predictions CSV / model pkls | verified on disk (T6) — `reports/batch_runs/batch_20260704_0701_ES_01B_SCOUT/predictions/ES01B_Sharpe_E03_predictions.csv`, both `E2E_HourSet_01B_{long,short}_logloss/final_model.pkl` |

Nothing else is required for warm start.

## 4. GVZ — confirmed OUT of scope for ES
Registry `ES.live_vol_index == "VIX"`; T4's ordered fetch list for ES is `["VIX"]`; the ES
models contain zero GVZ/OVX features (§3). The unverified-GVZ-entitlement note from T4 attaches
to the GC/MGC launch only. **Closed for T7 with no action.**

## 5. IBKR CME (ES) market-data entitlement
**De-facto verified already:** `C:\CL_Analyst_Data\data\raw\ibkr_samples\ES_ibkr_1h.csv`
(493 hourly ES bars, 2026-05-28 → 2026-06-26, realistic prices ~7,4xx-7,6xx) was downloaded
2026-06-27 through the paper gateway per grab-data Step 4 — which the workflow explicitly
documents as the market-data-subscription validation ("If a symbol fails here, you're missing
that exchange's data package"). Futures historical data requires the same CME entitlement as
streaming, so risk is residual (real-time vs delayed nuance only).

Canary check + failure signatures (if entitlement lapsed):
- Front-month `reqContractDetails` (start() Step 4) succeeds regardless of md subscription —
  NOT an entitlement probe.
- The seed-gap backfill (`reqHistoricalData` on ES ContFuture) is the first true probe:
  failure = IBKR **error 162** ("Historical Market Data Service error message:No market data
  permissions for GLOBEX ES") or 10168/10197 variants surfacing through the `_request_historical_data`
  retry loop → empty backfill / warm-start failure.
- Streaming (`keepUpToDate=True` subscription, start() Step 8): failure = **error 354**
  ("Requested market data is not subscribed") or silent absence of `NEW 5M BAR:` lines while
  `market=OPEN`. The code never calls `reqMarketDataType(3)` → no silent delayed-data fallback.
- Remedy if it fires: IBKR Account Management → Market Data Subscriptions → CME real-time
  (NP,L2 bundle or "CME Real-Time (NP,L1)") — **may cost a monthly fee → user decision**.

## 6. Dry-run canary runbook (ops — REQUIRES USER GO-AHEAD before touching the gateway)

### Preconditions (all must hold)
1. Code: HEAD ≥ `03218af` **plus the §1 equity calendar landed** (recommended sequence; if the
   user accepts running before it lands, expect one watchdog reconnect + Telegram alert daily
   ≈15:25-15:35 CT — do not count it as a canary failure).
2. Data: §2.1 copy done + §2.2 5m seed provisioned and validated. Macro CSVs present (are).
3. `.env`: `CL_DATA_ROOT`, `FRED_API_KEY`, `TELEGRAM_*` set (verified 2026-07-05); internet up
   (FRED refresh at startup is fatal-on-failure).
4. IB Gateway PAPER logged in on 127.0.0.1:**4002** (verified LISTENING, PID 25572 at audit
   time). NOTE: the CLI's `--data-port` DEFAULT IS 4001 (live) with TWS fallback 7496 — the
   command below overrides it; do not omit.
5. Client IDs: config `live_config.client_id=1010` → data cid **1010**, exec cid **1011**
   (`cli.py:311` uses cid+1). The running HS14B instance holds 1400/1401 (2 established API
   connections observed on 4002 — consistent) → no conflict expected. **Abort signature if
   wrong: IBKR error 326 "client id is already in use".**
6. Paper account FLAT in ES with no working ES orders. (Dry-run gates ENTRIES only,
   `live_trader.py:3382-3387`; an inherited ES position would be adopted and its exit paths
   are NOT dry-run-gated. Startup also cancels orphaned ES orders when flat.)
7. Run during ES hours (Sun 17:00 CT → Fri 16:00 CT) so bars actually flow; for the
   bars-flowing assertion avoid 15:15-15:30 and 16:00-17:00 CT.

### Command (repo root, Windows)
```powershell
conda run -n trader python -m src.live_execution.cli --config configs/strategies/ES01B_Sharpe_E03_07042026.json --data-port 4002 --exec-port 4002 --dry-run
```
Artifacts it will create (expected, leave in place if canary passes — they are the real ES
lineage): `warm_start_cache_ES.parquet`, `warm_start_cache_ES_1h.parquet`,
`es_continuous_master.parquet`, `es_continuous_master_1h.parquet`, `.roll_metadata_ES.json`
(all under `C:\CL_Analyst_Data\data\processed\`), `live_telemetry_cid1010.db`, log
`reports/livetrader_1010.log`. If the canary is INVALID, delete these before rerunning.

### Success criteria (in log order; log = console + `reports/livetrader_1010.log`)
1. `Instrument resolved: execution=ES (CME, tick=0.25) brain=ES` (pre-connect).
2. `DATA PATHS: 5m seed=...es-5m_bk.csv  cache=...warm_start_cache_ES.parquet` and
   `DATA PATHS: 1h seed=...ES_raw_1h.parquet  cache=...warm_start_cache_ES_1h.parquet`.
3. `Loaded macro daily closes: {'VIX': <float>}` — VIX only, no OVX/GVZ fetch.
4. `Front-month contract: ES?? (month=2026xx)` — an ES local symbol (e.g. ESU6), never CLxx.
5. Warm start: `Cache seeded: N bars ...` / backfill chunk lines, then
   `1h rolling window initialized: N bars` with N ≥ 4,320 and NO `CACHE VALIDATION FAILED`.
6. `Subscribed to 5-min continuous contract live bars`, `Subscribed to 1-hour continuous
   contract live bars`, `Subscribed to front-month live bars`.
7. Event loop reached: Telegram `LiveTrader Online ... Dry-run: True` +
   `HEARTBEAT: alive | last_bar=... | market=OPEN` within ~5 min.
8. **ES bars flowing:** `NEW 5M BAR: ... C=<5000-10000>` within ~10 min (ES price magnitude
   makes CL cross-feed impossible to miss — CL prints ~60-90).
9. **ZERO CL contract requests:**
   `grep -E "symbol='CL'|Front-month contract: CL|\bCL[FGHJKMNQUVXZ][0-9]\b" reports/livetrader_1010.log`
   → 0 hits (patterns chosen to dodge benign `CLOSED`/`CL_DATA_ROOT`/"CL Analyst" strings);
   AND mtimes of `warm_start_cache.parquet`, `warm_start_cache_1h.parquet`,
   `cl_continuous_master*.parquet`, `.roll_metadata.json` UNCHANGED from pre-canary.
10. Soak ≥ 1-2 hours spanning a 1h boundary: a `NEW 1H BAR:` line + inference/telemetry rows
    in `live_telemetry_cid1010.db` (signals table gets HOLD/DRY_RUN rows) + no watchdog events.

### Abort criteria (kill the process — Ctrl+C is graceful — and file a ticket)
- ANY CL-pattern hit from criterion 9 (T2 regression — highest severity).
- Error 326 (cid collision) / repeated connect failures.
- Error 162 / 354 / no bars while `market=OPEN` for >15 min outside halts (entitlement or feed
  problem; see §5 remedy).
- `FileNotFoundError`/`ValueError` at startup (seed/instrument/FRED-key) — fix inputs, rerun.
- Any REAL order transmitted (watch exec log for `placeOrder`) — should be impossible while
  flat + dry-run; if seen, abort immediately.
- Repeated `STALE BAR WATCHDOG` reconnect loop (if it clusters ≈15:25-15:35 CT daily, that is
  the §1 calendar gap — land §1 rather than debugging the feed).
- SAFETY MUTE at startup is NOT an abort (entries blocked, loop still proves out) — but record
  the staleness reason.
- Per run-live.md: the process runs forever — the operator/agent MUST terminate it explicitly
  when observation is done.

## 7. Severity, regression, residuals
- **Overall T7: MEDIUM.** One additive code change (§1, MEDIUM, TDD flow + 3 sanctioned pin
  evolutions + parity-gate convention), two ops provisioning steps (LOW risk), one runbook.
- **Regression: NO.** Nothing in T1-T6 regressed; all their invariants re-verified where touched.
- Residuals routed (NOT T7 work): `live_config.seed_path_5m`/`cache_path` keys promised in the
  T2→T6 handoff never landed (CLI flags + canonical names cover it — cosmetic);
  `CLOnlyLogFilter` in `log_config.py` is dead code (imported `noqa: F401`, never attached —
  no ES log-suppression risk, candidate for the m3 cosmetic sweep);
  other symbols' 5m seeds have the same §2.2 gap (future standups; T8 should add the 5m seed
  to build-symbol-pipeline's checklist).

## 8. Open questions requiring HUMAN AUTHORIZATION
1. **5m seed route (§2.2)** — approve option A (new small IBKR pull script + a paper-gateway
   session, free) now, with option B (Databento `ohlcv-1m` purchase → 5m resample;
   **costs money**, estimate first) as the pre-live-trading upgrade? Both need explicit approval.
2. **Running the canary against the user's gateway (§6)** — requires user go-ahead (uses the
   live paper session, creates ES data artifacts, sends Telegram messages, and shares the
   gateway with the running HS14B instance).
3. **Equity-calendar pin evolutions** — TDD-manager sanction for the 3 T5 Strict-Lock pin
   changes listed in §1.5.
4. **If entitlement check fails** — CME real-time market data subscription purchase (monthly
   fee) is a user decision.
