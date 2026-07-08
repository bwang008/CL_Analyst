"""A/B test: weekend-carry flatten overlay vs baseline, across the fleet.

For every config in the fleet manifest, this runs the BASELINE (overlay off) and
one or more TREATMENT arms (overlay on, at given profit-ATR thresholds) on the
HOLDOUT window only, using the same data, predictions, seed, and economics.  It
reports the deltas on the project's own annualized-monthly Sharpe/Sortino, plus
drawdown, PnL, trade count, and weekend-flatten attribution.

Backtester-only.  Reads production configs + predictions; writes nothing to prod.

Usage:
    python -m agent.ab_weekend_flatten
    python -m agent.ab_weekend_flatten --profit-atr-mult 0.0,0.5,1.0,1.5
    python -m agent.ab_weekend_flatten --full        # also show full-period rows
"""

from __future__ import annotations

import argparse
import copy
import glob
import json
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from agent.backtest_engine import (
    BacktestEngine,
    load_ohlcv_dual,
    load_predictions,
    _resolve_prob_column,
)
from src.data_paths import resolve_cli_path
from src.live_execution.config_loader import load_strategy_config


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Production configs backtest against the per-symbol HourSet parquet that
# produced their predictions (embeds EXEC_ raw-fill columns).  Explicit and
# auditable; override per-config with a "backtest_data_path" key if ever needed.
SYMBOL_DATA = {
    "CL": "data/processed/CL_HourSet_14B.parquet",
    "ES": "data/processed/ES_HourSet_01B.parquet",
    "NG": "data/processed/NG_HourSet_01B.parquet",
    "GC": "data/processed/GC_HourSet_01B.parquet",
    "SI": "data/processed/SI_HourSet_01B.parquet",
}


# ---------------------------------------------------------------------------
# path / data resolution (mirrors backtest_engine.main())
# ---------------------------------------------------------------------------


def _resolve_predictions_path(raw_path: str) -> str:
    """Resolve a predictions path, healing batch-dir name drift by basename."""
    if not raw_path:
        return raw_path
    p = resolve_cli_path(raw_path)
    if os.path.exists(p):
        return p
    # Fallback: production configs record a canonical batch-dir name that may
    # differ from the local suffixed dir — search by basename under reports/.
    matches = glob.glob(
        os.path.join(REPO_ROOT, "reports", "batch_runs", "**",
                     os.path.basename(raw_path)),
        recursive=True,
    )
    if matches:
        return matches[0]
    raise FileNotFoundError(
        f"predictions not found: {raw_path} (and no basename match under reports/)"
    )


def _load_predictions_for_config(config_path: str, cfg: dict) -> pd.DataFrame:
    """Build the merged prob_Buy/prob_Sell frame, mirroring main()'s dual path."""
    models = cfg.get("models", {})
    long_p = models.get("long", {}).get("predictions_path")
    short_p = models.get("short", {}).get("predictions_path")
    if long_p and short_p:
        long_df = load_predictions(_resolve_predictions_path(long_p))
        short_df = load_predictions(_resolve_predictions_path(short_p))
        long_col = _resolve_prob_column(long_df, "buy")
        short_col = _resolve_prob_column(short_df, "sell")
        if long_col is None or short_col is None:
            raise ValueError(
                f"{config_path}: predictions missing buy/sell columns "
                f"(long={list(long_df.columns)}, short={list(short_df.columns)})"
            )
        long_probs = long_df[[long_col]].rename(columns={long_col: "prob_Buy"})
        short_probs = short_df[[short_col]].rename(columns={short_col: "prob_Sell"})
        return long_probs.join(short_probs, how="outer").fillna(0.0)
    only = long_p or short_p
    if not only:
        raise ValueError(f"{config_path}: no predictions_path in config models")
    return load_predictions(_resolve_predictions_path(only))


def _resolve_economics(cfg: dict) -> tuple[float, float]:
    """(contract_multiplier, slippage_per_side) from execution_symbol registry."""
    from src.core.instrument_master import default_slippage_points, dollars_per_point

    sym = cfg.get("execution_symbol")
    if not sym:
        raise ValueError("config missing execution_symbol")
    return dollars_per_point(sym), default_slippage_points(sym)


def _data_path_for(cfg: dict) -> str:
    override = cfg.get("backtest_data_path")
    if override:
        return resolve_cli_path(override)
    sym = cfg.get("execution_symbol")
    if sym not in SYMBOL_DATA:
        raise ValueError(f"no data mapping for symbol {sym!r}; add to SYMBOL_DATA")
    return resolve_cli_path(SYMBOL_DATA[sym])


