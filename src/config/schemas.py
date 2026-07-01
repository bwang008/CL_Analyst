from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Dict, Optional, Literal, Union, Any
import pandas as pd

class SingleBarrierTarget(BaseModel):
    type: Literal["triple_barrier"]
    tp_multiplier: float
    sl_multiplier: float
    horizon: int

class GridBarrierTarget(BaseModel):
    type: Literal["triple_barrier_grid"]
    tp_multipliers: List[float]
    sl_multiplier: float  # Kept as a single float for pipeline consistency (typically SL=1.0)
    horizons: List[int]

class ReturnTarget(BaseModel):
    type: Literal["continuous_return"]
    horizons: List[int]

# Pydantic will look at the 'type' field to strictly route the validation
TargetDefinition = Union[SingleBarrierTarget, GridBarrierTarget, ReturnTarget]

class TargetConfig(BaseModel):
    raw_horizon: int = 120
    atr_period: int = 14
    definitions: List[TargetDefinition]

class FeatureConfig(BaseModel):
    windows: List[int] = [24, 72, 168, 336, 840]
    macro_windows: Dict[str, int] = {"1W": 168, "2W": 336, "1M": 840, "3M": 2160, "6M": 4320}
    include_momentum: bool = True
    include_macro: bool = True
    include_extended: bool = True
    include_dma: bool = True
    include_ichimoku: bool = True
    include_term_structure: bool = True
    drop_features: List[str] = Field(default_factory=list)
    keep_features: List[str] = Field(default_factory=list)

    @field_validator("windows")
    @classmethod
    def check_windows_positive(cls, v):
        if any(w <= 0 for w in v):
            raise ValueError("All window sizes must be strictly positive.")
        return v

    @field_validator("macro_windows")
    @classmethod
    def check_macro_windows_positive(cls, v):
        if any(w <= 0 for w in v.values()):
            raise ValueError("All macro window sizes must be strictly positive.")
        return v

class DataWorkflowConfig(BaseModel):
    dataset_version: str
    resolution: str = "1h"
    output_dir: str = "data/processed"
    output_filename: Optional[str] = None
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    targets: TargetConfig

class OptunaConfig(BaseModel):
    n_trials: int = 200
    metrics: List[str] = ["logloss", "average_precision"]
    max_depth_min: int = 3
    max_depth_max: int = 10
    num_leaves_min: int = 15
    num_leaves_max: int = 100
    max_n_estimators: int = 2000
    early_stopping_rounds: int = 25
    max_folds: int = 5
    learning_rate_min: float = 0.005
    learning_rate_max: float = 0.02
    min_child_samples_min: int = 150
    min_child_samples_max: int = 400
    feature_fraction_min: float = 0.3
    feature_fraction_max: float = 1.0
    post_optimizer_trials: int = 3
    # Holdout for the post-optimizer (months reserved from Optuna). REQUIRED — the
    # manifest is the single source of truth; no silent default may stand in.
    post_optimizer_holdout_months: int

    @field_validator("post_optimizer_holdout_months")
    @classmethod
    def validate_holdout_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("post_optimizer_holdout_months must be strictly positive — the holdout must be respected.")
        return v

class TrainingWorkflowConfig(BaseModel):
    train_cutoff_date: str
    holdout_cutoff_date: Optional[str] = None
    target_columns: List[str]
    gcs_base_dir: str
    optuna: OptunaConfig = Field(default_factory=OptunaConfig)

    @field_validator("train_cutoff_date")
    @classmethod
    def validate_train_cutoff_defined(cls, v: str) -> str:
        # Sanity check: the training end date must be explicitly defined and parseable.
        # A null/empty cutoff would let training consume the entire dataset and leak
        # into the holdout — forbidden.
        if v is None or str(v).strip() == "":
            raise ValueError("train_cutoff_date (training end date) must be defined — it cannot be null/empty.")
        try:
            pd.Timestamp(v)
        except Exception as e:
            raise ValueError(f"train_cutoff_date is not a valid date: {v!r} ({e})")
        return v

    @model_validator(mode="after")
    def validate_no_holdout_leak(self) -> "TrainingWorkflowConfig":
        # The training period must not leak into the holdout: train_cutoff_date must be
        # strictly before the holdout boundary. In 2-way mode (holdout_cutoff_date is
        # null) the vault = everything >= train_cutoff, which is held out by construction.
        if self.holdout_cutoff_date is not None and str(self.holdout_cutoff_date).strip() != "":
            try:
                train_ts = pd.Timestamp(self.train_cutoff_date)
                holdout_ts = pd.Timestamp(self.holdout_cutoff_date)
            except Exception as e:
                raise ValueError(f"Invalid cutoff date(s): {e}")
            if train_ts >= holdout_ts:
                raise ValueError(
                    f"train_cutoff_date ({train_ts.date()}) must be strictly before "
                    f"holdout_cutoff_date ({holdout_ts.date()}) — training cannot leak into the holdout period."
                )
        return self

