# Ticket Resolution Blueprint — batch-resume-recovery_07112026_0924
**Ticket Directory:** `.agents/collab/tickets/batch-resume-recovery_07112026_0924/`

## Bug Summary
Cloud batches (see `.agents/workflows/run-cloud-batch.md`) are driven by a LOCAL PowerShell
orchestrator (`gcp/run_sweep_batch.ps1`) that deploys/monitors cloud sweep VMs, downloads their
artifacts, then launches the post-optimizer VM. If that local process is **externally killed**
mid-run (terminal/IDE closed, OS kill, reboot — not a code crash; the log ends cleanly mid-poll),
the batch **STALLS with no recovery path**: completed sweeps stay uncollected, `batch_progress.json`
is left incomplete, and the post-optimizer never runs.

Cloud COST is already bounded by each VM's `--max-run-duration` DELETE TTL (added 2026-07-05), but
batch COMPLETION is unrecoverable, forcing a slow (~1 h) manual recovery. This just hit NG batch
`batch_20260711_061128` (5/6 sweeps done, orchestrator killed ~07:11).

**Root cause:** teardown *and* completion are both driven from the single local process; when it dies,
the completed-sweep collection + post-optimizer trigger are lost. The cloud side is fully recoverable
because each sweep VM uploads `production/*_artifacts.zip` + `pipeline_summary.json` to GCS **before**
self-shutdown, and the post-optimizer VM (`gcp/vm_post_optimize.sh`) consumes only GCS + the COMPLETED
entries of `batch_progress.json` — never local sweep dirs or orchestrator memory.

**Severity:** MEDIUM. **Regression:** No — the gap is original to the local-orchestrator design;
the 2026-07-05 TTL work capped cost, not completion. **Review verdict:** APPROVED (additive new file
only; no interface/base-class/refactor changes → no veto, no human authorization required).

## Target Files
- `scripts/resume_batch.ps1`  **(NEW — the sole implementation file)**
- `.agents/workflows/run-cloud-batch.md`  **(add a "## Resume a stalled batch" section documenting the new script)**

> No existing script is modified. `resume_batch.ps1` calls the existing `gcp/gcp_deploy_optimizer.ps1`
> and (optionally) `gcp/batch_orchestrator.py` unchanged.

## Grounding references (implementer: verify against these before coding)
- `gcp/run_sweep_batch.ps1:60-63` — `$BatchTimestamp = Get-Date 'yyyyMMdd-HHmmss'`;
  `$BatchId = "batch_$($BatchTimestamp -replace '-','_')"`. (Inverse used in step 3.)
- `gcp/run_sweep_batch.ps1:604-607` — timestamped prefix `"${basePrefix}_${BatchTimestamp}"` and local dir `reports\<tsPrefix>`.
- `gcp/run_sweep_batch.ps1:233-237` — `Save-Progress` / batch_progress.json write shape.
- `gcp/run_sweep_batch.ps1:1000-1011` — per-experiment progress entry schema
  (`index,label,vm_name,gcs_prefix,local_dir,status,exit_code,artifact_verified,failure_reason,wall_time_min`).
- `gcp/run_sweep_batch.ps1:1077-1091` — optimizer machine sizing: `armCount` (from `-Objective` list, `both`→2),
  `optTaskCount = completed × 4 × armCount`, tier map n2-standard-8/16/32/48.
- `gcp/run_sweep_batch.ps1:1093-1120` — post-opt param derivation: `-NTrials $postOptTrials`,
  `-HoldoutMonths $postOptHoldout`, `-OptMode` (from `baseline.execution_workflow.opt_mode`),
  `-ExecData`/`-SlippagePerSide` from manifest, `-Objective`, block params.
- `gcp/run_sweep_batch.ps1:1240-1295` — auto-rename convention `batch_<ts>_<SYMBOL>_<TIER>[_OBJAB]` +
  the post-rename config path-rewrite (`batch_runs/<id>/` → `batch_runs/<stampName>/`, BOM-less UTF-8).
- `gcp/gcp_deploy_optimizer.ps1:13-51` — `-BatchId` (Mandatory), `-NoMonitor`, `-NTrials`, `-OptMode`,
  `-ExecData`, `-SlippagePerSide`, `-MaxRunDurationMinutes` params.
