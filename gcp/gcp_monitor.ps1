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
    [int]$StaleThresholdMin = 10,
    [switch]$NoDownload,
    # --- Telegram / Batch options ---
    [switch]$DisableTelegram,
    [string]$ExperimentLabel = "",
    [string]$BatchId = "",
    [int]$ExperimentIndex = 0,
    [int]$BatchTotal = 0,
    [string]$ExitCodeFile = ""  # If set, write exit code to this file for the orchestrator
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

# Dynamically lookup the zone if the VM exists
$discoveredZone = gcloud compute instances list --filter="name:^$VmName$" --format="value(zone)" 2>$null
if ($discoveredZone) {
    $Zone = $discoveredZone.Trim()
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
$lastHeartbeat = $null          # Track last seen heartbeat timestamp
$heartbeatUnchangedSince = $null # When we first noticed the heartbeat was stale
$firstHeartbeatSent = $false     # Tracks whether we sent the "job started" Telegram message
$scriptExitCode = 2              # Default: no log found (overwritten on completion)



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
        $logFiles = @(gcloud storage ls "$LogsUrl" 2>$null | Sort-Object -Descending)
        if ($logFiles.Count -gt 0) {
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
    $passLines = $lines | Where-Object { $_ -cmatch "PASSED" }
    $failLines = $lines | Where-Object { $_ -cmatch "FAILED" }
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
    if ($content -match "E2E PIPELINE COMPLETE") {
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


function Get-VmLogTail {
    <# SSH into VM and grab the last N lines of the active canary log #>
    param([int]$Lines = 3)
    try {
        $output = gcloud compute ssh $VmName --zone=$Zone `
            --command="tail -n $Lines /home/*/project/sweep_run_*.log 2>/dev/null || tail -n $Lines /home/*/project/canary_run_*.log 2>/dev/null || tail -n $Lines /home/*/project/production_run_*.log 2>/dev/null" `
            --quiet 2>$null
        if ($output -and $LASTEXITCODE -eq 0) {
            return $output
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
        $zipFiles = @(gcloud storage ls "$zipUrl*.zip" 2>$null)
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
    
    # Download pipeline summary
    gcloud storage cp "$GcsBase/pipeline_summary.json" $LocalOutputDir 2>$null
    
    Write-Host "  Artifacts saved to: $LocalOutputDir" -ForegroundColor Green

    # --- Auto-Routing ---
    Write-Host "  Routing artifacts to active directories..." -ForegroundColor Cyan
    
    # 1. Route JSON configs to configs\strategies
    $configsDir = Join-Path $ProjectDir "configs\strategies"
    if (-not (Test-Path $configsDir)) { New-Item -ItemType Directory -Path $configsDir -Force | Out-Null }
    Get-ChildItem -Path $registryDir -Filter "*_opt.json" -Recurse -ErrorAction SilentlyContinue |
        Copy-Item -Destination $configsDir -Force -ErrorAction SilentlyContinue
        
    # 2. Route CSV predictions to data\predictions
    $predsDir = Join-Path $ProjectDir "data\predictions"
    if (-not (Test-Path $predsDir)) { New-Item -ItemType Directory -Path $predsDir -Force | Out-Null }
    Get-ChildItem -Path $registryDir -Filter "*.csv" -Recurse -ErrorAction SilentlyContinue |
        Copy-Item -Destination $predsDir -Force -ErrorAction SilentlyContinue
        
    # 3. Route Models to C:\CL_Analyst_Data\models\registry
    $env_vars = Read-DotEnv
    $dataRoot = if ($env_vars["CL_DATA_ROOT"]) { $env_vars["CL_DATA_ROOT"] } elseif ($env:CL_DATA_ROOT) { $env:CL_DATA_ROOT } else { "C:\CL_Analyst_Data" }
    $modelsRegistry = Join-Path $dataRoot "models\registry"
    
    $bundleDirs = Get-ChildItem -Path $registryDir -Directory -Filter "E2E_*" -Recurse -ErrorAction SilentlyContinue
    foreach ($bDir in $bundleDirs) {
        $targetDir = Join-Path $modelsRegistry $bDir.Name
        if (-not (Test-Path $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force | Out-Null }
        Get-ChildItem -Path $bDir.FullName -File -ErrorAction SilentlyContinue |
            Copy-Item -Destination $targetDir -Force -ErrorAction SilentlyContinue
    }
    Write-Host "  Artifacts routed successfully." -ForegroundColor Green
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
# TELEGRAM HELPERS
# ==========================================================

function Read-DotEnv {
    <# Parse .env file into a hashtable of KEY=value pairs #>
    $result = @{}
    $envPath = Join-Path $ProjectDir ".env"
    if (Test-Path $envPath) {
        Get-Content $envPath | ForEach-Object {
            if ($_ -match '^([A-Z_][A-Z0-9_]*)=(.*)$') {
                $result[$Matches[1]] = $Matches[2].Trim()
            }
        }
    }
    return $result
}


function Send-TelegramAlert {
    <# Fire-and-forget Telegram notification. Reads token/chat from .env. #>
    param([string]$Message)
    if ($DisableTelegram) { return }

    $env_vars = Read-DotEnv
    $token  = if ($env_vars["TELEGRAM_BOT_TOKEN"]) { $env_vars["TELEGRAM_BOT_TOKEN"] } else { $env:TELEGRAM_BOT_TOKEN }
    $chatId = if ($env_vars["TELEGRAM_CHAT_ID"])   { $env_vars["TELEGRAM_CHAT_ID"]   } else { $env:TELEGRAM_CHAT_ID   }

    if (-not $token -or $token -eq "") {
        Write-Host "  [Telegram] TELEGRAM_BOT_TOKEN not set - skipping." -ForegroundColor Gray
        return
    }

    $ts      = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $label   = if ($ExperimentLabel) { "[$ExperimentLabel]" } else { "[GCP Monitor]" }
    $batchSuffix = if ($BatchTotal -gt 0) { " ($ExperimentIndex/$BatchTotal)" } else { "" }
    $fullMsg = "${ts}${batchSuffix}`n${label}`n`n${Message}"

    # Strip all Markdown formatting to avoid Telegram parse_mode errors
    $plainMsg = $fullMsg -replace '\*', '' -replace '``', '' -replace '_', ''

    $bodyObj = @{ chat_id = $chatId; text = $plainMsg }
    $bodyJson = $bodyObj | ConvertTo-Json -Compress -Depth 3
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($bodyJson)

    try {
        Invoke-RestMethod -Method Post `
            -Uri "https://api.telegram.org/bot${token}/sendMessage" `
            -ContentType "application/json; charset=utf-8" `
            -Body $bodyBytes `
            -TimeoutSec 8 -ErrorAction Stop | Out-Null
        Write-Host "  [Telegram] Sent notification." -ForegroundColor DarkCyan
    } catch {
        Write-Host "  [Telegram] Send failed: $_" -ForegroundColor Yellow
    }
}


function Get-SerialConsoleOom {
    <#
    Check GCP serial console output for OOM/kernel-kill signals.
    Works even after the VM is gone (reads from GCP's stored console buffer).
    #>
    try {
        $serial = gcloud compute instances get-serial-port-output $VmName --zone=$Zone 2>$null
        if ($serial -match 'Out of memory|oom.kill|Killed process|Memory cgroup out of memory|oom-kill') {
            return "OOM detected in serial console (kernel OOM killer triggered)"
        }
    } catch {}
    return $null
}


# ==========================================================
# MAIN POLLING LOOP
# ==========================================================

$iteration = 0
$lastPeriodicUpdate = 0
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
        Write-Host "[$now] VM=$vmStatus - $searchInfo - heartbeat=$lastUpdate - elapsed=${elapsed}m" -ForegroundColor Gray
        
        # 5-minute periodic telegram update
        if ($elapsed - $lastPeriodicUpdate -ge 5.0) {
            $lastPeriodicUpdate = $elapsed
            if ($gcsStatus.workers -and $vmStatus -eq "RUNNING") {
                $msg = "Job Progress Update`nVM: $VmName`nElapsed: ${elapsed}m"
                foreach ($w in $gcsStatus.workers) {
                    if ($w.total_trials -gt 0) {
                        $pct = [math]::Round(($w.trials_done / $w.total_trials) * 100)
                        $score = if ($null -ne $w.best_score) { [math]::Round($w.best_score, 4) } else { "N/A" }
                        $msg += "`n`nTarget: $($w.target) ($($w.metric))`n   Progress: $($w.trials_done)/$($w.total_trials) (${pct}%) | Best: $score"
                    }
                }
                Send-TelegramAlert $msg
            }
        }

        # --- Telegram: fire "job started" on first heartbeat ---
        if (-not $firstHeartbeatSent) {
            $firstHeartbeatSent = $true
            $totalSearches = if ($gcsStatus.total) { $gcsStatus.total } else { "?" }
            Send-TelegramAlert "Job Started`nVM: $VmName`nGCS: $GcsBase/`nSearches: $totalSearches total`nPoll interval: ${PollIntervalSeconds}s"
        }
        
        # ---- Stale heartbeat detection ----
        $currentHB = $gcsStatus.last_update
        if ($currentHB -and $lastHeartbeat -and $currentHB -eq $lastHeartbeat) {
            # Heartbeat hasn't changed - track how long
            if (-not $heartbeatUnchangedSince) {
                $heartbeatUnchangedSince = Get-Date
            }
            $staleMins = [math]::Round(((Get-Date) - $heartbeatUnchangedSince).TotalMinutes, 1)
            if ($staleMins -ge $StaleThresholdMin) {
                Write-Host "  WARNING: STALE HEARTBEAT - unchanged for ${staleMins}m (threshold: ${StaleThresholdMin}m)" -ForegroundColor Red
                Write-Host "    The search may have crashed or stalled. Check VM logs below." -ForegroundColor Red
                # Send Telegram alert once per stale threshold crossing (not every poll)
                if ($staleMins -lt ($StaleThresholdMin + ($PollIntervalSeconds / 60) + 1)) {
                    Send-TelegramAlert "Stale Heartbeat`nVM: $VmName`nHeartbeat unchanged for ${staleMins}min`nSearch may be stalled or crashed - check VM logs"
                }
                
                # Auto-shutdown logic if exceedingly stale (>15 mins) and still running
                if ($staleMins -ge 15.0 -and $vmStatus -eq "RUNNING") {
                    Write-Host "  >> Stale for >= 15 mins. Validating if VM is truly idle before issuing shutdown..." -ForegroundColor Yellow
                    
                    # Check CPU load average (1-min) via Python3 to avoid quoting/regex hell over SSH
                    $loadAvgStr = gcloud compute ssh $VmName --zone=$Zone --command="python3 -c `"import os; print(os.getloadavg()[0])`"" --quiet 2>$null
                    
                    # Check for active optuna OR e2e pipeline processes
                    $pyProcs = gcloud compute ssh $VmName --zone=$Zone --command="pgrep -f 'optuna|vm_e2e_pipeline|vm_canary_run' | wc -l" --quiet 2>$null
                    
                    $loadAvg = 99.0
                    if ([double]::TryParse($loadAvgStr.Trim(), [ref]$loadAvg)) {}
                    
                    $procCount = 99
                    if ([int]::TryParse($pyProcs.Trim(), [ref]$procCount)) {}
                    
                    Write-Host "  >> 1-Min Load Average: $loadAvg | Active Procs: $procCount" -ForegroundColor DarkGray
                    
                    # Only stop if load is genuinely idle AND no pipeline processes are running
                    if ($procCount -eq 0 -and $loadAvg -lt 0.1) {
                        Write-Host "  >> Verified IDLE state. Issuing automated stop command to save costs." -ForegroundColor Red
                        gcloud compute instances stop $VmName --zone=$Zone --quiet 2>$null
                        Write-Host "  >> Stop command issued. Status will update on next cycle." -ForegroundColor Yellow
                    } else {
                        Write-Host "  >> VM is logically active (CPU or Procs). Awaiting natural termination." -ForegroundColor Yellow
                    }
                }
            } # end stale threshold block
        } else {
            # Heartbeat updated - reset tracker
            $heartbeatUnchangedSince = $null
        }
        $lastHeartbeat = $currentHB
    } else {
        Write-Host "[$now] VM=$vmStatus | no STATUS.json yet | elapsed=${elapsed}m" -ForegroundColor Gray
    }
    
    # ---- Live VM log tail ----
    if ($vmStatus -eq "RUNNING") {
        $logTail = Get-VmLogTail -Lines 3
        if ($logTail) {
            foreach ($line in $logTail) {
                $trimmed = $line.Trim()
                if ($trimmed) {
                    # Color errors red, passes green, worker info cyan, everything else dim
                    if ($trimmed -match 'FAILED|Error|Traceback|FileNotFound|exit 139') {
                        Write-Host "    >> $trimmed" -ForegroundColor Red
                    } elseif ($trimmed -match 'PASSED|COMPLETE|MEM') {
                        Write-Host "    >> $trimmed" -ForegroundColor Green
                    } elseif ($trimmed -match 'Worker W\d|Started worker|completed OK') {
                        Write-Host "    >> $trimmed" -ForegroundColor Cyan
                    } else {
                        Write-Host "    >> $trimmed" -ForegroundColor DarkGray
                    }
                }
            }
        }
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
            # VM is stopped but accessible - check OOM
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

        # --- OOM: try serial console if SSH-based check wasn't possible ---
        if (-not $oomResult) {
            $oomResult = Get-SerialConsoleOom
        }
        # --- Detect SPOT preemption: INTERRUPTED termination + no shutdown log line ---
        $wasPreempted = ($vmStatus -eq "TERMINATED" -and
                         $logReport -and
                         $logReport.TerminationReason -eq "INTERRUPTED")

        if ($logReport) {
            $reportPath = Write-Report -LogReport $logReport -OomResult $oomResult `
                -FinalStatus $vmStatus -MonitorWallTime $monitorWallTime

            # Determine exit code: 0=OK, 1=partial/failed
            $summaryJson = Join-Path $LocalOutputDir "pipeline_summary.json"
            $artOk = Test-Path $summaryJson
            $scriptExitCode = if ($logReport.E2ECompleted -and $logReport.Failed -eq 0 -and $artOk) { 0 } else { 1 }

            # Print summary to console
            $summaryColor = if ($scriptExitCode -eq 0) { "Green" } else { "Yellow" }
            Write-Host ""
            Write-Host "========================================================" -ForegroundColor $summaryColor
            Write-Host " RUN RESULTS" -ForegroundColor $summaryColor
            Write-Host "========================================================" -ForegroundColor $summaryColor
            Write-Host "  Termination:  $($logReport.TerminationReason)"
            Write-Host "  Agent:        $($logReport.AgentId)"
            Write-Host "  Passed:       $($logReport.Passed) searches"
            Write-Host "  Failed:       $($logReport.Failed) searches"
            Write-Host "  E2E Pipeline: $(if ($logReport.E2ECompleted) { 'COMPLETED' } else { 'NOT COMPLETED' })"
            Write-Host "  Artifacts:    $(if ($artOk) { 'VERIFIED' } else { 'MISSING (pipeline_summary.json)' })"
            Write-Host "  Wall Time:    $($logReport.WallTime)"
            if ($oomResult) { Write-Host "  OOM Events:   YES - check report" -ForegroundColor Red }
            if ($wasPreempted) { Write-Host "  SPOT:         Preempted by GCP" -ForegroundColor Red }
            Write-Host "  Report:       $reportPath"
            Write-Host "  Artifact Path:$LocalOutputDir"
            Write-Host "========================================================" -ForegroundColor $summaryColor

            # --- Telegram: completion message with ensemble PnL if available ---
            $pnlLines = @()
            if ($artOk) {
                try {
                    $ps = Get-Content $summaryJson -Raw | ConvertFrom-Json
                    foreach ($key in $ps.backtest_results.PSObject.Properties.Name) {
                        if ($key -match '^ensemble_') {
                            $r = $ps.backtest_results.$key
                            $pnlLines += "  ${key}: PF=$($r.profit_factor) PnL=`$$($r.total_pnl)"
                        }
                    }
                } catch {}
            } else {
                $pnlLines += "[WARNING] Artifact download failed (pipeline_summary.json not found locally)."
                $pnlLines += "Check if E2E crashed or GCS upload was rate-limited."
            }
            $icon        = if ($scriptExitCode -eq 0) { "SUCCESS" } else { "FAILED" }
            $statusLabel = if ($scriptExitCode -eq 0) { "SUCCESS" } else { "FAILED" }
            $oomLine     = if ($oomResult)    { "`nOOM: $oomResult" }    else { "" }
            $preemptLine = if ($wasPreempted) { "`nSPOT preempted by GCP" } else { "" }
            $pnlBlock    = if ($pnlLines)     { "`nArtifact / Ensemble Results:`n" + ($pnlLines -join "`n") } else { "" }
            Send-TelegramAlert ("Job $statusLabel`n" +
                "VM: $VmName`n" +
                "Searches: $($logReport.Passed)/$($logReport.Total) passed`n" +
                "E2E: $(if ($logReport.E2ECompleted) { 'COMPLETE' } else { 'INCOMPLETE' })`n" +
                "Wall Time: $($logReport.WallTime)" +
                $oomLine + $preemptLine + $pnlBlock)

        } else {
            # No log found - VM died before producing output
            $scriptExitCode = 2
            $oomLine = if ($oomResult) { "`n$oomResult" } else { "" }
            Write-Host ""
            Write-Host "  WARNING: No log file found in GCS" -ForegroundColor Red
            Write-Host "  The VM may have failed before any output was produced." -ForegroundColor Red
            Write-Host ('  Check serial console: gcloud compute instances get-serial-port-output ' + $VmName + ' --zone=' + $Zone)
            Send-TelegramAlert ("VM Terminated - No Log Found`n" +
                "VM: $VmName`nStatus: $vmStatus`n" +
                "VM may have crashed before producing any output" + $oomLine)
        }

        Write-Host ""
        Write-Host "  >> Cleaning up VM $VmName..." -ForegroundColor Yellow
        gcloud compute instances delete $VmName --zone=$Zone --quiet 2>$null
        Write-Host "  >> VM Deleted." -ForegroundColor Green

        # Write exit code to file for the orchestrator to read
        if ($ExitCodeFile) {
            $scriptExitCode | Out-File -FilePath $ExitCodeFile -Encoding ascii -NoNewline
        }
        exit $scriptExitCode
    }
    
    Start-Sleep -Seconds $PollIntervalSeconds
}
