# Impact Review — t8-workflow-doc-gates_07052026_0403

**Reviewer:** Ticket-Impact-Reviewer | **Date:** 2026-07-05 | **Repo @ HEAD `3738516`** (branch development)
**Proposal:** DOC-ONLY remediation (audit.md §3 gate insertions + §4 16-file edit plan).
**Decision: APPROVE — with 3 REQUIRED corrections and 2 recommended ones (below).**
No human authorization needed beyond the manager rulings already issued (banners not deletion for
`.agent/workflows/`; 34-manifest `defaults` retrofit OUT of T8; T4 call-site sweep → own code
micro-ticket; G7/G8 code debt deferred behind the gates). The audit's §7 open questions are all
covered by those rulings.

---

## 1. Constraint evaluation (workflow rules)

| Rule | Triggered? | Reasoning |
|------|-----------|-----------|
| Interface Rule | NO | Doc-only; no function signature changes. |
| Base Class Rule | NO | No source file touched. |
| Refactor Veto | NO | Multi-**file** doc edit, but no component rewrite; each edit is a localized insertion/banner. The one systemic artifact (the §3.4 gate script) was executed verbatim by this review and behaves as designed. |

Blast radius of a doc gate is "false blocking": a wrong gate command would freeze legitimate
operations. Both pytest suites exist (`tests/test_instrument_master_live_fields.py`,
`tests/test_instrument_context.py`), and the §3.4 validator was run verbatim against a real batch
dir (see §3 below) — it blocks exactly what it should and passes what it should, modulo the
corrections in §4.

## 2. Fact-check results — audit claims vs HEAD (all verified by direct read/execution)

