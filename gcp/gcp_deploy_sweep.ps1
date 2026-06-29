<#
.SYNOPSIS
    Provisions a sweep VM, uploads code + downloads data from GCS, and launches the sweep pipeline.
.DESCRIPTION
    Unified deployment for batch sweep experiments.
#>

param (
    [Parameter(Mandatory=$true)][string]$VmName,
    [Parameter(Mandatory=$true)][string]$MasterConfig,
    [string]$Zone = "us-east4-a",
    [string]$MachineType = "c2-standard-16",
    [string]$ProvisioningModel = "STANDARD",
    [string]$JobName = ""
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

# Internal/default variables
$DiskSizeGB = 100
$SkipProvision = $false
$NoShutdown = $false
$Project = gcloud config get-value project --quiet

# Parse the $MasterConfig JSON directly
$cfg = Get-Content $MasterConfig -Raw | ConvertFrom-Json
$symbol = $cfg.symbol
$datasetVersion = $cfg.data_workflow.dataset_version
$datasetName = "${symbol}_${datasetVersion}.parquet"
$GcsDataPath = "gs://cltrainer-data/processed/$datasetName"
$StrategyConfig = Split-Path -Leaf $cfg.execution_workflow.strategy_config_path

$DataFileName = Split-Path -Leaf $GcsDataPath

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Magenta
Write-Host " DEPLOY SWEEP PIPELINE" -ForegroundColor Magenta
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
    $maxWait = 600; $elapsed = 0
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
    if ($elapsed -ge $maxWait) {
        Write-Host "  WARNING: Reached ${maxWait}s timeout. Proceeding anyway." -ForegroundColor Yellow
    } else {
        Write-Host "  VM is ready!" -ForegroundColor Green
    }
} else {
    Write-Host "`n[1/6] Skipping provision (-SkipProvision)" -ForegroundColor Yellow
}

# --- [2/6] Create directory structure ---
Write-Host "`n[2/6] Creating directory structure..."
gcloud compute ssh $VmName --zone=$Zone --quiet `
    --command="mkdir -p $RemoteProject/agent $RemoteProject/src/live_execution/strategies $RemoteProject/src/features $RemoteProject/gcp $RemoteProject/configs/strategies $RemoteProject/configs/sweeps $RemoteProject/models/optuna_studies $RemoteProject/reports $RemoteHome/data" 2>$null

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
    @{ Local = "src\data_paths.py";              Remote = "src/" },
    @{ Local = "src\data_processor.py";          Remote = "src/" },
    @{ Local = "src\live_execution\__init__.py";                   Remote = "src/live_execution/" },
    @{ Local = "src\live_execution\strategy_config.py";            Remote = "src/live_execution/" },
    @{ Local = "src\live_execution\execution_guard.py";            Remote = "src/live_execution/" },
    @{ Local = "src\live_execution\strategies\__init__.py";        Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\execution_models.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\configurable_strategy.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "src\live_execution\strategies\buy70_sized_manatee.py"; Remote = "src/live_execution/strategies/" },
    @{ Local = "src\features\__init__.py";       Remote = "src/features/" },
    @{ Local = "src\features\feature_buckets.py"; Remote = "src/features/" },
    @{ Local = "gcp\vm_sweep_run.sh";           Remote = "gcp/" },
    @{ Local = "gcp\vm_e2e_pipeline.py";         Remote = "gcp/" },
    @{ Local = "gcp\orchestrator.py";            Remote = "gcp/" }
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
        try {
            gcloud compute scp "$localPath" "${VmName}:${remotePath}" --zone=$Zone --quiet 2>$null
        } catch {
            Write-Host "  Retrying scp for $($file.Local)..." -ForegroundColor Yellow
            Start-Sleep -Seconds 2
            try { gcloud compute scp "$localPath" "${VmName}:${remotePath}" --zone=$Zone --quiet 2>$null } catch { Write-Host "  Failed to copy $($file.Local)" -ForegroundColor Red }
        }
    } else {
        Write-Host "  WARNING: Missing $($file.Local)" -ForegroundColor Yellow
    }
}

# Upload resolved master config JSON to the VM
$remoteConfigDir = "$RemoteProject/configs/sweeps"
$remoteConfigPath = "${remoteConfigDir}/$(Split-Path -Leaf $MasterConfig)"
try {
    gcloud compute scp $MasterConfig "${VmName}:${remoteConfigPath}" --zone=$Zone --quiet 2>$null
} catch {
    Write-Host "  Retrying scp for $(Split-Path -Leaf $MasterConfig)..." -ForegroundColor Yellow
    Start-Sleep -Seconds 2
    try { gcloud compute scp $MasterConfig "${VmName}:${remoteConfigPath}" --zone=$Zone --quiet 2>$null } catch { Write-Host "  Failed to copy MasterConfig" -ForegroundColor Red }
}
Write-Host "  Uploaded MasterConfig: $(Split-Path -Leaf $MasterConfig)"

# Upload strategy config
$configPath = Join-Path $ProjectDir "configs\strategies\$StrategyConfig"
if (Test-Path $configPath) {
    try {
        gcloud compute scp "$configPath" "${VmName}:${RemoteProject}/configs/strategies/" --zone=$Zone --quiet 2>$null
    } catch {
        Write-Host "  Retrying scp for $StrategyConfig..." -ForegroundColor Yellow
        Start-Sleep -Seconds 2
        try { gcloud compute scp "$configPath" "${VmName}:${RemoteProject}/configs/strategies/" --zone=$Zone --quiet 2>$null } catch { Write-Host "  Failed to copy $StrategyConfig" -ForegroundColor Red }
    }
    Write-Host "  Uploaded strategy config: $StrategyConfig"
}

Write-Host "  Code uploaded!" -ForegroundColor Green

# Fix CRLF line endings on shell scripts (Windows git checkout produces CRLF which breaks bash)
Write-Host "  Fixing line endings on shell scripts..."
try {
    gcloud compute ssh $VmName --zone=$Zone --command="find $RemoteProject -name '*.sh' -exec sed -i 's/\r$//' {} + && chmod +x $RemoteProject/gcp/*.sh" --quiet 2>$null
} catch {}
Write-Host "  Line endings fixed." -ForegroundColor Green

# --- [4/6] Download data from GCS to VM ---
Write-Host "`n[4/6] Downloading data from GCS to VM..."
Write-Host "  Source: $GcsDataPath"
$downloadCmd = "gsutil cp '$GcsDataPath' '$RemoteHome/data/$DataFileName'"
try { gcloud compute ssh $VmName --zone=$Zone --command=$downloadCmd --quiet } catch { Write-Host "  Ignoring ssh stderr..." -ForegroundColor Gray }
Write-Host "  Data ready on VM!" -ForegroundColor Green

