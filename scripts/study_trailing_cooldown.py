"""Only-original-SL cooldown decision-gate study
(ticket trailing-sl-no-cooldown_07222026_2050).

Operator decision 2026-07-22: the post-exit re-entry cooldown should arm ONLY
on an original stop-loss exit — trailing-stop (TRAILING_BE), TP, time-barrier
and flatten exits no longer block re-entry. The engine + live wiring implement
the new rule; THIS study is the backtest-first validation gate: per fleet
symbol, old rule (flavor-blind arming) vs new rule (only-SL arming), all else
identical. The per-side cooldown_bars values were Optuna-chosen UNDER the old
rule, so this is exactly the re-validation the blueprint requires before any
live deploy.

Method
- Same fleet table, economics and data plumbing as
  scripts/study_tranche_exits.py (corrected predictions paths included).
- Arm A "old_flavor_blind": `resolve_cooldown_arming` forced to "all" in
  BOTH consuming namespaces (agent.backtest_engine + execution_models) —
  the pre-ticket flavor-blind behavior (also the config default).
- Arm B "new_only_SL": `resolve_cooldown_arming` forced to "sl_only" —
  the mode is FORCED per arm regardless of what the fleet configs declare,
  so the study stays a clean A/B after the opt-in fields landed in the
  live configs.
- Metrics: full-period net PnL / PF / max DD / PnL-over-DD / trades / exit
  distribution, plus a last-12-months tail slice (net + trades) matching the
  current holdout convention's window length.

Run:  conda run -n trader python scripts/study_trailing_cooldown.py
Output: reports/trailing_cooldown_study_<date>.md + .csv (repo-local).
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.backtest_engine as be  # noqa: E402
import src.live_execution.strategies.execution_models as em  # noqa: E402
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
TAIL_MONTHS = 12

# (config path, dataset parquet, corrected predictions path or None=config OK)
# — copied from scripts/study_tranche_exits.py (validate-parity Pitfall #1).
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
                f"scripts/study_trailing_cooldown.py before rerunning"
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


def _metrics(result, tail_start) -> dict:
    trades = result.trades
    net = sum(t.net_pnl_dollars for t in trades)
    dd = result.max_drawdown
    dist: dict = {}
    for t in trades:
        key = getattr(t.exit_reason, "value", str(t.exit_reason))
        dist[key] = dist.get(key, 0) + 1
    tail = [t for t in trades if t.entry_dt >= tail_start]
    return {
        "net_pnl": round(net, 2),
        "trades": len(trades),
        "win_rate": round(result.win_rate, 4),
        "profit_factor": round(result.profit_factor, 3),
        "max_dd": round(dd, 2),
        "pnl_over_dd": round(net / abs(dd), 3) if dd else float("inf"),
        "net_12mo": round(sum(t.net_pnl_dollars for t in tail), 2),
        "trades_12mo": len(tail),
        "exit_dist": dist,
    }


class _ForceMode:
    """Force every side's cooldown_arming resolution to one mode, in both
    consuming namespaces, for a clean A/B regardless of config declarations."""

    def __init__(self, mode: str):
        self._mode = mode

    def __enter__(self):
        self._be = be.resolve_cooldown_arming
        self._em = em.resolve_cooldown_arming
        forced = self._mode
        be.resolve_cooldown_arming = lambda cfg, side_key: forced
        em.resolve_cooldown_arming = lambda cfg, side_key: forced
        return self

    def __exit__(self, *exc):
        be.resolve_cooldown_arming = self._be
        em.resolve_cooldown_arming = self._em
        return False


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
        tail_start = ohlcv.index.max() - pd.DateOffset(months=TAIL_MONTHS)

        for arm, forced_mode in (("old_flavor_blind", "all"),
                                 ("new_only_SL", "sl_only")):
            # Construction AND run under the forced mode: from_config
            # resolves the modes at engine build time.
            with _ForceMode(forced_mode):
                bt = BacktestEngine.from_config(
                    base,
                    commission_per_side=COMMISSION_PER_SIDE,
                    slippage_per_side=slip,
                    contract_multiplier=mult,
                )
                result = bt.run(preds, ohlcv, ohlcv_exec_df=ohlcv_exec,
                                label=f"{sym}_{arm}")
            m = _metrics(result, tail_start)
            rows.append({"symbol": sym, "arm": arm, **m})
            print(f"[STUDY] {sym} {arm}: net={m['net_pnl']:+.0f} "
                  f"PF={m['profit_factor']} DD={m['max_dd']:.0f} "
                  f"trades={m['trades']} 12mo={m['net_12mo']:+.0f} "
                  f"exits={m['exit_dist']}")

    if not rows:
        print("[STUDY] nothing ran - all symbols skipped:", skipped)
        return 1

    df = pd.DataFrame(rows)
    stamp = date.today().isoformat()
    csv_path = f"reports/trailing_cooldown_study_{stamp}.csv"
    md_path = f"reports/trailing_cooldown_study_{stamp}.md"
    os.makedirs("reports", exist_ok=True)
    df.drop(columns=["exit_dist"]).to_csv(csv_path, index=False)

    lines = [f"# Only-Original-SL Cooldown Study - {stamp}", "",
             "Gate for trailing-sl-no-cooldown_07222026_2050. Per symbol: "
             "old flavor-blind cooldown arming vs new only-original-SL "
             "arming, identical configs/data/economics. The new rule can "
             "only ADD trades (formerly-blocked re-entries), so the "
             "question is whether those added trades help or hurt.", ""]
    wins = 0
    n_syms = 0
    for sym, grp in df.groupby("symbol", sort=False):
        old = grp[grp.arm == "old_flavor_blind"].iloc[0]
        new = grp[grp.arm == "new_only_SL"].iloc[0]
        n_syms += 1
        better = (new.net_pnl > old.net_pnl
                  and new.pnl_over_dd >= old.pnl_over_dd)
        if better:
            wins += 1
        lines.append(f"## {sym}")
        lines.append("")
        lines.append("| arm | net PnL | PF | max DD | PnL/DD | trades | "
                     "12mo net | 12mo trades |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in (old, new):
            lines.append(
                f"| {r.arm} | {r.net_pnl:+.0f} | {r.profit_factor} | "
                f"{r.max_dd:.0f} | {r.pnl_over_dd} | {r.trades} | "
                f"{r.net_12mo:+.0f} | {r.trades_12mo} |")
        lines.append("")
        lines.append(
            f"Delta: net {new.net_pnl - old.net_pnl:+.0f}, trades "
            f"{new.trades - old.trades:+d}, 12mo net "
            f"{new.net_12mo - old.net_12mo:+.0f} -> "
            f"{'IMPROVES' if better else 'does NOT improve'} "
            f"(net AND PnL/DD test)")
        lines.append("")

    lines.append("## Verdict")
    lines.append("")
    lines.append(f"- new rule improves {wins}/{n_syms} symbols on "
                 f"net PnL + PnL/DD")
    lines.append("")
    lines.append(
        "Recommendation basis: the operator decided the RULE on principle "
        "(winners should not block re-entry); this study reports whether "
        "the historical evidence supports deploying it with the CURRENT "
        "Optuna cooldown_bars values, or whether those values should be "
        "re-searched first. Live deploy remains an operator decision.")

    if skipped:
        lines.append("")
        lines.append("## Skipped")
        for cfg_path, why in skipped:
            lines.append(f"- {cfg_path}: {why}")

    with open(md_path, "w", encoding="ascii", errors="replace") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"[STUDY] wrote {md_path} and {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
