import warnings

import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401
from datetime import datetime

# Suppress pandas PerformanceWarning about DataFrame fragmentation.
# Feature generation naturally assigns many columns one at a time;
# the fragmentation is harmless and the warnings spam the live log.
warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)

try:
    from numba import njit  # type: ignore[import]
except ImportError:  # pragma: no cover - optional speedup
    njit = None


def _jit_or_py(func):
    if njit is None:
        return func
    return njit(func)


@_jit_or_py
def _hurst_rs_numba(values):
    if np.isnan(values[0]):
        return np.nan

    mean_val = np.mean(values)
    cum_dev = np.cumsum(values - mean_val)
    r = np.max(cum_dev) - np.min(cum_dev)
    s = np.std(values)
    if r == 0.0 or s == 0.0:
        return 0.0
    return np.log(r / s) / np.log(len(values))


@_jit_or_py
def _entropy_numba(values):
    if np.isnan(values[0]):
        return np.nan

    n_bins = 20
    v_min = np.min(values)
    v_max = np.max(values)
    if v_min == v_max:
        return 0.0

    bins = np.zeros(n_bins, dtype=np.int64)
    bin_width = (v_max - v_min) / n_bins
    for x in values:
        bin_idx = int((x - v_min) / bin_width)
        if bin_idx >= n_bins:
            bin_idx = n_bins - 1
        bins[bin_idx] += 1

    probs = bins / len(values)
    ent = 0.0
    for p in probs:
        if p > 0.0:
            ent -= p * np.log(p)
    return ent


@_jit_or_py
def _corwin_schultz_numba(high, low, window):
    n = len(high)
    spreads = np.full(n, np.nan)
    log_hl_sq = np.log(high / low) ** 2
    const_sqrt2 = np.sqrt(2.0)
    denom = 3.0 - 2.0 * const_sqrt2

    for i in range(1, n):
        h2 = max(high[i], high[i - 1])
        l2 = min(low[i], low[i - 1])
        gamma = np.log(h2 / l2) ** 2
        beta = log_hl_sq[i] + log_hl_sq[i - 1]
        if beta <= 0.0 or gamma <= 0.0:
            spreads[i] = 0.0
            continue

        sqrt_beta = np.sqrt(beta)
        alpha = (const_sqrt2 * sqrt_beta - sqrt_beta) / denom - np.sqrt(gamma / denom)
        if alpha <= 0.0:
            spreads[i] = 0.0
        else:
            exp_alpha = np.exp(alpha)
            spreads[i] = 2.0 * (exp_alpha - 1.0) / (1.0 + exp_alpha)

    rolling_spread = np.full(n, np.nan)
    current_sum = 0.0
    count = 0
    for i in range(n):
        if np.isfinite(spreads[i]):
            current_sum += spreads[i]
            count += 1
        if i >= window:
            old_val = spreads[i - window]
            if np.isfinite(old_val):
                current_sum -= old_val
                count -= 1
        if i >= window - 1:
            if count > 0:
                rolling_spread[i] = current_sum / window
    return rolling_spread


@_jit_or_py
def _rolling_slope_r2_numba(y, window):
    n = len(y)
    slopes = np.full(n, np.nan)
    r2s = np.full(n, np.nan)

    x = np.arange(window)
    sum_x = np.sum(x)
    sum_x_sq = np.sum(x * x)
    denom_x = window * sum_x_sq - sum_x * sum_x
    if denom_x == 0.0:
        return slopes, r2s

    for i in range(window, n + 1):
        y_slice = y[i - window : i]
        if np.isnan(y_slice).any():
            continue

        sum_y = np.sum(y_slice)
        sum_xy = np.sum(x * y_slice)

        numerator = window * sum_xy - sum_x * sum_y
        slope = numerator / denom_x
        intercept = (sum_y - slope * sum_x) / window

        mean_y = sum_y / window
        ss_tot = np.sum((y_slice - mean_y) ** 2)
        y_pred = slope * x + intercept
        ss_res = np.sum((y_slice - y_pred) ** 2)

        if ss_tot == 0.0:
            r2 = 0.0
        else:
            r2 = 1.0 - (ss_res / ss_tot)

        slopes[i - 1] = slope
        r2s[i - 1] = r2

    return slopes, r2s


