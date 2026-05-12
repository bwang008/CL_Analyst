<#
.SYNOPSIS
    Provisions and launches the FULL PRODUCTION ALPHA SEARCH VM.
.DESCRIPTION
    One-command deployment for the production alpha experiments:
    - Provisions optuna-runner-production (48-core, isolated from canary VMs)
    - Uploads all code files + target generation scripts
    - Downloads set_11 data from GCS to VM
    - Launches vm_production_run.sh in a detached tmux session
    - Auto-shutdown after completion (unless -NoShutdown)
.EXAMPLE
    .\gcp\gcp_deploy_production.ps1
    .\gcp\gcp_deploy_production.ps1 -ProvisioningModel STANDARD
    .\gcp\gcp_deploy_production.ps1 -NoShutdown
#>

param(
    [string]$VmName = "optuna-runner-production",
    [string]$MachineType = "n2-highcpu-48",
    [string]$Zone = "us-central1-a",
    [int]$DiskSizeGB = 50,
    [string]$Project = "cltrainer",
    [string]$ProvisioningModel = "STANDARD",
    [string]$GcsDataPath = "gs://cltrainer-optuna-results/data/cl-4h_bk_set_01.parquet",
    [string]$StrategyConfig = "ensemble4.json",
    [int]$NTrials = 30,
    [switch]$NoShutdown,
    [switch]$SkipProvision,
    [switch]$NoMonitor
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
$DataFileName = Split-Path -Leaf $GcsDataPath

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Magenta
Write-Host " DEPLOY FULL PRODUCTION ALPHA SEARCH" -ForegroundColor Magenta
Write-Host "=====================================================" -ForegroundColor Magenta
Write-Host "  VM:            $VmName"
Write-Host "  Machine:       $MachineType"
Write-Host "  Pricing:       $ProvisioningModel"
Write-Host "  Data (GCS):    $GcsDataPath"
Write-Host "  Strategy:      $StrategyConfig"
Write-Host "  Trials/exp:    $NTrials"
Write-Host "  Auto-Shutdown: $(-not $NoShutdown)"
Write-Host "=====================================================" -ForegroundColor Magenta

# --- [1/6] Provision VM (or reuse existing) ---
if (-not $SkipProvision) {
    Write-Host "`n[1/6] Provisioning production VM..."

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
        if ($ProvisioningModel -eq "SPOT") {
            $createArgs += "--provisioning-model=SPOT"
            $createArgs += "--instance-termination-action=STOP"
        }

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
        try {
            $ready = gcloud compute ssh $VmName --zone=$Zone `
                --command="test -f /tmp/startup_done && echo READY" `
                --quiet 2>$null
            if ($ready -match "READY") { break }
        } catch {}
        Write-Host "  Installing... (${elapsed}s elapsed)" -ForegroundColor Gray
    }
    Write-Host "  VM is ready!" -ForegroundColor Green
} else {
    Write-Host "`n[1/6] Skipping provision (-SkipProvision)" -ForegroundColor Yellow
}

# --- [2/6] Create directory structure ---
Write-Host "`n[2/6] Creating directory structure..."
gcloud compute ssh $VmName --zone=$Zone --quiet `
    --command="mkdir -p $RemoteProject/agent $RemoteProject/src/features $RemoteProject/src/live_execution/strategies $RemoteProject/gcp $RemoteProject/scripts $RemoteProject/configs/strategies $RemoteProject/models/optuna_studies $RemoteProject/reports $RemoteHome/data" 2>$null

# --- [3/6] Upload code ---
Write-Host "`n[3/6] Uploading code..."

$codeFiles = @(
    @{ Local = "agent\optuna_lgbm_search_v2.py"; Remote = "agent/" },
    @{ Local = "agent\experiment_runner.py";     Remote = "agent/" },
    @{ Local = "agent\backtest_engine.py";       Remote = "agent/" },
    @{ Local = "agent\__init__.py";              Remote = "agent/" },
    @{ Local = "src\util.py";                    Remote = "src/" },
    @{ Local = "src\__init__.py";                Remote = "src/" },
    @{ Local = "src\LGBMLearner.py";             Remote = "src/" },
    @{ Local = "src\features\__init__.py";       Remote = "src/features/" },
    @{ Local = "src\features\feature_buckets.py"; Remote = "src/features/" },
    @{ Local = "src\live_execution\__init__.py";                   Remote = "src/live_execution/" },
    @{ Local = "src\live_execution\strategies\__init__.py";        Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\execution_models.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\configurable_strategy.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\buy70_sized_manatee.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "gcp\vm_production_run.sh";      Remote = "gcp/" },
    @{ Local = "gcp\vm_e2e_pipeline.py";         Remote = "gcp/" },
    @{ Local = "scripts\generate_tbm_target.py"; Remote = "scripts/" },
    @{ Local = "scripts\generate_vol_target.py"; Remote = "scripts/" }
)

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

