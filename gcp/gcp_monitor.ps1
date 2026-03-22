<#
.SYNOPSIS
    Monitor a GCP VM running Optuna experiments. Auto-downloads results when done.
.DESCRIPTION
    Polls the VM status and GCS for a STATUS.json heartbeat. When the VM terminates:
    1. Downloads the run log from GCS
    2. Parses pass/fail status from the log
    3. Downloads artifacts (zip) from GCS
    4. Unpacks into local model registry or reports dir
    5. Checks for OOM / preemption signals
    6. Prints a summary report
.EXAMPLE
    .\gcp\gcp_monitor.ps1 -VmName optuna-runner-canary -GcsPrefix canary
    .\gcp\gcp_monitor.ps1 -VmName optuna-runner -GcsPrefix production -OutputDir models\registry
#>

param(
    [string]$VmName = "optuna-runner-canary",
    [string]$Zone = "us-central1-a",
    [string]$Bucket = "gs://cltrainer-optuna-results",
    [string]$GcsPrefix = "canary",
    [string]$OutputDir = "reports",
    [int]$PollIntervalSeconds = 60,
    [switch]$NoDownload
)

# Add gcloud to PATH if not already there
$gcloudBin = "C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin"
if ($env:PATH -notlike "*$gcloudBin*") {
    $env:PATH = "$gcloudBin;$env:PATH"
}

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$GcsBase = "$Bucket/$GcsPrefix"
$StatusUrl = "$GcsBase/STATUS.json"
$LogsUrl = "$GcsBase/logs/"
$LocalOutputDir = Join-Path (Join-Path $ProjectDir $OutputDir) $GcsPrefix

# Ensure output dir exists
if (-not (Test-Path $LocalOutputDir)) {
    New-Item -ItemType Directory -Path $LocalOutputDir -Force | Out-Null
}

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " GCP EXPERIMENT MONITOR" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  VM:          $VmName"
Write-Host "  Zone:        $Zone"
Write-Host "  GCS prefix:  $GcsPrefix"
Write-Host "  GCS bucket:  $GcsBase/"
Write-Host "  Output:      $LocalOutputDir"
Write-Host "  Poll every:  ${PollIntervalSeconds}s"
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Monitoring... (Ctrl+C to stop)" -ForegroundColor Gray
Write-Host ""

$startTime = Get-Date



function Get-VmStatus {
    try {
        $status = gcloud compute instances describe $VmName --zone=$Zone `
            --format="get(status)" 2>$null
        return $status.Trim()
    } catch {
        return "NOT_FOUND"
    }
}


function Get-GcsStatus {
    <# Download STATUS.json from GCS and parse it #>
    try {
        $tmpFile = [System.IO.Path]::GetTempFileName()
        gcloud storage cp $StatusUrl $tmpFile 2>$null
        if ($LASTEXITCODE -eq 0 -and (Test-Path $tmpFile)) {
            $content = Get-Content $tmpFile -Raw | ConvertFrom-Json
            Remove-Item $tmpFile -Force
            return $content
        }
        Remove-Item $tmpFile -Force -ErrorAction SilentlyContinue
    } catch {}
    return $null
}


function Get-LatestLog {
    <# Find and download the most recent log file from GCS #>
    try {
        $logFiles = gcloud storage ls "$LogsUrl" 2>$null | Sort-Object -Descending
        if ($logFiles -and $logFiles.Count -gt 0) {
            $latestLog = $logFiles[0].Trim()
            $logName = Split-Path -Leaf $latestLog
            $localLog = Join-Path $LocalOutputDir $logName
            gcloud storage cp $latestLog $localLog 2>$null
            return $localLog
        }
    } catch {}
    return $null
}


function Read-LogReport {
    param([string]$LogPath)
    
    if (-not (Test-Path $LogPath)) { return $null }
    
    $content = Get-Content $LogPath -Raw
    $lines = Get-Content $LogPath
    
    $report = @{
        LogFile = $LogPath
        Passed = 0
        Failed = 0
        Total = 0
        E2ECompleted = $false
        WallTime = "unknown"
        Searches = @()
        TerminationReason = "unknown"
        AgentId = "unknown"
        LastLines = ($lines | Select-Object -Last 30) -join "`n"
    }
    
    # Count passes and failures
    $passLines = $lines | Where-Object { $_ -match "PASSED" }
    $failLines = $lines | Where-Object { $_ -match "FAILED" }
    $report.Passed = @($passLines).Count
    $report.Failed = @($failLines).Count
    
    # Check for E2E completion
    if ($content -match "E2E PIPELINE COMPLETE") {
        $report.E2ECompleted = $true
    }
    
    # Extract wall time
    if ($content -match "Total wall time:\s*(\d+h\s*\d+m)") {
        $report.WallTime = $Matches[1]
    }
    
    # Extract agent ID
    if ($content -match "Agent:\s+(\S+)") {
        $report.AgentId = $Matches[1]
    }
    
    # Detect termination reason
    if ($content -match "RUN COMPLETE") {
        $report.TerminationReason = "COMPLETED_OK"
    } elseif ($content -match "Shutting down") {
        $report.TerminationReason = "SELF_SHUTDOWN"
    } else {
        $report.TerminationReason = "INTERRUPTED"
    }
    
    # Extract search results
    foreach ($line in $lines) {
        if ($line -match "(PASSED|FAILED)\s+\((\w+)\s+(\w+)\)") {
            $report.Searches += "$($Matches[1]): $($Matches[2]) $($Matches[3])"
        }
    }
    
    $report.Total = $report.Passed + $report.Failed
    return $report
}


