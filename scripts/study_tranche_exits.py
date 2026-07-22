"""Tranche-exit decision-gate study (ticket backtest-tranche-exits_07222026_1608).

Compares, per fleet symbol, the tuned SINGLE take-profit exit against
scale-out ladders at multi-lot sizing, using the engine's new tranche
support. This is the GO / NO-GO gate for live Stage 3
(oco-leg-race-audit_07212026_1935): ladders only graduate to live work if
they beat the same-lots single-TP baseline here.

Method
- For each fleet config: baseline (its own tuned single rung) and three
  ladder variants built by scaling the tuned tp_atr_mult per side:
    L2a  50/50 at [0.7x, 1.3x]
    L2b  60/40 at [0.8x, 1.5x]
    L3   40/30/30 at [0.6x, 1.0x, 1.6x]
  at lots {2, 3}. Everything else (thresholds, SL, trailing, barriers,
  economics) identical. Baselines are RUN, not assumed, so linear-scaling
  claims are proven rather than inferred.
- Economics per symbol from the instrument registry
  (dollars_per_point / default_slippage_points), commission $2.50/side —
  the engine CLI's own default.
- Predictions are pre-computed CSVs; three configs carry stale
  predictions_path values (renamed batch folders — the validate-parity
  Pitfall #1), corrected here explicitly. Missing data is REPORTED and the
  symbol skipped — never fabricated.

Run:  conda run -n trader python scripts/study_tranche_exits.py
Output: reports/tranche_exit_study_<date>.md + .csv (repo-local reports/).
"""
from __future__ import annotations

import copy
import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.backtest_engine import (  # noqa: E402
    BacktestEngine,
    load_ohlcv_dual,
    load_predictions,
    _resolve_prob_column,
)
from src.core.instrument_master import (  # noqa: E402
    dollars_per_point,
    default_slippage_points,
)
from src.data_paths import resolve_cli_path  # noqa: E402
from src.live_execution.config_loader import load_strategy_config  # noqa: E402

COMMISSION_PER_SIDE = 2.50
DATA_ROOT = r"C:\CL_Analyst_Data\data\processed"

# (config path, dataset parquet, corrected predictions path or None=config OK)
FLEET = [
    ("configs/strategies/HS14B_Sharpe_E01_06262026.json",
     rf"{DATA_ROOT}\CL_HourSet_14B.parquet",
     r"reports/batch_runs/batch_20260626_0017_HS14B_SCOUT_FIX/predictions/HS14B_Sharpe_E01_predictions.csv"),
    ("configs/strategies/ES02B_Sharpe_E01_07112026.json",
     rf"{DATA_ROOT}\ES_HourSet_02B.parquet",
     None),
    ("configs/strategies/NG01B_Sharpe_E03_07052026.json",
     rf"{DATA_ROOT}\NG_HourSet_01B.parquet",
     None),
    ("configs/strategies/GC02B_Sharpe_E04_07102026.json",
     rf"{DATA_ROOT}\GC_HourSet_02B.parquet",
     r"reports/batch_runs/PRODUCTION_batch_20260710_120946_GC_SCOUT/predictions/GC_Sharpe_E04_predictions.csv"),
    ("configs/strategies/SI01B_Sharpe_E02_07062026.json",
     rf"{DATA_ROOT}\SI_HourSet_01B.parquet",
     r"reports/batch_runs/batch_20260706_0925_SI_01B_SCOUT/predictions/SI_Sharpe_E02_predictions.csv"),
]

# (name, [(qty_pct, tp_scale), ...]); baseline = config's own single rung.
LADDERS = [
    ("L2a_50-50_0.7-1.3", [(0.5, 0.7), (0.5, 1.3)]),
    ("L2b_60-40_0.8-1.5", [(0.6, 0.8), (0.4, 1.5)]),
    ("L3_40-30-30_0.6-1.0-1.6", [(0.4, 0.6), (0.3, 1.0), (0.3, 1.6)]),
]
LOTS = [2, 3]


