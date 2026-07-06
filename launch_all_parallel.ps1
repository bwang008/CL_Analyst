$batches = @(
    @{ file=".\gcp\run_sweep_batch.ps1"; manifest="configs\batch_manifest_v2_gc_hourset01b_scout.json" },
    @{ file=".\gcp\run_canary_batch.ps1"; manifest="configs\batch_manifest_v2_si_hourset01b_canary.json" },
    @{ file=".\gcp\run_sweep_batch.ps1"; manifest="configs\batch_manifest_v2_gc_hourset01a_scout.json" },
    @{ file=".\gcp\run_sweep_batch.ps1"; manifest="configs\batch_manifest_v2_si_hourset01a_scout.json" }
)

foreach ($b in $batches) {
    Write-Host "Launching $($b.manifest) in detached background process..."
    Start-Process -FilePath "powershell.exe" -ArgumentList "-ExecutionPolicy Bypass -File $($b.file) -ManifestPath $($b.manifest)" -WindowStyle Hidden
    
    Write-Host "Sleeping 65 seconds to guarantee a unique BatchId (timestamp)..."
    Start-Sleep -Seconds 65
}
Write-Host "All 4 batches launched in parallel!"
