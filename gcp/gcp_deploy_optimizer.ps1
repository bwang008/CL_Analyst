<#
.SYNOPSIS
    Deploy a VM to run batch_post_optimizer.py on cloud with full parallelism.
.DESCRIPTION
    Provisions an n2-standard-32 VM (128GB RAM), uploads code and batch metadata,
    and launches vm_post_optimize.sh which downloads experiment artifacts
    from GCS and runs the optimizer with --workers 24.
.EXAMPLE
    .\gcp\gcp_deploy_optimizer.ps1 -BatchId batch_20260513_1941
    .\gcp\gcp_deploy_optimizer.ps1 -BatchId batch_20260513_1941 -NTrials 1000 -Workers 24
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$BatchId,
    [string]$VmName = "opt-post-$($BatchId.Replace('_','-'))",
    [string]$MachineType = "n2-standard-32",
    [string]$Zone = "us-central1-a",
    [int]$DiskSizeGB = 50,
    [string]$Project = "cltrainer",
    [string]$ProvisioningModel = "STANDARD",
    [int]$NTrials = 500,
    # DEPRECATED / IGNORED: holdout is read authoritatively from the manifest
    # (post_optimizer_holdout_months) by vm_post_optimize.sh. Passing this has no effect.
    [int]$HoldoutMonths = 0,
    [int]$Workers = 0,
    [switch]$NoShutdown,
    [switch]$NoMonitor,
    [switch]$DisableTelegram,
    [string]$Objective = "both",
    [string]$SweepMode = "backtest",
    [string]$OptMode = "individual",
    [string]$ExecData = "",
    [double]$SlippagePerSide = 0
)

# Add gcloud to PATH if not already there
$gcloudBin = "C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin"
if ($env:PATH -notlike "*$gcloudBin*") {
    $env:PATH = "$gcloudBin;$env:PATH"
}

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$GcpUser = $env:USERNAME
$RemoteHome = "/home/${GcpUser}"
$RemoteProject = "${RemoteHome}/project"
$Bucket = "gs://cltrainer-optuna-results"
$GcsOptPrefix = "batch_optimizer/$BatchId"

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

function Send-TelegramAlert {
    param([string]$Message)
    if ($DisableTelegram) { return }
    $env_vars = Read-DotEnv
    $token = if ($env_vars["TELEGRAM_BOT_TOKEN"]) { $env_vars["TELEGRAM_BOT_TOKEN"] } else { $env:TELEGRAM_BOT_TOKEN }
    $chatId = if ($env_vars["TELEGRAM_CHAT_ID"]) { $env_vars["TELEGRAM_CHAT_ID"] } else { $env:TELEGRAM_CHAT_ID }
    if (-not $token) { return }
    $plainMsg = $Message -replace '\*', '' -replace '``', '' -replace '_', ''
    $bodyObj = @{ chat_id = $chatId; text = $plainMsg }
    $bodyJson = $bodyObj | ConvertTo-Json -Compress
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($bodyJson)
    try {
        Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/sendMessage" -ContentType 'application/json; charset=utf-8' -Body $bodyBytes | Out-Null
    } catch {}
}

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Magenta
Write-Host " DEPLOY POST-OPTIMIZER VM" -ForegroundColor Magenta
Write-Host "=====================================================" -ForegroundColor Magenta
Write-Host "  Batch ID:      $BatchId"
Write-Host "  VM:            $VmName"
Write-Host "  Machine:       $MachineType"
Write-Host "  Pricing:       $ProvisioningModel"
Write-Host "  N Trials:      $NTrials"
Write-Host "  Holdout:       $HoldoutMonths months"
Write-Host "  Workers:       $Workers"
Write-Host "  Auto-Shutdown: $(-not $NoShutdown)"
Write-Host "=====================================================" -ForegroundColor Magenta

