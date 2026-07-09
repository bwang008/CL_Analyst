---
name: build-symbol-pipeline
description: End-to-end recipe to extend the CL Optuna futures pipeline to a NEW symbol (data → features/targets → GCS → v2 canary manifests → passing dry-run → validated canary), using CL as the template
---

# /build-symbol-pipeline — Stand up a new symbol end-to-end

Bring a new futures symbol (e.g. **NG**, NQ, GC) onto the existing GCP Optuna pipeline that is proven for CL, and prove it works by running a real 3-trial **canary** and validating the generated artifact folder. This is the exact process used to stand up **ES** (`HourSet_01A`/`01B`); mirror it for the next symbol.

This workflow composes two existing ones — do those steps there, don't re-document them:
- [grab-data](grab-data.md) — Databento download + convert to `raw`/`ratio`/`panama`.
- [generate-data](generate-data.md) — build a feature/target parquet from a `configs/master/DataMap_*.json`.
- [run-cloud-batch](run-cloud-batch.md) — the sweep→post-optimizer batch this validates against.

> The **CL recipe is the source of truth.** For every artifact you create for the new symbol, find the CL equivalent and mirror it exactly, changing only what must change (symbol, dataset_version, output paths). Two named datasets per symbol, mirroring CL's `HourSet_14A`/`14B`: an **A** variant (full feature suite) and a **B** variant (same features minus a `drop_features` list).

## Hard rules (do not violate)
- **Env:** `conda run -n trader python <file.py>`. Never `python3`. Never pass multi-line code via `python -c` — write a temp `.py` under the scratchpad and run it. Note the env is **pandas 1.5.3** (`format="mixed"` is unsupported → silent `NaT`; use auto-detect or an explicit format).
- **No code changes without SDLC.** If the pipeline needs a code change (see COT/TFF note in Phase 2), STOP, report the issue + proposed fix, and only implement test-first (see [tdd-tester](tdd-tester.md)/[tdd-coder](tdd-coder.md)) after approval. Guard shared (CL) code paths with a byte-identical regression test.
- **No silent nulls.** Every manifest/config field must be present and validated; a missing required field must crash, not default.
- **Raw vs ratio-adjusted (known past bug):** train on the **ratio-adjusted** series; run PnL/execution on the **raw unadjusted** series. Never cross them.
- **Do not commit** unless the user asks. **Do not launch the full paid batch** — the canary is the only VM launch this workflow performs, and only with user authorization.
- **PowerShell:** invoke the batch script directly as `& .\gcp\run_sweep_batch.ps1 ...` (never prefix `powershell -ExecutionPolicy Bypass` — a safety classifier blocks it).

---

## Phase 0 — Baseline & instrument registration
1. Establish a green test baseline so you can prove no regressions later:
   ```
   conda run -n trader python -m pytest -q --no-header
   ```
   Record the pass/skip count (see [run-tests](run-tests.md)).
