# TASK PROMPT — Stand up an end-to-end ES (E-mini S&P 500) canary pipeline

You are extending an existing, working GCP Optuna futures ML pipeline (currently CL-only) to a **second symbol, ES**. The pipeline is proven for CL; your job is to make the *same* machinery run for ES, from raw data → features/targets → GCS → a schema-valid v2 canary manifest → a **passing dry-run**, then STOP and hand off. Do **not** launch the paid cloud batch — the user launches VMs themselves.

This is a large, multi-phase job. Work methodically, verify each phase before moving on, and keep the user informed at the two explicit hand-off/notify points below.

---

## Decisions already made by the user (do not re-litigate)
1. **Two ES datasets**, named to mirror the CL recipes:
   - `ES_HourSet_01A` — built from the **HourSet_14A** feature+target recipe.
   - `ES_HourSet_01B` — built from the **HourSet_14B** feature+target recipe.
2. **Real data, not the sample.** A Databento *sample* already exists at `C:\CL_Analyst_Data\data\raw\DataBentoSample\ES\` but it is only ~1 month (~502 hourly bars, late-May→late-Jun 2026). This is inadequate. Download the **full ES history back to 2005 (or the earliest available)** and validate the entire pipeline against real data.
3. **End state = prepare + dry-run, then hand off.** Build data, upload to GCS, write both manifests, and get `run_sweep_batch.ps1 -DryRun` to pass for both. Then STOP and report. The user runs the real batch.

---

## Standing constraints (hard)
- **Environment:** conda env `trader`. Run Python as `conda run -n trader python <file.py>`. Do **not** use `python3`, and do **not** pass multi-line code via `python -c` — write a temp `.py` file under the scratchpad dir and run it.
- **No silent null defaults:** config/manifest fields must be present and validated. If a required field is missing, the code should crash, not silently default to `None`. Mirror the CL manifests exactly.
- **TDD** where you add code (e.g., the new `data_processor` branches): add/extend a test, watch it fail, implement, watch it pass.
- **Do not commit** unless the user explicitly asks.
- **Do not launch GCP sweep/optimizer VMs.** The one paid external action you ARE authorized to take is the Databento data download (see Phase 1) — but estimate cost first and report it.
- **PowerShell note:** invoke the batch script directly as `& .\gcp\run_sweep_batch.ps1 ...` (do **not** prefix with `powershell -ExecutionPolicy Bypass`, which a safety classifier blocks).

---

## Architecture you are extending (verify by reading, don't trust blindly)

**Data build (local):**
- `src/data/databento_data_builder.py` — Databento continuous-futures downloader/converter. **ES is already wired**: `SYMBOL_MAP["ES"] = "ES.v.0"`. Emits `raw` / `ratio` / `panama` adjustment variants. CLI: `python -m src.data.databento_data_builder {estimate|submit|download|convert} ...`. `DATABENTO_API_KEY` is present in `.env`.
- `src/data_processor.py` — turns a raw OHLCV series into a features+targets parquet. Dispatches on `dataset_version` in `run()` (see `elif self.dataset_version == "HourSet_14A":` at ~line 889 and `"HourSet_14B":` at ~line 891). Output path = `data/processed/{symbol}_{dataset_version}.parquet` (see the `output_path` property ~line 151). So `symbol="ES", dataset_version="HourSet_01A"` → `data/processed/ES_HourSet_01A.parquet`.
- The **training** parquet is the **ratio-adjusted** series (stationary signals). The **execution/PnL** parquet is the **raw unadjusted** series → `ES_raw.parquet`. Keep these two separate; conflating them is a known past bug.

**Cloud consumption (do not modify unless required):**
- `gcp/run_sweep_batch.ps1` → `agent/batch_orchestrator.py`/`gcp/orchestrator.py` (schema validation via `BatchSweepConfig`) → sweep VMs → post-optimizer VM. Full workflow doc: `.agents/workflows/run-cloud-batch.md`.
- VMs fetch training data from `gs://cltrainer-optuna-results/data/{symbol}_{dataset_version}.parquet` and execution data from the manifest's explicit `execution_data_path`. See the dataset-name derivation in `gcp/orchestrator.py:75-88`, `gcp/vm_e2e_pipeline.py:1118-1124`, `gcp/vm_post_optimize.sh:172-178`, and `gcp/gcp_deploy_sweep.ps1:46-55`. **Confirm the symbol-prefix conditional** so ES resolves to `ES_HourSet_01A.parquet` (CL files ARE prefixed, e.g. `CL_HourSet_14B.parquet`, so ES should be too — but verify).

