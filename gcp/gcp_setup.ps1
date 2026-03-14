<#
.SYNOPSIS
    Creates a high-CPU GCP VM for Optuna hyperparameter searches.
.DESCRIPTION
    Provisions a spot instance with Python, LightGBM, Optuna, and all
    dependencies pre-installed via startup script. Takes 2-3 minutes.
.EXAMPLE
    .\gcp_setup.ps1
    .\gcp_setup.ps1 -MachineType c3-highcpu-88
    .\gcp_setup.ps1 -VmName my-vm -Zone us-west1-b
#>

param(
    [string]$VmName = "optuna-runner",
    [string]$MachineType = "c2d-highcpu-56",
    [string]$Zone = "us-central1-a",
    [int]$DiskSizeGB = 50,
    [string]$Project = "cltrainer"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bucket = "gs://${Project}-optuna-results"

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " GCP OPTUNA VM SETUP" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "  VM Name:      $VmName"
Write-Host "  Machine Type: $MachineType"
Write-Host "  Zone:         $Zone"
Write-Host "  Disk:         ${DiskSizeGB}GB SSD"
Write-Host "  Pricing:      On-demand (~$1.30/hr for c2d-highcpu-56)"
Write-Host "  Project:      $Project"
Write-Host "=====================================================" -ForegroundColor Cyan

# --- Set defaults ---
Write-Host "`n[1/4] Setting default compute zone..."
gcloud config set compute/zone $Zone --quiet 2>$null

# --- Create GCS bucket for results ---
Write-Host "`n[2/4] Creating GCS bucket for results..."
$bucketCheck = gsutil ls $Bucket 2>&1
if ($LASTEXITCODE -ne 0) {
    gsutil mb -p $Project -l us-central1 $Bucket
    Write-Host "  Created: $Bucket" -ForegroundColor Green
} else {
    Write-Host "  Already exists: $Bucket" -ForegroundColor Yellow
}

# --- Create VM ---
Write-Host "`n[3/4] Creating VM instance..."
Write-Host "  (This takes about 30 seconds)" -ForegroundColor Gray

$startupScript = Join-Path $ScriptDir "vm_startup.sh"
if (-not (Test-Path $startupScript)) {
    Write-Host "ERROR: vm_startup.sh not found at $startupScript" -ForegroundColor Red
    exit 1
}

gcloud compute instances create $VmName `
    --project=$Project `
    --zone=$Zone `
    --machine-type=$MachineType `
    --provisioning-model=SPOT `
    --instance-termination-action=STOP `
    --image-family=ubuntu-2204-lts `
    --image-project=ubuntu-os-cloud `
    --boot-disk-size="${DiskSizeGB}GB" `
    --boot-disk-type=pd-ssd `
    --metadata-from-file="startup-script=$startupScript" `
    --scopes=storage-full `
    --quiet

if ($LASTEXITCODE -ne 0) {
    Write-Host "`nERROR: Failed to create VM." -ForegroundColor Red
    Write-Host "Common causes:" -ForegroundColor Yellow
    Write-Host "  - CPU quota exceeded (CPUS_ALL_REGIONS limit)"
    Write-Host "  - Try: -MachineType e2-highcpu-8"
    Write-Host "  - Check quota: gcloud compute regions describe us-central1 --format='table(quotas)'"
    exit 1
}

Write-Host "  VM created!" -ForegroundColor Green

# --- Wait for startup script to finish ---
Write-Host "`n[4/4] Waiting for Python + packages to install..."
Write-Host "  (This takes 2-3 minutes)" -ForegroundColor Gray

$maxWait = 300
$elapsed = 0
$ready = $false

while ($elapsed -lt $maxWait) {
    Start-Sleep -Seconds 15
    $elapsed += 15
    try {
        $result = gcloud compute ssh $VmName --zone=$Zone `
            --command="test -f /tmp/startup_done && echo READY" `
            --quiet 2>$null
        if ($result -match "READY") {
            $ready = $true
            break
        }
    } catch {
        # SSH might not be ready yet, keep waiting
    }
    Write-Host "  Installing... ($elapsed`s elapsed)" -ForegroundColor Gray
}

if ($ready) {
    Write-Host "`n  VM is ready!" -ForegroundColor Green
} else {
    Write-Host "`n  Startup may still be running. Check with:" -ForegroundColor Yellow
    Write-Host "  gcloud compute ssh $VmName --command='cat /tmp/startup.log'"
}

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host " SETUP COMPLETE" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""
Write-Host 'Next step - deploy code and start a search:' -ForegroundColor Cyan
Write-Host '  .\gcp\gcp_deploy_run.ps1 `'
Write-Host '      -DataPath ''C:\CL_Analyst_Data\data\processed\CL_set_08.parquet'''
Write-Host ""
Write-Host 'VM hourly cost: ~$1.30/hr (c2d-highcpu-56)' -ForegroundColor Gray
Write-Host 'Remember to tear down when done: .\gcp\gcp_teardown.ps1' -ForegroundColor Gray
Write-Host ""