def _tuned_tp(side_cfg: dict) -> float:
    """The side's tuned tp_atr_mult (tiered_exits[0] is the authority for
    these degenerate-TIERED fleet configs; crash loud if absent)."""
    exits = side_cfg.get("tiered_exits") or []
    if exits and exits[0].get("tp_atr_mult") is not None:
        return float(exits[0]["tp_atr_mult"])
    tiers = side_cfg.get("tiers") or []
    if tiers and tiers[0].get("tp_atr_mult") is not None:
        return float(tiers[0]["tp_atr_mult"])
    raise ValueError(f"no tuned tp_atr_mult in side config: {side_cfg.keys()}")


def _mutate(base: dict, ladder, lots: int) -> dict:
    """Deep-copied config with the ladder + lots applied to every enabled
    side. ladder=None keeps the tuned single rung (baseline)."""
    cfg = copy.deepcopy(base)
    for side in ("long", "short"):
        side_cfg = cfg.get(side)
        if not isinstance(side_cfg, dict) or not side_cfg.get("tiers"):
            continue
        if ladder is not None:
            tp = _tuned_tp(side_cfg)
            side_cfg["tiered_exits"] = [
                {"qty_pct": pct, "tp_atr_mult": round(tp * scale, 4)}
                for pct, scale in ladder
            ]
        for tier in side_cfg["tiers"]:
            tier["lots"] = lots
    return cfg


def _fix_predictions(cfg: dict, corrected: str | None) -> None:
    if corrected is None:
        return
    for side in ("long", "short"):
        m = cfg.get("models", {}).get(side)
        if isinstance(m, dict) and m.get("predictions_path"):
            m["predictions_path"] = corrected


def _load_preds(cfg: dict) -> pd.DataFrame:
    lp = resolve_cli_path(cfg["models"]["long"]["predictions_path"])
    sp = resolve_cli_path(cfg["models"]["short"]["predictions_path"])
    for p in (lp, sp):
        if not os.path.exists(p):
            raise FileNotFoundError(
                f"predictions CSV not found: {p} - fix the path table in "
                f"scripts/study_tranche_exits.py before rerunning"
            )
    long_df = load_predictions(lp)
    short_df = load_predictions(sp)
    lcol = _resolve_prob_column(long_df, "buy")
    scol = _resolve_prob_column(short_df, "sell")
    return (
        long_df[[lcol]].rename(columns={lcol: "prob_Buy"})
        .join(short_df[[scol]].rename(columns={scol: "prob_Sell"}),
              how="outer")
        .fillna(0.0)
    )


def _metrics(result) -> dict:
    trades = result.trades
    net = sum(t.net_pnl_dollars for t in trades)
    dd = result.max_drawdown
    dist: dict = {}
    for t in trades:
        key = getattr(t.exit_reason, "value", str(t.exit_reason))
        dist[key] = dist.get(key, 0) + 1
    return {
        "net_pnl": round(net, 2),
        "trades": len(trades),
        "win_rate": round(result.win_rate, 4),
        "profit_factor": round(result.profit_factor, 3),
        "max_dd": round(dd, 2),
        "pnl_over_dd": round(net / abs(dd), 3) if dd else float("inf"),
        "exit_dist": dist,
    }


