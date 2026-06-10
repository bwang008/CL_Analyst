<#
.SYNOPSIS
    Quota-aware batch orchestrator for canary Optuna experiments.
.DESCRIPTION
    Reads a JSON manifest and runs N experiments, firing up to 2 concurrent
    VMs while respecting the GCP vCPU quota cap (default: 100 vCPUs).
    Each experiment gets:
      - A fresh VM (deleted and recreated between runs for clean state)
      - A timestamped GCS prefix and local output directory (no overwrites)
      - An artifact verification gate before VM deletion
      - Telegram notifications at key milestones (start/complete/fail)
    Results are tracked in batch_progress.json. Run collect_batch_results.ps1
    after the batch to generate a consolidated comparison report.
.PARAMETER ManifestPath
    Path to the JSON manifest defining the experiments to run.
    Default: configs\canary_batch_manifest.json
.PARAMETER DryRun
    Validate manifest and print what would be deployed, but do NOT create VMs.
    Also sends a Telegram test message to verify credentials.
.PARAMETER EnableTelegram
    Send Telegram notifications. Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
    from the .env file in the project root.
.PARAMETER MaxConcurrentVcpus
    Override the vCPU quota cap from the manifest defaults.
.EXAMPLE
    .\gcp\run_canary_batch.ps1
    .\gcp\run_canary_batch.ps1 -ManifestPath configs\my_targets.json
    .\gcp\run_canary_batch.ps1 -DryRun -EnableTelegram
#>

param(
    [string]$ManifestPath        = "configs\sweep_batch_manifest.json",
    [string]$Zone                = "us-central1-a",
    [switch]$DryRun,
    [switch]$DisableTelegram,
    [int]$MaxConcurrentVcpus    = 0,   # 0 = read from manifest defaults
    [string]$SweepMode          = "backtest"  # 'frictionless' for Workflow C, 'backtest' for legacy
)

$ErrorActionPreference = "Continue"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

# Add gcloud to PATH
$gcloudBin = "C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin"
if ($env:PATH -notlike "*$gcloudBin*") { $env:PATH = "$gcloudBin;$env:PATH" }

$BatchTimestamp = Get-Date -Format "yyyyMMdd_HHmm"
$BatchId        = "batch_$BatchTimestamp"
$BatchDir       = Join-Path $ProjectDir "reports\batch_runs\$BatchId"
$ProgressFile   = Join-Path $BatchDir "batch_progress.json"

# ============================================================
# HELPERS
# ============================================================

function Read-DotEnv {
    $result = @{}
    $envPath = Join-Path $ProjectDir ".env"
    if (Test-Path $envPath) {
        Get-Content $envPath | ForEach-Object {
            if ($_ -match '^([A-Z_][A-Z0-9_]*)=(.*)$') {
                $result[$Matches[1]] = $Matches[2].Trim()
            }
        }
    }
    return $result
}


