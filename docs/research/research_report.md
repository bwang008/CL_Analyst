Institutional-Grade Feature Generation Strategy for LightGBM Volatility Breakout Prediction in Crude Oil Futures

Executive Summary
The quantitative modeling of volatility breakouts in Crude Oil futures (CL) constitutes a formidable challenge in modern algorithmic trading, necessitating a departure from rudimentary technical analysis toward a rigorous, structural deconstruction of market dynamics. Volatility in the energy complex is not merely a statistical artifact of price dispersion; it is a complex, path-dependent phenomenon driven by the interplay of liquidity provision, informed order flow toxicity, and macro-structural regime shifts. To effectively predict these phase transitions using gradient-boosting decision tree ensembles - specifically LightGBM - it is insufficient to rely on raw price levels or standard oscillators. Instead, the feature space must be populated with high-fidelity proxies that estimate the latent state variables of the market: the cost of liquidity, the persistence of memory, and the entropy of information flow.
This report articulates a comprehensive, institutional-grade feature generation strategy designed to produce a pool of 100-200 robust predictors derived exclusively from OHLCV (Open, High, Low, Close, Volume) data. By synthesizing academic microstructure theory - including the Corwin-Schultz spread estimator, the Amihud illiquidity ratio, and the Roll measure - with advanced signal processing techniques like Detrended Fluctuation Analysis (DFA) and Shannon Entropy, this framework aims to capture the multidimensional precursors to volatility expansion. Furthermore, this strategy integrates specific, actionable insights from high-stakes forecasting competitions, notably the Optiver Realized Volatility Prediction and Jane Street Market Prediction challenges, to bridge the gap between theoretical finance and empirical machine learning efficacy. The resulting feature set is optimized for LightGBM's leaf-wise tree growth algorithm, ensuring the model can isolate non-linear interactions and detect regime shifts with high precision.

1. Introduction: The Microstructure of Volatility in Energy Markets
The prediction of volatility breakouts requires a fundamental understanding of the data-generating process (DGP) governing asset returns. In the context of Crude Oil futures, this DGP is influenced by unique boundary conditions including physical storage constraints, geopolitical supply shocks, and the distinct behavior of the forward curve (contango and backwardation). However, the immediate precursor to a price dislocation - a breakout - is invariably a microstructure event. It is the collapse of liquidity on one side of the order book or an influx of toxic flow that overwhelms passive liquidity providers.

1.1 The Limitations of Standard Features
Traditional technical analysis features, such as Simple Moving Averages (SMA) or the Relative Strength Index (RSI), treat the price series as a homogeneous signal. They fail to distinguish between a price move caused by aggressive informed trading (high volume, high impact) and one caused by a liquidity vacuum (low volume, high impact). For an "institutional grade" model, features must decompose the price path to reveal how the price moved, not just where it moved. This distinction is critical for LightGBM, which excels at partitioning feature space based on information gain but cannot infer the physics of the market unless explicitly provided with structural descriptors.

1.2 The LightGBM Paradigm in Financial Forecasting
LightGBM (Light Gradient Boosting Machine) has emerged as the preeminent algorithm for tabular financial data, consistently outperforming deep learning baselines in Kaggle competitions like the Optiver Realized Volatility challenge.1 Its leaf-wise growth strategy allows for deeper, more complex trees that can model high-order interactions between features. However, this flexibility also makes it prone to overfitting noise. Therefore, the feature engineering strategy must prioritize structural features - metrics that describe the distribution and memory of the time series - over non-stationary price levels.
The strategy outlined herein focuses on generating features that serve as proxies for three critical latent variables:
Liquidity: The cost and difficulty of executing a trade, estimated via Corwin-Schultz and Amihud measures.
Information Asymmetry: The presence of informed traders, estimated via the Roll model and covariance properties.
Structural Persistence: The tendency of the market to trend or mean-revert, estimated via Hurst exponents and Entropy.

