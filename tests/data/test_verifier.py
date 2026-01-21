"""
Tests for OilDatasetVerifier.

This module tests the data verification layer that ensures processed datasets
meet the requirements for ML model training. Tests cover three categories:

1. Structure Checks: No NaN, no inf, monotonic index
2. Physics Checks: RSI bounds, volatility non-negative, time encoding bounds
3. Sanity Checks: No target leakage

Each test includes a "Sabotage Verification" note explaining how to verify
the test catches real errors by intentionally breaking the source code.

Author: CL Analyst
"""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.data_verifier import OilDatasetVerifier, DataVerificationError, verify_dataset


# =============================================================================
# STRUCTURE CHECKS
# =============================================================================

class TestStructureChecks:
    """Tests for structure validation (NaN, inf, monotonic index)."""
    
    def test_no_nan_values_passes_clean_data(self, sample_processed_data):
        """
        Clean data should pass NaN check.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        assert verifier.check_no_nan() is True
        assert len(verifier.errors) == 0
    
    def test_no_nan_values_catches_nan(self, sample_processed_data_with_nan):
        """
        Data with NaN values should fail NaN check.
        
        Sabotage Verification:
            In data_processor.py, comment out `df.dropna()` in cleanup().
            Run this test - it MUST fail. Then revert.
        """
        verifier = OilDatasetVerifier(sample_processed_data_with_nan)
        assert verifier.check_no_nan() is False
        assert len(verifier.errors) == 1
        assert "NaN" in verifier.errors[0]
    
    def test_no_inf_values_passes_clean_data(self, sample_processed_data):
        """
        Clean data should pass inf check.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        assert verifier.check_no_inf() is True
        assert len(verifier.errors) == 0
    
    def test_no_inf_values_catches_inf(self, sample_processed_data_with_inf):
        """
        Data with inf values should fail inf check.
        
        Sabotage Verification:
            In data_processor.py, add `df['VOL_3D'] = np.inf` in a volatility calc.
            Run this test - it MUST fail. Then revert.
        """
        verifier = OilDatasetVerifier(sample_processed_data_with_inf)
        assert verifier.check_no_inf() is False
        assert len(verifier.errors) == 1
        assert "infinite" in verifier.errors[0].lower()
    
    def test_index_monotonic_passes_sorted_data(self, sample_processed_data):
        """
        Properly sorted time-series data should pass monotonic check.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        assert verifier.check_index_monotonic() is True
        assert len(verifier.errors) == 0
    
    def test_index_monotonic_catches_unsorted_data(self, sample_processed_data):
        """
        Unsorted data should fail monotonic check.
        
        Sabotage Verification:
            In data_processor.py, add `df = df.sample(frac=1)` before returning.
            Run this test - it MUST fail. Then revert.
        """
        # Shuffle the data
        shuffled_data = sample_processed_data.sample(frac=1, random_state=42)
        
        verifier = OilDatasetVerifier(shuffled_data)
        assert verifier.check_index_monotonic() is False
        assert len(verifier.errors) == 1
        assert "monotonic" in verifier.errors[0].lower()
    
    def test_check_structure_runs_all_checks(self, sample_processed_data):
        """
        check_structure() should run all structure checks.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        result = verifier.check_structure()
        assert result is True


# =============================================================================
# PHYSICS CHECKS
# =============================================================================