# ---------------------------------------------------------------------------
# metrics — replicate strategy_optimizer's annualized-monthly Sharpe/Sortino
# ---------------------------------------------------------------------------


def _monthly_pnl(result) -> np.ndarray:
    if result.trade_count == 0 or not result.trades:
        return np.array([])
    df = pd.DataFrame(
        [{"exit_dt": t.exit_dt, "pnl": t.net_pnl_dollars} for t in result.trades]
    )
    df["exit_dt"] = pd.to_datetime(df["exit_dt"])
    return df.set_index("exit_dt").sort_index()["pnl"].resample("M").sum().dropna().values


def _sharpe(monthly: np.ndarray) -> float:
    if len(monthly) == 0:
        return float("nan")
    sd = float(np.std(monthly))
    if sd < 1e-9:
        return float("nan")
    return float(np.mean(monthly) / sd * np.sqrt(12))


def _sortino(monthly: np.ndarray) -> float:
    if len(monthly) == 0:
        return float("nan")
    dd = float(np.sqrt(np.mean(np.minimum(0, monthly) ** 2)))
    if dd < 1e-9:
        return float("inf") if float(np.mean(monthly)) > 0 else float("nan")
    return float(np.mean(monthly) / dd * np.sqrt(12))


@dataclass
class Metrics:
    trades: int
    pnl: float
    sharpe: float
    sortino: float
    max_dd: float
    win_rate: float
    monthly_std: float
    n_flat: int
    pnl_flat: float

    @classmethod
    def of(cls, result) -> "Metrics":
        monthly = _monthly_pnl(result)
        flat = [t for t in result.trades if t.exit_reason.value == "WEEKEND_FLATTEN"]
        return cls(
            trades=result.trade_count,
            pnl=result.total_pnl,
            sharpe=_sharpe(monthly),
            sortino=_sortino(monthly),
            max_dd=result.max_drawdown,
            win_rate=result.win_rate,
            monthly_std=float(np.std(monthly)) if len(monthly) else float("nan"),
            n_flat=len(flat),
            pnl_flat=float(sum(t.net_pnl_dollars for t in flat)),
        )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _run_engine(cfg: dict, preds: pd.DataFrame, ohlcv, ohlcv_exec,
                mult: float, cm: float, slip: float, min_gap: float):
    """Build an engine (overlay off if mult is None) and run it on `preds`."""
    run_cfg = copy.deepcopy(cfg)
    if mult is None:
        run_cfg.pop("weekend_flatten", None)
    else:
        run_cfg["weekend_flatten"] = {
            "enabled": True, "profit_atr_mult": mult, "min_gap_hours": min_gap,
        }
    engine = BacktestEngine.from_config(
        run_cfg, commission_per_side=2.50, slippage_per_side=slip,
        contract_multiplier=cm,
    )
    return engine.run(preds, ohlcv, ohlcv_exec_df=ohlcv_exec)


def _holdout_slice(preds: pd.DataFrame, holdout_months: int) -> pd.DataFrame:
    if not holdout_months:
        return preds
    cutoff = preds.index.max() - pd.DateOffset(months=holdout_months)
    return preds[preds.index >= cutoff]


def _fmt(x: float, nd: int = 2) -> str:
    if x != x:  # NaN
        return "   n/a"
    if x == float("inf"):
        return "   inf"
    return f"{x:>{4+nd}.{nd}f}"


