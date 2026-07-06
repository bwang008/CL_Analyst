<#
.SYNOPSIS
    Fleet error-queue watcher -- one shot, designed for an hourly cron at :06
    ("6 * * * *": after the hourly inference bar and the first 5m bar land).

.DESCRIPTION
    Scans .agents/collab/error_queue/pending/ for crash events written by
    fleet_runner.py (via fleet_error_events.py):

      * classification == "infrastructure"  -> filed straight to done/ with an
        audit_log.md line. Known IBKR connectivity signatures (from
        infra_patterns.json) never reach the agent -- no tokens wasted.
      * anything else                       -> moved to processing/ and a
        summary is printed for the AI agent, which then follows
        .agents/skills/fleet-error-monitor/SKILL.md.

    Prints NO_EVENTS when pending/ is empty. Always exits 0 (a watcher
    failure must not look like a fleet failure to the scheduler).

.EXAMPLE
    powershell -File scripts\error_watcher.ps1
#>
param(
    [string]$QueueDir = (Join-Path $PSScriptRoot "..\.agents\collab\error_queue")
)

$ErrorActionPreference = "Stop"

$QueueDir   = (Resolve-Path $QueueDir).Path
$PendingDir = Join-Path $QueueDir "pending"
$ProcessingDir = Join-Path $QueueDir "processing"
$DoneDir    = Join-Path $QueueDir "done"
$AuditLog   = Join-Path $QueueDir "audit_log.md"

function Get-UtcStamp {
    return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Add-AuditLine {
    param([string]$EventId, [string]$Role, [string]$Message)
    $line = "[$(Get-UtcStamp)] | $EventId | $Role | $Message"
    Add-Content -Path $AuditLog -Value $line -Encoding UTF8
}

function Move-EventFile {
    # Collision-safe move: a recurrence of a done/ event reuses the same
    # filename, so suffix a timestamp instead of clobbering the audit trail.
    param([System.IO.FileInfo]$File, [string]$DestDir)
    $dest = Join-Path $DestDir $File.Name
    if (Test-Path $dest) {
        $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
        $dest = Join-Path $DestDir ($File.BaseName + "_" + $stamp + ".json")
    }
    Move-Item -Path $File.FullName -Destination $dest
    return $dest
}

$pendingFiles = @(Get-ChildItem -Path $PendingDir -Filter "*.json" -File |
                  Sort-Object LastWriteTime)

if ($pendingFiles.Count -eq 0) {
    Write-Output "NO_EVENTS"
    exit 0
}

$agentEvents = @()
foreach ($file in $pendingFiles) {
    try {
        $event = Get-Content -Path $file.FullName -Raw | ConvertFrom-Json
    } catch {
        Write-Output "MALFORMED_EVENT: $($file.FullName) -- leaving in pending/ for manual review"
        continue
    }

    if ($event.classification -eq "infrastructure") {
        $dest = Move-EventFile -File $file -DestDir $DoneDir
        Add-AuditLine -EventId $event.event_id -Role "WATCHER" -Message (
            "INFRA auto-filed to done/ (pattern=$($event.matched_infra_pattern), " +
            "model=$($event.model_name), occurrences=$($event.occurrences), " +
            "gave_up=$($event.gave_up)) -- no ticket created")
        Write-Output "INFRA_FILED: $($event.event_id) (pattern=$($event.matched_infra_pattern))"
        continue
    }

    $dest = Move-EventFile -File $file -DestDir $ProcessingDir
    Add-AuditLine -EventId $event.event_id -Role "WATCHER" -Message (
        "moved pending/ -> processing/ for agent investigation " +
        "(model=$($event.model_name), exit=$($event.exit_code), " +
        "occurrences=$($event.occurrences), gave_up=$($event.gave_up))")
    $agentEvents += [PSCustomObject]@{ Event = $event; Path = $dest }
}

if ($agentEvents.Count -eq 0) {
    Write-Output "NO_AGENT_EVENTS (infra-only pass)"
    exit 0
}

Write-Output ""
Write-Output "=== $($agentEvents.Count) EVENT(S) FOR AGENT INVESTIGATION ==="
Write-Output "Protocol: .agents/skills/fleet-error-monitor/SKILL.md"
foreach ($item in $agentEvents) {
    $e = $item.Event
    Write-Output ""
    Write-Output "EVENT: $($item.Path)"
    Write-Output "  event_id:      $($e.event_id)"
    Write-Output "  model:         $($e.model_name)"
    Write-Output "  config:        $($e.config_path)"
    Write-Output "  exit_code:     $($e.exit_code)  restarts: $($e.restart_count)  gave_up: $($e.gave_up)  occurrences: $($e.occurrences)"
    Write-Output "  stderr log:    $($e.stderr_log_path)"
    Write-Output "  --- traceback (tail) ---"
    $tbLines = @($e.traceback -split "`n")
    if ($tbLines.Count -gt 25) { $tbLines = $tbLines[-25..-1] }
    foreach ($line in $tbLines) { Write-Output ("  | " + $line) }
}
exit 0
