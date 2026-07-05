# Audit — Seedless 5m Live Stream (shallow IBKR bootstrap for non-5m models)
**Ticket:** `seedless-5m-live-stream_07052026_0546` | **Auditor:** Ticket-Auditor | 2026-07-05 PT
**HEAD:** `336d29f` (branch `development`; T1-T8 multi-symbol program merged)
**User ruling (2026-07-05, supersedes the T7-era reading):** the "no 5m data" ruling covered
HISTORICAL/TRAINING purchases only. Every live model SHOULD stream live 5m bars (trailing-stop
granularity). The seed requirement must follow what the model CONSUMES: 5m models keep the hard
seed requirement; 1h/2h/4h models may shallow-bootstrap the 5m window from IBKR when no seed or
cache exists.

**Verdict: FEASIBLE, localized. Severity MEDIUM. Regression (this design): NO — the HEAD raise is
by-design; the change is a sanctioned behavior extension. SEPARATE pre-existing regression FOUND
at HEAD: commit `336d29f` flipped ES01B to MES/cid-2000 without evolving its test pins — 6 tests
FAIL at HEAD today (verified by run, §3.3). This ticket's pin evolution subsumes them.**

---

## 1. Exact code path map (deliverable 1)

### 1.1 Where the raise lives at HEAD
- `src/live_execution/data_manager.py:240-350` — `DataManager.initialize()`.
  Step 1 (`:271-308`): `cache_path.exists()` → `_load_cache()`; elif `seed_path.exists()` →
  `_seed_from_csv()` + `save_cache()`; **else `:292-308` — the No-Silent-Bootstrap hard fail**:
  `log.error("CRITICAL: Seed file missing at '%s'...")` + `raise FileNotFoundError("Seed file
  not found for {symbol}: {seed_path}\n... Check CL_DATA_ROOT ...")`. This is the ONLY raise the
  shallow path replaces (conditionally).
- **Second raise site the sketch missed:** `data_manager.py:1008-1076` `_update_training_ledger()`
  — first-run branch (`:1031-1034`, ledger file absent) calls `_load_full_seed()` (`:1078-1089`)
  which raises its own `FileNotFoundError` on a missing seed. Since `initialize()` Step 4 calls it
  on EVERY run with a data client (`:337-338`), a shallow bootstrap that only fixes Step 1 would
  crash at Step 4 — **on run 1 AND on every cache-warm-started run 2+** (ledger still absent, seed
  still absent). The design MUST gate this too (§1.5).
- Caller chain: `live_trader.py:2020` `_warm_start()` → `self.data_manager_5m.initialize()`
  (flag-gated at `:2018`; only site where the seed fail fires). `_warm_start` then requires a
  NON-EMPTY frame (`:2022-2025` RuntimeError) — the bootstrap must return ≥1 bar or raise loudly.
- Construction: `live_trader.py:398-410` — 5m DataManager built when `_enable_5m_stream` (default
  true, `:339-342`); seed/cache paths default to `derive_data_paths(brain_symbol)`
  (`data_manager.py:69-109`: non-CL 5m seed = `raw/{sym}-5m_bk.csv`, cache =
  `processed/warm_start_cache_{SYM}.parquet`). `cli.py:249/:257` resolves the same defaults.

### 1.2 What the shallow bootstrap needs
- **Fetch path (post-T2, symbol-aware):** `DataManager` already calls
  `self.data_client.fetch_historical_bars_by_duration(duration_str=…, continuous=True,
  bar_size=self.bar_size, what_to_show="TRADES", use_rth=False)` for backfill (`:634-640`) — the
  adapter (`adapters/ibkr_data_feed.py:69-86`) injects `symbol=self._instrument_context.
  brain_symbol` and the manager method (`ibkr_client.py:876-929`) builds/qualifies the continuous
  contract and returns `ib_bars_to_dataframe(...)` (`:1438-1464`) — **DateTime as BOTH index and
  column**, exactly the shape `save_cache`/`append_bar`/`_dedup_and_sort` expect. The bootstrap
  reuses this verbatim; zero ibkr_client/adapter changes.