- `gcp/gcp_deploy_optimizer.ps1:114-130` — requires `reports\batch_runs\<PLAIN BatchId>\batch_progress.json`
  to exist; uploads it + manifest.json to `gs://cltrainer-optuna-results/batch_optimizer/<BatchId>/`.
- `gcp/gcp_deploy_optimizer.ps1:364-372, 383-526` — `-NoMonitor` early-exits; the built-in poll uses **gsutil** (BROKEN here).
- `gcp/vm_post_optimize.sh:427-482` — iterates `batch_progress.json` experiments, skips non-`COMPLETED`,
  pulls each `gcs_prefix/production/*.zip` + `pipeline_summary.json` from GCS.
- `gcp/gcp_deploy_sweep.ps1:44-55` — dataset/prefix derivation (symbol-prefix-avoidance rule; reused by GCS path checks).
- `scripts/reap_orphan_vms.ps1:26-28` — targeted VM name-filter idiom `name~'^(optuna-sweep|opt-post)'`.

## Required Changes

Implement `scripts/resume_batch.ps1` with params:
`-BatchId <batch_...>` (required), `-DryRun`/`-WhatIf` (switch), `-Force` (switch),
`-Objective <string>` (default `"sharpe"`), and the pass-through knobs the orchestrator uses
(`-Zone`, `-SweepMode`, `-NBlocks`, `-LambdaDispersion`, `-MinBlockMonths`,
`-OptimizerMaxRunDurationMinutes` default 360, `-DisableTelegram`). Add `$gcloudBin` to PATH exactly
as `run_sweep_batch.ps1:57-58`. **Every bucket operation uses `gcloud storage`; gsutil is BANNED.**

**Step 1 — Resolve + load (crash loudly).**
- Validate `-BatchId` matches `^batch_\d{8}_\d{6}`; else fail with a clear message + non-zero exit.
- **Refinement #1 (glob-resolve, tolerate stamped dir):** resolve the batch dir by looking for
  `reports\batch_runs\<BatchId>` first, and if absent, glob `reports\batch_runs\<BatchId>_*` (an
  already-stamped/finalized dir). If neither resolves, or the resolved dir lacks `manifest.json`,
  crash loudly (non-zero exit).
- Load `manifest.json` (required) and `batch_progress.json` (may be absent → seed a fresh state in
  non-DryRun). Every manifest field the post-opt needs is REQUIRED — no silent defaults:
  `baseline.symbol`, `baseline.execution_workflow.opt_mode ∈ {individual,ensemble}`,
  `execution_data_path` (non-empty), `slippage_per_side` (present, in `[0,0.5]`),
  `training_workflow.optuna.post_optimizer_trials`, `.post_optimizer_holdout_months`.
  Any missing/invalid value → crash loudly.

**Step 2 — Authoritative experiment list.**
- Prefer re-deriving the experiment list the same way the orchestrator does (invoke
  `gcp/batch_orchestrator.py --batch-manifest <manifest>` and read its `experiments[]`,
  matching `run_sweep_batch.ps1:366-375`). If that invocation is impractical, fall back to reading
  `manifest.experiments[]` directly (each entry has `label` + base `gcs_prefix`). Whichever path is
  used, the base `gcs_prefix` per experiment is the key input to step 3.

**Step 3 — Reconstruct timestamped gcs_prefix (deterministic).**
- From `-BatchId` `batch_YYYYMMDD_HHMMSS`, derive the sweep timestamp `YYYYMMDD-HHMMSS`
  (i.e. `$BatchId -replace '^batch_','' -replace '_','-'`), then per experiment
  `tsPrefix = "<base gcs_prefix>_<YYYYMMDD-HHMMSS>"` and local dir `reports\<tsPrefix>`
  — matching `run_sweep_batch.ps1:604-607`. Cross-check the reconstruction against any existing
  `batch_progress.json` entries (their stored `gcs_prefix` already carries the ts suffix).

**Step 4 — Reconcile each experiment against GCS (`gcloud storage` only).**
- For each expected experiment, if it is already recorded `COMPLETED` AND its local sweep dir is
  structurally intact (`pipeline_summary.json` + a zip or extracted `registry/`), skip (idempotent).
- Otherwise probe GCS: `gcloud storage ls "$Bucket/<tsPrefix>/production/"` (look for `*.zip`) and
  `gcloud storage ls "$Bucket/<tsPrefix>/pipeline_summary.json"` (`ls` exits non-zero when absent).
