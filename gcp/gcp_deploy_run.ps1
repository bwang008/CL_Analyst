<#
.SYNOPSIS
    Uploads code and data to the GCP VM and launches an Optuna search.
.DESCRIPTION
    Uploads ONLY the files needed for Optuna search (not the entire project).
    Launches the search in a detached tmux session. Safe to disconnect after.
.EXAMPLE
    .\gcp_deploy_run.ps1 -DataPath "C:\CL_Analyst_Data\data\processed\CL_set_08.parquet"
    .\gcp_deploy_run.ps1 -DataPath "..." -NTrials 150 -NJobs 4 -StudyName "exp_wide"
    .\gcp_deploy_run.ps1 -DataPath "..." -MlMetric sharpe -StrategyConfig ensemble3.json
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$DataPath,

    [string]$VmName = "optuna-runner",
    [string]$Zone = "us-central1-a",
    [string]$Target = "TARGET_TRIPLE_2x1_24H_LONG",
    [string]$MlMetric = "logloss",
    [int]$NTrials = 100,
    [int]$NJobs = 4,
    [string]$StudyName = "",
    [string]$TrainCutoffDate = "2022-01-01",
    [string]$StrategyConfig = "",
    [string]$ProjectDir = "",
    [switch]$SkipDataUpload
)

# Add gcloud to PATH if not already there
$gcloudBin = "C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin"
if ($env:PATH -notlike "*$gcloudBin*") {
    $env:PATH = "$gcloudBin;$env:PATH"
}

$ErrorActionPreference = "Stop"

# Auto-detect project directory (parent of gcp/)
if (-not $ProjectDir) {
    $ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

# Validate data file
if (-not (Test-Path $DataPath)) {
    Write-Host "ERROR: Data file not found: $DataPath" -ForegroundColor Red
    exit 1
}

$DataFileName = Split-Path -Leaf $DataPath
$GcpUser = $env:USERNAME
$RemoteHome = "/home/${GcpUser}"
$RemoteProject = "${RemoteHome}/project"
$RemoteDataPath = "${RemoteHome}/data/${DataFileName}"

# Auto-generate study name
if (-not $StudyName) {
    $dataset = [System.IO.Path]::GetFileNameWithoutExtension($DataFileName)
    $dirTag = if ($Target -match "LONG$") { "long" } elseif ($Target -match "SHORT$") { "short" } else { "multi" }
    $StudyName = "wf_v2_${dirTag}_${MlMetric}_${dataset}"
}

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " DEPLOY & RUN OPTUNA ON GCP" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "  VM:         $VmName"
Write-Host "  Data:       $DataFileName"
Write-Host "  Target:     $Target"
Write-Host "  Metric:     $MlMetric"
Write-Host "  Trials:     $NTrials"
Write-Host "  Workers:    $NJobs"
Write-Host "  Study:      $StudyName"
Write-Host "  Cutoff:     $TrainCutoffDate"
Write-Host "=====================================================" -ForegroundColor Cyan

# --- [1/5] Check VM is running ---
Write-Host "`n[1/5] Checking VM status..."
$status = gcloud compute instances describe $VmName --zone=$Zone `
    --format="get(status)" 2>$null
if ($status -ne "RUNNING") {
    Write-Host "  VM status: $status - starting..." -ForegroundColor Yellow
    gcloud compute instances start $VmName --zone=$Zone --quiet
    Write-Host "  Waiting for VM to boot..." -ForegroundColor Gray
    Start-Sleep -Seconds 30
    $maxWait = 180; $elapsed = 0
    while ($elapsed -lt $maxWait) {
        $ready = gcloud compute ssh $VmName --zone=$Zone `
            --command="test -f /tmp/startup_done && echo READY" `
            --quiet 2>$null
        if ($ready -match "READY") { break }
        Start-Sleep -Seconds 10; $elapsed += 10
    }
}
Write-Host "  VM is running" -ForegroundColor Green

# --- [2/5] Upload code (minimal files only) ---
Write-Host "`n[2/5] Uploading code (minimal file set)..."

# Create directory structure on VM
gcloud compute ssh $VmName --zone=$Zone --quiet `
    --command="mkdir -p $RemoteProject/agent $RemoteProject/src $RemoteProject/gcp $RemoteProject/models/optuna_studies $RemoteProject/reports $RemoteHome/data" 2>$null

# Exact files needed for Optuna search
$codeFiles = @(
    @{ Local = "agent\optuna_lgbm_search_v2.py"; Remote = "agent/" },
    @{ Local = "agent\experiment_runner.py";     Remote = "agent/" },
    @{ Local = "agent\backtest_engine.py";       Remote = "agent/" },
    @{ Local = "agent\__init__.py";              Remote = "agent/" },
    @{ Local = "src\util.py";                    Remote = "src/" },
    @{ Local = "src\__init__.py";                Remote = "src/" },
    @{ Local = "experiments.json";               Remote = "" },
    @{ Local = "gcp\vm_run_optuna.sh";           Remote = "gcp/" }
)

foreach ($file in $codeFiles) {
    $localPath = Join-Path $ProjectDir $file.Local
    $remotePath = "$RemoteProject/$($file.Remote)"
    if (Test-Path $localPath) {
        gcloud compute scp "$localPath" "${VmName}:${remotePath}" `
            --zone=$Zone --quiet 2>$null
    }
}