- **Duration choice: `"5 D"` (module constant `_SHALLOW_BOOTSTRAP_DURATION = "5 D"`).**
  Justification:
  - *Sufficiency:* trailing needs bars-since-entry only — and on restart,
    `_recover_inherited_position` seeds extremes from ONLY the last bar (`live_trader.py:1567-
    1569`) and bars-held from the last bar's timestamp (`:1528-1540`); `_check_naked_position`
    reads the last close (`:4171-4172`). The deepest realistic in-position horizon is
    per-side `max_hold_bars` (ES01B: 12×1h) / the engine rail `_MAX_HOLD_BARS=288` 5m bars = 24h.
    5 calendar days covers both **across a weekend**, so an inherited-position restart always has
    bars at/before entry time; telemetry (`market_bars`) gets a few days of context.
  - *Roll-ratio symmetry:* `_compute_roll_ratio` intersects a `"3 D"` IBKR fetch with the cache
    (`:858-874`) — a ≥3 D window guarantees full overlap from day one.
  - *IBKR limits:* one single request; 5m-bar history serves ~60 days per the repo's own note
    (`data_manager.py:966-968`), and pacing (≤60 hist requests/10 min, 15 s identical-request
    cooldown) is untouched by 1 request — the fleet's `stagger_seconds: 60` further separates
    instances. ~5×276=1,380 ES bars ≈ instant.
  - *`_MAX_ROLLING_BARS` interplay:* 44,000 (`live_trader.py:121`) exists to protect deep 5m
    FEATURE lookbacks (the parity note: 52/80 features diverged when the window was too small) —
    that guard is for 5m MODELS. A ~1.4k-bar shallow window is 3% of the cap; the pruning at
    `:2908-2909`/`:2701-2702` is a no-op and irrelevant to 1h models (their features come from the
    1h frame, §2). `bars_per_day` is stored (`data_manager.py:215`) but **never read at runtime**
    (grep-verified; construction-pin tests only) — no interaction. `seed_lookback_days` is used
    only by `_seed_from_csv` (`:517`) — bypassed.
- **Fetch context is safe:** `initialize()` runs from `_warm_start` (start() Step 8, `:805`),
  main thread, before the event loop — same context as the existing Step-3 backfill fetches.
- **front_month_id is already set** before `_warm_start` (start() Step 6 `:758-767`), so
  Step 5 `_save_roll_metadata()` records the front month on the bootstrap run and run-2 roll
  detection (`_detect_rollover` string compare, `:811-839`) works normally. Run 1 itself returns
  False ("first run", `:824-828`) — no ratio machinery touches the shallow window on boot.

### 1.3 Run 2+ = normal cache warm-start (self-healing persistence)
The bootstrap saves the fetched frame via `save_cache()` immediately (mirror of the seed branch
`:284-285`); live appends flush every 12 bars (`append_bar` → `_FLUSH_INTERVAL_BARS`,
`:380-383`); `_shutdown` saves again (`live_trader.py:932-936`, already None-guarded per T7 C2);
reconnect stitches also save (`:2705-2708`). Run 2: `cache_path.exists()` → Step 1 cache branch →
gap backfill (`_backfill`, `:593-671`) — **byte-identical machinery to today's CL cache
warm-start**. The cache is never depth-trimmed (`_df` unpruned; `save_cache` persists all rows),
so the window deepens monotonically toward a CL-like history.

### 1.4 Ledger accrual in shallow mode — DECISION: SKIP while seedless, loudly (with evidence)
- **Mechanical tolerance:** `_detect_rollover` (string compare) and `_apply_roll_to_cache`
  (JIT ratios, depth-agnostic) tolerate a shallow window; `_compute_roll_ratio` needs only 3 D of
  overlap (§1.2). The ledger append path needs only `ledger.index.max()` (`:1062`). The ONLY
  intolerant site is first-run ledger CREATION → `_load_full_seed()` raise (`:1078-1089`).
- **Consumer census (grep-verified):** no code anywhere consumes a non-CL 5m ledger. The only
  `*_continuous_master.parquet` consumers are CL literals: `worktree_startup.py:59`,
  `scripts/generate_mock_shadow_log.py:56-63`, `scripts/backfill_shadow_log.py:77-79`,
  `check_vol.py:5`. The 5m ledger is a training-data by-product (T7 audit §1.1), and non-CL
  training data comes from Databento hourly purchases by ruling.
- **Zero data loss:** the warm-start cache accrues EVERY live bar raw and forever (§1.3) and roll
  ratios live in the roll-metadata JSON — a proper ledger can be reconstructed later from
  cache+history, or founded correctly the day a real seed is provisioned (the gated branch then
  takes `_load_full_seed()` on the full seed, as designed).