# --- [5/6] Launch sweep run ---
Write-Host "`n[5/6] Launching sweep pipeline in tmux..."

$launchCmd = "tmux kill-session -t sweep 2>/dev/null; tmux new-session -d -s sweep 'bash $RemoteProject/gcp/vm_sweep_run.sh --master-config=configs/sweeps/$(Split-Path -Leaf $MasterConfig) --gcs-data-bucket=gs://cltrainer-data/processed'"

# Execute and capture both streams with explicit exit code handling
$launchOutput = gcloud compute ssh $VmName --zone=$Zone --command=$launchCmd 2>&1
$sshExitCode = $LASTEXITCODE

if ($sshExitCode -ne 0) {
    Write-Host "  WARNING: SSH launch returned exit code $sshExitCode" -ForegroundColor Yellow
    Write-Host "  Output: $($launchOutput | Out-String)" -ForegroundColor DarkGray
}

# --- [6/6] Verify ---
Write-Host "`n[6/6] Verifying tmux session..."
Start-Sleep -Seconds 10
$tmuxCheck = gcloud compute ssh $VmName --zone=$Zone `
    --command="tmux has-session -t sweep 2>/dev/null && echo RUNNING" `
    --quiet 2>$null

if ($sshExitCode -eq 0 -and $tmuxCheck -match "RUNNING") {
    Write-Host "  tmux session 'sweep' is active!" -ForegroundColor Green
} else {
    Write-Host "  FATAL: Failed to initialize remote execution environment." -ForegroundColor Red
    Write-Host "  SSH Exit Code: $sshExitCode" -ForegroundColor Red
    Write-Host "  Output: $($launchOutput | Out-String)" -ForegroundColor DarkGray
    Write-Host "  Debug with: gcloud compute ssh $VmName --command='tmux attach -t sweep'"
    exit 1
}

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host " SWEEP PIPELINE LAUNCHED" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  VM:           $VmName"
Write-Host "  Expected:     ~hours"
$gcsOut = "gs://cltrainer-optuna-results/$JobName/"
Write-Host "  GCS output:   $gcsOut"
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Cyan
$dlCmd = "gsutil -m cp -r $gcsOut ./"
Write-Host "  Check status:     .\gcp\gcp_check_status.ps1 -VmName $VmName"
Write-Host "  View live output: gcloud compute ssh $VmName --zone=$Zone --command='tmux attach -t sweep'"
Write-Host "  Download results: $dlCmd"
Write-Host ""