function Send-BatchTelegram {
    param([string]$Message)
    if ($DisableTelegram) { return }

    $ev      = Read-DotEnv
    $token   = if ($ev["TELEGRAM_BOT_TOKEN"]) { $ev["TELEGRAM_BOT_TOKEN"] } else { $env:TELEGRAM_BOT_TOKEN }
    $chatId  = if ($ev["TELEGRAM_CHAT_ID"])   { $ev["TELEGRAM_CHAT_ID"]   } else { $env:TELEGRAM_CHAT_ID   }
    if (-not $token -or $token -eq "") {
        Write-Host "  [Telegram] Not configured." -ForegroundColor Gray; return
    }

    $ts      = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $fullMsg = "${ts}`n[Batch: $BatchId]`n`n${Message}"
    # Strip Markdown formatting to avoid Telegram parse_mode errors
    $plainMsg = $fullMsg -replace '\*', '' -replace '``', '' -replace '_', ''
    $body    = @{ chat_id = $chatId; text = $plainMsg } | ConvertTo-Json -Compress -Depth 3
    $bytes   = [System.Text.Encoding]::UTF8.GetBytes($body)

    try {
        Invoke-RestMethod -Method Post `
            -Uri "https://api.telegram.org/bot${token}/sendMessage" `
            -ContentType "application/json; charset=utf-8" `
            -Body $bytes -TimeoutSec 8 -ErrorAction Stop | Out-Null
        Write-Host "  [Telegram] Batch notification sent." -ForegroundColor DarkCyan
    } catch {
        Write-Host "  [Telegram] Batch send failed: $_" -ForegroundColor Yellow
    }
}


function Get-UsedVcpus {
    <# Count vCPUs consumed by all RUNNING VMs in the zone #>
    try {
        $machineTypes = gcloud compute instances list `
            --filter="status:RUNNING" `
            --format="value(machineType.basename())" 2>$null
        $total = 0
        foreach ($mt in $machineTypes) {
            if ($mt -match '-(\d+)$') { $total += [int]$Matches[1] }
        }
        return $total
    } catch {
        return 0
    }
}


function Write-WallClockSummary {
    <# Generate a wall_clock_summary.md in the batch directory with per-phase timing. #>
    param(
        [hashtable]$BatchState,
        [string]$BatchDir,
        [string]$SweepMachineType,
        [int]$SweepVcpus,
        [string]$OptMachineType,
        [int]$OptElapsedMin,
        [int]$OptTrials,
        [int]$OptWorkers
    )

    $summaryPath = Join-Path $BatchDir "wall_clock_summary.md"
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

    # Calculate total sweep phase duration from batch state timestamps
    $batchStart = $BatchState["started_at"]
    $batchEnd   = $BatchState["completed_at"]
    $sweepDurationMin = 0
    if ($batchStart -and $batchEnd) {
        try {
            $startDt = [datetime]::ParseExact($batchStart, "yyyy-MM-dd HH:mm:ss", $null)
            $endDt   = [datetime]::ParseExact($batchEnd, "yyyy-MM-dd HH:mm:ss", $null)
            $sweepDurationMin = [math]::Round(($endDt - $startDt).TotalMinutes, 1)
        } catch {}
    }

    # E2E total = sweep phase + optimizer elapsed
    $e2eTotalMin = [math]::Round($sweepDurationMin + $OptElapsedMin, 1)

    # Pre-compute detail strings
    $sweepDetail = "$($BatchState.total) experiments ($($BatchState.completed) completed, $($BatchState.failed) failed)"
    $optDetail   = "${OptTrials} trials/target, ${OptWorkers} workers"
    $optVcpus    = if ($OptMachineType -match '-(\d+)$') { $Matches[1] } else { 'N/A' }

    # Pre-pad cell values for aligned columns
    $sweepDurPad   = ('{0,-16}' -f "${sweepDurationMin} min")
    $optDurPad     = ('{0,-16}' -f "~${OptElapsedMin} min")
    $e2eDurPad     = ('{0,-16}' -f "**~${e2eTotalMin} min**")
    $sweepDetPad   = ('{0,-72}' -f $sweepDetail)
    $optDetPad     = ('{0,-72}' -f $optDetail)
    $e2eDetPad     = ('{0,-72}' -f 'Start to final report downloaded')
    $startPad      = ('{0,-19}' -f $batchStart)
    $endPad        = ('{0,-19}' -f $batchEnd)
    $tsPad         = ('{0,-19}' -f $ts)
    $sweepMtPad    = ('{0,-18}' -f "``${SweepMachineType}``")
    $optMtPad      = ('{0,-18}' -f "``${OptMachineType}``")
    $sweepVcPad    = ('{0,-5}' -f $SweepVcpus)
    $optVcPad      = ('{0,-5}' -f $optVcpus)

    # --- Build markdown with aligned columns ---
    $lines = @()
    $lines += "# Wall Clock Summary - $($BatchState.batch_id)"
    $lines += ''
    $lines += "Generated: $ts"
    $lines += "Batch ID: ``$($BatchState.batch_id)``"
    $lines += ''
    $lines += '---'
    $lines += ''
    $lines += '## End-to-End Timing'
    $lines += ''
    $lines += '| Phase              | Duration         | Details                                                                  |'
    $lines += '| ------------------ | ---------------- | ------------------------------------------------------------------------ |'
    $lines += "| **Sweep Phase**    | $sweepDurPad | $sweepDetPad |"
    $lines += "| **Post-Optimizer** | $optDurPad | $optDetPad |"
    $lines += "| **E2E Total**      | $e2eDurPad | $e2eDetPad |"
    $lines += ''
    $lines += '## Batch Timeline'
    $lines += ''
    $lines += '| Event              | Timestamp           |'
    $lines += '| ------------------ | ------------------- |'
    $lines += "| Batch Started      | $startPad |"
    $lines += "| Sweeps Completed   | $endPad |"
    $lines += "| E2E Completed      | $tsPad |"
    $lines += ''
    $lines += '## Per-Experiment Sweep Times'
    $lines += ''
    $lines += '| Experiment         | Status       | Wall (min) |'
    $lines += '| ------------------ | ------------ | ---------- |'
    foreach ($exp in $BatchState.experiments) {
        $wt = if ($exp.wall_time_min) { '{0:N1}' -f $exp.wall_time_min } else { 'N/A' }
        $labelPad  = ('{0,-18}' -f $exp.label)
        $statusPad = ('{0,-12}' -f $exp.status)
        $wtPad     = ('{0,-10}' -f $wt)
        $lines += "| $labelPad | $statusPad | $wtPad |"
    }
    $lines += ''
    $lines += '## Infrastructure'
    $lines += ''
    $lines += '| Component          | Machine Type       | vCPUs |'
    $lines += '| ------------------ | ------------------ | ----- |'
    $lines += "| Sweep VMs          | $sweepMtPad | $sweepVcPad |"
    $lines += "| Post-Optimizer VM  | $optMtPad | $optVcPad |"
    $lines += ''

    $md = $lines -join "`r`n"
    $md | Out-File -FilePath $summaryPath -Encoding utf8 -Force
    Write-Host "  Wall clock summary: $summaryPath" -ForegroundColor Green
}


function Save-Progress {
    param([hashtable]$BatchState)
    if (-not (Test-Path $BatchDir)) { New-Item -ItemType Directory -Path $BatchDir -Force | Out-Null }
    $BatchState | ConvertTo-Json -Depth 10 | Out-File -FilePath $ProgressFile -Encoding utf8 -Force
}


function Test-ArtifactsDownloaded {
    <#
    Artifact verification gate - runs BEFORE deleting the VM.
    Returns $true only when the minimum expected artifacts are on disk.
    #>
    param(
        [string]$LocalDir,
        [string]$GcsBase,
        [int]$MaxRetries = 3
    )

    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        $summaryOk = Test-Path (Join-Path $LocalDir "pipeline_summary.json")
        $logsOk    = ((Get-ChildItem (Join-Path $LocalDir "logs") -ErrorAction SilentlyContinue) | Measure-Object).Count -gt 0
        $modelsOk  = ((Get-ChildItem $LocalDir -Filter "*.pkl" -Recurse -ErrorAction SilentlyContinue) | Measure-Object).Count -gt 0

        if ($summaryOk -and ($logsOk -or $modelsOk)) {
            Write-Host "  [ArtifactGate] PASSED (attempt $attempt)" -ForegroundColor Green
            return $true
        }

        Write-Host "  [ArtifactGate] Attempt $attempt/$MaxRetries - missing artifacts:" -ForegroundColor Yellow
        if (-not $summaryOk) { Write-Host "    - pipeline_summary.json" -ForegroundColor Yellow }
        if (-not $logsOk)    { Write-Host "    - logs/ directory empty or missing" -ForegroundColor Yellow }
        if (-not $modelsOk)  { Write-Host "    - no *.pkl models found" -ForegroundColor Yellow }

        if ($attempt -lt $MaxRetries) {
            Write-Host "  [ArtifactGate] Re-downloading from GCS: $GcsBase" -ForegroundColor Yellow
            # Re-run partial downloads
            $logsDir    = Join-Path $LocalDir "logs"
            $reportsDir = Join-Path $LocalDir "reports"
            if (-not (Test-Path $logsDir))    { New-Item -ItemType Directory -Path $logsDir    -Force | Out-Null }
            if (-not (Test-Path $reportsDir)) { New-Item -ItemType Directory -Path $reportsDir -Force | Out-Null }
            gcloud storage cp -r "$GcsBase/logs/*"    $logsDir    2>$null
            gcloud storage cp -r "$GcsBase/reports/*" $reportsDir 2>$null
            gcloud storage cp    "$GcsBase/STATUS.json" $LocalDir  2>$null
            gcloud storage cp    "$GcsBase/pipeline_summary.json" $LocalDir 2>$null
            $tmpFile = [System.IO.Path]::GetTempFileName()
            gcloud storage cp "$GcsBase/logs/*" $tmpFile 2>$null
            Start-Sleep -Seconds 30
        }
    }

    Write-Host "  [ArtifactGate] FAILED after $MaxRetries attempts." -ForegroundColor Red
    return $false
}


function Save-CrashDiagnostics {
    <# Preserve serial console + startup logs before VM deletion so crash evidence survives. #>
    param(
        [string]$VmName,
        [string]$VmZone,
        [string]$GcsPrefix,
        [string]$LocalDir
    )

    $diagDir = Join-Path $LocalDir "crash_diagnostics"
    if (-not (Test-Path $diagDir)) { New-Item -ItemType Directory -Path $diagDir -Force | Out-Null }

    Write-Host "  [CrashDiag] Capturing pre-deletion diagnostics..." -ForegroundColor Yellow

    # 1. Serial console output (survives VM crash, lost after VM deletion)
    $serialFile = Join-Path $diagDir "serial_console.log"
    try {
        $serialOut = gcloud compute instances get-serial-port-output $VmName --zone=$VmZone 2>$null
        if ($serialOut) { $serialOut | Out-File -FilePath $serialFile -Encoding utf8 }
    } catch {}

    # 2. If VM is still accessible, grab startup script logs and dmesg
    $vmStatus = gcloud compute instances describe $VmName --zone=$VmZone --format="get(status)" 2>$null
    if ($vmStatus -and $vmStatus.ToString().Trim() -in @("RUNNING", "STOPPED")) {
        try {
            $startupFile = Join-Path $diagDir "startup_script.log"
            $startupOut = gcloud compute ssh $VmName --zone=$VmZone `
                --command="sudo journalctl -u google-startup-scripts.service --no-pager 2>/dev/null || cat /var/log/syslog 2>/dev/null | grep startup-script | tail -100" `
                --quiet 2>$null
            if ($startupOut) { $startupOut | Out-File -FilePath $startupFile -Encoding utf8 }
        } catch {}

        try {
            $dmesgFile = Join-Path $diagDir "dmesg.log"
            $dmesgOut = gcloud compute ssh $VmName --zone=$VmZone `
                --command="dmesg | tail -100" --quiet 2>$null
            if ($dmesgOut) { $dmesgOut | Out-File -FilePath $dmesgFile -Encoding utf8 }
        } catch {}
    }

    # 3. Upload to GCS for permanent record
    $gcsDiag = "gs://cltrainer-optuna-results/$GcsPrefix/crash_diagnostics/"
    gcloud storage cp -r "$diagDir/*" $gcsDiag 2>$null

    Write-Host "  [CrashDiag] Saved to $diagDir and $gcsDiag" -ForegroundColor Yellow
}


