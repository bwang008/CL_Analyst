"""
Tests for indicatorBuilder.py (pandas_ta based feature generation).

This module tests the technical indicator generation logic to ensure:
1. RSI behaves correctly at extremes (trending markets)
2. Volatility calculations handle edge cases (flat markets)
3. Rolling windows produce correct NaN patterns

Each test includes "Sabotage Verification" instructions for mutation testing.

Author: CL Analyst
"""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.indicatorBuilder import generate_features


# =============================================================================
# RSI TESTS
# =============================================================================

class TestRSI:
    """Tests for RSI (Relative Strength Index) calculation."""
    
    def test_rsi_trending_up_approaches_100(self, trending_up_data):
        """
        Strictly increasing prices should yield RSI approaching 100.

        When all price changes are positive (gains), RSI should be very high.
        The formula RSI = 100 - (100 / (1 + RS)) approaches 100 as RS -> infinity.

        Sabotage Verification:
            In indicatorBuilder / pandas_ta RSI, invert the RS calculation by
            swapping gain/loss. Run this test - it MUST fail (RSI near 0). Then revert.
        """
        df_features = generate_features(trending_up_data)
        assert df_features['RSI'].iloc[-1] > 99.0, \
            f"RSI should be > 99 for trending up market, got {df_features['RSI'].iloc[-1]}"
    
    def test_rsi_trending_down_approaches_0(self, trending_down_data):
        """
        Strong downtrend should yield RSI very low (< 10).

        Sabotage Verification: Invert RS calculation; RSI will be near 100 instead.
        """
        df_features = generate_features(trending_down_data)
        assert df_features['RSI'].iloc[-1] < 10.0, \
            f"RSI should be < 10 for trending down market, got {df_features['RSI'].iloc[-1]}"

    def test_rsi_bounds_always_valid(self, sufficient_history_data):
        """
        RSI must always be within [0, 100] regardless of input.
        """
        result = generate_features(sufficient_history_data)
        rsi_values = result['RSI'].dropna()
        assert rsi_values.min() >= 0, f"RSI below 0: {rsi_values.min()}"
        assert rsi_values.max() <= 100, f"RSI above 100: {rsi_values.max()}"

    def test_rsi_neutral_market_around_50(self, sufficient_history_data):
        """
        In a market with roughly equal up/down moves, RSI should hover around 50.
        """
        result = generate_features(sufficient_history_data)
        mean_rsi = result['RSI'].dropna().mean()
        assert 30 < mean_rsi < 70, f"Mean RSI {mean_rsi} outside expected neutral range"

    def test_rsi_warmup_logic(self, sufficient_history_data):
        """
        After generate_features (with dropna), RSI should have no NaNs.
        """
        res = generate_features(sufficient_history_data)
        assert not res['RSI'].isna().any(), "RSI should be clean after dropna"


# =============================================================================
# VOLATILITY TESTS
# =============================================================================

class TestVolatility:
    """Tests for volatility calculations."""
    
    def test_volatility_flat_market_is_zero(self, nearly_flat_market_data):
        """
        When price has negligible variance, VOL_30D should be effectively 0.

        (Perfectly flat prices make RSI all-NaN, so generate_features would
        drop all rows. We use nearly-flat data so the pipeline runs.)

        Sabotage Verification: Change volatility formula (e.g. add offset). Test MUST fail.
        """
        df_features = generate_features(nearly_flat_market_data)
        vol = df_features['VOL_30D'].iloc[-1]
        assert vol < 1e-5, f"VOL_30D should be ~0 for nearly flat market, got {vol}"

    def test_volatility_non_negative(self, sufficient_history_data):
        """
        Volatility (std dev) cannot be negative.
        """
        result = generate_features(sufficient_history_data)
        for col in ['VOL_24H', 'VOL_5D', 'VOL_30D']:
            if col in result.columns:
                vol_values = result[col].dropna()
                assert vol_values.min() >= 0, f"{col} has negative values: {vol_values.min()}"

    def test_volatility_increases_with_variance(self, low_vol_history, high_vol_history):
        """
        Higher price variance should result in higher VOL_24H.
        """
        low_vol_result = generate_features(low_vol_history)
        high_vol_result = generate_features(high_vol_history)
        low_vol_mean = low_vol_result['VOL_24H'].dropna().mean()
        high_vol_mean = high_vol_result['VOL_24H'].dropna().mean()
        assert high_vol_mean > low_vol_mean, \
            f"High vol ({high_vol_mean}) should be > low vol ({low_vol_mean})"


# =============================================================================
# ROLLING WINDOW TESTS
# =============================================================================