function Get-OomCheck {
    <# Check VM dmesg for OOM kills (only works if VM is still accessible) #>
    try {
        $oomOutput = gcloud compute ssh $VmName --zone=$Zone `
            --command="dmesg | grep -i 'oom\|killed process\|out of memory' | tail -5" `
            --quiet 2>$null
        if ($oomOutput -and $oomOutput.Trim()) {
            return $oomOutput.Trim()
        }
    } catch {}
    return $null
}


function Save-Artifacts {
    Write-Host "`n  Downloading artifacts from GCS..." -ForegroundColor Yellow
    
    # Download studies
    $studiesDir = Join-Path $LocalOutputDir "studies"
    if (-not (Test-Path $studiesDir)) { New-Item -ItemType Directory -Path $studiesDir -Force | Out-Null }
    gcloud storage cp -r "$GcsBase/studies/*" $studiesDir 2>$null
    
    # Download reports
    $reportsDir = Join-Path $LocalOutputDir "reports"
    if (-not (Test-Path $reportsDir)) { New-Item -ItemType Directory -Path $reportsDir -Force | Out-Null }
    gcloud storage cp -r "$GcsBase/reports/*" $reportsDir 2>$null
    
    # Download production artifacts zip if it exists
    $zipUrl = "$GcsBase/production/"
    try {
        $zipFiles = gcloud storage ls "$zipUrl*.zip" 2>$null
        foreach ($zip in $zipFiles) {
            if ($zip) {
                $zipName = Split-Path -Leaf $zip.Trim()
                $localZip = Join-Path $LocalOutputDir $zipName
                gcloud storage cp $zip.Trim() $localZip 2>$null
                Write-Host "  Downloaded: $zipName" -ForegroundColor Green
                
                # Unzip into registry
                $registryDir = Join-Path $LocalOutputDir "registry"
                if (-not (Test-Path $registryDir)) { New-Item -ItemType Directory -Path $registryDir -Force | Out-Null }
                Expand-Archive -Path $localZip -DestinationPath $registryDir -Force
                Write-Host "  Unpacked to: $registryDir" -ForegroundColor Green
            }
        }
    } catch {}
    
    # Download logs
    $logsDir = Join-Path $LocalOutputDir "logs"
    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
    gcloud storage cp -r "$LogsUrl*" $logsDir 2>$null
    
    Write-Host "  Artifacts saved to: $LocalOutputDir" -ForegroundColor Green
}


function Write-Report {
    param($LogReport, $OomResult, $FinalStatus, $MonitorWallTime)
    
    $reportPath = Join-Path $LocalOutputDir "run_report.md"
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $e2eStatus = if ($LogReport.E2ECompleted) { "Completed" } else { "Not completed" }
    $searchList = ($LogReport.Searches | ForEach-Object { "- $_" }) -join "`n"
    $oomSection = if ($OomResult) { $OomResult } else { "No OOM events detected." }
    $codeBlock = '```'
    
    $lines = @(
        "# GCP Run Report - $GcsPrefix"
        "Generated: $timestamp"
        ""
        "## Summary"
        "| Field | Value |"
        "|---|---|"
        "| VM | $VmName |"
        "| Final Status | $FinalStatus |"
        "| Termination Reason | $($LogReport.TerminationReason) |"
        "| Agent | $($LogReport.AgentId) |"
        "| Searches Passed | $($LogReport.Passed) |"
        "| Searches Failed | $($LogReport.Failed) |"
        "| E2E Pipeline | $e2eStatus |"
        "| Wall Time (VM) | $($LogReport.WallTime) |"
        "| Monitor Duration | $MonitorWallTime |"
        ""
        "## Search Results"
        $searchList
        ""
        "## OOM Detection"
        $oomSection
        ""
        "## Last 30 Lines of Log"
        $codeBlock
        $LogReport.LastLines
        $codeBlock
        ""
        "## Artifacts Downloaded To"
        $LocalOutputDir
    )
    
    $lines -join "`n" | Out-File -FilePath $reportPath -Encoding utf8
    Write-Host "`n  Report saved: $reportPath" -ForegroundColor Green
    return $reportPath
}