def main() -> int:
    rows = []
    skipped = []
    for cfg_path, data_path, corrected_preds in FLEET:
        try:
            base = load_strategy_config(resolve_cli_path(cfg_path))
        except Exception as exc:
            skipped.append((cfg_path, f"config load failed: {exc}"))
            continue
        _fix_predictions(base, corrected_preds)
        sym = base.get("execution_symbol") or base.get("symbol")
        if not os.path.exists(data_path):
            skipped.append((cfg_path, f"dataset missing: {data_path}"))
            continue
        try:
            preds = _load_preds(base)
            ohlcv, ohlcv_exec = load_ohlcv_dual(data_path)
        except Exception as exc:
            skipped.append((cfg_path, f"data load failed: {exc}"))
            continue
        mult = dollars_per_point(sym)
        slip = default_slippage_points(sym)
        variants = [("baseline_singleTP", None)] + LADDERS
        for lots in LOTS:
            for vname, ladder in variants:
                cfg = _mutate(base, ladder, lots)
                bt = BacktestEngine.from_config(
                    cfg,
                    commission_per_side=COMMISSION_PER_SIDE,
                    slippage_per_side=slip,
                    contract_multiplier=mult,
                )
                result = bt.run(
                    preds, ohlcv, ohlcv_exec_df=ohlcv_exec,
                    label=f"{sym}_{vname}_lots{lots}",
                )
                m = _metrics(result)
                rows.append({"symbol": sym, "lots": lots,
                             "variant": vname, **m})
                print(f"[STUDY] {sym} lots={lots} {vname}: "
                      f"net={m['net_pnl']:+.0f} PF={m['profit_factor']} "
                      f"DD={m['max_dd']:.0f} trades={m['trades']}")

    if not rows:
        print("[STUDY] nothing ran - all symbols skipped:", skipped)
        return 1

    df = pd.DataFrame(rows)
    stamp = date.today().isoformat()
    csv_path = f"reports/tranche_exit_study_{stamp}.csv"
    md_path = f"reports/tranche_exit_study_{stamp}.md"
    os.makedirs("reports", exist_ok=True)
    df.drop(columns=["exit_dist"]).to_csv(csv_path, index=False)

    # GO/NO-GO: a ladder graduates only when it beats the SAME-LOTS
    # baseline on BOTH net PnL and PnL/DD for that symbol.
    lines = [f"# Tranche-Exit Decision-Gate Study - {stamp}", "",
             "Gate for live Stage 3 (oco-leg-race-audit_07212026_1935). "
             "A ladder counts as a WIN only when it beats the same-lots "
             "single-TP baseline on BOTH net PnL and PnL/DD.", ""]
    win_counts: dict = {name: 0 for name, _ in LADDERS}
    comparisons = 0
    for (sym, lots), grp in df.groupby(["symbol", "lots"]):
        b = grp[grp.variant == "baseline_singleTP"]
        if b.empty:
            continue
        b = b.iloc[0]
        lines.append(f"## {sym} lots={lots} "
                     f"(baseline net {b.net_pnl:+.0f}, PnL/DD "
                     f"{b.pnl_over_dd})")
        lines.append("")
        lines.append("| variant | net PnL | PF | max DD | PnL/DD | trades "
                     "| beats baseline |")
        lines.append("|---|---|---|---|---|---|---|")
        for _, r in grp.iterrows():
            if r.variant == "baseline_singleTP":
                verdict = "-"
            else:
                comparisons += 1
                won = (r.net_pnl > b.net_pnl
                       and r.pnl_over_dd > b.pnl_over_dd)
                if won:
                    win_counts[r.variant] += 1
                verdict = "YES" if won else "no"
            lines.append(
                f"| {r.variant} | {r.net_pnl:+.0f} | {r.profit_factor} | "
                f"{r.max_dd:.0f} | {r.pnl_over_dd} | {r.trades} | "
                f"{verdict} |")
        lines.append("")
    n_groups = df.groupby(["symbol", "lots"]).ngroups
    lines.append("## Verdict")
    lines.append("")
    for name, wins in win_counts.items():
        lines.append(f"- {name}: beat baseline in {wins}/{n_groups} "
                     f"symbol-lot groups")
    best = max(win_counts.values()) if win_counts else 0
    go = best > n_groups / 2
    lines.append("")
    lines.append(
        f"**{'GO' if go else 'NO-GO'}** - "
        + ("a ladder variant beat the tuned single-TP baseline in a "
           "majority of symbol-lot groups; live Stage 3 work is justified."
           if go else
           "no ladder variant beat the tuned single-TP baseline in a "
           "majority of symbol-lot groups; keep single-TP exits and leave "
           "live Stage 3 shelved (multi-lot all-in/all-out remains "
           "available without Stage 3)."))
    if skipped:
        lines.append("")
        lines.append("## Skipped (reported, never fabricated)")
        for path, why in skipped:
            lines.append(f"- {path}: {why}")
    with open(md_path, "w", encoding="ascii", errors="replace") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[STUDY] wrote {md_path} and {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
