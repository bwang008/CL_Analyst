# Run live trader

python -m src.live_execution.live_trader --config configs/strategies/4h_ensemble_001.json  
python -m src.live_execution.live_trader --config configs/strategies/hourly_ensemble_004.json

# Run backtest

python agent/backtest_engine.py --config configs/strategies/4h_ensemble_001.json --data C:\CL_Analyst_Data\data\processed\cl-4h_bk_set_01.parquet
python agent/backtest_engine.py --config configs/strategies/hourly_ensemble_004.json --data C:\CL_Analyst_Data\data\processed\cl-1h_bk_HourSet_03.parquet