| Claim | Verdict | Evidence |
|-------|---------|----------|
| G2: 17 required `Instrument` fields (+2 defaulted), incl. T5 `roll_ratio_tolerance` + T7 session tuples; 15 entries | **EXACT** | `src/core/instrument_master.py:4-30` — counted: symbol, name, tick_size, tick_value, cftc_code, volatility_index, exchange, multiplier, quote_unit_usd, active_months, roll_reference, roll_buffer_days, session_hours_ct, bars_per_day_5m, bars_per_day_1h, live_vol_index, roll_ratio_tolerance = **17**; `micro_of`/`slippage_ticks` defaulted. 15 registry entries (CL MCL ES MES NG HG GC MGC PA NQ MNQ ZC ZS SI SIL) — ZC/ZS/SI/SIL are COMMITTED at HEAD (working tree clean for this file; the spawn brief's "modified" status was a stale snapshot). Three session shapes match §3.1 text verbatim (`instrument_master.py:33-41`). |
| G3 session dispatch raises on unknown shape | ✓ | `session_calendar.py:236-283` (`_unsupported_session_shape` in both `market_status` and `session_open_anchor`). |
| G4 `derive_data_paths` names, `REQUIRED_1H_BARS=4320`, CL 280/ES 292/ZC 406 | ✓ | `data_manager.py:56-128` — all names/values exact, incl. CL's 3 legacy exceptions. |
| G5 macro hard-raise (missing vol column / files) | ✓ | `macro_features.py:368,385,585-593`. |
| G6 `enable_5m_stream` default true; false requires hourly bar_size; 5m manager skipped | ✓ | `live_trader.py:331-348, 395-417` (audit's ranges off by ≤3 lines — immaterial). Promoted `configs/strategies/ES01B_Sharpe_E03_07042026.json` carries `"enable_5m_stream": false` at line 22 ✓. |
| C1 lines `strategy_optimizer.py:1443-1447` (`_opt_`) / `:1868-1872` (`_hybrid_`) | **EXACT** | Both write `Path(config_path).stem + suffix` into `dirname(config_path)`, no symbol stamping. |
| C1 path `batch_post_optimizer.py:1045/:1071/:1134` | **EXACT** | `progress.get("defaults", {}).get("strategy_config", "hourly_ensemble_010.json")` at :1045 and :1071; `opt_tasks.append((..., base_config_path, ...))` at :1134. |
| C2 `generate_ensemble_artifacts.py:303` silent CL fallback | **EXACT** | `manifest.get("defaults", {}).get("strategy_config", "hourly_ensemble_010.json")`. Post-T6 stamping at :441-442 + `resolve_instrument_context(cfg)` self-check at :484 ✓ (note: generator treats `model_path` existence as WARN-only at :485-488 — the §3.4 gate is deliberately stricter; correct for a post-canary local gate). |
| "NONE of the 34 v2 manifests carries `defaults`" | **EXACT** | Programmatic check: 34 files, 34 without `defaults` — including all 4 `batch_manifest_v2_zc_*` (tracked at HEAD; `baseline.symbol="ZC"`, `strategy_config_path` = the CL base `hourly_ensemble_010.json` → the C2 risk on the in-flight ZC standup is real). |
| VM/local asymmetry `vm_post_optimize.sh:180-182` | **EXACT** | `fail("baseline.execution_workflow.strategy_config_path is required (no default).")`. |
| `BatchSweepConfig` ignores extra top-level keys | ✓ | `src/config/schemas.py:229-237` (pydantic default `extra="ignore"`; no `extra="forbid"`). Minor: audit cites bare "schemas.py" — full path is `src/config/schemas.py`. |
| Resolver API used by the §3.4 script | ✓ | `resolve_instrument_context(Mapping) -> InstrumentContext` with `.execution_symbol` (`instrument_context.py:97-163`); batch dirs contain `manifest.json` + `configs/` (verified on disk + `generate_ensemble_artifacts.py:260`); `models.<side>.{model_path,predictions_path,symbol}` are the real emitted field names. |
| §6: gate exits non-zero on the pre-T6 ES01B fixtures | **VERIFIED BY EXECUTION** | Ran the §3.4 script **verbatim** (scratchpad) against `reports\batch_runs\batch_20260704_0701_ES_01B_SCOUT`: **exit 1**, all 8 configs fail with exactly the row-(a) error (`execution_symbol 'CL' ... does not match model symbol 'ES' declared by models.long.experiment_id 'E2E_ES_HourSet_01B_...'`). Rows (b)-(e) independently reproduce: `E2E_ES_*` model dirs absent, plain `batch_20260704_0701` predictions dir absent, `models.*.symbol` absent. Redundancy claim holds. |
| G10: HEAD ES01B config fixed | ✓ **with a scope catch** | The **promoted** `configs/strategies/ES01B_Sharpe_E03_07042026.json` is fixed (ES/ES, real `E2E_HourSet_01B_*` paths — verified on disk, predictions under `..._ES_01B_SCOUT` — verified on disk). **BUT the batch-dir copies were never regenerated** — see REQUIRED correction R1. |
| build-symbol-pipeline.md staleness (Phase 0 §2 incomplete; Phase 5 soft sentence at line 90; Phase 6 no config validation; PS-prefix hard rule at line 23) | ✓ | All verbatim at the cited lines. |
| run-cloud-batch.md | ✓ | `powershell -ExecutionPolicy Bypass -File` at :53/:68 (contradicts build-symbol-pipeline hard rule :23); "backtest-ready" configs at :95; no `baseline.symbol` REQUIRED mention. |
| post-optimize.md Option B = C1 path; also carries the blocked PS prefix (:35) | ✓ | :71-95; ":95 correctly-formatted ... ready for `live_trader.py`". |
| generate-trade-configs.md Step 3 "Duplicate the baseline canary config" | ✓ | :41 — the ES01B defect pattern verbatim. |
| run-live.md legacy module entry | ✓ | :44/:49 `python -m src.live_execution.live_trader`; canonical `cli.py` entry does exist and fail-fasts via resolver (`cli.py:227-229`). |
| grab-data.md symbol table missing ZC/ZS/SI | ✓ | Table :18-26 lists CL/HG/PA/GC/NG/ES/NQ only; registry has ZC/ZS/SI(+SIL). |
| livetest.md §1 stale | ✓ claim, **✗ proposed correction** | "BacktestEngine does NOT round" IS stale (B(a) landed). But the audit's replacement text is itself wrong — see REQUIRED correction R2. |
| smoketest.md cache list lacks per-symbol names | ✓ | Doc lists only `warm_start_cache[_1h|_2h|_4h].parquet`; `tests/smoke_test_pipeline.py:254-269` already validates `warm_start_cache_{SYM}[_1h]` (T6 regex) — doc-only gap, UPDATE verdict correct. |
| run-vector-cloud-batch.md DEPRECATE | ✓ **with a wording catch** | `configs/sweep_batch_hourset09_*.json` do not exist; no script references them (repo-wide grep). Legacy top-level-`defaults` manifests (the surviving `sweep_batch_short_hourset13/14*` files) FAIL v2 validation: `run_sweep_batch.ps1:355` → `gcp/batch_orchestrator.py:65-68` `BatchSweepConfig.model_validate` requires `infrastructure`+`baseline`. Schema is retired ✓. **BUT** `-SweepMode "frictionless"` still exists in `run_sweep_batch.ps1` (:37, :1019) — see RECOMMENDED correction R5. |
| Fleet docs gaps (headless-deployment.md §Fleet, deploy/systemd/README.md §Fleet) | ✓ | Both document client_id-only validation (spacing ≥2, ≤16 instances, stagger, backoff) — matches `fleet_runner.py:155-215` exactly; nothing on symbol/artifacts/5m. Proposed additions match reality: children run `python -m src.live_execution.cli` (`fleet_runner.py:223`) which fail-fasts via `resolve_instrument_context` (`cli.py:227-229`); missing seed/macro raises in DataManager/MacroFeatureEngine → capped crash-loop under the runner's backoff. Cloud Migration §3 is generic ("seed CSVs, model PKLs") ✓. |
| `.agent/workflows/` = stale duplicates | ✓ | Exactly 3 files; `fc.exe` confirms both twins DIFFER from the `.agents/` copies — two sources of truth confirmed; banner (not deletion) is the right call per manager ruling. |
| docs/prompts = 14 dated one-shot files | ✓ | Counted 14; ARCHIVE verdict sound. |
| G9 `src/core/dataset_tag.py::derive_dataset_tag` | ✓ | Exists (:29). |
| run-cloud-experiment.md conventions section | ✓ | :120 "Configuration Naming & File Tracking Conventions"; :132 saves to `configs/strategies/`. |
| sweep-ensembles.md `--base-config` propagation | ✓ | :17. NOTE verdict sound. |

## 3. Empirical gate execution (the decisive test)

The §3.4 script was transcribed **byte-for-byte** to the scratchpad and executed with
`conda run -n trader python <scratchpad>\validate_batch_configs.py reports\batch_runs\batch_20260704_0701_ES_01B_SCOUT`
from the repo root: **exit 1, 8/8 configs failed** with the exact resolver error predicted by §6
row (a). The script's imports, manifest access (`baseline.symbol` = "ES"), config field names, and
path checks all bind correctly to the real APIs. The `src` import resolves only because the
standing environment sets `PYTHONPATH=..;.` (no `setup.py`/`pyproject.toml` installs the package)
— see R4.

## 4. Corrections the doc-writer MUST / SHOULD apply

**R1 (REQUIRED — §6 verification instruction).** The final §6 sentence tells the editor to run the
gate against "the HEAD ES01B config + `batch_20260704_0701_ES_01B_SCOUT` dir expecting
`CONFIG GATE: PASS`". **False at HEAD**: T6 repaired only the promoted
`configs/strategies/ES01B_*.json` copies; the batch dir's `configs/` still holds the 8 pre-T6
CL-stamped originals, and the gate (correctly) FAILS there — verified by execution, exit 1.
Rewrite the editor instruction to: (a) gate on the batch dir → expect **FAIL** (this IS the
regression fixture — living proof the gate catches ES01B), and (b) gate on the promoted
`configs/strategies/ES01B_*.json` (single-config variant) → expect PASS (model/prediction paths
verified present on disk). Do not regenerate the batch dir just to make the gate green — it is the
preserved broken fixture.

**R2 (REQUIRED — livetest.md edit, §4 item 10 / §2a verdict).** The proposed replacement text
"post-B(a)/T3: both engines round to the instrument tick via `round_to_tick`" would introduce NEW
drift. Reality at HEAD: BacktestEngine rounds brackets to the **CL penny grid** via
`round(fill ± mult*atr, 2)` (`agent/backtest_engine.py:659-669, 793-797` — comment says so
explicitly), NOT via `round_to_tick`; `ConfigurableStrategy` still computes signal TP/SL with
`round(x, 2)` (`configurable_strategy.py:561-565`); only the live order-placement layer snaps to
the instrument tick via `round_to_tick` (`live_trader.py:1643-1644, 1792-1820`; `ibkr_client.py`).
For CL's power-of-ten tick these coincide bit-exactly (by `round_to_tick`'s fast path); for
0.25-tick symbols (ES/ZC) the backtest penny grid ≠ live tick grid — a **residual non-CL
backtest/live divergence** the doc must state, not paper over. Corrected §1 text: "stale in one
direction only — BacktestEngine now DOES round brackets (B(a), penny grid); live order prices snap
to the instrument tick (T3); grid equality is CL-only, non-CL configs retain a bracket-grid gap."
(This residual is also a candidate line-item for the deferred G7/G8 code ticket.)

**R3 (REQUIRED — §3.4 script hardening).** The gate passes **vacuously** when
`(batch_dir/"configs")` is missing or empty (glob yields nothing → "CONFIG GATE: PASS", exit 0) —
against the no-silent-null house rule, and run-cloud-batch/post-optimize invoke this gate
standalone where Phase 6's separate artifact checklist doesn't apply. Add:
`if not cfgs: failures.append("NO CONFIGS FOUND in <dir> — generator produced nothing")` (i.e.
zero configs = FAIL).

**R4 (recommended — §3.4 script robustness).** The `from src...` import works only because the
user environment sets `PYTHONPATH=..;.` (script runs by absolute path, so `sys.path[0]` is the
scratchpad). Add `import os, sys; sys.path.insert(0, os.getcwd())` above the import plus a "run
from the repo root" line, so the gate does not silently depend on a machine-specific env var.

**R5 (recommended — run-vector-cloud-batch.md banner wording).** Deprecate the doc for what is
actually retired: the `sweep_batch_hourset09_*` manifests (gone) and the legacy top-level
`defaults` manifest schema (fails `BatchSweepConfig.model_validate` — `batch_orchestrator.py:65-68`,
invoked by `run_sweep_batch.ps1:355`). Do NOT word the banner as "vectorized mode removed" —
`-SweepMode "frictionless"` still exists in `run_sweep_batch.ps1` (:37/:1019) and flows through the
v2 pipeline.

**Minor (no action forced):** audit G8 cites "schemas.py:229-237" — full path is
`src/config/schemas.py`; G6 cites `live_trader.py:331-345/395-414` — actual spans :331-348/:395-417
(content identical); carry the exact paths into any doc text that cites them.

## 5. Verdict

**APPROVE** the doc-only remediation as scoped by the manager rulings, conditional on R1-R3 being
applied before the edits land (R4/R5 strongly recommended). Every gate insertion was fact-checked
against HEAD and the blocking gate was executed for real; with R1-R3 the plan introduces no new
doc drift and the drafted gates demonstrably catch every ES01B-class defect.