2. Volatility Estimators: The Foundation of the Feature Pool
Before predicting a breakout, one must rigorously define the current state of volatility. In the domain of OHLCV data, the standard deviation of close-to-close returns is a sub-optimal estimator because it discards the path information contained in the High and Low prices. Institutional strategies employ range-based estimators which offer superior efficiency, converging to the true variance with fewer data points.

2.1 Range-Based Volatility Proxies
Range-based estimators exploit the extreme values of the price path to provide a more granular view of intraday variance. For Crude Oil, which often exhibits significant opening jumps due to overnight news, the choice of estimator is non-trivial.

2.1.1 The Parkinson Estimator
The Parkinson estimator (1980) utilizes the High and Low prices, assuming a geometric Brownian motion with zero drift. It is defined as:

$$\\sigma_{Parkinson} = \\sqrt{\\frac{1}{4N \\ln(2)} \\sum_{t=1}^{N} \\ln\\left(\\frac{H_t}{L_t}\\right)^2}$$

This metric captures the "expansion" of the market range. A divergence between Parkinson volatility and standard close-to-close volatility often signals a "liquidity trap" where price travels a long distance intraday but settles near the open - a classic precursor to a reversal or an explosive breakout as trapped traders exit.3

2.1.2 The Rogers-Satchell Estimator
To account for the non-zero drift often observed in trending commodity markets, the Rogers-Satchell estimator is preferred. It is robust to trends and allows for a more accurate estimation during the directional phases of a breakout:

$$\\sigma_{RS}^2 = \\frac{1}{N} \\sum_{t=1}^{N} \\left[ \\ln\\left(\\frac{H_t}{C_t}\\right)\\ln\\left(\\frac{H_t}{O_t}\\right) + \\ln\\left(\\frac{L_t}{C_t}\\right)\\ln\\left(\\frac{L_t}{O_t}\\right) \\right]$$

This estimator effectively penalizes the variance if the Close is far from the Open in the direction of the trend, isolating the volatility that is orthogonal to the trend.3

2.1.3 The Yang-Zhang Estimator
For a holistic view that includes opening jumps - critical in 24-hour futures markets - the Yang-Zhang estimator is the "minimum variance" unbiased estimator. It combines the overnight volatility (Close-to-Open) with the open-to-close Rogers-Satchell volatility.

$$\\sigma_{YZ}^2 = \\sigma_{Overnight}^2 + k \\cdot \\sigma_{OpenClose}^2 + (1-k) \\cdot \\sigma_{RS}^2$$
where $k$ is a weighting constant minimizing variance.4

2.2 Insights from Kaggle: The "Realized Volatility" Paradigm
In the Optiver Realized Volatility Prediction competition, the target variable was the "Realized Volatility" (RV), defined as the square root of the sum of squared log returns over a fixed window.5

$$RV = \\sqrt{\\sum_{t} r_{t}^2}$$

While Optiver provided high-frequency tick data, the winning solutions highlighted a crucial insight applicable to OHLCV data: the decay structure of volatility is a powerful predictor. Top competitors, including the 2nd place solution, utilized realized volatility calculated over different time subsamples to capture the acceleration of variance.1
Feature Implementation Strategy:
To mimic this on OHLCV data, we construct a "sub-grid" volatility feature set. If the base model operates on hourly bars, we calculate the realized volatility of the constituent 5-minute bars. This captures the "roughness" of the price path within the primary timeframe.
Table 1: Volatility Feature Cluster

Feature Name
Algorithm / Source
Rationale
Vol_Parkinson_20
Parkinson (1980)
Efficiency in zero-drift regimes; captures range expansion.
Vol_RogersSatchell_20
Rogers-Satchell
Robust to trending markets; isolates noise from drift.
Vol_YangZhang_20
Yang-Zhang
Captures overnight gap risk, critical for CL futures.
Vol_Ratio_PK_CC
\\sigma_{Parkinson} / \\sigma_{CloseClose}
High ratio indicates "churn" without net progress (indecision).
RV_Decay_Linear
Weighted sum of r^2
Emphasizes recent volatility; responsive to regime shifts.
Vol_Of_Vol
StdDev of Rolling Volatility
"Vol of Vol" is a primary driver of option pricing and breakout risk.

