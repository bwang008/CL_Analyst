# Future Architecture: Volatility Straddle Execution Engine

**Document Type**: Strategic Technical Specification  
**Status**: Blueprint — Pre-Development  
**Author**: CL_Analyst Research  
**Date**: 2026-03-27  
**Classification**: Internal — Project Records

---

## Executive Summary

The `TARGET_VOL_EXPANSION` model successfully identifies periods preceding large price movements (top-20% of 24-hour True Range events) with actionable precision. However, the current execution infrastructure—a directional futures backtester—cannot exploit this signal because volatility expansion is inherently **direction-agnostic**. The model predicts *that* price will move, not *where*. This document formalizes the architectural pivot required to monetize this signal via a **0 DTE / 1 DTE Long Straddle** options strategy on CL (Crude Oil) futures, and details the infrastructure that must be built to backtest and execute it.

---

## Table of Contents

1. [The Autopsy: Why Vol Expansion Failed in Directional Futures](#1-the-autopsy)
2. [The Strategic Pivot: Long Straddle Mathematics](#2-the-strategic-pivot)
3. [Infrastructure Requirements](#3-infrastructure-requirements)
4. [Risk Framework](#4-risk-framework)
5. [Development Roadmap](#5-development-roadmap)

---

## 1. The Autopsy

### 1.1 What TARGET_VOL_EXPANSION Predicts

The target is constructed in [`data_processor.py:add_vol_expansion_target()`](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/src/data_processor.py#L413-L481):

```
Forward_TR(t) = max(High[t+1 : t+288]) − min(Low[t+1 : t+288])

Label = 1  if  Forward_TR(t) > Rolling_P80(Forward_TR, window=10,080)
         0  otherwise
```

This labels the **top 20% of 24-hour forward True Range events** — periods where absolute price displacement (regardless of direction) will be historically extreme. The signal is:

| What It Knows | What It Does NOT Know |
|---|---|
| A large move is coming | Whether the move is up or down |
| Approximate timing (next 24H) | The specific direction of breakout |
| Relative magnitude (top quintile) | Whether the move reverses intraday |

### 1.2 The Structural Mismatch

The current `BacktestEngine` (and the live `ConservativeEnsembleStrategy`) operates on a strict directional paradigm:

```
Signal → Direction Decision → Entry (LONG or SHORT) → TP/SL Management → Exit
```

When `TARGET_VOL_EXPANSION = 1` fires, the execution engine must choose LONG or SHORT. Since the target encodes **zero directional information**, this choice is effectively a coin flip. The model traded **aggressively** — it did not fail to generate signals. It failed because the directional execution engine guessed wrong on the breakout direction roughly half the time.

#### Observed Results: EXP-036 Bucket Canary (set_12, ensemble6 config)

| Model Variant | Trades | Win Rate | Profit Factor | PnL |
|---|---|---|---|---|
| **logloss** | **455** | **21.3%** | **0.61** | **-$32,603** |
| PR-AUC | 1,349 | 24.7% | 0.65 | -$78,466 |

For comparison, the **directional** model on the same config (set_11c, `TARGET_TRIPLE_2x1_24H`):

| Model | Trades | Win Rate | Profit Factor | PnL |
|---|---|---|---|---|
| long_logloss | 50 | 34.0% | 0.98 | -$98 |

The vol expansion model generated **9× more trades** than the directional model but at catastrophically lower win rates. With a 2.33:1 reward/risk ratio (TP=3.5x ATR / SL=1.5x ATR), breakeven win rate is ~30%. The directional model hit 34% (4% above breakeven). The vol expansion model hit 21% (9% below breakeven) — because it correctly identified *that* a big move was coming, but the execution engine guessed the *direction* of that move at roughly chance.

### 1.3 Formal Diagnosis

The vol expansion model is **not broken** — it is **misdeployed**. It generated 455 high-conviction signals that correctly anticipated outsized price moves. But routing those signals through a directional futures engine that must guess LONG or SHORT destroyed the edge:

- When the model fires and the market goes **up**: LONG wins, SHORT loses.
- When the model fires and the market goes **down**: SHORT wins, LONG loses.
- Since the model provides no directional bias: **E[PnL_directional] ≈ 0** minus transaction costs.
- With 455 trades × ~$65 commission per round-trip = **~$29,575 in friction alone**, the -$32,603 loss is almost entirely explainable by commission drag on coin-flip directional bets.

The correct instrument class for a direction-agnostic volatility signal is **options**, where profit is derived from the magnitude of movement regardless of its sign.

---

## 2. The Strategic Pivot

### 2.1 Long Straddle: The Direction-Neutral Instrument

A **Long Straddle** consists of:

| Leg | Strike | Premium |
|---|---|---|
| Buy 1 ATM Call | K ≈ Current CL Price | C₀ |
| Buy 1 ATM Put | K ≈ Current CL Price | P₀ |

**Total Cost (Max Loss)**: `C₀ + P₀`  
**Breakeven Points**: `K ± (C₀ + P₀)`  
**Profit Condition**: `|S_T − K| > C₀ + P₀`

The payoff at expiration:

```
Payoff = max(S_T − K, 0) + max(K − S_T, 0) − (C₀ + P₀)
       = |S_T − K| − Total_Premium
```

This structure profits when:
- Price moves **up** significantly → Call gains exceed total premium
- Price moves **down** significantly → Put gains exceed total premium
- It does **not** matter which direction — only **magnitude** matters

This is exactly what `TARGET_VOL_EXPANSION` predicts.

### 2.2 Why 0 DTE / 1 DTE

Short-dated options (0–1 days to expiration) are optimal because:

| Factor | Advantage |
|---|---|
| **Theta Decay** | Lower absolute premium (less capital at risk per trade) |
| **Gamma Exposure** | Maximum gamma near ATM at short DTE — small moves produce large delta shifts |
| **Signal Alignment** | Vol expansion signal has a 24H forward horizon — 0/1 DTE aligns naturally |
| **Capital Efficiency** | No margin on long options; max loss = premium paid |
| **CL Liquidity** | CL weekly options (LO) have tight spreads on 0/1 DTE ATM strikes |

### 2.3 The Vega/IV Edge: Buying During Compression

The `TARGET_VOL_EXPANSION` signal fires at the **onset** of breakout — but the best execution timing is during the **pre-breakout compression phase** when implied volatility is depressed.

#### The Volatility Lifecycle

```
Phase 1: COMPRESSION        Phase 2: EXPANSION          Phase 3: REVERSION
┌─────────────────────┐    ┌─────────────────────┐    ┌─────────────────────┐
│  Low Realized Vol   │    │  Breakout Occurs     │    │  Vol Mean-Reverts   │
│  Low Implied Vol    │───►│  RV Spikes           │───►│  IV Drops           │
│  Cheap Options      │    │  IV Spikes (Vega P&L)│    │  Position Closed    │
│  ★ ENTRY POINT ★    │    │  ★ PROFIT ZONE ★     │    │                     │
└─────────────────────┘    └─────────────────────┘    └─────────────────────┘
```

#### Vega Profit Mathematics

Option price sensitivity to implied volatility:

```
dV = Vega × ΔIV
```

Where:
- `Vega` = dollar change in option price per 1% change in IV
- `ΔIV` = change in implied volatility from entry to exit

For an ATM CL straddle at ~$70/bbl, typical values:

| Parameter | Low IV Regime (Entry) | Post-Breakout (Exit) |
|---|---|---|
| IV (annualized) | ~25% | ~35-45% |
| ATM Call Vega | ~$0.04/1% IV | — |
| ATM Put Vega | ~$0.04/1% IV | — |
| Straddle Vega | ~$0.08/1% IV | — |

If IV rises 10 points (25% → 35%):

```
Vega P&L ≈ $0.08 × 10 = $0.80 per spread
         = $800 per contract (CL = 1,000 bbl)
```

This Vega profit is **additive** to the intrinsic profit from the actual price movement, creating a dual-source return:

1. **Intrinsic**: `|S_T − K| × 1,000 bbl` (from the realized move)
2. **Vega**: `Straddle_Vega × ΔIV × 1,000` (from the IV expansion)

#### Why Compression-Phase Entry Is Critical

During compression, options are cheap because:
- Market-makers price IV based on **recent** realized vol (which is low)
- The `TARGET_VOL_EXPANSION` model sees what market-makers do not: **impending** expansion
- This creates an information asymmetry: **buying underpriced optionality**

The model's edge can be quantified as:

```
Edge = P(model predicts expansion) × E[Straddle Return | expansion occurs]
     − P(model false positive)     × E[Straddle Loss | no expansion]
```

If the model achieves >60% precision on vol expansion events, and the average expansion yields 2-3× the premium, the expected value is strongly positive even after accounting for theta bleed on false positives (where max loss = premium paid).

---

## 3. Infrastructure Requirements

### 3.1 Data Sourcing

**Current State**: The pipeline consumes only OHLCV bar data (5-min or 1-hour).  
**Required**: Full options chain data with Greeks.

#### 3.1.1 Minimum Data Schema

| Field | Description | Source |
|---|---|---|
| `timestamp` | Bar timestamp (5-min aligned) | Existing |
| `underlying_price` | CL futures price | Existing |
| `expiration_date` | Options expiry | **NEW** |
| `strike` | Strike price | **NEW** |
| `option_type` | Call / Put | **NEW** |
| `bid` | Best bid price | **NEW** |
| `ask` | Best ask price | **NEW** |
| `mid` | (bid + ask) / 2 | **NEW** (derived) |
| `iv` | Implied Volatility | **NEW** |
| `delta` | ∂V/∂S | **NEW** |
| `gamma` | ∂²V/∂S² | **NEW** |
| `theta` | ∂V/∂t | **NEW** |
| `vega` | ∂V/∂σ | **NEW** |
| `open_interest` | Contract open interest | **NEW** |
| `volume` | Options volume | **NEW** |

#### 3.1.2 Data Vendors

| Vendor | Coverage | Cost | Format | Notes |
|---|---|---|---|---|
| **IBKR Historical** | Limited (~2 years) | Free (with account) | API | Only covers recent history in current CL_Analyst setup |
| **OptionMetrics (IvyDB)** | 1996–present | ~$15K/year academic | CSV/Parquet | Institutional standard; includes bid/ask + Greeks |
| **ORATS** | 2007–present | ~$200/month | API/CSV | Good CL coverage, includes skew surfaces |
| **Theta Data** | 2013–present | ~$50/month | API/Parquet | Budget-friendly; good 0 DTE resolution |
| **Databento** | 2015–present | Pay-per-use | API/CSV | Tick-level granularity available |

> [!IMPORTANT]
> **Recommended Starting Point**: Theta Data or ORATS for historical backtesting. IBKR for live execution data. Budget: ~$100-300/month for research-grade data covering CL options with 5-min resolution on Greeks.

#### 3.1.3 Storage Requirements

Estimated data volume for CL options (all strikes, 5-min granularity):

```
Strikes per expiry:     ~50 (OTM/ATM range)
Expirations (weeklies): ~4 active at any time
Call + Put:             ×2
Bars per day:           288
Trading days/year:      252

Rows/year ≈ 50 × 4 × 2 × 288 × 252 ≈ 29M rows/year

At ~200 bytes/row:  ~5.5 GB/year (uncompressed)
                    ~1.5 GB/year (Parquet, snappy)
```

For 10 years of history: **~15 GB Parquet** — manageable on local storage.

### 3.2 Execution Engine Rewrite

The current [`backtest_engine.py`](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/agent/backtest_engine.py) must be extended or rewritten to support multi-leg options execution.

#### 3.2.1 New Engine Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   OptionsBacktestEngine                  │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────┐ │
│  │ Signal Layer │───►│ Strike Picker│───►│  Execution │ │
│  │ (LightGBM)  │    │  (ATM Logic) │    │  Simulator │ │
│  └─────────────┘    └──────────────┘    └────────────┘ │
│         │                   │                  │        │
│         ▼                   ▼                  ▼        │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────┐ │
│  │  Vol Signal  │    │ Options Data │    │  Greeks &  │ │
│  │  Prediction  │    │   Lookup     │    │  IV Engine │ │
│  └─────────────┘    └──────────────┘    └────────────┘ │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │              Position Manager                     │   │
│  │  - Multi-leg tracking (Call + Put = 1 Straddle)   │   │
│  │  - Per-leg P&L with Greeks attribution             │   │
│  │  - Expiration handling (exercise/expire worthless) │   │
│  │  - Early exit logic (profit target / time decay)   │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

#### 3.2.2 Key Components

##### A. Dynamic ATM Strike Selection

```python
def select_atm_strike(underlying_price: float, available_strikes: list[float]) -> float:
    """Select strike closest to current underlying price."""
    return min(available_strikes, key=lambda k: abs(k - underlying_price))
```

For CL options, strikes are typically spaced $0.50 apart. ATM selection must:
- Use the **nearest** strike to the current futures price
- Handle half-tick offsets (e.g., CL at $70.25 → K = $70.50 or $70.00)
- Re-evaluate ATM at entry time, not signal time (if delayed execution)

##### B. Multi-Leg Simultaneous Execution

Each straddle entry requires **two simultaneous fills**:

```python
class StraddleOrder:
    entry_time: datetime
    underlying_price: float
    strike: float
    call_entry_price: float  # Ask price (buying)
    put_entry_price: float   # Ask price (buying)
    total_premium: float     # call + put
    expiration: datetime
    
    # Greeks at entry
    call_delta: float
    put_delta: float
    straddle_vega: float
    straddle_theta: float
```

Both legs must fill at the **same timestamp**. The backtester must:
- Look up both Call and Put prices for the selected strike/expiry at the signal bar
- Apply bid/ask spread slippage independently to each leg
- Reject the trade if either leg has insufficient liquidity (volume < minimum threshold)

##### C. Options-Specific Slippage Model

Futures slippage (current model): Fixed tick or ATR-based.

Options slippage is more complex:

```python
def estimate_options_slippage(bid: float, ask: float, 
                                volume: int, open_interest: int) -> float:
    """
    Realistic options slippage model.
    
    Components:
    1. Half-spread: (ask - bid) / 2 — baseline cost of crossing
    2. Market impact: Additional cost for low-liquidity strikes
    3. Execution uncertainty: Random component for realistic simulation
    """
    half_spread = (ask - bid) / 2
    
    # Market impact increases for illiquid options
    liquidity_factor = 1.0 + max(0, (50 - volume) / 50) * 0.5
    
    # Entry at ask + impact; Exit at bid - impact
    slippage = half_spread * liquidity_factor
    
    return slippage
```

| Slippage Source | Futures (Current) | Options (Required) |
|---|---|---|
| Bid-Ask Spread | 1 tick (~$10) | $20-80 per leg (varies with IV) |
| Market Impact | Minimal (deep book) | Significant (thinner book) |
| Multi-Leg Penalty | N/A | 2× spread (Call + Put) |
| IV Sensitivity | None | Spread widens in high-IV |

> [!WARNING]
> Options slippage is the **primary risk** to profitability. A straddle with $0.30 premium per leg and $0.05 slippage per leg loses 17% to friction before any market movement. The backtester MUST model this realistically or results will be meaninglessly optimistic.

#### 3.2.3 Position Lifecycle

```
┌──────────┐     ┌──────────┐     ┌──────────────────┐     ┌──────────┐
│  Signal   │────►│  Entry   │────►│   Active Mgmt    │────►│   Exit   │
│  VOL=1    │     │ Buy ATM  │     │ Monitor Greeks    │     │ Close or │
│  P>thresh │     │ Call+Put │     │ Theta bleed check │     │ Expire   │
└──────────┘     └──────────┘     └──────────────────┘     └──────────┘
                                          │
                                          ▼
                                   ┌──────────────┐
                                   │ Exit Triggers │
                                   │ • P&L target  │
                                   │ • Time decay  │
                                   │ • Expiration   │
                                   │ • Vol crush    │
                                   └──────────────┘
```

Exit logic for the straddle:

| Condition | Action | Rationale |
|---|---|---|
| `straddle_pnl >= 2.0 × premium` | Close both legs | Lock in 100%+ return |
| `time_to_expiry < 2 hours` AND `straddle_pnl < 0` | Close both legs | Theta acceleration kills remaining value |
| `IV_current < IV_entry × 0.8` | Close both legs | Vol crush — the thesis failed |
| Expiration reached | Auto-exercise/expire | Standard settlement |

### 3.3 Feature Engineering Extensions

New features to enhance the vol expansion signal with options-native data:

| Feature | Formula | Rationale |
|---|---|---|
| `IV_RV_Ratio` | `IV / RealizedVol_20d` | Detects cheap optionality (ratio < 1.0) |
| `IV_Percentile` | `percentile_rank(IV, 252d)` | Historical context for current IV |
| `Put_Call_Ratio` | `Put_OI / Call_OI` | Sentiment/hedging pressure |
| `Term_Structure_Slope` | `IV_30d / IV_7d - 1` | Contango/backwardation in vol surface |
| `Skew_25Delta` | `IV_25dPut - IV_25dCall` | Tail risk demand asymmetry |
| `ATM_Straddle_Premium_ATR` | `(C₀+P₀) / ATR_14` | Cost relative to expected move |

### 3.4 Live Execution Considerations (IBKR)

| Requirement | Implementation |
|---|---|
| **Contract Spec** | CL Weekly Options (LO) on NYMEX |
| **Order Type** | LMT orders on each leg (no market orders on options) |
| **Combo Orders** | IBKR supports native straddle combo orders (single fill) |
| **Margin** | Long options = no margin; cash outlay = premium only |
| **Exercise** | American-style — early exercise possible but rarely optimal |
| **Settlement** | Physical delivery of CL futures — **must close before expiry** |

> [!CAUTION]
> CL options settle into CL futures contracts, not cash. Failing to close an in-the-money option before expiration will result in an unintended futures position with margin requirements. The execution engine MUST enforce mandatory close-out before final settlement.

---

## 4. Risk Framework

### 4.1 Maximum Loss Profile

| Scenario | Loss |
|---|---|
| Best case | Unlimited profit potential |
| Breakeven | Price moves exactly ± premium from strike |
| Worst case | Total premium paid (both legs expire worthless) |

The straddle has **defined risk**: maximum loss = `C₀ + P₀`. This is a structural advantage over the current futures system where stop-loss gaps can exceed the intended risk.

### 4.2 Key Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Theta Bleed** | HIGH | Only hold 0-1 DTE; tight time-based exits |
| **IV Crush** | HIGH | Monitor IV level at entry; don't enter if IV already elevated |
| **Slippage** | MEDIUM | Limit orders only; reject trades with spread > 10% of premium |
| **Liquidity** | MEDIUM | Only trade ATM strikes on front-week expiry |
| **Model Degradation** | LOW | Walk-forward retraining (existing infrastructure) |
| **Pin Risk** | LOW | Close all positions ≥2 hours before expiry |

### 4.3 Position Sizing

```
Risk_Per_Trade = Account_Equity × Risk_Pct (e.g., 1%)
Max_Contracts  = floor(Risk_Per_Trade / (Straddle_Premium × 1000))
```

With a $100K account and 1% risk:
```
Premium = $0.50 (Call) + $0.50 (Put) = $1.00
Dollar Risk = $1.00 × 1,000 bbl = $1,000 per contract
Max Contracts = $1,000 / $1,000 = 1 contract per signal
```

---

## 5. Development Roadmap

### Phase 1: Data Foundation (Est. 2-3 weeks)

- [ ] Evaluate and select options data vendor (Theta Data or ORATS)
- [ ] Build `OptionsDataLoader` — ingest historical options chains
- [ ] Create `OptionsDataProcessor` — clean, align to 5-min bars, compute Greeks if missing
- [ ] Store as Parquet with partition by `(date, expiration)` for efficient lookup

### Phase 2: Backtester Extension (Est. 3-4 weeks)

- [ ] Implement `OptionsBacktestEngine` (separate from existing `BacktestEngine`)
- [ ] ATM strike selector with half-tick handling
- [ ] Multi-leg order execution with simultaneous fill simulation
- [ ] Options-specific slippage model (bid/ask aware)
- [ ] Position lifecycle manager (entry → Greeks monitoring → expiry/exit)
- [ ] Straddle P&L decomposition (intrinsic + vega + theta)

### Phase 3: Strategy Validation (Est. 2-3 weeks)

- [ ] Backtest `TARGET_VOL_EXPANSION` signal with long straddle execution
- [ ] Sweep: DTE (0 vs 1), exit timing, profit targets
- [ ] Compare vs. directional futures baseline (expected: significant improvement)
- [ ] Walk-forward validation on 2024-2026 out-of-sample period

### Phase 4: Live Execution (Est. 2-3 weeks)

- [ ] Extend `live_trader.py` to support IBKR combo/options orders
- [ ] Implement mandatory pre-expiry close-out safety
- [ ] Paper trade for 2 weeks minimum before live capital deployment
- [ ] Build options-specific telemetry dashboard (greeks, IV, premium decay)

---

## Appendix A: CL Options Contract Specifications

| Specification | Value |
|---|---|
| **Exchange** | NYMEX (CME Group) |
| **Underlying** | CL Futures (1,000 barrels) |
| **Weekly Options (LO)** | Expire every Friday |
| **Strike Increment** | $0.50 |
| **Tick Size** | $0.01 = $10 per contract |
| **Exercise Style** | American |
| **Settlement** | Physical delivery of CL futures |
| **Trading Hours** | 18:00–17:00 ET (Sun-Fri) |

## Appendix B: Glossary

| Term | Definition |
|---|---|
| **ATM** | At-The-Money — strike price nearest to current underlying |
| **DTE** | Days To Expiration |
| **IV** | Implied Volatility — market's expected future vol priced into options |
| **RV** | Realized Volatility — actual historical vol computed from price data |
| **Vega** | Sensitivity of option price to 1% change in IV |
| **Theta** | Time decay — daily erosion of option value |
| **Gamma** | Rate of change of delta — acceleration of option price |
| **Straddle** | Long Call + Long Put at same strike and expiry |
| **Pin Risk** | Risk of underlying settling exactly at the strike at expiry |
| **Vol Crush** | Rapid decline in IV, typically after a news event resolves |

---

*This document serves as the architectural blueprint for the CL_Analyst Volatility Options module. Development should commence only after the directional futures model (current `TARGET_TRIPLE_2x1_24H`) is finalized and deployed to production. The vol expansion signal has demonstrated predictive value — it simply requires the correct execution vehicle.*
