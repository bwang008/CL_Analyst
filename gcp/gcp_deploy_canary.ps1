<#
.SYNOPSIS
    Provisions a canary VM, uploads code + downloads data from GCS, and launches the light canary pipeline.
.DESCRIPTION
    One-command deployment for the "canary" smoke test:
    - Provisions optuna-runner-canary (96-core SPOT by default)
    - Uploads all code files needed for Optuna search + E2E pipeline
    - Downloads set_10 data from GCS (already uploaded) to the VM
    - Launches vm_canary_run.sh in a detached tmux session
    - Auto-shutdown after completion (unless -NoShutdown)
.EXAMPLE
    .\gcp_deploy_canary.ps1
    .\gcp_deploy_canary.ps1 -ProvisioningModel STANDARD
    .\gcp_deploy_canary.ps1 -NoShutdown
#>

param(
    [string]$VmName = "optuna-runner-canary",
    [string]$MachineType = "n2-highcpu-48",
    [string]$Zone = "us-central1-a",
    [int]$DiskSizeGB = 50,
    [string]$Project = "cltrainer",
    [string]$ProvisioningModel = "SPOT",
    [string]$GcsDataPath = "gs://cltrainer-optuna-results/data/cl-5m_bk_set_10.parquet",
    [string]$StrategyConfig = "ensemble4.json",
    [string]$Metrics = "logloss,f0.5",
    [string]$TargetLong = "",
    [string]$TargetShort = "",
    [switch]$NoShutdown,
    [switch]$SkipProvision
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
Write-Host " DEPLOY CANARY (LIGHT) PIPELINE" -ForegroundColor Magenta
Write-Host "=====================================================" -ForegroundColor Magenta
Write-Host "  VM:            $VmName"
Write-Host "  Machine:       $MachineType"
Write-Host "  Pricing:       $ProvisioningModel"
Write-Host "  Data (GCS):    $GcsDataPath"
Write-Host "  Strategy:      $StrategyConfig"
Write-Host "  Auto-Shutdown: $(-not $NoShutdown)"
Write-Host "=====================================================" -ForegroundColor Magenta

# --- [1/6] Provision VM (or reuse existing) ---
if (-not $SkipProvision) {
    Write-Host "`n[1/6] Provisioning canary VM..."
    
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
        
        # Build gcloud create args - termination-action only valid for SPOT
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
            Write-Host "  Check quota: gcloud compute regions describe $Zone --format='table(quotas)'"
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
    --command="mkdir -p $RemoteProject/agent $RemoteProject/src/live_execution/strategies $RemoteProject/gcp $RemoteProject/configs/strategies $RemoteProject/models/optuna_studies $RemoteProject/reports $RemoteHome/data" 2>$null

# --- [3/6] Upload code ---
Write-Host "`n[3/6] Uploading code..."

$codeFiles = @(
    @{ Local = "agent\optuna_lgbm_search_v2.py"; Remote = "agent/" },
    @{ Local = "agent\experiment_runner.py";     Remote = "agent/" },
    @{ Local = "agent\backtest_engine.py";       Remote = "agent/" },
    @{ Local = "agent\__init__.py";              Remote = "agent/" },
    @{ Local = "src\util.py";                    Remote = "src/" },
    @{ Local = "src\__init__.py";                Remote = "src/" },
    @{ Local = "src\live_execution\__init__.py";                   Remote = "src/live_execution/" },
    @{ Local = "src\live_execution\strategies\__init__.py";        Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\execution_models.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\configurable_strategy.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\buy70_sized_manatee.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "gcp\vm_canary_run.sh";           Remote = "gcp/" },
    @{ Local = "gcp\vm_e2e_pipeline.py";         Remote = "gcp/" }
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

# --- [5/6] Launch canary run ---
Write-Host "`n[5/6] Launching canary pipeline in tmux..."

$shutdownFlag = if ($NoShutdown) { "" } else { "--shutdown" }
$datasetName = [System.IO.Path]::GetFileNameWithoutExtension($DataFileName)
$targetFlags = ""
if ($TargetLong) { $targetFlags += " --target-long=$TargetLong" }
if ($TargetShort) { $targetFlags += " --target-short=$TargetShort" }
$launchCmd = "tmux kill-session -t canary 2>/dev/null; tmux new-session -d -s canary 'bash $RemoteProject/gcp/vm_canary_run.sh $shutdownFlag --dataset=$datasetName --metrics=$Metrics$targetFlags'"
gcloud compute ssh $VmName --zone=$Zone --command=$launchCmd --quiet 2>$null

Write-Host "  Canary pipeline launched!" -ForegroundColor Green

# --- [6/6] Verify ---
Write-Host "`n[6/6] Verifying tmux session..."
Start-Sleep -Seconds 3
$tmuxCheck = gcloud compute ssh $VmName --zone=$Zone `
    --command="tmux has-session -t canary 2>/dev/null && echo RUNNING" `
    --quiet 2>$null

if ($tmuxCheck -match "RUNNING") {
    Write-Host "  tmux session 'canary' is active!" -ForegroundColor Green
} else {
    Write-Host "  WARNING: tmux session may not have started." -ForegroundColor Yellow
    Write-Host "  Debug with: gcloud compute ssh $VmName --command='tmux attach -t canary'"
}

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host " CANARY PIPELINE LAUNCHED" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  VM:           $VmName"
Write-Host "  Expected:     ~30 minutes"
$gcsOut = "gs://cltrainer-optuna-results/canary/"
Write-Host "  GCS output:   $gcsOut"
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Cyan
$dlCmd = "gsutil -m cp -r $gcsOut ./"
Write-Host "  Check status:     .\gcp\gcp_check_status.ps1 -VmName $VmName"
Write-Host "  View live output: gcloud compute ssh $VmName --zone=$Zone --command='tmux attach -t canary'"
Write-Host "  Download results: $dlCmd"
Write-Host ""
