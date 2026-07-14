# Prototype: two-rung trailing-stop ladder counterfactual.
# Replays each actual trade bar-by-bar with engine-identical exit semantics:
#   - TP/SL checked intrabar, SL wins ties, gap fill at open
#   - ladder advance AFTER exit checks (moved stop effective next bar)
#   - moved SL relative to entry_price (unrounded); initial TP/SL from ledger
# Validation: single-rung ladder (= existing config) must reproduce every trade.
import numpy as np
import pandas as pd

from src.live_execution.config_loader import load_strategy_config
from agent.backtest_engine import (
    BacktestEngine, load_ohlcv_dual, load_predictions, _resolve_prob_column,
)

BATCH = "reports/batch_runs/batch_20260713_005758_NG_02D_SCOUT"
DATA = "data/processed/NG_HourSet_02D.parquet"
SLIP = 0.001
MULT = 10000

ohlcv, ex = load_ohlcv_dual(DATA)
ex_idx = ex.index
EXO, EXH, EXL = ex["Open"].values, ex["High"].values, ex["Low"].values


def run_actual(ens):
    cfg = load_strategy_config(f"{BATCH}/configs/baseline/NG_Sharpe_{ens}_baseline_07132026.json")
    dl = load_predictions(f"{BATCH}/predictions/baseline/NG_Sharpe_{ens}_baseline_long_predictions.csv")
    ds = load_predictions(f"{BATCH}/predictions/baseline/NG_Sharpe_{ens}_baseline_short_predictions.csv")
    b = _resolve_prob_column(dl, "buy"); s = _resolve_prob_column(ds, "sell")
    preds = (dl[[b]].rename(columns={b: "prob_Buy"})
             .join(ds[[s]].rename(columns={s: "prob_Sell"}), how="outer").fillna(0.0))
    cut = preds.index.max() - pd.DateOffset(months=cfg.get("holdout_months", 12))
    hp = preds[preds.index >= cut]
    bt = BacktestEngine.from_config(cfg, slippage_per_side=SLIP, contract_multiplier=MULT)
    t = bt.run(hp, ohlcv, ohlcv_exec_df=ex).to_dataframe()
    t["entry_time"] = pd.to_datetime(t["entry_time"])
    t["exit_time"] = pd.to_datetime(t["exit_time"])
    return cfg, t


def gap_fill(bar_open, target, side, is_tp):
    if side == 1:
        if is_tp:
            return bar_open if bar_open >= target else target
        return bar_open if bar_open <= target else target
    else:
        if is_tp:
            return bar_open if bar_open <= target else target
        return bar_open if bar_open >= target else target


def replay_trade(r, rungs):
    """Replay one trade with ladder `rungs` = [(act_mult, off_mult), ...] ascending.
    Returns (net_pnl, exit_reason, rung_at_exit)."""
    side = 1 if r.signal_side == "LONG" else -1
    atr = r.atr_at_entry
    ep, ef = r.entry_price, r.entry_fill
    tp, sl = r.initial_tp_price, r.initial_sl_price
    i0 = ex_idx.searchsorted(r.entry_time)   # entry bar
    i1 = ex_idx.searchsorted(r.exit_time)    # original exit bar
    hh, ll = EXH[i0], EXL[i0]
    rung = 0
    for i in range(i0 + 1, i1 + 1):
        o, h, l = EXO[i], EXH[i], EXL[i]
        tp_hit = (h >= tp) if side == 1 else (l <= tp)
        sl_hit = (l <= sl) if side == 1 else (h >= sl)
        if sl_hit:  # SL wins ties
            px = gap_fill(o, sl, side, is_tp=False)
            fill = px - side * SLIP
            net = side * (fill - ef) * MULT - r.commission_dollars
            return net, ("TRAILING" if rung > 0 else "SL"), rung
        if tp_hit:
            px = gap_fill(o, tp, side, is_tp=True)
            fill = px - side * SLIP
            net = side * (fill - ef) * MULT - r.commission_dollars
            return net, "TP", rung
        hh, ll = max(hh, h), min(ll, l)
        while rung < len(rungs):
            a, off = rungs[rung]
            trig = (hh >= ep + a * atr) if side == 1 else (ll <= ep - a * atr)
            if not trig:
                break
            sl = ep + off * atr if side == 1 else ep - off * atr
            rung += 1
    return r.net_pnl_dollars, r.exit_reason, rung  # original exit stands


def side_cfg(cfg, side):
    c = cfg[side]
    return float(c["trailing_atr_mult"]), float(c["trailing_sl_atr_offset"])