class TestRollingWindows:
    """Tests for rolling window behavior (NaN patterns, window sizes)."""
    
    def test_rolling_indicators_have_initial_nan(self, sufficient_history_data):
        """
        Output should start significantly later than input (no look-ahead).

        The first 8640 rows are consumed by VOL_30D warm-up. The output's first
        row should be at least 29 days after the input start.
        """
        result = generate_features(sufficient_history_data)
        input_start = sufficient_history_data.index[0]
        output_start = result.index[0]
        assert output_start > input_start + pd.Timedelta(days=29), \
            "Output should start after 30-day warm-up (no look-ahead)"
    
    def test_sma_window_size_correct(self, synthetic_price_data):
        """
        SMA with window N should have N-1 initial NaN values.
        """
        df = synthetic_price_data.copy()
        
        import pandas_ta as ta
        df['SMA_20'] = df.ta.sma(length=20)
        
        # First 19 values should be NaN
        assert df['SMA_20'].iloc[:19].isna().all(), \
            "First 19 SMA_20 values should be NaN for window=20"
        
        # Value at index 19 should be valid
        assert pd.notna(df['SMA_20'].iloc[19]), \
            "SMA_20 at index 19 should be valid"
    
    def test_generate_features_drops_nan(self, sufficient_history_data):
        """
        generate_features() should drop rows with NaN values.
        """
        result = generate_features(sufficient_history_data)
        assert result.isna().sum().sum() == 0, "No NaN values should remain"

    def test_row_count_reduced_by_largest_window(self, sufficient_history_data):
        """
        Row count should be reduced by the largest rolling window (VOL_30D=8640).
        """
        original_rows = len(sufficient_history_data)
        result = generate_features(sufficient_history_data)
        result_rows = len(result)
        assert result_rows < original_rows, "Result should have fewer rows than input"
        assert result_rows > 0, "Result should be non-empty after warm-up"


# =============================================================================
# FEATURE OUTPUT TESTS
# =============================================================================

class TestFeatureOutput:
    """Tests for the structure of generated features."""
    
    def test_output_contains_expected_columns(self, sufficient_history_data):
        """
        generate_features should produce expected indicator columns.
        """
        result = generate_features(sufficient_history_data)
        for col in ['RSI', 'SMA_20', 'EMA_20', 'VOL_24H', 'VOL_5D', 'VOL_30D']:
            assert col in result.columns, f"Missing expected column: {col}"

    def test_output_preserves_ohlcv(self, sufficient_history_data):
        """
        generate_features should preserve original OHLCV columns.
        """
        result = generate_features(sufficient_history_data)
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            assert col in result.columns, f"Missing OHLCV column: {col}"

    def test_output_index_preserved(self, sufficient_history_data):
        """
        DateTime index should be preserved after feature generation.
        """
        result = generate_features(sufficient_history_data)
        assert isinstance(result.index, pd.DatetimeIndex), "Index should be DatetimeIndex"
        assert result.index.isin(sufficient_history_data.index).all(), \
            "All result indices should be in original data"

    def test_ema_follows_price(self, sufficient_history_data):
        """
        EMA should follow the general price trend.
        """
        result = generate_features(sufficient_history_data)
        ema_close_corr = result['EMA_20'].corr(result['Close'])
        assert ema_close_corr > 0.9, f"EMA should be highly correlated with Close, got {ema_close_corr}"


# =============================================================================
# EDGE CASE HANDLING
# =============================================================================

class TestEdgeCaseHandling:
    """Tests for edge case handling in feature generation."""
    
    def test_handles_minimum_data(self):
        """
        Should handle minimum viable data without crashing.
        """
        # Create minimal dataset (just enough for smallest window)
        n_bars = 50
        start_date = datetime(2024, 1, 1, 0, 0)
        date_index = pd.date_range(start=start_date, periods=n_bars, freq='5min')
        
        df = pd.DataFrame({
            'Open': np.full(n_bars, 75.0),
            'High': np.full(n_bars, 75.1),
            'Low': np.full(n_bars, 74.9),
            'Close': np.full(n_bars, 75.0),
            'Volume': np.full(n_bars, 1000)
        }, index=date_index)
        
        # Should not raise an error
        result = generate_features(df)
        
        # May return empty or very few rows, but should not crash
        assert isinstance(result, pd.DataFrame)
    
    def test_handles_large_price_spike(self, sufficient_history_data):
        """
        Should handle large price spikes without producing invalid values.
        """
        df = sufficient_history_data.copy()
        spike_idx = len(df) // 2
        df.iloc[spike_idx, df.columns.get_loc('Close')] *= 2
        df.iloc[spike_idx, df.columns.get_loc('High')] *= 2

        result = generate_features(df)
        assert result['RSI'].min() >= 0 and result['RSI'].max() <= 100
        for col in result.select_dtypes(include=[np.number]).columns:
            assert not np.isinf(result[col]).any(), f"Inf values in {col}"
    
    def test_handles_duplicate_timestamps(self):
        """
        Should handle (or appropriately error on) duplicate timestamps.
        """
        n_bars = 100
        start_date = datetime(2024, 1, 1, 0, 0)
        date_index = pd.date_range(start=start_date, periods=n_bars, freq='5min')
        
        # Create duplicate by repeating an index
        date_index_with_dup = date_index.tolist()
        date_index_with_dup[50] = date_index_with_dup[49]  # Duplicate
        
        df = pd.DataFrame({
            'Open': np.full(n_bars, 75.0),
            'High': np.full(n_bars, 75.1),
            'Low': np.full(n_bars, 74.9),
            'Close': np.linspace(74, 76, n_bars),
            'Volume': np.full(n_bars, 1000)
        }, index=pd.DatetimeIndex(date_index_with_dup))
        
        # Should either work or raise a clear error, not produce silent garbage
        try:
            result = generate_features(df)
            # If it works, check output is valid
            assert isinstance(result, pd.DataFrame)
        except (ValueError, KeyError) as e:
            # Acceptable to raise an error for invalid input
            pass
