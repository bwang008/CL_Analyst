# Audit — t8-workflow-doc-gates_07052026_0403
**Ticket:** T8 — Workflow doc remediation (from `multi-symbol-live-gaps_07042026_1520/blueprint.md` §T8, user-requested)
**Auditor:** Ticket-Auditor | **Date:** 2026-07-05 | **Branch:** development @ `3738516` (T1–T7 merged)
**Scope:** DOC-ONLY. This audit inventories every workflow/prompt doc that generates or consumes
configs/manifests/registry entries, fact-checks the ground truth at HEAD, and drafts the exact gate
insertions. No source code and no workflow doc was modified — all writes are inside this ticket folder.

---

## 1. Ground truth at HEAD (fact-checked — every claim below verified against `3738516`)

| # | Fact | Verified at |
|---|------|-------------|
| G1 | `execution_symbol` REQUIRED; missing/unknown/mismatched raises before IBKR construction. Resolver: `src/live_execution/instrument_context.py::resolve_instrument_context` (also exported: `derive_model_symbol`, `validate_models_against_symbol`). `models.<side>.symbol` HARD-enforced when present; symbol tag in `experiment_id` (`E2E_<SYM>_...`) checked opportunistically. | `instrument_context.py:42-163`; wired in `cli.py` (fail-fast pre-factory) and `live_trader.py.__init__` |
| G2 | Registry `Instrument` now has **17 required fields** + 2 defaulted (`micro_of=None`, `slippage_ticks=1`): `symbol, name, tick_size, tick_value, cftc_code, volatility_index, exchange, multiplier, quote_unit_usd, active_months, roll_reference, roll_buffer_days, session_hours_ct, bars_per_day_5m, bars_per_day_1h, live_vol_index, roll_ratio_tolerance`. Test-enforced invariant: `tick_value == tick_size * multiplier * quote_unit_usd`. 15 entries (8 outrights + 5 micros + NG/HG/PA...). | `src/core/instrument_master.py:4-348` |
| G3 | `session_hours_ct` dispatches on the EXACT shape tuple — only `_GLOBEX_SESSION`, `_GRAINS_SESSION`, `_EQUITY_SESSION` are modeled; an unknown shape RAISES (`_unsupported_session_shape`) → a new symbol with an unmodeled session **will not start live**. New shape = SDLC code change in `session_calendar.py`. | `src/live_execution/session_calendar.py:236-283`; `instrument_master.py:33-41` |
| G4 | Per-symbol data paths via `derive_data_paths(symbol)` (`DataPaths`): 1h seed = `data/processed/{SYM}_raw_1h.parquet`, cache `warm_start_cache_{SYM}[_1h].parquet`, ledger `{sym}_continuous_master[_1h].parquet`, roll metadata `.roll_metadata_{SYM}.json` (CL keeps 3 legacy names). Missing seed RAISES. Floor: `REQUIRED_1H_BARS = 4320`; lookback `derive_seed_lookback_days(bars_per_day_1h)` → CL 280 / ES 292 / ZC 406 days. | `data_manager.py:57-127,225-227` |
| G5 | Per-symbol macro: `fred_macro_data_<sym>.csv` + `cftc_cot_<sym>.csv`; FRED file missing the instrument's vol column HARD-RAISES (T4 Q1); live daily-close fetch list is instrument-derived (CL `["VIX","OVX"]`, GC `["VIX","GVZ"]`, ES/ZC/ZS/SI `["VIX"]`). | `src/features/macro_features.py`; T4 tdd_result |
| G6 | Hourly-only mode: `live_config.enable_5m_stream` (default **true**); `false` = no 5m DataManager/seed/subscription; requires hourly `bar_size`. NO 5m data acquisition — Databento in this repo is hourly-only (USER RULING, T7). `ES01B_Sharpe_E03_07042026.json` carries `"enable_5m_stream": false`. | `live_trader.py:331-345,395-414`; config line 22 |
| G7 | **T6-C1 residual (code fix deferred → workflow gate):** the target-pairs post-opt path (`agent/batch_post_optimizer.py:1045,:1071` → `opt_tasks.append((..., base_config_path, ...))` at :1134) deep-copies the raw CL base and `agent/strategy_optimizer.py` writes the results as `_opt_` (**:1443-1447**) / `_hybrid_` (**:1868-1872**) configs into `configs/strategies/` (dirname of the base config) with NO symbol stamping → future non-CL batches would emit CL-labeled/CL-parameterized candidates. | verified at HEAD |
| G8 | **T6-C2 residual:** local generator falls back silently to the CL base when the manifest lacks `defaults.strategy_config` — at HEAD this is `agent/generate_ensemble_artifacts.py:303` (the spawn brief's ":272" moved after T6's symbol-validation insertion; :272 is now a comment in that block) and `batch_post_optimizer.py:1045/:1071` (reads `batch_progress.json`'s `defaults`). **Verified: NONE of the 34 `configs/batch_manifest_v2_*.json` carries a `defaults` block** — the fallback is ACTIVE on every local re-generation today, including the in-flight ZC standup. Post-T6 the emitted config still gets correct symbols stamped (from `baseline.symbol`) + a `resolve_instrument_context` self-check (`generate_ensemble_artifacts.py:484`), so the residual risk is silent inheritance of CL-tuned strategy parameters (blocked entry hours, thresholds, offsets — blueprint m5), plus a v2 asymmetry: the VM side REQUIRES `baseline.execution_workflow.strategy_config_path` (`vm_post_optimize.sh:180-182`) while the local side ignores it and reads `defaults`. `BatchSweepConfig` ignores extra top-level keys (pydantic default; `schemas.py:229-237`), so adding a `defaults` block to a v2 manifest is schema-safe. | verified at HEAD |
| G9 | Shared dataset-tag authority: `src/core/dataset_tag.py::derive_dataset_tag` (T6) — generator and `gcp/vm_e2e_pipeline.py` can no longer diverge on `E2E_*` names. Docs must reference it instead of describing ad-hoc prefix rules. | T6 tdd_result |
| G10 | `ES01B_Sharpe_E03_07042026.json` at HEAD is FIXED (resolves ES/ES/CME; `models.*.symbol: ES`; real `E2E_HourSet_01B_*` model paths; predictions under `batch_20260704_0701_ES_01B_SCOUT`). The walk-through in §6 uses the PRE-T6 broken values documented in the T6 ticket. | config inspected |

---

## 2. Doc inventory + staleness verdicts

Legend: **GATE** = must gain a validation gate; **UPDATE** = stale claims to correct; **NOTE** = one-line warning/cross-ref; **DEPRECATE** = banner pointing to the live doc; **ARCHIVE** = historical, leave untouched.

### 2a. `.agents/workflows/` (live)

| Doc | Verdict | Staleness vs T1–T7 |
|-----|---------|--------------------|
| `build-symbol-pipeline.md` | **GATE + UPDATE (primary)** | Phase 0 §2 lists only `SYMBOL_MAP`, `cftc_code`, `volatility_index`, COT family — **wrong/incomplete**: 14 more required registry fields, no session-shape rule, no completeness test. Phase 1 lacks the live 1h seed (`{SYM}_raw_1h.parquet`, T7) and the hourly-only ruling. Phase 5 line 90 treats the CL-derived baseline as "flag a follow-up" — the exact softness that shipped ES01B broken. Phase 6 "SUCCESS" checks artifacts + PnL but never validates the emitted configs. No C1/C2 warnings, no `enable_5m_stream`, no resolver mention. Full insertions in §3. |
| `run-cloud-batch.md` | **GATE + UPDATE** | Output section lists `configs/` as "backtest-ready" with no validation step; no mention `baseline.symbol` is REQUIRED (generator raises post-T6); §2/§3 use `powershell -ExecutionPolicy Bypass -File .\gcp\run_sweep_batch.ps1` — build-symbol-pipeline's hard rules say that prefix is blocked by a safety classifier (internal contradiction; canonical form is `& .\gcp\run_sweep_batch.ps1 ...`). |
| `post-optimize.md` | **GATE + UPDATE** | Option B documents the exact **C1 path** (`batch_post_optimizer.py` local + `generate_batch_configs.py`) with zero symbol caveats; output section calls emitted configs "correctly-formatted... ready for live_trader.py". Needs the C1 quarantine warning + the §3.4 validation gate applied to any batch `configs/` dir. |
| `generate-trade-configs.md` | **GATE** | Step 3 "Duplicate the baseline canary config" is the ES01B defect pattern verbatim (execution_symbol/model symbols inherited from the donor). Must require: stamp `execution_symbol` + `models.*.symbol`, then run the §3.4 gate before Step 4's backtest. |
| `run-live.md` | **UPDATE + NOTE** | Uses the legacy `python -m src.live_execution.live_trader` entry; the canonical multi-symbol entry is `python -m src.live_execution.cli --config <json> [--dry-run]` (T1 fail-fast lives there; both validate, but docs should show one). No preflight note that startup now hard-raises on missing `execution_symbol`/seed/macro files; no hourly-only note for non-CL configs. |
| `grab-data.md` | **UPDATE** | Symbol table missing ZC/ZS/SI (registry has them; ZC canary validated 2026-07-04). Must state the T7 ruling: acquisition here is HOURLY-ONLY — never acquire 5m; new symbols run live with `enable_5m_stream: false`. |
| `generate-data.md` | **NOTE** | Mechanically current. Add one line: the DataMap `symbol` must be a fully-registered `INSTRUMENT_REGISTRY` entry (Phase 0 of build-symbol-pipeline) — `MacroFeatureEngine` now hard-raises on missing per-symbol FRED/COT files and missing vol column (T4). |
| `sweep-ensembles.md` | **NOTE** | Base-config inheritance: add warning that every field of `--base-config` (including `execution_symbol`) propagates into derived configs — non-CL sweeps need a symbol-correct base. |
| `run-cloud-experiment.md` | **UPDATE + NOTE** | Legacy v1 single-VM flow (pre-v2-manifest); its "Configuration Naming & File Tracking Conventions" section instructs saving generated configs into `configs/strategies/` with no validation — attach the §3.4 gate reference. Consider a banner deferring to `/run-cloud-batch` for batch work. |
| `run-vector-cloud-batch.md` | **DEPRECATE (banner)** | References retired legacy manifests (`configs/sweep_batch_hourset09_*.json`, `defaults`-schema) that predate `BatchSweepConfig` v2. Mark legacy; point at run-cloud-batch. |
| `livetest.md` | **UPDATE (minor)** | "Known Architectural Differences" §1 claims BacktestEngine "does NOT round" and `round(price, 2)` is CL-correct — stale twice over (B(a) fix rounds backtest brackets; T3 made rounding instrument-tick via `round_to_tick`). "Adapting for Other Configs" should say the config must first pass the resolver. |
| `validate-parity.md` | **NOTE** | Current for CL. Add scope note: the suite does NOT exercise session calendar/watchdog/front-month/seed-math (T5 pins are the only fence) and is CL-fixtured — green parity says nothing about a non-CL config's correctness. |
| `validate-ledger-parity.md` | **NOTE** | Same scope note; commands/fixtures verified current (HS14B, `--disable-trailing`, PASS baseline). No structural edit. |
| `smoketest.md` | **UPDATE (minor)** | Warm-start cache filename list lacks the per-symbol names `warm_start_cache_{SYM}[_1h].parquet` (T2 paths, T6 cadence-regex update). |
| `run-experiment.md`, `analyze-trade-patterns.md`, `gen-feature-report.md`, others | **no action** | Consume configs read-only or are unrelated to config/manifest/registry emission. |

### 2b. Legacy `.agent/workflows/` (old dir — decision requested in brief)

| Doc | Verdict |
|-----|---------|
| `run-cloud-experiment.md`, `run-experiment.md` | **DEPRECATE** — stale duplicates of `.agents/workflows/` copies; two sources of truth is exactly how doc drift regenerates. Recommendation: 3-line banner at top ("DEPRECATED — maintained copy: `.agents/workflows/<name>.md`; do not edit here") rather than deletion (preserves inbound links). Deletion requires human authorization. |
| `terminal-output.md` | **no action** — environment advice, no config content, no `.agents/` twin. Optionally migrate later. |

### 2c. `docs/prompts/`

All 14 files are dated, one-shot handoff prompts (HourSet_07, set_07, EXP-030, Track-4, etc.). Verdict: **ARCHIVE — no updates.** They are historical records, not live workflows; retro-editing them would falsify history. `run_local_optimizer_prompt.md`/`sortino_optimizer_prompt.md` do describe the C1 path but are pinned to specific CL batches. (Optional, needs authorization: a one-line `README` in that folder declaring it archival.)

### 2d. `deploy/` + `docs/headless-deployment.md`

| Doc | Verdict | Staleness |
|-----|---------|-----------|
| `docs/headless-deployment.md` (Fleet Runner §) | **UPDATE** | Describes fleet validation as client_id-only. Missing post-T1/T2/T4/T7 truths: each child config must carry a truthful `execution_symbol` (child fail-fasts via `resolve_instrument_context` in `cli.py`); per-symbol artifacts must exist on the host data root (`{SYM}_raw_1h.parquet` seed ≥4,320 1h bars, `fred_macro_data_<sym>.csv`, `cftc_cot_<sym>.csv`) or the child crash-loops under the runner's restart backoff; non-CL configs without a 5m seed MUST set `enable_5m_stream: false`. "Cloud Migration Notes" §3 should say per-symbol seeds/macro CSVs. |
| `deploy/systemd/README.md` (fleet §) | **UPDATE** | Same gap: manifest prerequisites mention client_id spacing only — add the per-config symbol/artifact prerequisites above. |
| `deploy/systemd/*.service`, `setup_ubuntu.sh` | **no action** | `live-trader.service` still ExecStarts the legacy module entry — works (LiveTrader init validates); unit-file churn is out of doc-only scope. |

---

## 3. build-symbol-pipeline.md — exact gate insertions (drafted, fact-checked)

### 3.1 Phase 0 §2 — replace with the complete registration gate

> 2. Register the instrument **completely** — post-T1/T5 the registry is the single source of truth
>    for the live engine; every field is REQUIRED (dataclass has no defaults except `micro_of`,
>    `slippage_ticks`) and live startup RAISES on gaps:
>    - `src/data/databento_data_builder.py` → `SYMBOL_MAP["<SYM>"] = "<SYM>.v.0"`.
>    - `src/core/instrument_master.py` → `INSTRUMENT_REGISTRY["<SYM>"]` with ALL fields:
>      - identity: `symbol`, `name`
>      - pricing: `tick_size`, `tick_value`, `multiplier`, `quote_unit_usd` (0.01 for grains quoted
>        in cents/bu) — **invariant, test-enforced:** `tick_value == tick_size * multiplier * quote_unit_usd`.
>        T3 snaps every live order price to `tick_size` (`round_to_tick`) — a wrong tick = rejected orders.
>      - training: `cftc_code`, `volatility_index` (FRED series: equities → `VIXCLS`; energy → `OVXCLS`;
>        gold → `GVZCLS`; grains/silver/copper → `VIXCLS` proxy, no FRED series exists)
>      - routing: `exchange` (IBKR string: NYMEX / CME / COMEX / CBOT)
>      - rollover: `active_months` (MGL codes — EXCLUDE illiquid serials, e.g. GC=`"GJMQVZ"`),
>        `roll_reference` (`"LTD"`, or `"FND"` for physically delivered), `roll_buffer_days`
>        (CL 6, ES/NQ 8, FND-referenced metals/grains 3), `roll_ratio_tolerance` (CL/MCL pinned
>        `0.01`; all new symbols `0.001`)
>      - session: `session_hours_ct` — **MUST reuse one of the three modeled shapes**:
>        `_GLOBEX_SESSION` (17:00–16:00 CT), `_GRAINS_SESSION` (19:00–07:45 + 08:30–13:20 CT),
>        `_EQUITY_SESSION` (17:00–15:15 + 15:30–16:00 CT). `src/live_execution/session_calendar.py`
>        dispatches on the exact tuple and **RAISES on any other shape** — an instrument with an
>        unmodeled session will not start live. A new session shape is an SDLC code change: STOP,
>        report, test-first.
>      - provisioning: `bars_per_day_5m`, `bars_per_day_1h` (drives the live seed-lookback formula;
>        24h markets 288/24 (CL pins), 23h 276/23, grains 200/16)
>      - live vol: `live_vol_index` (IBKR CBOE index symbol `"VIX"`/`"OVX"`/`"GVZ"` — NOT the FRED name)
>      - micro sibling (if traded): a separate `M<SYM>` entry with `micro_of="<SYM>"`, inheriting
>        the parent's `cftc_code`/`volatility_index` (micros are execution-only).
>    - `scripts/download_macro_data.py` → `COT_REPORT_BY_SYMBOL["<SYM>"]` … *(existing text unchanged)*
>    - **GATE 0 — registry completeness (blocking):**
>      ```
>      conda run -n trader python -m pytest tests/test_instrument_master_live_fields.py tests/test_instrument_context.py -q
>      ```
>      Both suites green before Phase 1. They enforce field completeness, the tick-value invariant,
>      session-shape membership, and resolver behavior for every registry entry. (A `TypeError` on
>      `Instrument(...)` construction = you omitted a required field — that is the intended failure.)

### 3.2 Phase 1 — append step 7 (live seed + hourly-only ruling)

> 7. **Live 1h seed (T7):** stage `data/processed/<SYM>_raw_1h.parquet` — the per-symbol live seed
>    resolved by `derive_data_paths()` (`src/live_execution/data_manager.py`); a missing seed RAISES
>    at live startup. Since `<SYM>_raw.parquet` is already hourly, a copy suffices — but verify it
>    holds ≥ **4,320** hourly bars (`REQUIRED_1H_BARS`) *inside the instrument's lookback window*
>    (`derive_seed_lookback_days(bars_per_day_1h)`: ES 292 calendar days, grains 406) and re-stage
>    near launch time (the window decays ~1 trading day/day).
>    **NO 5m acquisition — Databento in this repo is hourly-only (USER RULING, T7).** New symbols
>    run the live engine in hourly-only mode (`live_config.enable_5m_stream: false`, Phase 6 gate 2).

### 3.3 Phase 5 — replace the soft baseline sentence (current line 90, "…flag a symbol-tuned baseline as a follow-up") with

> - `baseline.execution_workflow.strategy_config_path` may point at the CL base
>   `configs/strategies/hourly_ensemble_010.json` for machinery validation — but every config the
>   batch derives from it MUST pass the Phase 6 CONFIG VALIDATION GATE before any backtest/live use.
>   Two generator residuals are open at HEAD (T6 audit; code fixes deliberately deferred):
>   - **C1 — do NOT ship `_opt_`/`_hybrid_` configs from the target-pairs path for a non-CL symbol.**
>     `agent/batch_post_optimizer.py` (target-pairs mode, `:1045-1134`) hands the raw CL base to
>     `agent/strategy_optimizer.py`, which writes `*_opt_*.json` (`:1443-1447`) and `*_hybrid_*.json`
>     (`:1868-1872`) into `configs/strategies/` with **no symbol stamping**. Until the code fix lands,
>     quarantine/delete any such files a non-CL batch produces and regenerate via
>     `agent/generate_ensemble_artifacts.py` (which stamps `execution_symbol` + `models.*.symbol`
>     from `baseline.symbol` and self-checks with `resolve_instrument_context`).
>   - **C2 — non-CL manifests MUST carry a `defaults` block.** The local generator
>     (`agent/generate_ensemble_artifacts.py:303`; same pattern `batch_post_optimizer.py:1045/:1071`)
>     ignores `strategy_config_path` and reads `defaults.strategy_config`, silently falling back to
>     the CL base when absent — and NO v2 manifest carries `defaults` today. Add
>     `"defaults": {"strategy_config": "<symbol-baseline>.json", "local_data_path": "<local parquet>", "local_exec_data": "<local raw parquet>"}`
>     mirroring the `baseline.execution_workflow` values (`BatchSweepConfig` ignores the extra key —
>     verified). A CL-parameterized deep-copy is silent misconfiguration even when the symbols are
>     stamped correctly (CL-tuned blocked hours/thresholds/offsets).

### 3.4 Phase 6 — insert the POST-CANARY CONFIG VALIDATION GATE (new, blocking; between the artifact list and "substantively valid")

> **CONFIG VALIDATION GATE (hard — the workflow may not report success without exit 0).**
> For EVERY strategy config in `reports\batch_runs\batch_<ID>\configs\` (and any config promoted to
> `configs/strategies/`), run a scratchpad script (per the hard rules — no multi-line `python -c`):
>
> ```python
> # <scratchpad>\validate_batch_configs.py  — usage: python validate_batch_configs.py <batch_dir>
> import json, sys
> from pathlib import Path
> from src.live_execution.instrument_context import resolve_instrument_context
>
> batch_dir = Path(sys.argv[1])
> manifest = json.loads((batch_dir / "manifest.json").read_text())
> expected = manifest["baseline"]["symbol"].upper()          # KeyError here = manifest bug: fix the manifest
> failures = []
> for cfg_path in sorted((batch_dir / "configs").glob("*.json")):
>     cfg = json.loads(cfg_path.read_text())
>     try:
>         ctx = resolve_instrument_context(cfg)              # raises on missing/unknown symbol + model-tag mismatch
>         if ctx.execution_symbol != expected:
>             raise ValueError(f"execution_symbol {ctx.execution_symbol!r} != manifest baseline.symbol {expected!r}")
>         for side, m in cfg.get("models", {}).items():
>             if not m.get("symbol"):
>                 raise ValueError(f"models.{side}.symbol missing (T6 generator stamps it — regenerate, don't hand-patch)")
>             for key in ("model_path", "predictions_path"):
>                 p = m.get(key)
>                 if not p or not Path(p).exists():
>                     raise ValueError(f"models.{side}.{key} not on disk: {p}")
>     except Exception as e:
>         failures.append(f"{cfg_path.name}: {e}")
> print("\n".join(failures) or "CONFIG GATE: PASS")
> sys.exit(1 if failures else 0)
> ```
> ```
> conda run -n trader python <scratchpad>\validate_batch_configs.py reports\batch_runs\batch_<ID>
> ```
> Exit 0 required. **Any failure = the canary FAILED**, regardless of PnL/artifact checks. Checks, per
> config: (a) resolves via `resolve_instrument_context` (execution_symbol present + registered, model
> symbol tags consistent), (b) `execution_symbol == manifest baseline.symbol`, (c) `models.*.symbol`
> present, (d) every `model_path` exists on disk, (e) every `predictions_path` exists on disk.
>
> **Gate 2 — hourly-only stamp:** any config destined for live on a symbol with no 5m seed (ALL new
> symbols — hourly-only ruling) MUST set `"live_config": {"enable_5m_stream": false, ...}`; the key
> defaults to `true` and startup then fails on the missing 5m seed. Verify on the promoted config.
>
> **Gate 3 — C1 quarantine:** if the post-optimizer ran in target-pairs mode, list
> `configs/strategies/*_opt_*.json` / `*_hybrid_*.json` created during the run and quarantine them
> (see Phase 5 C1) — they are unstamped CL-base clones.

### 3.5 Phase 7 §3 (report) — add
> …canary artifact-validation result **including the CONFIG VALIDATION GATE output (must be PASS)**…

### 3.6 Artifact checklist — add three boxes
> - [ ] `data/processed/<SYM>_raw_1h.parquet` live seed staged (≥4,320 1h bars in-window; no 5m data).
> - [ ] CONFIG VALIDATION GATE exit 0 on `reports\batch_runs\batch_<ID>` (resolver + symbol + paths).
> - [ ] No unquarantined `*_opt_*/*_hybrid_*` CL-base configs in `configs/strategies/`; non-CL manifests carry a `defaults` block.

### 3.7 Key-files table — add rows
> | `src/live_execution/instrument_context.py` | `resolve_instrument_context` — config fail-fast validation (T1) |
> | `src/live_execution/session_calendar.py` | the three modeled session shapes; unknown shape raises (T5/T7) |
> | `src/live_execution/data_manager.py` | `derive_data_paths` per-symbol seed/cache/ledger names; `REQUIRED_1H_BARS` (T2/T5) |
> | `src/core/dataset_tag.py` | `derive_dataset_tag` — the ONLY authority for `E2E_*` names (T6) |
> | `agent/generate_ensemble_artifacts.py` | config (re)generation with symbol stamping + self-check (T6) |

---

## 4. Per-file edit plan (all other docs)

| # | File | Edit |
|---|------|------|
| 1 | `.agents/workflows/run-cloud-batch.md` | (a) Add step 5 "Validate generated configs" = §3.4 gate command against the batch dir; (b) in the manifest intro, state `baseline.symbol` is REQUIRED (generator hard-raises) and non-CL manifests need the C2 `defaults` block; (c) fix both `powershell -ExecutionPolicy Bypass -File` invocations to `& .\gcp\run_sweep_batch.ps1 ...` (classifier-blocked prefix); (d) annotate the `configs/` line in the output tree: "subject to the config validation gate before use". |
| 2 | `.agents/workflows/post-optimize.md` | (a) Warning box on Option B: local `batch_post_optimizer.py` in target-pairs mode is the C1 path — never ship its `_opt_/_hybrid_` emissions for non-CL symbols; (b) after "Download results", add the §3.4 gate on the downloaded `configs/`; (c) same `powershell -ExecutionPolicy Bypass` fix. |
| 3 | `.agents/workflows/generate-trade-configs.md` | Step 3 gains sub-steps: set `execution_symbol` to the target symbol, set `models.long.symbol`/`models.short.symbol`, then run the §3.4 checks on the single config (resolver + on-disk paths) BEFORE Step 4's backtest. Note that duplicating a donor config inherits its symbol — the ES01B defect. |
| 4 | `.agents/workflows/run-live.md` | (a) Switch examples to `conda run -n trader python -m src.live_execution.cli --config <json> --dry-run` (keep a note that the legacy module entry still exists); (b) add a "Preflight" list: config resolves (resolver one-liner), `{SYM}_raw_1h.parquet` seed present, `fred_macro_data_<sym>.csv`/`cftc_cot_<sym>.csv` present, `enable_5m_stream: false` for symbols without 5m seeds — startup hard-raises on each of these by design. |
| 5 | `.agents/workflows/grab-data.md` | Add ZC/ZS/SI (+ CBOT/COMEX) to the symbol table; add the hourly-only ruling banner (no 5m acquisition, ever, per T7 user ruling); point Step 7's "pipeline compatibility" at build-symbol-pipeline Phase 0's registry gate. |
| 6 | `.agents/workflows/generate-data.md` | One NOTE line (§2a). |
| 7 | `.agents/workflows/sweep-ensembles.md` | One NOTE line (§2a). |
| 8 | `.agents/workflows/run-cloud-experiment.md` | Banner: batch work → `/run-cloud-batch`; in "Configuration Naming & File Tracking Conventions", require the §3.4 single-config checks before a config may enter `configs/strategies/`. |
| 9 | `.agents/workflows/run-vector-cloud-batch.md` | DEPRECATED banner (legacy manifest schema). |
| 10 | `.agents/workflows/livetest.md` | Fix "Known Architectural Differences" §1 (post-B(a)/T3: both engines round to the instrument tick via `round_to_tick`); add resolver precondition in "Adapting for Other Configs". |
| 11 | `.agents/workflows/validate-parity.md` + `validate-ledger-parity.md` | Scope NOTE: CL-fixtured; does not validate non-CL configs nor the session/watchdog/front-month layer (T5 pins are the only fence). |
| 12 | `.agents/workflows/smoketest.md` | Extend cache-cadence filename list with `warm_start_cache_{SYM}[_1h].parquet`. |
| 13 | `.agent/workflows/run-cloud-experiment.md`, `.agent/workflows/run-experiment.md` | DEPRECATED banner → `.agents/workflows/` twins (deletion needs human authorization). |
| 14 | `docs/headless-deployment.md` | Fleet Runner §: add per-config prerequisites (truthful `execution_symbol` — child fail-fasts via resolver; per-symbol seed/macro files on the host or the child crash-loops; `enable_5m_stream: false` for 5m-seedless symbols); Cloud Migration §3: per-symbol artifacts. |
| 15 | `deploy/systemd/README.md` | Same fleet prerequisites paragraph. |
| 16 | `docs/prompts/*` | ARCHIVE — no edits. |

---

## 5. Severity / regression classification

**Severity: MEDIUM** (multi-file, structural doc changes; no code, no tests, no refactor).
**Regression class: DOC DRIFT — no code regression.** All T1–T7 code is merged and green
(1381 passed at T7; ledger parity PASS ×8). The defect being remediated is process-level: the
workflows that guided the ES standup still describe (and would re-produce) the pre-T1/T6 world.
Two live code residuals (G7/G8) are intentionally gated at the workflow layer per the T6→T8
routing; they remain candidates for a future code ticket.

---

## 6. Verification plan — fixture walk-through (would the updated doc have caught ES01B?)

Fixture: the PRE-T6 ES standup artifacts as recorded in the T6 ticket — config
`ES01B_Sharpe_E03_07042026.json` with `execution_symbol: "CL"` (inherited from the
`hourly_ensemble_010.json` deep-copy), `models.*.model_path` under
`.../registry/E2E_ES_HourSet_01B_long_logloss/...` (nonexistent — real dirs are `E2E_HourSet_01B_*`),
`predictions_path` = `reports/batch_runs/batch_20260704_0701/predictions/...` (never synced locally),
no `models.*.symbol`, no `enable_5m_stream` key; manifest `baseline.symbol` = `"ES"`.

Running the §3.4 gate on that batch dir:

| Check | Result on the broken artifact | Catches |
|-------|-------------------------------|---------|
| (a) `resolve_instrument_context(cfg)` | `derive_model_symbol("E2E_ES_HourSet_01B_long_logloss")` → `"ES"` (token after `E2E_` is a registry symbol) ≠ brain `"CL"` → **ValueError, GATE FAIL** | **execution_symbol=CL bug** |
| (b) `execution_symbol == baseline.symbol` | `"CL" != "ES"` → **FAIL** | same bug, independently (load-bearing even if model ids had carried no symbol tag — the gates are deliberately redundant) |
| (c) `models.*.symbol` present | absent → **FAIL** | forces regeneration through the stamping generator |
| (d) `model_path` exists | `E2E_ES_HourSet_01B_*` dirs do not exist → **FAIL** | **the E2E_ES_\* path-derivation bug** |
| (e) `predictions_path` exists | unsynced batch dir → **FAIL** | blueprint m4 (launch-blocking ops gap) |
| Gate 2 `enable_5m_stream` | key absent (defaults true) on a symbol with no 5m seed → **flagged** | the startup crash T7 later fixed by config |

Additionally, Phase 0's GATE 0 would have run `tests/test_instrument_context.py` — whose T1-era pin
deliberately encoded "ES01B refuses to start until T6 regenerates it".

**Verdict: every defect the ES standup shipped is caught by at least one drafted gate, two of them
redundantly.** Doc-only verification for the eventual editor: apply §3/§4, then re-run this table
against the pre-T6 fixture values (they are preserved in
`.agents/collab/tickets/t6-config-generator-fix_07052026_0043/`) and confirm each row still FAILs,
and against the HEAD ES01B config + `batch_20260704_0701_ES_01B_SCOUT` dir expecting `CONFIG GATE: PASS`.

---

## 7. Deviations & open questions (human authorization required)

1. **[decision needed] `defaults` block on the 34 existing v2 manifests.** The drafted C2 gate mandates it for FUTURE non-CL manifests (schema-safe, verified). Retro-fitting the existing ES/NG/GC/NQ/SI/ZC/ZS manifests is a CONFIG change outside doc-only scope — authorize separately (recommended for any manifest that will be re-run locally, ZC first: its standup is in flight and its manifests lack the block).
2. **[decision needed] `.agent/workflows/` disposition.** Recommendation: deprecation banners (this audit), deletion optional later.
3. **[scope mismatch to route onward] T4's tdd_result routed "sweep the 10 legacy training `MacroFeatureEngine` call sites to explicit instrument" to T8 — that is a CODE task and contradicts T8's doc-only minting. Needs its own micro-ticket; NOT covered by this audit.**
4. **[residuals deliberately left as code debt]** G7 (C1 stamping in `strategy_optimizer.py`/`batch_post_optimizer.py` target-pairs path) and G8 (C2 silent CL-base fallback) — the drafted gates are compensating controls, not fixes. Recommend minting the code ticket once ZC/ZS batches are past.
5. **[line-number correction to the brief]** T6-C2 fallback is at `generate_ensemble_artifacts.py:303` at HEAD (the brief's ":272" predates the T6 insertion); C1 lines `:1443-1447`/`:1868-1872` confirmed accurate.
6. Protocol adaptation per spawn prompt: reporting via final agent message instead of `send_message`; audit-log + status updated in the ticket folder.