# --- Validate batch_progress.json exists locally ---
$batchDir = Join-Path $ProjectDir "reports\batch_runs\$BatchId"
$progressFile = Join-Path $batchDir "batch_progress.json"
if (-not (Test-Path $progressFile)) {
    Write-Host "ERROR: batch_progress.json not found at: $progressFile" -ForegroundColor Red
    exit 1
}
Write-Host "`n  Found batch_progress.json" -ForegroundColor Green

# --- [1/7] Upload batch metadata to GCS ---
Write-Host "`n[1/7] Uploading batch metadata to GCS..."
gcloud storage cp $progressFile "$Bucket/$GcsOptPrefix/batch_progress.json"
$manifestFile = Join-Path $batchDir "manifest.json"
if (Test-Path $manifestFile) {
    gcloud storage cp $manifestFile "$Bucket/$GcsOptPrefix/manifest.json"
}
Write-Host "  Uploaded to $Bucket/$GcsOptPrefix/" -ForegroundColor Green

# --- [2/7] Provision VM ---
Write-Host "`n[2/7] Provisioning optimizer VM..."

# Check if VM already exists
$existingStatus = $null
try {
    $existingStatus = gcloud compute instances describe $VmName --zone=$Zone `
        --format="get(status)" 2>&1 | Select-String -Pattern "^(RUNNING|TERMINATED|STOPPED|SUSPENDED)$"
    if ($existingStatus) { $existingStatus = $existingStatus.ToString().Trim() }
} catch {
    $existingStatus = $null
}

if ($existingStatus) {
    Write-Host "  VM already exists (status: $existingStatus)" -ForegroundColor Yellow
    if ($existingStatus -ne "RUNNING") {
        Write-Host "  Starting existing VM..." -ForegroundColor Yellow
        gcloud compute instances start $VmName --zone=$Zone --quiet
        Start-Sleep -Seconds 20
    }
} else {
    Write-Host "  Creating new VM ($MachineType, $ProvisioningModel)..."
    $startupScript = Join-Path $ScriptDir "vm_startup.sh"
    
    # ORPHAN BACKSTOP: control-plane-enforced TTL so a dead local monitor can
    # never orphan this VM (post-opt runs ~1-2h; 360m is a generous ceiling).
    $createArgs = @(
        "compute", "instances", "create", $VmName,
        "--project=$Project",
        "--zone=$Zone",
        "--machine-type=$MachineType",
        "--image-family=ubuntu-2204-lts",
        "--image-project=ubuntu-os-cloud",
        "--boot-disk-size=${DiskSizeGB}GB",
        "--boot-disk-type=pd-ssd",
        "--metadata-from-file=startup-script=$startupScript",
        "--scopes=compute-rw,storage-full",
        "--max-run-duration=360m",
        "--instance-termination-action=DELETE",
        "--quiet"
    )
    
    & gcloud @createArgs
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`nERROR: Failed to create VM." -ForegroundColor Red
        exit 1
    }
    Write-Host "  VM created!" -ForegroundColor Green
}

# Wait for startup script
Write-Host "  Waiting for startup script to complete..."
$maxWait = 900; $elapsed = 0
while ($elapsed -lt $maxWait) {
    Start-Sleep -Seconds 15; $elapsed += 15
    $vmStatus = gcloud compute instances describe $VmName --zone=$Zone --format="get(status)" 2>$null
    if ($vmStatus -and $vmStatus.ToString().Trim() -in @("TERMINATED", "STOPPED")) {
        Write-Host "`n  FATAL: VM is $vmStatus during startup." -ForegroundColor Red
        exit 3
    }
    try {
        $ready = gcloud compute ssh $VmName --zone=$Zone `
            --command="test -f /tmp/startup_done && echo READY" `
            --quiet 2>$null
        if ($ready -match "READY") { break }
    } catch {}
    Write-Host "  Installing... (${elapsed}s elapsed)" -ForegroundColor Gray
}

