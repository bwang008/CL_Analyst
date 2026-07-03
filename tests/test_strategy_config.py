"""
Tests for StrategyConfig DataClass.

Validates:
  1. New canonical key (trailing_sl_atr_offset) is parsed correctly
  2. Legacy key (trailing_activation_mult) is accepted as fallback
  3. Missing keys default to 1.0
  4. New key takes priority when both are present
  5. Per-side values are independent
  6. Global → per-side fallback cascade works
  7. Frozen immutability is enforced
  8. Production config roundtrip produces correct values
"""

import copy

import os
import sys
from dataclasses import FrozenInstanceError

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.live_execution.strategy_config import StrategyConfig, SideConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CONFIG_WITH_NEW_KEY = {
    "tp_atr_mult": 7.0,
    "sl_atr_mult": 3.25,
    "trailing_atr_mult": 3.25,
    "trailing_sl_atr_offset": 2.5,
    "atr_period": 18,
    "max_hold_bars": 192,
    "entry_threshold": 0.5,
    "long": {
        "tp_atr_mult": 7.0,
        "sl_atr_mult": 3.25,
        "trailing_atr_mult": 3.25,
        "trailing_sl_atr_offset": 3.0,
        "atr_period": 18,
        "tiers": [{"min_prob": 0.5, "lots": 1}],
    },
    "short": {
        "tp_atr_mult": 5.0,
        "sl_atr_mult": 1.75,
        "trailing_atr_mult": 4.25,
        "trailing_sl_atr_offset": 3.5,
        "atr_period": 26,
        "tiers": [{"min_prob": 0.53, "lots": 1}],
    },
    "models": {
        "long": {"threshold": 0.5},
        "short": {"threshold": 0.53},
    },
}

CONFIG_WITH_LEGACY_KEY = {
    "tp_atr_mult": 7.0,
    "sl_atr_mult": 3.25,
    "trailing_atr_mult": 3.25,
    "trailing_activation_mult": 2.5,
    "atr_period": 18,
    "max_hold_bars": 192,
    "entry_threshold": 0.5,
    "long": {
        "tp_atr_mult": 7.0,
        "sl_atr_mult": 3.25,
        "trailing_atr_mult": 3.25,
        "trailing_activation_mult": 3.0,
        "atr_period": 18,
        "tiers": [{"min_prob": 0.5, "lots": 1}],
    },
    "short": {
        "tp_atr_mult": 5.0,
        "sl_atr_mult": 1.75,
        "trailing_atr_mult": 4.25,
        "trailing_activation_mult": 3.5,
        "atr_period": 26,
        "tiers": [{"min_prob": 0.53, "lots": 1}],
    },
    "models": {
        "long": {"threshold": 0.5},
        "short": {"threshold": 0.53},
    },
}

MINIMAL_CONFIG = {
    "models": {
        "long": {"threshold": 0.6},
        "short": {"threshold": 0.6},
    },
    "long": {"tiers": [{"min_prob": 0.6, "lots": 1}]},
    "short": {"tiers": [{"min_prob": 0.6, "lots": 1}]},
}


# ---------------------------------------------------------------------------
# Tests: Key Resolution
# ---------------------------------------------------------------------------


class TestKeyResolution:
    """Verify the dual-key resolution logic for trailing_sl_atr_offset."""

    def test_new_key_parsed(self):
        """Config with trailing_sl_atr_offset should be read directly."""
        sc = StrategyConfig.from_dict(copy.deepcopy(CONFIG_WITH_NEW_KEY))
        assert sc.trailing_sl_atr_offset == 2.5
        assert sc.long.trailing_sl_atr_offset == 3.0
        assert sc.short.trailing_sl_atr_offset == 3.5

    def test_legacy_key_parsed(self):
        """Config with trailing_activation_mult should be accepted as fallback."""
        sc = StrategyConfig.from_dict(copy.deepcopy(CONFIG_WITH_LEGACY_KEY))
        assert sc.trailing_sl_atr_offset == 2.5
        assert sc.long.trailing_sl_atr_offset == 3.0
        assert sc.short.trailing_sl_atr_offset == 3.5

    def test_no_key_defaults_to_1_0(self):
        """Config with neither key should default to 1.0."""
        sc = StrategyConfig.from_dict(copy.deepcopy(MINIMAL_CONFIG))
        assert sc.trailing_sl_atr_offset == 1.0
        assert sc.long.trailing_sl_atr_offset == 1.0
        assert sc.short.trailing_sl_atr_offset == 1.0

    def test_new_key_takes_priority(self):
        """If both keys exist, trailing_sl_atr_offset takes priority."""
        cfg = copy.deepcopy(CONFIG_WITH_LEGACY_KEY)
        # Add the new key alongside the legacy key
        cfg["trailing_sl_atr_offset"] = 99.0
        cfg["long"]["trailing_sl_atr_offset"] = 88.0
        cfg["short"]["trailing_sl_atr_offset"] = 77.0

        sc = StrategyConfig.from_dict(cfg)
        assert sc.trailing_sl_atr_offset == 99.0
        assert sc.long.trailing_sl_atr_offset == 88.0
        assert sc.short.trailing_sl_atr_offset == 77.0


