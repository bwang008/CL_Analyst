<#
.SYNOPSIS
    Resume a STALLED cloud batch whose local orchestrator (gcp/run_sweep_batch.ps1)
    was externally killed mid-run (terminal/IDE closed, OS kill, reboot - not a
    code crash). Salvages the completed sweeps from GCS, repairs
    batch_progress.json, launches the post-optimizer, and finalizes the batch dir.

.DESCRIPTION
    Cloud COST is already bounded by each VM's --max-run-duration DELETE TTL; this
    script recovers batch COMPLETION, which is otherwise lost when the single local
    process dies. Each sweep VM uploads production/*_artifacts.zip + pipeline_summary.json
    to GCS BEFORE self-shutdown, and the post-optimizer VM consumes only GCS + the
    COMPLETED entries of batch_progress.json - so the cloud side is fully recoverable.

    8-step recovery (see .agents/collab/tickets/batch-resume-recovery_07112026_0924/blueprint.md):
      1. Resolve + load manifest/progress (crash loudly on missing/invalid required fields).
      2. Derive the authoritative experiment list.
      3. Reconstruct the timestamped gcs_prefix deterministically from the BatchId.
      4. Reconcile each experiment against GCS (gcloud storage; BOTH zip+summary or nothing).
      5. Repair batch_progress.json idempotently (backup-first; NON-DryRun only).
      6. Running-VM guard (targeted filter; REFUSE unless -Force).
      7. Post-optimizer (only if outputs absent; -NoMonitor + self-poll via gcloud storage).
      8. Finalize: auto-rename + config path-rewrite (batch_runs/<id>/ -> batch_runs/<stampName>/).

    gcloud storage is used for EVERY bucket op - the legacy bucket CLI is BANNED here
    (its poll is broken in this env - python3.13). Crash loudly on missing/invalid input; targeted
    VM ops only (never broad-kill); backup-before-mutate; idempotent + safe to run twice.

.EXAMPLE
    .\scripts\resume_batch.ps1 -BatchId batch_20260711_061128 -DryRun
    .\scripts\resume_batch.ps1 -BatchId batch_20260711_061128
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$BatchId,

    # -WhatIf is accepted as an alias of -DryRun (both mean "read-only plan, no mutations").
    [Alias('WhatIf')]
    [switch]$DryRun,

    # Override the running-VM guard (an in-flight recovery/optimizer would otherwise block us).
    [switch]$Force,

    # Objective ARM LIST (comma-separated). NOT in the manifest - operator-supplied.
    # `both` -> 2 arms; multi-arm batches must pass the correct list or the opt VM under-sizes.
    [string]$Objective = "sharpe",

    # Pass-through knobs the orchestrator/optimizer use.
    [string]$Zone = "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f",
    [string]$SweepMode = "backtest",
    [int]$NBlocks = 3,
    [double]$LambdaDispersion = 1.0,
    [int]$MinBlockMonths = 10,
    [int]$OptimizerMaxRunDurationMinutes = 360,
    [switch]$DisableTelegram
)

$ErrorActionPreference = "Continue"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

# Add gcloud to PATH (mirror run_sweep_batch.ps1:57-58).
$gcloudBin = "C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin"
if ($env:PATH -notlike "*$gcloudBin*") { $env:PATH = "$gcloudBin;$env:PATH" }

$Bucket = "gs://cltrainer-optuna-results"

function Write-Fatal {
    param([string]$Message)
    Write-Host "FATAL: $Message" -ForegroundColor Red
    exit 1
}

function Write-Info { param([string]$m) Write-Host $m -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host $m -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Host $m -ForegroundColor Yellow }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host " RESUME STALLED BATCH$(if ($DryRun) { '  [DRY RUN - read-only plan]' })" -ForegroundColor Magenta
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host "  BatchId:   $BatchId"
Write-Host "  Objective: $Objective"
Write-Host "  DryRun:    $DryRun   Force: $Force"

# ============================================================
# STEP 1 - Resolve + load (crash loudly)
# ============================================================

# Validate BatchId shape.
if ($BatchId -notmatch '^batch_\d{8}_\d{6}') {
    Write-Fatal "invalid -BatchId '$BatchId' - must match ^batch_\d{8}_\d{6} (e.g. batch_20260711_061128)."
}

$batchRunsRoot = Join-Path $ProjectDir "reports\batch_runs"

# Refinement #1: glob-resolve, tolerate an already-stamped/finalized dir.
$BatchDir = $null
$plainDir = Join-Path $batchRunsRoot $BatchId
if (Test-Path $plainDir) {
    $BatchDir = (Resolve-Path $plainDir).Path
} else {
    $stampedMatch = Get-ChildItem -Path $batchRunsRoot -Directory -Filter "$($BatchId)_*" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($stampedMatch) { $BatchDir = $stampedMatch.FullName }
}

if (-not $BatchDir) {
    Write-Fatal "batch dir not found: neither reports\batch_runs\$BatchId nor reports\batch_runs\$($BatchId)_* resolves. Nothing to resume."
}

$ManifestPath = Join-Path $BatchDir "manifest.json"
if (-not (Test-Path $ManifestPath)) {
    Write-Fatal "manifest.json not found in $BatchDir - cannot resume without the frozen manifest."
}
Write-Ok "  Resolved batch dir: $BatchDir"

# Load manifest (required).
try {
    $manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
} catch {
    Write-Fatal "manifest.json failed to parse ($_)."
}

# ---- Required manifest fields - NO silent defaults; crash loudly on missing/invalid. ----
$symbol = [string]$manifest.baseline.symbol
if ([string]::IsNullOrWhiteSpace($symbol)) {
    Write-Fatal "baseline.symbol missing/empty in manifest - required."
}

$optMode = [string]$manifest.baseline.execution_workflow.opt_mode
if ($optMode -ne 'individual' -and $optMode -ne 'ensemble') {
    Write-Fatal "baseline.execution_workflow.opt_mode must be one of {individual, ensemble}; got '$optMode'."
}

$execData = [string]$manifest.baseline.execution_workflow.execution_data_path
if ([string]::IsNullOrWhiteSpace($execData)) {
    Write-Fatal "baseline.execution_workflow.execution_data_path missing/empty in manifest - required (no silent default)."
}

if ($null -eq $manifest.baseline.execution_workflow.slippage_per_side) {
    Write-Fatal "baseline.execution_workflow.slippage_per_side missing in manifest - required (no silent default)."
}
$slippage = [double]$manifest.baseline.execution_workflow.slippage_per_side
if ($slippage -lt 0 -or $slippage -gt 0.5) {
    Write-Fatal "baseline.execution_workflow.slippage_per_side ($slippage) out of range - must be within [0, 0.5]."
}

if ($null -eq $manifest.baseline.training_workflow.optuna.post_optimizer_trials) {
    Write-Fatal "baseline.training_workflow.optuna.post_optimizer_trials missing in manifest - required."
}
$postOptTrials = [int]$manifest.baseline.training_workflow.optuna.post_optimizer_trials

# Pass-2 budget + MANIFEST-DRIVEN trigger (2026-07-14, same rule as
# run_sweep_batch.ps1): post_optimizer_ensemble_trials > 0 -> pass-2 runs on the
# resumed optimizer; 0 or null/missing -> baseline-only. VALUE inheritance kept so
# the sh always receives a concrete positive number (legacy opt_mode="ensemble").
# This also FIXES the old resume gap where pass-2 was never re-enabled on resume.
$postOptEnsTrials = $manifest.baseline.training_workflow.optuna.post_optimizer_ensemble_trials
$runEnsembleOpt = ($null -ne $postOptEnsTrials -and [int]$postOptEnsTrials -gt 0)
if ($null -eq $postOptEnsTrials -or [int]$postOptEnsTrials -le 0) { $postOptEnsTrials = $postOptTrials }
$postOptEnsTrials = [int]$postOptEnsTrials

# Pair-selection width (optional; EXPLICIT default 2 = historical 2x2 pairs).
# Resume reads the RAW manifest (no pydantic pass), so the [1, 8] schema range
# is re-enforced loudly here — never forwarded unvalidated to the VM.
$pairTopN = $manifest.baseline.training_workflow.optuna.pair_selection_top_n
if ($null -eq $pairTopN) { $pairTopN = 2 }
$pairTopN = [int]$pairTopN
if ($pairTopN -lt 1 -or $pairTopN -gt 8) {
    Write-Fatal "baseline.training_workflow.optuna.pair_selection_top_n ($pairTopN) out of range - must be within [1, 8]."
}

if ($null -eq $manifest.baseline.training_workflow.optuna.post_optimizer_holdout_months) {
    Write-Fatal "baseline.training_workflow.optuna.post_optimizer_holdout_months missing in manifest - required."
}
$postOptHoldout = [int]$manifest.baseline.training_workflow.optuna.post_optimizer_holdout_months

Write-Ok "  Manifest validated: symbol=$symbol opt_mode=$optMode slippage=$slippage trials=$postOptTrials ens_trials=$postOptEnsTrials pair_top_n=$pairTopN holdout_months=$postOptHoldout"

# Load batch_progress.json (may be absent - seed a fresh state in NON-DryRun).
$progressFile = Join-Path $BatchDir "batch_progress.json"
$progress = $null
if (Test-Path $progressFile) {
    try {
        $progress = Get-Content $progressFile -Raw | ConvertFrom-Json
    } catch {
        Write-Fatal "batch_progress.json exists but failed to parse ($_)."
    }
    Write-Info "  Loaded existing batch_progress.json ($(@($progress.experiments).Count) experiment entries)."
} else {
    Write-Warn "  batch_progress.json absent - will seed fresh state (NON-DryRun only)."
}

# ============================================================
# STEP 2 - Authoritative experiment list
# ============================================================
# Prefer re-deriving via the orchestrator; fall back to manifest.experiments[].
$experiments = $null
try {
    $orchOut = & python gcp/batch_orchestrator.py --batch-manifest $ManifestPath 2>$null
    if ($LASTEXITCODE -eq 0 -and $orchOut) {
        $orchData = $orchOut | ConvertFrom-Json
        if ($orchData.experiments) { $experiments = $orchData.experiments }
    }
} catch {
    # non-fatal - fall back to the frozen manifest below.
}
if (-not $experiments) {
    Write-Info "  Using manifest.experiments[] (orchestrator re-derivation unavailable)."
    $experiments = $manifest.experiments
}
if (-not $experiments -or @($experiments).Count -eq 0) {
    Write-Fatal "no experiments found (neither orchestrator nor manifest.experiments[] yielded any)."
}
Write-Ok "  Experiment list: $(@($experiments).Count) experiment(s)."

# ============================================================
# STEP 3 - Reconstruct timestamped gcs_prefix (deterministic; STRING-SPLIT, not Get-Date)
# ============================================================
$plainBatchId = $BatchId  # rename happens AFTER post-opt; deploy needs the PLAIN id.
$sweepTs = $BatchId -replace '^batch_','' -replace '_','-'   # e.g. batch_20260711_061128 -> 20260711-061128
Write-Info "  Reconstructed sweep timestamp: $sweepTs"

# Build the expected-experiment table.
$plan = @()
$idx = 0
foreach ($exp in $experiments) {
    $idx++
    $basePrefix = [string]$exp.gcs_prefix
    $tsPrefix   = "$($basePrefix)_$sweepTs"
    $localDir   = Join-Path $ProjectDir "reports\$tsPrefix"
    $plan += [pscustomobject]@{
        Index      = $idx
        Label      = [string]$exp.label
        BasePrefix = $basePrefix
        GcsPrefix  = $tsPrefix
        LocalDir   = $localDir
        VmName     = "optuna-sweep-$($basePrefix -replace '_','-')-$sweepTs"
    }
}

# Cross-check the reconstruction against any existing progress gcs_prefix values.
if ($progress -and $progress.experiments) {
    $storedPrefixes = @($progress.experiments | ForEach-Object { [string]$_.gcs_prefix })
    foreach ($p in $plan) {
        if ($storedPrefixes -contains $p.GcsPrefix) {
            Write-Host "    [ts-check] $($p.GcsPrefix) matches stored progress entry." -ForegroundColor DarkGray
        }
    }
}

# ============================================================
# STEP 4 - Reconcile each experiment against GCS (gcloud storage only)
# ============================================================
Write-Host ""
Write-Info "Reconciling experiments against GCS ($Bucket) ..."

# Index existing progress entries by gcs_prefix for idempotent skip.
$progressByPrefix = @{}
if ($progress -and $progress.experiments) {
    foreach ($e in $progress.experiments) { $progressByPrefix[[string]$e.gcs_prefix] = $e }
}

$recovered = @()   # newly-recoverable entries (to write in NON-DryRun)
foreach ($p in $plan) {
    $existing = $progressByPrefix[$p.GcsPrefix]

    # Idempotent skip: already COMPLETED and locally structurally intact.
    if ($existing -and ([string]$existing.status -eq 'COMPLETED')) {
        $localOk = $false
        if (Test-Path $p.LocalDir) {
            $hasSummary = Test-Path (Join-Path $p.LocalDir 'pipeline_summary.json')
            $hasRegistry = Test-Path (Join-Path $p.LocalDir 'registry')
            $hasZip = @(Get-ChildItem -Path (Join-Path $p.LocalDir 'production') -Filter '*.zip' -ErrorAction SilentlyContinue).Count -gt 0
            if ($hasSummary -and ($hasRegistry -or $hasZip)) { $localOk = $true }
        }
        if ($localOk) {
            Write-Ok "  [SKIP]  $($p.Label) - already COMPLETED and locally intact."
            continue
        }
        Write-Warn "  [RECHK] $($p.Label) - recorded COMPLETED but local dir incomplete; re-probing GCS."
    }

    # NEVER flip an existing DEPLOY_FAILED / TIMEOUT to COMPLETED.
    if ($existing -and (([string]$existing.status -eq 'DEPLOY_FAILED') -or ([string]$existing.status -eq 'TIMEOUT'))) {
        Write-Warn "  [LEAVE] $($p.Label) - status $($existing.status); never re-flipped to COMPLETED. Human re-sweep required."
        continue
    }

    # Probe GCS: production/*.zip AND pipeline_summary.json must BOTH be present.
    $prodLs = gcloud storage ls "$Bucket/$($p.GcsPrefix)/production/" 2>$null
    $hasProdZip = $false
    if ($LASTEXITCODE -eq 0 -and $prodLs) {
        foreach ($line in @($prodLs)) { if ($line -match '\.zip\s*$') { $hasProdZip = $true; break } }
    }
    $summaryLs = gcloud storage ls "$Bucket/$($p.GcsPrefix)/pipeline_summary.json" 2>$null
    $hasSummary = ($LASTEXITCODE -eq 0 -and $summaryLs)

    # Refinement #3: BOTH-or-neither.
    if ($hasProdZip -and $hasSummary) {
        Write-Ok "  [RECOVER] $($p.Label) - production/*.zip + pipeline_summary.json present in GCS."
        if ($DryRun) {
            Write-Host "            WOULD download: $Bucket/$($p.GcsPrefix)/pipeline_summary.json + production/*.zip -> $($p.LocalDir)\ (unzip into registry\)" -ForegroundColor DarkGray
        } else {
            # MUTATION: download + extract into reports\<tsPrefix>\.
            if (-not (Test-Path $p.LocalDir)) { New-Item -ItemType Directory -Path $p.LocalDir -Force | Out-Null }
            gcloud storage cp "$Bucket/$($p.GcsPrefix)/pipeline_summary.json" (Join-Path $p.LocalDir 'pipeline_summary.json') 2>$null
            $prodDir = Join-Path $p.LocalDir 'production'
            if (-not (Test-Path $prodDir)) { New-Item -ItemType Directory -Path $prodDir -Force | Out-Null }
            gcloud storage cp "$Bucket/$($p.GcsPrefix)/production/*.zip" $prodDir 2>$null
            gcloud storage cp "$Bucket/$($p.GcsPrefix)/logs/*" (Join-Path $p.LocalDir 'logs') 2>$null  # best-effort
            $registryDir = Join-Path $p.LocalDir 'registry'
            if (-not (Test-Path $registryDir)) { New-Item -ItemType Directory -Path $registryDir -Force | Out-Null }
            foreach ($zip in @(Get-ChildItem -Path $prodDir -Filter '*.zip' -ErrorAction SilentlyContinue)) {
                Expand-Archive -Path $zip.FullName -DestinationPath $registryDir -Force -ErrorAction SilentlyContinue
            }
            # Verify the dir is intact - crash rather than record a false COMPLETED.
            $okSummary = Test-Path (Join-Path $p.LocalDir 'pipeline_summary.json')
            $okRegistry = (@(Get-ChildItem -Path $registryDir -Recurse -ErrorAction SilentlyContinue).Count -gt 0)
            if (-not ($okSummary -and $okRegistry)) {
                Write-Fatal "extraction left $($p.LocalDir) incomplete (summary=$okSummary registry=$okRegistry) - refusing to record a false COMPLETED."
            }
        }
        $recovered += $p
    }
    elseif ($hasProdZip -xor $hasSummary) {
        # Partial (zip XOR summary) - NOT recoverable.
        Write-Warn "  [PARTIAL] $($p.Label) - only one of {production/*.zip, pipeline_summary.json} present in GCS; NOT recoverable. Report-and-leave for a human."
    }
    else {
        # Genuinely missing - report and leave; NEVER fabricate.
        Write-Warn "  [MISSING] $($p.Label) - no sweep artifacts in GCS ($Bucket/$($p.GcsPrefix)/). Left for a human to re-sweep (not fabricated)."
    }
}

# ============================================================
# STEP 5 - Repair batch_progress.json idempotently (NON-DryRun only)
# ============================================================
# Build a fresh/updated state object mirroring Save-Progress shape.
if (-not $progress) {
    $progress = [pscustomobject]@{
        manifest    = "manifest.json"
        batch_id    = $plainBatchId
        experiments = @()
        total       = @($plan).Count
        completed   = 0
        failed      = 0
        skipped     = 0
    }
}

# Convert experiments to an editable list keyed by gcs_prefix.
$expList = @()
if ($progress.experiments) { $expList = @($progress.experiments) }
$expByPrefix = @{}
foreach ($e in $expList) { $expByPrefix[[string]$e.gcs_prefix] = $e }

# Compose recovered COMPLETED entries with the exact schema + recovered marker.
$newEntries = @()
foreach ($p in $recovered) {
    if ($expByPrefix.ContainsKey($p.GcsPrefix) -and ([string]$expByPrefix[$p.GcsPrefix].status -eq 'COMPLETED')) {
        continue  # already COMPLETED - idempotent, leave as-is.
    }
    $newEntries += [pscustomobject]@{
        index             = $p.Index
        label             = $p.Label
        vm_name           = $p.VmName
        gcs_prefix        = $p.GcsPrefix
        local_dir         = $p.LocalDir
        status            = 'COMPLETED'
        exit_code         = 0
        artifact_verified = $true
        failure_reason    = $null
        recovered         = $true
    }
}

# All backups and progress writes are strictly gated on NON-DryRun (Refinement #4).
if (-not $DryRun) {
    if (@($newEntries).Count -gt 0 -or -not (Test-Path $progressFile)) {
        # Back up FIRST (backup-before-mutate) - only when an original exists.
        if (Test-Path $progressFile) {
            $bakStamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $bakFile = "$progressFile.bak.$bakStamp"
            Copy-Item -Path $progressFile -Destination $bakFile -Force
            Write-Ok "  Backed up existing progress -> $bakFile"
        }
        # Merge: replace existing entry for each recovered prefix, else append.
        $merged = @()
        $mergedPrefixes = @{}
        foreach ($e in $expList) {
            $pref = [string]$e.gcs_prefix
            $replacement = $newEntries | Where-Object { [string]$_.gcs_prefix -eq $pref } | Select-Object -First 1
            if ($replacement) { $merged += $replacement } else { $merged += $e }
            $mergedPrefixes[$pref] = $true
        }
        foreach ($ne in $newEntries) {
            if (-not $mergedPrefixes.ContainsKey([string]$ne.gcs_prefix)) { $merged += $ne }
        }

        $completedCount = @($merged | Where-Object { [string]$_.status -eq 'COMPLETED' }).Count
        $failedCount    = @($merged | Where-Object { (([string]$_.status -eq 'DEPLOY_FAILED') -or ([string]$_.status -eq 'TIMEOUT')) }).Count

        $progress | Add-Member -NotePropertyName experiments  -NotePropertyValue $merged -Force
        $progress | Add-Member -NotePropertyName total        -NotePropertyValue (@($plan).Count) -Force
        $progress | Add-Member -NotePropertyName completed    -NotePropertyValue $completedCount -Force
        $progress | Add-Member -NotePropertyName failed       -NotePropertyValue $failedCount -Force
        $progress | Add-Member -NotePropertyName batch_id     -NotePropertyValue $plainBatchId -Force
        $progress | Add-Member -NotePropertyName recovery_note -NotePropertyValue "Recovered by scripts/resume_batch.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') after the local orchestrator was externally killed mid-run; salvaged completed sweeps from GCS ($Bucket) and repaired batch_progress.json." -Force

        $progress | ConvertTo-Json -Depth 10 | Out-File -FilePath $progressFile -Encoding utf8 -Force
        Write-Ok "  Repaired batch_progress.json ($completedCount COMPLETED / $failedCount failed / $(@($merged).Count) total)."
    } else {
        Write-Info "  batch_progress.json already up to date - no repair needed (idempotent)."
    }
} else {
    Write-Host ""
    Write-Info "  [DryRun] WOULD repair batch_progress.json (zero writes performed):"
    Write-Host "           - back up -> batch_progress.json.bak.<timestamp>" -ForegroundColor DarkGray
    Write-Host "           - add/repair $(@($newEntries).Count) recovered COMPLETED entr(y/ies) (recovered=`$true + top-level recovery_note)" -ForegroundColor DarkGray
    Write-Host "           - recompute completed/failed/total, write via ConvertTo-Json -Depth 10" -ForegroundColor DarkGray
}

# Determine the completed count for downstream sizing (works in both modes).
$completedForSizing = 0
if ($progress -and $progress.experiments) {
    $completedForSizing = @($progress.experiments | Where-Object { [string]$_.status -eq 'COMPLETED' }).Count
}
# In DryRun the progress object hasn't been rewritten; count existing COMPLETED + newly recoverable.
if ($DryRun) {
    $existingCompleted = 0
    if ($progress -and $progress.experiments) {
        $existingCompleted = @($progress.experiments | Where-Object { [string]$_.status -eq 'COMPLETED' }).Count
    }
    $newlyRecoverable = @($recovered | Where-Object {
        -not ($expByPrefix.ContainsKey($_.GcsPrefix) -and ([string]$expByPrefix[$_.GcsPrefix].status -eq 'COMPLETED'))
    }).Count
    $completedForSizing = $existingCompleted + $newlyRecoverable
}

if ($completedForSizing -lt 1) {
    Write-Fatal "zero experiments are COMPLETED after reconcile - nothing to optimize. Re-sweep the missing experiments first."
}
Write-Info "  Completed experiments available for the optimizer: $completedForSizing"

# ============================================================
# STEP 6 - Running-VM guard (targeted; runs in BOTH DryRun and live)
# ============================================================
Write-Host ""
Write-Info "Running-VM guard (targeted filter) ..."
$optVmName = "opt-post-$($plainBatchId.Replace('_','-'))"

$vmRaw = gcloud compute instances list --filter="name~'^(optuna-sweep|opt-post)'" --format="csv[no-heading](name,zone,status)" 2>$null
$blockingVms = @()
if ($vmRaw) {
    foreach ($line in @($vmRaw)) {
        if (-not $line) { continue }
        $parts = $line.Split(",")
        if ($parts.Count -lt 3) { continue }
        $vmN = $parts[0]; $vmZ = $parts[1]; $vmS = $parts[2]
        # Locally keep only VMs carrying THIS batch's sweep ts (sweep VMs) or the opt VM name.
        $belongs = ($vmN -like "*$sweepTs*") -or ($vmN -eq $optVmName)
        if ($belongs -and ($vmS -eq 'RUNNING')) {
            $blockingVms += [pscustomobject]@{ Name = $vmN; Zone = $vmZ; Status = $vmS }
        }
    }
}

if (@($blockingVms).Count -gt 0) {
    foreach ($v in $blockingVms) { Write-Warn "  RUNNING VM for this batch: $($v.Name) [$($v.Zone)] status=$($v.Status)" }
    if ($DryRun) {
        Write-Warn "  [DryRun] A LIVE run would be BLOCKED by the RUNNING VM(s) above (another process may own an in-flight recovery/optimizer). Pass -Force to override."
    } elseif ($Force) {
        Write-Warn "  -Force set - proceeding despite RUNNING VM(s). (Never broad-kill; targeted ops only.)"
    } else {
        Write-Fatal "RUNNING VM(s) for this batch - refusing to proceed (protects an in-flight recovery/optimizer). Re-run with -Force to override."
    }
} else {
    Write-Ok "  No RUNNING VMs for this batch's timestamp - clear to proceed."
}

# ============================================================
# STEP 7 - Post-optimizer (only if outputs absent; NON-DryRun deploys, DryRun reports)
# ============================================================
Write-Host ""
Write-Info "Post-optimizer stage ..."

# armCount from -Objective (comma-split; both->2; min 1) - NOT in the manifest (Refinement #2).
$armCount = 0
foreach ($arm in $Objective.Split(',')) {
    $arm = $arm.Trim()
    if (-not $arm) { continue }
    if ($arm -eq 'both') { $armCount += 2 } else { $armCount += 1 }
}
if ($armCount -lt 1) { $armCount = 1 }

$optTaskCount = $completedForSizing * 4 * $armCount
$optMachineType = if ($optTaskCount -le 8) { "n2-standard-8" }
                  elseif ($optTaskCount -le 16) { "n2-standard-16" }
                  elseif ($optTaskCount -le 32) { "n2-standard-32" }
                  else { "n2-standard-48" }
Write-Info "  Optimizer sizing: $completedForSizing completed x 4 x $armCount arm(s) [$Objective] = $optTaskCount tasks -> $optMachineType"

# Detect existing post-opt outputs (skip / just-download rather than re-deploy).
$localOptSummary = @(Get-ChildItem -Path $BatchDir -Filter 'batch_summary_optimized_*.md' -ErrorAction SilentlyContinue)
$postOptDone = $false
if (@($localOptSummary).Count -gt 0) {
    Write-Ok "  Post-opt outputs already present locally ($(@($localOptSummary).Count) batch_summary_optimized_*.md) - skipping optimizer."
    $postOptDone = $true
} else {
    $gcsOptLs = gcloud storage ls "$Bucket/batch_optimizer/$plainBatchId/batch_summary_optimized_*.md" 2>$null
    $gcsHasOpt = ($LASTEXITCODE -eq 0 -and $gcsOptLs)
    if ($gcsHasOpt) {
        if ($DryRun) {
            Write-Warn "  [DryRun] Post-opt outputs exist in GCS ($Bucket/batch_optimizer/$plainBatchId/) - WOULD download them (no VM deploy)."
            $postOptDone = $true
        } else {
            Write-Ok "  Post-opt outputs exist in GCS - downloading (no VM deploy)."
            gcloud storage cp "$Bucket/batch_optimizer/$plainBatchId/*.md" $BatchDir 2>$null
            gcloud storage cp "$Bucket/batch_optimizer/$plainBatchId/*.json" $BatchDir 2>$null
            $postOptDone = $true
        }
    }
}

if (-not $postOptDone) {
    # Fallback zone (first of the -Zone list).
    $fallbackZone = ($Zone -split ',')[0].Trim()

    if ($DryRun) {
        $slipEcho = if ($slippage -gt 0) { " -SlippagePerSide $slippage" } else { "" }
        $ensEcho  = if ($runEnsembleOpt) { " -EnsembleOptimization" } else { "" }
        $tgEcho   = if ($DisableTelegram) { " -DisableTelegram" } else { "" }
        Write-Host ""
        Write-Info "  [DryRun] WOULD deploy the post-optimizer (PLAIN id $plainBatchId; rename is AFTER post-opt):"
        Write-Host "    gcp_deploy_optimizer.ps1 -BatchId $plainBatchId -NoMonitor" -ForegroundColor DarkGray
        Write-Host "        -NTrials $postOptTrials -EnsembleTrials $postOptEnsTrials -PairTopN $pairTopN -HoldoutMonths $postOptHoldout -MachineType $optMachineType -Workers 0" -ForegroundColor DarkGray
        Write-Host "        -Zone $fallbackZone -SweepMode $SweepMode -OptMode $optMode -Objective $Objective" -ForegroundColor DarkGray
        Write-Host "        -NBlocks $NBlocks -LambdaDispersion $LambdaDispersion -MinBlockMonths $MinBlockMonths" -ForegroundColor DarkGray
        Write-Host "        -MaxRunDurationMinutes $OptimizerMaxRunDurationMinutes -ExecData $execData$slipEcho$ensEcho$tgEcho" -ForegroundColor DarkGray
        Write-Host "    then self-poll (via gcloud storage) for batch_summary_optimized_*.md, download results, targeted-delete $optVmName." -ForegroundColor DarkGray
    } else {
        # Build the deploy arg array (PLAIN $BatchId; -NoMonitor mandatory - built-in poll uses the banned bucket CLI).
        $optArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ".\gcp\gcp_deploy_optimizer.ps1",
            "-BatchId", $plainBatchId,
            "-NoMonitor",
            "-NTrials", $postOptTrials,
            "-EnsembleTrials", $postOptEnsTrials,
            "-PairTopN", $pairTopN,
            "-HoldoutMonths", $postOptHoldout,
            "-MachineType", $optMachineType,
            "-Workers", 0,
            "-Zone", $fallbackZone,
            "-SweepMode", $SweepMode,
            "-OptMode", $optMode,
            "-Objective", $Objective,
            "-NBlocks", $NBlocks,
            "-LambdaDispersion", $LambdaDispersion,
            "-MinBlockMonths", $MinBlockMonths,
            "-MaxRunDurationMinutes", $OptimizerMaxRunDurationMinutes,
            "-ExecData", $execData)
        if ($slippage -gt 0)   { $optArgs += @("-SlippagePerSide", $slippage) }
        if ($runEnsembleOpt)   { $optArgs += "-EnsembleOptimization" }
        if ($DisableTelegram)  { $optArgs += "-DisableTelegram" }

        Write-Info "  Deploying post-optimizer VM ($optVmName) in zone $fallbackZone ..."
        & powershell @optArgs
        $optDeployExit = $LASTEXITCODE
        if ($optDeployExit -ne 0) {
            Write-Fatal "post-optimizer deploy failed (exit $optDeployExit)."
        }

        # Self-poll (via gcloud storage only): wait for the landed batch_summary_optimized_*.md OR the VM to terminate.
        $waitCapMin = [math]::Max($OptimizerMaxRunDurationMinutes, 360)
        $deadline = (Get-Date).AddMinutes($waitCapMin)
        Write-Info "  Self-polling for post-opt completion (cap ${waitCapMin}m; gcloud storage + gcloud compute instances describe) ..."
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 60
            $landed = gcloud storage ls "$Bucket/batch_optimizer/$plainBatchId/batch_summary_optimized_*.md" 2>$null
            if ($LASTEXITCODE -eq 0 -and $landed) {
                Write-Ok "  Post-opt report landed in GCS."
                $postOptDone = $true
                break
            }
            $vmStatus = gcloud compute instances describe $optVmName --zone=$fallbackZone --format="get(status)" 2>$null
            if ([string]::IsNullOrWhiteSpace([string]$vmStatus) -or ($vmStatus -match 'TERMINATED|STOPPED')) {
                # VM gone/stopped - one final report check before giving up.
                $landed2 = gcloud storage ls "$Bucket/batch_optimizer/$plainBatchId/batch_summary_optimized_*.md" 2>$null
                if ($LASTEXITCODE -eq 0 -and $landed2) {
                    Write-Ok "  Post-opt report landed (VM lifecycle ended)."
                    $postOptDone = $true
                }
                break
            }
        }

        if ($postOptDone) {
            # Download the optimizer outputs.
            Write-Info "  Downloading post-opt outputs from $Bucket/batch_optimizer/$plainBatchId/ ..."
            gcloud storage cp "$Bucket/batch_optimizer/$plainBatchId/*.md" $BatchDir 2>$null
            gcloud storage cp "$Bucket/batch_optimizer/$plainBatchId/*.json" $BatchDir 2>$null
            $dstCfg = Join-Path $BatchDir 'batch_configs'
            if (-not (Test-Path $dstCfg)) { New-Item -ItemType Directory -Path $dstCfg -Force | Out-Null }
            gcloud storage cp --recursive "$Bucket/batch_optimizer/$plainBatchId/batch_configs/*" $dstCfg 2>$null
            $dstPred = Join-Path $BatchDir 'predictions'
            if (-not (Test-Path $dstPred)) { New-Item -ItemType Directory -Path $dstPred -Force | Out-Null }
            gcloud storage cp --recursive "$Bucket/batch_optimizer/$plainBatchId/predictions/*" $dstPred 2>$null

            # Targeted-delete the opt VM (idempotent; it self-deletes on --shutdown, so ignore "already gone").
            Write-Info "  Targeted cleanup of $optVmName (ignore already-gone) ..."
            gcloud compute instances delete $optVmName --zone=$fallbackZone --quiet 2>$null
        } else {
            Write-Warn "  Post-opt did not complete within the ${waitCapMin}m cap. Leaving the VM to its --max-run-duration TTL (never force-kill inside its TTL). Re-run resume_batch to collect once it lands."
        }
    }
}

# ============================================================
# STEP 8 - Finalize: auto-rename AFTER post-opt (NON-DryRun; DryRun reports)
# ============================================================
Write-Host ""
Write-Info "Finalize stage ..."

# TIER from manifest CONTENT (the resume path has no manifest filename): match
# (canary|scout|prod) in training_workflow.gcs_base_dir or any experiment gcs_prefix.
function Get-BatchTier {
    param($Manifest, $Plan)
    $tier = "RUN"
    $base = [string]$Manifest.baseline.training_workflow.gcs_base_dir
    if ($base -match '(canary|scout|prod)') { return $Matches[1].ToUpper() }
    foreach ($p in $Plan) {
        if ([string]$p.BasePrefix -match '(canary|scout|prod)') { return $Matches[1].ToUpper() }
    }
    return $tier
}

$stampTier = Get-BatchTier -Manifest $manifest -Plan $plan
$stampSuffix = if ($armCount -gt 1) { "_OBJAB" } else { "" }
# Dataset tag in the stamp — mirrors run_sweep_batch.ps1 (operator request
# 2026-07-12): batch_<ts>_<SYM>_<DATASET>_<TIER>, e.g. ..._NG_02C_CANARY.
$stampDataset = ""
try {
    $dsVer = [string]$manifest.baseline.data_workflow.dataset_version
    if (-not [string]::IsNullOrWhiteSpace($dsVer)) {
        $stampDataset = ($dsVer -replace '^(?i)HourSet_?', '').Trim().ToUpper()
    }
} catch {}
$stampName = if ($stampDataset) {
    "${plainBatchId}_$($symbol.Trim().ToUpper())_${stampDataset}_${stampTier}${stampSuffix}"
} else {
    "${plainBatchId}_$($symbol.Trim().ToUpper())_${stampTier}${stampSuffix}"
}

# Only rename if post-opt outputs exist AND the dir is still the plain batch_<ts> name.
$dirLeaf = Split-Path -Leaf $BatchDir
$isPlainNamed = ($dirLeaf -eq $plainBatchId)

if (-not $postOptDone) {
    Write-Warn "  Post-opt outputs not present yet - skipping finalize rename (nothing to stamp)."
} elseif (-not $isPlainNamed) {
    Write-Info "  Batch dir already stamped ($dirLeaf) - finalize rename skipped (idempotent)."
} elseif ($DryRun) {
    Write-Info "  [DryRun] WOULD rename reports\batch_runs\$plainBatchId -> reports\batch_runs\$stampName, then rewrite embedded batch_runs/$plainBatchId/ -> batch_runs/$stampName/ in batch_configs\*.json (BOM-less UTF-8; model_path untouched)."
} else {
    # A failed rename only WARNS (never fails the run).
    try {
        $stampFullPath = Join-Path $batchRunsRoot $stampName
        Rename-Item -Path $BatchDir -NewName $stampName -ErrorAction Stop
        Write-Ok "  Batch folder stamped: reports\batch_runs\$stampName"
        $BatchDir = $stampFullPath

        # CRITICAL CROSS-LINK: rewrite embedded batch_runs/<id>/ -> batch_runs/<stampName>/ in the
        # recovered config JSONs (reuse run_sweep_batch.ps1:1267-1288 exactly). model_path is
        # sweep-rooted so the batch_runs/ replace never matches it - it stays byte-identical.
        # Target the REAL config dir that exists in a batch run: batch_configs\ (NOT configs\).
        $stampedCfgDir = Join-Path $BatchDir "batch_configs"
        if (Test-Path $stampedCfgDir) {
            $cfgPatched = 0
            foreach ($cfgFile in Get-ChildItem "$stampedCfgDir\*.json" -ErrorAction SilentlyContinue) {
                $cfgRaw = [System.IO.File]::ReadAllText($cfgFile.FullName)
                $cfgNew = $cfgRaw.Replace("batch_runs/$plainBatchId/", "batch_runs/$stampName/")
                if ($cfgNew -ne $cfgRaw) {
                    [System.IO.File]::WriteAllText($cfgFile.FullName, $cfgNew, [System.Text.UTF8Encoding]::new($false))
                    $cfgPatched++
                }
            }
            if ($cfgPatched -gt 0) {
                Write-Ok "  Rewrote embedded batch-dir paths in $cfgPatched config(s) to the stamped name."
            } else {
                Write-Warn "  WARNING: 0 config(s) matched - no 'batch_runs/$plainBatchId/' segment found in $stampedCfgDir; predictions_path may be stale."
            }
        } else {
            Write-Warn "  WARNING: stamped batch_configs dir NOT found: $stampedCfgDir - embedded predictions_path values were NOT rewritten."
        }
    } catch {
        Write-Warn "  WARNING: batch folder finalize rename failed ($_) - folder remains: $BatchDir"
    }
}

# Re-list with the targeted filter; report any leftover VMs for this batch.
Write-Host ""
Write-Info "Leftover-VM check (targeted filter) ..."
$vmRaw2 = gcloud compute instances list --filter="name~'^(optuna-sweep|opt-post)'" --format="csv[no-heading](name,zone,status)" 2>$null
$leftover = @()
if ($vmRaw2) {
    foreach ($line in @($vmRaw2)) {
        if (-not $line) { continue }
        $parts = $line.Split(",")
        if ($parts.Count -lt 3) { continue }
        $vmN = $parts[0]
        if (($vmN -like "*$sweepTs*") -or ($vmN -eq $optVmName)) { $leftover += $line }
    }
}
if (@($leftover).Count -gt 0) {
    Write-Warn "  Leftover VM(s) for this batch:"
    foreach ($l in $leftover) { Write-Warn "    $l" }
    Write-Warn "  Reconcile with scripts/reap_orphan_vms.ps1 (report-only; add -Delete to remove)."
} else {
    Write-Ok "  No leftover VMs for this batch."
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " RESUME $(if ($DryRun) { 'DRY-RUN PLAN COMPLETE' } else { 'COMPLETE' })" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
exit 0