- **Refinement #3 (BOTH-or-neither):** an experiment is recoverable **only** when BOTH
  `production/*.zip` AND `pipeline_summary.json` are present. "zip present but summary absent" (or the
  reverse) = NOT recoverable → report-and-leave for a human; **never** write a half-populated
  `COMPLETED` entry.
- For a genuinely-missing sweep (no artifacts), REPORT it clearly and leave it for a human — never
  fabricate a result. Also **never flip an existing `DEPLOY_FAILED`/`TIMEOUT` entry to `COMPLETED`.**
- **Refinement #4 + Step 5 gating:** downloading/extracting artifacts is a MUTATION → gated on
  NON-DryRun. In DryRun, only REPORT what would be downloaded/extracted. In non-DryRun, download
  `pipeline_summary.json` + `production/*.zip` (and best-effort `logs/*`) into `reports\<tsPrefix>\`,
  extract each zip into `registry\` (structure-equivalent to a normal completed dir), and verify the
  dir is intact afterward (crash if extraction left it incomplete rather than recording a false COMPLETED).

**Step 5 — Repair batch_progress.json idempotently (NON-DryRun only).**
- **Back up the original FIRST:** copy `batch_progress.json` → `batch_progress.json.bak.<timestamp>`
  before any write. **Refinement #4:** this `.bak` write and all progress writes are strictly gated on
  NON-DryRun (DryRun performs zero writes).
- Add/repair each recovered experiment as a `COMPLETED` entry using the exact schema at
  `run_sweep_batch.ps1:1000-1011` (`index,label,vm_name,gcs_prefix,local_dir,status,exit_code=0,
  artifact_verified=true,failure_reason=null`) plus a **`recovered:true`** provenance marker and a
  top-level `recovery_note`. Recompute `completed`/`failed`/`total` counts. Write via
  `ConvertTo-Json -Depth 10` (mirror `Save-Progress`, `run_sweep_batch.ps1:233-237`).
- If, after reconcile, zero experiments are `COMPLETED`, crash loudly (nothing to optimize;
  re-sweep the missing experiments first).

**Step 6 — Running-VM guard (targeted; runs in BOTH DryRun and live).**
- List this batch's VMs with the targeted filter `name~'^(optuna-sweep|opt-post)'`
  (`reap_orphan_vms.ps1:26-28`), then locally keep only names carrying this batch's sweep timestamp
  `YYYYMMDD-HHMMSS` (sweep VMs) or `opt-post-<BatchId-with-dashes>` (optimizer VM).
- If any such VM is `RUNNING`: in DryRun, REPORT that a live run would be blocked; in live mode,
  **REFUSE** (crash loudly, non-zero exit) unless `-Force` is passed — this protects an in-flight
  recovery/optimizer. **Never broad-kill processes.**

**Step 7 — Post-optimizer (only if outputs absent; NON-DryRun deploys, DryRun reports).**
- Detect existing post-opt outputs locally (`batch_summary_optimized_*.md` in the batch dir) or in GCS
  (`gcloud storage ls "$Bucket/batch_optimizer/<BatchId>/batch_summary_optimized_*.md"`). If present
  locally → skip. If present in GCS only → in non-DryRun just download them (no VM); in DryRun report that.
- **Refinement #2 (arm count is operator-supplied):** compute `armCount` from the `-Objective` arm list
  (comma-split; `both`→2; min 1) — it is NOT in the manifest. Print the assumed arm count and the
  resulting `optTaskCount = completed × 4 × armCount` and machine tier in the DryRun plan, so an operator
  can correct a multi-arm batch (else the opt VM under-sizes). Tier map exactly per
  `run_sweep_batch.ps1:1085-1088`.
- **Refinement #1 (deploy uses PLAIN id; rename is later):** invoke
  `gcp/gcp_deploy_optimizer.ps1 -BatchId <PLAIN batch_<ts>> -NoMonitor` with the derived params
  (`-NTrials $postOptTrials`, `-HoldoutMonths $postOptHoldout`, `-MachineType <tier>`, `-Workers 0`,
  `-Zone <fallback-zone>`, `-SweepMode`, `-OptMode <from manifest>`, `-Objective`, block params,
  `-MaxRunDurationMinutes $OptimizerMaxRunDurationMinutes`, `-ExecData`, `-SlippagePerSide` when >0,
  `-DisableTelegram` when set). `gcp_deploy_optimizer.ps1` REQUIRES
  `reports\batch_runs\<PLAIN BatchId>\batch_progress.json` (`gcp_deploy_optimizer.ps1:114-120`), so the
  batch dir MUST still be the unstamped `batch_<ts>` name at deploy time.
- **`-NoMonitor` is mandatory** because the built-in poll uses gsutil. The resume script then SELF-POLLS,
  gsutil-free: `gcloud storage ls` for the landed `batch_summary_optimized_*.md`, and
  `gcloud compute instances describe <optVm> --zone=<z> --format="get(status)"` for VM lifecycle
  (TERMINATED/STOPPED). On report-landing: download `*.md`/`*.json`/`batch_configs/*`/`predictions/*`
  from `batch_optimizer/<BatchId>/` with `gcloud storage cp`, then targeted-delete the opt VM (ignore
  "already gone"; it self-deletes on `--shutdown`, so this is a belt-and-suspenders idempotent cleanup).
  Keep the local wait cap ≥ `-OptimizerMaxRunDurationMinutes` (orphan rule: never force-kill a VM inside its TTL).

**Step 8 — Finalize: auto-rename AFTER post-opt (NON-DryRun; DryRun reports).**
- **Refinement #1:** rename to `batch_<ts>_<SYMBOL>_<TIER>[_OBJAB]` ONLY after post-opt outputs exist,
  and only if the dir is still the plain `batch_<ts>` name (skip if already stamped). SYMBOL from
  `baseline.symbol`; **TIER** derived from the frozen manifest CONTENT (the resume path has no manifest
  filename) — match `(canary|scout|prod)` in `training_workflow.gcs_base_dir` or any experiment
  `gcs_prefix`, uppercased, fallback `RUN`; `_OBJAB` when `armCount > 1`. After rename, rewrite embedded
  `batch_runs/<id>/` → `batch_runs/<stampName>/` in `configs\*.json` written BOM-less UTF-8, exactly as
  `run_sweep_batch.ps1:1264-1288`. A failed rename only warns (never fails the run).
- Confirm zero leftover VMs for this batch (re-list with the targeted filter); report any survivors and
  point at `scripts/reap_orphan_vms.ps1`.

**DryRun contract (explicit):** `-DryRun`/`-WhatIf` performs steps 1-4 and 6 READ-ONLY, prints the full
plan (dirs/files it would download, entries it would reconstruct, the optimizer command + assumed arm
count/tier it would deploy, the finalize rename it would apply), and makes **zero** filesystem mutations,
**zero** `.bak` writes, and **zero** deploys/VM ops. It must correctly report an already-recovered/complete
batch (e.g. the live NG reference) without attempting to modify or deploy.

**Global rules:** `gcloud storage` for all bucket ops (never gsutil); crash loudly on any missing/invalid
input; targeted VM ops only (never broad-kill); backup-before-mutate; idempotent + safe to run twice.

## Documentation change (`.agents/workflows/run-cloud-batch.md`)
Add a `## Resume a stalled batch` section: when to use it (local orchestrator externally killed mid-run
— stalled, not crashed), the **`-DryRun`-first habit**, the running-VM guard (`-Force` to override), the
`gcloud storage`/no-gsutil note and why the optimizer is launched with `-NoMonitor`, the `-Objective`
arm-count caveat for multi-arm batches, and that a truly-missing sweep (no GCS artifacts) is left for a
human to re-sweep rather than fabricated. Example:
`.\scripts\resume_batch.ps1 -BatchId batch_YYYYMMDD_HHMMSS -DryRun` then the same without `-DryRun`.

## Validation the implementer MUST run before declaring done
1. AST parse-check: `[System.Management.Automation.Language.Parser]::ParseFile('<abs path>', [ref]$null, [ref]$errs)` — must be error-free.
2. **DryRun ONLY** against the live NG reference batch `batch_20260711_061128` (its `opt-post-*` VM is
   RUNNING and another process owns it): the script must REPORT the already-recovered/complete state and
   the running-VM guard WITHOUT mutating anything or touching any VM. Do NOT run it live against that batch.

## Edge cases deliberately left to a human (v1)
- A truly-missing sweep (no GCS artifacts): reported, NOT re-launched. Re-sweeping is a manual follow-up.
- Multi-arm objective batches: operator must pass the correct `-Objective`; the script cannot infer arm
  count from the manifest (surfaced + printed in the plan per Refinement #2).
