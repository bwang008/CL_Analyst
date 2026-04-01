import subprocess, sys, os
result = subprocess.run(
    [sys.executable, 'agent/backtest_engine.py',
     '--config', 'configs/strategies/hourly_ensemble_003.json',
     '--data', 'data/cl-1h_bk_HourSet_03.parquet'],
    capture_output=True, text=True, cwd='.'
)
out = result.stdout + result.stderr
with open('tmp/backtest_hourset03.txt', 'w', encoding='utf-8') as f:
    f.write(out)
print('exit_code=' + str(result.returncode))
if result.returncode != 0:
    print(out[-2000:])
else:
    print('done -> tmp/backtest_hourset03.txt')
