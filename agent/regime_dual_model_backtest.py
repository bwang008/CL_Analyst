"""
Regime backtest runner for combined long + short models.

This merges separate buy/sell prediction files into a single signal set,
then runs concurrent backtests over specified regime windows.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent.backtest_cl_concurrent import CLConcurrentPositionBacktester, load_ohlcv, load_predictions


@dataclass
class RegimeWindow:
    name: str
    start: str
    end: str


def _prepare_signals(
    buy_preds: pd.DataFrame,
    sell_preds: pd.DataFrame,
    *,
    buy_threshold: float,
    sell_threshold: float,
) -> pd.DataFrame:
    merged = pd.DataFrame(index=buy_preds.index.union(sell_preds.index).sort_values())
    if "prob_Buy" in buy_preds.columns:
        merged["prob_Buy"] = buy_preds["prob_Buy"]
    if "prob_Sell" in sell_preds.columns:
        merged["prob_Sell"] = sell_preds["prob_Sell"]

    if "prob_Buy" not in merged.columns and "prob_Sell" not in merged.columns:
        raise ValueError("Merged predictions missing prob_Buy/prob_Sell columns.")

    if "prob_Buy" in merged.columns:
        merged.loc[merged["prob_Buy"] < buy_threshold, "prob_Buy"] = np.nan
    if "prob_Sell" in merged.columns:
        merged.loc[merged["prob_Sell"] < sell_threshold, "prob_Sell"] = np.nan

    return merged


def run_regime_backtests(
    *,
    buy_predictions_path: str,
    sell_predictions_path: str,
    data_path: str,
    buy_threshold: float,
    sell_threshold: float,
    tp_mult: float,
    sl_mult: float,
    commission_per_side: float,
    slippage_per_side: float,
    contract_multiplier: float,
    output_dir: str,
) -> pd.DataFrame:
    os.makedirs(output_dir, exist_ok=True)

    buy_preds = load_predictions(buy_predictions_path)
    sell_preds = load_predictions(sell_predictions_path)
    ohlcv = load_ohlcv(data_path)

    signals = _prepare_signals(
        buy_preds,
        sell_preds,
        buy_threshold=buy_threshold,
        sell_threshold=sell_threshold,
    )

    regimes = [
        RegimeWindow("gfc_aftershock_2009", "2009-01-01", "2009-06-30"),
        RegimeWindow("bull_2017", "2017-01-01", "2017-12-31"),
        RegimeWindow("covid_crash_2020", "2020-02-01", "2020-06-30"),
    ]

    rows: list[dict] = []
    for regime in regimes:
        start_ts = pd.Timestamp(regime.start)
        end_ts = pd.Timestamp(regime.end)

        regime_ohlcv = ohlcv[(ohlcv.index >= start_ts) & (ohlcv.index <= end_ts)]
        regime_signals = signals.loc[signals.index.intersection(regime_ohlcv.index)]

        if regime_ohlcv.empty or regime_signals.empty:
            rows.append(
                {
                    "regime": regime.name,
                    "start": regime.start,
                    "end": regime.end,
                    "status": "skipped",
                    "reason": "empty_ohlcv_or_signals",
                }
            )
            continue

        bt = CLConcurrentPositionBacktester(
            tp_atr_mult=tp_mult,
            sl_atr_mult=sl_mult,
            prob_threshold=0.0,
            commission_per_side=commission_per_side,
            slippage_per_side=slippage_per_side,
            contract_multiplier=contract_multiplier,
            position_sizing=False,
        )

        result = bt.run(
            regime_signals,
            regime_ohlcv,
            label=f"{regime.name}",
            signal_side="auto",
            signal_col=None,
        )

        rows.append(
            {
                "regime": regime.name,
                "start": regime.start,
                "end": regime.end,
                "status": "ok",
                "trade_count": result.trade_count,
                "win_rate": result.win_rate,
                "profit_factor": result.profit_factor,
                "total_pnl": result.total_pnl,
                "max_drawdown": result.max_drawdown,
                "pnl_to_drawdown_ratio": (
                    result.total_pnl / abs(result.max_drawdown)
                    if result.max_drawdown != 0
                    else float("inf") if result.total_pnl > 0 else 0.0
                ),
                "max_concurrent": result.max_concurrent,
            }
        )

        regime_signals.to_csv(
            os.path.join(output_dir, f"{regime.name}_predictions.csv")
        )

    summary_df = pd.DataFrame(rows)
    summary_path = os.path.join(output_dir, "dual_model_regime_summary.csv")
    summary_df.to_csv(summary_path, index=False)
    return summary_df


def main() -> None:
    parser = argparse.ArgumentParser(description="Dual-model regime backtests")
    parser.add_argument(
        "--buy-predictions",
        default="reports/vault_predictions_exp017.csv",
        help="CSV with prob_Buy predictions",
    )
    parser.add_argument(
        "--sell-predictions",
        default="reports/short_sniper_predictions.csv",
        help="CSV with prob_Sell predictions",
    )
    parser.add_argument(
        "--data",
        default="data/processed/CL_set_06_shortfix.parquet",
        help="OHLCV parquet (aligned with predictions)",
    )
    parser.add_argument("--buy-threshold", type=float, default=0.60)
    parser.add_argument("--sell-threshold", type=float, default=0.60)
    parser.add_argument("--tp-mult", type=float, default=5.0)
    parser.add_argument("--sl-mult", type=float, default=0.75)
    parser.add_argument("--commission-per-side", type=float, default=2.50)
    parser.add_argument("--slippage-per-side", type=float, default=0.03)
    parser.add_argument("--contract-multiplier", type=float, default=1000.0)
    parser.add_argument(
        "--output-dir",
        default=os.path.join("reports", "oos_regimes"),
        help="Directory to write regime results",
    )
    args = parser.parse_args()

    summary = run_regime_backtests(
        buy_predictions_path=args.buy_predictions,
        sell_predictions_path=args.sell_predictions,
        data_path=args.data,
        buy_threshold=args.buy_threshold,
        sell_threshold=args.sell_threshold,
        tp_mult=args.tp_mult,
        sl_mult=args.sl_mult,
        commission_per_side=args.commission_per_side,
        slippage_per_side=args.slippage_per_side,
        contract_multiplier=args.contract_multiplier,
        output_dir=args.output_dir,
    )
    print(summary)


if __name__ == "__main__":
    main()
