<#
.SYNOPSIS
    Downloads results and deletes the GCP VM to stop charges.
.EXAMPLE
    .\gcp_teardown.ps1                  # Download results + delete VM
    .\gcp_teardown.ps1 -SkipDownload    # Just delete VM
    .\gcp_teardown.ps1 -CleanAll        # Delete VM + GCS bucket
#>

param(
    [string]$VmName = "optuna-runner",
    [string]$Zone = "us-central1-a",
    [string]$Project = "cltrainer",
    [switch]$SkipDownload,
    [switch]$CleanAll
)

$Bucket = "gs://${Project}-optuna-results"
$ProjDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Yellow
Write-Host " GCP OPTUNA VM TEARDOWN" -ForegroundColor Yellow
Write-Host "=====================================================" -ForegroundColor Yellow

# --- Dynamically lookup the zone if the VM exists ---
$discoveredZone = gcloud compute instances list --filter="name:^$VmName$" --format="value(zone)" 2>$null
if ($discoveredZone) {
    $Zone = $discoveredZone.Trim()
}

# --- [1/3] Download results ---
if (-not $SkipDownload) {
    Write-Host "`n[1/3] Downloading results..."

    $studiesDir = Join-Path $ProjDir "models\optuna_studies"
    $reportsDir = Join-Path $ProjDir "reports"
    New-Item -ItemType Directory -Force -Path $studiesDir | Out-Null
    New-Item -ItemType Directory -Force -Path $reportsDir | Out-Null

    # Try downloading from VM directly
    $vmStatus = gcloud compute instances describe $VmName --zone=$Zone `
        --format="get(status)" 2>$null

    if ($vmStatus -eq "RUNNING") {
        Write-Host "  Downloading from VM..."
        gcloud compute scp --recurse `
            "${VmName}:~/project/models/optuna_studies/" "$studiesDir\" `
            --zone=$Zone 2>$null
        gcloud compute scp "${VmName}:~/project/reports/optuna_*" `
            "$reportsDir\" --zone=$Zone 2>$null
        gcloud compute scp "${VmName}:~/project/optuna_run_*" `
            "$reportsDir\" --zone=$Zone 2>$null
        Write-Host "  Downloaded from VM" -ForegroundColor Green
    }

    # Also download from GCS (backup / in case VM was preempted)
    Write-Host "  Downloading from GCS bucket..."
    gsutil -m cp "$Bucket/studies/*" "$studiesDir\" 2>$null
    gsutil -m cp "$Bucket/reports/*" "$reportsDir\" 2>$null
    gsutil -m cp "$Bucket/logs/*" "$reportsDir\" 2>$null
    Write-Host "  Downloaded from GCS" -ForegroundColor Green

    Write-Host ""
    Write-Host "  Results saved to:" -ForegroundColor Green
    Write-Host "    Studies: $studiesDir"
    Write-Host "    Reports: $reportsDir"
} else {
    Write-Host "`n[1/3] Skipping download" -ForegroundColor Gray
}

# --- [2/3] Delete VM ---
Write-Host "`n[2/3] Deleting VM instance '$VmName'..."
gcloud compute instances delete $VmName --zone=$Zone --quiet 2>$null

if ($LASTEXITCODE -eq 0) {
    Write-Host "  VM deleted!" -ForegroundColor Green
} else {
    Write-Host "  VM not found or already deleted" -ForegroundColor Yellow
}

# --- [3/3] GCS bucket ---
if ($CleanAll) {
    Write-Host "`n[3/3] Deleting GCS bucket..."
    gsutil -m rm -r "${Bucket}/**" 2>$null
    gsutil rb $Bucket 2>$null
    Write-Host "  Bucket deleted" -ForegroundColor Green
} else {
    Write-Host "`n[3/3] GCS bucket preserved: $Bucket" -ForegroundColor Gray
    Write-Host "  Results remain accessible via: gsutil ls $Bucket"
    Write-Host "  Delete later with: gsutil -m rm -r $Bucket" -ForegroundColor Gray
}

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host " TEARDOWN COMPLETE - NO MORE CHARGES" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""