- **No-silent-misdata:** a `{sym}_continuous_master.parquet` FOUNDED on a 5-day IBKR fetch would
  masquerade as training-grade history — precisely the silent-fake-environment failure the
  No-Silent-Bootstrap comment (`:293-296`) exists to prevent. Skipping WITH a loud WARNING is the
  honest state.
- **Rule:** in `_update_training_ledger`, when ledger file absent AND seed absent AND
  `allow_shallow_bootstrap` → log WARNING ("master-ledger accrual SKIPPED — no seed to found a
  training ledger; cache still accrues raw bars + roll metadata") and return. Ledger-present/
  seed-absent keeps appending (works today, `:1018-1030` + `:1060-1073`). 5m models
  (`allow_shallow_bootstrap=False`) keep the raise — dormant guard intact.

## 2. bar_size gate verification (deliverable 2)
`bar_size == "5m"` is the correct "needs deep 5m features" discriminator. Re-verified at HEAD:
- **What bar_size=="5m" drives:** inference dispatch off the 5m frame (`live_trader.py:2922-2924`
  `_on_new_bar(bar_time, rolling_df_5m, "5m")` → feature build on the 5m window), warmup source
  fallback (`:2120-2121`), reconnect warmup counting (`:2721-2722`). These are the ONLY deep-5m
  consumers; `_atr_at_entry` comes from `signal.atr_at_entry` (`:3530`) computed on the inference
  frame (1h for 1h models).
- **Complete `rolling_df_5m`/`data_manager_5m` consumer list for a 1h model (all shallow-tolerant
  — last-bar reads or forward-only accrual):** trailing extremes `:1155-1160` (last bar);
  recovery `:1528-1569` (last-bar timestamp + last-bar extremes); reconnect stitch `:2694-2710`
  (bounded by gap); 5m callback append/telemetry/trailing `:2907-2924`; naked-position flatten
  price `:4171-4172` (last close); rollover front_month_id `:760-763`, `:2486-2487`; shutdown
  save `:932-936`. `_warmup_inference_state` for 1h models uses
  `data_manager_1h.get_ratio_adjusted_df()` (`:2118-2119`) — zero 5m dependency. The 1h seed
  hard requirement (`:444-454`) and the 4320-bar floor (`:2091-2108`) are UNTOUCHED — the deep
  window a 1h model actually consumes stays hard-required.
- Gate set: use `self._bar_size in ("1h", "2h", "4h")` — the exact set the T7 flag validation
  legalizes (`:343-348`). Exotic/unknown bar_size → conservative False → today's raise (no silent
  default).
- The 5m-model NaN-feature guard (parity note at `_MAX_ROLLING_BARS`, `:117-121`) survives
  verbatim for `bar_size=="5m"` — the raise is not touched on that path.

## 3. Interaction with T7 hourly-only machinery (deliverable 3)

### 3.1 Reversion is automatic (presence/flag-driven) — verified at HEAD
With ES01B 5m-enabled (flag default true):
- **Watchdog:** `_check_stale_bars:4037-4042` flag-read → `_last_bar_time_5m` + 15-min threshold
  (T7 CL pins already fence this branch); `_session_open_anchor` uses the brain instrument's
  EQUITY arm (halt 15:15-15:30 CT gated by `market_status != "OPEN"`, reopen anchors cap
  staleness) — no false positives.
- **Trailing:** `_check_trailing_stop:1155-1160` selects by frame PRESENCE — 5m frame exists →
  5m extremes granularity (the existing pin `test_5m_frame_still_drives_when_present_pin` covers
  this exact reversion; no change).
- **Heartbeat/telemetry:** `_log_heartbeat:3889-3894` (`_last_bar_time_5m`) reports real
  staleness again; `_build_heartbeat_payload:609-610` gets `_last_5m_bar_log`; `market_bars`
  telemetry fills again via `:2913-2917` — the T7 telemetry gap CLOSES for ES.
- **Reconnect:** `_deferred_resubscribe:2598-2607` re-subscribes 5m; `_backfill_reconnect_gap_
  async:2670-2728` stitches the 5m gap (manager guard `:2705` already present).
- **T7 residuals that become moot for ES** (remain only for future flag-FALSE configs, note for
  runbook): `_recover_inherited_position` and `_log_heartbeat` never got the T7-design
  primary-frame/1h fallbacks — at HEAD they read only the 5m artifacts (guarded, so hourly-only
  merely degrades: unseeded extremes / "no bars received yet"). Out of scope here.