if ($elapsed -ge $maxWait) {
    Write-Host "  WARNING: Reached 300s timeout. Proceeding anyway." -ForegroundColor Yellow
} else {
    Write-Host "  VM is ready!" -ForegroundColor Green
}

# --- [3/7] Create directory structure ---
# configs/ is NOT in the code zip (reviewer condition 1): configs/strategies must
# exist for the strategy-config scp below. reports/batch_runs + data/processed are
# also created VM-side by vm_post_optimize.sh; kept here for first-boot ordering.
Write-Host "`n[3/7] Creating directory structure..."
gcloud compute ssh $VmName --zone=$Zone --quiet `
    --command="mkdir -p $RemoteProject/configs/strategies $RemoteProject/reports/batch_runs $RemoteProject/data/processed" 2>$null

# --- [4/7] Upload code (whole-tree zip) ---
# Ticket optimizer-vm-deploy-whitelist_07052026_1820: the previous hand-curated
# file whitelist silently drifted from the import graph and killed 4 batches in
# one day (src.core, then src.live_execution.instrument_context). Ship the SAME
# whole src/agent/gcp trees the sweep deploy ships (mechanism transplanted from
# gcp_deploy_sweep.ps1) so import drift can never strand the optimizer VM again.
Write-Host "`n[4/7] Uploading code (whole-tree zip)..."

# Unique per-VM archive name: parallel deploys must never share a zip filename.
$zipName = "deploy_$VmName.zip"
$zipFile = Join-Path $ProjectDir $zipName
if (Test-Path $zipFile) { Remove-Item $zipFile -Force }

$itemsToZip = @("src", "agent", "gcp", "requirements.txt", ".env")
$existingItems = @()
foreach ($item in $itemsToZip) {
    if (Test-Path (Join-Path $ProjectDir $item)) {
        $existingItems += $item
    }
}

if ($existingItems.Count -gt 0) {
    Write-Host "  Zipping codebase (excluding pycache and datasets)..."
    $tarArgs = @("-a", "-c", "-C", $ProjectDir, "-f", $zipFile, "--exclude=__pycache__", "--exclude=*.parquet", "--exclude=*.csv") + $existingItems
    & "tar.exe" $tarArgs

    # Upload + unzip + VERIFY with retries; sentinel is vm_post_optimize.sh
    # (reviewer condition 2 — NOT the sweep's vm_sweep_run.sh).
    $codeLanded = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Write-Host "  Uploading code to VM (attempt $attempt/3)..."
        gcloud compute ssh $VmName --zone=$Zone --command="mkdir -p $RemoteProject" --quiet 2>$null
        gcloud compute scp "$zipFile" "${VmName}:${RemoteProject}/$zipName" --zone=$Zone --quiet 2>$null
        gcloud compute ssh $VmName --zone=$Zone --command="cd $RemoteProject && unzip -q -o $zipName && rm -f $zipName" --quiet 2>$null
        $verify = gcloud compute ssh $VmName --zone=$Zone --command="test -f $RemoteProject/gcp/vm_post_optimize.sh && echo CODE_OK" --quiet 2>$null
        if ($verify -match "CODE_OK") { $codeLanded = $true; break }
        Write-Host "  Code not present on VM after attempt $attempt; retrying..." -ForegroundColor Yellow
        Start-Sleep -Seconds 3
    }
    Remove-Item $zipFile -ErrorAction SilentlyContinue

    if (-not $codeLanded) {
        Write-Host "  FATAL: code failed to land on VM (gcp/vm_post_optimize.sh missing after 3 attempts)." -ForegroundColor Red
        Write-Host "  Aborting deploy to avoid launching the optimizer on an empty VM." -ForegroundColor Red
        exit 1
    }
    Write-Host "  Code verified on VM (gcp/vm_post_optimize.sh present)." -ForegroundColor Green
} else {
    Write-Host "  FATAL: No code directories found to upload locally." -ForegroundColor Red
    exit 1
}

