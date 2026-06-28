import pytest
from pydantic import ValidationError
from src.config.schemas import (
    MasterConfig, DataWorkflowConfig, FeatureConfig, 
    TargetConfig, SingleBarrierTarget, GridBarrierTarget
)
from src.core.instrument_master import get_instrument

def test_instrument_master_valid():
    cl = get_instrument("CL")
    assert cl.symbol == "CL"
    assert cl.tick_size == 0.01

def test_instrument_master_invalid():
    with pytest.raises(ValueError, match="Unknown instrument symbol"):
        get_instrument("INVALID_SYMBOL")

def test_valid_master_config():
    config = MasterConfig(
        symbol="CL",
        data_workflow=DataWorkflowConfig(
            dataset_version="HourSet_14A",
            targets=TargetConfig(
                definitions=[
                    SingleBarrierTarget(
                        type="triple_barrier",
                        tp_multiplier=2.0,
                        sl_multiplier=1.0,
                        horizon=3
                    ),
                    GridBarrierTarget(
                        type="triple_barrier_grid",
                        tp_multipliers=[2.0, 3.0, 4.0, 5.0],
                        sl_multiplier=1.0,
                        horizons=[6, 12, 24]
                    )
                ]
            )
        )
    )
    assert config.symbol == "CL"

def test_invalid_symbol_in_master_config():
    with pytest.raises(ValidationError) as excinfo:
        MasterConfig(symbol="INVALID_SYMBOL")
    assert "Unknown instrument symbol" in str(excinfo.value)

def test_negative_window_in_feature_config():
    with pytest.raises(ValidationError) as excinfo:
        FeatureConfig(windows=[24, 72, -10, 336])
    assert "strictly positive" in str(excinfo.value)

def test_invalid_target_definition_combination():
    # Attempting to pass a list to a single barrier target should fail
    with pytest.raises(ValidationError):
        TargetConfig(
            definitions=[
                {
                    "type": "triple_barrier",
                    "tp_multipliers": [2.0, 3.0], # Invalid for SingleBarrier
                    "sl_multiplier": 1.0,
                    "horizon": 3
                }
            ]
        )

def test_missing_required_fields_in_grid():
    with pytest.raises(ValidationError):
        TargetConfig(
            definitions=[
                {
                    "type": "triple_barrier_grid",
                    "tp_multipliers": [2.0, 3.0],
                    "sl_multiplier": 1.0
                    # Missing horizons
                }
            ]
        )