### 3.2 T7 suite: what stays green
All hourly-only machinery tests in `tests/test_hourly_only_equity_session.py` use SYNTHETIC
flag-false configs (`_es_hourly_only_cfg`, `_build_hourly_only_trader`, tmp_path) — the flag stays
an opt-out, so `TestEnable5mStreamFlag`, `TestHourlyOnlyBoot`, `TestHourlyOnlyCacheSaveGuard`,
`TestTrailingFrameSelection`, `TestHourlyOnlyWatchdog` (incl. the 5m-enabled 15-min pins) all stay
green untouched. The fixture that "carries false" after the flip is the synthetic
`_es_hourly_only_cfg` — no shipped config carries false anymore.

### 3.3 Sanctioned test-pin evolutions (enumerated; cite this ticket)
File `tests/test_hourly_only_equity_session.py` is **Strict-Lock** — editing it requires exactly
this kind of sanction. **Six of these pins ALREADY FAIL at HEAD** (pre-existing regression:
`336d29f` changed ES01B to `execution_symbol: "MES"` / `client_id: 2000` without evolving pins;
verified by pytest run 2026-07-05):
1. `test_hourly_only_equity_session.py::TestES01BFlagPatch::test_es01b_carries_enable_5m_stream_false`
   — passes at HEAD; **must evolve** → asserts the key is ABSENT from `live_config`
   (per §5 config decision), i.e. ES01B rides the default-true/5m-enabled path.
2. `...::TestES01BFlagPatch::test_es01b_still_resolves_es_es_cme` — **FAILS at HEAD** (`'MES' ==
   'ES'`); evolve → execution MES / brain ES / exchange CME / tick 0.25.
3. `...::TestES01BFlagPatch::test_es01b_t6_sentinel_fields_unchanged` — **FAILS at HEAD**;
   evolve pins: `execution_symbol == "MES"`, `client_id == 2000`; keep the remaining sentinels
   (thresholds .53/.56, experiment ids, marketable_limit ×2, holdout 6, conflict_resolution,
   `"brain_symbol" not in cfg`).
4. `tests/test_instrument_context.py::TestShippedConfigs::test_es01b_shipped_config_resolves_as_es`
   — **FAILS at HEAD**; evolve → ctx MES/ES/CME + artifact existence unchanged.
5. `tests/test_config_generator_symbols.py::TestES01BPatchedConfig::test_patched_fields_match_audit_table`
   — **FAILS at HEAD**; evolve execution_symbol pin → "MES".
6. `...::TestES01BPatchedConfig::test_resolves_as_es_es_cme` — **FAILS at HEAD**; evolve as (2).
7. `...::TestES01BPatchedConfig::test_untouched_fields_pinned` — **FAILS at HEAD** (client_id
   1010); evolve → 2000.
No other tests pin the shipped ES01B fields (grep census: exactly these 3 files).
**Stays-green census for the DataManager change:** `tests/test_data_manager.py::
test_missing_seed_raises` (CL, `data_client=None`) and `tests/test_symbol_data_paths.py::
test_missing_non_cl_seed_raises_actionable` (ES, no client) construct DataManager DIRECTLY →
`allow_shallow_bootstrap` defaults False → raise preserved, green untouched.
`test_es_missing_1h_seed_raises_with_es_paths` pins the 1h raise — untouched by design.

## 4. CL byte-identity, no-silent-defaults, scope guards (deliverable 4)
- **CL byte-identity:** CL/HS14B has both legacy seed (`raw/cl-5m_bk.csv`) and cache — Step 1
  never reaches the else-branch; `allow_shallow_bootstrap=True` is dead code for CL (presence-
  conditional, like the T7 frame-presence argument). `_update_training_ledger` guard predicate
  (`ledger absent AND seed absent`) is False for CL. Zero CL config edits; parity-gate convention
  re-run post-green.