# Upload global risk filters
$globalFilters = Join-Path $ProjectDir "configs\global_risk_filters.json"
if (Test-Path $globalFilters) {
    gcloud compute scp "$globalFilters" "${VmName}:${RemoteProject}/configs/" --zone=$Zone --quiet 2>$null
} else {
    Write-Host "  WARNING: Missing configs\global_risk_filters.json" -ForegroundColor Yellow
}

# Upload strategy configs
$configsDir = Join-Path $ProjectDir "configs\strategies\*.json"
try {
    gcloud compute scp "$configsDir" "${VmName}:${RemoteProject}/configs/strategies/" --zone=$Zone --quiet 2>$null
} catch {
    Write-Host "  WARNING: Failed to copy configs" -ForegroundColor Yellow
}

Write-Host "  Code uploaded!" -ForegroundColor Green

# Fix CRLF line endings on shell scripts (Windows git checkout produces CRLF which breaks bash)
Write-Host "  Fixing line endings on shell scripts..."
try {
    gcloud compute ssh $VmName --zone=$Zone --command="find $RemoteProject -name '*.sh' -exec sed -i 's/\r$//' {} + && chmod +x $RemoteProject/gcp/*.sh" --quiet 2>$null
} catch {}
Write-Host "  Line endings fixed." -ForegroundColor Green

# --- [4b/7] Verify uploaded code integrity ---
# Guards against stale-disk reuse (fixed VM name) + silently-swallowed scp errors:
# scp runs with 2>$null and "Code uploaded!" prints unconditionally, so a failed
# overwrite on a reused boot disk would run OLD code. Hash a sentinel file on the VM
# and compare to local; abort BEFORE launch on mismatch.
Write-Host "`n[4b/7] Verifying uploaded code matches local..."
$localSweep  = Join-Path $ProjectDir "agent\sweep_ensembles.py"
$remoteSweep = "$RemoteProject/agent/sweep_ensembles.py"
$localHash   = (Get-FileHash -Algorithm SHA256 -Path $localSweep).Hash.ToLower()
$remoteHash  = gcloud compute ssh $VmName --zone=$Zone --quiet `
    --command="sha256sum '$remoteSweep' 2>/dev/null | awk '{print `$1}'" 2>$null
if ($remoteHash) { $remoteHash = $remoteHash.ToString().Trim().ToLower() }
if ($remoteHash -ne $localHash) {
    Write-Host "  FATAL: sweep_ensembles.py on VM does NOT match local copy!" -ForegroundColor Red
    Write-Host "    local : $localHash"  -ForegroundColor Red
    Write-Host "    remote: $remoteHash" -ForegroundColor Red
    Write-Host "  Aborting BEFORE launch to prevent running stale code." -ForegroundColor Red
    Send-TelegramAlert "[ABORT] Code integrity check failed`nBatch: $BatchId`nVM: $VmName`nsweep_ensembles.py hash mismatch - stale code on disk. Delete the VM and re-run on a clean slate."
    exit 4
}
Write-Host "  Code integrity verified (sweep_ensembles.py SHA256 matches)." -ForegroundColor Green

# --- [5/7] Launch optimizer ---
Write-Host "`n[5/7] Launching post-optimizer in tmux..."

$shutdownFlag = if ($NoShutdown) { "" } else { "--shutdown" }
$execDataFlag = if ($ExecData) { " --exec-data=$ExecData" } else { "" }
$slippageFlag = if ($SlippagePerSide -gt 0) { " --slippage-per-side=$SlippagePerSide" } else { "" }
$launchCmd = "tmux kill-session -t optimizer 2>/dev/null; tmux new-session -d -s optimizer 'bash $RemoteProject/gcp/vm_post_optimize.sh --batch-id=$BatchId --n-trials=$NTrials --holdout-months=$HoldoutMonths --workers=$Workers --objective=$Objective --sweep-mode=$SweepMode --opt-mode=$OptMode$execDataFlag$slippageFlag $shutdownFlag'"
gcloud compute ssh $VmName --zone=$Zone --command=$launchCmd --quiet 2>$null

