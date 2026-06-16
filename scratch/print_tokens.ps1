$tokens = $null
$errs = $null
[Management.Automation.Language.Parser]::ParseFile("gcp\run_sweep_batch.ps1", [ref]$tokens, [ref]$errs) | Out-Null
$tokens[-30..-1] | ForEach-Object { Write-Host "$($_.TokenFlags) : $($_.Text)" }
