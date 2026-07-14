# Prototype: probability-path forensics for winning vs losing trades.
# Subject: batch_20260713_005758_NG_02D_SCOUT, baseline ensembles.
import sys
import numpy as np
import pandas as pd

from src.live_execution.config_loader import load_strategy_config
from agent.backtest_engine import (
    BacktestEngine,
    load_ohlcv_dual,
    load_predictions,
    _resolve_prob_column,
)

BATCH = "reports/batch_runs/batch_20260713_005758_NG_02D_SCOUT"
DATA = "data/processed/NG_HourSet_02D.parquet"
SLIP = 0.001
MULT = 10000

EXPECTED = {"E01": 28084.94, "E02": -3595.20}

ohlcv, ex = load_ohlcv_dual(DATA)
print("exec cols:", list(ex.columns))


def analyze(ens: str) -> None:
    cfg = load_strategy_config(f"{BATCH}/configs/baseline/NG_Sharpe_{ens}_baseline_07132026.json")
    dl = load_predictions(f"{BATCH}/predictions/baseline/NG_Sharpe_{ens}_baseline_long_predictions.csv")
    ds = load_predictions(f"{BATCH}/predictions/baseline/NG_Sharpe_{ens}_baseline_short_predictions.csv")
    b = _resolve_prob_column(dl, "buy")
    s = _resolve_prob_column(ds, "sell")
    preds = (
        dl[[b]].rename(columns={b: "prob_Buy"})
        .join(ds[[s]].rename(columns={s: "prob_Sell"}), how="outer")
        .fillna(0.0)
    )
    cut = preds.index.max() - pd.DateOffset(months=cfg.get("holdout_months", 12))
    hp = preds[preds.index >= cut]
    bt = BacktestEngine.from_config(cfg, slippage_per_side=SLIP, contract_multiplier=MULT)
    t = bt.run(hp, ohlcv, ohlcv_exec_df=ex).to_dataframe()
    t["entry_time"] = pd.to_datetime(t["entry_time"])
    t["exit_time"] = pd.to_datetime(t["exit_time"])
    net = round(t.net_pnl_dollars.sum(), 2)
    print(f"\n{'='*80}\n{ens}: holdout net ${net:,.2f} (report ${EXPECTED[ens]:,.2f})  trades={len(t)}")

    thr = {"LONG": cfg["long"]["tiers"][0]["min_prob"], "SHORT": cfg["short"]["tiers"][0]["min_prob"]}
    col = {"LONG": "prob_Buy", "SHORT": "prob_Sell"}
    oppcol = {"LONG": "prob_Sell", "SHORT": "prob_Buy"}
    oppthr = {"LONG": thr["SHORT"], "SHORT": thr["LONG"]}
    pidx = preds.index

    rows = []
    for r in t.itertuples():
        side = r.signal_side
        th = thr[side]
        win = preds.loc[(pidx >= r.entry_time) & (pidx <= r.exit_time)]
        if len(win) == 0:
            continue
        p = win[col[side]]
        below = p < (th - 1e-12)
        first_dip_pos = int(np.argmax(below.values)) if below.any() else -1
        opp = win[oppcol[side]]
        rows.append(dict(
            side=side, entry=r.entry_time, exit=r.exit_time,
            exit_reason=r.exit_reason, pnl=r.net_pnl_dollars,
            commission=r.commission_dollars,
            entry_fill=r.entry_fill, win=r.net_pnl_dollars > 0,
            nbars=len(p), p_entry=p.iloc[0], p_min=p.min(),
            p_exit=p.iloc[-1], min_minus_thr=p.min() - th,
            below_frac=float(below.mean()), ever_below=bool(below.any()),
            deep_below_010=bool((p < th - 0.10).any()),
            deep_below_020=bool((p < th - 0.20).any()),
            first_dip_pos=first_dip_pos,
            dip_before_exit=bool(below.any() and first_dip_pos < len(p) - 1),
            opp_fired=bool((opp >= oppthr[side]).any()),
        ))
    d = pd.DataFrame(rows)
    print(f"joined {len(d)}/{len(t)} trades; entry-bar prob >= thr: {(d.p_entry >= d.apply(lambda r: thr[r['side']], axis=1) - 1e-9).mean():.1%}")

    pd.set_option("display.width", 200)
    for side in ["LONG", "SHORT"]:
        ds_ = d[d.side == side]
        if ds_.empty:
            continue
        print(f"\n--- {ens} {side} (thr={thr[side]:.4f}, n={len(ds_)}) ---")
        g = ds_.groupby("win").agg(
            n=("pnl", "size"), pnl_sum=("pnl", "sum"),
            p_entry_med=("p_entry", "median"), p_min_med=("p_min", "median"),
            p_exit_med=("p_exit", "median"),
            min_minus_thr_med=("min_minus_thr", "median"),
            below_frac_mean=("below_frac", "mean"),
            ever_below=("ever_below", "mean"),
            dip_before_exit=("dip_before_exit", "mean"),
            deep010=("deep_below_010", "mean"),
            deep020=("deep_below_020", "mean"),
            opp_fired=("opp_fired", "mean"),
            nbars_med=("nbars", "median"),
        ).round(3)
        print(g.to_string())
        # exit-reason x win with prob-collapse rates
        piv = ds_.groupby(["exit_reason", "win"]).agg(
            n=("pnl", "size"), pnl=("pnl", "sum"),
            ever_below=("ever_below", "mean"), deep010=("deep_below_010", "mean"),
        ).round(2)
        print(piv.to_string())
        # SL losers: confident vs collapsed
        sl = ds_[(ds_.exit_reason == "SL") & (~ds_.win)]
        if len(sl):
            conf = sl[~sl.ever_below]
            coll = sl[sl.ever_below]
            print(f"SL losers: {len(sl)}  | prob NEVER dipped below thr (stopped while confident): "
                  f"{len(conf)} (${conf.pnl.sum():,.0f}) | prob dipped first (model flipped): "
                  f"{len(coll)} (${coll.pnl.sum():,.0f})")
        # winners that spent time deep below threshold
        wdeep = ds_[ds_.win & ds_.deep_below_020]
        print(f"winners with prob >0.20 below thr at some point: {len(wdeep)}/{ds_.win.sum()} "
              f"(${wdeep.pnl.sum():,.0f})")

    # ---- counterfactual: exit at next bar open once prob < thr - delta ----
    ex_open = ex["Open"] if "Open" in ex.columns else ex[[c for c in ex.columns if c.lower().endswith("open")][0]]
    ex_idx = ex.index

    def counterfactual(delta: float, only_profitable: bool) -> tuple[float, int]:
        total = 0.0
        n_cut = 0
        ex_close = ex["Close"] if "Close" in ex.columns else ex[[c for c in ex.columns if c.lower().endswith("close")][0]]
        for r in t.itertuples():
            side_i = 1 if r.signal_side == "LONG" else -1
            th = thr[r.signal_side]
            win = preds.loc[(pidx > r.entry_time) & (pidx < r.exit_time)]  # strictly inside
            pnl = r.net_pnl_dollars
            if len(win):
                p = win[col[r.signal_side]]
                trig = p < (th - delta - 1e-12)
                if only_profitable and trig.any():
                    # require floating PnL > 0 at the trigger bar close
                    closes = ex_close.reindex(p.index)
                    floating = side_i * (closes - r.entry_fill)
                    trig = trig & (floating > 0)
                if trig.any():
                    dip_time = p.index[np.argmax(trig.values)]
                    loc = ex_idx.searchsorted(dip_time) + 1  # next bar open
                    if loc < len(ex_idx) and ex_idx[loc] <= r.exit_time:
                        fill = ex_open.iloc[loc] - side_i * SLIP
                        pnl = side_i * (fill - r.entry_fill) * MULT - r.commission_dollars
                        n_cut += 1
            total += pnl
        return total, n_cut

    print(f"\n--- {ens} counterfactual early exit on prob < thr - delta (exit next bar open) ---")
    print(f"{'delta':>6} | {'uncond PnL':>12} {'cut':>4} | {'only-if-profit PnL':>18} {'cut':>4} | actual ${net:,.0f}")
    for delta in [0.00, 0.05, 0.10, 0.15, 0.20]:
        u, nu = counterfactual(delta, False)
        c, nc = counterfactual(delta, True)
        print(f"{delta:>6.2f} | {u:>12,.0f} {nu:>4} | {c:>18,.0f} {nc:>4}")


for ens in ["E01", "E02"]:
    analyze(ens)
print("\nDONE")