Write-Host "  Optimizer launched!" -ForegroundColor Green

# --- [6/7] Verify tmux session ---
Write-Host "`n[6/7] Verifying tmux session..."
Start-Sleep -Seconds 15
$tmuxCheck = gcloud compute ssh $VmName --zone=$Zone `
    --command="tmux has-session -t optimizer 2>/dev/null && echo RUNNING || echo NOT_RUNNING" `
    --quiet 2>$null

if ($tmuxCheck -match "RUNNING") {
    Write-Host "  tmux session 'optimizer' is active!" -ForegroundColor Green
    Send-TelegramAlert "[STARTING] Post-Optimizer Deploy`nBatch: $BatchId`nTrials: $NTrials | Workers: $Workers | Objective: $Objective`nVM: $VmName"
} else {
    Write-Host "  WARNING: tmux session died! Checking VM log for errors..." -ForegroundColor Yellow
    $errorLog = gcloud compute ssh $VmName --zone=$Zone `
        --command="cat /home/*/project/post_optimize_*.log 2>/dev/null | tail -20 || echo 'No log file found'" `
        --quiet 2>$null
    Write-Host "  --- VM Log Tail ---" -ForegroundColor Yellow
    Write-Host $errorLog -ForegroundColor Red
    Write-Host "  --- End Log ---" -ForegroundColor Yellow
    Send-TelegramAlert "[WARNING] Post-Optimizer Deploy Failed`nBatch: $BatchId`nVM: $VmName`ntmux session died immediately.`nLog: $errorLog"
}

# Note: The canary-style gcp_monitor.ps1 is NOT used for the optimizer VM.
# Instead, we poll GCS directly for completed report files below.

$LocalBatchDir = Join-Path $ProjectDir "reports\batch_runs\$BatchId"

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host " POST-OPTIMIZER DEPLOYED" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  VM:           $VmName"
Write-Host "  Batch:        $BatchId"
Write-Host "  GCS output:   $Bucket/$GcsOptPrefix/"
Write-Host "  Local output: $LocalBatchDir"
Write-Host ""

if ($NoMonitor) {
    Write-Host "Monitoring disabled (-NoMonitor). Download results manually:" -ForegroundColor Yellow
    Write-Host "  gsutil -m cp $Bucket/$GcsOptPrefix/batch_summary_optimized_*.md $LocalBatchDir\"
    Write-Host "  gsutil -m cp $Bucket/$GcsOptPrefix/optimization_results_*.json $LocalBatchDir\"
    Write-Host ""
    Write-Host "Useful commands:" -ForegroundColor Cyan
    Write-Host "  View live output: gcloud compute ssh $VmName --zone=$Zone --command='tmux attach -t optimizer'"
    Write-Host ""
    exit 0
}

# --- [7/7] Poll GCS for results and download when complete ---
Write-Host "[7/7] Monitoring GCS for completed reports..." -ForegroundColor Cyan
Write-Host "  Polling every 2 minutes. Press Ctrl+C to stop monitoring (VM continues running)."
Write-Host ""

# Fetch the crash reason the VM trap persisted to GCS before self-deleting
# (CRASH_REPORT.txt; falls back to error lines from the newest uploaded log).
# Without this, a failed run only reports "no reports found" with no cause.
function Get-CrashReason {
    $crashLocal = Join-Path $env:TEMP "crash_report_$BatchId.txt"
    Remove-Item $crashLocal -Force -ErrorAction SilentlyContinue
    gsutil cp "$Bucket/$GcsOptPrefix/CRASH_REPORT.txt" $crashLocal 2>$null
    if (Test-Path $crashLocal) { return (Get-Content $crashLocal -Raw) }
    $latestLog = gsutil ls "$Bucket/$GcsOptPrefix/logs/" 2>$null | Select-Object -Last 1
    if ($latestLog) {
        $logLocal = Join-Path $env:TEMP "postopt_lastlog_$BatchId.log"
        gsutil cp "$latestLog" $logLocal 2>$null
        if (Test-Path $logLocal) {
            $errLines = Select-String -Path $logLocal -Pattern "FATAL|MANIFEST ERROR|ModuleNotFoundError|ERROR|Exception" |
                Select-Object -Last 8 | ForEach-Object { $_.Line.Trim() }
            if ($errLines) { return "(extracted from $latestLog)`n" + ($errLines -join "`n") }
        }
    }
    return "(no CRASH_REPORT.txt or error lines found under $Bucket/$GcsOptPrefix/)"
}