3. Estimating Liquidity: The Invisible Barrier
Market microstructure theory posits that volatility is inversely related to liquidity. When the limit order book (LOB) is deep, large market orders are absorbed with minimal price impact. When liquidity evaporates, even small orders can trigger large price displacements - a breakout. Since we are restricted to OHLCV data, we must employ low-frequency proxies to estimate the effective bid-ask spread and the depth of the market.

3.1 The Corwin-Schultz Spread Estimator
The Corwin-Schultz (2012) estimator is a structural breakthrough that allows the estimation of the bid-ask spread using only High and Low prices.7 It relies on the arbitrage relationship between the High/Low range of two consecutive days versus the High/Low range of the single period covering both days.
Theoretical Basis: The daily High is almost certainly a buy-initiated trade (at the Ask), and the Low is a sell-initiated trade (at the Bid). Therefore, the observed range reflects both the fundamental volatility of the asset and the width of the spread. By comparing the range of a 2-day period with the sum of the ranges of the two individual days, one can algebraically isolate the spread component.9
Derivation of Features: The estimator utilizes the following structural variables 10:
Beta (beta): The sum of squared log High/Low ratios for two consecutive periods.
$$\\beta = \\left[\\ln\\left(\\frac{H_t}{L_t}\\right)\\right]^2 + \\left[\\ln\\left(\\frac{H_{t+1}}{L_{t+1}}\\right)\\right]^2$$
Gamma (gamma): The squared log High/Low ratio for the single period covering both intervals.
$$\\gamma = \\left[\\ln\\left(\\frac{\\max(H_t, H_{t+1})}{\\min(L_t, L_{t+1})}\\right)\\right]^2$$
Alpha (alpha): Derived to isolate the spread.
$$\\alpha = \\frac{\\sqrt{2\\beta} - \\sqrt{\\beta}}{3 - 2\\sqrt{2}} - \\sqrt{\\frac{\\gamma}{3 - 2\\sqrt{2}}}$$
Spread Estimate (S):
$$S_{CS} = \\frac{2(e^\\alpha - 1)}{1 + e^\\alpha}$$
Implementation Nuances: The Corwin-Schultz estimator is known to produce negative spread estimates when the 2-day range is paradoxically smaller than the component ranges (a rare microstructure anomaly). These negative values should be set to zero or treated as a distinct "high noise" regime indicator.11 Furthermore, the derived alpha term itself serves as a feature, often signaling the "height" of the variance relative to the "width" of the spread.

3.2 The Amihud Illiquidity Ratio
The Amihud (2002) ratio is the standard academic proxy for price impact, measuring the absolute return generated per unit of volume.13

$$ILLIQ_t = \\frac{|R_t|}{Vol_t \\times P_t}$$
For Crude Oil futures, utilizing Dollar Volume (Vol_t x P_t) is crucial over simple contract volume to account for the changing notional value of the contract over long backtests (e.g., CL at $40 vs $100).
Feature Engineering:
Amihud_Term_Structure: Calculate ILLIQ over rolling windows (1-day, 5-day, 20-day). A divergence where the 1-day Amihud spikes above the 20-day average suggests a sudden withdrawal of liquidity, often preceding a volatility event.
Cost_of_Vol: The interaction term Vol_RS x ILLIQ. This feature highlights periods where price movement is both fast and expensive - a characteristic of stop-run cascades and forced liquidations.

4. Flow Toxicity and Information Asymmetry
Beyond the cost of trading (liquidity), the nature of the trading flow is a critical predictor. "Toxic" flow refers to order flow driven by informed traders who possess private information. Market makers, detecting this toxicity, widen spreads and retreat, catalyzing volatility.

4.1 The Roll Measure (Covariance of Changes)
Roll (1984) established that in an efficient market, the effective bid-ask spread induces negative serial covariance in price changes (the "bid-ask bounce").15

