import os
import sys
import json
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pandas.tseries.holiday import USFederalHolidayCalendar, AbstractHolidayCalendar, GoodFriday

# Ensure project root is in path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent.backtest_engine import BacktestEngine, BacktestResult, _resolve_prob_column
from src.data_paths import resolve_cli_path

# Custom calendar that includes Good Friday and other key CME market holidays
class CMEMarketHolidayCalendar(AbstractHolidayCalendar):
    rules = USFederalHolidayCalendar.rules + [
        GoodFriday,
    ]

def get_market_holidays(start_date, end_date):
    cal = CMEMarketHolidayCalendar()
    holidays = cal.holidays(start=start_date, end=end_date)
    return pd.to_datetime(holidays).date

def load_predictions(path):
    path = resolve_cli_path(path)
    df = pd.read_csv(path)
    if "DateTime" in df.columns:
        df["DateTime"] = pd.to_datetime(df["DateTime"])
        df = df.set_index("DateTime")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime")
    elif df.index.name != "DateTime":
        df.index = pd.to_datetime(df.iloc[:, 0])
        df.index.name = "DateTime"
        df = df.iloc[:, 1:]
    return df

def parse_args():
    parser = argparse.ArgumentParser(description="Analyze trade patterns, losing days, and hourly execution filters.")
    parser.add_argument("--config", required=True, help="Path to strategy config JSON file")
    parser.add_argument("--data", required=True, help="Path to historical OHLCV parquet file")
    parser.add_argument("--slippage", type=float, default=0.01, help="Slippage per side in price units")
    parser.add_argument("--output", default="reports/losing_days_analysis_report.md", help="Path to output markdown report")
    return parser.parse_args()

