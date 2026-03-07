"""
Validate Parity — Offline replay of shadow-logged data.

Loads a shadow log Parquet file and compares logged predictions against
offline model inference. Operates in two modes:

  Mode A (Feature Replay): If the shadow log contains logged feature columns,
  use them directly for offline inference. This validates that feature
  computation is deterministic.

  Mode B (Full Rebuild): If enough OHLCV history is available (10,000+ rows),
  rebuild features from scratch via AlphaFactory and compare both the
  features AND predictions. This is the gold-standard parity test.

If predictions match -> Pipeline Parity (the code is correct).
If predictions diverge -> Pipeline Bug (live vs. batch feature mismatch).

Usage:
    python scripts/validate_parity.py
    python scripts/validate_parity.py --file data/processed/mock_shadow_log.parquet
    python scripts/validate_parity.py --file data/processed/live_shadow_log.parquet --model-dir models/registry/EXP-017_S_Ultimate
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.features.alpha_factory import AlphaFactory
from src.LGBMLearner import LGBMLearner


# AlphaFactory windows (must match training & live pipeline)
_ALPHA_WINDOWS = [864, 2016, 4032, 10080]

# Minimum rows needed for a full feature rebuild
_MIN_REBUILD_ROWS = _ALPHA_WINDOWS[-1] + 500


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-x))


def find_default_model_dir() -> Path | None:
    """Auto-detect the first model in the registry."""
    from src.data_paths import get_models_root
    registry = get_models_root() / "registry"
    if not registry.exists():
        return None
    for d in sorted(registry.iterdir()):
        if (d / "final_model.pkl").exists():
            return d
    return None


def load_model(model_dir: Path) -> LGBMLearner:
    """Load a trained LGBMLearner from disk."""
    model_path = model_dir / "final_model.pkl"
    if not model_path.exists():
        print(f"ERROR: Model not found: {model_path}")
        sys.exit(1)
    learner = LGBMLearner.__new__(LGBMLearner)
    learner.load(str(model_path))
    print(f"Model loaded: {model_path} ({len(learner.feature_names)} features)")
    return learner


def rebuild_features(ohlcv: pd.DataFrame, feature_names: list[str]) -> pd.DataFrame | None:
    """Run the offline feature pipeline on OHLCV data.

    Replicates the exact same steps as build_live_features in live_trader.py.
    Returns None if insufficient rows for AlphaFactory warmup.
    """
    df = ohlcv.copy()

    if len(df) < _MIN_REBUILD_ROWS:
        return None

    # Ensure DateTime index
    if "timestamp" in df.columns:
        df["DateTime"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("DateTime")
    elif not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    # 1. Cyclical time features
    minutes = df.index.hour * 60 + df.index.minute
    df["Time_Sin"] = np.sin(2 * np.pi * minutes / 1440)
    df["Time_Cos"] = np.cos(2 * np.pi * minutes / 1440)

    # 2. AlphaFactory
    df = AlphaFactory(df).add_all_features(
        windows=_ALPHA_WINDOWS,
        include_momentum=True,
        include_macro=True,
    )

    # 3. ATR_14
    if "ATR_14" not in df.columns:
        import pandas_ta as ta  # noqa: F401
        df["ATR_14"] = df.ta.atr(length=14)

    # 4. Volume_Log
    df["Volume_Log"] = np.log1p(df["Volume"])

    # 5. Clean NaN/inf
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df.ffill(inplace=True)
    df.bfill(inplace=True)
    df.fillna(0, inplace=True)

    # 6. Select model features
    missing = set(feature_names) - set(df.columns)
    if missing:
        print(f"WARNING: {len(missing)} features missing from rebuilt data: {sorted(missing)[:10]}...")

    available = [c for c in feature_names if c in df.columns]
    return df[available]


def run_inference(learner: LGBMLearner, features_df: pd.DataFrame) -> np.ndarray:
    """Run model inference on a features DataFrame and return probabilities."""
    probs = []
    for i in range(len(features_df)):
        row = features_df.iloc[[i]]
        raw_pred = learner.model.predict(row)
        raw_val = float(np.asarray(raw_pred).ravel()[0])
        if raw_val < 0 or raw_val > 1:
            prob = _sigmoid(raw_val)
        else:
            prob = raw_val
        probs.append(prob)
    return np.array(probs)


# ═══════════════════════════════════════════════════════════════════
# Report printing
# ═══════════════════════════════════════════════════════════════════

def _print_trade_simulation(
    w: int,
    probs: np.ndarray,
    closes: np.ndarray,
    atrs: np.ndarray,
    timestamps: np.ndarray | None,
    direction: str = "LONG",
    threshold: float = 0.70,
    tp_mult: float = 5.0,
    sl_mult: float = 1.0,
) -> None:
    """Simulate bracket trades and print win/loss stats."""
    print()
    print("=" * w)
    print("SIMULATED TRADE RESULTS (bracket orders)".center(w))
    print("=" * w)
    print(f"  Direction:       {direction}")
    print(f"  Entry threshold: {threshold}")
    print(f"  TP multiplier:   {tp_mult}x ATR")
    print(f"  SL multiplier:   {sl_mult}x ATR")
    print("-" * w)

    trades = []
    i = 0
    n = len(probs)
    while i < n:
        if probs[i] >= threshold and not np.isnan(atrs[i]) and atrs[i] > 0:
            entry_price = closes[i]
            atr = atrs[i]
            if direction == "LONG":
                tp_price = entry_price + tp_mult * atr
                sl_price = entry_price - sl_mult * atr
            else:
                tp_price = entry_price - tp_mult * atr
                sl_price = entry_price + sl_mult * atr

            # Walk forward to find exit
            exit_price = None
            exit_reason = "TIMEOUT"
            exit_bar = min(i + 288, n - 1)  # 24h max hold
            for j in range(i + 1, min(i + 289, n)):
                if direction == "LONG":
                    if closes[j] >= tp_price:
                        exit_price = tp_price
                        exit_reason = "TP"
                        exit_bar = j
                        break
                    if closes[j] <= sl_price:
                        exit_price = sl_price
                        exit_reason = "SL"
                        exit_bar = j
                        break
                else:
                    if closes[j] <= tp_price:
                        exit_price = tp_price
                        exit_reason = "TP"
                        exit_bar = j
                        break
                    if closes[j] >= sl_price:
                        exit_price = sl_price
                        exit_reason = "SL"
                        exit_bar = j
                        break

            if exit_price is None:
                exit_price = closes[exit_bar]

            if direction == "LONG":
                pnl = exit_price - entry_price
            else:
                pnl = entry_price - exit_price

            ts_str = str(timestamps[i])[:19] if timestamps is not None else f"bar_{i}"
            trades.append({
                "entry_bar": i,
                "timestamp": ts_str,
                "entry": entry_price,
                "exit": exit_price,
                "pnl": pnl,
                "reason": exit_reason,
                "prob": probs[i],
                "bars_held": exit_bar - i,
            })
            i = exit_bar + 1  # skip to after exit
        else:
            i += 1

    if not trades:
        print("  No trades triggered at this threshold.")
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    avg_pnl = total_pnl / len(trades)
    win_rate = len(wins) / len(trades) * 100.0
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0

    print(f"  Total trades:    {len(trades)}")
    print(f"  Wins:            {len(wins)}  ({win_rate:.1f}%)")
    print(f"  Losses:          {len(losses)}")
    print(f"  Avg Win:         ${avg_win:.2f}  (price points per contract)")
    print(f"  Avg Loss:        ${avg_loss:.2f}")
    print(f"  Total P&L:       ${total_pnl:.2f}")
    print(f"  Avg P&L/trade:   ${avg_pnl:.2f}")
    print()

    # Show individual trades
    show = min(20, len(trades))
    print(f"  {'#':>3}  {'Timestamp':<20}  {'Entry':>8}  {'Exit':>8}  "
          f"{'P&L':>8}  {'Reason':>7}  {'Prob':>6}  {'Bars':>4}")
    print("  " + "-" * 78)
    for idx, t in enumerate(trades[:show]):
        pnl_str = f"${t['pnl']:>+7.2f}"
        print(
            f"  {idx+1:>3}  {t['timestamp']:<20}  {t['entry']:>8.2f}  "
            f"{t['exit']:>8.2f}  {pnl_str}  {t['reason']:>7}  "
            f"{t['prob']:>6.2f}  {t['bars_held']:>4}"
        )
    if len(trades) > show:
        print(f"  ... and {len(trades) - show} more trades")


def print_report(
    fpath: Path,
    model_name: str,
    mode: str,
    n: int,
    live_prob_col: str,
    tolerance: float,
    live_probs: np.ndarray,
    offline_probs: np.ndarray,
    timestamps: np.ndarray | None = None,
    closes: np.ndarray | None = None,
    atrs: np.ndarray | None = None,
    df: pd.DataFrame | None = None,
    rebuilt: pd.DataFrame | None = None,
    feature_names: list[str] | None = None,
    offset: int = 0,
) -> None:
    """Print the parity validation report with sample rows and trade stats."""
    diff = np.abs(offline_probs - live_probs)
    mae = np.mean(diff)
    max_diff = np.max(diff)
    parity_pct = np.mean(diff < tolerance) * 100.0
    divergent_idx = np.where(diff >= tolerance)[0]

    w = 70

    # ── Section 1: Parity Summary ────────────────────────────────
    print()
    print("=" * w)
    print("STATE PARITY VALIDATION REPORT".center(w))
    print("=" * w)
    print(f"  Shadow log file:     {fpath.name}")
    print(f"  Model:               {model_name}")
    print(f"  Validation mode:     {mode}")
    print(f"  Total rows compared: {n}")
    print(f"  Live prob column:    {live_prob_col}")
    print(f"  Tolerance:           {tolerance}")
    print("-" * w)
    print(f"  Parity Match:        {parity_pct:.1f}%")
    print(f"  Mean Abs Error:      {mae:.8f}")
    print(f"  Max Abs Error:       {max_diff:.8f}")
    print(f"  Divergent rows:      {len(divergent_idx)}")
    print("-" * w)

    if len(divergent_idx) == 0:
        print("  [PASS] PIPELINE PARITY CONFIRMED")
        print("    The live pipeline produces identical predictions.")
        print("    If live performance diverges, the issue is DATA, not CODE.")
    else:
        print("  [FAIL] PIPELINE DIVERGENCE DETECTED")
        print(f"    {len(divergent_idx)} rows show prediction differences >= {tolerance}")
        print()
        show = min(10, len(divergent_idx))
        print(f"  First {show} divergent rows:")
        print(f"  {'Index':>8}  {'Live':>10}  {'Offline':>10}  {'Diff':>10}")
        for idx in divergent_idx[:show]:
            print(
                f"  {idx:>8}  {live_probs[idx]:>10.6f}  "
                f"{offline_probs[idx]:>10.6f}  {diff[idx]:>10.6f}"
            )

    # ── Section 2: Sample Rows (proof of work) ───────────────────
    print()
    print("=" * w)
    print("SAMPLE ROWS (side-by-side proof)".center(w))
    print("=" * w)

    # Pick representative sample: first 3, middle 3, highest 3, lowest 3
    sample_indices = []
    sample_indices.extend(range(min(3, n)))
    mid = n // 2
    sample_indices.extend(range(max(0, mid - 1), min(n, mid + 2)))
    top_idx = np.argsort(live_probs)[-3:][::-1]
    sample_indices.extend(top_idx.tolist())
    low_idx = np.argsort(live_probs)[:3]
    sample_indices.extend(low_idx.tolist())
    sample_indices = sorted(set(sample_indices))

    if timestamps is not None:
        print(f"  {'Row':>5}  {'Timestamp':<20}  {'Close':>8}  "
              f"{'LiveProb':>10}  {'OffProb':>10}  {'Match':>5}")
        print("  " + "-" * 68)
        for idx in sample_indices:
            ts_str = str(timestamps[idx])[:19] if idx < len(timestamps) else "?"
            close_str = f"{closes[idx]:>8.2f}" if closes is not None and idx < len(closes) else "     N/A"
            match_str = "  YES" if diff[idx] < tolerance else "   NO"
            print(
                f"  {idx:>5}  {ts_str:<20}  {close_str}  "
                f"{live_probs[idx]:>10.6f}  {offline_probs[idx]:>10.6f}  {match_str}"
            )
    else:
        print(f"  {'Row':>5}  {'LiveProb':>10}  {'OffProb':>10}  {'Match':>5}")
        print("  " + "-" * 36)
        for idx in sample_indices:
            match_str = "  YES" if diff[idx] < tolerance else "   NO"
            print(
                f"  {idx:>5}  {live_probs[idx]:>10.6f}  "
                f"{offline_probs[idx]:>10.6f}  {match_str}"
            )

    # ── Section 3: Prediction Distribution ───────────────────────
    print()
    print("=" * w)
    print("PREDICTION DISTRIBUTION".center(w))
    print("=" * w)
    print(f"  {'Stat':<20}  {'Live':>12}  {'Offline':>12}")
    print("  " + "-" * 48)
    for label, func in [
        ("Mean", np.mean), ("Std Dev", np.std),
        ("Min", np.min), ("25th pctile", lambda x: np.percentile(x, 25)),
        ("Median", np.median),
        ("75th pctile", lambda x: np.percentile(x, 75)),
        ("Max", np.max),
    ]:
        print(f"  {label:<20}  {func(live_probs):>12.6f}  {func(offline_probs):>12.6f}")

    # ── Section 4: Signal Analysis at Thresholds ─────────────────
    print()
    print("=" * w)
    print("SIGNAL ANALYSIS (live predictions)".center(w))
    print("=" * w)
    thresholds = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
    print(f"  {'Threshold':>10}  {'Signals':>8}  {'% of Bars':>10}  {'Avg Prob':>10}")
    print("  " + "-" * 44)
    for th in thresholds:
        mask = live_probs >= th
        count = int(np.sum(mask))
        pct = count / n * 100.0 if n > 0 else 0
        avg = float(np.mean(live_probs[mask])) if count > 0 else 0
        marker = "  <-- entry threshold" if abs(th - 0.70) < 0.001 else ""
        print(f"  {th:>10.2f}  {count:>8}  {pct:>9.1f}%  {avg:>10.4f}{marker}")

    # ── Section 5: Simulated Trade Results ───────────────────────
    if closes is not None and atrs is not None:
        _print_trade_simulation(
            w, live_probs, closes, atrs, timestamps,
            direction="LONG" if live_prob_col == "prob_buy" else "SHORT",
        )

    # ── Section 6: Feature-level comparison (divergence only) ────
    if len(divergent_idx) > 0 and df is not None and rebuilt is not None and feature_names:
        logged_features = [c for c in df.columns if c in feature_names]
        if logged_features and len(rebuilt) > 0:
            print()
            print("-" * w)
            print("  FEATURE-LEVEL DIVERGENCE (top 10):")
            feature_mae = {}
            for feat in logged_features:
                if feat in rebuilt.columns:
                    live_vals = df[feat].values[offset:offset + n]
                    offline_vals = rebuilt[feat].values[-n:]
                    fmae = np.mean(np.abs(live_vals - offline_vals))
                    feature_mae[feat] = fmae
            sorted_feats = sorted(feature_mae.items(), key=lambda x: x[1], reverse=True)
            print(f"  {'Feature':<35}  {'MAE':>12}")
            for feat, fmae in sorted_feats[:10]:
                print(f"  {feat:<35}  {fmae:>12.8f}")

    print("=" * w)


# ═══════════════════════════════════════════════════════════════════
# Main validation pipeline
# ═══════════════════════════════════════════════════════════════════

def run_validation(
    file_path: str,
    model_dir_str: str | None,
    tolerance: float,
    strategy: str | None = None,
) -> None:
    """Run the full parity validation pipeline."""
    # Load shadow log
    fpath = Path(file_path)
    if not fpath.exists():
        print(f"ERROR: File not found: {fpath}")
        sys.exit(1)

    df = pd.read_parquet(str(fpath))
    print(f"Loaded shadow log: {len(df)} rows, {len(df.columns)} columns")

    # Show available strategies and filter (auto-selects latest if mixed)
    if "strategy_name" in df.columns:
        strategies = df["strategy_name"].dropna().unique()
        if len(strategies) > 0:
            print(f"  Strategies found: {', '.join(sorted(strategies))}")
        if strategy:
            before = len(df)
            df = df[df["strategy_name"] == strategy].reset_index(drop=True)
            print(f"  Filtered to strategy '{strategy}': {before} → {len(df)} rows")
            if df.empty:
                print(f"ERROR: No rows match strategy '{strategy}'.")
                print(f"  Available strategies: {', '.join(sorted(strategies))}")
                sys.exit(1)
        elif len(strategies) > 1:
            # Auto-select the most recent strategy
            latest = df.dropna(subset=["strategy_name"]).iloc[-1]["strategy_name"]
            before = len(df)
            df = df[df["strategy_name"] == latest].reset_index(drop=True)
            print(f"  Auto-selected latest strategy '{latest}': {before} → {len(df)} rows")
            print(f"  (Use --strategy to override)")
        elif len(strategies) == 1:
            # Single strategy — filter to it (exclude any NaN rows)
            before = len(df)
            df = df[df["strategy_name"] == strategies[0]].reset_index(drop=True)
            if len(df) < before:
                print(f"  Filtered to '{strategies[0]}': {before} → {len(df)} rows")

    # Find model
    if model_dir_str:
        model_dir = Path(model_dir_str)
    else:
        model_dir = find_default_model_dir()
        if model_dir is None:
            print("ERROR: No model found in models/registry/. Use --model-dir.")
            sys.exit(1)
    print(f"Using model: {model_dir.name}")

    learner = load_model(model_dir)
    feature_names = learner.feature_names

    # Determine live probability column
    live_prob_col = None
    if "prob_buy" in df.columns and df["prob_buy"].notna().any():
        live_prob_col = "prob_buy"
    elif "prob_sell" in df.columns and df["prob_sell"].notna().any():
        live_prob_col = "prob_sell"

    if live_prob_col is None:
        print("ERROR: No prob_buy or prob_sell column with data found.")
        sys.exit(1)

    live_probs = df[live_prob_col].values

    # Extract timestamps and OHLCV for the report
    timestamps = df["timestamp"].values if "timestamp" in df.columns else None
    closes = df["Close"].values if "Close" in df.columns else None
    atrs = df["ATR_14"].values if "ATR_14" in df.columns else None

    # Decide validation mode based on available data
    logged_feature_cols = [c for c in feature_names if c in df.columns]
    has_logged_features = len(logged_feature_cols) >= len(feature_names) * 0.8

    ohlcv_cols = ["Open", "High", "Low", "Close", "Volume"]
    has_ohlcv = all(c in df.columns for c in ohlcv_cols)
    can_rebuild = has_ohlcv and len(df) >= _MIN_REBUILD_ROWS

    if has_logged_features:
        # ── Mode A: Feature Replay ──────────────────────────────────
        print()
        print(f"Mode A: FEATURE REPLAY ({len(logged_feature_cols)}/{len(feature_names)} features available)")
        print("  Using logged feature columns for offline model inference.")

        features_df = df[logged_feature_cols].copy()

        missing = set(feature_names) - set(logged_feature_cols)
        if missing:
            print(f"  WARNING: {len(missing)} features missing, filling with 0: {sorted(missing)[:5]}...")
            for m in missing:
                features_df[m] = 0.0

        features_df = features_df[feature_names]
        features_df = features_df.fillna(0)

        print(f"Running offline inference on {len(features_df)} rows...")
        offline_probs = run_inference(learner, features_df)

        print_report(
            fpath=fpath,
            model_name=model_dir.name,
            mode="Feature Replay",
            n=len(live_probs),
            live_prob_col=live_prob_col,
            tolerance=tolerance,
            live_probs=live_probs,
            offline_probs=offline_probs,
            timestamps=timestamps,
            closes=closes,
            atrs=atrs,
        )

    elif can_rebuild:
        # ── Mode B: Full Rebuild ────────────────────────────────────
        print()
        print("Mode B: FULL FEATURE REBUILD")
        print(f"  Rebuilding features from {len(df)} OHLCV rows via AlphaFactory.")

        ts_col = "timestamp" if "timestamp" in df.columns else None
        ohlcv_df = df[([ts_col] if ts_col else []) + ohlcv_cols].copy()

        print("Rebuilding features offline...")
        rebuilt = rebuild_features(ohlcv_df, feature_names)

        if rebuilt is None or len(rebuilt) == 0:
            print("ERROR: Feature rebuild failed.")
            sys.exit(1)

        print(f"Running offline inference on {len(rebuilt)} rows...")
        offline_probs = run_inference(learner, rebuilt)

        # Align lengths
        n = min(len(offline_probs), len(live_probs))
        offset = 0
        if n < len(live_probs):
            offset = len(live_probs) - n
            live_probs_aligned = live_probs[offset:]
            ts_aligned = timestamps[offset:] if timestamps is not None else None
            closes_aligned = closes[offset:] if closes is not None else None
            atrs_aligned = atrs[offset:] if atrs is not None else None
        else:
            live_probs_aligned = live_probs
            ts_aligned = timestamps
            closes_aligned = closes
            atrs_aligned = atrs

        offline_probs_aligned = offline_probs[-n:]

        print_report(
            fpath=fpath,
            model_name=model_dir.name,
            mode="Full Rebuild",
            n=n,
            live_prob_col=live_prob_col,
            tolerance=tolerance,
            live_probs=live_probs_aligned,
            offline_probs=offline_probs_aligned,
            timestamps=ts_aligned,
            closes=closes_aligned,
            atrs=atrs_aligned,
            df=df,
            rebuilt=rebuilt,
            feature_names=feature_names,
            offset=offset,
        )

    else:
        print()
        print("ERROR: Cannot validate parity.")
        if not has_logged_features:
            print(f"  - Shadow log has only {len(logged_feature_cols)}/{len(feature_names)} feature columns.")
        if not can_rebuild:
            print(f"  - Only {len(df)} OHLCV rows available (need {_MIN_REBUILD_ROWS}+ for full rebuild).")
        print()
        print("Options:")
        print("  1. Wait for more live data to accumulate.")
        print("  2. Re-generate mock data with more rows:")
        print(f"     python scripts/generate_mock_shadow_log.py --rows {_MIN_REBUILD_ROWS}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate parity between live and offline prediction pipelines"
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Path to the shadow log Parquet file",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help="Path to the model directory (auto-detected if omitted)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1e-6,
        help="Maximum acceptable prediction difference (default: 1e-6)",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        help="Filter shadow log to a specific strategy_name (e.g. 'ManateeKoala_Conservative')",
    )
    args = parser.parse_args()

    # Resolve paths via CL_DATA_ROOT fallback
    from src.data_paths import resolve_cli_path, get_data_path
    if args.file is None:
        args.file = str(get_data_path("processed/live_shadow_log.parquet"))
    else:
        args.file = resolve_cli_path(args.file)
    if args.model_dir:
        args.model_dir = resolve_cli_path(args.model_dir)

    run_validation(args.file, args.model_dir, args.tolerance, args.strategy)


if __name__ == "__main__":
    main()
