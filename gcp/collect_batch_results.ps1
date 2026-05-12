<#
.SYNOPSIS
    Collect and compare results from a completed canary batch run.
.DESCRIPTION
    Reads batch_progress.json for a given BatchId, loads each experiment's
    pipeline_summary.json, and generates a consolidated Markdown comparison
    table. Optionally sends the summary to Telegram.
.PARAMETER BatchId
    The batch ID to collect results for (e.g. "batch_20260424_0954").
    Defaults to the most recently created batch directory.
.PARAMETER EnableTelegram
    Send the final comparison table to Telegram.
.EXAMPLE
    .\gcp\collect_batch_results.ps1 -BatchId batch_20260424_0954
    .\gcp\collect_batch_results.ps1                    # uses most recent batch
    .\gcp\collect_batch_results.ps1 -EnableTelegram
#>

param(
    [string]$BatchId        = "",
    [switch]$DisableTelegram
)

$ErrorActionPreference = "Continue"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$BatchRoot  = Join-Path $ProjectDir "reports\batch_runs"

# ---- Resolve BatchId ----
if (-not $BatchId) {
    $latestBatch = Get-ChildItem $BatchRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $latestBatch) {
        Write-Host "ERROR: No batch directories found under $BatchRoot" -ForegroundColor Red
        exit 1
    }
    $BatchId = $latestBatch.Name
    Write-Host "Using most recent batch: $BatchId" -ForegroundColor Cyan
}

$BatchDir     = Join-Path $BatchRoot $BatchId
$ProgressFile = Join-Path $BatchDir "batch_progress.json"
$ReportPath   = Join-Path $BatchDir "batch_summary.md"

if (-not (Test-Path $ProgressFile)) {
    Write-Host "ERROR: batch_progress.json not found: $ProgressFile" -ForegroundColor Red
    exit 1
}

$progress = Get-Content $ProgressFile -Raw | ConvertFrom-Json

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " BATCH RESULTS COLLECTOR" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Batch ID:    $BatchId"
Write-Host "  Total:       $($progress.total)"
Write-Host "  Completed:   $($progress.completed)"
Write-Host "  Failed:      $($progress.failed)"
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ============================================================
# HELPERS
# ============================================================

