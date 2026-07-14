# Which E01 LONG trades does the upper rung (3.0 -> lock 2.0) actually change?
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

cfg = load_strategy_config(f"{BATCH}/configs/baseline/NG_Sharpe_E01_baseline_07132026.json")
dl = load_predictions(f"{BATCH}/predictions/baseline/NG_Sharpe_E01_baseline_long_predictions.csv")
ds = load_predictions(f"{BATCH}/predictions/baseline/NG_Sharpe_E01_baseline_short_predictions.csv")
b = _resolve_prob_column(dl, "buy")
s = _resolve_prob_column(ds, "sell")
preds = (dl[[b]].rename(columns={b: "prob_Buy"})
         .join(ds[[s]].rename(columns={s: "prob_Sell"}), how="outer").fillna(0.0))
cut = preds.index.max() - pd.DateOffset(months=12)
hp = preds[preds.index >= cut]
bt = BacktestEngine.from_config(cfg, slippage_per_side=SLIP, contract_multiplier=MULT)
t = bt.run(hp, ohlcv, ohlcv_exec_df=ex).to_dataframe()
t["entry_time"] = pd.to_datetime(t["entry_time"])
t["exit_time"] = pd.to_datetime(t["exit_time"])


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
            return side * ((px - side * SLIP) - ef) * MULT - r.commission_dollars, ("TRAIL" if rung else "SL")
        if tp_hit:
            px = gap_fill(o, tp, side, True)
            return side * ((px - side * SLIP) - ef) * MULT - r.commission_dollars, "TP"
        hh, ll = max(hh, h), min(ll, l)
        while rung < len(rungs):
            a, off = rungs[rung]
            if not ((hh >= ep + a * atr) if side == 1 else (ll <= ep - a * atr)):
                break
            sl = ep + off * atr if side == 1 else ep - off * atr
            rung += 1
    return r.net_pnl_dollars, r.exit_reason


for label, tag, rungs in [
    ("E01 LONG ladder [(2.55,0.51),(3.0,2.0)]", "LONG", [(2.55, 0.51), (3.0, 2.0)]),
    ("E01 SHORT ladder [(1.45,0.29),(3.0,1.0)]", "SHORT", [(1.45, 0.29), (3.0, 1.0)]),
]:
    print(f"\n{label} — changed trades:")
    for r in t[t.signal_side == tag].itertuples():
        net, reason = replay(r, rungs)
        d = net - r.net_pnl_dollars
        if abs(d) > 0.01:
            print(f"  {r.entry_time}  orig {r.exit_reason:>12} ${r.net_pnl_dollars:>9,.0f} "
                  f"-> {reason:>5} ${net:>9,.0f}  delta ${d:>8,.0f}")