- **No-silent-defaults discipline:**
  - The shallow mode is LOUD: `log.warning("SHALLOW 5M BOOTSTRAP: ...")` in DataManager, a
    live_trader banner in `_warm_start`, and a `Mode:` stamp in the startup Telegram payload —
    the same three-surface discipline as the T7 HOURLY-ONLY banner (`:413-417`, `:841-843`).
  - The 5m-model raise keeps its EXACT message (`data_manager.py:303-308` byte-pinned).
  - No new silent fallbacks: `allow_shallow_bootstrap` defaults False at the DataManager level;
    empty IBKR return → RuntimeError (never an empty window); `data_client=None` + no seed/cache
    → today's FileNotFoundError (a bootstrap that CANNOT fetch must not soft-succeed).
  - The flag semantics are untouched: `enable_5m_stream:false` remains the explicit opt-out;
    hourly-only mode remains valid and loudly bannered.
- **Scope guards honored:** NO changes to fleet_runner (flag consumers are live_trader-only,
  grep-verified), backtest engine, generators (`generate_ensemble_artifacts.py` /
  `batch_post_optimizer.py` never write live_config keys), livetest harness/parity fence,
  ibkr_client, adapters, cli.py, session_calendar, instrument_master.

## 5. Severity, regression, file-by-file design (deliverable 5)

**Severity: MEDIUM** — multi-line but strictly localized (2 source files: data_manager.py +
live_trader.py; 1 config field removal; test evolutions; 7 doc lines). No refactor.
**Regression: NO** for this design (HEAD behavior is by-design; this is a user-decided extension).
**Pre-existing regression found:** 6 failing pins at HEAD from `336d29f` (§3.3) — repair is
subsumed by this ticket's sanctioned evolutions but should be acknowledged as a 336d29f defect
(config committed without test evolution — exactly what add-remove-fleet-model.md's own gate 
should have caught; see §6 doc note).

### 5.1 `src/live_execution/data_manager.py`
1. Module constant (near `_FLUSH_INTERVAL_BARS`):
   `_SHALLOW_BOOTSTRAP_DURATION = "5 D"` — comment carries the §1.2 justification.
2. `DataManager.__init__(..., allow_shallow_bootstrap: bool = False)` — keyword-only, stored;
   plus `self.shallow_bootstrapped: bool = False` (instance state, read by live_trader).
3. `initialize()` Step 1 else-branch becomes:
   ```python
   else:
       if self.allow_shallow_bootstrap and self.data_client is not None:
           self._df = self._shallow_bootstrap_from_ibkr()
           self.shallow_bootstrapped = True
           self.save_cache()   # run 2+ warm-starts from this cache
       else:
           # existing log.error + FileNotFoundError — BYTE-IDENTICAL
   ```
4. New method:
   ```python
   def _shallow_bootstrap_from_ibkr(self) -> pd.DataFrame:
       """SHALLOW 5M BOOTSTRAP (seedless non-5m models): fetch a few days of
       bars from IBKR to build the rolling window. NOT training-grade history."""
       log.warning("SHALLOW 5M BOOTSTRAP: no seed/cache for %s — fetching %s of %s "
                   "bars from IBKR (trailing/telemetry window; NOT training data)...",
                   self.symbol, _SHALLOW_BOOTSTRAP_DURATION, self.bar_size)
       df = self.data_client.fetch_historical_bars_by_duration(
           duration_str=_SHALLOW_BOOTSTRAP_DURATION, continuous=True,
           bar_size=self.bar_size, what_to_show="TRADES", use_rth=False)
       if df is not None and not df.empty:
           df = self._drop_incomplete_bar(df)
       if df is None or df.empty:
           raise RuntimeError(f"SHALLOW 5M BOOTSTRAP failed: IBKR returned no "
                              f"{self.bar_size} bars for {self.symbol}.")
       log.warning("SHALLOW 5M BOOTSTRAP: %d bars  %s → %s", len(df),
                   df.index.min(), df.index.max())
       return df
   ```
   (Return shape is already save_cache/append_bar-compatible — `ib_bars_to_dataframe` sets
   DateTime as index AND column, §1.2.)
5. `_update_training_ledger()` — gate the first-run creation branch only:
   ```python
   else:
       if self.allow_shallow_bootstrap and not self.seed_path.exists():
           log.warning("SHALLOW MODE: master-ledger accrual SKIPPED for %s — no seed "
                       "to found a training ledger (cache still accrues raw bars + "
                       "roll metadata). Provision %s to start the ledger.",
                       self.symbol, self.seed_path)
           return
       ledger = self._load_full_seed()   # unchanged raise for 5m models
   ```

