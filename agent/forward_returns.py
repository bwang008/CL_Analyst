"""
Forward Returns Calculator (Workflow C Foundation)

Computes vectorized log forward returns for multiple horizons simultaneously.
Log returns are used because they are additive across time, symmetric, and
avoid the percentage-return bias in multi-period analysis.

Formula: r_{t,t+N} = ln(P_{t+N} / P_t)
"""

import numpy as np
import pandas as pd


def compute_forward_returns(
    ohlcv: pd.DataFrame,
    horizons: list[int] = [6, 12, 24, 48, 72],
    price_col: str = "Close",
) -> pd.DataFrame:
    """
    Compute log forward returns for each horizon.

    Returns DataFrame with columns: 'fwd_ret_6', 'fwd_ret_12', 'fwd_ret_24',
    'fwd_ret_48', 'fwd_ret_72' (one per horizon).

    Formula: r_{t,t+N} = ln(P_{t+N} / P_t)

    Log returns are used because:
    - They are additive across time (ln(P2/P0) = ln(P2/P1) + ln(P1/P0))
    - They are symmetric (a +10% and -10% move don't compound asymmetrically)
    - They avoid the percentage-return bias in multi-period analysis

    Implementation: np.log(df[price_col].shift(-horizon) / df[price_col])
    NaN tail rows (last max(horizons) bars) are preserved but excluded
    by downstream consumers.

    Parameters
    ----------
    ohlcv : pd.DataFrame
        DataFrame with at least a ``price_col`` column, indexed by DateTime.
    horizons : list[int]
        Forward-looking horizons in bars (default [6, 12, 24, 48, 72]).
    price_col : str
        Column to use for price (default "Close").

    Returns
    -------
    pd.DataFrame
        Same index as *ohlcv*, with one column per horizon named
        ``fwd_ret_{H}``.
    """
    if price_col not in ohlcv.columns:
        raise KeyError(
            f"Price column '{price_col}' not found in DataFrame. "
            f"Available columns: {list(ohlcv.columns)}"
        )

    price = ohlcv[price_col]

    result = pd.DataFrame(index=ohlcv.index)
    for h in horizons:
        result[f"fwd_ret_{h}"] = np.log(price.shift(-h) / price)

    return result
