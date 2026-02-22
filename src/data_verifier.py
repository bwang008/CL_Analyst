"""
Data Verification Module for CL Futures ML Pipeline.

This module implements the OilDatasetVerifier class which performs three levels
of data validation:

1. Structure Checks: No NaN, no inf, monotonic index
2. Physics Checks: RSI bounds, volatility non-negative, cyclical time bounds
3. Sanity Checks: No target leakage (perfect correlations)

The verifier follows the "Data Contracts" philosophy - we don't just test code,
we test data validity to catch silent failures that could corrupt ML models.

Author: CL Analyst
"""

import numpy as np
import pandas as pd
from typing import List, Tuple, Optional, Dict, Any


class DataVerificationError(Exception):
    """Exception raised when data verification fails."""
    pass


class OilDatasetVerifier:
    """
    Verifier for processed oil futures datasets.
    
    This class provides comprehensive validation of processed DataFrames
    to ensure they meet the requirements for ML model training.
    
    Attributes:
        df (pd.DataFrame): The DataFrame to verify
        errors (List[str]): List of verification errors found
        warnings (List[str]): List of verification warnings found
    
    Example:
        >>> verifier = OilDatasetVerifier(processed_df)
        >>> is_valid = verifier.verify_all()
        >>> if not is_valid:
        ...     print(verifier.errors)
    """
    
    # Expected feature columns for set_01 dataset
    EXPECTED_FEATURES_SET_01 = [
        'Time_Sin', 'Time_Cos', 'RSI', 'MACD', 'MACD_Signal', 'MACD_Hist',
        'VOL_3D', 'VOL_7D', 'VOL_30D', 'Parkinson_Vol_24H',
        'Return_Skew_24H', 'Return_Kurt_24H', 'SMA_20_Dist', 'SMA_30d_Dist',
        'Volume_Log', 'TARGET_DIR_8PCT_MULTI'
    ]
    
    # Columns that must be non-negative
    NON_NEGATIVE_COLUMNS = ['VOL_3D', 'VOL_7D', 'VOL_30D', 'Parkinson_Vol_24H']
    
    # Columns with bounded ranges
    BOUNDED_COLUMNS = {
        'RSI': (0, 100),
        'Time_Sin': (-1, 1),
        'Time_Cos': (-1, 1),
    }
    
    def __init__(self, df: pd.DataFrame):
        """
        Initialize the verifier with a DataFrame.
        
        Args:
            df: The processed DataFrame to verify
        """
        self.df = df
        self.errors: List[str] = []
        self.warnings: List[str] = []
    
    def reset(self):
        """Reset errors and warnings for a fresh verification run."""
        self.errors = []
        self.warnings = []
    
    # =========================================================================
    # STRUCTURE CHECKS
    # =========================================================================
    
    def check_no_nan(self) -> bool:
        """
        Verify that the DataFrame contains no NaN values.
        
        Returns:
            bool: True if no NaN values found
        """
        nan_counts = self.df.isna().sum()
        total_nan = nan_counts.sum()
        
        if total_nan > 0:
            cols_with_nan = nan_counts[nan_counts > 0].to_dict()
            self.errors.append(
                f"Found {total_nan} NaN values in columns: {cols_with_nan}"
            )
            return False
        return True
    
    def check_no_inf(self) -> bool:
        """
        Verify that the DataFrame contains no infinite values.
        
        Returns:
            bool: True if no inf values found
        """
        # Check numeric columns only
        numeric_cols = self.df.select_dtypes(include=[np.number]).columns
        
        for col in numeric_cols:
            inf_count = np.isinf(self.df[col]).sum()
            if inf_count > 0:
                self.errors.append(
                    f"Found {inf_count} infinite values in column '{col}'"
                )
                return False
        return True
    
    def check_index_monotonic(self) -> bool:
        """
        Verify that the index is strictly monotonically increasing.
        
        This ensures proper time ordering for time-series analysis.
        
        Returns:
            bool: True if index is strictly monotonic increasing
        """
        if not self.df.index.is_monotonic_increasing:
            # Find where monotonicity breaks
            if hasattr(self.df.index, 'to_numpy'):
                idx_array = self.df.index.to_numpy()
                breaks = np.where(idx_array[1:] <= idx_array[:-1])[0]
                if len(breaks) > 0:
                    self.errors.append(
                        f"Index is not monotonically increasing. "
                        f"First break at position {breaks[0]}"
                    )
            else:
                self.errors.append("Index is not monotonically increasing")
            return False
        return True
    
    def check_structure(self) -> bool:
        """
        Run all structure checks.
        
        Returns:
            bool: True if all structure checks pass
        """
        results = [
            self.check_no_nan(),
            self.check_no_inf(),
            self.check_index_monotonic(),
        ]
        return all(results)
    
    # =========================================================================
    # PHYSICS CHECKS
    # =========================================================================
    
    def check_rsi_bounds(self) -> bool:
        """
        Verify that RSI values are within [0, 100].
        
        RSI by definition cannot be outside this range.
        
        Returns:
            bool: True if RSI is within bounds
        """
        if 'RSI' not in self.df.columns:
            self.warnings.append("RSI column not found, skipping RSI bounds check")
            return True
        
        rsi = self.df['RSI']
        min_rsi = rsi.min()
        max_rsi = rsi.max()
        
        if min_rsi < 0 or max_rsi > 100:
            self.errors.append(
                f"RSI out of bounds [0, 100]: min={min_rsi:.4f}, max={max_rsi:.4f}"
            )
            return False
        return True
    
    def check_volatility_non_negative(self) -> bool:
        """
        Verify that volatility columns are non-negative.
        
        Volatility (standard deviation) cannot be negative.
        
        Returns:
            bool: True if all volatility columns are non-negative
        """
        all_valid = True
        
        for col in self.NON_NEGATIVE_COLUMNS:
            if col not in self.df.columns:
                continue
            
            min_val = self.df[col].min()
            if min_val < 0:
                self.errors.append(
                    f"Column '{col}' has negative values: min={min_val:.6f}"
                )
                all_valid = False
        
        return all_valid
    
    def check_time_sin_bounds(self) -> bool:
        """
        Verify that Time_Sin values are within [-1, 1].
        
        Sine function output is bounded to [-1, 1].
        
        Returns:
            bool: True if Time_Sin is within bounds
        """
        if 'Time_Sin' not in self.df.columns:
            self.warnings.append("Time_Sin column not found, skipping bounds check")
            return True
        
        time_sin = self.df['Time_Sin']
        min_val = time_sin.min()
        max_val = time_sin.max()
        
        # Allow small floating point tolerance
        tolerance = 1e-10
        if min_val < -1 - tolerance or max_val > 1 + tolerance:
            self.errors.append(
                f"Time_Sin out of bounds [-1, 1]: min={min_val:.6f}, max={max_val:.6f}"
            )
            return False
        return True
    
    def check_time_cos_bounds(self) -> bool:
        """
        Verify that Time_Cos values are within [-1, 1].
        
        Cosine function output is bounded to [-1, 1].
        
        Returns:
            bool: True if Time_Cos is within bounds
        """
        if 'Time_Cos' not in self.df.columns:
            self.warnings.append("Time_Cos column not found, skipping bounds check")
            return True
        
        time_cos = self.df['Time_Cos']
        min_val = time_cos.min()
        max_val = time_cos.max()
        
        # Allow small floating point tolerance
        tolerance = 1e-10
        if min_val < -1 - tolerance or max_val > 1 + tolerance:
            self.errors.append(
                f"Time_Cos out of bounds [-1, 1]: min={min_val:.6f}, max={max_val:.6f}"
            )
            return False
        return True
    
    def check_physics(self) -> bool:
        """
        Run all physics checks.
        
        Returns:
            bool: True if all physics checks pass
        """
        results = [
            self.check_rsi_bounds(),
            self.check_volatility_non_negative(),
            self.check_time_sin_bounds(),
            self.check_time_cos_bounds(),
        ]
        return all(results)
    
    # =========================================================================
    # SANITY CHECKS (LEAKAGE DETECTION)
    # =========================================================================
    
    def check_no_target_leakage(self, threshold: float = 0.99) -> bool:
        """
        Verify that no feature has perfect correlation with Target.
        
        Perfect correlation indicates target leakage - the model would
        effectively be "cheating" by using future information.
        
        Args:
            threshold: Correlation threshold above which leakage is flagged
        
        Returns:
            bool: True if no leakage detected
        """
        target_col = None
        if 'TARGET_Direction' in self.df.columns:
            target_col = 'TARGET_Direction'
        elif 'Target' in self.df.columns:
            target_col = 'Target'
        
        if target_col is None:
            self.warnings.append(
                "Target column not found (expected TARGET_Direction or Target), skipping leakage check"
            )
            return True
        
        target = self.df[target_col]
        feature_cols = [col for col in self.df.columns if col != target_col]
        
        leaking_features = []
        for col in feature_cols:
            try:
                corr = np.abs(target.corr(self.df[col]))
                if corr >= threshold:
                    leaking_features.append((col, corr))
            except Exception:
                # Skip columns that can't be correlated (e.g., non-numeric)
                continue
        
        if leaking_features:
            leak_str = ", ".join([f"{col}({corr:.4f})" for col, corr in leaking_features])
            self.errors.append(
                f"Potential target leakage detected! Features with |correlation| >= {threshold}: {leak_str}"
            )
            return False
        return True
    
    def check_sanity(self) -> bool:
        """
        Run all sanity checks.
        
        Returns:
            bool: True if all sanity checks pass
        """
        results = [
            self.check_no_target_leakage(),
        ]
        return all(results)
    
    # =========================================================================
    # EDGE CASE CHECKS
    # =========================================================================
    
    def check_zero_price_handling(self, df_with_prices: pd.DataFrame = None) -> bool:
        """
        Check if zero prices would cause division-by-zero errors.
        
        This is typically run on raw data before processing.
        
        Args:
            df_with_prices: DataFrame with Close column (uses self.df if None)
        
        Returns:
            bool: True if no zero prices found
        """
        df = df_with_prices if df_with_prices is not None else self.df
        
        if 'Close' not in df.columns:
            return True  # Already processed, no Close column
        
        zero_count = (df['Close'] == 0).sum()
        if zero_count > 0:
            self.errors.append(
                f"Found {zero_count} zero-price rows which will cause division errors"
            )
            return False
        return True
    
    def check_midnight_wraparound(self) -> bool:
        """
        Check that Time_Sin/Time_Cos are continuous at midnight.
        
        At 23:55 and 00:05, the cyclical time encoding should be similar
        (the values should be close, indicating proper cyclical encoding).
        
        Returns:
            bool: True if midnight wraparound is handled correctly
        """
        if 'Time_Sin' not in self.df.columns:
            return True
        
        # Find rows near midnight (23:55-23:59 and 00:00-00:05)
        hours = self.df.index.hour
        minutes = self.df.index.minute
        
        late_night = (hours == 23) & (minutes >= 55)
        early_morning = (hours == 0) & (minutes <= 5)
        
        if not late_night.any() or not early_morning.any():
            self.warnings.append(
                "No midnight crossing data found, skipping wraparound check"
            )
            return True
        
        late_time_sin = self.df.loc[late_night, 'Time_Sin'].values
        early_time_sin = self.df.loc[early_morning, 'Time_Sin'].values
        
        # At midnight boundary, Time_Sin should be close
        # 23:55 -> minutes = 1435, sin(2*pi*1435/1440) ≈ sin(2*pi*0.9965) ≈ -0.022
        # 00:05 -> minutes = 5, sin(2*pi*5/1440) ≈ sin(2*pi*0.0035) ≈ 0.022
        # They should be close in absolute value and opposite in sign
        
        if len(late_time_sin) > 0 and len(early_time_sin) > 0:
            # Check that the values are in expected range (close to 0 at midnight)
            max_late = np.max(np.abs(late_time_sin))
            max_early = np.max(np.abs(early_time_sin))
            
            # Near midnight, |Time_Sin| should be small (< 0.1)
            if max_late > 0.15 or max_early > 0.15:
                self.errors.append(
                    f"Time_Sin values near midnight are unexpectedly large: "
                    f"late_night max={max_late:.4f}, early_morning max={max_early:.4f}"
                )
                return False
        
        return True
    
    # =========================================================================
    # MAIN VERIFICATION METHOD
    # =========================================================================
    
    def verify_all(self, raise_on_error: bool = False) -> bool:
        """
        Run all verification checks.
        
        Args:
            raise_on_error: If True, raise DataVerificationError on first error
        
        Returns:
            bool: True if all checks pass
        """
        self.reset()
        
        results = [
            self.check_structure(),
            self.check_physics(),
            self.check_sanity(),
        ]
        
        is_valid = all(results)
        
        if not is_valid and raise_on_error:
            raise DataVerificationError(
                f"Data verification failed with {len(self.errors)} errors:\n" +
                "\n".join(f"  - {e}" for e in self.errors)
            )
        
        return is_valid
    
    def get_report(self) -> Dict[str, Any]:
        """
        Get a detailed verification report.
        
        Returns:
            Dict containing verification results, errors, and warnings
        """
        return {
            'is_valid': len(self.errors) == 0,
            'num_errors': len(self.errors),
            'num_warnings': len(self.warnings),
            'errors': self.errors.copy(),
            'warnings': self.warnings.copy(),
            'shape': self.df.shape,
            'columns': list(self.df.columns),
        }


def verify_dataset(df: pd.DataFrame, raise_on_error: bool = True) -> bool:
    """
    Convenience function to verify a dataset.
    
    Args:
        df: DataFrame to verify
        raise_on_error: If True, raise exception on verification failure
    
    Returns:
        bool: True if dataset passes all verification checks
    """
    verifier = OilDatasetVerifier(df)
    return verifier.verify_all(raise_on_error=raise_on_error)
