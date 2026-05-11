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
    [string]$ManifestPath        = "configs\canary_batch_manifest.json",
    [string]$Zone                = "us-central1-a",
    [switch]$DryRun,
    [switch]$EnableTelegram,
    [int]$MaxConcurrentVcpus    = 0   # 0 = read from manifest defaults
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
    if (-not $EnableTelegram) { return }

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
            --filter="status:RUNNING zone:$Zone" `
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


function Save-Progress {
    param([hashtable]$BatchState)
    if (-not (Test-Path $BatchDir)) { New-Item -ItemType Directory -Path $BatchDir -Force | Out-Null }
    $BatchState | ConvertTo-Json -Depth 10 | Out-File -FilePath $ProgressFile -Encoding utf8 -Force
}


function Test-ArtifactsDownloaded {
    <#
    Artifact verification gate — runs BEFORE deleting the VM.
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

        Write-Host "  [ArtifactGate] Attempt $attempt/$MaxRetries — missing artifacts:" -ForegroundColor Yellow
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


function Remove-ExperimentVm {
    param([string]$VmName)
    Write-Host "  Deleting VM: $VmName ..." -ForegroundColor Yellow
    if (-not $DryRun) {
        gcloud compute instances delete $VmName --zone=$Zone --quiet 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  VM deleted." -ForegroundColor Green
        } else {
            Write-Host "  VM delete returned non-zero (may already be gone)." -ForegroundColor Gray
        }
    } else {
        Write-Host "  [DryRun] Would delete VM: $VmName" -ForegroundColor Cyan
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
$timeoutMins = if ($defaults.timeout_minutes) { [int]$defaults.timeout_minutes } else { 90 }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host " CANARY BATCH ORCHESTRATOR" -ForegroundColor Magenta
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host "  Batch ID:      $BatchId"
Write-Host "  Manifest:      $manifestFull"
Write-Host "  Experiments:   $($expList.Count)"
Write-Host "  Max vCPUs:     $maxVcpus  (allows $(  [math]::Floor($maxVcpus / $vcpusPerVm)) concurrent VMs)"
Write-Host "  vCPU/VM:       $vcpusPerVm"
Write-Host "  Timeout/exp:   ${timeoutMins}m"
Write-Host "  Dry Run:       $DryRun"
Write-Host "  Telegram:      $EnableTelegram"
Write-Host "  Output Dir:    $BatchDir"
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host ""

# DryRun: validate and exit
if ($DryRun) {
    Write-Host "=== DRY RUN — No VMs will be created ===" -ForegroundColor Cyan
    $idx = 0
    foreach ($exp in $expList) {
        $idx++
        $label        = if ($exp.label) { $exp.label } else { "Exp-$idx" }
        $basePrefix   = $exp.gcs_prefix
        $vmName       = "optuna-canary-$($basePrefix -replace '_','-')"
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
        Write-Host "    Local out:   reports\$tsPrefix\"
    }
    Write-Host ""
    Send-BatchTelegram "🔍 *Dry Run Validated*`n$($expList.Count) experiments in manifest.`n_No VMs were created._"
    Write-Host "Dry run complete." -ForegroundColor Green
    exit 0
}

# ============================================================
# INITIALISE BATCH STATE
# ============================================================

if (-not (Test-Path $BatchDir)) { New-Item -ItemType Directory -Path $BatchDir -Force | Out-Null }

$batchState = @{
    batch_id    = $BatchId
    manifest    = $ManifestPath
    started_at  = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    total       = $expList.Count
    completed   = 0
    failed      = 0
    skipped     = 0
    experiments = @()
}
Save-Progress $batchState

Send-BatchTelegram ("🎬 *Batch Started*`n" +
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
    $vmName      = "optuna-canary-$($basePrefix -replace '_','-')"
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
            $quotaMsg = "[$(Get-Date -F 'HH:mm:ss')] Quota cap reached `($usedCpus/$maxVcpus vCPU in use`). Waiting for a slot..."
            Write-Host $quotaMsg -ForegroundColor Gray
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

        # Deploy VM
        $deployArgs = @(
            "-ExecutionPolicy", "Bypass",
            "-File", (Join-Path $ScriptDir "gcp_deploy_canary.ps1"),
            "-VmName",      $exp.VmName,
            "-MachineType", $exp.MachineType,
            "-Zone",        $Zone,
            "-GcsDataPath", $exp.GcsDataPath,
            "-StrategyConfig", $exp.StrategyConf,
            "-Metrics",     $exp.Metrics,
            "-GcsPrefix",   $exp.GcsPrefix,
            "-ProvisioningModel", $exp.Provisioning,
            "-NoMonitor"
        )
        if ($exp.TargetLong)  { $deployArgs += @("-TargetLong",  $exp.TargetLong)  }
        if ($exp.TargetShort) { $deployArgs += @("-TargetShort", $exp.TargetShort) }
        if ($exp.UseBuckets)  { $deployArgs += @("-UseBuckets") }

        Write-Host "  Deploying VM..." -ForegroundColor Yellow
        $deployOutput = & powershell @deployArgs 2>&1
        $deployExit   = $LASTEXITCODE

        if ($deployExit -ne 0) {
            $errText = ($deployOutput | Select-Object -Last 10) -join "`n"
            Write-Host "  DEPLOY FAILED (exit $deployExit):" -ForegroundColor Red
            Write-Host $errText -ForegroundColor Red
            Send-BatchTelegram ("🚫 *Deploy Failed: $($exp.Label)*`n" +
                "VM: ``$($exp.VmName)```n" +
                "Exit code: $deployExit`n" +
                "``````$errText``````")
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
        $tgEnabled_   = $EnableTelegram.IsPresent
        $pollSecs_    = 90    # 90-second poll — fast enough to catch early-stopping completions
        $projDir_     = $ProjectDir
        $gcpBin_      = $gcloudBin
        $exitCodeFile_= Join-Path $env:TEMP "monitor_exit_$($exp.VmName)_$(Get-Date -Format 'yyyyMMdd_HHmmss').txt"

        $job = Start-Job -ScriptBlock {
            param($monScript, $vmName, $gcsPrefix, $label, $batchId, $expIdx, $expTotal, $tgEnabled, $pollSecs, $projDir, $gcpBin, $exitCodeFile)
            $env:PATH = "$gcpBin;$env:PATH"
            Set-Location $projDir
            # Build args array — cannot pass [switch] through -ArgumentList, so conditionally add -EnableTelegram
            $monArgs = @(
                "-ExecutionPolicy", "Bypass",
                "-File", $monScript,
                "-VmName", $vmName,
                "-GcsPrefix", $gcsPrefix,
                "-ExperimentLabel", $label,
                "-BatchId", $batchId,
                "-ExperimentIndex", $expIdx,
                "-BatchTotal", $expTotal,
                "-PollIntervalSeconds", $pollSecs,
                "-ExitCodeFile", $exitCodeFile
            )
            if ($tgEnabled) { $monArgs += "-EnableTelegram" }
            & powershell @monArgs
            return $LASTEXITCODE
        } -ArgumentList $monScript, $vmName_, $gcsPrefix_, $label_, $batchId_, $expIdx_, $expTotal_, $tgEnabled_, $pollSecs_, $projDir_, $gcloudBin, $exitCodeFile_

        $exp.StartTime    = Get-Date
        $exp.Job          = $job
        $exp.Status       = "RUNNING"
        $exp.ExitCodeFile = $exitCodeFile_
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
            Write-Host "  TIMEOUT: $($slot.Label) exceeded ${elapsed:N0}min (limit: $($slot.TimeoutMins)min)" -ForegroundColor Red
            Stop-Job $job -ErrorAction SilentlyContinue
            Remove-Job $job -Force -ErrorAction SilentlyContinue
            gcloud compute instances stop $slot.VmName --zone=$Zone --quiet 2>$null
            Send-BatchTelegram ("⏱️ *Timeout: $($slot.Label)*`n" +
                "Exceeded $($slot.TimeoutMins)min limit`nVM stopped.")
            $slot.Status       = "TIMEOUT"
            $slot.FailureReason = "Exceeded timeout ($($slot.TimeoutMins)min)"
            $batchState.failed++
            $completed += $slot
            continue
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
                Send-BatchTelegram ("⚠️ *Artifact Download Failed: $($slot.Label)*`n" +
                    "pipeline_summary.json or logs not found locally after 3 retries.`n" +
                    "VM will be deleted to free quota.")
            }
            $slot.ArtifactOk = $artOk

            # --- DELETE VM (quota freed here) ---
            Remove-ExperimentVm -VmName $slot.VmName

            # --- Update status ---
            if ($exitCode -eq 0 -and $artOk) {
                $slot.Status  = "COMPLETED"
                $batchState.completed++
            } else {
                $slot.Status       = "FAILED"
                $slot.FailureReason = if (-not $artOk) { "Artifact download failed" } `
                                      elseif ($exitCode -eq 2) { "No log found (VM died before output)" } `
                                      else { "E2E pipeline incomplete (exit $exitCode)" }
                $batchState.failed++
            }
            $slot.ExitCode = $exitCode
            $completed    += $slot

            Write-Host "  Batch progress: $($batchState.completed) done, $($batchState.failed) failed, $($queue.Count) queued" -ForegroundColor Cyan
        } else {
            # Still running — print a heartbeat
            $elStr = "{0:N0}" -f $elapsed
            Write-Host "[$(Get-Date -F 'HH:mm:ss')] $($slot.Label) — running ${elStr}m (job state: $($job.State))" -ForegroundColor Gray
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

Send-BatchTelegram ("$(if ($batchState.failed -eq 0) { '🏁' } else { '⚠️' }) *Batch Complete*`n" +
    "Completed: $($batchState.completed)/$($batchState.total)`n" +
    "Failed: $($batchState.failed)`n" +
    "Run collect_batch_results.ps1 to generate the comparison report.")

Write-Host ""
Write-Host "Next: generate the consolidated report:" -ForegroundColor Cyan
Write-Host ('  powershell -ExecutionPolicy Bypass -File .\gcp\collect_batch_results.ps1 -BatchId ' + $BatchId) -ForegroundColor Cyan
Write-Host ""

exit $(if ($batchState.failed -eq 0) { 0 } else { 1 })
