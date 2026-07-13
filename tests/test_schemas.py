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


# ---------------------------------------------------------------------------
# Curve calendar-spread config hygiene (HourSet_03B) — no half-states
# ---------------------------------------------------------------------------

def test_curve_defaults_off_and_valid():
    """Defaults keep every existing DataMap parsing identically (all flags off)."""
    cfg = FeatureConfig()
    assert cfg.include_curve_spread is False
    assert cfg.curve_front_leg_csv is None
    assert cfg.curve_second_leg_csv is None
    assert cfg.include_month_encoding is False
    assert cfg.curve_seasonal_bucket == "week"
    assert cfg.curve_seasonal_min_prior_years == 2
    assert cfg.curve_seasonal_pctl is False


def test_curve_flag_on_requires_both_leg_paths():
    with pytest.raises(ValidationError, match="curve_front_leg_csv"):
        FeatureConfig(include_curve_spread=True)
    with pytest.raises(ValidationError, match="curve_second_leg_csv"):
        FeatureConfig(include_curve_spread=True, curve_front_leg_csv="C:/legs/c0.csv")
    with pytest.raises(ValidationError, match="curve_front_leg_csv"):
        FeatureConfig(include_curve_spread=True, curve_second_leg_csv="C:/legs/c1.csv")


def test_curve_leg_paths_without_flag_rejected():
    with pytest.raises(ValidationError, match="half-configured"):
        FeatureConfig(curve_front_leg_csv="C:/legs/c0.csv")
    with pytest.raises(ValidationError, match="half-configured"):
        FeatureConfig(curve_second_leg_csv="C:/legs/c1.csv")


def test_curve_seasonal_settings_without_flag_rejected():
    with pytest.raises(ValidationError, match="half-configured"):
        FeatureConfig(curve_seasonal_bucket="month")
    with pytest.raises(ValidationError, match="half-configured"):
        FeatureConfig(curve_seasonal_min_prior_years=3)
    with pytest.raises(ValidationError, match="half-configured"):
        FeatureConfig(curve_seasonal_pctl=True)


def test_curve_seasonal_min_prior_years_floor():
    with pytest.raises(ValidationError, match=">= 2"):
        FeatureConfig(
            include_curve_spread=True,
            curve_front_leg_csv="C:/legs/c0.csv",
            curve_second_leg_csv="C:/legs/c1.csv",
            curve_seasonal_min_prior_years=1,
        )


def test_curve_fully_configured_accepted():
    cfg = FeatureConfig(
        include_curve_spread=True,
        curve_front_leg_csv="C:/legs/c0.csv",
        curve_second_leg_csv="C:/legs/c1.csv",
        curve_seasonal_bucket="week",
        curve_seasonal_min_prior_years=2,
    )
    assert cfg.include_curve_spread is True


def test_month_encoding_independent_of_curve():
    # include_month_encoding is a time feature — legal without any curve config
    cfg = FeatureConfig(include_month_encoding=True)
    assert cfg.include_month_encoding is True
    assert cfg.include_curve_spread is False
