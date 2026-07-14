# Test the user's FIXED rung-2 rule on NG 02D E01/E02:
#   trigger2 = a1 + 0.5*(TP - a1)   (midpoint of remaining distance)
#   lock2    = a1                   (stop shifts to the previous trigger level)
# plus a softer variant lock2 = 0.8*a1, and a 3-rung extension of the same rule.
import pandas as pd

from src.live_execution.config_loader import load_strategy_config
from agent.backtest_engine import (
    BacktestEngine, load_ohlcv_dual, load_predictions, _resolve_prob_column,
)

BATCH = "reports/batch_runs/batch_20260713_005758_NG_02D_SCOUT"
SLIP = 0.001
MULT = 10000

ohlcv, ex = load_ohlcv_dual("data/processed/NG_HourSet_02D.parquet")
ex_idx = ex.index
EXO, EXH, EXL = ex["Open"].values, ex["High"].values, ex["Low"].values


def gap_fill(o, tg, side, is_tp):
    if side == 1:
        return (o if o >= tg else tg) if is_tp else (o if o <= tg else tg)
    return (o if o <= tg else tg) if is_tp else (o if o >= tg else tg)


def replay(r, rungs):
    side = 1 if r.signal_side == "LONG" else -1
    atr = r.atr_at_entry
    ep, ef = r.entry_price, r.entry_fill
    tp, sl = r.initial_tp_price, r.initial_sl_price
    i0 = ex_idx.searchsorted(r.entry_time)
    i1 = ex_idx.searchsorted(r.exit_time)
    hh, ll = EXH[i0], EXL[i0]
    rung = 0
    for i in range(i0 + 1, i1 + 1):
        o, h, l = EXO[i], EXH[i], EXL[i]
        tp_hit = (h >= tp) if side == 1 else (l <= tp)
        sl_hit = (l <= sl) if side == 1 else (h >= sl)
        if sl_hit:
            px = gap_fill(o, sl, side, False)
            return side * ((px - side * SLIP) - ef) * MULT - r.commission_dollars
        if tp_hit:
            px = gap_fill(o, tp, side, True)
            return side * ((px - side * SLIP) - ef) * MULT - r.commission_dollars
        hh, ll = max(hh, h), min(ll, l)
        while rung < len(rungs):
            a, off = rungs[rung]
            if not ((hh >= ep + a * atr) if side == 1 else (ll <= ep - a * atr)):
                break
            sl = ep + off * atr if side == 1 else ep - off * atr
            rung += 1
    return r.net_pnl_dollars


def run_actual(ens):
    cfg = load_strategy_config(f"{BATCH}/configs/baseline/NG_Sharpe_{ens}_baseline_07132026.json")
    dl = load_predictions(f"{BATCH}/predictions/baseline/NG_Sharpe_{ens}_baseline_long_predictions.csv")
    ds = load_predictions(f"{BATCH}/predictions/baseline/NG_Sharpe_{ens}_baseline_short_predictions.csv")
    b = _resolve_prob_column(dl, "buy")
    s = _resolve_prob_column(ds, "sell")
    preds = (dl[[b]].rename(columns={b: "prob_Buy"})
             .join(ds[[s]].rename(columns={s: "prob_Sell"}), how="outer").fillna(0.0))
    cut = preds.index.max() - pd.DateOffset(months=cfg.get("holdout_months", 12))
    hp = preds[preds.index >= cut]
    bt = BacktestEngine.from_config(cfg, slippage_per_side=SLIP, contract_multiplier=MULT)
    t = bt.run(hp, ohlcv, ohlcv_exec_df=ex).to_dataframe()
    t["entry_time"] = pd.to_datetime(t["entry_time"])
    t["exit_time"] = pd.to_datetime(t["exit_time"])
    return cfg, t


def rule_2rung(a1, o1, tp, lock_frac=1.0):
    a2 = a1 + 0.5 * (tp - a1)
    return [(a1, o1), (a2, lock_frac * a1)]


def rule_3rung(a1, o1, tp):
    # keep halving the remaining distance; each new lock = previous trigger
    a2 = a1 + 0.5 * (tp - a1)
    a3 = a2 + 0.5 * (tp - a2)
    return [(a1, o1), (a2, a1), (a3, a2)]


for ens in ["E01", "E02"]:
    cfg, t = run_actual(ens)
    print(f"\n{'='*84}\n{ens}: actual holdout net ${t.net_pnl_dollars.sum():,.2f}")
    for side_name, tag in [("long", "LONG"), ("short", "SHORT")]:
        c = cfg[side_name]
        a1, o1, tp = float(c["trailing_atr_mult"]), float(c["trailing_sl_atr_offset"]), float(c["tp_atr_mult"])
        sub = t[t.signal_side == tag]
        actual = sub.net_pnl_dollars.sum()
        variants = {
            "fixed rule (lock2 = a1)": rule_2rung(a1, o1, tp, 1.0),
            "softer   (lock2 = 0.8*a1)": rule_2rung(a1, o1, tp, 0.8),
            "3-rung same rule": rule_3rung(a1, o1, tp),
        }
        print(f"\n {ens} {tag}: TP {tp}, rung1 ({a1}, {o1}); side actual ${actual:,.0f}")
        for name, rungs in variants.items():
            tot = 0.0
            n_ch = imp = wor = 0
            for r in sub.itertuples():
                net = replay(r, rungs)
                tot += net
                d = net - r.net_pnl_dollars
                if abs(d) > 0.01:
                    n_ch += 1
                    imp += d > 0
                    wor += d < 0
            r2 = ", ".join(f"({a:.2f}->{o:.2f})" for a, o in rungs[1:])
            print(f"   {name:<26} rungs2+: {r2:<32} new ${tot:>9,.0f}  delta ${tot-actual:>8,.0f}  changed {n_ch:>3} (+{imp}/-{wor})")
print("\nDONE")