def main():
    args = parse_args()
    
    # 1. Load Strategy Config
    config_path = resolve_cli_path(args.config)
    print(f"Loading strategy config: {config_path}")
    with open(config_path) as f:
        strategy_cfg = json.load(f)

    # 2. Recreate Backtest Engine
    bt = BacktestEngine.from_config(strategy_cfg, slippage_per_side=args.slippage)

    # 3. Load Predictions
    models_cfg = strategy_cfg.get("models", {})
    long_preds_path = resolve_cli_path(models_cfg.get("long", {}).get("predictions_path"))
    short_preds_path = resolve_cli_path(models_cfg.get("short", {}).get("predictions_path"))
    
    print(f"Loading Long predictions: {long_preds_path}")
    long_df = load_predictions(long_preds_path)
    print(f"Loading Short predictions: {short_preds_path}")
    short_df = load_predictions(short_preds_path)
    
    long_col = _resolve_prob_column(long_df, "buy")
    short_col = _resolve_prob_column(short_df, "sell")
    
    long_probs = long_df[[long_col]].rename(columns={long_col: "prob_Buy"})
    short_probs = short_df[[short_col]].rename(columns={short_col: "prob_Sell"})
    preds = long_probs.join(short_probs, how="outer").fillna(0.0)
    
    # 4. Load OHLCV Data
    data_path = resolve_cli_path(args.data)
    print(f"Loading OHLCV data: {data_path}")
    ohlcv_df = pd.read_parquet(data_path)
    
    # 5. Run Baseline Backtest
    print("Running Baseline Backtest...")
    base_result = bt.run(preds, ohlcv_df, label="Baseline")
    trades_df = base_result.to_dataframe()
    if trades_df.empty:
        print("No trades found in backtest!")
        return

    # Set up date features
    trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
    trades_df["exit_time"] = pd.to_datetime(trades_df["exit_time"])
    trades_df["entry_date"] = trades_df["entry_time"].dt.date
    trades_df["entry_day_name"] = trades_df["entry_time"].dt.day_name()
    trades_df["entry_hour"] = trades_df["entry_time"].dt.hour
    trades_df["entry_month"] = trades_df["entry_time"].dt.month
    trades_df["entry_year"] = trades_df["entry_time"].dt.year
    trades_df["entry_day_of_week"] = trades_df["entry_time"].dt.dayofweek
    trades_df["is_long"] = trades_df["signal_side"] == "LONG"
    
    # Get daily PnL & trade counts
    daily_pnl = trades_df.groupby("entry_date").agg(
        net_pnl=("net_pnl_dollars", "sum"),
        trade_count=("net_pnl_dollars", "count"),
        win_count=("net_pnl_dollars", lambda x: (x > 0).sum()),
        loss_count=("net_pnl_dollars", lambda x: (x < 0).sum()),
    ).reset_index()
    
    daily_pnl["entry_date_dt"] = pd.to_datetime(daily_pnl["entry_date"])
    daily_pnl["day_name"] = daily_pnl["entry_date_dt"].dt.day_name()
    daily_pnl["day_of_week"] = daily_pnl["entry_date_dt"].dt.dayofweek
    daily_pnl["is_loss_day"] = daily_pnl["net_pnl"] < 0
    daily_pnl["is_win_day"] = daily_pnl["net_pnl"] > 0
    
    # Get holidays
    min_date = trades_df["entry_time"].min().date()
    max_date = trades_df["entry_time"].max().date()
    holidays = get_market_holidays(min_date - timedelta(days=5), max_date + timedelta(days=5))
    holidays_set = set(holidays)
    
    # Analyze Holiday & Long Weekend Proximity
    def classify_holiday_proximity(d):
        d_dt = pd.to_datetime(d)
        if d in holidays_set:
            return "ON_HOLIDAY", 0, d
            
        closest_holiday = None
        min_dist = 999
        for h in holidays:
            dist = abs((h - d).days)
            if dist < min_dist:
                min_dist = dist
                closest_holiday = h
                
        # Long weekends check
        if closest_holiday and closest_holiday.weekday() == 0: # Monday holiday
            if d_dt.weekday() == 4 and (closest_holiday - d).days == 3: # Friday before
                return "BEFORE_LONG_WEEKEND", 3, closest_holiday
            if d_dt.weekday() == 6 and (closest_holiday - d).days == 1: # Sunday evening before
                return "BEFORE_LONG_WEEKEND", 1, closest_holiday
            if d_dt.weekday() == 1 and (d - closest_holiday).days == 1: # Tuesday after
                return "AFTER_LONG_WEEKEND", 1, closest_holiday
                
        if closest_holiday and closest_holiday.weekday() == 4: # Friday holiday
            if d_dt.weekday() == 3 and (closest_holiday - d).days == 1: # Thursday before
                return "BEFORE_LONG_WEEKEND", 1, closest_holiday
            if d_dt.weekday() == 0 and (d - closest_holiday).days == 3: # Monday after
                return "AFTER_LONG_WEEKEND", 3, closest_holiday
                
        if min_dist == 1:
            return "DAY_BEFORE_HOLIDAY" if d < closest_holiday else "DAY_AFTER_HOLIDAY", 1, closest_holiday
        elif min_dist == 2:
            return "2_DAYS_BEFORE_HOLIDAY" if d < closest_holiday else "2_DAYS_AFTER_HOLIDAY", 2, closest_holiday
                
        return "REGULAR_DAY", min_dist, closest_holiday

    daily_pnl["holiday_class"], daily_pnl["holiday_dist"], daily_pnl["closest_holiday"] = zip(
        *daily_pnl["entry_date"].apply(classify_holiday_proximity)
    )
    
    trades_df["holiday_class"], trades_df["holiday_dist"], trades_df["closest_holiday"] = zip(
        *trades_df["entry_date"].apply(classify_holiday_proximity)
    )

    # Summarize holiday metrics
    holiday_summary = daily_pnl.groupby("holiday_class").agg(
        total_days=("entry_date", "count"),
        losing_days=("is_loss_day", "sum"),
        winning_days=("is_win_day", "sum"),
        total_pnl=("net_pnl", "sum"),
        avg_pnl=("net_pnl", "mean"),
    ).reset_index()
    holiday_summary["loss_day_pct"] = holiday_summary["losing_days"] / holiday_summary["total_days"] * 100
    
    # Day of Week Breakdown (Daily PnL)
    dow_summary = daily_pnl.groupby(["day_of_week", "day_name"]).agg(
        total_days=("entry_date", "count"),
        losing_days=("is_loss_day", "sum"),
        winning_days=("is_win_day", "sum"),
        total_pnl=("net_pnl", "sum"),
        avg_pnl=("net_pnl", "mean"),
    ).reset_index().sort_values("day_of_week")
    dow_summary["loss_day_pct"] = dow_summary["losing_days"] / dow_summary["total_days"] * 100
    
    # Day of Week Breakdown (Trades)
    dow_trades = trades_df.groupby(["entry_day_of_week", "entry_day_name"]).agg(
        total_trades=("net_pnl_dollars", "count"),
        winning_trades=("net_pnl_dollars", lambda x: (x > 0).sum()),
        losing_trades=("net_pnl_dollars", lambda x: (x < 0).sum()),
        total_pnl=("net_pnl_dollars", "sum"),
        avg_pnl=("net_pnl_dollars", "mean"),
    ).reset_index().sort_values("entry_day_of_week")
    dow_trades["win_rate"] = dow_trades["winning_trades"] / dow_trades["total_trades"] * 100

    # Hourly Breakdown (Trades)
    hour_trades = trades_df.groupby("entry_hour").agg(
        total_trades=("net_pnl_dollars", "count"),
        winning_trades=("net_pnl_dollars", lambda x: (x > 0).sum()),
        losing_trades=("net_pnl_dollars", lambda x: (x < 0).sum()),
        total_pnl=("net_pnl_dollars", "sum"),
        avg_pnl=("net_pnl_dollars", "mean"),
    ).reset_index().sort_values("entry_hour")
    hour_trades["win_rate"] = hour_trades["winning_trades"] / hour_trades["total_trades"] * 100

    # Performance on losing days: Long vs Short
    losing_days_dates = set(daily_pnl[daily_pnl["is_loss_day"]]["entry_date"])
    trades_on_losing_days = trades_df[trades_df["entry_date"].isin(losing_days_dates)]
    side_loss_summary = trades_on_losing_days.groupby("signal_side").agg(
        total_trades=("net_pnl_dollars", "count"),
        winning_trades=("net_pnl_dollars", lambda x: (x > 0).sum()),
        losing_trades=("net_pnl_dollars", lambda x: (x < 0).sum()),
        total_pnl=("net_pnl_dollars", "sum"),
        avg_pnl=("net_pnl_dollars", "mean"),
    ).reset_index()

    # Worst 15 days
    worst_days = daily_pnl.sort_values("net_pnl").head(15).copy()

    # 6. RUN DYNAMIC SIMULATION BLOCKING RULES
    print("Running hour-blocking simulations...")
    simulations = []
    
    # Configs to check: [None, 8, 9, 10, 11, [9, 11]]
    blocking_configs = [
        {"name": "Baseline (All Hours)", "block": []},
        {"name": "Block 8:00 AM Bar (9 AM fill)", "block": [8]},
        {"name": "Block 9:00 AM Bar (10 AM fill)", "block": [9]},
        {"name": "Block 10:00 AM Bar (11 AM fill)", "block": [10]},
        {"name": "Block 11:00 AM Bar (12 PM fill)", "block": [11]},
        {"name": "Block both 9:00 AM & 11:00 AM Bars", "block": [9, 11]},
    ]
    
    for cfg in blocking_configs:
        sim_preds = preds.copy()
        if cfg["block"]:
            sim_preds.loc[sim_preds.index.hour.isin(cfg["block"]), ["prob_Buy", "prob_Sell"]] = 0.0
        
        sim_bt = BacktestEngine.from_config(strategy_cfg, slippage_per_side=args.slippage)
        sim_res = sim_bt.run(sim_preds, ohlcv_df)
        sim_trades = sim_res.to_dataframe()
        
        simulations.append({
            "Configuration": cfg["name"],
            "Trades": len(sim_trades),
            "Win Rate": f"{sim_res.win_rate*100:.1f}%",
            "Net PnL": f"${sim_res.total_pnl:,.2f}",
            "Max Drawdown": f"${sim_res.max_drawdown:,.2f}",
            "Profit Factor": f"{sim_res.profit_factor:.2f}",
            "Net PnL Value": sim_res.total_pnl
        })
        
    sim_df = pd.DataFrame(simulations)
    
    # 7. Write Markdown Report
    output_report_path = resolve_cli_path(args.output)
    os.makedirs(os.path.dirname(output_report_path) or ".", exist_ok=True)
    
    with open(output_report_path, "w", encoding="utf-8") as f:
        f.write(f"# Backtest Trade Pattern Analysis Report\n\n")
        f.write(f"- **Strategy Config**: `{os.path.basename(args.config)}`\n")
        f.write(f"- **Total Net PnL**: ${base_result.total_pnl:,.2f}\n")
        f.write(f"- **Total Trades**: {len(trades_df)}\n")
        f.write(f"- **Active Trading Days**: {len(daily_pnl)}\n")
        f.write(f"- **Winning Days**: {daily_pnl['is_win_day'].sum()} ({daily_pnl['is_win_day'].sum()/len(daily_pnl)*100:.1f}%)\n")
        f.write(f"- **Losing Days**: {daily_pnl['is_loss_day'].sum()} ({daily_pnl['is_loss_day'].sum()/len(daily_pnl)*100:.1f}%)\n\n")
        
        f.write("## 1. Day of the Week Breakdown (Daily PnL)\n")
        f.write("Evaluation of daily trading outcomes based on day of week. Futures trading begins Sunday afternoon (evening EST) and ends Friday.\n\n")
        f.write(dow_summary.to_markdown(index=False) + "\n\n")
        
        f.write("## 2. Day of the Week Breakdown (Trade Level)\n")
        f.write(dow_trades.to_markdown(index=False) + "\n\n")
        
        f.write("## 3. Holiday and Long Weekend Proximity (Daily PnL)\n")
        f.write("Analyzes if trade entries adjacent to holidays suffer from structural volatility gaps:\n")
        f.write("- **BEFORE_LONG_WEEKEND**: Friday before a Monday holiday, or Thursday before a Friday holiday.\n")
        f.write("- **AFTER_LONG_WEEKEND**: Tuesday after a Monday holiday, or Monday after a Friday holiday.\n")
        f.write("- **DAY_BEFORE_HOLIDAY / DAY_AFTER_HOLIDAY**: 1 calendar day before or after a holiday.\n")
        f.write("- **ON_HOLIDAY**: Shortened holiday trading sessions.\n\n")
        f.write(holiday_summary.to_markdown(index=False) + "\n\n")
        
        f.write("## 4. Top 15 Worst Trading Days\n")
        f.write(worst_days[["entry_date", "day_name", "net_pnl", "trade_count", "holiday_class", "closest_holiday"]].to_markdown(index=False) + "\n\n")
        
        f.write("## 5. Entry Hour Performance Analysis\n")
        f.write("Breakdown of performance by the **start** hour of the bar on which the signal is generated.\n\n")
        f.write(hour_trades.to_markdown(index=False) + "\n\n")
        
        f.write("## 6. Performance of Long vs Short on Losing Days\n")
        f.write("Evaluates whether buy or sell side trades are driving down outcomes on losing days.\n\n")
        f.write(side_loss_summary.to_markdown(index=False) + "\n\n")
        
        f.write("## 7. Dynamic Hour-Blocking Simulations\n")
        f.write("Simulates blocking specific hourly bar signals to isolate pit opens, midday churn, and post-report whipsaws:\n\n")
        f.write(sim_df.to_markdown(index=False) + "\n\n")
        
        # Key Insights
        f.write("## 🔍 Key Insights & Patterns\n")
        worst_day_row = dow_summary.sort_values("avg_pnl").iloc[0]
        f.write(f"- **Day of Week Bias**: **{worst_day_row['day_name']}** represents the worst performing day, averaging **${worst_day_row['avg_pnl']:,.2f}** daily returns with a loss rate of **{worst_day_row['loss_day_pct']:.1f}%**.\n")
        
        hol_days = daily_pnl[daily_pnl["holiday_class"] != "REGULAR_DAY"]
        total_hol_pnl = hol_days["net_pnl"].sum()
        f.write(f"- **Holiday Proximity**: Cumulative returns during all holiday-adjacent transitions equal **${total_hol_pnl:,.2f}**.\n")
        
        best_sim = sim_df.sort_values("Net PnL Value", ascending=False).iloc[0]
        f.write(f"- **Optimal Execution Block**: The best simulated blocking configuration is **{best_sim['Configuration']}**, resulting in a net PnL of **{best_sim['Net PnL']}** and Max Drawdown of **{best_sim['Max Drawdown']}**.\n")

    print(f"\nAnalysis completed successfully! Detailed report saved to: {output_report_path}")

if __name__ == "__main__":
    main()