$pollInterval = 120  # seconds
$pollCount = 0

while ($true) {
    $pollCount++
    $elapsed = [math]::Round($pollCount * $pollInterval / 60, 0)

    # Check if any optimization report exists on GCS
    $gcsFiles = $null
    try { $gcsFiles = gsutil ls "$Bucket/$GcsOptPrefix/batch_summary_optimized_*.md" 2>$null } catch {}
    if ($gcsFiles) {
        Write-Host ""
        Write-Host "  Reports detected on GCS after ~${elapsed} min!" -ForegroundColor Green

        # Download all reports and results to local batch dir
        if (!(Test-Path $LocalBatchDir)) { New-Item -ItemType Directory -Path $LocalBatchDir -Force | Out-Null }

        Write-Host "  Downloading reports..."
        gsutil -m cp "$Bucket/$GcsOptPrefix/batch_summary_optimized_*.md" "$LocalBatchDir\" 2>$null
        gsutil -m cp "$Bucket/$GcsOptPrefix/optimization_results_*.json" "$LocalBatchDir\" 2>$null

        # Download batch configs if they exist
        $batchConfigsExist = gsutil ls "$Bucket/$GcsOptPrefix/batch_configs/" 2>$null
        if ($batchConfigsExist) {
            $localConfigs = Join-Path $LocalBatchDir "configs"
            if (!(Test-Path $localConfigs)) { New-Item -ItemType Directory -Path $localConfigs -Force | Out-Null }
            gsutil -m cp "$Bucket/$GcsOptPrefix/batch_configs/*.json" "$localConfigs\" 2>$null
            Write-Host "  Downloaded batch configs"
        }

        # Download logs
        gsutil -m cp "$Bucket/$GcsOptPrefix/logs/*" (Join-Path $LocalBatchDir "logs\") 2>$null

        # List what we downloaded
        Write-Host ""
        Write-Host "  Downloaded to: $LocalBatchDir" -ForegroundColor Green
        Get-ChildItem $LocalBatchDir -Filter "batch_summary_optimized_*" | ForEach-Object {
            Write-Host "    $($_.Name) ($([math]::Round($_.Length/1KB, 1)) KB)" -ForegroundColor White
        }
        Get-ChildItem $LocalBatchDir -Filter "optimization_results_*" | ForEach-Object {
            Write-Host "    $($_.Name) ($([math]::Round($_.Length/1KB, 1)) KB)" -ForegroundColor White
        }

        Write-Host ""
        Send-TelegramAlert "[COMPLETE] Post-Optimizer Results Downloaded`nBatch: $BatchId`nLocal: $LocalBatchDir"

        Write-Host "=====================================================" -ForegroundColor Green
        Write-Host " RESULTS DOWNLOADED SUCCESSFULLY" -ForegroundColor Green
        Write-Host "=====================================================" -ForegroundColor Green
        Write-Host ""
        exit 0
    }

    # Not ready yet -- show status
    $statusStr = "  [$pollCount] Waiting... (~${elapsed} min elapsed)"

    # Check if VM still exists
    $vmStatus = gcloud compute instances describe $VmName --zone=$Zone --format="value(status)" 2>$null
    if ($vmStatus) {
        $vmStatusStr = $vmStatus.ToString().Trim()
        if ($vmStatusStr -in @("TERMINATED", "STOPPED")) {
            Write-Host "$statusStr | VM: $vmStatusStr -- downloading available results..." -ForegroundColor Yellow

            # Try to download whatever results exist
            if (!(Test-Path $LocalBatchDir)) { New-Item -ItemType Directory -Path $LocalBatchDir -Force | Out-Null }
            gsutil -m cp "$Bucket/$GcsOptPrefix/batch_summary_optimized_*.md" "$LocalBatchDir\" 2>$null
            gsutil -m cp "$Bucket/$GcsOptPrefix/optimization_results_*.json" "$LocalBatchDir\" 2>$null
            gsutil -m cp "$Bucket/$GcsOptPrefix/batch_ensemble_pre_opt.md" "$LocalBatchDir\" 2>$null

            # Download logs
            $logsDir = Join-Path $LocalBatchDir "logs"
            if (!(Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
            gsutil -m cp "$Bucket/$GcsOptPrefix/logs/*" "$logsDir\" 2>$null

            # Check if optimized report was produced
            $hasReport = Test-Path (Join-Path $LocalBatchDir "batch_summary_optimized_*.md")

            # Delete the VM to free resources
            Write-Host "  Deleting $vmStatusStr VM: $VmName ..." -ForegroundColor Yellow
            gcloud compute instances delete $VmName --zone=$Zone --quiet 2>$null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  VM deleted." -ForegroundColor Green
            } else {
                Write-Host "  VM delete returned non-zero (may already be gone)." -ForegroundColor Gray
            }

            if ($hasReport) {
                Write-Host ""
                Write-Host "  Downloaded to: $LocalBatchDir" -ForegroundColor Green
                Send-TelegramAlert "[COMPLETE] Post-Optimizer Results Downloaded`nBatch: $BatchId`nLocal: $LocalBatchDir"
                Write-Host "=====================================================" -ForegroundColor Green
                Write-Host " RESULTS DOWNLOADED SUCCESSFULLY" -ForegroundColor Green
                Write-Host "=====================================================" -ForegroundColor Green
                exit 0
            } else {
                Write-Host ""
                Write-Host "  ERROR: VM $vmStatusStr but no optimized report found on GCS!" -ForegroundColor Red
                $crashReason = Get-CrashReason
                Write-Host "  Crash reason:" -ForegroundColor Red
                Write-Host $crashReason -ForegroundColor Red
                $reasonShort = $crashReason.Substring(0, [Math]::Min(1500, $crashReason.Length))
                Send-TelegramAlert "[ERROR] Post-Optimizer VM $vmStatusStr but no reports found`nBatch: $BatchId`nReason:`n$reasonShort`nGCS: $Bucket/$GcsOptPrefix/"
                exit 1
            }
        } else {
            Write-Host "$statusStr | VM: $vmStatusStr" -ForegroundColor DarkGray
        }
    } else {
        Write-Host "$statusStr | VM deleted (checking GCS one more time...)" -ForegroundColor Yellow
        # VM gone but no reports -- give it one more check
        Start-Sleep -Seconds 10
        $finalCheck = gsutil ls "$Bucket/$GcsOptPrefix/batch_summary_optimized_*.md" 2>$null
        if (!$finalCheck) {
            Write-Host "  ERROR: VM deleted but no reports found on GCS!" -ForegroundColor Red
            $crashReason = Get-CrashReason
            Write-Host "  Crash reason:" -ForegroundColor Red
            Write-Host $crashReason -ForegroundColor Red
            $reasonShort = $crashReason.Substring(0, [Math]::Min(1500, $crashReason.Length))
            Send-TelegramAlert "[ERROR] Post-Optimizer VM deleted but no reports found`nBatch: $BatchId`nReason:`n$reasonShort`nGCS: $Bucket/$GcsOptPrefix/"
            exit 1
        }
        continue  # Will download on next iteration
    }

    Start-Sleep -Seconds $pollInterval
}