class ExecutionWorkflowConfig(BaseModel):
    # Absolute slippage applied per side in price units by backtest_engine (price +/-
    # slippage_per_side). NOT a tick multiplier. REQUIRED — no default, so a missing
    # value fails loudly instead of silently substituting a dangerous number.
    slippage_per_side: float
    # Raw UNADJUSTED price series used for execution/PnL. The strategy trains/signals on
    # ratio-adjusted data but MUST execute on raw prices (with rollover gaps). REQUIRED — no
    # default: a missing value previously defaulted to null, silently reverting PnL to adjusted
    # prices AND desyncing the optimizer from the ensemble backtest. Crash instead.
    execution_data_path: str
    strategy_config_path: str
    # Post-optimization mode. REQUIRED — the manifest is the single source of truth so
    # behavior is consistent and never depends on a CLI flag passed on the fly.
    #   "ensemble"   = joint long+short ensemble optimization
    #   "individual" = per-side Long/Short optimization
    opt_mode: Literal["individual", "ensemble"]

    @field_validator("execution_data_path")
    @classmethod
    def validate_exec_data_present(cls, v: str) -> str:
        if v is None or str(v).strip() == "":
            raise ValueError(
                "execution_data_path must be defined — it cannot be null/empty. PnL is executed "
                "on the raw unadjusted series; omitting it silently reverts to adjusted prices and "
                "desyncs the optimizer from the ensemble backtest."
            )
        return v

    @field_validator("slippage_per_side")
    @classmethod
    def validate_slippage_sane(cls, v: float) -> float:
        if v < 0:
            raise ValueError("slippage_per_side must be non-negative.")
        if v > 0.5:
            raise ValueError(
                f"slippage_per_side={v} is implausibly large for price-unit slippage "
                f"(>$0.50/side). This is the class of error that caused the -$2.5M PnL bug."
            )
        return v

class MasterConfig(BaseModel):
    symbol: str
    data_workflow: Optional[DataWorkflowConfig] = None
    training_workflow: Optional[TrainingWorkflowConfig] = None
    execution_workflow: Optional[ExecutionWorkflowConfig] = None

    @field_validator("symbol")
    @classmethod
    def check_symbol_exists(cls, v):
        from src.core.instrument_master import get_instrument
        try:
            get_instrument(v)
        except ValueError as e:
            raise ValueError(str(e))
        return v

class InfrastructureConfig(BaseModel):
    max_concurrent_vms: int = Field(0, description="Max concurrent VM count limit (0 means uncapped)")
    max_concurrent_vcpus: int = Field(..., description="Max concurrent vCPU count limit")
    vcpus_per_vm: int = Field(..., description="vCPU requirement per VM instance")
    machine_type: str = Field("c2-standard-16", description="GCP machine type")
    provisioning_model: Literal["STANDARD", "SPOT"] = Field("STANDARD", description="GCP VM provisioning strategy")
    timeout_minutes: int = Field(120, description="Max execution duration in minutes")

    @field_validator("max_concurrent_vcpus", "vcpus_per_vm", "timeout_minutes")
    @classmethod
    def validate_positive_ints(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Must be strictly positive.")
        return v

    @field_validator("max_concurrent_vms")
    @classmethod
    def validate_non_negative_vms(cls, v: int) -> int:
        if v < 0:
            raise ValueError("Must be non-negative (0 means uncapped).")
        return v

class ExperimentConfig(BaseModel):
    label: str
    gcs_prefix: str
    overrides: Dict[str, Any] = Field(default_factory=dict)

class BatchSweepConfig(BaseModel):
    comment: Optional[str] = Field(None, alias="_comment")
    infrastructure: InfrastructureConfig
    baseline: MasterConfig
    experiments: List[ExperimentConfig]

    model_config = {
        "populate_by_name": True
    }
