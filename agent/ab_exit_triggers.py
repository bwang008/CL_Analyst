"""A/B test: exit-trigger overlays vs baseline, across the fleet.

Ticket: exit-triggers-eod-oppsignal_07072026_1924

For every config in the fleet manifest, runs the BASELINE (all overlays off)
and one arm per exit enhancement on the HOLDOUT window only, using the same
data, predictions, seed, and economics.  Arms:

  - ``wkd@G``    weekend_flatten enabled at profit gate G (ATR multiples)
  - ``eod@G``    eod_flatten enabled at gate G
  - ``both@G``   weekend + EOD together at gate G
  - ``oppo``     conflict_resolution overridden to
                 ``close_existing_position_if_profit`` (Phase 2; requires the
                 mode to exist — skipped gracefully if not yet implemented)

Reports deltas on the project's own annualized-monthly Sharpe/Sortino, plus
drawdown, PnL, trade count, and per-trigger exit attribution.

Backtester-only.  Reads production configs + predictions; writes nothing.

Usage:
    python -m agent.ab_exit_triggers                       # 4-arm trigger A/B
    python -m agent.ab_exit_triggers --gates 0.0,1.0       # profit gates
    python -m agent.ab_exit_triggers --arms wkd,eod,both   # trigger arms
    python -m agent.ab_exit_triggers --arms wkd,eod,oppo   # phase-2 individual toggles
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
# auditable; override per-config with a "backtest_data_path" key if needed.
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
    n_wkd: int
    pnl_wkd: float
    n_eod: int
    pnl_eod: float
    n_sig: int

    @classmethod
    def of(cls, result) -> "Metrics":
        monthly = _monthly_pnl(result)
        wkd = [t for t in result.trades if t.exit_reason.value == "WEEKEND_FLATTEN"]
        eod = [t for t in result.trades if t.exit_reason.value == "EOD_FLATTEN"]
        sig = [t for t in result.trades if t.exit_reason.value == "SIGNAL_EXIT"]
        return cls(
            trades=result.trade_count,
            pnl=result.total_pnl,
            sharpe=_sharpe(monthly),
            sortino=_sortino(monthly),
            max_dd=result.max_drawdown,
            win_rate=result.win_rate,
            monthly_std=float(np.std(monthly)) if len(monthly) else float("nan"),
            n_wkd=len(wkd),
            pnl_wkd=float(sum(t.net_pnl_dollars for t in wkd)),
            n_eod=len(eod),
            pnl_eod=float(sum(t.net_pnl_dollars for t in eod)),
            n_sig=len(sig),
        )


# ---------------------------------------------------------------------------
# arms
# ---------------------------------------------------------------------------


def _apply_arm(cfg: dict, arm: dict) -> dict:
    """Return a deep-copied config with the arm's overlays applied.

    arm keys: weekend_mult / eod_mult (Optional[float]),
              conflict_override (Optional[str]).
    """
    run_cfg = copy.deepcopy(cfg)
    run_cfg.pop("weekend_flatten", None)
    run_cfg.pop("eod_flatten", None)
    if arm.get("weekend_mult") is not None:
        run_cfg["weekend_flatten"] = {
            "enabled": True, "profit_atr_mult": arm["weekend_mult"],
        }
    if arm.get("eod_mult") is not None:
        run_cfg["eod_flatten"] = {
            "enabled": True, "profit_atr_mult": arm["eod_mult"],
        }
    if arm.get("conflict_override"):
        run_cfg["conflict_resolution"] = arm["conflict_override"]
    return run_cfg


def _build_arms(arm_names: list[str], gates: list[float]) -> list[tuple[str, dict]]:
    arms: list[tuple[str, dict]] = []
    for g in gates:
        tag = format(g, ".1f")
        if "eod" in arm_names:
            arms.append((f"eod@{tag}", {"eod_mult": g}))
        if "wkd" in arm_names:
            arms.append((f"wkd@{tag}", {"weekend_mult": g}))
        if "both" in arm_names:
            arms.append((f"both@{tag}", {"weekend_mult": g, "eod_mult": g}))
    if "oppo" in arm_names:
        arms.append(("oppo", {"conflict_override": "close_existing_position_if_profit"}))
    return arms


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _run_engine(run_cfg: dict, preds: pd.DataFrame, ohlcv, ohlcv_exec,
                cm: float, slip: float):
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
    if x != x:
        return "   n/a"
    if x == float("inf"):
        return "   inf"
    return f"{x:>{4+nd}.{nd}f}"


def _print_block(title: str, symbol: str, base: Metrics,
                 arms: list[tuple[str, Metrics]]):
    w = 118
    print(f"\n{'='*w}")
    print(f"  {title}   [{symbol}]")
    print(f"{'-'*w}")
    print(f"  {'arm':<10} | {'trades':>6} | {'net PnL':>12} | {'Sharpe':>7} | "
          f"{'Sortino':>7} | {'maxDD':>11} | {'wkd#':>4} | {'wkdPnL':>9} | "
          f"{'eod#':>4} | {'eodPnL':>9} | {'sig#':>4} | delta")
    print(f"  {'-'*(w-4)}")
    print(f"  {'baseline':<10} | {base.trades:>6} | {base.pnl:>12,.0f} | "
          f"{_fmt(base.sharpe)} | {_fmt(base.sortino)} | {base.max_dd:>11,.0f} | "
          f"{'-':>4} | {'-':>9} | {'-':>4} | {'-':>9} | {base.n_sig:>4} |")
    for label, m in arms:
        dS = m.sharpe - base.sharpe
        print(f"  {label:<10} | {m.trades:>6} | {m.pnl:>12,.0f} | "
              f"{_fmt(m.sharpe)} | {_fmt(m.sortino)} | {m.max_dd:>11,.0f} | "
              f"{m.n_wkd:>4} | {m.pnl_wkd:>9,.0f} | {m.n_eod:>4} | "
              f"{m.pnl_eod:>9,.0f} | {m.n_sig:>4} | "
              f"dSharpe {dS:+.2f}, dPnL {m.pnl-base.pnl:+,.0f}, "
              f"dDD {m.max_dd-base.max_dd:+,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default="configs/fleet/fleet_manifest.json")
    ap.add_argument("--arms", default="eod,wkd,both",
                    help="comma list from {eod,wkd,both,oppo}")
    ap.add_argument("--gates", default="0.0,1.0",
                    help="comma-separated profit gates (ATR multiples) for trigger arms")
    ap.add_argument("--full", action="store_true",
                    help="also print full-period rows (default: holdout only)")
    args = ap.parse_args()

    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    gates = [float(x) for x in args.gates.split(",") if x.strip() != ""]
    arms_spec = _build_arms(arm_names, gates)

    manifest = json.load(open(resolve_cli_path(args.manifest)))
    instances = [i for i in manifest["instances"] if i.get("enabled", True)]

    print(f"Exit-trigger A/B  |  {len(instances)} configs  |  arms: "
          f"{', '.join(l for l, _ in arms_spec)}")

    rows: list[tuple[str, Metrics, list[tuple[str, Metrics]]]] = []

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

        print(f"\n### {name}  ({symbol})  conflict={cfg.get('conflict_resolution')}  "
              f"data={os.path.basename(data_path)}  holdout={holdout_months}mo "
              f"({len(ho_preds):,} rows)")

        base = Metrics.of(_run_engine(_apply_arm(cfg, {}), ho_preds, ohlcv,
                                      ohlcv_exec, cm, slip))
        arm_metrics = []
        for label, arm in arms_spec:
            m = Metrics.of(_run_engine(_apply_arm(cfg, arm), ho_preds, ohlcv,
                                       ohlcv_exec, cm, slip))
            arm_metrics.append((label, m))
        _print_block(f"HOLDOUT ({holdout_months}mo)", symbol, base, arm_metrics)
        rows.append((f"{symbol} {name}", base, arm_metrics))

        if args.full:
            base_f = Metrics.of(_run_engine(_apply_arm(cfg, {}), preds, ohlcv,
                                            ohlcv_exec, cm, slip))
            arm_f = [(l, Metrics.of(_run_engine(_apply_arm(cfg, a), preds, ohlcv,
                                                ohlcv_exec, cm, slip)))
                     for l, a in arms_spec]
            _print_block("FULL PERIOD", symbol, base_f, arm_f)

    # ---- aggregate verdict (holdout) ----
    print(f"\n\n{'#'*100}\n  AGGREGATE (HOLDOUT) - per arm, summed across configs\n{'#'*100}")
    print(f"  {'arm':<10} | {'dSharpe(sum)':>13} | {'dPnL(sum)':>13} | "
          f"{'dMaxDD(sum)':>13} | {'#improved':>10} | {'wkd':>5} | {'eod':>5}")
    for i, (label, _) in enumerate(arms_spec):
        dS = sum(arms[i][1].sharpe - base.sharpe for _, base, arms in rows
                 if arms[i][1].sharpe == arms[i][1].sharpe and base.sharpe == base.sharpe)
        dP = sum(arms[i][1].pnl - base.pnl for _, base, arms in rows)
        dDD = sum(arms[i][1].max_dd - base.max_dd for _, base, arms in rows)
        improved = sum(1 for _, base, arms in rows if arms[i][1].pnl > base.pnl)
        n_w = sum(arms[i][1].n_wkd for _, _, arms in rows)
        n_e = sum(arms[i][1].n_eod for _, _, arms in rows)
        print(f"  {label:<10} | {dS:>13.2f} | {dP:>13,.0f} | {dDD:>13,.0f} | "
              f"{improved:>3}/{len(rows):<6} | {n_w:>5} | {n_e:>5}")
    print()


if __name__ == "__main__":
    main()
