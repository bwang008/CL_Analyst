# NIGHT_LOG — Autonomous Alpha Discovery

## Session Info
- **Date**: 2026-03-22 21:39 PDT
- **Dataset**: set_11 (macro-augmented, FRED + CFTC, causally safe)
- **GCS**: `gs://cltrainer-optuna-results/data/cl-5m_bk_set_11.parquet`
- **VM**: `optuna-runner-canary` (n2-highcpu-48, GCS prefix: `canary/`)
- **Parallel agent**: Running on separate worktree, VM `optuna-runner-overnight`, prefix `overnight_alpha/`

---

## Iteration 1: Clean Baseline Retrain
**Timestamp**: 2026-03-22 21:39 PDT

**Hypothesis**: No legitimate baseline exists — all prior models (EXP-017 through EXP-033) were trained on datasets with MACRO resample lookahead bias. set_11 is the first macro-augmented dataset that is also causally safe. Retraining with the exact same pipeline (no feature changes, no target changes) will establish the TRUE performance floor. The leaked ensemble3_3 (PF=4.01) was a hallucination; the real edge on clean data will be smaller — but if it exists at all, that's the signal we need.

**Code Changes**: None — pure retrain on set_11 with default canary config.

**Config**: ensemble4.json (TP=2.5 ATR, SL=1.5 ATR, consecutive_signal=2, threshold=0.60)

**Results**: 
- **PF**: 0.80 (193 trades, 35.2% win rate)
- **PnL**: -$7,206.27
- **Details**: The SHORT model produced 0 trades at the 0.60 threshold. The LONG model had an edge in 2022 (PF 1.31) and 2023 (PF 1.48) but degraded heavily in 2024 (PF 0.19) and 2025 (PF 0.33).

**Verdict: DISCARD**. The clean set_11 baseline is unprofitable. The leaked baseline (PF=4.01) was indeed a hallucination caused by lookahead bias. We now have a legitimate floor to beat.

---

## Iteration 2: Volume Z-Score + Relaxed Confirmation
**Timestamp**: 2026-03-22 23:59 PDT

**Hypothesis**: Standard momentum signals fail in choppy markets. A rolling Z-score of Volume over 12H (144 bars) detects institutional accumulation before price breaks out. Combined with interaction features (spread-adjusted momentum) and a lower `consecutive_signal_threshold` (1 instead of 2), the model will catch more early signals and improve the 35% win rate. Also, reducing the strategy probability threshold to 0.50 should allow the SHORT model to fire.

**Code Changes**: 
- Added `add_volume_zscore_cluster`, `add_interaction_cluster`, and `add_exhaustion_divergence_cluster` to `alpha_factory.py`.
- Registered `set_11b` in `data_processor.py`.
- Created `ensemble5_canary.json` with relaxed parameters.

**Config**: ensemble5_canary.json (threshold=0.50, consecutive_signal=1)

**Results**: 
- **Trades**: 19,527
- **PF**: 0.5238
- **WR**: 28.8%
- **PnL**: -$1,324,971.84

**Verdict: DISCARD**. Relaxing the threshold to 0.50 and consecutive_signal to 1 allowed the model to trade massive amounts of noise. The experimental volume Z-score features did not provide enough discriminatory power to offset the relaxed confirmation logic. Reverting experimental clusters to maintain code cleanliness.
## Iteration 3: Tighter Target (1.5x/0.75x, 12H)
**Timestamp**: 2026-03-23 01:52 PDT

**Hypothesis**: The standard 2x1 ATR barrier with a 24H horizon catches too much noise. A tighter `TARGET_TRIPLE_1.5x0.75_12H` produces higher quality labels by forcing the model to predict shorter-term momentum bursts rather than longer-term swings, reducing exposure to volatile market chops.

**Code Changes**: 
- Added `TARGET_TRIPLE_1.5x0.75_12H` targets to `data_processor.py` for both Long and Short.

**Config**: ensemble4.json (default baseline parameters)

**Results**: 
- **Trades**: 325 (LONG only, SHORT produced 0 trades)
- **PF**: 0.5066
- **WR**: 28.6%
- **PnL**: -$23,015.74

**Verdict: DISCARD**. The tighter 0.75x ATR stop loss is far too tight for the 5-minute market noise, resulting in an abysmal win rate of 28.6% and zero valid short trades at the 0.60 probability threshold. Squeezing the target barrier merely eliminated the actual price swings we need to capture. Reverting.

---

## Iteration 4: Spread-Adjusted Momentum + Wider TP
**Timestamp**: 2026-03-23 09:25 PDT

**Hypothesis**: High Corwin-Schultz (CS) bid-ask spread implies liquidity is thin and volatility is noise-driven, making momentum indicators (like RSI) unreliable. By engineering an interaction feature \`RSI × (1 - CORWIN_SPREAD_PCT)\`, we gracefully dampen momentum signals during illiquid, choppy periods, reducing false breakouts. Additionally, since the tighter TP (1.5x ATR) in Iteration 3 failed, we will test letting winners run with a much wider Take Profit (3.5 ATR) while keeping the standard 1.5 ATR Stop Loss.

**Code Changes**: 
- Added \`add_spread_momentum_interaction\` to \`alpha_factory.py\` to compute \`RSI_14 * (1 - Corwin_Spread)\`.
- Registered \`set_11c\` in \`data_processor.py\` to include the new interaction feature.
- Created `ensemble6_canary.json` with \`tp_atr_mult: 3.5\`.

**Config**: ensemble6_canary.json (tp_atr_mult=3.5, sl_atr_mult=1.5, threshold=0.60)

**Results**: *(pending)*
## Iteration 5: (pending)