**Templates to mirror (v2 manifest, `baseline`/`experiments` schema):**
- `configs/batch_manifest_v2_hourset14a_canary.json`  ← template for the ES **01A** manifest
- `configs/batch_manifest_v2_hourset14b_canary.json`  ← template for the ES **01B** manifest
- Both share the same `data_workflow` (windows `[24,72,168]`, macro `{"1W":168}`, targets `2x1_6H` + `3x1_6H`, `raw_horizon:120`, `atr_period:14`), `random_seed:42`, `opt_mode:"individual"`, `slippage_per_side:0.01`, `holdout_cutoff_date:null` (2-way), `n_trials:3`, `post_optimizer_trials:3`, `post_optimizer_holdout_months:6`. The 14A vs 14B difference is entirely inside `data_processor.py`.
- **Reference only, do not reuse:** `configs/sweeps/sweep_es_v1.json` is a stale earlier ES attempt (5m resolution, `ES_Sweep_V1` recipe, different bucket). Do not build on it.

---

## Phase 0 — Orient & verify (no changes yet)
1. Read the files listed above; confirm the ES `SYMBOL_MAP` entry, the `data_processor` dispatch + `output_path` naming, and the manifest schema (`src/config/schemas.py` — `BatchSweepConfig`, `MasterConfig`, `DataWorkflowConfig`, `TrainingWorkflowConfig`, `ExecutionWorkflowConfig`, `OptunaConfig`).
2. Read exactly what the `HourSet_14A` and `HourSet_14B` dispatch branches call (the underlying `process_hourset_*` methods) so you can alias them correctly. Note any differences between 14A and 14B processing.
3. Confirm the target columns produced (`TARGET_TRIPLE_2x1_6H_LONG/SHORT`, `TARGET_TRIPLE_3x1_6H_LONG/SHORT`) — these must exist in the built ES parquets, since the manifests reference them.

## Phase 1 — ES data acquisition (real, full history) [NOTIFY POINT]
The existing sample is ~1 month; you must get full history.
1. Run the Databento **cost estimate** for a full ES pull (2005-01-01 → today, `ohlcv-1h`, `ES.v.0`) via `python -m src.data.databento_data_builder estimate --symbols ES ...`. **Report the estimate to the user** before submitting. (The user has pre-authorized the full download to 2005, but the cost must be surfaced.)
2. Submit the batch job (`submit`), poll, and `download` when ready (Databento batch jobs are async — handle the job-id lifecycle). If 2005 is unavailable, take the earliest available and note the actual start date.
3. Convert to the two variants the pipeline needs:
   - **Raw unadjusted** → produce `data/processed/ES_raw.parquet` (execution/PnL series; analogous to `CL_raw.parquet`).
   - **Ratio-adjusted** → the input series `data_processor` will featurize into the training parquets.
   Follow the CL provenance: `CL_raw.parquet` is the raw variant; the HourSet training parquets are built from the ratio-adjusted series. Match that convention exactly.
4. Sanity-check the downloaded series: date range covers ≥ the manifest `train_cutoff_date` (2022-01-01) with several years before AND after it, no massive gaps, monotonic timestamps, sane OHLC.

## Phase 2 — Build the two ES datasets (TDD)
1. Add two dispatch branches in `src/data_processor.py`:
   - `elif self.dataset_version == "HourSet_01A":` → call the **same** processing path as `HourSet_14A`.
   - `elif self.dataset_version == "HourSet_01B":` → call the **same** processing path as `HourSet_14B`.
   Add them to `DATASET_VERSIONS` metadata too. Add a focused test (mirror existing data_processor tests) asserting the ES branches dispatch to the 14A/14B logic and that the expected target columns are emitted. Red → green.
2. Build both parquets from the ratio-adjusted ES series:
   - `data/processed/ES_HourSet_01A.parquet`
   - `data/processed/ES_HourSet_01B.parquet`
3. Verify each parquet contains the four target columns and a sane feature matrix (no all-NaN columns, row count consistent with the date range after warmup).

## Phase 3 — Write the two ES canary manifests
Create, mirroring the CL templates exactly (change only what must change):
- `configs/batch_manifest_v2_es_hourset01a_canary.json` (from the 14A template)
- `configs/batch_manifest_v2_es_hourset01b_canary.json` (from the 14B template)

