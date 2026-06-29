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
    [string]$Zone                = "us-central1-a,us-central1-b,us-central1-c,us-central1-f,us-west1-a,us-west1-b,us-west1-c,us-east1-b,us-east1-c,us-east1-d,us-east4-a,us-east4-b,us-east4-c",
    [switch]$DryRun,
    [switch]$DisableTelegram,
    [int]$MaxConcurrentVcpus    = 0,   # 0 = read from manifest defaults
    [string]$SweepMode          = "backtest",  # 'frictionless' for Workflow C, 'backtest' for legacy
    [string]$OptMode            = "individual"  # 'individual' for per-side Long/Short optimization, 'ensemble' for joint
)

$ErrorActionPreference = "Continue"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

# Add gcloud to PATH
$gcloudBin = "C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin"
if ($env:PATH -notlike "*$gcloudBin*") { $env:PATH = "$gcloudBin;$env:PATH" }

$BatchTimestamp = Get-Date -Format "yyyyMMdd-HHmm"
$BatchId        = "batch_$($BatchTimestamp -replace '-','_')"
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
        [int]$OptWorkers,
        [double]$TotalScriptDurationMin
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

    # E2E total = sweep phase + optimizer
    $e2eTotalMin = if ($TotalScriptDurationMin) { $TotalScriptDurationMin } else { [math]::Round($sweepDurationMin + $OptElapsedMin, 1) }

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
# LOAD AND VALIDATE MANIFEST USING PYTHON ORCHESTRATOR
# ============================================================

$manifestFull = Join-Path $ProjectDir $ManifestPath
if (-not (Test-Path $manifestFull)) {
    Write-Host "ERROR: Manifest not found: $manifestFull" -ForegroundColor Red
    exit 1
}

Write-Host "Executing Python Batch Orchestrator schema validation & config generation..." -ForegroundColor Cyan
$pythonOut = & python gcp/batch_orchestrator.py --batch-manifest $ManifestPath
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python Orchestrator failed." -ForegroundColor Red
    exit 1
}

$orchestratorData = $pythonOut | ConvertFrom-Json
$limits = $orchestratorData.infrastructure_limits
$experiments = $orchestratorData.experiments

# Apply MaxConcurrentVcpus override or read from manifest infrastructure
$maxVcpus    = if ($MaxConcurrentVcpus -gt 0) { $MaxConcurrentVcpus } `
               else { [int]$limits.max_concurrent_vcpus }
$vcpusPerVm  = [int]$limits.vcpus_per_vm
$maxVms      = [int]$limits.max_concurrent_vms
$timeoutMins = [int]$limits.timeout_minutes
$machineType = $limits.machine_type
$provisioning = $limits.provisioning_model

Write-Host ""
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host " CANARY BATCH ORCHESTRATOR" -ForegroundColor Magenta
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host "  Batch ID:      $BatchId"
Write-Host "  Manifest:      $manifestFull"
Write-Host "  Experiments:   $($experiments.Count)"
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
    foreach ($exp in $experiments) {
        $idx++
        $label        = $exp.label
        $basePrefix   = $exp.gcs_prefix
        $vmName       = "optuna-sweep-$($basePrefix -replace '_','-')-$($BatchTimestamp -replace '-','-')"
        $tsPrefix     = "${basePrefix}_${BatchTimestamp}"
        Write-Host "  [$idx/$($experiments.Count)] $label" -ForegroundColor Cyan
        Write-Host "    VM:          $vmName"
        Write-Host "    GCS prefix:  $tsPrefix"
        Write-Host "    Machine:     $machineType"
        Write-Host "    Local out:   reports\$tsPrefix"
        Write-Host "    Config Path: $($exp.config_path)"
    }
    Write-Host ""
    Send-BatchTelegram "[DRY-RUN] *Dry Run Validated*`n$($experiments.Count) experiments in manifest.`n_No VMs were created._"
    Write-Host "Dry run complete." -ForegroundColor Green
    exit 0
}

# ============================================================
# INITIALISE BATCH STATE
# ============================================================

if (-not (Test-Path $BatchDir)) { New-Item -ItemType Directory -Path $BatchDir -Force | Out-Null }
$ScriptStartTracker = Get-Date

$savedManifestPath = Join-Path $BatchDir "manifest.json"
Copy-Item $ManifestPath $savedManifestPath -Force

$batchState = @{
    batch_id    = $BatchId
    manifest    = "manifest.json"
    started_at  = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    total       = $experiments.Count
    completed   = 0
    failed      = 0
    skipped     = 0
    experiments = @()
}
Save-Progress $batchState

Send-BatchTelegram ("[STARTING] *Batch Started*`n" +
    "Experiments: $($experiments.Count)`n" +
    "Max concurrent: $([math]::Floor($maxVcpus / $vcpusPerVm)) VMs`n" +
    "Timeout per experiment: ${timeoutMins}min")

