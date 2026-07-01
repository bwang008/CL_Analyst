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

def mock_gcs_path(gcs_uri: str) -> str:
    """Helper to strip gs://bucket/ and route to tests/sandbox/mock_gcs/"""
    import os
    if not gcs_uri.startswith("gs://"):
        return gcs_uri
    
    parts = gcs_uri[5:].split("/", 1)
    path = parts[1] if len(parts) > 1 else ""
    
    mock_base = os.path.join(os.getcwd(), "tests", "sandbox", "mock_gcs")
    final_path = os.path.join(mock_base, path)
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    return final_path

def download_from_gcs(gcs_uri, local_path):
    is_sandbox = os.environ.get("IS_LOCAL_SANDBOX", "False").lower() == "true"
    if is_sandbox:
        import shutil
        mock_path = mock_gcs_path(gcs_uri)
        print(f"[SANDBOX] Copying {mock_path} to {local_path} instead of {gcs_uri}...")
        if os.path.exists(mock_path):
            if os.path.isdir(mock_path):
                shutil.copytree(mock_path, local_path, dirs_exist_ok=True)
            else:
                shutil.copy(mock_path, local_path)
        else:
            print(f"[SANDBOX] Warning: Mock source missing at {mock_path}")
            # Touch the file to prevent hard crashes downstream if it's just a mock
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w") as f:
                f.write("mock")
    else:
        print(f"Downloading {gcs_uri} to {local_path}...")
        subprocess.run(["gsutil", "cp", gcs_uri, local_path], check=True)

def main():
    parser = argparse.ArgumentParser(description="Python E2E Orchestrator")
    parser.add_argument("--master-config", required=True)
    parser.add_argument("--gcs-data-bucket", default="gs://cltrainer-optuna-results/data")
    parser.add_argument("--worker-threads", type=int, default=11)
    parser.add_argument("--db-dir", default="models/optuna_studies")
    # Job-scoped GCS prefix for artifact/summary upload (consumed by Phase 2).
    # Declared here so it is not forwarded to the Phase 1 search workers.
    parser.add_argument("--gcs-prefix", default="")
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

    # Download execution data if it is a GCS path
    if master_config.execution_workflow and master_config.execution_workflow.execution_data_path:
        exec_uri = master_config.execution_workflow.execution_data_path
        if exec_uri.startswith("gs://"):
            exec_name = exec_uri.split("/")[-1]
            local_exec_path = get_data_root() / "processed" / exec_name
            if not local_exec_path.exists():
                try:
                    download_from_gcs(exec_uri, str(local_exec_path))
                except Exception as e:
                    print(f"Warning: could not download {exec_uri}: {e}")
            
            # Update the config to point to the local path for vm_e2e_pipeline.py
            master_config.execution_workflow.execution_data_path = str(local_exec_path)
            with open(config_path, "w") as f:
                f.write(master_config.model_dump_json(indent=2))

    # 2. Parallel Optuna Search
    print("\n" + "="*50)
    print(" PHASE 1: Launching Parallel Optuna Searches")
    print("="*50)
    
    os.makedirs("reports", exist_ok=True)
    processes = []
    targets = master_config.training_workflow.target_columns
    metrics = master_config.training_workflow.optuna.metrics
    # Global RNG seed (REQUIRED in the manifest — schema crashes if missing). Threaded to
    # every stochastic stage so sweeps are reproducible. Passed with the EXACT flag name
    # --random-seed that the Task 2 consumers (optuna_lgbm_search_v2.py, vm_e2e_pipeline.py)
    # expect.
    random_seed = master_config.training_workflow.random_seed
    # One worker per (target, metric). Cap LightGBM threads so all concurrent
    # workers together stay within the VM's core budget (avoid oversubscription).
    total_workers = max(1, len(targets) * len(metrics))
    threads_per_worker = max(1, (os.cpu_count() or total_workers) // total_workers)
    worker_id = 1

    for target in targets:
        # Determine direction
        direction = "long" if "LONG" in target.upper() else "short"

        for metric in metrics:
            study_name = f"sweep_{direction}_{metric}"

            cmd = [
                sys.executable, "agent/optuna_lgbm_search_v2.py",
                "--master-config", args.master_config,
                "--target", target,
                "--ml-metric", metric,
                "--study-name", study_name,
                "--db-dir", args.db_dir,
                "--num-threads", str(threads_per_worker),
                "--worker-id", str(worker_id),
                "--random-seed", str(random_seed)
            ] + unknown_args

            print(f"Launching W{worker_id} for {target} [{metric}] (threads={threads_per_worker})...")
            log_file = open(f"reports/worker_{worker_id}.log", "w")
            p = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
            processes.append((p, f"{target} [{metric}]", log_file))
            worker_id += 1

    failed = 0
    for p, label, log_file in processes:
        p.wait()
        log_file.close()
        if p.returncode != 0:
            print(f"ERROR: Worker for {label} failed (exit code {p.returncode})")
            failed += 1
        else:
            print(f"SUCCESS: Worker for {label} completed")

    if failed > 0:
        print(f"FATAL: {failed} searches failed. Aborting E2E pipeline.")
        sys.exit(1)

    # 3. E2E Pipeline
    print("\n" + "="*50)
    print(" PHASE 2: E2E Pipeline")
    print("="*50)
    
    post_opt_trials = master_config.training_workflow.optuna.post_optimizer_trials
    cmd = [
        sys.executable, "gcp/vm_e2e_pipeline.py",
        "--master-config", args.master_config,
        "--db-dir", args.db_dir,
        "--opt-trials", str(post_opt_trials),
        "--study-prefix", "sweep",
        "--random-seed", str(random_seed),
        "--metrics", *metrics,
    ]
    if args.gcs_prefix:
        # Upload artifacts/pipeline_summary.json to gs://bucket/<job>/ so the
        # batch monitor finds them. (VM shutdown is handled by vm_sweep_run.sh
        # AFTER this log is uploaded, to avoid racing the completion marker.)
        cmd += ["--gcs-prefix", args.gcs_prefix]
    subprocess.run(cmd, check=True)

    # Completion marker the batch monitor (gcp_monitor.ps1) greps for to mark
    # the run as successfully completed.
    print("E2E PIPELINE COMPLETE")

if __name__ == "__main__":
    main()