def _print_block(name: str, symbol: str, base: Metrics, arms: list[tuple[float, Metrics]]):
    print(f"\n{'='*100}")
    print(f"  {name}   [{symbol}]")
    print(f"{'-'*100}")
    hdr = (f"  {'arm':<16} | {'trades':>6} | {'net PnL':>12} | {'Sharpe':>7} | "
           f"{'Sortino':>7} | {'maxDD':>11} | {'moStd':>9} | {'flat#':>5} | {'flatPnL':>10}")
    print(hdr)
    print(f"  {'-'*96}")
    print(f"  {'baseline':<16} | {base.trades:>6} | {base.pnl:>12,.0f} | "
          f"{_fmt(base.sharpe)} | {_fmt(base.sortino)} | {base.max_dd:>11,.0f} | "
          f"{base.monthly_std:>9,.0f} | {'-':>5} | {'-':>10}")
    for mult, m in arms:
        dS = m.sharpe - base.sharpe
        print(f"  {'flat@'+format(mult,'.2f')+'ATR':<16} | {m.trades:>6} | {m.pnl:>12,.0f} | "
              f"{_fmt(m.sharpe)} | {_fmt(m.sortino)} | {m.max_dd:>11,.0f} | "
              f"{m.monthly_std:>9,.0f} | {m.n_flat:>5} | {m.pnl_flat:>10,.0f}"
              f"   (dSharpe {dS:+.2f}, dPnL {m.pnl-base.pnl:+,.0f}, dDD {m.max_dd-base.max_dd:+,.0f})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default="configs/fleet/fleet_manifest.json")
    ap.add_argument("--profit-atr-mult", default="0.0,0.5,1.0,1.5",
                    help="comma-separated ATR thresholds to test as treatment arms")
    ap.add_argument("--min-gap-hours", type=float, default=40.0)
    ap.add_argument("--full", action="store_true",
                    help="also print full-period rows (default: holdout only)")
    args = ap.parse_args()

    mults = [float(x) for x in args.profit_atr_mult.split(",") if x.strip() != ""]
    manifest = json.load(open(resolve_cli_path(args.manifest)))
    instances = [i for i in manifest["instances"] if i.get("enabled", True)]

    print(f"Weekend-flatten A/B  |  {len(instances)} configs  |  arms: "
          f"{', '.join(f'{m:.2f}ATR' for m in mults)}  |  min_gap={args.min_gap_hours}h")

    holdout_rows: list[tuple[str, Metrics, list[tuple[float, Metrics]]]] = []

    for inst in instances:
        cfg_path = resolve_cli_path(inst["config"])
        cfg = load_strategy_config(cfg_path)
        symbol = cfg.get("execution_symbol", "?")
        name = os.path.basename(inst["config"])
        cm, slip = _resolve_economics(cfg)
        data_path = _data_path_for(cfg)
        ohlcv, ohlcv_exec = load_ohlcv_dual(data_path)
        preds = _load_predictions_for_config(cfg_path, cfg)
        holdout_months = cfg.get("holdout_months", 0) or 0
        ho_preds = _holdout_slice(preds, holdout_months)

        print(f"\n### {name}  ({symbol})  data={os.path.basename(data_path)}  "
              f"preds={len(preds):,} rows  holdout={holdout_months}mo "
              f"({len(ho_preds):,} rows)")

        # HOLDOUT (the decision window)
        base_ho = Metrics.of(_run_engine(cfg, ho_preds, ohlcv, ohlcv_exec, None, cm, slip, args.min_gap_hours))
        arms_ho = [(m, Metrics.of(_run_engine(cfg, ho_preds, ohlcv, ohlcv_exec, m, cm, slip, args.min_gap_hours)))
                   for m in mults]
        _print_block(f"HOLDOUT ({holdout_months}mo)", symbol, base_ho, arms_ho)
        holdout_rows.append((f"{symbol} {name}", base_ho, arms_ho))

        if args.full:
            base_f = Metrics.of(_run_engine(cfg, preds, ohlcv, ohlcv_exec, None, cm, slip, args.min_gap_hours))
            arms_f = [(m, Metrics.of(_run_engine(cfg, preds, ohlcv, ohlcv_exec, m, cm, slip, args.min_gap_hours)))
                      for m in mults]
            _print_block("FULL PERIOD", symbol, base_f, arms_f)

    # ---- aggregate verdict (holdout) ----
    print(f"\n\n{'#'*100}\n  AGGREGATE (HOLDOUT) - per arm, summed across configs\n{'#'*100}")
    print(f"  {'arm':<12} | {'dSharpe(sum)':>13} | {'dPnL(sum)':>13} | "
          f"{'dMaxDD(sum)':>13} | {'#improved':>10} | {'flats':>6}")
    for i, mult in enumerate(mults):
        dS = sum(arms[i][1].sharpe - base.sharpe for _, base, arms in holdout_rows
                 if arms[i][1].sharpe == arms[i][1].sharpe and base.sharpe == base.sharpe)
        dP = sum(arms[i][1].pnl - base.pnl for _, base, arms in holdout_rows)
        dDD = sum(arms[i][1].max_dd - base.max_dd for _, base, arms in holdout_rows)
        improved = sum(1 for _, base, arms in holdout_rows if arms[i][1].pnl > base.pnl)
        flats = sum(arms[i][1].n_flat for _, base, arms in holdout_rows)
        print(f"  {format(mult,'.2f')+'ATR':<12} | {dS:>13.2f} | {dP:>13,.0f} | "
              f"{dDD:>13,.0f} | {improved:>3}/{len(holdout_rows):<6} | {flats:>6}")
    print()


if __name__ == "__main__":
    main()
