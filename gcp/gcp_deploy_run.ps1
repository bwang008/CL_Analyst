<#
.SYNOPSIS
    Uploads code and data to the GCP VM and launches an Optuna search.
.DESCRIPTION
    SCP's project code and parquet dataset to the VM, then starts the
    Optuna search in a detached tmux session. Safe to disconnect after launch.
.EXAMPLE
    .\gcp_deploy_run.ps1 -DataPath "C:\CL_Analyst_Data\data\processed\CL_set_08.parquet"
    .\gcp_deploy_run.ps1 -DataPath "..." -NTrials 150 -NJobs 8 -StudyName "exp_wide"
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
    [string]$ProjectDir = ""
)

$ErrorActionPreference = "Stop"

# Auto-detect project directory (parent of gcp/)
if (-not $ProjectDir) {
    $ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

# Validate inputs
if (-not (Test-Path $DataPath)) {
    Write-Host "ERROR: Data file not found: $DataPath" -ForegroundColor Red
    exit 1
}

$DataFileName = Split-Path -Leaf $DataPath

# SSH username on GCP defaults to local Windows username
$GcpUser = $env:USERNAME
$RemoteDataPath = "/home/${GcpUser}/data/${DataFileName}"

# Auto-generate study name from dataset + metric
if (-not $StudyName) {
    $dataset = [System.IO.Path]::GetFileNameWithoutExtension($DataFileName)
    $StudyName = "wf_v2_long_${MlMetric}_${dataset}"
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
Write-Host "  Project:    $ProjectDir"
Write-Host "=====================================================" -ForegroundColor Cyan

# --- [1/5] Check VM is running ---
Write-Host "`n[1/5] Checking VM status..."
$status = gcloud compute instances describe $VmName --zone=$Zone `
    --format="get(status)" 2>$null
if ($status -ne "RUNNING") {
    Write-Host "  VM status: $status — starting it..." -ForegroundColor Yellow
    gcloud compute instances start $VmName --zone=$Zone --quiet
    Write-Host "  Waiting for VM to boot..." -ForegroundColor Gray
    Start-Sleep -Seconds 30

    # Wait for startup script
    $maxWait = 180
    $elapsed = 0
    while ($elapsed -lt $maxWait) {
        $ready = gcloud compute ssh $VmName --zone=$Zone `
            --command="test -f /tmp/startup_done && echo READY" `
            --quiet 2>$null
        if ($ready -match "READY") { break }
        Start-Sleep -Seconds 10
        $elapsed += 10
    }
}
Write-Host "  VM is running" -ForegroundColor Green

# --- [2/5] Upload project code ---
Write-Host "`n[2/5] Uploading project code..."

# Create directories on VM
gcloud compute ssh $VmName --zone=$Zone --quiet `
    --command="mkdir -p ~/project ~/data" 2>$null

# Upload key directories
$dirs = @("agent", "src", "configs", "gcp")
foreach ($dir in $dirs) {
    $localDir = Join-Path $ProjectDir $dir
    if (Test-Path $localDir) {
        Write-Host "  Uploading $dir/..."
        gcloud compute scp --recurse --compress `
            "$localDir" "${VmName}:~/project/" `
            --zone=$Zone --quiet 2>$null
    }
}

# Upload root-level files needed by the scripts
$rootFiles = @("experiments.json")
foreach ($f in $rootFiles) {
    $localFile = Join-Path $ProjectDir $f
    if (Test-Path $localFile) {
        gcloud compute scp "$localFile" "${VmName}:~/project/$f" `
            --zone=$Zone --quiet 2>$null
    }
}

Write-Host "  Code uploaded!" -ForegroundColor Green

# --- [3/5] Upload dataset ---
Write-Host "`n[3/5] Uploading dataset ($DataFileName)..."
Write-Host "  (This may take 2-3 minutes for ~200MB)" -ForegroundColor Gray

gcloud compute scp "$DataPath" "${VmName}:${RemoteDataPath}" `
    --zone=$Zone --quiet

Write-Host "  Data uploaded!" -ForegroundColor Green

# --- [4/5] Build and launch Optuna command ---
Write-Host "`n[4/5] Launching Optuna in detached tmux session..."

$optunaArgs = "--target $Target --data $RemoteDataPath --ml-metric $MlMetric --n-trials $NTrials --n-jobs $NJobs --study-name $StudyName --train-cutoff-date $TrainCutoffDate"

if ($StrategyConfig) {
    $optunaArgs += " --strategy-config configs/strategies/$StrategyConfig"
}

# Kill any existing tmux session, then launch new one
$launchCmd = "tmux kill-session -t optuna 2>/dev/null; tmux new-session -d -s optuna 'bash ~/project/gcp/vm_run_optuna.sh $optunaArgs'"

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
Write-Host " SEARCH LAUNCHED — SAFE TO DISCONNECT" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""
Write-Host "The search is running in a detached tmux session."
Write-Host "You can close this terminal and shut down your PC."
Write-Host ""
Write-Host 'Useful commands:' -ForegroundColor Cyan
Write-Host '  Check status:     .\gcp\gcp_check_status.ps1'
Write-Host "  View live output: gcloud compute ssh $VmName --command='tmux attach -t optuna'"
Write-Host '  Download results: .\gcp\gcp_teardown.ps1'
Write-Host ""
Write-Host "Results auto-upload to GCS when the search completes."
Write-Host ""