function Remove-ExperimentVm {
    param(
        [string]$VmName,
        [string]$VmZone = $Zone
    )
    Write-Host "  Deleting VM: $VmName in zone $VmZone ..." -ForegroundColor Yellow
    if (-not $DryRun) {
        gcloud compute instances delete $VmName --zone=$VmZone --quiet 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  VM deleted." -ForegroundColor Green
        } else {
            Write-Host "  VM delete returned non-zero (may already be gone)." -ForegroundColor Gray
        }
    } else {
        Write-Host "  [DryRun] Would delete VM: $VmName in zone $VmZone" -ForegroundColor Cyan
    }
}


# ============================================================
# LOAD MANIFEST
# ============================================================

$manifestFull = Join-Path $ProjectDir $ManifestPath
if (-not (Test-Path $manifestFull)) {
    Write-Host "ERROR: Manifest not found: $manifestFull" -ForegroundColor Red
    exit 1
}

$manifest  = Get-Content $manifestFull -Raw | ConvertFrom-Json
$defaults  = $manifest.defaults
$expList   = $manifest.experiments

# Apply MaxConcurrentVcpus override or read from manifest
$maxVcpus    = if ($MaxConcurrentVcpus -gt 0) { $MaxConcurrentVcpus } `
               elseif ($defaults.max_concurrent_vcpus) { [int]$defaults.max_concurrent_vcpus } `
               else { 100 }
$vcpusPerVm  = if ($defaults.vcpus_per_vm) { [int]$defaults.vcpus_per_vm } else { 48 }
$maxVms      = if ($defaults.max_concurrent_vms) { [int]$defaults.max_concurrent_vms } else { 0 }  # 0 = no VM count cap, use vCPU cap only
$timeoutMins = if ($defaults.timeout_minutes) { [int]$defaults.timeout_minutes } else { 90 }
$postOptTrials  = if ($defaults.post_optimizer_trials) { [int]$defaults.post_optimizer_trials } else { 1000 }
$postOptHoldout = if ($defaults.post_optimizer_holdout_months) { [int]$defaults.post_optimizer_holdout_months } else { 4 }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host " CANARY BATCH ORCHESTRATOR" -ForegroundColor Magenta
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host "  Batch ID:      $BatchId"
Write-Host "  Manifest:      $manifestFull"
Write-Host "  Experiments:   $($expList.Count)"
Write-Host "  Max vCPUs:     $maxVcpus  (allows $([math]::Floor($maxVcpus / $vcpusPerVm)) concurrent VMs)"
$maxVmsDisplay = if ($maxVms -gt 0) { "$maxVms (IP/VM cap)" } else { 'uncapped (vCPU-only gating)' }
Write-Host "  Max VMs:       $maxVmsDisplay"
Write-Host "  vCPU/VM:       $vcpusPerVm"
Write-Host "  Timeout/exp:   ${timeoutMins}m"
$tgStr = if ($DisableTelegram) { $false } else { $true }
Write-Host "  Dry Run:       $DryRun"
Write-Host "  Telegram:      $tgStr"
Write-Host "  Output Dir:    $BatchDir"
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host ""

# DryRun: validate and exit
if ($DryRun) {
    Write-Host "=== DRY RUN - No VMs will be created ===" -ForegroundColor Cyan
    $idx = 0
    foreach ($exp in $expList) {
        $idx++
        $label        = if ($exp.label) { $exp.label } else { "Exp-$idx" }
        $basePrefix   = $exp.gcs_prefix
        $vmName       = "optuna-sweep-$($basePrefix -replace '_','-')"
        $tsPrefix     = "${basePrefix}_${BatchTimestamp}"
        $machineType  = if ($exp.machine_type)       { $exp.machine_type }       else { $defaults.machine_type }
        $targetLong   = if ($exp.target_long)        { $exp.target_long }        else { "" }
        $targetShort  = if ($exp.target_short)       { $exp.target_short }       else { "" }
        Write-Host "  [$idx/$($expList.Count)] $label" -ForegroundColor Cyan
        Write-Host "    VM:          $vmName"
        Write-Host "    GCS prefix:  $tsPrefix"
        Write-Host "    Machine:     $machineType"
        Write-Host "    Target L:    $targetLong"
        Write-Host "    Target S:    $targetShort"
        Write-Host "    Local out:   reports\$tsPrefix"
    }
    Write-Host ""
    Send-BatchTelegram "[DRY-RUN] *Dry Run Validated*`n$($expList.Count) experiments in manifest.`n_No VMs were created._"
    Write-Host "Dry run complete." -ForegroundColor Green
    exit 0
}

