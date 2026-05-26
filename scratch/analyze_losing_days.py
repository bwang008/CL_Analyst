import os
import sys
import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pandas.tseries.holiday import USFederalHolidayCalendar, AbstractHolidayCalendar, Holiday, EasterMonday, GoodFriday

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
        # First column is usually index
        df.index = pd.to_datetime(df.iloc[:, 0])
        df.index.name = "DateTime"
        df = df.iloc[:, 1:]
    return df

def run_backtest():
    config_path = "configs/strategies/HourSet_08_Ensemble_03_05242026.json"
    data_path = "C:\\CL_Analyst_Data\\data\processed\\CL_HourSet_08.parquet"
    slippage = 0.01

    with open(config_path) as f:
        strategy_cfg = json.load(f)

    # Recreate backtest engine
    bt = BacktestEngine.from_config(
        strategy_cfg,
        slippage_per_side=slippage,
    )

    # Load predictions
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
    
    print(f"Loading data: {data_path}")
    ohlcv_df = pd.read_parquet(data_path)
    
    print("Running programmatic backtest...")
    result = bt.run(preds, ohlcv_df, label="Historical")
    return result, ohlcv_df

def analyze_patterns(result: BacktestResult, ohlcv_df: pd.DataFrame):
    trades_df = result.to_dataframe()
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
    trades_df["entry_day_of_week"] = trades_df["entry_time"].dt.dayofweek # 0=Monday, 6=Sunday
    trades_df["is_long"] = trades_df["signal_side"] == "LONG"
    
    # 1. Get daily PnL & trade counts
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
        # returns (label, distance_in_days, holiday_date)
        d_dt = pd.to_datetime(d)
        
        # Check if it is a holiday
        if d in holidays_set:
            return "ON_HOLIDAY", 0, d
            
        # Check closest holiday
        closest_holiday = None
        min_dist = 999
        for h in holidays:
            dist = abs((h - d).days)
            if dist < min_dist:
                min_dist = dist
                closest_holiday = h
                
        # Classify based on distance and day of week
        # Long weekends: holiday on Monday, trade on Friday/Sunday before. Holiday on Friday, trade on Thursday/Sunday.
        day_idx = d_dt.dayofweek # 0=Mon, 4=Fri, 6=Sun
        
        # If holiday is Monday and d is Friday/Sunday before
        if closest_holiday and closest_holiday.weekday() == 0: # Monday holiday
            if d_dt.weekday() == 4 and (closest_holiday - d).days == 3: # Friday before Monday holiday
                return "BEFORE_LONG_WEEKEND", 3, closest_holiday
            if d_dt.weekday() == 6 and (closest_holiday - d).days == 1: # Sunday evening before Monday holiday
                return "BEFORE_LONG_WEEKEND", 1, closest_holiday
            if d_dt.weekday() == 1 and (d - closest_holiday).days == 1: # Tuesday after Monday holiday
                return "AFTER_LONG_WEEKEND", 1, closest_holiday
                
        # If holiday is Friday and d is Thursday before or Sunday after
        if closest_holiday and closest_holiday.weekday() == 4: # Friday holiday
            if d_dt.weekday() == 3 and (closest_holiday - d).days == 1: # Thursday before Friday holiday
                return "BEFORE_LONG_WEEKEND", 1, closest_holiday
            if d_dt.weekday() == 0 and (d - closest_holiday).days == 3: # Monday after Friday holiday
                return "AFTER_LONG_WEEKEND", 3, closest_holiday
                
        # Standard adjacent
        if min_dist == 1:
            if d < closest_holiday:
                return "DAY_BEFORE_HOLIDAY", 1, closest_holiday
            else:
                return "DAY_AFTER_HOLIDAY", 1, closest_holiday
        elif min_dist == 2:
            if d < closest_holiday:
                return "2_DAYS_BEFORE_HOLIDAY", 2, closest_holiday
            else:
                return "2_DAYS_AFTER_HOLIDAY", 2, closest_holiday
                
        return "REGULAR_DAY", min_dist, closest_holiday

    daily_pnl["holiday_class"], daily_pnl["holiday_dist"], daily_pnl["closest_holiday"] = zip(
        *daily_pnl["entry_date"].apply(classify_holiday_proximity)
    )
    
    trades_df["holiday_class"], trades_df["holiday_dist"], trades_df["closest_holiday"] = zip(
        *trades_df["entry_date"].apply(classify_holiday_proximity)
    )

    # Let's count losing days vs winning days by holiday classification
    holiday_summary = daily_pnl.groupby("holiday_class").agg(
        total_days=("entry_date", "count"),
        losing_days=("is_loss_day", "sum"),
        winning_days=("is_win_day", "sum"),
        total_pnl=("net_pnl", "sum"),
        avg_pnl=("net_pnl", "mean"),
    ).reset_index()
    holiday_summary["loss_day_pct"] = holiday_summary["losing_days"] / holiday_summary["total_days"] * 100
    
    # 2. Day of Week Breakdown for Days
    dow_summary = daily_pnl.groupby(["day_of_week", "day_name"]).agg(
        total_days=("entry_date", "count"),
        losing_days=("is_loss_day", "sum"),
        winning_days=("is_win_day", "sum"),
        total_pnl=("net_pnl", "sum"),
        avg_pnl=("net_pnl", "mean"),
    ).reset_index().sort_values("day_of_week")
    dow_summary["loss_day_pct"] = dow_summary["losing_days"] / dow_summary["total_days"] * 100
    
    # 3. Day of Week Breakdown for Trades
    dow_trades = trades_df.groupby(["entry_day_of_week", "entry_day_name"]).agg(
        total_trades=("net_pnl_dollars", "count"),
        winning_trades=("net_pnl_dollars", lambda x: (x > 0).sum()),
        losing_trades=("net_pnl_dollars", lambda x: (x < 0).sum()),
        total_pnl=("net_pnl_dollars", "sum"),
        avg_pnl=("net_pnl_dollars", "mean"),
    ).reset_index().sort_values("entry_day_of_week")
    dow_trades["win_rate"] = dow_trades["winning_trades"] / dow_trades["total_trades"] * 100

    # 4. Hourly Breakdown for Trades
    hour_trades = trades_df.groupby("entry_hour").agg(
        total_trades=("net_pnl_dollars", "count"),
        winning_trades=("net_pnl_dollars", lambda x: (x > 0).sum()),
        losing_trades=("net_pnl_dollars", lambda x: (x < 0).sum()),
        total_pnl=("net_pnl_dollars", "sum"),
        avg_pnl=("net_pnl_dollars", "mean"),
    ).reset_index().sort_values("entry_hour")
    hour_trades["win_rate"] = hour_trades["winning_trades"] / hour_trades["total_trades"] * 100

    # 5. Long vs Short breakdown on losing days
    # Let's see which side loses more on the losing days
    losing_days_dates = set(daily_pnl[daily_pnl["is_loss_day"]]["entry_date"])
    trades_on_losing_days = trades_df[trades_df["entry_date"].isin(losing_days_dates)]
    side_loss_summary = trades_on_losing_days.groupby("signal_side").agg(
        total_trades=("net_pnl_dollars", "count"),
        winning_trades=("net_pnl_dollars", lambda x: (x > 0).sum()),
        losing_trades=("net_pnl_dollars", lambda x: (x < 0).sum()),
        total_pnl=("net_pnl_dollars", "sum"),
        avg_pnl=("net_pnl_dollars", "mean"),
    ).reset_index()

    # 6. Worst 15 days analysis
    worst_days = daily_pnl.sort_values("net_pnl").head(15).copy()
    
    # 7. Print analysis reports in Markdown
    print("\n# BACKTEST LOSING DAYS PATTERN ANALYSIS")
    print(f"Analyzed {len(trades_df)} trades across {len(daily_pnl)} active trading days.")
    print(f"Total net profit: ${result.total_pnl:,.2f}")
    print(f"Winning days: {daily_pnl['is_win_day'].sum()} | Losing days: {daily_pnl['is_loss_day'].sum()} | Win Rate: {daily_pnl['is_win_day'].sum()/len(daily_pnl)*100:.1f}%\n")
    
    print("## 1. Day of the Week Breakdown (Daily PnL)")
    print(dow_summary.to_markdown(index=False))
    print("\n")
    
    print("## 2. Day of the Week Breakdown (Trade Level)")
    print(dow_trades.to_markdown(index=False))
    print("\n")
    
    print("## 3. Holiday and Long Weekend Proximity (Daily PnL)")
    print(holiday_summary.to_markdown(index=False))
    print("\n")
    
    print("## 4. Top 15 Worst Losing Days")
    print(worst_days[["entry_date", "day_name", "net_pnl", "trade_count", "holiday_class", "closest_holiday"]].to_markdown(index=False))
    print("\n")
    
    print("## 5. Entry Hour Performance Analysis")
    print(hour_trades.to_markdown(index=False))
    print("\n")
    
    print("## 6. Performance of Long vs Short on Losing Days")
    print(side_loss_summary.to_markdown(index=False))
    print("\n")

    # Let's save a structured markdown report
    output_path = os.path.join(PROJECT_ROOT, "reports", "losing_days_analysis_report.md")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# Backtest Worst Days and Losing Days Analysis\n\n")
        f.write(f"- **Strategy Config**: `HourSet_08_Ensemble_03_05242026.json`\n")
        f.write(f"- **Total Net PnL**: ${result.total_pnl:,.2f}\n")
        f.write(f"- **Total Trades**: {len(trades_df)}\n")
        f.write(f"- **Active Trading Days**: {len(daily_pnl)}\n")
        f.write(f"- **Winning Days**: {daily_pnl['is_win_day'].sum()} ({daily_pnl['is_win_day'].sum()/len(daily_pnl)*100:.1f}%)\n")
        f.write(f"- **Losing Days**: {daily_pnl['is_loss_day'].sum()} ({daily_pnl['is_loss_day'].sum()/len(daily_pnl)*100:.1f}%)\n\n")
        
        f.write("## 1. Day of the Week Breakdown (Daily PnL)\n")
        f.write("Do specific days of the week perform worse than others? In CME futures, trading starts Sunday evening and runs through Friday afternoon.\n\n")
        f.write(dow_summary.to_markdown(index=False) + "\n\n")
        
        f.write("## 2. Day of the Week Breakdown (Trade Level)\n")
        f.write(dow_trades.to_markdown(index=False) + "\n\n")
        
        f.write("## 3. Holiday and Long Weekend Proximity\n")
        f.write("Are losing days closely aligned with holidays or long weekends?\n")
        f.write("- **BEFORE_LONG_WEEKEND**: Friday or Sunday before a Monday holiday, or Thursday before a Friday holiday.\n")
        f.write("- **AFTER_LONG_WEEKEND**: Tuesday after a Monday holiday, or Monday after a Friday holiday.\n")
        f.write("- **DAY_BEFORE_HOLIDAY / DAY_AFTER_HOLIDAY**: 1 calendar day before or after a holiday.\n")
        f.write("- **ON_HOLIDAY**: Entered on the actual holiday calendar date (futures market may be open for shortened holiday hours).\n\n")
        f.write(holiday_summary.to_markdown(index=False) + "\n\n")
        
        f.write("## 4. Top 15 Worst Trading Days\n")
        f.write(worst_days[["entry_date", "day_name", "net_pnl", "trade_count", "holiday_class", "closest_holiday"]].to_markdown(index=False) + "\n\n")
        
        f.write("## 5. Entry Hour Performance Analysis\n")
        f.write("Which hours of the day generate the most/worst losing trades? (All times in EST/exchange timezone format as loaded in DataFrame)\n\n")
        f.write(hour_trades.to_markdown(index=False) + "\n\n")
        
        f.write("## 6. Analysis of Trade Types on Losing Days\n")
        f.write("On days when the overall strategy lost money, did LONG or SHORT trades drive the losses?\n\n")
        f.write(side_loss_summary.to_markdown(index=False) + "\n\n")
        
        # Add summary/insights section
        f.write("## Key Insights & Patterns\n")
        
        # We will compute these dynamically in python and write them
        # Day of week insights
        worst_day_row = dow_summary.sort_values("avg_pnl").iloc[0]
        f.write(f"- **Day of Week Bias**: **{worst_day_row['day_name']}** is the worst performing day of the week, with an average daily PnL of **${worst_day_row['avg_pnl']:,.2f}** and a daily losing rate of **{worst_day_row['loss_day_pct']:.1f}%**.\n")
        
        # Holiday insights
        holiday_losses = daily_pnl[daily_pnl["holiday_class"] != "REGULAR_DAY"]
        total_hol_pnl = holiday_losses["net_pnl"].sum()
        f.write(f"- **Holiday Proximity Impact**: Across all holiday-adjacent periods (before/after/on holiday), the strategy generated a total net PnL of **${total_hol_pnl:,.2f}**.\n")
        
        # Long weekend insights
        long_we_losses = daily_pnl[daily_pnl["holiday_class"].isin(["BEFORE_LONG_WEEKEND", "AFTER_LONG_WEEKEND"])]
        f.write(f"- **Long Weekend Behavior**: During long weekend transition periods, the strategy had **{long_we_losses['is_loss_day'].sum()}** losing days out of **{len(long_we_losses)}** active days ({long_we_losses['is_loss_day'].sum()/max(1, len(long_we_losses))*100:.1f}% loss rate), with a total PnL of **${long_we_losses['net_pnl'].sum():,.2f}**.\n")

    print(f"Saved detailed analysis report to: {output_path}")

if __name__ == "__main__":
    result, ohlcv_df = run_backtest()
    analyze_patterns(result, ohlcv_df)
