import subprocess

try:
    out = subprocess.check_output(
        ["python", "agent/backtest_engine.py", "--config", "configs/strategies/hourly_ensemble_002.json", "--data", "data/cl-1h_bk_HourSet_02.parquet", "--slippage-per-side", "0.01"],
        universal_newlines=True, encoding="utf-8"
    )
    with open("artifacts/final_run.txt", "w", encoding="utf-8") as f:
        f.write(out)
    print("Successfully wrote artifacts/final_run.txt")
except Exception as e:
    print(f"Failed: {e}")