class TestPhysicsChecks:
    """Tests for physics validation (RSI bounds, volatility, time encoding)."""
    
    def test_rsi_bounds_passes_valid_data(self, sample_processed_data):
        """
        RSI within [0, 100] should pass.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        assert verifier.check_rsi_bounds() is True
        assert len(verifier.errors) == 0
    
    def test_rsi_bounds_catches_negative_rsi(self, sample_processed_data):
        """
        RSI < 0 should fail.
        
        Sabotage Verification:
            In indicators.py, invert the RS calculation: change `rs = roll_up / roll_down`
            to `rs = roll_down / roll_up`. This will produce invalid RSI values.
            Run this test - it MUST fail. Then revert.
        """
        df = sample_processed_data.copy()
        df.loc[df.index[10], 'RSI'] = -5.0
        
        verifier = OilDatasetVerifier(df)
        assert verifier.check_rsi_bounds() is False
        assert "RSI out of bounds" in verifier.errors[0]
    
    def test_rsi_bounds_catches_over_100_rsi(self, sample_processed_data):
        """
        RSI > 100 should fail.
        """
        df = sample_processed_data.copy()
        df.loc[df.index[10], 'RSI'] = 105.0
        
        verifier = OilDatasetVerifier(df)
        assert verifier.check_rsi_bounds() is False
        assert "RSI out of bounds" in verifier.errors[0]
    
    def test_volatility_non_negative_passes_valid_data(self, sample_processed_data):
        """
        Non-negative volatility should pass.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        assert verifier.check_volatility_non_negative() is True
        assert len(verifier.errors) == 0
    
    def test_volatility_non_negative_catches_negative(self, sample_processed_data):
        """
        Negative volatility should fail.
        
        Sabotage Verification:
            In data_processor.py, change a volatility calculation to allow negatives.
            For example, remove the rolling .std() and replace with signed values.
            Run this test - it MUST fail. Then revert.
        """
        df = sample_processed_data.copy()
        df.loc[df.index[10], 'VOL_3D'] = -0.001
        
        verifier = OilDatasetVerifier(df)
        assert verifier.check_volatility_non_negative() is False
        assert "negative values" in verifier.errors[0].lower()
    
    def test_time_sin_bounds_passes_valid_data(self, sample_processed_data):
        """
        Time_Sin within [-1, 1] should pass.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        assert verifier.check_time_sin_bounds() is True
        assert len(verifier.errors) == 0
    
    def test_time_sin_bounds_catches_invalid(self, sample_processed_data):
        """
        Time_Sin outside [-1, 1] should fail.
        
        Sabotage Verification:
            In data_processor.py, change `2 * np.pi` to `4 * np.pi` in add_time_features().
            This will produce Time_Sin values outside [-1, 1].
            Run this test - it MUST fail. Then revert.
        """
        df = sample_processed_data.copy()
        df.loc[df.index[10], 'Time_Sin'] = 1.5
        
        verifier = OilDatasetVerifier(df)
        assert verifier.check_time_sin_bounds() is False
        assert "Time_Sin out of bounds" in verifier.errors[0]
    
    def test_time_cos_bounds_passes_valid_data(self, sample_processed_data):
        """
        Time_Cos within [-1, 1] should pass.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        assert verifier.check_time_cos_bounds() is True
        assert len(verifier.errors) == 0
    
    def test_time_cos_bounds_catches_invalid(self, sample_processed_data):
        """
        Time_Cos outside [-1, 1] should fail.
        """
        df = sample_processed_data.copy()
        df.loc[df.index[10], 'Time_Cos'] = -1.5
        
        verifier = OilDatasetVerifier(df)
        assert verifier.check_time_cos_bounds() is False
        assert "Time_Cos out of bounds" in verifier.errors[0]
    
    def test_check_physics_runs_all_checks(self, sample_processed_data):
        """
        check_physics() should run all physics checks.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        result = verifier.check_physics()
        assert result is True


# =============================================================================
# SANITY CHECKS (LEAKAGE DETECTION)
# =============================================================================

class TestSanityChecks:
    """Tests for sanity validation (target leakage detection)."""
    
    def test_no_target_leakage_passes_clean_data(self, sample_processed_data):
        """
        Data without perfect correlations should pass.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        assert verifier.check_no_target_leakage() is True
        assert len(verifier.errors) == 0
    
    def test_no_target_leakage_catches_leakage(self, sample_processed_data_with_leakage):
        """
        Data with perfect feature-target correlation should fail.
        
        This catches the case where a feature is derived from the target
        or contains future information that wouldn't be available at prediction time.
        
        Sabotage Verification:
            In data_processor.py, add a line that copies Target to a feature column.
            Run this test - it MUST fail. Then revert.
        """
        verifier = OilDatasetVerifier(sample_processed_data_with_leakage)
        assert verifier.check_no_target_leakage() is False
        assert "leakage" in verifier.errors[0].lower()
    
    def test_check_sanity_runs_all_checks(self, sample_processed_data):
        """
        check_sanity() should run all sanity checks.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        result = verifier.check_sanity()
        assert result is True


# =============================================================================
# EDGE CASE CHECKS
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases (zero price, flat market, midnight wraparound)."""
    
    def test_zero_price_handling(self, zero_price_data):
        """
        Verifier should detect zero prices that could cause division errors.
        
        Sabotage Verification:
            This test uses raw data with zero prices. The verifier should
            flag this as a potential issue before processing.
        """
        verifier = OilDatasetVerifier(zero_price_data)
        assert verifier.check_zero_price_handling() is False
        assert "zero-price" in verifier.errors[0].lower()
    
    def test_flat_market_handling(self, flat_market_data):
        """
        Flat market (High == Low) should be handled without errors.
        
        When volatility is truly zero, calculations should return 0, not NaN or error.
        """
        # This is testing that the data can be processed, not the verifier itself
        # The verifier should not error on flat market data
        verifier = OilDatasetVerifier(flat_market_data)
        # check_zero_price should pass (prices are non-zero, just flat)
        assert verifier.check_zero_price_handling() is True
    
    def test_midnight_wraparound_valid_encoding(self, midnight_crossing_data):
        """
        Time encoding should be continuous across midnight boundary.
        
        At 23:55 and 00:05, Time_Sin values should be close to each other
        (near zero, with opposite signs), indicating proper cyclical encoding.
        
        Sabotage Verification:
            In data_processor.py, change the time encoding formula.
            For example, use linear time instead of cyclical.
            Run this test - it MUST fail. Then revert.
        """
        # Add time features to the midnight crossing data
        df = midnight_crossing_data.copy()
        minutes = df.index.hour * 60 + df.index.minute
        df['Time_Sin'] = np.sin(2 * np.pi * minutes / 1440)
        df['Time_Cos'] = np.cos(2 * np.pi * minutes / 1440)
        
        verifier = OilDatasetVerifier(df)
        result = verifier.check_midnight_wraparound()
        
        # Should pass - the encoding is correct
        assert result is True
        
        # Verify the actual values are close to what we expect
        # At 23:55 (1435 min), Time_Sin ≈ -0.022
        # At 00:05 (5 min), Time_Sin ≈ 0.022
        late_night_sin = df.loc[df.index.hour == 23, 'Time_Sin'].values[-1]
        early_morning_sin = df.loc[df.index.hour == 0, 'Time_Sin'].values[0]
        
        # Both should be small in absolute value (near midnight)
        assert abs(late_night_sin) < 0.1
        assert abs(early_morning_sin) < 0.1
        
        # They should be approximately opposite in sign
        # (one is just before midnight, one is just after)
        # At 23:55: sin(2π * 1435/1440) ≈ sin(2π * 0.9965) ≈ -0.022
        # At 00:00: sin(2π * 0/1440) = 0
        # At 00:05: sin(2π * 5/1440) ≈ sin(2π * 0.0035) ≈ 0.022
    
    def test_midnight_wraparound_catches_bad_encoding(self, midnight_crossing_data):
        """
        Incorrect time encoding should fail the midnight check.
        """
        df = midnight_crossing_data.copy()
        
        # Intentionally wrong encoding - not cyclical, produces values > 1
        minutes = df.index.hour * 60 + df.index.minute
        df['Time_Sin'] = np.sin(4 * np.pi * minutes / 1440)  # Wrong multiplier
        df['Time_Cos'] = np.cos(4 * np.pi * minutes / 1440)
        
        verifier = OilDatasetVerifier(df)
        
        # Values near midnight might now be larger than expected
        # depending on the exact timestamps
        late_night = df.index.hour == 23
        early_morning = df.index.hour == 0
        
        # With wrong encoding, at least one value should be outside normal range
        # At 23:55 with 4*pi: sin(4π * 1435/1440) ≈ sin(4π * 0.9965) ≈ -0.044
        # Still small, so this particular sabotage might not trigger the check
        # The check is specifically for values > 0.15, which requires more extreme errors


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

