---
description: Analyze trade patterns, losing days, and hour-blocking filters
---

# Analyze Trade Patterns Workflow

Use this workflow to systematically analyze a strategy backtest to detect structural weaknesses such as:
1. **Day of Week Biases**: Recognizing which days (e.g. Tuesdays) suffer from pre-report inventory positioning.
2. **Holiday Transition Gaps**: Identifying toxic periods surrounding long weekends.
3. **Execution Hour Volatility**: Isolating false breakout traps at NYMEX pit opens or EIA inventory report releases.
4. **Dynamic Hour-Blocking Filters**: Running backtest simulations to mathematically prove the impact of ignoring specific hour signals.

---

## 🏃‍♂️ How to Run the Analysis

To run the automated analysis on your model's backtest:

1. Activate your trading environment:
   ```bash
   conda activate trader
   ```

2. Execute the pattern analyzer script, passing the strategy config and the matching processed feature dataset:
   ```bash
   python scripts/analyze_trade_patterns.py --config configs/strategies/<STRATEGY_CONFIG>.json --data "C:\CL_Analyst_Data\data\processed\<DATA_SET>.parquet" --output reports/<REPORT_NAME>_trade_patterns.md
   ```

   *Example for HourSet_08 Ensemble:*
   ```bash
   python scripts/analyze_trade_patterns.py --config configs/strategies/HourSet_08_Ensemble_03_05242026.json --data "C:\CL_Analyst_Data\data\processed\CL_HourSet_08.parquet" --output reports/losing_days_analysis_report.md
   ```

3. Review the generated markdown report located at `reports/<REPORT_NAME>_trade_patterns.md`.

---

## 🔍 Key Indicators to Inspect in the Report

When reviewing the generated report, inspect these sections for key optimizations:

### 1. Day of the Week Performance (Section 1)
*   **The Tuesday Anomaly**: In crude oil, pre-API inventory report squeezes occur on Tuesday afternoons. Check if Tuesdays represent an outsized portion of losses. If daily PnL is negative on Tuesdays, consider restricting entries on that day.

### 2. Holiday and Long Weekend Transitions (Section 3)
*   **Long Weekend Gaps**: Check the PnL of **`BEFORE_LONG_WEEKEND`** and **`AFTER_LONG_WEEKEND`** categories. Markets usually have low liquidity and erratic positioning adjustments immediately before/after long weekends, triggering protective stops. If toxic, add holiday transition gates.

### 3. Hourly Bar Performance (Section 5)
*   **The NYMEX Pit Open Whipsaw**: Look at the performance of the **`08:00:00`** bar. Its signals execute at **09:00:00 EST** (the Pit Open). Opening range noise commonly blows past ATR parameters.
*   **The EIA Inventory Whipsaw**: Look at the **`11:00:00`** bar. Its signals execute at **12:00:00 PM EST** (following the 10:30 AM EIA report). Post-report reversals and midday liquidity drains often make this a toxic trap.

### 4. Simulation Summary Results (Section 7)
*   Review the **Dynamic Hour-Blocking Simulations** table. It isolates specific hourly signals programmatically inside the backtest engine FSM. 
*   **Actionable Decision**: Implement the configuration that shows the highest net PnL and lowest drawdown. (e.g., blocking `08:00:00` and `11:00:00` bars while keeping `10:00:00` intact).

---

## 🛠 Applying Optimizations to the Strategy

If the report highlights actionable improvements:

1. To block toxic hours, modify the signal generator or pre-processor pipeline to force probabilities to `0.0` during the toxic windows.
2. To require stronger confirmation before entering in volatile zones, increase the `"consecutive_signal_threshold"` to `2` or higher in the strategy config file.
