# Run Cloud Batch Experiment Workflow

// turbo-all

Fully-automated GCP Optuna batch: **deploy → monitor → collect → post-optimize → report**.
Orchestrated by `gcp/run_sweep_batch.ps1` against a **v2 manifest** (`configs/batch_manifest_v2_*.json`,
`baseline`/`overrides` schema validated by `BatchSweepConfig`). The legacy `defaults`/`target_long`
format is retired.

> The **manifest is the single source of truth.** Every operational parameter is required and
> validated; there are no silent code-side defaults. opt_mode, slippage, holdout, and trials all
> come from the manifest, never from CLI flags.
>
> `baseline.symbol` is **REQUIRED** — the config generator hard-raises without it (post-T6) and
> stamps `execution_symbol` + `models.*.symbol` from it. **Non-CL manifests MUST also carry a
> `defaults` block** (`defaults.strategy_config` = the symbol's baseline config): the local
> generator (`agent/generate_ensemble_artifacts.py:303`) ignores `strategy_config_path` and
> silently falls back to the CL base `hourly_ensemble_010.json` when `defaults` is absent. See
> [build-symbol-pipeline](build-symbol-pipeline.md) Phase 5 (C2).

## Orphaned-VM prevention (RULES — added 2026-07-05 after the 0805/0807/0808 incident)

VM teardown is driven by **local** PowerShell monitor processes. If that process dies
(machine reboot, killed terminal, IDE/workspace refresh, an agent broad-killing
`powershell.exe`), the cloud VMs are orphaned and burn credits silently. Defenses, in order:

1. **Control-plane TTL (automatic, primary):** every VM is now created with
   `--max-run-duration` (`gcp_deploy_sweep.ps1`: 480m; `gcp_deploy_optimizer.ps1`: 360m,
   `--instance-termination-action=DELETE` for STANDARD / `STOP` for SPOT). GCP kills the VM
   at the deadline even if this machine is off. Do not remove these flags; if an experiment
   legitimately needs longer, raise the TTL alongside `timeout_minutes`, keeping TTL > timeout.
   The optimizer TTL is parameterized (`gcp_deploy_optimizer.ps1 -MaxRunDurationMinutes`,
   forwarded from `run_sweep_batch.ps1 -OptimizerMaxRunDurationMinutes`): **multi-arm objective
   A/B runs should pass `-OptimizerMaxRunDurationMinutes 720`** (sharpe-only default stays 360m).
2. **Agents MUST NEVER broad-kill processes** (`Stop-Process powershell`, `taskkill /im
   powershell.exe`, etc.) — each running orchestrator is some batch's only local teardown path.
   Kill a specific PID only when you launched it and know its batch is finished.
3. **Reconcile after any interruption:** if this machine rebooted, a terminal was killed, or
   you inherit a session with unknown state, run `.\scripts\reap_orphan_vms.ps1` (report-only)
   and review; add `-Delete` to remove anything older than its legitimate runtime. Also run it
   as a routine end-of-day check while pre-TTL VMs may still exist.
4. **Batch completion check:** a finished batch should leave ZERO `optuna-sweep-*`/`opt-post-*`
   instances for its batch-id timestamp. The monitor prints teardown; if you didn't see it,
   assume orphans and reconcile.

## Resume a stalled batch

**When to use it:** the LOCAL orchestrator (`gcp/run_sweep_batch.ps1`) was **externally KILLED
mid-run** — terminal/IDE closed, OS kill, reboot (the log ends cleanly mid-poll). This is a
**stall, not a crash**: the sweep VMs finished and uploaded `production/*_artifacts.zip` +
`pipeline_summary.json` to GCS before self-shutdown, but the local process died before it could
collect them and trigger the post-optimizer, so `batch_progress.json` is left incomplete and the
batch never completes. `scripts/resume_batch.ps1` recovers COMPLETION (cost was already bounded by
each VM's `--max-run-duration` DELETE TTL). Do NOT use it for a genuine code crash — that is a bug
to fix, not a recovery.

**Always `-DryRun` first.** The dry run performs steps 1-4 and the running-VM guard **read-only**,
prints the full plan (which GCS artifacts it would download, the `batch_progress.json` entries it
would reconstruct, the optimizer command + assumed arm count/machine tier, and the finalize rename),
and makes **zero** filesystem mutations, zero `.bak` writes, and zero deploys/VM ops. Review the plan,
then re-run without `-DryRun` to execute it.

```powershell
.\scripts\resume_batch.ps1 -BatchId batch_YYYYMMDD_HHMMSS -DryRun
.\scripts\resume_batch.ps1 -BatchId batch_YYYYMMDD_HHMMSS
```

- **Running-VM guard (`-Force` to override).** Before doing anything mutating, it lists this batch's
  VMs with the **targeted** filter `name~'^(optuna-sweep|opt-post)'` (never a broad kill) and keeps
  only names carrying this batch's sweep timestamp or its `opt-post-<batch-id>` VM. If any is
  `RUNNING`, a live run **REFUSES** (non-zero exit) — another process may own an in-flight
  recovery/optimizer. Pass **`-Force`** to override deliberately. In `-DryRun` it only reports the block.
- **`gcloud storage`, never the legacy bucket CLI.** Every bucket op goes through `gcloud storage`;
  the legacy `gsutil` poll is broken in this env (python3.13). For the same reason the post-optimizer
  is deployed with **`-NoMonitor`** (its built-in poll uses the broken CLI) and this script **self-polls**
  gsutil-free — `gcloud storage ls` for the landed `batch_summary_optimized_*.md` and
  `gcloud compute instances describe --format="get(status)"` for VM lifecycle.
- **`-Objective` arm-count caveat (multi-arm batches).** The optimizer machine tier is sized from
  `completed × 4 × armCount`, and **armCount is inferred from `-Objective`, not the manifest**
  (`both` → 2; a comma-separated arm list → one per arm). For a multi-arm A/B batch you MUST pass the
  same `-Objective` list the sweep used (and `-OptimizerMaxRunDurationMinutes 720`), or the opt VM
  under-sizes. The dry run prints the assumed arm count + tier so you can correct it.
- **A truly-missing sweep is left for a human.** An experiment is recoverable **only** when BOTH
  `production/*.zip` AND `pipeline_summary.json` are present in GCS. A partial (one but not the other)
  or fully-missing sweep is **reported and left** — never fabricated into a `COMPLETED` entry, and an
  existing `DEPLOY_FAILED`/`TIMEOUT` is never flipped. Re-sweeping those is a manual follow-up. If
  zero experiments are recoverable, the script crashes loudly (nothing to optimize).

The script is idempotent and safe to run twice: it backs up `batch_progress.json` before any write,
skips already-COMPLETED-and-intact experiments, downloads post-opt outputs already in GCS instead of
re-deploying, and only finalize-renames the dir to `batch_<ts>_<SYMBOL>_<TIER>[_OBJAB]` after the
post-opt outputs exist (rewriting the embedded `batch_runs/<id>/` config paths, BOM-less, exactly like
the orchestrator). A failed rename only warns. Reconcile any leftover VMs it reports with
`scripts/reap_orphan_vms.ps1`.

## Tiers

| Tier | Manifest (v2) | Experiments | Sweep `n_trials` | Post-opt trials | Use |
|------|---------------|-------------|------------------|-----------------|-----|
| **Canary** | `batch_manifest_v2_hourset14a_canary.json` | 2 | 3 | 3 | Pipeline validation / parity (~20-30 min) |
| **Scout** | `batch_manifest_v2_hourset14a_scout.json` | 4 | 200 | 200 | Moderate exploration |
| **Production** | generate via `scripts/generate_v2_manifest.py` | 8 | 500 | 1500 | Deep optimization |

## opt_mode — the post-optimizer chain

`baseline.execution_workflow.opt_mode` selects the post-optimizer chain. Required; read from the manifest.

| `opt_mode` | Passes | Selection | Produces | Notes |
|------------|--------|-----------|----------|-------|
| **`individual`** (default) | 2 (individual → ensemble) | `unified_pair_optimizer.py` → **Top 4** (`top_pairs.json`) | per-side `batch_summary_optimized_sharpe.md` **and** `batch_summary_optimized_ensembles_sharpe.md` + `sharpe_ensemble_backtests.md` | Reproduces CANARY_V1. Pass 1 optimizes each side; top individuals are paired; pass 2 re-optimizes the pairs — all in one optimizer-VM call. |
| **`ensemble`** | 1 (brute force) | `select_top_ensembles.py` → Top 8 (`top_8_ensembles.json`) | `batch_ensemble_pre_opt.md` + ensemble reports only | Sweeps all long/short combos; skips per-side optimization. Diverges from CANARY_V1. |

> **Objective = Sharpe only (since 2026-07-04, ticket `drop-sortino-objective_07042026_2301`).** The
> post-optimizer chain no longer runs the Sortino pass, so `*_sortino.*` artifacts are **not** produced.
> This is a deploy-chain default (`gcp/gcp_deploy_optimizer.ps1` → `gcp/vm_post_optimize.sh`), not a
> manifest field — old manifests run unchanged. **Rollback (per run):** pass `-Objective both` to
> `gcp_deploy_optimizer.ps1` (or `--objective=both` to `vm_post_optimize.sh`); all Sortino code is intact.
> Run folders produced **before 2026-07-04** contain Sortino artifacts (historical; still parity-checkable).
>
> **Block-wise Sharpe objectives (since 2026-07-09, ticket `block-sharpe-objective-ab_07092026_1031`).**
> `-Objective` accepts a comma-separated ARM LIST; besides `sharpe`/`sortino` there are three block
> metrics that partition the in-sample monthly PnL into contiguous calendar blocks and aggregate the
> per-block Sharpes:
>
> | Metric | Aggregation |
> |--------|-------------|
> | `block_min` | min of per-block Sharpes (punishes any dead block) |
> | `block_median` | median of per-block Sharpes |
> | `block_mean_std` | mean − λ·std of per-block Sharpes (dispersion-penalized) |
>
> A/B invocation through the deploy chain (all arms score the SAME sweep models in one batch;
> per-arm Optuna studies, seed offsets 2/3/4):
> ```powershell
> & .\gcp\run_sweep_batch.ps1 -ManifestPath <manifest> `
>     -Objective "sharpe,block_min,block_median,block_mean_std" `
>     -OptimizerMaxRunDurationMinutes 720
> ```
> Block params (defaults): `-NBlocks 3`, `-LambdaDispersion 1.0`, `-MinBlockMonths 10` — threaded to
> both post-optimizer passes and echoed in every report header (self-describing runs). **Per-arm
> artifacts are suffixed with the metric name** exactly like the sharpe/sortino era:
> `batch_summary_optimized_block_min.md`, `optimization_results_block_min.json`,
> `top_pairs_block_min.json` (sharpe keeps plain `top_pairs.json` — parity-compatible),
> `batch_summary_optimized_ensembles_block_min.md`, `block_min_ensemble_backtests.md`, …
> Pass-1 → pair selection → pass-2 is **per-arm end-to-end** (no cross-arm pooling). After the run,
> `scripts/compare_objective_arms.py --batch-dir <dir>` writes `objective_ab_summary.md` — read the
> verdict on **holdout PnL** (block arms' opt PnL is lower than baseline's by construction).
> **TTL:** multi-arm runs take ~1 pool wave per arm — pass `-OptimizerMaxRunDurationMinutes 720`
> (default 360 is sized for sharpe-only), keeping TTL above the local monitor timeout.

## Date controls — train_cutoff vs holdout_cutoff vs holdout_months

Three distinct controls; getting them wrong silently collapses the OOS window (→ `0/0/0` "pre" trades).
The dry run now **fails** on collapse (see below).

| Field | Type | Stage | Meaning |
|-------|------|-------|---------|
| `train_cutoff_date` | date | sweep | **Training end.** Train = data before it. |
| `holdout_cutoff_date` | date / `null` | sweep | **`null` (default) = 2-way:** vault = all OOS after `train_cutoff`. **Set (3-way):** OOS splits into Validation `[train_cutoff, holdout_cutoff)` + final Vault `[holdout_cutoff, data_end]`; the post-optimizer backtests the **Vault**. |
| `post_optimizer_holdout_months` | length | post-opt | The **last N months** of the backtest window are carved as the post-opt holdout; everything before is "pre". |

> **Collapse rule:** if the backtest window (Vault in 3-way, OOS in 2-way) ≤ `post_optimizer_holdout_months`,
> the post-opt carve swallows the whole window → "pre" = 0 trades. **Default to 2-way (`null`)** unless you
> deliberately need a separate vault; the dry-run guard verifies the window against the real dataset dates.
>
> **`post_optimizer_holdout_months: 12` is the 15B-scout setting** (was 6; changed by ticket
> `block-sharpe-objective-ab_07092026_1031`): 12 monthly holdout observations instead of 6, and on
> the 2022-01 → 2026-06 CL window the remaining in-sample ≈ 41 months splits into 3 calendar blocks
> of ~14 months each. **Preflight block gate:** when any block metric is in `-Objective`, the dry
> run additionally requires `(window − holdout) ≥ n_blocks × min_block_months` (via
> `scripts/preflight_holdout_check.py --objectives ... --n-blocks ... --min-block-months ...`) and
> prints the computed calendar block layout; violation fails the dry run.

## 1. Verify no VMs are running
```powershell
gcloud compute instances list
```

## 2. Dry run (schema + sanity gate — no VMs created)
```powershell
& .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\batch_manifest_v2_hourset14a_scout.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f" `
    -DryRun
```
(Invoke the script directly with `&` — never prefix `powershell -ExecutionPolicy Bypass`, a safety
classifier blocks it.)
The dry run aborts before any deploy if any gate fails:
1. `train_cutoff_date` defined & parseable
2. no leak: `train_cutoff < holdout_cutoff` (when 3-way)
3. `post_optimizer_holdout_months > 0`
4. `slippage_per_side ∈ [0, 0.5]` (absolute price units; guards the −$2.5M class)
5. `opt_mode ∈ {individual, ensemble}`
6. **holdout/OOS collapse** — `scripts/preflight_holdout_check.py` loads the dataset's real date range and fails if the post-opt holdout would swallow the whole backtest window
7. **GCS dataset existence** — verifies the referenced cloud inputs actually exist in the bucket **before any VM is created**: the sweep dataset `gs://cltrainer-optuna-results/data/<SYMBOL>_<dataset_version>.parquet` (derived exactly as `gcp_deploy_sweep.ps1` does — `<SYMBOL>_` prefix unless `dataset_version` already starts with the symbol) **and** `baseline.execution_workflow.execution_data_path`. A missing object fails the dry run in seconds with `FAIL: required input not found in GCS: <url>` — zero VMs, zero credits. Closes the gap that let a locally-built-but-never-uploaded parquet reach deploy, where `[4/6]` fails with `No URLs matched` and (pre-fix) burned ~22 min of pointless zone retries mis-classified as `STARTUP_TIMEOUT`. Uses `gcloud storage` (local `gsutil` is broken — python3.13).

## 3. Launch
```powershell
& .\gcp\run_sweep_batch.ps1 `
    -ManifestPath "configs\batch_manifest_v2_hourset14a_scout.json" `
    -Zone "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f"
```
The orchestrator then: deploys sweep VMs across fallback zones (quota-aware), monitors via background
jobs, runs an artifact-verification gate before deleting each VM, captures crash diagnostics on failure,
deploys the post-optimizer VM (reads `opt_mode`), downloads results, and writes the consolidated reports.

> **Supervision (do not fire-and-forget).** After launch, the agent MUST watch the run to completion —
> tail the orchestrator's foreground output, or poll `reports\batch_runs\<batch_id>\batch_progress.json`
> (the machine-readable failure signal; Telegram alerts go to the human, not the agent). A `DEPLOY_FAILED`
> on the FIRST experiment means a systemic problem (missing shared dataset, quota, bad config) that will
> doom every remaining experiment. **`DATA_MISSING` (missing GCS input) is non-retryable** — the object is
> absent in every zone; the orchestrator now classifies it, skips the remaining zones, and **aborts the
> whole batch** (rather than the old behavior of cycling 7 zones × every experiment ≈ 2 h of wasted VMs).
> If you ever see a batch grind on past a first-experiment deploy failure, abort it and reconcile VMs.

**Folder Naming Convention (AUTO-STAMPED since ticket `block-sharpe-objective-ab_07092026_1031`):**
`run_sweep_batch.ps1` now renames the output directory itself after the batch completes and results
are downloaded — no manual rename step. This prevents ambiguity across multiple runs.
Format: `batch_<timestamp>_<SYMBOL>_<TIER>[_OBJAB]` — SYMBOL from the manifest `baseline.symbol`,
TIER = first match of (canary|scout|prod) in the manifest filename (uppercased; fallback `RUN`),
and `_OBJAB` appended when the objective list has more than one arm.
Example: `batch_20260706_143139` → `batch_20260706_143139_CL_SCOUT_OBJAB`.
A failed rename only logs a warning (never fails the batch) — if you see the warning, the folder
keeps its plain `batch_<timestamp>` name and may be renamed manually to the same format.

## 4. Validate parity (canary/parity runs)
```powershell
conda activate trader
python scripts/compare_parity.py --run reports\batch_runs\batch_<timestamp>
# exit 0 = PARITY PASS: checks artifact set, Top-4, no FileNotFound/new tracebacks, slippage 0.01, sane PnL
```

## 5. Validate generated configs (blocking)
Run the CONFIG VALIDATION GATE from [build-symbol-pipeline](build-symbol-pipeline.md) Phase 6
against the downloaded batch dir (from the repo root):
```powershell
conda run -n trader python <scratchpad>\validate_batch_configs.py reports\batch_runs\batch_<timestamp>
```
Exit 0 required (zero configs found = FAIL). Per config it asserts: resolves via
`resolve_instrument_context`, `execution_symbol == manifest baseline.symbol`, `models.*.symbol`
present, and every `model_path`/`predictions_path` exists on disk. The batch is not "done" until
this gate passes.

## Output (opt_mode=individual layout)
```
reports/batch_runs/batch_<timestamp>/
├── batch_progress.json                          ← live progress tracker
├── batch_summary.md                             ← unoptimized results
├── batch_summary_optimized_sharpe.md            ← per-side individual optimization (MAIN)
├── optimization_results_sharpe.json
├── top_pairs.json                               ← Top 4 ensemble pairs
├── batch_summary_optimized_ensembles_sharpe.md  ← Top-4 ensemble optimization
├── optimization_results_ensembles_sharpe.json
├── sharpe_ensemble_backtests.md                 ← full backtest dumps per ensemble
├── wall_clock_summary.md
├── configs/                                     ← config JSONs per ensemble — subject to the config validation gate (step 5) before use
├── predictions/                                 ← merged prediction CSVs per ensemble
└── manifest.json                                ← frozen config
```
(opt_mode=ensemble instead emits `batch_ensemble_pre_opt.md` + `top_8_ensembles.json`.)

## Objective tuning notes
- **Trade-floor penalty** (`agent/strategy_optimizer.py`): `TRADES_PER_YEAR_FLOOR=100` (ensemble) /
  `50` (single-side); smooth sigmoid weight multiplies positive scores so hyper-selective low-trade
  configs are penalized.
- **`OBJECTIVE_SCORE_CAP = 5.0`**: ceiling on the Sharpe *objective* (not the displayed metric).
  Caps the exploding ratio of low-downside (low-trade) configs so the trade-floor penalty stays dominant.
  (The cap/floor mechanics are objective-agnostic and still apply to Sortino under `-Objective both`.)

## Infrastructure
- **Sweep machine**: `c2-standard-16` (16 vCPUs, ~64 GB). Threads auto-detected via `nproc`.
- **Concurrency**: vCPU- and VM-count-gated (`max_concurrent_vms` in the manifest `infrastructure`).
- **IP quota**: external-IP-limited per region; the post-optimizer runs **after** all sweep VMs are deleted.
- **STANDARD** provisioning for runs that must complete (SPOT can be preempted).
- Preferred region **us-west1**; pass comma-separated zones to `-Zone` for fallback.

## Key scripts
| Script | Purpose |
|--------|---------|
| `gcp/run_sweep_batch.ps1` | Batch orchestrator (deploy → monitor → collect → post-optimize → report) |
| `gcp/gcp_deploy_sweep.ps1` | Single sweep VM deploy (per-VM zip + upload verify/retry) |
| `gcp/gcp_deploy_optimizer.ps1` | Post-optimizer VM deploy (code-integrity hash gate) |
| `gcp/vm_sweep_run.sh` / `gcp/vm_e2e_pipeline.py` | VM-side sweep |
| `gcp/vm_post_optimize.sh` | VM-side post-optimizer (parses manifest, runs opt_mode chain) |
| `scripts/preflight_holdout_check.py` | Dry-run holdout/OOS collapse guard + block-layout gate (`--objectives/--n-blocks/--min-block-months`) |
| `scripts/compare_parity.py` | Structural parity check vs a reference run |
| `scripts/compare_objective_arms.py` | Cross-arm objective A/B readout → `objective_ab_summary.md` (holdout PnL is the verdict) |
| `scripts/generate_v2_manifest.py` | Generate a v2 manifest |
