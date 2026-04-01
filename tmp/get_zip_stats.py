import zipfile
import json
import os

zf_path = "tmp/canary_artifacts.zip"

try:
    with zipfile.ZipFile(zf_path) as zf:
        # Find pipeline_summary.json
        summary_name = next((name for name in zf.namelist() if name.endswith("pipeline_summary.json")), None)
        
        if summary_name:
            content = zf.read(summary_name).decode("utf-8")
            data = json.loads(content)
            
            wall_time = data.get("wall_time_seconds", 0)
            minutes = wall_time / 60
            hours = minutes / 60
            
            bundles = data.get("registry_bundles", [])
            total_backtests = len(bundles)
            
            print(f"--- Pipeline Summary ---")
            print(f"Timestamp: {data.get('timestamp')}")
            print(f"Run Time: {hours:.2f} hours ({minutes:.1f} minutes, {wall_time:.0f} seconds)")
            print(f"Models Generated & Backtested: {total_backtests}")
        else:
            print("No pipeline_summary.json found in the zip.")
            
        print("\n--- Optuna Study Databases ---")
        studies = [n for n in zf.namelist() if n.endswith(".journal") or n.endswith(".db")]
        for s in studies:
            info = zf.getinfo(s)
            kb = info.file_size / 1024
            print(f"{os.path.basename(s):<40} | Size: {kb:.1f} KB")

except Exception as e:
    print(f"Failed to read zip: {e}")
