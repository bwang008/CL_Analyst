import json
import os
import sys
from agent.batch_post_optimizer import generate_optimized_report

batch_dir = "reports/batch_runs/batch_20260518_2321"

with open(os.path.join(batch_dir, "batch_progress.json")) as f:
    progress = json.load(f)

with open(os.path.join(batch_dir, "optimization_results.json")) as f:
    all_results = json.load(f)

report = generate_optimized_report(
    batch_dir=batch_dir,
    progress=progress,
    all_results=all_results,
    ohlcv_path="",
    wall_time_seconds=8097, 
    n_trials=1500,
    n_workers=14
)

report_path = os.path.join(batch_dir, "batch_summary_optimized.md")
with open(report_path, "w", encoding="utf-8") as f:
    f.write(report)
print(f"Report saved to {report_path}")