# ============================================================
# INITIALISE BATCH STATE
# ============================================================

if (-not (Test-Path $BatchDir)) { New-Item -ItemType Directory -Path $BatchDir -Force | Out-Null }

# Freeze manifest in the batch directory
$savedManifestPath = Join-Path $BatchDir "manifest.json"
Copy-Item $ManifestPath $savedManifestPath -Force

$batchState = @{
    batch_id    = $BatchId
    manifest    = "manifest.json"
    started_at  = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    total       = $expList.Count
    completed   = 0
    failed      = 0
    skipped     = 0
    experiments = @()
}
Save-Progress $batchState

Send-BatchTelegram ("[STARTING] *Batch Started*`n" +
    "Experiments: $($expList.Count)`n" +
    "Max concurrent: $([math]::Floor($maxVcpus / $vcpusPerVm)) VMs`n" +
    "Timeout per experiment: ${timeoutMins}min")

# ============================================================
# BUILD EXPERIMENT QUEUE
# ============================================================

$queue       = [System.Collections.Queue]::new()
$expIndex    = 0
foreach ($exp in $expList) {
    $expIndex++
    $basePrefix  = $exp.gcs_prefix
    $tsPrefix    = "${basePrefix}_${BatchTimestamp}"
    $vmName      = "optuna-sweep-$($basePrefix -replace '_','-')"
    $localDir    = Join-Path $ProjectDir "reports\$tsPrefix"
    $label       = if ($exp.label) { $exp.label } else { "Exp-$expIndex" }
    $queue.Enqueue(@{
        Index        = $expIndex
        Label        = $label
        VmName       = $vmName
        BasePrefix   = $basePrefix
        GcsPrefix    = $tsPrefix
        LocalDir     = $localDir
        MachineType  = if ($exp.machine_type)       { $exp.machine_type }       else { $defaults.machine_type       }
        Provisioning = if ($exp.provisioning_model)  { $exp.provisioning_model  } else { $defaults.provisioning_model }
        GcsDataPath  = if ($exp.gcs_data_path)       { $exp.gcs_data_path       } else { $defaults.gcs_data_path      }
        StrategyConf = if ($exp.strategy_config)     { $exp.strategy_config     } else { $defaults.strategy_config    }
        Metrics      = if ($exp.metrics)             { $exp.metrics             } else { $defaults.metrics            }
        TargetLong   = if ($exp.target_long)         { $exp.target_long         } else { ""                           }
        TargetShort  = if ($exp.target_short)        { $exp.target_short        } else { ""                           }
        UseBuckets   = if ($exp.use_buckets -ne $null) { $exp.use_buckets       } else { $defaults.use_buckets        }
        TimeoutMins  = if ($exp.timeout_minutes)     { [int]$exp.timeout_minutes } else { $timeoutMins                }
        # Search space params (0 = use shell script defaults)
        NTrials              = if ($exp.n_trials)              { [int]$exp.n_trials              } elseif ($defaults.n_trials)              { [int]$defaults.n_trials              } else { 0 }
        MaxDepthMin          = if ($exp.max_depth_min)          { [int]$exp.max_depth_min          } elseif ($defaults.max_depth_min)          { [int]$defaults.max_depth_min          } else { 0 }
        MaxDepthMax          = if ($exp.max_depth_max)          { [int]$exp.max_depth_max          } elseif ($defaults.max_depth_max)          { [int]$defaults.max_depth_max          } else { 0 }
        NumLeavesMin         = if ($exp.num_leaves_min)         { [int]$exp.num_leaves_min         } elseif ($defaults.num_leaves_min)         { [int]$defaults.num_leaves_min         } else { 0 }
        NumLeavesMax         = if ($exp.num_leaves_max)         { [int]$exp.num_leaves_max         } elseif ($defaults.num_leaves_max)         { [int]$defaults.num_leaves_max         } else { 0 }
        MaxNEstimators       = if ($exp.max_n_estimators)       { [int]$exp.max_n_estimators       } elseif ($defaults.max_n_estimators)       { [int]$defaults.max_n_estimators       } else { 0 }
        EarlyStoppingRounds  = if ($exp.early_stopping_rounds)  { [int]$exp.early_stopping_rounds  } elseif ($defaults.early_stopping_rounds)  { [int]$defaults.early_stopping_rounds  } else { 0 }
        MaxFolds             = if ($exp.max_folds)              { [int]$exp.max_folds              } elseif ($defaults.max_folds)              { [int]$defaults.max_folds              } else { 0 }
        LearningRateMin      = if ($exp.learning_rate_min)      { [double]$exp.learning_rate_min      } elseif ($defaults.learning_rate_min)      { [double]$defaults.learning_rate_min      } else { 0 }
        LearningRateMax      = if ($exp.learning_rate_max)      { [double]$exp.learning_rate_max      } elseif ($defaults.learning_rate_max)      { [double]$defaults.learning_rate_max      } else { 0 }
        MinChildSamplesMin   = if ($exp.min_child_samples_min)  { [int]$exp.min_child_samples_min  } elseif ($defaults.min_child_samples_min)  { [int]$defaults.min_child_samples_min  } else { 0 }
        MinChildSamplesMax   = if ($exp.min_child_samples_max)  { [int]$exp.min_child_samples_max  } elseif ($defaults.min_child_samples_max)  { [int]$defaults.min_child_samples_max  } else { 0 }
        FeatureFractionMin   = if ($exp.feature_fraction_min)   { [double]$exp.feature_fraction_min   } elseif ($defaults.feature_fraction_min)   { [double]$defaults.feature_fraction_min   } else { 0 }
        FeatureFractionMax   = if ($exp.feature_fraction_max)   { [double]$exp.feature_fraction_max   } elseif ($defaults.feature_fraction_max)   { [double]$defaults.feature_fraction_max   } else { 0 }
        StartTime    = $null
        Job          = $null
        Status       = "QUEUED"
        ExitCode     = $null
        ArtifactOk   = $null
        FailureReason = $null
    })
}

