import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401
from datetime import datetime

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


class AlphaFactory:
    """
    Feature generation engine for OHLCV-based signals.

    Current clusters:
    - Volatility: Parkinson, Rogers-Satchell, Yang-Zhang
    - Liquidity: Amihud illiquidity, Corwin-Schultz spread
    - Structure: Efficiency ratio (PER)
    - Momentum: RSI, Bollinger Bands (via pandas_ta)
    """

    REQUIRED_COLUMNS = {"Open", "High", "Low", "Close", "Volume"}

    def __init__(self, df: pd.DataFrame):
        missing = self.REQUIRED_COLUMNS - set(df.columns)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(f"Missing required columns: {missing_list}")

        self.df = df.copy()
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
        macro_windows: dict[str, int] | None = None,
        log_progress: bool = False,
    ) -> pd.DataFrame:
        """Run all feature clusters across multiple rolling windows."""
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
            self.add_trend_cluster(window=window)
            self.add_volume_flow_cluster(window=window)
            if log_progress:
                print(f"[AlphaFactory] Window {window} done at {datetime.now().isoformat(timespec='seconds')}")

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
        dollar_vol = (self.close * self.volume).replace(0, np.nan)
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

    def add_trend_cluster(self, window: int) -> pd.DataFrame:
        """Trend positioning and regression fit."""
        suffix = f"_{window}"
        roll_max = self.close.rolling(window).max()
        roll_min = self.close.rolling(window).min()
        range_span = roll_max - roll_min
        self.df[f"TREND_DONCHIAN_POS{suffix}"] = (self.close - roll_min) / range_span

        linreg = ta.linreg(self.close, length=window, slope=True, r=True)
        if isinstance(linreg, pd.Series):
            self.df[f"TREND_LR_SLOPE{suffix}"] = linreg
            self.df[f"TREND_LR_R2{suffix}"] = np.nan
        else:
            slope_col = linreg.get(f"LRS_{window}") if linreg is not None else None
            r_col = linreg.get(f"LRr_{window}") if linreg is not None else None
            if slope_col is None and linreg is not None:
                slope_col = linreg.get(f"LRS_{window}.0")
            if r_col is None and linreg is not None:
                r_col = linreg.get(f"LRr_{window}.0")

            self.df[f"TREND_LR_SLOPE{suffix}"] = slope_col if slope_col is not None else np.nan
            if r_col is not None:
                self.df[f"TREND_LR_R2{suffix}"] = r_col.pow(2)
            else:
                self.df[f"TREND_LR_R2{suffix}"] = np.nan

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

        vol_sum = self.volume.rolling(window).sum()
        vwap = (self.close * self.volume).rolling(window).sum() / vol_sum.replace(0, np.nan)
        self.df[f"VOLFLOW_VWAP_DIST{suffix}"] = (self.close - vwap) / vwap

        return self.df

    def add_macro_context(self, macro_windows: dict[str, int] | None = None) -> pd.DataFrame:
        """Macro context features via resampled windows."""
        if not isinstance(self.df.index, pd.DatetimeIndex):
            raise ValueError("AlphaFactory requires a DatetimeIndex for resampling.")

        if macro_windows is None:
            macro_windows = {"1M": 840, "3M": 2160}

        ohlcv = {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }

        hourly = self.df.resample("1h").agg(ohlcv).dropna()
        macro_frames = []
        for label, window in macro_windows.items():
            macro_frames.append(self._add_macro_donchian(hourly, window=window, label=label))

        macro = pd.concat(macro_frames, axis=1)
        macro = macro.reindex(self.df.index, method="ffill")
        self.df = self.df.join(macro)
        return self.df

    def _add_macro_donchian(self, df: pd.DataFrame, window: int, label: str) -> pd.DataFrame:
        roll_max = df["High"].rolling(window).max()
        roll_min = df["Low"].rolling(window).min()
        range_span = roll_max - roll_min

        width_col = f"MACRO_WIDTH_{label}"
        pos_col = f"MACRO_POS_{label}"
        df[width_col] = range_span / df["Close"]
        df[pos_col] = (df["Close"] - roll_min) / range_span
        return df[[width_col, pos_col]]

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

        return self.df