# Upload strategy config
$configPath = Join-Path $ProjectDir "configs\strategies\$StrategyConfig"
if (Test-Path $configPath) {
    gcloud compute scp "$configPath" "${VmName}:${RemoteProject}/configs/strategies/" `
        --zone=$Zone --quiet 2>$null
    Write-Host "  Uploaded strategy config: $StrategyConfig"
}

Write-Host "  Code uploaded!" -ForegroundColor Green

# --- [4/6] Download data from GCS to VM ---
Write-Host "`n[4/6] Downloading data from GCS to VM..."
Write-Host "  Source: $GcsDataPath"
$downloadCmd = "gsutil cp '$GcsDataPath' '$RemoteHome/data/$DataFileName'"
gcloud compute ssh $VmName --zone=$Zone --command=$downloadCmd --quiet
Write-Host "  Data ready on VM!" -ForegroundColor Green

# --- [5/6] Launch production run ---
Write-Host "`n[5/6] Launching production alpha search in tmux..."

$shutdownFlag = if ($NoShutdown) { "" } else { "--shutdown" }
$datasetName = [System.IO.Path]::GetFileNameWithoutExtension($DataFileName)

# Fix line endings on the VM (Windows → Unix)
$fixCmd = "sed -i 's/\r$//' $RemoteProject/gcp/vm_production_run.sh $RemoteProject/scripts/generate_tbm_target.py $RemoteProject/scripts/generate_vol_target.py"
gcloud compute ssh $VmName --zone=$Zone --command=$fixCmd --quiet 2>$null

$launchCmd = "tmux kill-session -t production 2>/dev/null; tmux new-session -d -s production 'bash $RemoteProject/gcp/vm_production_run.sh $shutdownFlag --dataset=$datasetName --n-trials=$NTrials'"
gcloud compute ssh $VmName --zone=$Zone --command=$launchCmd --quiet 2>$null

Write-Host "  production pipeline launched!" -ForegroundColor Green

# --- [6/6] Verify ---
Write-Host "`n[6/6] Verifying tmux session..."
Start-Sleep -Seconds 3
$tmuxCheck = gcloud compute ssh $VmName --zone=$Zone `
    --command="tmux has-session -t production 2>/dev/null && echo RUNNING" `
    --quiet 2>$null

if ($tmuxCheck -match "RUNNING") {
    Write-Host "  tmux session 'production' is active!" -ForegroundColor Green
    
    Send-TelegramAlert "[STARTING] Deploy Success: Production Run`nVM: $VmName`nGCS: gs://cltrainer-optuna-results/production_4h_v2/`nCheck status with: .\gcp\gcp_monitor.ps1 -VmName $VmName -GcsPrefix production_4h_v2"
    
    if (-not $NoMonitor) {
        $monScript = Join-Path $ScriptDir "gcp_monitor.ps1"
        $monArgs = @("-ExecutionPolicy", "Bypass", "-File", $monScript, "-VmName", $VmName, "-GcsPrefix", "production_4h_v2", "-ExperimentLabel", "Production Run")
        
        # Check if telegram is configured to pass the flag
        $envPath = Join-Path $ProjectDir ".env"
        $hasTelegram = ((Test-Path $envPath) -and (Select-String -Path $envPath -Pattern "TELEGRAM_BOT_TOKEN" -Quiet))
        if ($hasTelegram -or $env:TELEGRAM_BOT_TOKEN) { $monArgs += "-EnableTelegram" }
        
        Write-Host "  Starting detached monitor for health checks & telegram alerts..." -ForegroundColor Yellow
        Start-Process powershell -WindowStyle Hidden -ArgumentList $monArgs
    } else {
        Write-Host "  Skipping detached monitor launch (-NoMonitor specified)." -ForegroundColor Yellow
    }
} else {
    Write-Host "  WARNING: tmux session may not have started." -ForegroundColor Yellow
    Write-Host "  Debug with: gcloud compute ssh $VmName --command='tmux attach -t production'"
    Send-TelegramAlert "[WARNING] Deploy Warning: Production Run`nVM: $VmName`ntmux session may not have started. Check logs."
}

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host " production ALPHA SEARCH LAUNCHED" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  VM:           $VmName"
Write-Host "  Expected:     ~2-3 hours (3 experiments × 30 trials)"
$gcsOut = "gs://cltrainer-optuna-results/production_4h_v2/"
Write-Host "  GCS output:   $gcsOut"
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Cyan
Write-Host "  Check status:     gsutil cat ${gcsOut}STATUS.json"
Write-Host "  View live output: gcloud compute ssh $VmName --zone=$Zone --command='tmux attach -t production'"
Write-Host "  Download results: gsutil -m cp -r ${gcsOut} ./production_4h_v2_results/"
Write-Host "  Monitor:          .\gcp\gcp_monitor.ps1 -VmName $VmName -GcsPrefix production_4h_v2 -PollIntervalSeconds 120"
Write-Host ""
