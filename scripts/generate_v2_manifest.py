import argparse
import json
import os
import sys

# Ensure we can import from src
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.config.schemas import (
    BatchSweepConfig, InfrastructureConfig, MasterConfig, 
    DataWorkflowConfig, FeatureConfig, TargetConfig, SingleBarrierTarget,
    TrainingWorkflowConfig, ExecutionWorkflowConfig, ExperimentConfig, OptunaConfig
)

def generate_manifest(args):
    dataset_prefix = args.dataset_version.split('_')[-1].lower() # e.g. '14a'
    
    # 1. Optuna Config
    optuna = OptunaConfig(
        n_trials=args.n_trials,
        max_depth_min=args.max_depth_min,
        max_depth_max=args.max_depth_max,
        num_leaves_min=args.num_leaves_min,
        num_leaves_max=args.num_leaves_max,
        max_n_estimators=args.max_n_estimators,
        early_stopping_rounds=args.early_stopping_rounds,
        max_folds=args.max_folds,
        learning_rate_min=args.learning_rate_min,
        learning_rate_max=args.learning_rate_max,
        min_child_samples_min=args.min_child_samples_min,
        min_child_samples_max=args.min_child_samples_max,
        feature_fraction_min=args.feature_fraction_min,
        feature_fraction_max=args.feature_fraction_max,
        post_optimizer_trials=args.post_optimizer_trials,
        post_optimizer_holdout_months=args.post_optimizer_holdout_months
    )

    # 2. Infrastructure Config
    infra = InfrastructureConfig(
        machine_type="c2-standard-16",
        provisioning_model="STANDARD",
        timeout_minutes=240,
        max_concurrent_vcpus=288,
        vcpus_per_vm=16,
        max_concurrent_vms=12
    )

    # 3. Baseline Master Config (Default symbol is the first one, but overridden per experiment)
    baseline = MasterConfig(
        symbol=args.symbols[0],
        data_workflow=DataWorkflowConfig(
            dataset_version=args.dataset_version,
            resolution="1h",
            features=FeatureConfig(
                windows=[24, 72, 168],
                macro_windows={"1W": 168}
            ),
            targets=TargetConfig(
                raw_horizon=120,
                atr_period=14,
                definitions=[
                    SingleBarrierTarget(type="triple_barrier", tp_multiplier=2.0, sl_multiplier=1.0, horizon=6),
                    SingleBarrierTarget(type="triple_barrier", tp_multiplier=3.0, sl_multiplier=1.0, horizon=6)
                ]
            )
        ),
        training_workflow=TrainingWorkflowConfig(
            train_cutoff_date="2022-01-01",
            holdout_cutoff_date="2026-01-01",
            target_columns=[], # To be overridden per experiment
            gcs_base_dir="gs://cltrainer-optuna-results/canary",
            optuna=optuna
        ),
        execution_workflow=ExecutionWorkflowConfig(
            slippage_multiplier=0.01,
            strategy_config_path="configs/strategies/hourly_ensemble_010.json"
        )
    )

    # 4. Generate Experiments per Symbol and Target
    experiments = []
    targets = [
        ("2x1 6H", ["TARGET_TRIPLE_2x1_6H_LONG", "TARGET_TRIPLE_2x1_6H_SHORT"]),
        ("3x1 6H", ["TARGET_TRIPLE_3x1_6H_LONG", "TARGET_TRIPLE_3x1_6H_SHORT"])
    ]
    
    for symbol in args.symbols:
        for tgt_label, tgt_cols in targets:
            # e.g., CL HS14A 2x1 6H -> However, to match the user's legacy labels:
            if len(args.symbols) == 1 and symbol == "CL":
                exp_label = f"HS{dataset_prefix.upper()} {tgt_label}"
                gcs_prefix = f"sweep_hs{dataset_prefix}_{tgt_label.replace(' ', '_').lower()}_canary"
            else:
                exp_label = f"{symbol} HS{dataset_prefix.upper()} {tgt_label}"
                gcs_prefix = f"sweep_{symbol.lower()}_hs{dataset_prefix}_{tgt_label.replace(' ', '_').lower()}_canary"
            
            # Create overrides
            overrides = {
                "symbol": symbol,
                "training_workflow": {
                    "target_columns": tgt_cols
                }
            }
            
            experiments.append(ExperimentConfig(
                label=exp_label,
                gcs_prefix=gcs_prefix,
                overrides=overrides
            ))

    # 5. Assemble Batch Sweep Config
    batch_config = BatchSweepConfig(
        infrastructure=infra,
        baseline=baseline,
        experiments=experiments
    )

    # 6. Write JSON
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        f.write(batch_config.model_dump_json(indent=2, by_alias=True, exclude_unset=False))
        
    print(f"Successfully generated V2 manifest: {args.output}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=str, required=True, help="Output JSON path")
    parser.add_argument("--dataset-version", type=str, default="HourSet_14A")
    parser.add_argument("--symbols", type=str, nargs="+", default=["CL"])
    
    # Optuna Args
    parser.add_argument("--n-trials", type=int, default=200)
    parser.add_argument("--max-depth-min", type=int, default=3)
    parser.add_argument("--max-depth-max", type=int, default=10)
    parser.add_argument("--num-leaves-min", type=int, default=15)
    parser.add_argument("--num-leaves-max", type=int, default=100)
    parser.add_argument("--max-n-estimators", type=int, default=2000)
    parser.add_argument("--early-stopping-rounds", type=int, default=25)
    parser.add_argument("--max-folds", type=int, default=5)
    parser.add_argument("--learning-rate-min", type=float, default=0.005)
    parser.add_argument("--learning-rate-max", type=float, default=0.02)
    parser.add_argument("--min-child-samples-min", type=int, default=150)
    parser.add_argument("--min-child-samples-max", type=int, default=400)
    parser.add_argument("--feature-fraction-min", type=float, default=0.3)
    parser.add_argument("--feature-fraction-max", type=float, default=1.0)
    parser.add_argument("--post-optimizer-trials", type=int, default=3)
    parser.add_argument("--post-optimizer-holdout-months", type=int, default=6)
    
    args = parser.parse_args()
    generate_manifest(args)