# ---------------------------------------------------------------------------
# Tests: Per-side Independence
# ---------------------------------------------------------------------------


class TestPerSideIndependence:
    """Verify per-side values are independent from each other."""

    def test_asymmetric_offsets(self):
        """Long and short trailing_sl_atr_offset can differ."""
        sc = StrategyConfig.from_dict(copy.deepcopy(CONFIG_WITH_NEW_KEY))
        assert sc.long.trailing_sl_atr_offset != sc.short.trailing_sl_atr_offset
        assert sc.long.trailing_sl_atr_offset == 3.0
        assert sc.short.trailing_sl_atr_offset == 3.5

    def test_asymmetric_atr_periods(self):
        """Long and short atr_period can differ."""
        sc = StrategyConfig.from_dict(copy.deepcopy(CONFIG_WITH_NEW_KEY))
        assert sc.long.atr_period == 18
        assert sc.short.atr_period == 26


# ---------------------------------------------------------------------------
# Tests: Fallback Cascade
# ---------------------------------------------------------------------------


class TestFallbackCascade:
    """Verify global → per-side fallback cascade."""

    def test_side_missing_falls_back_to_global(self):
        """If per-side block lacks trailing_sl_atr_offset, use global."""
        cfg = copy.deepcopy(CONFIG_WITH_NEW_KEY)
        # Remove per-side keys
        del cfg["long"]["trailing_sl_atr_offset"]
        del cfg["short"]["trailing_sl_atr_offset"]

        sc = StrategyConfig.from_dict(cfg)
        # Should fall back to global value (2.5)
        assert sc.long.trailing_sl_atr_offset == 2.5
        assert sc.short.trailing_sl_atr_offset == 2.5

    def test_global_missing_falls_back_to_default(self):
        """If global also lacks the key, fall back to 1.0."""
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        # Explicitly verify nothing is set
        assert "trailing_sl_atr_offset" not in cfg
        assert "trailing_activation_mult" not in cfg

        sc = StrategyConfig.from_dict(cfg)
        assert sc.trailing_sl_atr_offset == 1.0
        assert sc.long.trailing_sl_atr_offset == 1.0
        assert sc.short.trailing_sl_atr_offset == 1.0

    def test_side_legacy_key_overrides_global_new_key(self):
        """Side-level legacy key should be used over global value."""
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        cfg["trailing_sl_atr_offset"] = 1.0  # global new key
        cfg["long"]["trailing_activation_mult"] = 2.0  # side legacy key

        sc = StrategyConfig.from_dict(cfg)
        assert sc.trailing_sl_atr_offset == 1.0  # global
        assert sc.long.trailing_sl_atr_offset == 2.0  # side override via legacy key
        assert sc.short.trailing_sl_atr_offset == 1.0  # falls back to global


# ---------------------------------------------------------------------------
# Tests: Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    """Verify that StrategyConfig is frozen (immutable after creation)."""

    def test_strategy_config_frozen(self):
        """Attempting to mutate a StrategyConfig raises FrozenInstanceError."""
        sc = StrategyConfig.from_dict(copy.deepcopy(MINIMAL_CONFIG))
        with pytest.raises(FrozenInstanceError):
            sc.trailing_sl_atr_offset = 999.0

    def test_side_config_frozen(self):
        """Attempting to mutate a SideConfig raises FrozenInstanceError."""
        sc = StrategyConfig.from_dict(copy.deepcopy(MINIMAL_CONFIG))
        with pytest.raises(FrozenInstanceError):
            sc.long.trailing_sl_atr_offset = 999.0



# ---------------------------------------------------------------------------
# Tests: Other Defaults
# ---------------------------------------------------------------------------


class TestOtherDefaults:
    """Verify non-trailing defaults are correctly resolved."""

    def test_default_trailing_atr_mult_is_100(self):
        """When trailing_atr_mult is missing, default to 100.0 (disabled)."""
        sc = StrategyConfig.from_dict(copy.deepcopy(MINIMAL_CONFIG))
        assert sc.trailing_atr_mult == 100.0

    def test_default_atr_period_is_14(self):
        """When atr_period is missing, default to 14."""
        sc = StrategyConfig.from_dict(copy.deepcopy(MINIMAL_CONFIG))
        assert sc.atr_period == 14
        assert sc.long.atr_period == 14
        assert sc.short.atr_period == 14

    def test_default_max_hold_bars_is_288(self):
        """When max_hold_bars is missing, default to 288."""
        sc = StrategyConfig.from_dict(copy.deepcopy(MINIMAL_CONFIG))
        assert sc.max_hold_bars == 288

    def test_raw_dict_preserved(self):
        """The raw config dict should be accessible for downstream consumers."""
        cfg = copy.deepcopy(CONFIG_WITH_NEW_KEY)
        sc = StrategyConfig.from_dict(cfg)
        assert sc.raw is cfg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