# Upload strategy config if sharpe mode
if ($StrategyConfig) {
    gcloud compute ssh $VmName --zone=$Zone --quiet `
        --command="mkdir -p $RemoteProject/configs/strategies" 2>$null
    $configPath = Join-Path $ProjectDir "configs\strategies\$StrategyConfig"
    if (Test-Path $configPath) {
        gcloud compute scp "$configPath" "${VmName}:${RemoteProject}/configs/strategies/" `
            --zone=$Zone --quiet 2>$null
        Write-Host "  Uploaded strategy config: $StrategyConfig"
    }
}

Write-Host "  Code uploaded! (8 files)" -ForegroundColor Green

# --- [3/5] Upload dataset ---
if (-not $SkipDataUpload) {
    $dataSizeMB = [math]::Round((Get-Item $DataPath).Length / 1MB, 1)
    Write-Host "`n[3/5] Uploading dataset ($DataFileName, ${dataSizeMB}MB)..."
    Write-Host "  (This takes 2-3 minutes)" -ForegroundColor Gray
    gcloud compute scp "$DataPath" "${VmName}:${RemoteDataPath}" `
        --zone=$Zone --compress
    Write-Host "  Data uploaded!" -ForegroundColor Green
} else {
    Write-Host "`n[3/5] Skipping data upload (-SkipDataUpload)" -ForegroundColor Yellow
}

# --- [4/5] Launch in tmux ---
Write-Host "`n[4/5] Launching Optuna in detached tmux session..."

$optunaArgs = "--target $Target --data $RemoteDataPath --ml-metric $MlMetric --n-trials $NTrials --n-jobs $NJobs --study-name $StudyName --train-cutoff-date $TrainCutoffDate"
if ($StrategyConfig) {
    $optunaArgs += " --strategy-config configs/strategies/$StrategyConfig"
}

$launchCmd = "tmux kill-session -t optuna 2>/dev/null; tmux new-session -d -s optuna 'bash $RemoteProject/gcp/vm_run_optuna.sh $optunaArgs'"
gcloud compute ssh $VmName --zone=$Zone --command=$launchCmd --quiet 2>$null

Write-Host "  Optuna search launched!" -ForegroundColor Green

# --- [5/5] Verify ---
Write-Host "`n[5/5] Verifying tmux session..."
Start-Sleep -Seconds 3
$tmuxCheck = gcloud compute ssh $VmName --zone=$Zone `
    --command="tmux has-session -t optuna 2>/dev/null && echo RUNNING" `
    --quiet 2>$null

if ($tmuxCheck -match "RUNNING") {
    Write-Host "  tmux session 'optuna' is active" -ForegroundColor Green
} else {
    Write-Host "  WARNING: tmux session may not have started." -ForegroundColor Yellow
    Write-Host "  Debug: gcloud compute ssh $VmName --command='tmux attach -t optuna'"
}

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host " SEARCH LAUNCHED - SAFE TO DISCONNECT" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""
Write-Host "You can close this terminal and shut down your PC."
Write-Host ""
Write-Host 'Useful commands:' -ForegroundColor Cyan
Write-Host '  Check status:     .\gcp\gcp_check_status.ps1'
Write-Host "  View live output: gcloud compute ssh $VmName --command='tmux attach -t optuna'"
Write-Host '  Download results: .\gcp\gcp_teardown.ps1'
Write-Host ""
