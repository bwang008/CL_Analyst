import json
import pytest
from pydantic import ValidationError
from src.config.schemas import BatchSweepConfig, InfrastructureConfig, ExperimentConfig, MasterConfig
from gcp.batch_orchestrator import deep_merge

def test_deep_merge():
    base = {
        "symbol": "CL",
        "data_workflow": {
            "dataset_version": "V1",
            "resolution": "1h"
        }
    }
    overrides = {
        "data_workflow": {
            "resolution": "5m"
        }
    }
    merged = deep_merge(base, overrides)
    assert merged["symbol"] == "CL"
    assert merged["data_workflow"]["dataset_version"] == "V1"
    assert merged["data_workflow"]["resolution"] == "5m"

def test_infrastructure_validation():
    # Valid config
    infra = InfrastructureConfig(
        max_concurrent_vms=8,
        max_concurrent_vcpus=288,
        vcpus_per_vm=16,
        machine_type="c2-standard-16",
        provisioning_model="STANDARD",
        timeout_minutes=120
    )
    assert infra.max_concurrent_vms == 8

    # Invalid values
    with pytest.raises(ValidationError):
        InfrastructureConfig(
            max_concurrent_vms=-1,  # Should be non-negative
            max_concurrent_vcpus=288,
            vcpus_per_vm=16
        )

    with pytest.raises(ValidationError):
        InfrastructureConfig(
            max_concurrent_vms=8,
            max_concurrent_vcpus=0,  # Should be positive
            vcpus_per_vm=16
        )

def test_batch_sweep_config_validation():
    raw_manifest = {
        "_comment": "Test batch sweep",
        "infrastructure": {
            "max_concurrent_vms": 2,
            "max_concurrent_vcpus": 100,
            "vcpus_per_vm": 44,
            "machine_type": "c2-standard-16",
            "provisioning_model": "STANDARD",
            "timeout_minutes": 120
        },
        "baseline": {
            "symbol": "CL",
            "data_workflow": {
                "dataset_version": "HourSet_08",
                "resolution": "1h",
                "targets": {
                    "raw_horizon": 120,
                    "atr_period": 14,
                    "definitions": [
                        {
                            "type": "triple_barrier",
                            "tp_multiplier": 3.0,
                            "sl_multiplier": 1.0,
                            "horizon": 6
                        }
                    ]
                }
            },
            "training_workflow": {
                "train_cutoff_date": "2022-01-01",
                "target_columns": ["TARGET_TRIPLE_3x1_6H_LONG"],
                "gcs_base_dir": "gs://test"
            },
            "execution_workflow": {
                "strategy_config_path": "configs/strategies/hourly_ensemble_008_2.json"
            }
        },
        "experiments": [
            {
                "label": "Exp1",
                "gcs_prefix": "prefix1",
                "overrides": {
                    "training_workflow": {
                        "target_columns": ["TARGET_TRIPLE_3x1_6H_SHORT"]
                    }
                }
            }
        ]
    }

    config = BatchSweepConfig.model_validate(raw_manifest)
    assert config.infrastructure.max_concurrent_vms == 2
    assert config.experiments[0].label == "Exp1"

    # Merge check
    baseline_dict = config.baseline.model_dump()
    merged = deep_merge(baseline_dict, config.experiments[0].overrides)
    validated = MasterConfig.model_validate(merged)
    assert validated.training_workflow.target_columns == ["TARGET_TRIPLE_3x1_6H_SHORT"]