class AlphaFactory:
    """
    Feature generation engine for OHLCV-based signals.

    Current clusters:
    - Volatility: Parkinson, Rogers-Satchell, Yang-Zhang, Vol-ROC, Vol-of-Vol
    - Liquidity: Amihud illiquidity, Corwin-Schultz spread
    - Structure: Efficiency ratio (PER), candle-microstructure
    - Momentum: RSI, Bollinger Bands, ADX, MACD (via pandas_ta)
    """

    REQUIRED_COLUMNS = {"Open", "High", "Low", "Close", "Volume"}

    def __init__(self, df: pd.DataFrame, bars_per_hour: int = 12):
        missing = self.REQUIRED_COLUMNS - set(df.columns)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(f"Missing required columns: {missing_list}")

        self.df = df.copy()
        self.bars_per_hour = bars_per_hour
        self.open = self.df["Open"]
        self.high = self.df["High"]
        self.low = self.df["Low"]
        self.close = self.df["Close"]
        self.volume = self.df["Volume"]

        self.df["log_ret"] = np.log(self.close / self.close.shift(1))

    def add_all_features(
        self,
        windows: list[int] | tuple[int, ...] | int | None = None,
        include_momentum: bool = True,
        include_macro: bool = True,
        include_extended: bool = False,
        macro_windows: dict[str, int] | None = None,
        log_progress: bool = False,
    ) -> pd.DataFrame:
        """Run all feature clusters across multiple rolling windows.

        Args:
            windows: Rolling window sizes (in bars).
            include_momentum: Add momentum cluster (RSI, BB, ADX, MACD).
            include_macro: Add macro context features.
            include_extended: Add extended clusters (set_07+): return
                distribution, stochastic oscillator, Chaikin Money Flow,
                and cross-timeframe ratios.
            macro_windows: Dict of label→hourly-window for macro context.
            log_progress: Print progress timestamps.
        """
        if windows is None:
            windows = [24, 288, 1440]
        if isinstance(windows, int):
            windows = [windows]

        if log_progress:
            print(f"[AlphaFactory] Start: {datetime.now().isoformat(timespec='seconds')}")

        for window in windows:
            if log_progress:
                print(f"[AlphaFactory] Window {window} start")
            self.add_volatility_cluster(window=window)
            self.add_liquidity_cluster(window=window)
            self.add_structure_cluster(window=window)
            self.add_microstructure_cluster()  # Single pass, not window dependent
            self.add_trend_cluster(window=window)
            self.add_volume_flow_cluster(window=window)
            if include_extended:
                self.add_return_distribution_cluster(window=window)
                self.add_stochastic_cluster(window=window)
                self.add_exhaustion_cluster(window=window)
            if log_progress:
                print(f"[AlphaFactory] Window {window} done at {datetime.now().isoformat(timespec='seconds')}")

        if include_extended:
            if log_progress:
                print(f"[AlphaFactory] Cross-timeframe ratios start")
            self.add_cross_timeframe_ratios()
            if log_progress:
                print(f"[AlphaFactory] Cross-timeframe ratios done at {datetime.now().isoformat(timespec='seconds')}")

        if include_momentum:
            if log_progress:
                print(f"[AlphaFactory] Momentum start")
            self.add_momentum_cluster()
            if log_progress:
                print(f"[AlphaFactory] Momentum done at {datetime.now().isoformat(timespec='seconds')}")
        if include_macro:
            if log_progress:
                print(f"[AlphaFactory] Macro start")
            self.add_macro_context(macro_windows=macro_windows)
            if log_progress:
                print(f"[AlphaFactory] Macro done at {datetime.now().isoformat(timespec='seconds')}")

        self.df.replace([np.inf, -np.inf], np.nan, inplace=True)
        if log_progress:
            print(f"[AlphaFactory] Complete: {datetime.now().isoformat(timespec='seconds')}")
        return self.df

    def add_volatility_cluster(self, window: int = 24) -> pd.DataFrame:
        """Range-based volatility estimators."""
        const_parkinson = 1.0 / (4.0 * np.log(2.0))
        log_hl = np.log(self.high / self.low)
        suffix = f"_{window}"
        self.df[f"VOL_PARK{suffix}"] = np.sqrt(
            const_parkinson * (log_hl**2).rolling(window).mean()
        )

        # Volatility Second-Order Features
        self.df[f"VOL_ROC{suffix}"] = self.df[f"VOL_PARK{suffix}"].pct_change(window)
        self.df[f"VOL_VOLVOL{suffix}"] = (
            self.df[f"VOL_PARK{suffix}"].rolling(window).std()
            / self.df[f"VOL_PARK{suffix}"].rolling(window).mean()
        )

        log_hc = np.log(self.high / self.close)
        log_ho = np.log(self.high / self.open)
        log_lc = np.log(self.low / self.close)
        log_lo = np.log(self.low / self.open)
        rs_term = (log_hc * log_ho) + (log_lc * log_lo)
        self.df[f"VOL_RS{suffix}"] = np.sqrt(rs_term.rolling(window).mean())

        log_oc = np.log(self.open / self.close.shift(1))
        var_overnight = (log_oc**2).rolling(window).mean()
        log_co = np.log(self.close / self.open)
        var_open_close = (log_co**2).rolling(window).mean()
        var_rs = rs_term.rolling(window).mean()

        k = 0.34
        self.df[f"VOL_YZ{suffix}"] = np.sqrt(
            var_overnight + k * var_open_close + (1 - k) * var_rs
        )

        return self.df

    def add_liquidity_cluster(self, window: int = 24) -> pd.DataFrame:
        """Liquidity proxies from OHLCV data."""
        dollar_vol = (self.close * self.volume).clip(lower=1e-8)
        suffix = f"_{window}"
        self.df[f"LIQ_AMIHUD{suffix}"] = (
            (self.df["log_ret"].abs() / dollar_vol).rolling(window).mean() * 1e6
        )

        high_values = self.high.to_numpy(dtype=np.float64)
        low_values = self.low.to_numpy(dtype=np.float64)
        self.df[f"LIQ_CORWIN{suffix}"] = _corwin_schultz_numba(
            high_values, low_values, window
        )

        return self.df

    def add_structure_cluster(self, window: int = 24) -> pd.DataFrame:
        """Efficiency ratio (PER) for trend vs. noise."""
        change = self.close.diff()
        abs_change = change.abs()
        direction = self.close.diff(window).abs()
        volatility = abs_change.rolling(window).sum()
        self.df[f"STRUC_EFFICIENCY_{window}"] = direction / volatility

        if "STRUC_HURST_100" not in self.df.columns:
            physics_window = 100
            log_ret = self.df["log_ret"].fillna(0.0)

            self.df["STRUC_HURST_100"] = log_ret.rolling(
                physics_window, min_periods=physics_window
            ).apply(_hurst_rs_numba, raw=True)
            self.df["STRUC_ENTROPY_100"] = log_ret.rolling(
                physics_window, min_periods=physics_window
            ).apply(_entropy_numba, raw=True)

        return self.df

    def add_microstructure_cluster(self) -> pd.DataFrame:
        """Bar microstructure features: body and wick ratios."""
        if "STRUC_BODY_RATIO" in self.df.columns:
            return self.df

        candle_range = (self.high - self.low).clip(lower=1e-8)
        body = (self.close - self.open).abs()
        
        self.df["STRUC_BODY_RATIO"] = body / candle_range
        self.df["STRUC_WICK_UP_RATIO"] = (self.high - np.maximum(self.open, self.close)) / candle_range
        self.df["STRUC_WICK_LOW_RATIO"] = (np.minimum(self.open, self.close) - self.low) / candle_range
        
        # Color: 1 for green, 0 for red
        self.df["STRUC_COLOR"] = (self.close >= self.open).astype(int)
        
        return self.df

    def add_trend_cluster(self, window: int) -> pd.DataFrame:
        """Trend positioning and regression fit."""
        suffix = f"_{window}"
        roll_max = self.close.rolling(window).max()
        roll_min = self.close.rolling(window).min()
        range_span = (roll_max - roll_min).clip(lower=1e-8)
        self.df[f"TREND_DONCHIAN_POS{suffix}"] = (self.close - roll_min) / range_span

        prices = self.close.to_numpy(dtype=np.float64)
        slopes, r2s = _rolling_slope_r2_numba(prices, window)
        self.df[f"TREND_LR_SLOPE{suffix}"] = slopes
        self.df[f"TREND_LR_R2{suffix}"] = r2s

        return self.df

    def add_volume_flow_cluster(self, window: int) -> pd.DataFrame:
        """Volume flow and price/volume divergence signals."""
        suffix = f"_{window}"
        obv = ta.obv(self.close, self.volume)
        if obv is None:
            self.df[f"VOLFLOW_OBV_SLOPE{suffix}"] = np.nan
            self.df[f"VOLFLOW_DIVERGENCE{suffix}"] = np.nan
        else:
            obv_slope = ta.linreg(obv, length=window, slope=True)
            price_slope = ta.linreg(self.close, length=window, slope=True)
            self.df[f"VOLFLOW_OBV_SLOPE{suffix}"] = (
                obv_slope if isinstance(obv_slope, pd.Series) else obv_slope.iloc[:, 0]
            )
            price_slope_series = (
                price_slope if isinstance(price_slope, pd.Series) else price_slope.iloc[:, 0]
            )
            self.df[f"VOLFLOW_DIVERGENCE{suffix}"] = (
                self.df[f"VOLFLOW_OBV_SLOPE{suffix}"] - price_slope_series
            )

        vol_sum = self.volume.rolling(window).sum().clip(lower=1e-8)
        vwap = (self.close * self.volume).rolling(window).sum() / vol_sum
        self.df[f"VOLFLOW_VWAP_DIST{suffix}"] = (self.close - vwap) / vwap

        # Chaikin Money Flow (A5): volume-weighted close position
        clv = ((self.close - self.low) - (self.high - self.close)) / (
            self.high - self.low
        ).clip(lower=1e-8)
        mf_volume = clv * self.volume
        self.df[f"VOLFLOW_CMF{suffix}"] = (
            mf_volume.rolling(window).sum()
            / self.volume.rolling(window).sum().clip(lower=1e-8)
        )

        return self.df

    def add_macro_context(self, macro_windows: dict[str, int] | None = None) -> pd.DataFrame:
        """Macro context features via causally-safe bar-level rolling windows.

        Each macro window is specified in hours and converted to bars
        using ``self.bars_per_hour`` (12 for 5-min data, 1 for 1H data).
        Donchian channel position and width are computed
        directly on the bar-level High/Low/Close — no resample, no
        lookahead.

        Previous implementation used ``resample("1h")`` which created
        complete hourly bars from all 12 five-minute bars in the hour,
        then forward-filled back to 5-min resolution. This leaked up to
        55 minutes of future data into training features. Fixed 2026-03-20.
        """
        if macro_windows is None:
            macro_windows = {"1M": 840, "3M": 2160}

        for label, hours in macro_windows.items():
            bars = int(hours * self.bars_per_hour)

            # ffill raw data to prevent NaN propagation from holiday gaps,
            # then roll with min_periods=1 so the window warms up from row 1.
            roll_max = self.df["High"].ffill().rolling(window=bars, min_periods=1).max()
            roll_min = self.df["Low"].ffill().rolling(window=bars, min_periods=1).min()

            range_span = roll_max - roll_min
            # Bulletproof protection against floating-point 0s and division
            # by zero.  .replace(0, x) can miss float zeros; .clip() is safe.
            range_span = range_span.clip(lower=1e-8)

            self.df[f"MACRO_WIDTH_{label}"] = range_span / self.close
            self.df[f"MACRO_POS_{label}"] = (self.close - roll_min) / range_span

        return self.df

    def add_momentum_cluster(self) -> pd.DataFrame:
        """Momentum indicators via pandas_ta."""
        rsi = self.df.ta.rsi(length=14)
        if rsi is not None:
            self.df["MOM_RSI_14"] = rsi if isinstance(rsi, pd.Series) else rsi.iloc[:, 0]
        else:
            self.df["MOM_RSI_14"] = np.nan

        bb = self.df.ta.bbands(length=20, std=2)
        if bb is not None and not bb.empty:
            bb_width = bb.get("BBB_20_2.0")
            if bb_width is None:
                bb_width = bb.get("BBW_20_2.0")
            bb_pctb = bb.get("BBP_20_2.0")

            self.df["MOM_BB_Width"] = bb_width if bb_width is not None else np.nan
            self.df["MOM_BB_PctB"] = bb_pctb if bb_pctb is not None else np.nan
        else:
            self.df["MOM_BB_Width"] = np.nan
            self.df["MOM_BB_PctB"] = np.nan

        # ADX (Trend Strength)
        adx = self.df.ta.adx(length=14)
        if adx is not None:
            self.df["MOM_ADX_14"] = adx.iloc[:, 0]
            self.df["MOM_DMP_14"] = adx.iloc[:, 1]
            self.df["MOM_DMN_14"] = adx.iloc[:, 2]
        
        # MACD (Trend Intensity)
        macd = self.df.ta.macd(fast=12, slow=26, signal=9)
        if macd is not None:
            self.df["MOM_MACD"] = macd.iloc[:, 0]
            self.df["MOM_MACD_Signal"] = macd.iloc[:, 1]
            self.df["MOM_MACD_Hist"] = macd.iloc[:, 2]

        return self.df

    # ------------------------------------------------------------------
    # Extended clusters (set_07+)
    # ------------------------------------------------------------------

    def add_return_distribution_cluster(self, window: int) -> pd.DataFrame:
        """Rolling return distribution shape features."""
        suffix = f"_{window}"
        log_ret = self.df["log_ret"]

        # Rolling skewness: asymmetry indicator
        self.df[f"DIST_SKEW{suffix}"] = log_ret.rolling(window).skew()

        # Rolling kurtosis: tail thickness
        self.df[f"DIST_KURT{suffix}"] = log_ret.rolling(window).kurt()

        # Rolling Z-score of current return vs recent distribution
        roll_mean = log_ret.rolling(window).mean()
        roll_std = log_ret.rolling(window).std()
        self.df[f"DIST_ZSCORE{suffix}"] = (
            (log_ret - roll_mean) / roll_std.replace(0, np.nan)
        )

        return self.df

    def add_stochastic_cluster(self, window: int) -> pd.DataFrame:
        """Stochastic oscillator features at multiple timeframes."""
        suffix = f"_{window}"

        roll_low = self.low.rolling(window).min()
        roll_high = self.high.rolling(window).max()
        range_span = (roll_high - roll_low).clip(lower=1e-8)

        # %K: raw stochastic (Close relative to High-Low range)
        stoch_k = (self.close - roll_low) / range_span
        self.df[f"MOM_STOCH_K{suffix}"] = stoch_k

        # %D: smoothed stochastic (3-bar SMA of %K)
        self.df[f"MOM_STOCH_D{suffix}"] = stoch_k.rolling(3).mean()

        return self.df

    def add_exhaustion_cluster(self, window: int) -> pd.DataFrame:
        """Move-exhaustion features: cumulative return, ATR-normalised, and
        distance from recent high.

        These help the model detect overextended moves where a snap-back
        is more likely than continuation.
        """
        suffix = f"_{window}"

        # 1) Cumulative log-return over the window
        cum_ret = self.df["log_ret"].rolling(window).sum()
        self.df[f"EXHAUST_CUM_RET{suffix}"] = cum_ret

        # 2) Cumulative return normalised by ATR (scale-invariant)
        atr_col = "ATR_14"
        if atr_col not in self.df.columns:
            import pandas_ta as _ta  # noqa: F811
            self.df[atr_col] = self.df.ta.atr(length=14)
        atr = self.df[atr_col].replace(0, np.nan)
        # Convert cum log-return to price-space move, then /ATR
        price_move = cum_ret * self.close  # approx price change
        self.df[f"EXHAUST_CUM_ATR{suffix}"] = price_move / atr

        # 3) Distance from recent high in ATR units (≤ 0 normally)
        recent_high = self.close.rolling(window).max()
        self.df[f"EXHAUST_DIST_HIGH{suffix}"] = (
            (self.close - recent_high) / atr
        )

        return self.df

    def add_cross_timeframe_ratios(self) -> pd.DataFrame:
        """Ratios between short and long-term features for regime detection."""
        # Volatility regime: short-term vol vs long-term vol
        if "VOL_PARK_288" in self.df.columns and "VOL_PARK_10080" in self.df.columns:
            self.df["CROSS_VOL_RATIO_1D_35D"] = (
                self.df["VOL_PARK_288"]
                / self.df["VOL_PARK_10080"].replace(0, np.nan)
            )
        if "VOL_PARK_864" in self.df.columns and "VOL_PARK_4032" in self.df.columns:
            self.df["CROSS_VOL_RATIO_3D_14D"] = (
                self.df["VOL_PARK_864"]
                / self.df["VOL_PARK_4032"].replace(0, np.nan)
            )

        # Trend regime: short-term Donchian vs long-term Donchian
        if "TREND_DONCHIAN_POS_288" in self.df.columns and "TREND_DONCHIAN_POS_10080" in self.df.columns:
            self.df["CROSS_TREND_DIFF_1D_35D"] = (
                self.df["TREND_DONCHIAN_POS_288"]
                - self.df["TREND_DONCHIAN_POS_10080"]
            )
        if "TREND_DONCHIAN_POS_864" in self.df.columns and "TREND_DONCHIAN_POS_4032" in self.df.columns:
            self.df["CROSS_TREND_DIFF_3D_14D"] = (
                self.df["TREND_DONCHIAN_POS_864"]
                - self.df["TREND_DONCHIAN_POS_4032"]
            )

        # VWAP regime: short-term vs long-term VWAP distance
        if "VOLFLOW_VWAP_DIST_288" in self.df.columns and "VOLFLOW_VWAP_DIST_10080" in self.df.columns:
            self.df["CROSS_VWAP_DIFF_1D_35D"] = (
                self.df["VOLFLOW_VWAP_DIST_288"]
                - self.df["VOLFLOW_VWAP_DIST_10080"]
            )

        return self.df