# ==========================================================
# MAIN POLLING LOOP
# ==========================================================

$iteration = 0
while ($true) {
    $iteration++
    $now = Get-Date -Format "HH:mm:ss"
    $elapsed = [math]::Round(((Get-Date) - $startTime).TotalMinutes, 1)
    
    # Check VM status
    $vmStatus = Get-VmStatus
    
    # Check GCS status heartbeat
    $gcsStatus = Get-GcsStatus
    
    # Build status line
    if ($gcsStatus) {
        $searchInfo = "search $($gcsStatus.completed)/$($gcsStatus.total)"
        if ($gcsStatus.current) { $searchInfo += " (current: $($gcsStatus.current))" }
        $lastUpdate = if ($gcsStatus.last_update) { $gcsStatus.last_update } else { "?" }
        Write-Host "[$now] VM=$vmStatus | $searchInfo | heartbeat=$lastUpdate | elapsed=${elapsed}m" -ForegroundColor Gray
    } else {
        Write-Host "[$now] VM=$vmStatus | no STATUS.json yet | elapsed=${elapsed}m" -ForegroundColor Gray
    }
    
    # Check if VM has stopped/terminated
    if ($vmStatus -in @("TERMINATED", "STOPPED", "NOT_FOUND")) {
        Write-Host ""
        Write-Host "========================================================" -ForegroundColor Yellow
        Write-Host " VM $vmStatus - Collecting results..." -ForegroundColor Yellow
        Write-Host "========================================================" -ForegroundColor Yellow
        
        # Download and parse the log
        $logPath = Get-LatestLog
        $logReport = if ($logPath) { Read-LogReport -LogPath $logPath } else { $null }
        
        # Try OOM check (may fail if VM is terminated)
        $oomResult = $null
        if ($vmStatus -eq "STOPPED") {
            # VM is stopped but accessible — check OOM
            try {
                gcloud compute instances start $VmName --zone=$Zone --quiet 2>$null
                Start-Sleep -Seconds 20
                $oomResult = Get-OomCheck
                gcloud compute instances stop $VmName --zone=$Zone --quiet 2>$null
            } catch {}
        }
        
        # Download artifacts
        if (-not $NoDownload) {
            Save-Artifacts
        }
        
        # Generate report
        $monitorWallTime = "{0:N1} minutes" -f ((Get-Date) - $startTime).TotalMinutes
        
        if ($logReport) {
            $reportPath = Write-Report -LogReport $logReport -OomResult $oomResult `
                -FinalStatus $vmStatus -MonitorWallTime $monitorWallTime
            
            # Print summary to console
            Write-Host ""
            Write-Host "========================================================" -ForegroundColor $(if ($logReport.E2ECompleted -and $logReport.Failed -eq 0) { "Green" } else { "Yellow" })
            Write-Host " RUN RESULTS" -ForegroundColor $(if ($logReport.E2ECompleted -and $logReport.Failed -eq 0) { "Green" } else { "Yellow" })
            Write-Host "========================================================" -ForegroundColor $(if ($logReport.E2ECompleted -and $logReport.Failed -eq 0) { "Green" } else { "Yellow" })
            Write-Host "  Termination:  $($logReport.TerminationReason)"
            Write-Host "  Agent:        $($logReport.AgentId)"
            Write-Host "  Passed:       $($logReport.Passed) searches"
            Write-Host "  Failed:       $($logReport.Failed) searches"
            Write-Host "  E2E Pipeline: $(if ($logReport.E2ECompleted) { 'COMPLETED' } else { 'NOT COMPLETED' })"
            Write-Host "  Wall Time:    $($logReport.WallTime)"
            if ($oomResult) {
                Write-Host "  OOM Events:   YES - check report" -ForegroundColor Red
            }
            Write-Host "  Report:       $reportPath"
            Write-Host "  Artifacts:    $LocalOutputDir"
            Write-Host "========================================================" -ForegroundColor $(if ($logReport.E2ECompleted -and $logReport.Failed -eq 0) { "Green" } else { "Yellow" })
        } else {
            Write-Host ""
            Write-Host "  WARNING: No log file found in GCS" -ForegroundColor Red
            Write-Host "  The VM may have failed before any output was produced." -ForegroundColor Red
            Write-Host ('  Check serial console: gcloud compute instances get-serial-port-output ' + $VmName + ' --zone=' + $Zone)
        }
        
        Write-Host ""
        break
    }
    
    Start-Sleep -Seconds $PollIntervalSeconds
}