2. Register the instrument **completely** — post-T1/T5 the registry is the single source of truth
   for the live engine; every field is REQUIRED (dataclass has no defaults except `micro_of`,
   `slippage_ticks`) and live startup RAISES on gaps:
   - `src/data/databento_data_builder.py` → `SYMBOL_MAP["<SYM>"] = "<SYM>.v.0"`.
   - `src/core/instrument_master.py` → `INSTRUMENT_REGISTRY["<SYM>"]` with ALL fields:
     - identity: `symbol`, `name`
     - pricing: `tick_size`, `tick_value`, `multiplier`, `quote_unit_usd` (0.01 for grains quoted
       in cents/bu) — **invariant, test-enforced:** `tick_value == tick_size * multiplier * quote_unit_usd`.
       T3 snaps every live order price to `tick_size` (`round_to_tick`) — a wrong tick = rejected orders.
     - training: `cftc_code`, `volatility_index` (FRED series: equities → `VIXCLS`; energy → `OVXCLS`;
       gold → `GVZCLS`; grains/silver/copper → `VIXCLS` proxy, no FRED series exists)
     - routing: `exchange` (IBKR string: NYMEX / CME / COMEX / CBOT)
     - rollover: `active_months` (MGL codes — EXCLUDE illiquid serials, e.g. GC=`"GJMQVZ"`),
       `roll_reference` (`"LTD"`, or `"FND"` for physically delivered), `roll_buffer_days`
       (CL 6, ES/NQ 8, FND-referenced metals/grains 3), `roll_ratio_tolerance` (CL/MCL pinned
       `0.01`; all new symbols `0.001`)
     - session: `session_hours_ct` — **MUST reuse one of the three modeled shapes**:
       `_GLOBEX_SESSION` (17:00–16:00 CT), `_GRAINS_SESSION` (19:00–07:45 + 08:30–13:20 CT),
       `_EQUITY_SESSION` (17:00–15:15 + 15:30–16:00 CT). `src/live_execution/session_calendar.py`
       dispatches on the exact tuple and **RAISES on any other shape** — an instrument with an
       unmodeled session will not start live. A new session shape is an SDLC code change: STOP,
       report, test-first.
     - provisioning: `bars_per_day_5m`, `bars_per_day_1h` (drives the live seed-lookback formula;
       24h markets 288/24 (CL pins), 23h 276/23, grains 200/16)
     - live vol: `live_vol_index` (IBKR CBOE index symbol `"VIX"`/`"OVX"`/`"GVZ"` — NOT the FRED name)
     - micro sibling (if traded): a separate `M<SYM>` entry with `micro_of="<SYM>"`, inheriting
       the parent's `cftc_code`/`volatility_index` (micros are execution-only).
   - `scripts/download_macro_data.py` → `COT_REPORT_BY_SYMBOL["<SYM>"]` = `disaggregated` (commodities: CL/NG/HG/GC/PA) or `tff` (financials: ES/NQ). **Unmapped raises** — if the symbol is missing, add it. If it needs a COT report family not yet supported, that's an SDLC code change (see Phase 2).
   - **GATE 0 — registry completeness (blocking):**
     ```
     conda run -n trader python -m pytest tests/test_instrument_master_live_fields.py tests/test_instrument_context.py -q
     ```
     Both suites green before Phase 1. They enforce field completeness, the tick-value invariant,
     session-shape membership, and resolver behavior for every registry entry. (A `TypeError` on
     `Instrument(...)` construction = you omitted a required field — that is the intended failure.)

## Phase 1 — Data acquisition (real full history)
Follow [grab-data](grab-data.md). Concretely:
1. **Estimate first** (free), report the cost:
   ```
   conda run -n trader python -m src.data.databento_data_builder estimate --symbols <SYM> --start 2005-01-01
   ```
   GLBX.MDP3 begins **2010-06-06**; 2005 errors — fall back to the earliest available and note the real start.