function Read-DotEnv {
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


function Send-TelegramMessage {
    param([string]$Message)
    if ($DisableTelegram) { return }

    $ev      = Read-DotEnv
    $token   = if ($ev["TELEGRAM_BOT_TOKEN"]) { $ev["TELEGRAM_BOT_TOKEN"] } else { $env:TELEGRAM_BOT_TOKEN }
    $chatId  = if ($ev["TELEGRAM_CHAT_ID"])   { $ev["TELEGRAM_CHAT_ID"]   } else { $env:TELEGRAM_CHAT_ID   }
    if (-not $token -or $token -eq "") { Write-Host "  [Telegram] Not configured." -ForegroundColor Gray; return }

    $ts      = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $fullMsg = "_${ts}_`n*[Batch Summary: $BatchId]*`n`n${Message}"
    $body    = @{ chat_id = $chatId; text = $fullMsg; parse_mode = "Markdown" } | ConvertTo-Json -Compress -Depth 3
    $bytes   = [System.Text.Encoding]::UTF8.GetBytes($body)

    try {
        Invoke-RestMethod -Method Post `
            -Uri "https://api.telegram.org/bot${token}/sendMessage" `
            -ContentType "application/json; charset=utf-8" `
            -Body $bytes -TimeoutSec 8 -ErrorAction Stop | Out-Null
        Write-Host "  [Telegram] Summary sent." -ForegroundColor DarkCyan
    } catch {
        Write-Host "  [Telegram] Send failed: $_" -ForegroundColor Yellow
    }
}


function Format-Metric {
    param($val, [string]$fmt = "N2")
    if ($null -eq $val) { return "N/A" }
    try { return ([double]$val).ToString($fmt) } catch { return "$val" }
}



function Align-MarkdownTable {
    param([string[]]$Lines)
    $validLines = $Lines | Where-Object { $_.Trim() -ne '' }
    if ($validLines.Count -eq 0) { return '' }
    
    $parsedRows = @()
    foreach ($line in $validLines) {
        $cells = $line.Trim().Trim('|').Split('|') | ForEach-Object { $_.Trim() }
        $parsedRows += ,$cells
    }
    
    $maxCols = 0
    foreach ($row in $parsedRows) { if ($row.Count -gt $maxCols) { $maxCols = $row.Count } }
    
    $widths = @(0) * $maxCols
    foreach ($row in $parsedRows) {
        for ($i = 0; $i -lt $row.Count; $i++) {
            if ($row[$i] -match '^-+$') { continue }
            $len = $row[$i].Length
            # Emojis have length 2 but display as 1 or 2. We'll just use string length.
            if ($len -gt $widths[$i]) { $widths[$i] = $len }
        }
    }
    
    $alignedLines = @()
    foreach ($row in $parsedRows) {
        $formattedCells = @()
        for ($i = 0; $i -lt $row.Count; $i++) {
            if ($row[$i] -match '^-+$') {
                $formattedCells += '-' * $widths[$i]
            } else {
                $formattedCells += $row[$i].PadRight($widths[$i])
            }
        }
        $alignedLines += '| ' + ($formattedCells -join ' | ') + ' |'
    }
    return $alignedLines -join "`n"
}

# ============================================================
# COLLECT RESULTS FROM EACH EXPERIMENT
# ============================================================

$rows     = @()
$failRows = @()

foreach ($exp in $progress.experiments) {
    $localDir     = $exp.local_dir
    $summaryPath  = Join-Path $localDir "pipeline_summary.json"

    $row = [ordered]@{
        Label          = $exp.label
        Status         = $exp.status
        WallTimeMin    = $exp.wall_time_min
        ArtifactOk     = $exp.artifact_verified
        # Per-model metrics (filled below)
        LongLL_Trades  = "—"; LongLL_WR  = "—"; LongLL_PF  = "—"; LongLL_PnL  = "—"
        LongAP_Trades  = "—"; LongAP_WR  = "—"; LongAP_PF  = "—"; LongAP_PnL  = "—"
        ShortLL_Trades = "—"; ShortLL_WR = "—"; ShortLL_PF = "—"; ShortLL_PnL = "—"
        ShortAP_Trades = "—"; ShortAP_WR = "—"; ShortAP_PF = "—"; ShortAP_PnL = "—"
        EnsLL_Trades   = "—"; EnsLL_WR   = "—"; EnsLL_PF   = "—"; EnsLL_PnL   = "—"
        EnsAP_Trades   = "—"; EnsAP_WR   = "—"; EnsAP_PF   = "—"; EnsAP_PnL   = "—"
        FailureReason  = $exp.failure_reason
        LocalDir       = $localDir
    }

    if ($exp.status -eq "COMPLETED" -and (Test-Path $summaryPath)) {
        try {
            $ps = Get-Content $summaryPath -Raw | ConvertFrom-Json
            $bt = $ps.backtest_results

            $keys = $bt.PSObject.Properties.Name
            foreach ($key in $keys) {
                $r = $bt.$key
                switch ($key) {
                    "long_logloss"            { $row.LongLL_Trades  = $r.trade_count; $row.LongLL_WR  = Format-Metric $r.win_rate "N1"; $row.LongLL_PF  = Format-Metric $r.profit_factor; $row.LongLL_PnL  = Format-Metric $r.total_pnl "N0" }
                    "long_average_precision"  { $row.LongAP_Trades  = $r.trade_count; $row.LongAP_WR  = Format-Metric $r.win_rate "N1"; $row.LongAP_PF  = Format-Metric $r.profit_factor; $row.LongAP_PnL  = Format-Metric $r.total_pnl "N0" }
                    "short_logloss"           { $row.ShortLL_Trades = $r.trade_count; $row.ShortLL_WR = Format-Metric $r.win_rate "N1"; $row.ShortLL_PF = Format-Metric $r.profit_factor; $row.ShortLL_PnL = Format-Metric $r.total_pnl "N0" }
                    "short_average_precision" { $row.ShortAP_Trades = $r.trade_count; $row.ShortAP_WR = Format-Metric $r.win_rate "N1"; $row.ShortAP_PF = Format-Metric $r.profit_factor; $row.ShortAP_PnL = Format-Metric $r.total_pnl "N0" }
                    "ensemble_logloss"        { $row.EnsLL_Trades   = $r.trade_count; $row.EnsLL_WR   = Format-Metric $r.win_rate "N1"; $row.EnsLL_PF   = Format-Metric $r.profit_factor; $row.EnsLL_PnL   = Format-Metric $r.total_pnl "N0" }
                    "ensemble_average_precision" { $row.EnsAP_Trades = $r.trade_count; $row.EnsAP_WR = Format-Metric $r.win_rate "N1"; $row.EnsAP_PF = Format-Metric $r.profit_factor; $row.EnsAP_PnL = Format-Metric $r.total_pnl "N0" }
                }
            }
        } catch {
            Write-Host "  WARNING: Could not parse $summaryPath : $_" -ForegroundColor Yellow
        }
        $rows     += $row
    } else {
        $row.LongLL_Trades = "FAILED"
        $failRows += $row
    }
}

$allRows = $rows + $failRows

# ============================================================
# BUILD MARKDOWN REPORT
# ============================================================

$ts      = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$header  = @"
# Batch Experiment Summary — $BatchId

Generated: $ts
Manifest: $($progress.manifest)

## Batch Status

| Field | Value |
|---|---|
| Total Experiments | $($progress.total) |
| Completed | $($progress.completed) |
| Failed | $($progress.failed) |
| Started | $($progress.started_at) |
| Completed | $($progress.completed_at) |

---

## Results by Experiment

### Long Model (Logloss)

| Experiment | Status | Wall (min) | Trades | Win Rate | Profit Factor | PnL |
|---|---|---|---|---|---|---|
"@

$llLongRows = $allRows | ForEach-Object {
    "| $($_.Label) | $($_.Status) | $($_.WallTimeMin) | $($_.LongLL_Trades) | $($_.LongLL_WR)% | $($_.LongLL_PF) | `$$($_.LongLL_PnL) |"
}

$apLongHeader = @"

### Long Model (Average Precision)

| Experiment | Status | Trades | Win Rate | Profit Factor | PnL |
|---|---|---|---|---|---|
"@
$llApRows = $allRows | ForEach-Object {
    "| $($_.Label) | $($_.Status) | $($_.LongAP_Trades) | $($_.LongAP_WR)% | $($_.LongAP_PF) | `$$($_.LongAP_PnL) |"
}

$shortLLHeader = @"

### Short Model (Logloss)

| Experiment | Status | Trades | Win Rate | Profit Factor | PnL |
|---|---|---|---|---|---|
"@
$shortLLRows = $allRows | ForEach-Object {
    "| $($_.Label) | $($_.Status) | $($_.ShortLL_Trades) | $($_.ShortLL_WR)% | $($_.ShortLL_PF) | `$$($_.ShortLL_PnL) |"
}

$shortAPHeader = @"

### Short Model (Average Precision)

| Experiment | Status | Trades | Win Rate | Profit Factor | PnL |
|---|---|---|---|---|---|
"@
$shortAPRows = $allRows | ForEach-Object {
    "| $($_.Label) | $($_.Status) | $($_.ShortAP_Trades) | $($_.ShortAP_WR)% | $($_.ShortAP_PF) | `$$($_.ShortAP_PnL) |"
}

$ensHeader = @"

### Ensemble (Both Metrics)

| Experiment | Status | LL Trades | LL PF | LL PnL | AP Trades | AP PF | AP PnL |
|---|---|---|---|---|---|---|---|
"@
$ensRows = $allRows | ForEach-Object {
    "| $($_.Label) | $($_.Status) | $($_.EnsLL_Trades) | $($_.EnsLL_PF) | `$$($_.EnsLL_PnL) | $($_.EnsAP_Trades) | $($_.EnsAP_PF) | `$$($_.EnsAP_PnL) |"
}

$failSection = ""
if ($failRows.Count -gt 0) {
    $failLines = $failRows | ForEach-Object { "| $($_.Label) | $($_.Status) | $($_.FailureReason) | $($_.LocalDir) |" }
    $failSection = @"

---

## Failed Experiments

| Experiment | Status | Reason | Local Dir |
|---|---|---|---|
$($failLines -join "`n")
"@
}

$artifactSection = @"

---

## Artifact Locations

| Experiment | Status | Artifact Verified | Local Directory |
|---|---|---|---|
"@
$artRows = $allRows | ForEach-Object {
    $artOk = if ($_.ArtifactOk) { "✅" } elseif ($null -eq $_.ArtifactOk) { "—" } else { "❌" }
    "| $($_.Label) | $($_.Status) | $artOk | $($_.LocalDir) |"
}

$headerLines = $header -split "`n"
$llLongAligned = Align-MarkdownTable ($headerLines[-2..-1] + $llLongRows)
$headerTop = ($headerLines[0..($headerLines.Count-3)] -join "`n")

$apLongLines = $apLongHeader -split "`n"
$llApAligned = Align-MarkdownTable ($apLongLines[-2..-1] + $llApRows)
$apLongTop = ($apLongLines[0..($apLongLines.Count-3)] -join "`n")

$shortLLLines = $shortLLHeader -split "`n"
$shortLLAligned = Align-MarkdownTable ($shortLLLines[-2..-1] + $shortLLRows)
$shortLLTop = ($shortLLLines[0..($shortLLLines.Count-3)] -join "`n")

$shortAPLines = $shortAPHeader -split "`n"
$shortAPAligned = Align-MarkdownTable ($shortAPLines[-2..-1] + $shortAPRows)
$shortAPTop = ($shortAPLines[0..($shortAPLines.Count-3)] -join "`n")

$ensLines = $ensHeader -split "`n"
$ensAligned = Align-MarkdownTable ($ensLines[-2..-1] + $ensRows)
$ensTop = ($ensLines[0..($ensLines.Count-3)] -join "`n")

$failAligned = ""
if ($failRows.Count -gt 0) {
    $failLinesSplit = $failSection -split "`n"
    # Find the table header
    $tableStartIdx = -1
    for ($i=0; $i -lt $failLinesSplit.Count; $i++) {
        if ($failLinesSplit[$i] -match '^\| Experiment \|') { $tableStartIdx = $i; break }
    }
    if ($tableStartIdx -ge 0) {
        $failAlignedTable = Align-MarkdownTable ($failLinesSplit[$tableStartIdx..($failLinesSplit.Count-1)])
        $failTop = ($failLinesSplit[0..($tableStartIdx-1)] -join "`n")
        $failAligned = $failTop + "`n" + $failAlignedTable + "`n"
    } else {
        $failAligned = $failSection
    }
}

$artLines = $artifactSection -split "`n"
$artAligned = Align-MarkdownTable ($artLines[-2..-1] + $artRows)
$artTop = ($artLines[0..($artLines.Count-3)] -join "`n")

$reportContent = $headerTop + "`n" + $llLongAligned + "`n" +
    $apLongTop + "`n" + $llApAligned + "`n" +
    $shortLLTop + "`n" + $shortLLAligned + "`n" +
    $shortAPTop + "`n" + $shortAPAligned + "`n" +
    $ensTop + "`n" + $ensAligned + "`n" +
    $failAligned + "`n" +
    $artTop + "`n" + $artAligned + "`n"

$reportContent | Out-File -FilePath $ReportPath -Encoding utf8 -Force
Write-Host "Report saved: $ReportPath" -ForegroundColor Green

# ============================================================
# CONSOLE SUMMARY TABLE
# ============================================================

Write-Host ""
Write-Host "ENSEMBLE SUMMARY (quick view):" -ForegroundColor Cyan
Write-Host ("{0,-30} {1,-12} {2,-8} {3,-8} {4,-12} {5,-8} {6,-8} {7,-12}" -f "Experiment","Status","LL PF","LL PnL","AP PF","AP PnL","Wall(m)","ArtOk")
Write-Host ("-" * 100)
foreach ($row in $allRows) {
    $artStr = if ($row.ArtifactOk) { "YES" } elseif ($null -eq $row.ArtifactOk) { "—" } else { "NO" }
    $color  = if ($row.Status -eq "COMPLETED") { "White" } else { "Yellow" }
    Write-Host ("{0,-30} {1,-12} {2,-8} {3,-8} {4,-12} {5,-8} {6,-8} {7,-12}" -f `
        $row.Label, $row.Status, $row.EnsLL_PF, "`$$($row.EnsLL_PnL)", $row.EnsAP_PF, "`$$($row.EnsAP_PnL)", $row.WallTimeMin, $artStr) -ForegroundColor $color
}
Write-Host ("-" * 100)

# ============================================================
# TELEGRAM: send condensed summary
# ============================================================

$tgLines = @("*Experiment Ensemble Results:*")
foreach ($row in $rows) {
    $tgLines += "• *$($row.Label)*: LL PF=$($row.EnsLL_PF) PnL=`$$($row.EnsLL_PnL) | AP PF=$($row.EnsAP_PF) PnL=`$$($row.EnsAP_PnL)"
}
if ($failRows.Count -gt 0) {
    $tgLines += "`n*Failed:* " + ($failRows | ForEach-Object { $_.Label }) -join ", "
}
$tgLines += "`n*Full report:* ``$ReportPath``"
Send-TelegramMessage ($tgLines -join "`n")

Write-Host ""
Write-Host "Done. Full report: $ReportPath" -ForegroundColor Green
