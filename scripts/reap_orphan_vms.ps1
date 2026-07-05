<#
.SYNOPSIS
    Find (and optionally delete) orphaned pipeline VMs.

.DESCRIPTION
    Lists all optuna-sweep-* / opt-post-* instances with their age. A VM is
    flagged ORPHAN when it is older than -MaxAgeHours (default 8h — longer than
    any legitimate sweep or post-opt run). Read-only by default; pass -Delete
    to actually remove flagged VMs.

    Background: teardown normally comes from the local orchestrator process.
    If that process dies (reboot, killed terminal), VMs created BEFORE the
    max-run-duration backstop (2026-07-05) would spin forever. This script is
    the reconciliation tool for those, and a manual safety net in general.

.EXAMPLE
    .\scripts\reap_orphan_vms.ps1                 # report only
    .\scripts\reap_orphan_vms.ps1 -Delete         # delete flagged VMs
    .\scripts\reap_orphan_vms.ps1 -MaxAgeHours 4 -Delete
#>
param(
    [int]$MaxAgeHours = 8,
    [switch]$Delete
)

$raw = gcloud compute instances list `
    --filter="name~'^(optuna-sweep|opt-post)'" `
    --format="csv[no-heading](name,zone,status,creationTimestamp)" 2>$null

if (-not $raw) {
    Write-Host "No pipeline VMs (optuna-sweep-*/opt-post-*) found." -ForegroundColor Green
    exit 0
}

$now = Get-Date
$orphans = @()
foreach ($line in $raw) {
    $parts = $line.Split(",")
    if ($parts.Count -lt 4) { continue }
    $name = $parts[0]; $zone = $parts[1]; $status = $parts[2]
    $created = [DateTimeOffset]::Parse($parts[3]).LocalDateTime
    $ageH = [math]::Round(($now - $created).TotalHours, 1)
    $flag = if ($ageH -gt $MaxAgeHours) { "ORPHAN?" } else { "ok" }
    Write-Host ("{0,-55} {1,-18} {2,-10} age {3,5}h  {4}" -f $name, $zone, $status, $ageH, $flag)
    if ($ageH -gt $MaxAgeHours) {
        $orphans += [pscustomobject]@{ Name = $name; Zone = $zone }
    }
}

if ($orphans.Count -eq 0) {
    Write-Host "`nNo VMs older than $MaxAgeHours h. Nothing to reap." -ForegroundColor Green
    exit 0
}

if (-not $Delete) {
    Write-Host "`n$($orphans.Count) VM(s) exceed $MaxAgeHours h. Re-run with -Delete to remove them." -ForegroundColor Yellow
    exit 0
}

foreach ($vm in $orphans) {
    Write-Host "Deleting $($vm.Name) ($($vm.Zone))..." -ForegroundColor Yellow
    gcloud compute instances delete $vm.Name --zone=$vm.Zone --quiet
}
Write-Host "Done: $($orphans.Count) VM(s) reaped." -ForegroundColor Green
