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
    [string]$VmName = "optuna-post-optimizer",
    [string]$MachineType = "n2-standard-32",
    [string]$Zone = "us-central1-a",
    [int]$DiskSizeGB = 50,
    [string]$Project = "cltrainer",
    [string]$ProvisioningModel = "STANDARD",
    [int]$NTrials = 500,
    [int]$HoldoutMonths = 4,
    [int]$Workers = 0,
    [switch]$NoShutdown,
    [switch]$NoMonitor,
    [switch]$DisableTelegram,
    [string]$Objective = "both"
)

# Add gcloud to PATH if not already there
$gcloudBin = "C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin"
if ($env:PATH -notlike "*$gcloudBin*") {
    $env:PATH = "$gcloudBin;$env:PATH"
}

$ErrorActionPreference = "Stop"
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
        "--scopes=storage-full",
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
$maxWait = 300; $elapsed = 0
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
Write-Host "`n[3/7] Creating directory structure..."
gcloud compute ssh $VmName --zone=$Zone --quiet `
    --command="mkdir -p $RemoteProject/agent $RemoteProject/src/live_execution/strategies $RemoteProject/src/features $RemoteProject/gcp $RemoteProject/configs/strategies $RemoteProject/reports/batch_runs $RemoteProject/data/processed" 2>$null

# --- [4/7] Upload code ---
Write-Host "`n[4/7] Uploading code..."

$codeFiles = @(
    @{ Local = "agent\batch_post_optimizer.py";  Remote = "agent/" },
    @{ Local = "agent\generate_batch_configs.py"; Remote = "agent/" },
    @{ Local = "agent\strategy_optimizer.py";    Remote = "agent/" },
    @{ Local = "agent\backtest_engine.py";       Remote = "agent/" },
    @{ Local = "agent\__init__.py";              Remote = "agent/" },
    @{ Local = "src\util.py";                    Remote = "src/" },
    @{ Local = "src\__init__.py";                Remote = "src/" },
    @{ Local = "src\LGBMLearner.py";             Remote = "src/" },
    @{ Local = "src\data_processor.py";          Remote = "src/" },
    @{ Local = "src\data_paths.py";              Remote = "src/" },
    @{ Local = "src\live_execution\__init__.py";                   Remote = "src/live_execution/" },
    @{ Local = "src\live_execution\strategy_config.py";            Remote = "src/live_execution/" },
    @{ Local = "src\live_execution\strategies\__init__.py";        Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\execution_models.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\configurable_strategy.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\buy70_sized_manatee.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "src\features\__init__.py";       Remote = "src/features/" },
    @{ Local = "src\features\feature_buckets.py"; Remote = "src/features/" },
    @{ Local = "gcp\vm_post_optimize.sh";        Remote = "gcp/" }
)

# Upload .env for Telegram (if exists)
$envFile = Join-Path $ProjectDir ".env"
if (Test-Path $envFile) {
    $codeFiles += @{ Local = ".env"; Remote = "" }
}

foreach ($file in $codeFiles) {
    $localPath = Join-Path $ProjectDir $file.Local
    $remotePath = "$RemoteProject/$($file.Remote)"
    if (Test-Path $localPath) {
        gcloud compute scp "$localPath" "${VmName}:${remotePath}" `
            --zone=$Zone --quiet 2>$null
    } else {
        Write-Host "  WARNING: Missing $($file.Local)" -ForegroundColor Yellow
    }
}
Write-Host "  Code uploaded!" -ForegroundColor Green

# Fix CRLF line endings on shell scripts (Windows git checkout produces CRLF which breaks bash)
Write-Host "  Fixing line endings on shell scripts..."
try {
    gcloud compute ssh $VmName --zone=$Zone --command="find $RemoteProject -name '*.sh' -exec sed -i 's/\r$//' {} + && chmod +x $RemoteProject/gcp/*.sh" --quiet 2>$null
} catch {}
Write-Host "  Line endings fixed." -ForegroundColor Green

# --- [5/7] Launch optimizer ---
Write-Host "`n[5/7] Launching post-optimizer in tmux..."

$shutdownFlag = if ($NoShutdown) { "" } else { "--shutdown" }
$launchCmd = "tmux kill-session -t optimizer 2>/dev/null; tmux new-session -d -s optimizer 'bash $RemoteProject/gcp/vm_post_optimize.sh --batch-id=$BatchId --n-trials=$NTrials --holdout-months=$HoldoutMonths --workers=$Workers --objective=$Objective $shutdownFlag'"
gcloud compute ssh $VmName --zone=$Zone --command=$launchCmd --quiet 2>$null

Write-Host "  Optimizer launched!" -ForegroundColor Green

# --- [6/7] Verify tmux session ---
Write-Host "`n[6/7] Verifying tmux session..."
Start-Sleep -Seconds 3
$tmuxCheck = gcloud compute ssh $VmName --zone=$Zone `
    --command="tmux has-session -t optimizer 2>/dev/null && echo RUNNING || echo NOT_RUNNING" `
    --quiet 2>$null

if ($tmuxCheck -match "RUNNING") {
    Write-Host "  tmux session 'optimizer' is active!" -ForegroundColor Green
    Send-TelegramAlert "[STARTING] Post-Optimizer Deploy`nBatch: $BatchId`nTrials: $NTrials | Workers: $Workers | Objective: $Objective`nVM: $VmName"
} else {
    Write-Host "  WARNING: tmux session may not have started." -ForegroundColor Yellow
    Write-Host "  Debug with: gcloud compute ssh $VmName --command='tmux attach -t optimizer'"
    Send-TelegramAlert "[WARNING] Post-Optimizer Deploy Failed`nBatch: $BatchId`nVM: $VmName`ntmux session may not have started."
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

$pollInterval = 120  # seconds
$maxPolls = 180      # 6 hours max wait
$pollCount = 0

while ($pollCount -lt $maxPolls) {
    $pollCount++
    $elapsed = [math]::Round($pollCount * $pollInterval / 60, 0)

    # Check if any optimization report exists on GCS
    $gcsFiles = gsutil ls "$Bucket/$GcsOptPrefix/batch_summary_optimized_*.md" 2>$null
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

    # Not ready yet — show status
    $statusStr = "  [$pollCount] Waiting... (~${elapsed} min elapsed)"

    # Check if VM still exists
    $vmStatus = gcloud compute instances describe $VmName --zone=$Zone --format="value(status)" 2>$null
    if ($vmStatus) {
        Write-Host "$statusStr | VM: $vmStatus" -ForegroundColor DarkGray
    } else {
        Write-Host "$statusStr | VM deleted (checking GCS one more time...)" -ForegroundColor Yellow
        # VM gone but no reports — give it one more check
        Start-Sleep -Seconds 10
        $finalCheck = gsutil ls "$Bucket/$GcsOptPrefix/batch_summary_optimized_*.md" 2>$null
        if (!$finalCheck) {
            Write-Host "  ERROR: VM deleted but no reports found on GCS!" -ForegroundColor Red
            Send-TelegramAlert "[ERROR] Post-Optimizer VM deleted but no reports found`nBatch: $BatchId`nGCS: $Bucket/$GcsOptPrefix/"
            exit 1
        }
        continue  # Will download on next iteration
    }

    Start-Sleep -Seconds $pollInterval
}

Write-Host "  TIMEOUT: Max poll time reached (6 hours)" -ForegroundColor Red
Send-TelegramAlert "[TIMEOUT] Post-Optimizer monitor timed out after 6 hours`nBatch: $BatchId"
exit 1

