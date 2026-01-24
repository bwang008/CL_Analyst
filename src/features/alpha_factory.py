import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401


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

    def add_all_features(self, window: int = 24) -> pd.DataFrame:
        """Run all feature clusters with a shared rolling window."""
        self.add_volatility_cluster(window=window)
        self.add_liquidity_cluster(window=window)
        self.add_structure_cluster(window=window)
        self.add_momentum_cluster()

        self.df.replace([np.inf, -np.inf], np.nan, inplace=True)
        return self.df

    def add_volatility_cluster(self, window: int = 24) -> pd.DataFrame:
        """Range-based volatility estimators."""
        const_parkinson = 1.0 / (4.0 * np.log(2.0))
        log_hl = np.log(self.high / self.low)
        self.df[f"VOL_PARKINSON_{window}"] = np.sqrt(
            const_parkinson * (log_hl**2).rolling(window).mean()
        )

        log_hc = np.log(self.high / self.close)
        log_ho = np.log(self.high / self.open)
        log_lc = np.log(self.low / self.close)
        log_lo = np.log(self.low / self.open)
        rs_term = (log_hc * log_ho) + (log_lc * log_lo)
        self.df[f"VOL_RS_{window}"] = np.sqrt(rs_term.rolling(window).mean())

        log_oc = np.log(self.open / self.close.shift(1))
        var_overnight = (log_oc**2).rolling(window).mean()
        log_co = np.log(self.close / self.open)
        var_open_close = (log_co**2).rolling(window).mean()
        var_rs = rs_term.rolling(window).mean()

        k = 0.34
        self.df[f"VOL_YZ_{window}"] = np.sqrt(
            var_overnight + k * var_open_close + (1 - k) * var_rs
        )

        return self.df

    def add_liquidity_cluster(self, window: int = 24) -> pd.DataFrame:
        """Liquidity proxies from OHLCV data."""
        dollar_vol = (self.close * self.volume).replace(0, np.nan)
        self.df[f"LIQ_AMIHUD_{window}"] = (
            (self.df["log_ret"].abs() / dollar_vol).rolling(window).mean() * 1e6
        )

        hl = self.high / self.low
        hl_2 = self.high.rolling(2).max() / self.low.rolling(2).min()
        beta = (np.log(hl) ** 2).rolling(2).sum()
        gamma = np.log(hl_2) ** 2

        denom = 3.0 - 2.0 * np.sqrt(2.0)
        alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / denom - np.sqrt(gamma / denom)
        spread = (2.0 * (np.exp(alpha) - 1.0)) / (1.0 + np.exp(alpha))
        spread = spread.clip(lower=0)

        self.df[f"LIQ_CORWIN_{window}"] = spread.rolling(window).mean()

        return self.df

    def add_structure_cluster(self, window: int = 24) -> pd.DataFrame:
        """Efficiency ratio (PER) for trend vs. noise."""
        change = self.close.diff()
        abs_change = change.abs()
        direction = self.close.diff(window).abs()
        volatility = abs_change.rolling(window).sum()
        self.df[f"STRUC_EFFICIENCY_{window}"] = direction / volatility
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

        return self.df