### 5.2 `src/live_execution/live_trader.py`
6. `__init__` 5m DataManager construction (`:400-410`): add
   `allow_shallow_bootstrap=(self._bar_size in ("1h", "2h", "4h"))` (comment cites this ticket +
   the parity-note rationale: only 5m MODELS consume deep 5m features). Init
   `self._shallow_5m_bootstrap: bool = False` in the state block (`~:487`).
7. `_warm_start()` after `initialize()` (`:2020`):
   ```python
   self._shallow_5m_bootstrap = bool(getattr(self.data_manager_5m, "shallow_bootstrapped", False))
   if self._shallow_5m_bootstrap:
       log.warning("SHALLOW 5M MODE: no 5m seed/cache existed — window bootstrapped "
                   "from IBKR (%d bars). Run 2+ warm-starts from the saved cache.",
                   len(self.rolling_df_5m))
   ```
8. start() Telegram stamp (mirror of the T7 block at `:841-844`):
   ```python
   if getattr(self, "_shallow_5m_bootstrap", False):
       startup_msg += "Mode: `5M SHALLOW BOOTSTRAP (no 5m seed — IBKR-fetched window)`\n"
   ```

### 5.3 `configs/strategies/ES01B_Sharpe_E03_07042026.json`
9. **REMOVE line 22** `"enable_5m_stream": false` (leave client_id/entry_mode/exit_mode).
   **Decision — remove vs set true:** REMOVE is cleaner: (a) restores "flag present ⟺ deviation
   from default" — the whole CL fleet omits the key, so ES01B exercises the IDENTICAL default
   construction path (maximum sentinel coverage of the new mode); (b) the sentinel evolves to
   `"enable_5m_stream" not in cfg["live_config"]` — a strong anti-drift pin against silently
   re-adding false; (c) generators never emit live_config keys, so regenerated configs match.
   (Explicit `true` would be behaviorally identical — `bool(get(key, True))` — but creates a
   third config shape and a weaker pin.)
   **Sequencing (fleet-live caveat):** ES01B is ENABLED (dry-run) in
   `configs/fleet/fleet_manifest.json` and the fleet may be running from this checkout. The code
   (§5.1-5.2) MUST land in the same commit as (or before) the config flip, else the next fleet
   restart crash-loops the ES child on the 5m FileNotFoundError. Running processes are unaffected
   until restart.

