<#
.SYNOPSIS
    Check the status of a running Optuna search on GCP.
.EXAMPLE
    .\gcp_check_status.ps1                     # Quick status
    .\gcp_check_status.ps1 -Attach             # Attach to live output
    .\gcp_check_status.ps1 -DownloadDb         # Download latest .db
#>

param(
    [string]$VmName = "optuna-runner",
    [string]$Zone = "us-central1-a",
    [string]$Project = "cltrainer",
    [switch]$Attach,
    [switch]$DownloadDb
)

$Bucket = "gs://${Project}-optuna-results"

Write-Host ""
Write-Host "=== Optuna GCP Status ===" -ForegroundColor Cyan

# --- Dynamically lookup the zone if the VM exists ---
$discoveredZone = gcloud compute instances list --filter="name:^$VmName$" --format="value(zone)" 2>$null
if ($discoveredZone) {
    $Zone = $discoveredZone.Trim()
}

# --- Check VM ---
$status = gcloud compute instances describe $VmName --zone=$Zone `
    --format="get(status)" 2>$null

if ($LASTEXITCODE -ne 0) {
    Write-Host "  VM '$VmName' not found. Run gcp_setup.ps1 first." -ForegroundColor Red
    Write-Host "`n  Checking GCS for completed results..."
    gsutil ls "$Bucket/**" 2>$null
    exit 0
}

Write-Host "  VM Status: $status"

if ($status -ne "RUNNING") {
    Write-Host "  VM is not running." -ForegroundColor Yellow
    Write-Host "  Start with: gcloud compute instances start $VmName --zone=$Zone"
    Write-Host "`n  Checking GCS for completed results..."
    gsutil ls -l "$Bucket/**" 2>$null
    exit 0
}

# --- Check tmux session ---
$tmuxStatus = gcloud compute ssh $VmName --zone=$Zone `
    --command="tmux has-session -t optuna 2>/dev/null && echo RUNNING || echo STOPPED" `
    --quiet 2>$null

if ($tmuxStatus -match "RUNNING") {
    Write-Host "  Optuna:   RUNNING" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Recent output:" -ForegroundColor Cyan
    Write-Host "  ─────────────────────────────────────────"
    gcloud compute ssh $VmName --zone=$Zone `
        --command="tmux capture-pane -t optuna -p | tail -20" --quiet 2>$null
    Write-Host "  ─────────────────────────────────────────"
} else {
    Write-Host "  Optuna:   COMPLETED (or not started)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Files on VM:" -ForegroundColor Cyan
    gcloud compute ssh $VmName --zone=$Zone `
        --command="ls -lh ~/project/models/optuna_studies/*.db 2>/dev/null; ls -lh ~/project/reports/optuna_*.json 2>/dev/null; ls -lh ~/project/reports/optuna_*.csv 2>/dev/null" `
        --quiet 2>$null
}

# --- Attach to tmux ---
if ($Attach) {
    Write-Host ""
    Write-Host "Attaching to tmux session (press Ctrl+B then D to detach)..." -ForegroundColor Cyan
    gcloud compute ssh $VmName --zone=$Zone -- -t "tmux attach -t optuna"
    exit 0
}

# --- Download .db files ---
if ($DownloadDb) {
    Write-Host ""
    Write-Host "Downloading results..." -ForegroundColor Cyan

    $projDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
    $studiesDir = Join-Path $projDir "models\optuna_studies"
    $reportsDir = Join-Path $projDir "reports"
    New-Item -ItemType Directory -Force -Path $studiesDir | Out-Null

    # Download from VM
    gcloud compute scp --recurse "${VmName}:~/project/models/optuna_studies/" `
        "$studiesDir\" --zone=$Zone 2>$null
    gcloud compute scp "${VmName}:~/project/reports/optuna_*" `
        "$reportsDir\" --zone=$Zone 2>$null

    Write-Host "  Studies: $studiesDir" -ForegroundColor Green
    Write-Host "  Reports: $reportsDir" -ForegroundColor Green
}

# --- Check GCS ---
Write-Host ""
Write-Host "  GCS bucket:" -ForegroundColor Cyan
$gcsContents = gsutil ls "$Bucket/**" 2>$null
if ($gcsContents) {
    $gcsContents | ForEach-Object { Write-Host "    $_" }
} else {
    Write-Host "    (no results uploaded yet)" -ForegroundColor Gray
}

Write-Host ""