Per manifest set:
- `baseline.symbol = "ES"` and every experiment `overrides.symbol = "ES"`.
- `baseline.data_workflow.dataset_version = "HourSet_01A"` (resp. `"HourSet_01B"`).
- `baseline.execution_workflow.execution_data_path = "gs://cltrainer-optuna-results/data/ES_raw.parquet"`.
- Keep `random_seed:42`, `opt_mode:"individual"`, `slippage_per_side:0.01`, `holdout_cutoff_date:null`, `n_trials:3`, `post_optimizer_trials:3`, `post_optimizer_holdout_months:6`.
- Update `gcs_prefix`/`label` per experiment to ES-specific names (e.g. `sweep_es01a_2x1_6h_canary`), keep the same two target-column pairs.
- Decide `strategy_config_path`: the CL canary uses `configs/strategies/hourly_ensemble_010.json`. For a *pipeline* canary this is acceptable to reuse (the canary validates machinery, not ES-optimal params) — but flag in your hand-off that the baseline strategy is CL-derived and an ES-tuned baseline is a follow-up.
- **Watch the holdout/OOS collapse rule:** with `train_cutoff_date` and a full history, the 2-way OOS window must exceed `post_optimizer_holdout_months` (6). The dry-run guard checks this against the real ES dates; if ES history is short, adjust `train_cutoff_date` so the OOS window is comfortably > 6 months and note it.

## Phase 4 — Upload data to GCS
Upload the three parquets to the bucket the VMs read from, using the exact names the derivation expects:
```
gcloud storage cp data/processed/ES_HourSet_01A.parquet gs://cltrainer-optuna-results/data/ES_HourSet_01A.parquet
gcloud storage cp data/processed/ES_HourSet_01B.parquet gs://cltrainer-optuna-results/data/ES_HourSet_01B.parquet
gcloud storage cp data/processed/ES_raw.parquet          gs://cltrainer-optuna-results/data/ES_raw.parquet
```
Verify with `gcloud storage ls`. (gcloud project is `cltrainer`; CLI is used directly, no MCP.)

## Phase 5 — Dry-run gate (both manifests)
For each manifest, run the dry run directly and confirm ALL gates pass (train_cutoff parseable, no leak, holdout>0, slippage∈[0,0.5], opt_mode valid, **no holdout/OOS collapse** against the real ES dataset dates):
```
& .\gcp\run_sweep_batch.ps1 -ManifestPath "configs\batch_manifest_v2_es_hourset01a_canary.json" -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" -DryRun
& .\gcp\run_sweep_batch.ps1 -ManifestPath "configs\batch_manifest_v2_es_hourset01b_canary.json" -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" -DryRun
```
If a gate fails, fix the root cause (data range, manifest field, naming) and re-run until green. Do NOT proceed to a real launch.

## Phase 6 — Hand-off report [STOP HERE]
Report to the user:
- Databento cost incurred and the actual ES date range obtained.
- The files created (2 parquets + ES_raw, 2 manifests, GCS uploads, data_processor branches + tests).
- Dry-run results for both manifests (gate-by-gate).
- The exact command(s) the user should run to launch each real batch (the non-`-DryRun` form).
- Any caveats (e.g., CL-derived baseline strategy; any `train_cutoff` adjustment made for OOS window).
Then STOP. Do not launch the batch.

---

## Deliverables checklist
- [ ] Full ES history downloaded (real, back to 2005 or earliest) — cost estimated & reported.
- [ ] `data/processed/ES_raw.parquet` (raw/unadjusted, execution series).
- [ ] `data/processed/ES_HourSet_01A.parquet` and `ES_HourSet_01B.parquet` (ratio-adjusted, with target columns).
- [ ] `data_processor.py` HourSet_01A/01B branches + passing test(s).
- [ ] `configs/batch_manifest_v2_es_hourset01a_canary.json` and `..._01b_canary.json`.
- [ ] All three parquets uploaded to `gs://cltrainer-optuna-results/data/`.
- [ ] Both manifests pass `run_sweep_batch.ps1 -DryRun`.
- [ ] Hand-off report written; nothing committed; no VMs launched.

## Known gotchas (learned from the CL pipeline)
- **Raw vs ratio-adjusted:** train on ratio-adjusted, PnL on raw. Do not cross them.
- **Symbol-prefix naming:** VM data-path derivation expects `{symbol}_{dataset_version}.parquet`; verify ES resolves with the `ES_` prefix.
- **Holdout/OOS collapse:** if the backtest window ≤ `post_optimizer_holdout_months`, "pre" trades collapse to 0 and the dry run fails. Ensure the ES OOS window > 6 months.
- **No silent nulls:** every manifest field required & validated; crash on missing, never default to `None`.
- **Sample-vs-full trap:** the `DataBentoSample` ES file is a decoy — do not build the real pipeline on it.