$$Spread_{Roll} = 2 \\sqrt{-\\text{Cov}(\\Delta P_t, \\Delta P_{t-1})}$$
The Information Signal: When the covariance term inside the square root is positive, the model is mathematically undefined in its original context. However, for a feature generation strategy, this "failure" is a powerful signal. A positive serial covariance implies that momentum (persistence) is overwhelming the bid-ask bounce. This suggests that trades are occurring sequentially on the same side of the book (e.g., buy, buy, buy), indicating a strong directional order flow or "splitting" of large institutional parent orders.17
Feature Construction:
Roll_Covariance_Raw: The raw covariance value over a rolling window (e.g., 20 bars).
Roll_Impact_Indicator: A binary feature {0, 1} indicating if the covariance is positive.
Roll_Spread_Magnitude: The computed spread when covariance is negative, set to 0 when positive.

4.2 The Kyle's Lambda Proxy
Based on Kyle (1985), we can approximate the slope of the aggregate supply curve (Lambda) using the ratio of absolute returns to the square root of volume. This proxies the "elasticity" of the price to volume flow.

$$\\lambda_{proxy} = \\frac{|R_t|}{\\sqrt{Vol_t}}$$
High values of lambda indicate that small volumes are moving the price significantly - a fragile market structure prone to "air pocket" gaps.

5. Structural Dynamics: Fractals and Entropy
To predict a breakout, one must detect the transition from a mean-reverting (anti-persistent) regime to a trending (persistent) regime. Features derived from fractal geometry and information theory are uniquely suited to quantify this "memory" and "complexity" of the time series.

5.1 Detrended Fluctuation Analysis (DFA)
While the Hurst exponent is the standard measure of long-term memory, the classic Rescaled Range (R/S) analysis is sensitive to short-term trends and non-stationarity. Detrended Fluctuation Analysis (DFA) is the robust alternative favored in econophysics and advanced signal processing.19
Algorithm Step-by-Step:
Integration: Compute the cumulative sum of the mean-centered series:
$$y(k) = \\sum_{i=1}^{k} [x(i) - \\bar{x}]$$
Windowing: Divide the profile y(k) into non-overlapping boxes of equal length n.
Detrending: In each box, fit a polynomial trend (usually linear, DFA-1) y_n(k) and calculate the residuals.21
$$Y(k) = y(k) - y_n(k)$$
Fluctuation Calculation: Compute the root-mean-square (RMS) fluctuation for the scale n:
$$F(n) = \\sqrt{\\frac{1}{N}\\sum^2}$$
Scaling Law: The slope alpha of the linear regression of log(F(n)) vs log(n) estimates the Hurst exponent.
Interpretation for Features:
alpha approx 0.5: Random Walk (Brownian Motion).
alpha < 0.5: Mean Reverting (Anti-persistent). A value nearing 0 indicates a tightly bracketed "chop" market.
alpha > 0.5: Trending (Persistent). A rising alpha signals the onset of a breakout trend.
Feature Set:
DFA_Hurst_100: Rolling Hurst exponent estimated via DFA over a large window (100 bars) for statistical significance.
Hurst_Regime: Categorical feature: 0 (Reversion), 1 (Random), 2 (Trend).
Fractal_Dimension: Calculated as D = 2 - H. This offers a geometric interpretation of the price curve's "roughness".23

5.2 Shannon Entropy and Information Features
Shannon entropy measures the uncertainty or information content in a probability distribution. In financial time series, a drop in entropy often precedes a breakout. As the market "decides" on a direction, the distribution of returns collapses from a broad, messy distribution (high entropy) to a narrower, directional distribution (low entropy).25

$$H(X) = - \\sum_{i} P(x_i) \\log P(x_i)$$
Implementation Strategy:
To apply this to continuous price returns, the data must be discretized:
Binning: Create a histogram of log-returns over a rolling window (e.g., 50 bars) using a fixed number of bins (e.g., 10) or the Freedman-Diaconis rule.
Probability Estimation: Calculate P(x_i) as the relative frequency of returns in each bin.
Calculation: Compute the entropy sum.
Feature Set:
Entropy_Shannon: Rolling Shannon entropy.
Entropy_Ratio: Entropy_short / Entropy_long. A ratio < 1 suggests the recent price action is becoming more ordered (trending) relative to the longer-term history.
Permutation Entropy: A variant that analyzes the order of values (ordinal patterns) rather than their magnitude, highly effective for detecting algorithmic trading patterns that repeat specific sequences.28

