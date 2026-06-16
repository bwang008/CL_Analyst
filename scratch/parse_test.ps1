$errs = $null
[Management.Automation.Language.Parser]::ParseFile("C:\Users\bwang\Documents\GitHub\CL_Analyst_Development\gcp\run_sweep_batch.ps1", [ref]$null, [ref]$errs) | Out-Null
foreach ($err in $errs) {
    Write-Host "Error: $($err.Message)"
    Write-Host "Line: $($err.Extent.StartLineNumber) Char: $($err.Extent.StartColumnNumber)"
    break
}