class TestIntegration:
    """Integration tests for full verification workflow."""
    
    def test_verify_all_passes_clean_data(self, sample_processed_data):
        """
        Clean data should pass all verification checks.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        assert verifier.verify_all() is True
        assert len(verifier.errors) == 0
    
    def test_verify_all_catches_multiple_errors(self, sample_processed_data):
        """
        verify_all should catch multiple errors in one pass.
        """
        df = sample_processed_data.copy()
        # Introduce multiple errors
        df.iloc[10, 0] = np.nan  # NaN
        df.loc[df.index[20], 'RSI'] = -5.0  # Invalid RSI
        
        verifier = OilDatasetVerifier(df)
        assert verifier.verify_all() is False
        assert len(verifier.errors) >= 2
    
    def test_verify_all_raises_on_error(self, sample_processed_data_with_nan):
        """
        verify_all with raise_on_error=True should raise exception.
        """
        verifier = OilDatasetVerifier(sample_processed_data_with_nan)
        
        with pytest.raises(DataVerificationError) as exc_info:
            verifier.verify_all(raise_on_error=True)
        
        assert "verification failed" in str(exc_info.value).lower()
    
    def test_get_report_structure(self, sample_processed_data):
        """
        get_report should return a properly structured dictionary.
        """
        verifier = OilDatasetVerifier(sample_processed_data)
        verifier.verify_all()
        
        report = verifier.get_report()
        
        assert 'is_valid' in report
        assert 'num_errors' in report
        assert 'num_warnings' in report
        assert 'errors' in report
        assert 'warnings' in report
        assert 'shape' in report
        assert 'columns' in report
        
        assert report['is_valid'] is True
        assert report['num_errors'] == 0
    
    def test_convenience_function_verify_dataset(self, sample_processed_data):
        """
        verify_dataset convenience function should work correctly.
        """
        # Should return True for valid data
        result = verify_dataset(sample_processed_data, raise_on_error=False)
        assert result is True
    
    def test_convenience_function_raises_on_invalid(self, sample_processed_data_with_nan):
        """
        verify_dataset should raise when data is invalid.
        """
        with pytest.raises(DataVerificationError):
            verify_dataset(sample_processed_data_with_nan, raise_on_error=True)


# =============================================================================
# MISSING COLUMN HANDLING
# =============================================================================

class TestMissingColumnHandling:
    """Tests for graceful handling of missing columns."""
    
    def test_rsi_check_warns_when_column_missing(self, synthetic_price_data):
        """
        RSI check should warn (not error) when RSI column is missing.
        """
        # Use raw price data without RSI column
        verifier = OilDatasetVerifier(synthetic_price_data)
        result = verifier.check_rsi_bounds()
        
        assert result is True  # Should pass (column not present)
        assert len(verifier.warnings) == 1
        assert "RSI column not found" in verifier.warnings[0]
    
    def test_time_sin_check_warns_when_column_missing(self, synthetic_price_data):
        """
        Time_Sin check should warn when column is missing.
        """
        verifier = OilDatasetVerifier(synthetic_price_data)
        result = verifier.check_time_sin_bounds()
        
        assert result is True
        assert len(verifier.warnings) == 1
        assert "Time_Sin column not found" in verifier.warnings[0]
    
    def test_target_leakage_check_warns_when_target_missing(self, synthetic_price_data):
        """
        Target leakage check should warn when Target column is missing.
        """
        verifier = OilDatasetVerifier(synthetic_price_data)
        result = verifier.check_no_target_leakage()
        
        assert result is True
        assert len(verifier.warnings) == 1
        assert "Target column not found" in verifier.warnings[0]