6. Insights from Kaggle: Optiver and Jane Street
Forecasting competitions provide a testing ground where theoretical metrics are subjected to the rigors of practical predictivity. Winning solutions from the Optiver and Jane Street competitions offer specific feature engineering blueprints that can be adapted for Crude Oil.

6.1 Optiver Realized Volatility Prediction
The Optiver competition focused explicitly on predicting short-term volatility. Key insights derived from top solutions include:
Time-ID Aggregation: Top competitors like nyanpn used "nearest neighbor" aggregation features.1 They grouped data not just by time, but by similar market states. For a CL model, this implies creating features based on "similar volatility clusters" - e.g., "Average volatility of the last 10 periods where spread was > X".
Trade-to-Trade Correlation: The winning solutions utilized the correlation between trade size and price impact. Adapted to OHLCV, this supports the use of Volume-Price Correlation features (e.g., rolling correlation between Delta P and Volume) to detect if volume is confirming price action.29
Linearity of Volatility: A common finding was that volatility features benefit significantly from log-transformation. The distribution of volatility is log-normal; transforming features like log(Vol_Parkinson) assists LightGBM in finding splits in the long tail of the distribution.5

6.2 Jane Street Market Prediction
The Jane Street competition involved predicting a binary action (trade/no trade) based on a utility function, essentially a direction-volatility hybrid problem.
Utility-Based Features: The competition metric emphasized risk-adjusted return (Weight x Return). This validates the use of Sharpe-like features: Return_rolling / Volatility_rolling. A high rolling Sharpe ratio indicates a smooth trend, whereas a low one indicates chop.
Non-Stationarity Handling: Many participants used techniques to neutralize the drift in features over time. This supports the use of Fractional Differentiation (as proposed by Lopez de Prado) rather than simple differencing to make features stationary while preserving memory.31
Action Patterning: The "resp" (response) targets in Jane Street were forward-looking returns. Features that modeled the derivative of the trend (velocity and acceleration) were crucial. We implement this as the first and second differences of our structural features (e.g., Delta CS_Spread, Delta Delta Hurst).

7. Feature Interactions and "Golden Features"
In the realm of gradient boosting, explicit interaction terms can significantly accelerate model convergence, even though trees can theoretically learn them. These "Golden Features" represent canonical relationships in finance.

7.1 The Liquidity-Volatility Spiral
Market crises often follow a specific mechanics: Liquidity drops -> Volatility spikes -> Market Makers widen spreads -> Liquidity drops further. Capturing this feedback loop is vital.
Vol_Liquidity_Ratio: RealizedVolatility / Amihud_Illiquidity. This proxies the "efficiency" of price discovery. A high value means the market can sustain high volatility without breaking the liquidity structure.
Spread_to_Range: CS_Spread / (High - Low). This indicates what percentage of the daily range is consumed by the bid-ask spread. High values indicate a noise-dominated environment unsuited for breakout trading.

7.2 Efficiency and Noise Ratios
Price_Efficiency_Ratio (PER): Derived from Perry Kaufman's work, defined as the net price change divided by the sum of absolute price changes over a window N.
$$PER = \\frac{|C_t - C_{t-N}|}{\\sum_{i=0}^{N-1} |\\Delta C_{t-i}|}$$
Values close to 1 indicate a straight line (clean trend); values close to 0 indicate random noise. This is a powerful filter for LightGBM to distinguish between "fake" breakouts (noise) and "true" breakouts (signal).33
Fractal_Efficiency: Range / (Vol x sqrt(N)). A variation of the Hurst exponent used to normalize volatility by the timeframe.