# ============================================================
# MAIN ORCHESTRATION LOOP
# ============================================================

$activeSlots = [System.Collections.ArrayList]::new()
$allDone     = $false

while (-not $allDone) {

    # --- Try to fire new VMs if quota allows ---
    while ($queue.Count -gt 0) {
        $usedCpus = Get-UsedVcpus
        if ($usedCpus + $vcpusPerVm -gt $maxVcpus) {
            $quotaMsg = "[$(Get-Date -F 'HH:mm:ss')] vCPU cap reached ($usedCpus/$maxVcpus vCPU in use). Waiting for a slot..."
            Write-Host $quotaMsg -ForegroundColor Gray
            break
        }
        # Check VM count cap (IP address quota protection)
        if ($maxVms -gt 0 -and $activeSlots.Count -ge $maxVms) {
            Write-Host "[$(Get-Date -F 'HH:mm:ss')] VM cap reached ($($activeSlots.Count)/$maxVms VMs). Waiting for a slot..." -ForegroundColor Gray
            break
        }

        $exp = $queue.Dequeue()
        Write-Host ""
        Write-Host "------------------------------------------------------------" -ForegroundColor Cyan
        Write-Host " LAUNCHING [$($exp.Index)/$($expList.Count)]: $($exp.Label)" -ForegroundColor Cyan
        Write-Host "  VM:        $($exp.VmName)"
        Write-Host "  GCS:       gs://cltrainer-optuna-results/$($exp.GcsPrefix)/"
        Write-Host "  Local:     $($exp.LocalDir)"
        Write-Host "------------------------------------------------------------" -ForegroundColor Cyan

        # Delete any pre-existing VM with this name (clean slate)
        $existing = gcloud compute instances describe $exp.VmName --zone=$Zone --format="get(status)" 2>$null
        if ($existing) {
            Write-Host "  Pre-existing VM found ($existing). Deleting for clean slate..." -ForegroundColor Yellow
            gcloud compute instances delete $exp.VmName --zone=$Zone --quiet 2>$null
            Start-Sleep -Seconds 10
        }

        # Deploy VM across fallback zones
        $zoneList = $Zone -split ','
        $deployExit = 1
        $deployOutput = $null

        foreach ($z in $zoneList) {
            $z = $z.Trim()
            $deployArgs = @(
                "-ExecutionPolicy", "Bypass",
                "-File", (Join-Path $ScriptDir "gcp_deploy_sweep.ps1"),
                "-VmName",      $exp.VmName,
                "-MachineType", $exp.MachineType,
                "-Zone",        $z,
                "-GcsDataPath", $exp.GcsDataPath,
                "-StrategyConfig", $exp.StrategyConf,
                "-Metrics",     $exp.Metrics,
                "-JobName",     $exp.GcsPrefix,
                "-ProvisioningModel", $exp.Provisioning
            )
            if ($exp.TargetLong)  { $deployArgs += @("-TargetLong",  $exp.TargetLong)  }
            if ($exp.TargetShort) { $deployArgs += @("-TargetShort", $exp.TargetShort) }
            if ($exp.UseBuckets)  { $deployArgs += @("-UseBuckets") }
            # Search space overrides from manifest
            if ($exp.NTrials -gt 0)             { $deployArgs += @("-NTrials",             $exp.NTrials) }
            if ($exp.MaxDepthMin -gt 0)         { $deployArgs += @("-MaxDepthMin",         $exp.MaxDepthMin) }
            if ($exp.MaxDepthMax -gt 0)         { $deployArgs += @("-MaxDepthMax",         $exp.MaxDepthMax) }
            if ($exp.NumLeavesMin -gt 0)        { $deployArgs += @("-NumLeavesMin",        $exp.NumLeavesMin) }
            if ($exp.NumLeavesMax -gt 0)        { $deployArgs += @("-NumLeavesMax",        $exp.NumLeavesMax) }
            if ($exp.MaxNEstimators -gt 0)      { $deployArgs += @("-MaxNEstimators",      $exp.MaxNEstimators) }
            if ($exp.EarlyStoppingRounds -gt 0) { $deployArgs += @("-EarlyStoppingRounds", $exp.EarlyStoppingRounds) }
            if ($exp.MaxFolds -gt 0)            { $deployArgs += @("-MaxFolds",            $exp.MaxFolds) }
            if ($exp.LearningRateMin -gt 0)     { $deployArgs += @("-LearningRateMin",     $exp.LearningRateMin) }
            if ($exp.LearningRateMax -gt 0)     { $deployArgs += @("-LearningRateMax",     $exp.LearningRateMax) }
            if ($exp.MinChildSamplesMin -gt 0)  { $deployArgs += @("-MinChildSamplesMin",  $exp.MinChildSamplesMin) }
            if ($exp.MinChildSamplesMax -gt 0)  { $deployArgs += @("-MinChildSamplesMax",  $exp.MinChildSamplesMax) }
            if ($exp.FeatureFractionMin -gt 0)  { $deployArgs += @("-FeatureFractionMin",  $exp.FeatureFractionMin) }
            if ($exp.FeatureFractionMax -gt 0)  { $deployArgs += @("-FeatureFractionMax",  $exp.FeatureFractionMax) }

            Write-Host "  Deploying VM in zone $z..." -ForegroundColor Yellow
            $deployOutput = & powershell @deployArgs 2>&1
            $deployExit   = $LASTEXITCODE

            if ($deployExit -eq 0) {
                $actualZone = $z
                break
            } else {
                Write-Host "  Failed to deploy in zone $z (exit $deployExit)." -ForegroundColor Yellow
                # Clean up zombie VM — it may have been created before the deploy step failed
                Write-Host "  Cleaning up zombie VM in zone $z..." -ForegroundColor Yellow
                gcloud compute instances delete $exp.VmName --zone=$z --quiet 2>$null
            }
        }

        if ($deployExit -ne 0) {
            $errText = ($deployOutput | Select-Object -Last 10) -join "`n"
            Write-Host "  DEPLOY FAILED in all zones (last exit $deployExit):" -ForegroundColor Red
            Write-Host $errText -ForegroundColor Red
            Send-BatchTelegram ("[FAILED] *Deploy Failed: $($exp.Label)*`n" +
                "VM: ``$($exp.VmName)```n" +
                "Exit code: $deployExit`n" +
                "``````$errText``````")
            # Always attempt to delete the VM — it may have been created before the
            # verification step failed (e.g. tmux race condition). Prevents VM leaks.
            Remove-ExperimentVm -VmName $exp.VmName -VmZone $actualZone
            $exp.Status       = "DEPLOY_FAILED"
            $exp.FailureReason = "Deploy exit code $deployExit"
            $batchState.failed++
            $batchState.experiments += $exp
            Save-Progress $batchState
            continue
        }

        Write-Host "  VM deployed. Starting monitor background job..." -ForegroundColor Green

        # Start monitor as background PS job
        $monScript    = Join-Path $ScriptDir "gcp_monitor.ps1"
        $vmName_      = $exp.VmName
        $gcsPrefix_   = $exp.GcsPrefix
        $label_       = $exp.Label
        $batchId_     = $BatchId
        $expIdx_      = $exp.Index
        $expTotal_    = $expList.Count
        $tgEnabled_   = if ($DisableTelegram) { $false } else { $true }
        $pollSecs_    = 90    # 90-second poll - fast enough to catch early-stopping completions
        $projDir_     = $ProjectDir
        $gcpBin_      = $gcloudBin
        $exitCodeFile_= Join-Path $env:TEMP "monitor_exit_$($exp.VmName)_$(Get-Date -Format 'yyyyMMdd_HHmmss').txt"
        $actualZone_  = $actualZone

        $job = Start-Job -ScriptBlock {
            param($monScript, $vmName, $gcsPrefix, $label, $batchId, $expIdx, $expTotal, $tgEnabled, $pollSecs, $projDir, $gcpBin, $exitCodeFile, $vmZone)
            $env:PATH = "$gcpBin;$env:PATH"
            Set-Location $projDir
            # Build args array - cannot pass [switch] through -ArgumentList, so conditionally add -DisableTelegram
            $monArgs = @(
                "-ExecutionPolicy", "Bypass",
                "-File", $monScript,
                "-VmName", $vmName,
                "-Zone", $vmZone,
                "-GcsPrefix", $gcsPrefix,
                "-ExperimentLabel", $label,
                "-BatchId", $batchId,
                "-ExperimentIndex", $expIdx,
                "-BatchTotal", $expTotal,
                "-PollIntervalSeconds", $pollSecs,
                "-ExitCodeFile", $exitCodeFile
            )
            if (-not $tgEnabled) { $monArgs += "-DisableTelegram" }
            & powershell @monArgs
            return $LASTEXITCODE
        } -ArgumentList $monScript, $vmName_, $gcsPrefix_, $label_, $batchId_, $expIdx_, $expTotal_, $tgEnabled_, $pollSecs_, $projDir_, $gcloudBin, $exitCodeFile_, $actualZone_

        $exp.StartTime    = Get-Date
        $exp.Job          = $job
        $exp.Status       = "RUNNING"
        $exp.ExitCodeFile = $exitCodeFile_
        $exp.ActualZone   = $actualZone
        [void]$activeSlots.Add($exp)
        Write-Host "  Monitor job started (PS Job ID: $($job.Id))" -ForegroundColor Green
    }

    # --- Poll active jobs for completion ---
    $completed = @()
    foreach ($slot in $activeSlots) {
        $job     = $slot.Job
        $elapsed = ((Get-Date) - $slot.StartTime).TotalMinutes

        # Check timeout
        if ($elapsed -gt $slot.TimeoutMins) {
            Write-Host ""
            Write-Host "  TIMEOUT: $($slot.Label) exceeded ${elapsed:N0}min (limit: $($slot.TimeoutMins)min). Initiating teardown." -ForegroundColor Red
            Stop-Job $job -ErrorAction SilentlyContinue
            Remove-Job $job -Force -ErrorAction SilentlyContinue

            try {
                # CRITICAL: Do not let salvage operations hang the orchestrator.
                # Wrap in a background job with a strict temporal bound.
                $salvageJob = Start-Job -ScriptBlock {
                    param($LocalDir, $GcsPrefix, $VmName, $VmZone)
                    if (-not (Test-Path $LocalDir)) { New-Item -ItemType Directory -Path $LocalDir -Force | Out-Null }
                    $gcsBase = "gs://cltrainer-optuna-results/$GcsPrefix"
                    gcloud storage cp "$gcsBase/pipeline_summary.json" "$LocalDir\pipeline_summary.json" 2>$null
                    $canaryDir = Join-Path $LocalDir "registry\canary_output"
                    if (-not (Test-Path $canaryDir)) { New-Item -ItemType Directory -Path $canaryDir -Force | Out-Null }
                    gcloud storage cp -r "$gcsBase/production/*" "$canaryDir\" 2>$null
                    # Capture serial console (survives VM crash, lost after deletion)
                    $diagDir = Join-Path $LocalDir "crash_diagnostics"
                    if (-not (Test-Path $diagDir)) { New-Item -ItemType Directory -Path $diagDir -Force | Out-Null }
                    $serialOut = gcloud compute instances get-serial-port-output $VmName --zone=$VmZone 2>$null
                    if ($serialOut) { $serialOut | Out-File -FilePath (Join-Path $diagDir "serial_console.log") -Encoding utf8 }
                } -ArgumentList $slot.LocalDir, $slot.GcsPrefix, $slot.VmName, $slot.ActualZone

                # Wait maximum 2 minutes for salvage — then forcibly stop it
                Wait-Job $salvageJob -Timeout 120 | Out-Null
                Stop-Job $salvageJob -ErrorAction SilentlyContinue
                Remove-Job $salvageJob -Force -ErrorAction SilentlyContinue
            } catch {
                Write-Host "  WARNING: Artifact salvage failed during timeout handling." -ForegroundColor Yellow
            } finally {
                # GUARANTEED EXECUTION: Destroy the VM to free quota and stop billing
                Remove-ExperimentVm -VmName $slot.VmName -VmZone $slot.ActualZone
            }

            Send-BatchTelegram ("[TIMEOUT] *Timeout: $($slot.Label)*`n" +
                "Exceeded $($slot.TimeoutMins)min limit`nVM deleted. Salvage attempted.")
            $slot.Status       = "TIMEOUT"
            $slot.FailureReason = "Exceeded timeout ($($slot.TimeoutMins)min)"
            $batchState.failed++
            
            Write-Host ""
            Write-Host "  [FATAL] A VM timed out ($($slot.Label)). Fail-Fast triggered!" -ForegroundColor Red
            foreach ($active in $activeSlots) {
                if ($active.VmName -ne $slot.VmName) {
                    Write-Host "  [CLEANUP] Stopping other active VM: $($active.VmName)" -ForegroundColor Yellow
                    Stop-Job $active.Job -ErrorAction SilentlyContinue
                    Remove-ExperimentVm -VmName $active.VmName -VmZone $active.ActualZone
                }
            }
            $batchState["completed_at"] = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
            Save-Progress $batchState
            throw "Batch aborted due to timeout in experiment: $($slot.Label)"
        }

        if ($job.State -in @("Completed", "Failed", "Stopped")) {
            $jobOutput  = Receive-Job $job -ErrorAction SilentlyContinue
            # Read exit code from temp file written by the monitor
            $exitCode = 1
            if ($slot.ExitCodeFile -and (Test-Path $slot.ExitCodeFile)) {
                $raw = (Get-Content $slot.ExitCodeFile -Raw).Trim()
                if ($raw -match '^\d+$') { $exitCode = [int]$raw }
                Remove-Item $slot.ExitCodeFile -Force -ErrorAction SilentlyContinue
            }
            Remove-Job $job -Force -ErrorAction SilentlyContinue

            Write-Host ""
            Write-Host "  Monitor finished for: $($slot.Label) (exit: $exitCode)" -ForegroundColor $(if ($exitCode -eq 0) { "Green" } else { "Yellow" })

            # --- ARTIFACT VERIFICATION GATE ---
            $localDir  = $slot.LocalDir
            $gcsBase   = "gs://cltrainer-optuna-results/$($slot.GcsPrefix)"
            $artOk     = Test-ArtifactsDownloaded -LocalDir $localDir -GcsBase $gcsBase -MaxRetries 3

            if (-not $artOk) {
                Send-BatchTelegram ("[WARNING] *Artifact Download Failed: $($slot.Label)*`n" +
                    "pipeline_summary.json or logs not found locally after 3 retries.`n" +
                    "VM will be deleted to free quota.")
            }
            $slot.ArtifactOk = $artOk

            # --- CRASH DIAGNOSTICS (before deletion, only on failure) ---
            if ($exitCode -ne 0 -or -not $artOk) {
                Save-CrashDiagnostics -VmName $slot.VmName -VmZone $slot.ActualZone `
                    -GcsPrefix $slot.GcsPrefix -LocalDir $slot.LocalDir
            }

            # --- DELETE VM (quota freed here) ---
            Remove-ExperimentVm -VmName $slot.VmName -VmZone $slot.ActualZone

            # --- Update status ---
            if ($exitCode -eq 0 -and $artOk) {
                $slot.Status  = "COMPLETED"
                $batchState.completed++
            } else {
                $slot.Status       = "FAILED"
                if (-not $artOk) {
                    $slot.FailureReason = "Artifact download failed"
                } elseif ($exitCode -eq 2) {
                    $slot.FailureReason = "No log found (VM died before output)"
                } else {
                    $slot.FailureReason = "E2E pipeline incomplete (exit $exitCode)"
                }
                $batchState.failed++
                $slot.ExitCode = $exitCode

                Write-Host ""
                Write-Host "  [FATAL] A VM failed ($($slot.Label)). Fail-Fast triggered!" -ForegroundColor Red
                foreach ($active in $activeSlots) {
                    if ($active.VmName -ne $slot.VmName) {
                        Write-Host "  [CLEANUP] Stopping other active VM: $($active.VmName)" -ForegroundColor Yellow
                        Stop-Job $active.Job -ErrorAction SilentlyContinue
                        Remove-ExperimentVm -VmName $active.VmName -VmZone $active.ActualZone
                    }
                }
                $batchState["completed_at"] = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
                Save-Progress $batchState
                throw "Batch aborted due to critical failure in experiment: $($slot.Label). Reason: $($slot.FailureReason)"
            }
            $slot.ExitCode = $exitCode
            $completed    += $slot

            Write-Host "  Batch progress: $($batchState.completed) done, $($batchState.failed) failed, $($queue.Count) queued" -ForegroundColor Cyan
        } else {
            # Still running - print a heartbeat
            $elStr = "{0:N0}" -f $elapsed
            Write-Host "[$(Get-Date -F 'HH:mm:ss')] $($slot.Label) - running ${elStr}m (job state: $($job.State))" -ForegroundColor Gray
        }
    }

    # Remove completed slots and add to state
    foreach ($done in $completed) {
        $activeSlots.Remove($done)
        $batchState.experiments += @{
            index            = $done.Index
            label            = $done.Label
            vm_name          = $done.VmName
            gcs_prefix       = $done.GcsPrefix
            local_dir        = $done.LocalDir
            status           = $done.Status
            exit_code        = $done.ExitCode
            artifact_verified = $done.ArtifactOk
            failure_reason   = $done.FailureReason
            wall_time_min    = if ($done.StartTime) { [math]::Round(((Get-Date) - $done.StartTime).TotalMinutes, 1) } else { 0 }
        }
        Save-Progress $batchState
    }

    # Check termination condition
    $allDone = ($queue.Count -eq 0 -and $activeSlots.Count -eq 0)
    if (-not $allDone) { Start-Sleep -Seconds 30 }
}

# ============================================================
# BATCH COMPLETE
# ============================================================

$batchState["completed_at"] = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Save-Progress $batchState

Write-Host ""
Write-Host "============================================================" -ForegroundColor $(if ($batchState.failed -eq 0) { "Green" } else { "Yellow" })
Write-Host " BATCH COMPLETE" -ForegroundColor $(if ($batchState.failed -eq 0) { "Green" } else { "Yellow" })
Write-Host "============================================================" -ForegroundColor $(if ($batchState.failed -eq 0) { "Green" } else { "Yellow" })
Write-Host "  Total:     $($batchState.total)"
Write-Host "  Completed: $($batchState.completed)"
Write-Host "  Failed:    $($batchState.failed)"
Write-Host "  Progress:  $ProgressFile"
Write-Host "============================================================" -ForegroundColor $(if ($batchState.failed -eq 0) { "Green" } else { "Yellow" })

Send-BatchTelegram ("$(if ($batchState.failed -eq 0) { '[COMPLETE]' } else { '[WARNING]' }) *Batch Complete*`n" +
    "Completed: $($batchState.completed)/$($batchState.total)`n" +
    "Failed: $($batchState.failed)`n" +
    "_Generating consolidated report..._")

Write-Host ""
Write-Host "Generating the consolidated report..." -ForegroundColor Cyan
$collectArgs = @("-ExecutionPolicy", "Bypass", "-File", ".\gcp\collect_batch_results.ps1", "-BatchId", $BatchId)
if ($DisableTelegram) { $collectArgs += "-DisableTelegram" }
& powershell @collectArgs

# --- Post-Optimization: Deploy optimizer VM ---
$optElapsedTotal = 0  # Track optimizer wall clock for summary
$optMachineType  = "N/A"
$optWorkerCount  = 0
if ($batchState.completed -gt 0) {
    Write-Host ""
    Write-Host "Deploying cloud optimizer VM..." -ForegroundColor Cyan
    $optVmName = "optuna-post-optimizer"
    $optZoneList = ($Zone -split ',') | ForEach-Object { $_.Trim() }
    $optDeployExit = 1
    $optActualZone = $optZoneList[0]  # fallback default

    # Dynamically size the optimizer VM based on completed experiment count
    # Each experiment has 2 metrics (logloss, average_precision) = 2 optimization tasks
    $optTaskCount = $batchState.completed * 2
    $optMachineType = if ($optTaskCount -le 8) { "n2-standard-8" }
                      elseif ($optTaskCount -le 16) { "n2-standard-16" }
                      elseif ($optTaskCount -le 32) { "n2-standard-32" }
                      else { "n2-standard-48" }
    # Derive worker count from the dynamically-sized machine (match nproc on VM)
    $optWorkerCount = if ($optMachineType -match '-(\d+)$') { [int]$Matches[1] } else { 8 }
    Write-Host "  Optimizer sizing: $($batchState.completed) experiments × 2 metrics = $optTaskCount tasks → $optMachineType ($optWorkerCount workers)" -ForegroundColor Cyan

    foreach ($oz in $optZoneList) {
        $optArgs = @("-ExecutionPolicy", "Bypass", "-File", ".\gcp\gcp_deploy_optimizer.ps1",
            "-BatchId", $BatchId,
            "-NTrials", $postOptTrials,
            "-HoldoutMonths", $postOptHoldout,
            "-MachineType", $optMachineType,
            "-Workers", $optWorkerCount,
            "-Zone", $oz,
            "-SweepMode", $SweepMode)
        if ($DisableTelegram) { $optArgs += "-DisableTelegram" }
        Write-Host "  Trying optimizer deploy in zone $oz..." -ForegroundColor Yellow
        & powershell @optArgs
        $optDeployExit = $LASTEXITCODE
        if ($optDeployExit -eq 0) {
            $optActualZone = $oz
            Write-Host "  Optimizer deployed in zone $oz" -ForegroundColor Green
            break
        } else {
            Write-Host "  Optimizer deploy failed in zone $oz (exit $optDeployExit)" -ForegroundColor Yellow
        }
    }

    if ($optDeployExit -ne 0) {
        Write-Host "  ERROR: Optimizer deploy failed in all zones." -ForegroundColor Red
        Send-BatchTelegram "[FAILED] Post-optimizer deploy failed in all zones."
    } else {
        # Wait for optimizer VM to finish (self-shutdown)
        Write-Host ""
        Write-Host "Waiting for optimizer VM to complete (zone: $optActualZone)..." -ForegroundColor Cyan
        $optTimeoutMins = [int]([math]::Round($timeoutMins * 1.5))
        $optElapsed = 0

        while ($true) {
            Start-Sleep -Seconds 60
            $optElapsed++
            
            if ($optElapsed -gt $optTimeoutMins) {
                Write-Host "  TIMEOUT: Post-optimizer VM exceeded ${optElapsed}min (limit: ${optTimeoutMins}min). Force killing." -ForegroundColor Red
                Send-BatchTelegram "[TIMEOUT] Post-optimizer VM exceeded limit (${optTimeoutMins}min). VM killed."
                gcloud compute instances delete $optVmName --zone=$optActualZone --quiet 2>$null
                $optDeployExit = 1  # mark as failure
                break
            }

            $optStatus = gcloud compute instances describe $optVmName --zone=$optActualZone --format="get(status)" 2>$null
            if (-not $optStatus -or $optStatus.ToString().Trim() -in @("TERMINATED", "STOPPED")) {
                $optElapsedTotal = $optElapsed
                Write-Host "  Optimizer VM finished after ~${optElapsed}min." -ForegroundColor Green
                break
            }
            if ($optElapsed % 5 -eq 0) {
                Write-Host "  [$(Get-Date -F 'HH:mm:ss')] Optimizer running... (${optElapsed}min)" -ForegroundColor Gray
            }
        }

        if ($optElapsed -le $optTimeoutMins) {
            # Download results from GCS to local batch directory
            Write-Host "  Downloading optimized results from GCS..." -ForegroundColor Cyan
            $gcsBucket = "gs://cltrainer-optuna-results/batch_optimizer/$BatchId"
            $localBatch = Join-Path $ProjectDir "reports\batch_runs\$BatchId"
            gcloud storage cp "$gcsBucket/*.md" "$localBatch\" 2>$null
            gcloud storage cp "$gcsBucket/*.json" "$localBatch\" 2>$null

            if (Test-Path (Join-Path $localBatch "batch_summary_optimized_sharpe.md")) {
                Write-Host "  Optimized reports downloaded to: $localBatch" -ForegroundColor Green
                Send-BatchTelegram "[COMPLETE] Post-Optimization finished.`nResults downloaded to local batch directory.`nbatch_summary_optimized_sharpe.md is ready."
            } else {
                Write-Host "  WARNING: Could not download optimized reports." -ForegroundColor Yellow
                Send-BatchTelegram "[WARNING] Post-Optimization may have failed.`nCould not download optimized reports from GCS."
            }

            # Clean up optimizer VM
            gcloud compute instances delete $optVmName --zone=$optActualZone --quiet 2>$null
        }
    }
} else {
    Write-Host "  Skipping post-optimization -- no completed experiments." -ForegroundColor Yellow
}

# --- Generate Wall Clock Summary ---
$sweepMachine = if ($defaults.machine_type) { $defaults.machine_type } else { "unknown" }
Write-WallClockSummary -BatchState $batchState -BatchDir $BatchDir `
    -SweepMachineType $sweepMachine -SweepVcpus $vcpusPerVm `
    -OptMachineType $optMachineType -OptElapsedMin $optElapsedTotal `
    -OptTrials $postOptTrials -OptWorkers $optWorkerCount

Write-Host ""

exit $(if ($batchState.failed -eq 0) { 0 } else { 1 })