2. **Submit + download** the full history (async batch): `... submit --symbols <SYM> --start <earliest> --outdir <dir>`.
3. **Convert** to the three variants: `... convert <raw.csv> --symbol <SYM> --mode all --format semicolon --outdir <dir>` → `<SYM>_raw.csv` / `<SYM>_ratio.csv` / `<SYM>_panama.csv` (format `DD/MM/YYYY;HH:MM;O;H;L;C;V`).
4. **Stage the training input:** copy `<SYM>_ratio.csv` → `C:\CL_Analyst_Data\data\raw\<SYM>.csv` (this is the default input `regenerate_features.py` loads; it is the **ratio-adjusted** series — verified: CL's `raw/CL.csv` ≡ `CL_ratio.csv`).
5. **Sanity check** the series (temp script): date range covers ≥ several years each side of `train_cutoff` 2022-01-01, monotonic timestamps, no dup rows, valid OHLC (`High≥Low`, etc.), no NaN.
6. **Build the execution parquet** `<SYM>_raw.parquet` (schema `[DateTime, Open, High, Low, Close, Volume]`, hourly) from `<SYM>_raw.csv` — mirror `CL_raw_1h.parquet`. This is the PnL/execution series the manifest's `execution_data_path` points at.
7. **Live 1h seed (T7):** stage `data/processed/<SYM>_raw_1h.parquet` — the per-symbol live seed
   resolved by `derive_data_paths()` (`src/live_execution/data_manager.py`); a missing seed RAISES
   at live startup. Since `<SYM>_raw.parquet` is already hourly, a copy suffices — but verify it
   holds ≥ **4,320** hourly bars (`REQUIRED_1H_BARS`) *inside the instrument's lookback window*
   (`derive_seed_lookback_days(bars_per_day_1h)`: ES 292 calendar days, grains 406) and re-stage
   near launch time (the window decays ~1 trading day/day).
   **NO 5m acquisition — Databento in this repo is hourly-only (USER RULING, T7).** New symbols
   need NO 5m seed: live 5m streaming is the default — a seedless symbol SHALLOW-bootstraps its 5m
   window from IBKR on first run (loud banner + Telegram stamp) and warm-starts from the saved
   cache thereafter; `live_config.enable_5m_stream: false` is an explicit opt-out only (Phase 6
   gate 2).

## Phase 2 — Macro data (FRED + COT)  ⚠ symbol-type sensitive
The feature build calls `MacroFeatureEngine.merge_all` which requires **both** `raw/macro/fred_macro_data_<sym>.csv` and `raw/macro/cftc_cot_<sym>.csv`; COT is mandatory (`include_cot=True`, no config knob) and `_load_cot` hard-raises if the file is missing.
```
conda run -n trader python scripts/download_macro_data.py --symbol <SYM>
```
- **FRED** uses the instrument's `volatility_index` (equity index → real VIX; energy → OVX) — symbol-specific by design.
- **COT report family matters:**
  - **Commodity** symbols (CL, NG, HG, GC, PA) → CFTC **Disaggregated** report (`fut_disagg_txt_*`). Trader roles: Money Manager / Producer-Merchant / Swap Dealer.
  - **Financial** symbols (ES, NQ) → CFTC **TFF** report (`fut_fin_txt_*`, 2010+ only). Roles remapped to the canonical MM/Prod/Spec via the approved mapping **MM←Leveraged Funds, Prod←Asset Manager, Spec←Dealer** so financial symbols emit the SAME `COT_*` feature names as commodities.
  - The adapter layer (`CotReportAdapter` / `DisaggregatedAdapter` / `TffAdapter` + `COT_REPORT_BY_SYMBOL`) already supports both. **If a new symbol needs a report family not yet implemented, STOP and follow SDLC** (test-first, byte-identical CL guard) before adding it.
- Verify the resulting `cftc_cot_<sym>.csv`: canonical cols `Date,OI,MM_*,Prod_*,Spec_*,*_Net`; monotonic weekly dates; 0 NaN/NaT. Note the COT coverage start (financials begin ~2010; a `*_PCTILE_52W` lookback pushes usable data ~1yr past that — fine vs a 2022 cutoff).

## Phase 3 — Build both datasets (via generate-data)
Follow [generate-data](generate-data.md). For each variant (A, B):
1. Create `configs/master/DataMap_<SYM>_HourSet_<VER>.json` by mirroring the CL master `configs/master/DataMap_CL_HourSet_14A.json` (resp. `14B`) — change only `symbol`, `dataset_version`, `output_filename`. Keep `windows`, `macro_windows`, all `include_*`, `raw_horizon`, `atr_period`, and the target `definitions`. The **A vs B difference is entirely the `drop_features` list** (A: none; B: the ~137-feature list). `dataset_version` only names the output — the build is fully config-driven (`process_from_config`), not the legacy `run()` dispatch, so no `data_processor.py` change is needed.
   > Cross-check the master DataMap against the **deployed** parquet's columns; the CL master `14B` DataMap is known to carry a stray `continuous_return` target the real parquet does not have — reproduce the *parquet*, not a drifted map.
2. Build (ratio input is implicit via `raw/<SYM>.csv`; pass raw as exec):
   ```
   conda run -n trader python scripts/regenerate_features.py --config configs/master/DataMap_<SYM>_HourSet_<VER>.json --exec-data C:\CL_Analyst_Data\data\raw\DataBento\<SYM>\<SYM>_raw.csv
   ```
3. **Verify each parquet** (temp script) against the CL reference:
   - The four required target columns exist and are populated: `TARGET_TRIPLE_2x1_6H_{LONG,SHORT}`, `TARGET_TRIPLE_3x1_6H_{LONG,SHORT}`.
   - `COT_*` features present (incl. `COT_MM_NET`/`COT_PROD_NET`/`COT_SPEC_NET`).
   - 0 all-NaN columns; 0 feature columns >50% NaN.
   - Column set ⊆ CL reference; the only legitimate differences are symbol-specific vol-index columns (e.g. ES has VIX, lacks CL's `MACRO_OVX*`).
   - B variant column count = A minus the drop_features count.

## Phase 4 — Upload to GCS
```
gcloud storage cp C:/CL_Analyst_Data/data/processed/<SYM>_HourSet_<VER>.parquet gs://cltrainer-optuna-results/data/<SYM>_HourSet_<VER>.parquet
gcloud storage cp C:/CL_Analyst_Data/data/processed/<SYM>_raw.parquet          gs://cltrainer-optuna-results/data/<SYM>_raw.parquet
```
Do both variants + the raw. Verify: `gcloud storage ls -l "gs://cltrainer-optuna-results/data/<SYM>_*.parquet"` (sizes match local). VM path derivation prefixes the symbol, so `dataset_version=HourSet_01A` → `<SYM>_HourSet_01A.parquet`.

## Phase 5 — Write canary + scout manifests & dry-run gate
Generate BOTH tiers for each variant (A, B), by mirroring the matching CL template and changing ONLY: `baseline.symbol` + every `overrides.symbol` → `<SYM>`; `baseline.data_workflow.dataset_version`; `execution_data_path` → `gs://.../<SYM>_raw.parquet`; experiment `label`/`gcs_prefix` to symbol-specific names; and the `_comment`. **Preserve every other field of the template verbatim** (never derive one tier from the other — they differ in more than one field).

1. **Canary** — mirror `configs/batch_manifest_v2_hourset14a_canary.json` (resp. `14b`) → `configs/batch_manifest_v2_<sym>_hourset<ver>_canary.json`. Canary tier = smoke test: `n_trials:3`, `post_optimizer_trials:3`, `timeout_minutes:240`, `gcs_base_dir:.../canary`, **2 experiments** (`2x1_6H`, `3x1_6H`).
2. **Scout** — mirror `configs/batch_manifest_v2_hourset14a_scout.json` (resp. `14b`) → `configs/batch_manifest_v2_<sym>_hourset<ver>_scout.json`. Scout tier = heavier exploration: `n_trials:100`, `post_optimizer_trials:200`, `timeout_minutes:360`, `gcs_base_dir:.../scout`, the **wide LGBM box** (`max_depth_max:8`, `num_leaves_max:64`, `max_n_estimators:1500`, `learning_rate 0.001–0.05`, `max_folds:5`, `early_stopping_rounds:30`, `min_child_samples 100–500`), and **4 experiments** (adds `2x1_3H`, `4x1_36H`). **14B is the source of truth for the optuna box; 14A is kept identical to it.** Verify the parquet actually contains all 4 scout target-column pairs (`2x1_6H`, `3x1_6H`, `2x1_3H`, `4x1_36H` × LONG/SHORT) before writing.
   - Both tiers keep `random_seed:42`, `opt_mode:"individual"`, `slippage_per_side:0.01`, `holdout_cutoff_date:null`, `post_optimizer_holdout_months:6`.
   - `baseline.execution_workflow.strategy_config_path` may point at the CL base
     `configs/strategies/hourly_ensemble_010.json` for machinery validation — but every config the
     batch derives from it MUST pass the Phase 6 CONFIG VALIDATION GATE before any backtest/live use.
     Two generator residuals are open at HEAD (T6 audit; code fixes deliberately deferred):
     - **C1 — do NOT ship `_opt_`/`_hybrid_` configs from the target-pairs path for a non-CL symbol.**
       `agent/batch_post_optimizer.py` (target-pairs mode, `:1045-1134`) hands the raw CL base to
       `agent/strategy_optimizer.py`, which writes `*_opt_*.json` (`:1443-1447`) and `*_hybrid_*.json`
       (`:1868-1872`) into `configs/strategies/` with **no symbol stamping**. Until the code fix lands,
       quarantine/delete any such files a non-CL batch produces and regenerate via
       `agent/generate_ensemble_artifacts.py` (which stamps `execution_symbol` + `models.*.symbol`
       from `baseline.symbol` and self-checks with `resolve_instrument_context`).
     - **C2 — non-CL manifests MUST carry a `defaults` block.** The local generator
       (`agent/generate_ensemble_artifacts.py:303`; same pattern `batch_post_optimizer.py:1045/:1071`)
       ignores `strategy_config_path` and reads `defaults.strategy_config`, silently falling back to
       the CL base when absent — and NO v2 manifest carries `defaults` today. Add
       `"defaults": {"strategy_config": "<symbol-baseline>.json", "local_data_path": "<local parquet>", "local_exec_data": "<local raw parquet>"}`
       mirroring the `baseline.execution_workflow` values (`BatchSweepConfig` ignores the extra key —
       verified, `src/config/schemas.py:229-237`). A CL-parameterized deep-copy is silent misconfiguration even when the symbols are
       stamped correctly (CL-tuned blocked hours/thresholds/offsets).
3. Dry-run each (canary + scout, both variants — 4 dry-runs) until green:
   ```
   & .\gcp\run_sweep_batch.ps1 -ManifestPath "configs\batch_manifest_v2_<sym>_hourset<ver>_{canary|scout}.json" -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" -DryRun
   ```
   All gates must pass: train_cutoff parseable, 2-way holdout, `post_optimizer_holdout_months=6`, `slippage∈[0,0.5]`, `opt_mode` valid, and **OOS window > 6mo (no holdout/OOS collapse)** computed against the real dataset dates. (A PowerShell 5.1 `NativeCommandError` trailer echoing INFO logs is not a failure.)

## Phase 6 — Canary run (real, 3 trials) + artifact validation  ← success measure
With user authorization, launch ONE real canary (non-`-DryRun`) for the A variant (validates the full machinery; B shares it):
```
& .\gcp\run_sweep_batch.ps1 -ManifestPath "configs\batch_manifest_v2_<sym>_hourset01a_canary.json" -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f"
```
Run it in the background and monitor. This provisions sweep VMs → post-optimizer VM → downloads reports (~10–20 min). See [run-cloud-batch](run-cloud-batch.md)/[post-optimize](post-optimize.md).

**SUCCESS is defined by a fully generated & populated artifact folder** at `reports\batch_runs\batch_<ID>\`. It MUST contain, non-empty:
- `manifest.json`, `batch_progress.json`, `batch_summary.md`, `wall_clock_summary.md`, `top_pairs.json`
- `batch_summary_optimized_sharpe.md` (+ `batch_summary_optimized_ensembles_sharpe.md`)
- `optimization_results_sharpe.json` (+ `optimization_results_ensembles_sharpe.json`)
- `sharpe_ensemble_backtests.md`
- (Sortino artifacts dropped 2026-07-04, ticket `drop-sortino-objective_07042026_2301`; only present on `-Objective both` rollback runs.)
- `configs/` populated with per-experiment `<SYM>...E0N_*.json`
- `predictions/` populated with per-experiment `<SYM>...E0N_predictions.csv`

**CONFIG VALIDATION GATE (hard — the workflow may not report success without exit 0).**
For EVERY strategy config in `reports\batch_runs\batch_<ID>\configs\` (and any config promoted to
`configs/strategies/`), run a scratchpad script (per the hard rules — no multi-line `python -c`):

```python
# <scratchpad>\validate_batch_configs.py  — usage: python validate_batch_configs.py <batch_dir>
# Run from the repo root (the sys.path bootstrap below and the repo-relative
# model/prediction paths inside the configs both depend on it).
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.getcwd())
from src.live_execution.instrument_context import resolve_instrument_context

batch_dir = Path(sys.argv[1])
manifest = json.loads((batch_dir / "manifest.json").read_text())
expected = manifest["baseline"]["symbol"].upper()          # KeyError here = manifest bug: fix the manifest
failures = []
cfgs = sorted((batch_dir / "configs").glob("*.json"))
if not cfgs:
    failures.append(f"NO CONFIGS FOUND in {batch_dir / 'configs'} — generator produced nothing")
for cfg_path in cfgs:
    cfg = json.loads(cfg_path.read_text())
    try:
        ctx = resolve_instrument_context(cfg)              # raises on missing/unknown symbol + model-tag mismatch
        if ctx.execution_symbol != expected:
            raise ValueError(f"execution_symbol {ctx.execution_symbol!r} != manifest baseline.symbol {expected!r}")
        for side, m in cfg.get("models", {}).items():
            if not m.get("symbol"):
                raise ValueError(f"models.{side}.symbol missing (T6 generator stamps it — regenerate, don't hand-patch)")
            for key in ("model_path", "predictions_path"):
                p = m.get(key)
                if not p or not Path(p).exists():
                    raise ValueError(f"models.{side}.{key} not on disk: {p}")
    except Exception as e:
        failures.append(f"{cfg_path.name}: {e}")
print("\n".join(failures) or "CONFIG GATE: PASS")
sys.exit(1 if failures else 0)
```
```
conda run -n trader python <scratchpad>\validate_batch_configs.py reports\batch_runs\batch_<ID>
```
Exit 0 required. **Zero configs found = FAIL** (a silently-empty `configs/` dir is a generator
failure, not a pass). **Any failure = the canary FAILED**, regardless of PnL/artifact checks. Checks, per
config: (a) resolves via `resolve_instrument_context` (execution_symbol present + registered, model
symbol tags consistent), (b) `execution_symbol == manifest baseline.symbol`, (c) `models.*.symbol`
present, (d) every `model_path` exists on disk, (e) every `predictions_path` exists on disk.

**Known fixtures (calibrate the gate before trusting it):**
- The preserved ES standup batch dir `reports\batch_runs\batch_20260704_0701_ES_01B_SCOUT`
  **correctly FAILS** this gate — its `configs/` still holds the 8 pre-T6 CL-stamped originals,
  kept deliberately as the negative regression fixture (living proof the gate catches ES01B).
  Do NOT regenerate that batch dir just to make the gate green.
- The promoted `configs/strategies/ES01B_Sharpe_E03_07042026.json` **PASSES** (single-config
  variant: stage a copy as `<tmpdir>\configs\<name>.json` next to a minimal
  `<tmpdir>\manifest.json` stub `{"baseline": {"symbol": "ES"}}`, then run the script on `<tmpdir>`).

**Gate 2 — 5m stream mode:** the promoted config either OMITS `enable_5m_stream` (default: live 5m
streaming; a seedless hourly model shallow-bootstraps its 5m window from IBKR on first run, loudly)
or deliberately opts out with `"enable_5m_stream": false`. 5m MODELS (`bar_size` "5m") still
hard-require a real 5m seed.

**Gate 3 — C1 quarantine:** if the post-optimizer ran in target-pairs mode, list
`configs/strategies/*_opt_*.json` / `*_hybrid_*.json` created during the run and quarantine them
(see Phase 5 C1) — they are unstamped CL-base clones.

And the results must be **substantively valid** (not just present): open a summary and confirm real trades and PnL (non-zero `Trades (pre)`/`Trades (opt)`), and a populated **Holdout** column with non-zero trades — i.e. **no period/OOS collapse** (0-trade holdout is the classic failure). Confirm the run exited 0 and any sweep VMs were auto-cleaned.

**If it crashes:** capture the full task output + VM logs, root-cause it, and report the cause and any newly-found gaps to patch (SDLC for code fixes) — do not silently retry.

## Phase 7 — Regression check & hand-off
1. Re-run the full suite; confirm no regressions vs the Phase-0 baseline (new tests may raise the pass count, but nothing previously green may fail).
2. Clean up: delete any leftover VMs (`gcloud compute instances list --project cltrainer`; the persistent `optuna-post-optimizer` may remain RUNNING — confirm with the user before deleting).
3. Report: Databento cost + actual date range; files created (2 parquets + raw, 2 DataMaps in `configs/master/`, 2 manifests, GCS uploads, any code changes + tests); dry-run results; canary artifact-validation result **including the CONFIG VALIDATION GATE output (must be PASS)**; the exact non-`-DryRun` launch commands for the real batches; and caveats (CL-derived baseline strategy, symbol-specific vol index, COT coverage start). Nothing committed; no full batch launched.

---

## Artifact checklist (new symbol, both variants)
- [ ] Databento full history downloaded (cost estimated & reported) + `raw`/`ratio`/`panama` converted; `raw/<SYM>.csv` staged (ratio).
- [ ] `fred_macro_data_<sym>.csv` + `cftc_cot_<sym>.csv` present & valid (correct COT report family).
- [ ] `<SYM>_raw.parquet` (execution) + `<SYM>_HourSet_01A.parquet` + `01B.parquet` built & verified (targets, COT features, no all-NaN, CL parity).
- [ ] `configs/master/DataMap_<SYM>_HourSet_01A.json` + `01B.json` (consistent with the built parquets).
- [ ] Canary manifests: `configs/batch_manifest_v2_<sym>_hourset01a_canary.json` + `01b`.
- [ ] Scout manifests: `configs/batch_manifest_v2_<sym>_hourset01a_scout.json` + `01b` (wide LGBM box, 4 experiments).
- [ ] All 3 parquets uploaded to `gs://cltrainer-optuna-results/data/`.
- [ ] All 4 manifests (canary + scout, both variants) pass `-DryRun` (no OOS collapse).
- [ ] Canary run: `reports\batch_runs\batch_<ID>\` fully populated (list above) with real trades/PnL/holdout.
- [ ] `data/processed/<SYM>_raw_1h.parquet` live seed staged (≥4,320 1h bars in-window; no 5m data).
- [ ] CONFIG VALIDATION GATE exit 0 on `reports\batch_runs\batch_<ID>` (resolver + symbol + paths).
- [ ] No unquarantined `*_opt_*/*_hybrid_*` CL-base configs in `configs/strategies/`; non-CL manifests carry a `defaults` block.
- [ ] Full test suite green (no regressions); VMs cleaned up; hand-off written; nothing committed.

## Key files
| File | Purpose |
|------|---------|
| `src/data/databento_data_builder.py` | Databento download/convert; `SYMBOL_MAP` |
| `src/core/instrument_master.py` | `INSTRUMENT_REGISTRY` (17 required fields — see Phase 0) |
| `src/live_execution/instrument_context.py` | `resolve_instrument_context` — config fail-fast validation (T1) |
| `src/live_execution/session_calendar.py` | the three modeled session shapes; unknown shape raises (T5/T7) |
| `src/live_execution/data_manager.py` | `derive_data_paths` per-symbol seed/cache/ledger names; `REQUIRED_1H_BARS` (T2/T5) |
| `src/core/dataset_tag.py` | `derive_dataset_tag` — the ONLY authority for `E2E_*` names (T6) |
| `agent/generate_ensemble_artifacts.py` | config (re)generation with symbol stamping + self-check (T6) |
| `scripts/download_macro_data.py` | FRED + COT; `COT_REPORT_BY_SYMBOL`, `CotReportAdapter`/`DisaggregatedAdapter`/`TffAdapter` |
| `scripts/regenerate_features.py` | Config-driven parquet build (`process_from_config`) |
| `configs/master/DataMap_CL_HourSet_14A.json` / `14B` | CL DataMap templates to mirror |
| `configs/batch_manifest_v2_hourset14a_canary.json` / `14b` | CL v2 CANARY manifest templates to mirror (n_trials 3, 2 exps) |
| `configs/batch_manifest_v2_hourset14a_scout.json` / `14b` | CL v2 SCOUT manifest templates to mirror (wide LGBM box, n_trials 100, 4 exps) |
| `gcp/run_sweep_batch.ps1` | Dry-run gate + canary/batch launcher |