for ens in ["E01", "E02"]:
    cfg, t = run_actual(ens)
    print(f"\n{'='*90}\n{ens}: actual holdout net ${t.net_pnl_dollars.sum():,.2f}  trades={len(t)}")

    # --- validation: single existing rung must reproduce every trade ---
    bad = 0
    for r in t.itertuples():
        side = "long" if r.signal_side == "LONG" else "short"
        a2, o2 = side_cfg(cfg, side)
        net, reason, _ = replay_trade(r, [(a2, o2)])
        if abs(net - r.net_pnl_dollars) > 0.01:
            bad += 1
            if bad <= 3:
                print(f"  VALIDATION MISMATCH {r.entry_time} {r.signal_side} "
                      f"orig={r.net_pnl_dollars:.2f}({r.exit_reason}) replay={net:.2f}({reason})")
    print(f"validation (existing single rung): {len(t)-bad}/{len(t)} trades reproduced exactly")
    if bad:
        print("  -> replay semantics diverge; treat sweep as approximate")

    # --- rung-1 sweep, per side independently ---
    rows = []
    for side_name, tag in [("long", "LONG"), ("short", "SHORT")]:
        a2, o2 = side_cfg(cfg, side_name)
        sub = t[t.signal_side == tag]
        if sub.empty:
            continue
        actual = sub.net_pnl_dollars.sum()
        for a1 in [0.5, 1.0, 1.5, 2.0, 3.0, 4.0]:
            if a1 >= a2:
                continue
            for o1 in [0.0, 0.25, 0.5, 1.0, 2.0]:
                if o1 >= a1 or o1 >= o2:
                    continue
                tot = 0.0; n_r1 = 0; rescued = 0; resc_d = 0.0; trunc = 0; trunc_d = 0.0
                for r in sub.itertuples():
                    net, reason, rung = replay_trade(r, [(a1, o1), (a2, o2)])
                    tot += net
                    d = net - r.net_pnl_dollars
                    if abs(d) > 0.01:
                        n_r1 += 1
                        if d > 0: rescued += 1; resc_d += d
                        else: trunc += 1; trunc_d += d
                rows.append(dict(side=tag, a1=a1, o1=o1, a2=a2, o2=o2,
                                 actual=actual, new=tot, delta=tot - actual,
                                 changed=n_r1, improved=rescued, imp_d=resc_d,
                                 worsened=trunc, wors_d=trunc_d))
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    for tag in ["LONG", "SHORT"]:
        d = df[df.side == tag]
        if d.empty:
            continue
        d = d.sort_values("delta", ascending=False)
        print(f"\n--- {ens} {tag} LOWER rung-1 sweep (existing rung2: act {d.a2.iloc[0]}, off {d.o2.iloc[0]}; actual ${d.actual.iloc[0]:,.0f}) ---")
        out = d[["a1", "o1", "new", "delta", "changed", "improved", "imp_d", "worsened", "wors_d"]].copy()
        for c in ["new", "delta", "imp_d", "wors_d"]:
            out[c] = out[c].round(0)
        print(out.to_string(index=False))

    # --- UPPER rung sweep: keep existing rung as rung1, add higher rung above it ---
    rows_hi = []
    for side_name, tag in [("long", "LONG"), ("short", "SHORT")]:
        a_ex, o_ex = side_cfg(cfg, side_name)
        tp_mult = float(cfg[side_name]["tp_atr_mult"])
        sub = t[t.signal_side == tag]
        if sub.empty:
            continue
        actual = sub.net_pnl_dollars.sum()
        for a_hi in [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]:
            if not (a_ex < a_hi < tp_mult):
                continue
            for o_hi in [0.5, 1.0, 2.0, 3.0, 4.0, 5.0]:
                if not (o_ex < o_hi < a_hi):
                    continue
                tot = 0.0; n_ch = 0; imp = 0; imp_d = 0.0; wor = 0; wor_d = 0.0
                for r in sub.itertuples():
                    net, reason, rung = replay_trade(r, [(a_ex, o_ex), (a_hi, o_hi)])
                    tot += net
                    dd = net - r.net_pnl_dollars
                    if abs(dd) > 0.01:
                        n_ch += 1
                        if dd > 0: imp += 1; imp_d += dd
                        else: wor += 1; wor_d += dd
                rows_hi.append(dict(side=tag, a_hi=a_hi, o_hi=o_hi, actual=actual,
                                    new=tot, delta=tot - actual, changed=n_ch,
                                    improved=imp, imp_d=imp_d, worsened=wor, wors_d=wor_d))
    dfh = pd.DataFrame(rows_hi)
    for tag in ["LONG", "SHORT"]:
        d = dfh[dfh.side == tag] if not dfh.empty else pd.DataFrame()
        if d.empty:
            print(f"\n--- {ens} {tag} UPPER rung sweep: no room above existing rung (act too close to TP) ---")
            continue
        d = d.sort_values("delta", ascending=False)
        a_ex, o_ex = side_cfg(cfg, "long" if tag == "LONG" else "short")
        print(f"\n--- {ens} {tag} UPPER rung sweep (rung1 = existing act {a_ex}, off {o_ex}; actual ${d.actual.iloc[0]:,.0f}) ---")
        out = d[["a_hi", "o_hi", "new", "delta", "changed", "improved", "imp_d", "worsened", "wors_d"]].copy()
        for c in ["new", "delta", "imp_d", "wors_d"]:
            out[c] = out[c].round(0)
        print(out.to_string(index=False))
print("\nDONE")
