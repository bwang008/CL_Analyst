<#
.SYNOPSIS
    Parallel execution of Canary experiments for 3H, 6H, and 12H triple barrier targets.
#>

$ErrorActionPreference = "Stop"
$horizons = @("3H", "6H", "12H")
$jobs = @()

foreach ($h in $horizons) {
    $vmName = "optuna-canary-${h}".ToLower()
    $prefix = "canary_${h}".ToLower()
    
    Write-Host "Deploying VM $vmName..." -ForegroundColor Cyan
    
    # Run deployment synchronously to avoid API rate limits/race conditions on GCP
    .\gcp\gcp_deploy_canary.ps1 -VmName $vmName `
        -MachineType "n2-highcpu-48" `
        -GcsDataPath "gs://cltrainer-optuna-results/data/cl-1h_bk_HourSet_04.parquet" `
        -TargetLong "TARGET_TRIPLE_2x1_${h}_LONG" `
        -TargetShort "TARGET_TRIPLE_2x1_${h}_SHORT" `
        -GcsPrefix $prefix `
        -NoShutdown
        
    # Start monitor and cleanup as a background job
    $scriptBlock = {
        param($vm, $px)
        Set-Location $using:PWD
        .\gcp\gcp_monitor.ps1 -VmName $vm -GcsPrefix $px -PollIntervalSeconds 120
        gcloud compute instances delete $vm --zone="us-central1-a" --quiet
    }
    
    $jobs += Start-Job -ScriptBlock $scriptBlock -ArgumentList $vmName, $prefix
}

Write-Host "All VMs deployed. Waiting for all to complete..." -ForegroundColor Yellow

# Wait for all jobs
$jobs | Wait-Job
$jobs | Receive-Job

Write-Host "All parallel canary runs completed successfully!" -ForegroundColor Green