### 5.4 Docs (deliverable / sketch item 5) — exact lines
Replace "non-CL 1h models must set enable_5m_stream:false" guidance with: *historical/training
acquisition stays hourly-only (no 5m purchases); live 5m streaming is the DEFAULT for every
symbol — seedless symbols SHALLOW-BOOTSTRAP the 5m window from IBKR on first run (loud banner +
Telegram stamp) and warm-start from the saved cache thereafter; `enable_5m_stream: false` remains
a valid explicit opt-out; 5m MODELS (bar_size "5m") still hard-require a real seed.*
1. `.agents/workflows/build-symbol-pipeline.md:87-88` (Phase 1 item 7 tail: "New symbols run the
   live engine in hourly-only mode (...enable_5m_stream: false..., Phase 6 gate 2)").
2. `.agents/workflows/build-symbol-pipeline.md:226-229` (Phase 6 "Gate 2 — hourly-only stamp":
   MUST-set-false + "startup then fails on the missing 5m seed" — becomes a gate that the config
   either omits the key (default 5m + shallow bootstrap) or deliberately opts out).
3. `.agents/workflows/run-live.md:47-50` (Preflight item 4).
4. `.agents/workflows/add-remove-fleet-model.md:41-42` (ADD prereq: "enable_5m_stream: false for
   any non-CL hourly model"). Also note: this workflow's validation gate should have caught the
   336d29f pin breakage — add "run the ES01B/T6 sentinel tests after any shipped-config edit" to
   its gate list.
5. `.agents/workflows/grab-data.md:11-14` (IMPORTANT block: keep "acquisition is HOURLY-ONLY —
   never acquire 5m data"; drop "their configs set enable_5m_stream: false").
6. `docs/headless-deployment.md:239-241` (bullet "`enable_5m_stream: false` for symbols without a
   5m seed").
7. `deploy/systemd/README.md:151-153` (same bullet).
T7 canary-expectation deltas (audit_hourly_only.md §7 "Amended canary expectations") flip back for
ES: expect `Subscribed to 5-min continuous`, `NEW 5M BAR:` lines, and the SHALLOW banner on run 1.

### 5.5 TDD test list (new file, e.g. `tests/test_shallow_5m_bootstrap.py`, + sanctioned edits §3.3)
1. **CL-with-seed pins (anti-drift):** CL config, tmp seed+cache present → `initialize()` takes
   the cache/seed branch; `shallow_bootstrapped is False`; NO "SHALLOW" log record; NO bootstrap
   fetch call. DataManager-level: `allow_shallow_bootstrap` DEFAULTS False (signature pin).
2. **live_trader wiring:** 1h config → MockDM 5m call kwargs carry `allow_shallow_bootstrap=True`;
   `bar_size:"5m"` config → `allow_shallow_bootstrap=False` (the NaN-guard discriminator pin).
3. **Shallow bootstrap happy path (mocked IBKR):** ES-shaped DataManager, tmp paths ABSENT,
   mocked feed returns a 3-day 5m OHLCV frame → `initialize()` returns it; cache file CREATED on
   disk; `shallow_bootstrapped is True`; fetch called ONCE with `duration_str="5 D"`,
   `continuous=True`, `bar_size="5 mins"`; log contains "SHALLOW 5M BOOTSTRAP".
4. **Run-2 cache warm-start:** fresh DataManager over the same tmp cache (seed still absent) →
   cache branch, NO bootstrap fetch, `shallow_bootstrapped is False`.
5. **5m-model-still-raises pin:** `allow_shallow_bootstrap=False`, no seed/cache →
   FileNotFoundError with the EXACT existing message (byte pin on "Seed file not found for" +
   CL_DATA_ROOT hint); also `allow=True` + `data_client=None` → still raises.
6. **Empty-fetch hard fail:** mocked feed returns empty frame → RuntimeError (no silent empty
   window; `_warm_start`'s ≥1-bar contract).
7. **Ledger skip:** shallow-state (`allow=True`, seed absent, ledger absent) →
   `_update_training_ledger` returns, no `_load_full_seed` call, WARNING logged, no ledger file
   created; ledger PRESENT + seed absent → append path unchanged; `allow=False` → raise pin.
8. **Loud-log/Telegram pins:** `_warm_start` with a stub manager (`shallow_bootstrapped=True`) →
   `_shallow_5m_bootstrap` set + banner containing "SHALLOW" and "5m"; startup payload contains
   the Mode stamp (reuse the T7 telegram seam); flag-false configs still emit HOURLY-ONLY
   (existing pins untouched).
9. **ES01B flip sentinels:** key absent from live_config; resolves execution MES / brain ES /
   CME; evolved T6/T7 sentinels per §3.3 (MES, cid 2000, artifact existence).
10. **Watchdog/trailing reversion for 5m-enabled ES:** ES-shaped default-flag stub →
    `_check_stale_bars` reads `_last_bar_time_5m` against 15 min (16 min stale → True, 10 min →
    False; reuse T7 stubs with `_enable_5m_stream=True`); trailing reads the 5m frame when
    present (existing `test_5m_frame_still_drives_when_present_pin` — cite, no change needed).
11. Post-green: HS14B ledger parity gate re-run (convention, expect $0.00) + full T5/T6/T7 suites.

## 6. Open questions requiring HUMAN AUTHORIZATION
1. **ES01B config edit sign-off:** remove `enable_5m_stream: false` (recommended, §5.3) vs
   explicit `true`. Either edits a T6/T7-frozen shipped config that is ENABLED (dry-run) in the
   live fleet manifest — approve the edit + same-commit sequencing with the code.
2. **336d29f pin repair:** acknowledge the 6 pre-existing failing pins (§3.3) as a 336d29f defect
   and sanction their evolution INSIDE this ticket's TDD (they block any clean red/green run).
3. **Ledger-accrual skip (§1.4):** ack that `es_continuous_master.parquet` (5m) will NOT be
   created while ES is seedless (cache + roll metadata accrue everything; ledger founds itself
   the day a real seed lands). Alternative (found the ledger from the shallow window) rejected as
   silent-misdata risk — confirm.
4. **Duration constant:** `"5 D"` (§1.2) — ack or override.
5. **Deferred (pre-existing, noted §3.1):** hourly-only residuals in `_recover_inherited_position`
   / `_log_heartbeat` for future flag-false configs; 1h-stream watchdog for 5m-enabled instances
   (T7 C6). Micro-ticket candidates, not this ticket.
