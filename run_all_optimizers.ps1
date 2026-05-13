$configs = @(
    "configs\strategies\HourSet_08_Ensemble_01.json",
    "configs\strategies\HourSet_08_Ensemble_2x1_6H.json",
    "configs\strategies\HourSet_08_Ensemble_2x1_12H.json",
    "configs\strategies\HourSet_08_Ensemble_3x1_12H.json"
)

foreach ($config in $configs) {
    Write-Host "======================================================" -ForegroundColor Cyan
    Write-Host " STARTING OPTIMIZATION: $config" -ForegroundColor Cyan
    Write-Host "======================================================" -ForegroundColor Cyan
    python agent/strategy_optimizer.py --config $config --n-trials 1000 --data data/processed/CL_HourSet_08.parquet
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Optimization failed for $config" -ForegroundColor Red
    }
}

Write-Host "ALL OPTIMIZATIONS COMPLETE." -ForegroundColor Green