# ============================================================
# BUILD EXPERIMENT QUEUE
# ============================================================

$queue       = [System.Collections.Queue]::new()
$expIndex    = 0
foreach ($exp in $experiments) {
    $expIndex++
    $basePrefix  = $exp.gcs_prefix
    $tsPrefix    = "${basePrefix}_${BatchTimestamp}"
    $vmName      = "optuna-sweep-$($basePrefix -replace '_','-')-$($BatchTimestamp -replace '-','-')"
    $localDir    = Join-Path $ProjectDir "reports\$tsPrefix"
    $queue.Enqueue(@{
        Index        = $expIndex
        Label        = $exp.label
        VmName       = $vmName
        BasePrefix   = $basePrefix
        GcsPrefix    = $tsPrefix
        LocalDir     = $localDir
        MachineType  = $machineType
        Provisioning = $provisioning
        ConfigPath   = $exp.config_path
        TimeoutMins  = $timeoutMins
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

try {
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
            Write-Host " LAUNCHING [$($exp.Index)/$($experiments.Count)]: $($exp.Label)" -ForegroundColor Cyan
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
            $actualZone = $null

            foreach ($z in $zoneList) {
                $z = $z.Trim()
                $deployArgs = @(
                    "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", (Join-Path $ScriptDir "gcp_deploy_sweep.ps1"),
                    "-VmName",            $exp.VmName,
                    "-MasterConfig",      $exp.ConfigPath,
                    "-Zone",              $z,
                    "-MachineType",       $exp.MachineType,
                    "-ProvisioningModel", $exp.Provisioning,
                    "-JobName",           $exp.GcsPrefix
                )

                Write-Host "  Deploying VM in zone $z..." -ForegroundColor Yellow
                $deployOutput = & powershell @deployArgs 2>&1
                $deployExit   = $LASTEXITCODE

                if ($deployExit -eq 0) {
                    $actualZone = $z
                    break
                } else {
                    Write-Host "  Failed to deploy in zone $z (exit $deployExit)." -ForegroundColor Yellow
                    # Clean up zombie VM - it may have been created before the deploy step failed
                    Write-Host "  Cleaning up zombie VM in zone $z to prevent leaks..." -ForegroundColor Yellow
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
                # Always attempt to delete the VM - it may have been created before the
                # verification step failed. Prevents VM leaks.
                if ($actualZone) {
                    Remove-ExperimentVm -VmName $exp.VmName -VmZone $actualZone
                } else {
                    Remove-ExperimentVm -VmName $exp.VmName -VmZone $zoneList[0].Trim()
                }
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
            $expTotal_    = $experiments.Count
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
                    "-NoProfile", "-ExecutionPolicy", "Bypass",
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

                    # Wait maximum 2 minutes for salvage - then forcibly stop it
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
} finally {
    # Teardown any remaining active VMs to prevent leaks
    if ($activeSlots.Count -gt 0) {
        Write-Host ""
        Write-Host "============================================================" -ForegroundColor Yellow
        Write-Host " TEARDOWN: Cleaning up active VMs on exit..." -ForegroundColor Yellow
        Write-Host "============================================================" -ForegroundColor Yellow
        foreach ($slot in $activeSlots) {
            Write-Host "  Teardown VM: $($slot.VmName) in zone $($slot.ActualZone) ..." -ForegroundColor Yellow
            gcloud compute instances delete $slot.VmName --zone=$slot.ActualZone --quiet 2>$null
        }
    }
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
$collectArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ".\gcp\collect_batch_results.ps1", "-BatchId", $BatchId)
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

    # Dynamically size the optimizer VM based on total concurrent task count
    # Each experiment: 2 metrics × 2 sides = 4 tasks/objective
    # Both sharpe+sortino run concurrently = ×2 → completed * 8 total
    $optTaskCount = $batchState.completed * 8
    $optMachineType = if ($optTaskCount -le 8) { "n2-standard-8" }
                      elseif ($optTaskCount -le 16) { "n2-standard-16" }
                      elseif ($optTaskCount -le 32) { "n2-standard-32" }
                      else { "n2-standard-48" }
    # Let Python auto-size workers (1 per task, memory-capped)
    $optWorkerCount = 0
    Write-Host "  Optimizer sizing: $($batchState.completed) experiments × 8 (2 metrics × 2 sides × 2 objectives) = $optTaskCount tasks → $optMachineType (workers=auto)" -ForegroundColor Cyan

    $manifestRaw = Get-Content $ManifestPath -Raw | ConvertFrom-Json
    $optExecData = if ($manifestRaw.baseline.execution_workflow.execution_data_path) { $manifestRaw.baseline.execution_workflow.execution_data_path } else { "" }
    $optSlippage = if ($manifestRaw.baseline.execution_workflow.slippage_multiplier) { [double]$manifestRaw.baseline.execution_workflow.slippage_multiplier } else { 0 }

    foreach ($oz in $optZoneList) {
        $optArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ".\gcp\gcp_deploy_optimizer.ps1",
            "-BatchId", $BatchId,
            "-NTrials", $postOptTrials,
            "-HoldoutMonths", $postOptHoldout,
            "-MachineType", $optMachineType,
            "-Workers", $optWorkerCount,
            "-Zone", $oz,
            "-SweepMode", $SweepMode,
            "-OptMode", $OptMode)
        if ($optExecData)       { $optArgs += @("-ExecData", $optExecData) }
        if ($optSlippage -gt 0) { $optArgs += @("-SlippagePerSide", $optSlippage) }
        if ($DisableTelegram) { $optArgs += "-DisableTelegram" }
        $optStartTracker = Get-Date
        Write-Host "  Trying optimizer deploy in zone $oz..." -ForegroundColor Yellow
        & powershell @optArgs
        $optDeployExit = $LASTEXITCODE
        $optDeployMins = [math]::Round(((Get-Date) - $optStartTracker).TotalMinutes, 1)

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
                $optElapsedTotal = $optElapsed + $optDeployMins
                Write-Host "  Optimizer VM finished after ~${optElapsedTotal}min." -ForegroundColor Green
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

            # --- Download Ensemble Results from Cloud ---
            $topPairsPath = Join-Path $localBatch "top_pairs.json"
            if (Test-Path $topPairsPath) {
                Write-Host ""
                Write-Host "Downloading ensemble results from GCS..." -ForegroundColor Cyan

                # Download ensemble backtest MDs
                gcloud storage cp "$gcsBucket/*ensemble_backtests*.md" "$localBatch\" 2>$null

                # Download optimized JSON configurations
                $localConfigDir = Join-Path $localBatch "configs"
                if (-not (Test-Path $localConfigDir)) { New-Item -ItemType Directory -Path $localConfigDir -Force | Out-Null }
                gcloud storage cp "$gcsBucket/batch_configs/*.json" "$localConfigDir\" 2>$null
                gcloud storage cp "$gcsBucket/*.json" "$localBatch\" 2>$null

                # Download ensemble predictions
                $localPredDir = Join-Path $localBatch "predictions"
                if (-not (Test-Path $localPredDir)) { New-Item -ItemType Directory -Path $localPredDir -Force | Out-Null }
                gcloud storage cp -r "$gcsBucket/predictions/*" "$localPredDir\" 2>$null

                $ensFiles = Get-ChildItem $localBatch -Filter "*ensemble_backtests*" -ErrorAction SilentlyContinue
                if ($ensFiles) {
                    Write-Host "  Ensemble results downloaded ($($ensFiles.Count) report(s))." -ForegroundColor Green
                    Send-BatchTelegram "[COMPLETE] Ensemble results downloaded from cloud."
                } else {
                    Write-Host "  WARNING: No ensemble backtest reports found on GCS." -ForegroundColor Yellow
                    Send-BatchTelegram "[WARNING] No ensemble backtest reports found on GCS."
                }
            } else {
                Write-Host "  No top_pairs.json found - skipping ensemble download." -ForegroundColor Yellow
            }
        }
    }
} else {
    Write-Host "  Skipping post-optimization -- no completed experiments." -ForegroundColor Yellow
}

# --- Generate Wall Clock Summary ---
$sweepMachine = $machineType
$ScriptEndTracker = Get-Date
$TotalScriptDurationMin = [math]::Round(($ScriptEndTracker - $ScriptStartTracker).TotalMinutes, 1)

Write-WallClockSummary -BatchState $batchState -BatchDir $BatchDir `
    -SweepMachineType $sweepMachine -SweepVcpus $vcpusPerVm `
    -OptMachineType $optMachineType -OptElapsedMin $optElapsedTotal `
    -OptTrials $postOptTrials -OptWorkers $optWorkerCount `
    -TotalScriptDurationMin $TotalScriptDurationMin

Write-Host ""

exit $(if ($batchState.failed -eq 0) { 0 } else { 1 })
