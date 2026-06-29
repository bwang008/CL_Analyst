import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.config.schemas import MasterConfig
from src.core.instrument_master import get_instrument
from src.data_paths import get_data_root

def download_from_gcs(gcs_uri, local_path):
    print(f"Downloading {gcs_uri} to {local_path}...")
    subprocess.run(["gsutil", "cp", gcs_uri, local_path], check=True)

def main():
    parser = argparse.ArgumentParser(description="Python E2E Orchestrator")
    parser.add_argument("--master-config", required=True)
    parser.add_argument("--gcs-data-bucket", default="gs://cltrainer-data/processed")
    parser.add_argument("--worker-threads", type=int, default=11)
    parser.add_argument("--db-dir", default="models/optuna_studies")
    args, unknown_args = parser.parse_known_args()

    config_path = Path(args.master_config)
    if not config_path.exists():
        print(f"FATAL: Config not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r") as f:
        master_config = MasterConfig(**json.load(f))

    if not master_config.training_workflow:
        print("FATAL: training_workflow missing from config")
        sys.exit(1)

    # 1. Download Dataset
    raw_version = master_config.data_workflow.dataset_version
    # Avoid redundant prefixing if version already starts with symbol
    if raw_version.upper().startswith(master_config.symbol.upper()):
        dataset_name = f"{raw_version}.parquet"
    else:
        dataset_name = f"{master_config.symbol}_{raw_version}.parquet"

    data_path = get_data_root() / "processed" / dataset_name
    data_path.parent.mkdir(parents=True, exist_ok=True)
    
    if not data_path.exists():
        gcs_uri = f"{args.gcs_data_bucket}/{dataset_name}"
        try:
            download_from_gcs(gcs_uri, str(data_path))
        except Exception as e:
            print(f"Warning: could not download {gcs_uri}: {e}")

    # 2. Parallel Optuna Search
    print("\n" + "="*50)
    print(" PHASE 1: Launching Parallel Optuna Searches")
    print("="*50)
    
    os.makedirs("reports", exist_ok=True)
    processes = []
    targets = master_config.training_workflow.target_columns
    worker_id = 1
    
    for target in targets:
        # Determine direction
        direction = "long" if "LONG" in target.upper() else "short"
        
        # We assume 1 metric for now (logloss) to keep it simple, or loop metrics if needed.
        study_name = f"sweep_{direction}_logloss"
        
        cmd = [
            sys.executable, "agent/optuna_lgbm_search_v2.py",
            "--master-config", args.master_config,
            "--target", target,
            "--study-name", study_name,
            "--db-dir", args.db_dir,
            "--worker-id", str(worker_id)
        ] + unknown_args
        
        print(f"Launching W{worker_id} for {target} (PID pending)...")
        log_file = open(f"reports/worker_{worker_id}.log", "w")
        p = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
        processes.append((p, target, log_file))
        worker_id += 1

    failed = 0
    for p, target, log_file in processes:
        p.wait()
        log_file.close()
        if p.returncode != 0:
            print(f"ERROR: Worker for {target} failed (exit code {p.returncode})")
            failed += 1
        else:
            print(f"SUCCESS: Worker for {target} completed")

    if failed > 0:
        print(f"FATAL: {failed} searches failed. Aborting E2E pipeline.")
        sys.exit(1)

    # 3. E2E Pipeline
    print("\n" + "="*50)
    print(" PHASE 2: E2E Pipeline")
    print("="*50)
    
    cmd = [
        sys.executable, "gcp/vm_e2e_pipeline.py",
        "--master-config", args.master_config,
        "--db-dir", args.db_dir
    ]
    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    main()
