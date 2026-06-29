#!/usr/bin/env python3
import argparse
import copy
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, Any
from pydantic import ValidationError

# 1. Setup Sys Path for Project Root Imports
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Now safely import relative files
from src.config.schemas import BatchSweepConfig, MasterConfig

# 2. Configure Logging specifically to Standard Error (sys.stderr)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)

def deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively merges overrides into a copy of the base dictionary.
    """
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and key in result and isinstance(result[key], dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result

def main():
    # 3. CLI Argument Parsing
    parser = argparse.ArgumentParser(description="GCP Python Batch Sweep Orchestrator")
    parser.add_argument(
        "--batch-manifest",
        type=str,
        required=True,
        help="Path to the JSON batch manifest containing baseline and overrides",
    )
    args = parser.parse_args()

    manifest_path = Path(args.batch_manifest)
    if not manifest_path.exists():
        logging.error(f"Batch manifest file does not exist: {manifest_path}")
        sys.exit(1)

    # 4. Load Manifest File
    logging.info(f"Loading batch manifest: {manifest_path}")
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_raw = json.load(f)
    except Exception as e:
        logging.error(f"Failed to load JSON from manifest file: {e}")
        sys.exit(1)

    # 5. Schema Validation (BatchSweepConfig)
    logging.info("Validating manifest file against BatchSweepConfig schema")
    try:
        batch_sweep = BatchSweepConfig.model_validate(manifest_raw)
    except ValidationError as e:
        logging.error(f"Manifest validation failed:\n{e}")
        sys.exit(1)

    # 6. Temporary Folder Creation for Merged JSON files
    # Note: We do NOT delete the temp folder during runtime, as the PowerShell VM deploy
    # task needs to read the generated configs. The files persist in OS temp.
    temp_dir = tempfile.mkdtemp(prefix="gcp_sweep_configs_")
    logging.info(f"Created temporary MasterConfig storage: {temp_dir}")

    # 7. Iterative Experiment Deep Merging & Validation
    baseline_dict = batch_sweep.baseline.model_dump()
    experiments_output = []

    for idx, exp in enumerate(batch_sweep.experiments):
        logging.info(f"Processing experiment [{idx + 1}/{len(batch_sweep.experiments)}]: {exp.label}")
        
        # Merge baseline with experiment specific overrides
        merged_dict = deep_merge(baseline_dict, exp.overrides)

        # Validate merged dictionary against MasterConfig
        try:
            validated_master = MasterConfig.model_validate(merged_dict)
        except ValidationError as e:
            logging.error(f"Re-validation failed for experiment '{exp.label}':\n{e}")
            sys.exit(1)

        # Write validated configuration back to temporary folder
        # Replace non-safe file characters in label for filename safety
        safe_label = "".join([c if c.isalnum() or c in ("-", "_") else "_" for c in exp.label])
        config_filename = f"{safe_label}_{exp.gcs_prefix}_config.json"
        config_path = Path(temp_dir) / config_filename

        try:
            with open(config_path, "w", encoding="utf-8") as out_f:
                out_f.write(validated_master.model_dump_json(indent=2))
        except Exception as e:
            logging.error(f"Failed writing temp configuration for '{exp.label}': {e}")
            sys.exit(1)

        logging.info(f"Successfully generated config path for '{exp.label}' -> {config_path}")

        experiments_output.append({
            "label": exp.label,
            "gcs_prefix": exp.gcs_prefix,
            "config_path": str(config_path.resolve())
        })

    # 8. Output Structured Results to stdout
    optuna_dump = None
    if batch_sweep.baseline.training_workflow and batch_sweep.baseline.training_workflow.optuna:
        optuna_dump = batch_sweep.baseline.training_workflow.optuna.model_dump()

    output_result = {
        "infrastructure_limits": {
            "max_concurrent_vms": batch_sweep.infrastructure.max_concurrent_vms,
            "max_concurrent_vcpus": batch_sweep.infrastructure.max_concurrent_vcpus,
            "vcpus_per_vm": batch_sweep.infrastructure.vcpus_per_vm,
            "machine_type": batch_sweep.infrastructure.machine_type,
            "provisioning_model": batch_sweep.infrastructure.provisioning_model,
            "timeout_minutes": batch_sweep.infrastructure.timeout_minutes,
        },
        "optuna_config": optuna_dump,
        "experiments": experiments_output
    }

    # Print pure JSON output
    print(json.dumps(output_result, indent=2))

if __name__ == "__main__":
    main()