8. Comprehensive Feature Pool Summary
The following table synthesizes the strategic clusters of features to be generated. This pool of 100-200 features provides the LightGBM model with a holographic view of the market state, covering magnitude, cost, structure, and complexity.
Table 2: Master Feature Pool Taxonomy
Feature Cluster
Core Metrics
Derivatives & Transformations
Rationale
Variance Dynamics
Parkinson, Rogers-Satchell, Yang-Zhang, Realized Vol (RV)
Ratio_PK_Close, Vol_of_Vol, RV_Decay, Gap_Contribution
Decomposes total variance into trend, noise, and jump components.
Liquidity Structure
Corwin-Schultz (CS), Amihud, Kyle's Lambda
CS_Spread_Exp, Amihud_Shock, Cost_of_Vol, CS_Alpha
Estimates the "friction" of the market; widening spreads predict dislocation.
Flow Toxicity
Roll Measure, VPIN (Proxy), Volume Imbalance
Roll_Impact, Roll_Trend_Strength, Vol_Z_Score
Identifies informed trading and one-sided order flow pressure.
Fractal Memory
DFA Hurst, Rescaled Range, Fractal Dimension
DFA_Regime, Fractal_Trend, Hurst_Slope
Distinguishes between mean-reverting chop and persistent trending regimes.
Information Entropy
Shannon Entropy, Approximate Entropy, Permutation Entropy
Entropy_Ratio, Complexity_Index
Signals phase transitions from chaotic noise to ordered trends.
Momentum & Signal
RSI, MACD, Bollinger Bands (Z-Score)
Vol_Adjusted_Momentum, VWAP_Dev, Efficiency_Ratio
Standard trend strength indicators normalized by dynamic volatility.
Temporal Cyclics
Hour, Minute, Day of Week
Sin_Time, Cos_Time, Session_Encoding
Captures seasonality of CL inventory reports and global trading sessions.

9. Implementation Strategy: Ensuring Structural Integrity
The generation of these features requires a rigorous data pipeline to ensure stationarity and prevent look-ahead bias, particularly when calculating complex structural metrics like DFA.

9.1 Handling Non-Stationarity: Fractional Differentiation
Standard integer differencing (e.g., Price_t - Price_{t-1}) makes a series stationary but erases the long-term memory essential for volatility prediction. Institutional strategies employ Fractional Differentiation (FracDiff), differentiating the series by a fractional order d (e.g., d=0.4).34
Strategy:
Apply Fixed-Width Window FracDiff to all raw price features before feeding them into the feature generation functions. This preserves the Hurst exponent and structural correlations while passing the Augmented Dickey-Fuller (ADF) test for stationarity.

9.2 Handling Class Imbalance
Volatility breakouts are, by definition, tail events. The dataset will be heavily imbalanced (many "normal" periods, few "breakouts").
Labeling: Use the Triple Barrier Method (Lopez de Prado). Set a vertical barrier (time limit) and two horizontal barriers (stop-loss and profit-take based on current volatility). A "breakout" is defined as touching the upper barrier first.
Model Weighting: Use LightGBM's scale_pos_weight parameter to penalize missed breakouts. Avoid SMOTE for time series as it destroys the temporal dependency of the structural features.36

9.3 Feature Selection and Recursive Elimination
With a pool of 200 features, multicollinearity is a risk, although LightGBM is relatively robust to it.
Recursive Feature Elimination (RFE): Train the model on the full set, compute gain-based feature importance, and iteratively prune the bottom 10% of features.
Drift Check: Explicitly test features for "covariate shift" between train and test sets (e.g., using an adversarial classifier). Features like raw Roll_Covariance may drift if market regimes change (e.g., electronic vs pit trading eras); rank-transforming these features can improve stability.

10. Conclusion
The prediction of volatility breakouts in Crude Oil is not a problem of finding a "magic" indicator, but of reconstructing the hidden state of the market from available data. This feature generation strategy transcends standard libraries by embedding the physics of the market into the data. By quantifying the friction of trading (Corwin-Schultz/Amihud), the intent of traders (Roll), and the memory of the price path (DFA/Entropy), we provide LightGBM with the structural context necessary to disentangle noise from the signal of an impending volatility event. This approach transforms raw OHLCV data into an institutional-grade information system, capable of supporting robust, high-sharpe trading strategies in the energy sector.
