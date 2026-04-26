function CheckExitCode {
    $logFile = 'C:\Users\bwang\Documents\GitHub\CL_Analyst_Development\reports\canary_1x2_3h_20260425_1442\logs\canary_run_20260425_214545.log'
    $content = Get-Content $logFile -Raw
    $lines = Get-Content $logFile
    
    $report = New-Object PSObject -Property @{
        TerminationReason = 'UNKNOWN'
        E2ECompleted = $false
        Passed = 0
        Failed = 0
    }
    
    if ($content -match 'E2E PIPELINE COMPLETE') { $report.E2ECompleted = $true }
    
    $passLines = $lines | Where-Object { $_ -cmatch 'PASSED' }
    $failLines = $lines | Where-Object { $_ -cmatch 'FAILED' }
    $report.Passed = @($passLines).Count
    $report.Failed = @($failLines).Count
    
    if ($content -match 'E2E PIPELINE COMPLETE') {
        $report.TerminationReason = 'COMPLETED_OK'
    } else {
        $report.TerminationReason = 'INTERRUPTED'
    }
    
    $scriptExitCode = if ($report.E2ECompleted -and $report.Failed -eq 0) { 0 } else { 1 }
    
    Write-Host "$($report | ConvertTo-Json)"
    Write-Host "ScriptExitCode: $scriptExitCode"
    if ($report.TerminationReason -eq 'COMPLETED_OK' -and $scriptExitCode -eq 0) { Write-Host 'Final Exit: 0' } else { Write-Host 'Final Exit: 1' }
}
CheckExitCode
