# Full live-period decision impact — jit-roll-ratio-empty_07102026_1453

Measured 2026-07-11 (pre-relaunch), raw-basis vs training-basis inference on ALL 113 live
1h bars 2026-07-06 00:00 → 2026-07-10 20:00, deployed model pickles, joined against
trade_ledger EXECUTE rows. (GC evaluated with GC02B/HourSet_02B per the updated manifest;
GC01B over the last 48 bars separately confirmed 0 flips — GC is threshold-dominated either way.)

| Symbol | Long flips (phantom/missed) | Short flips (phantom/missed) | EXECUTEs | Trades on flipped bars |
|---|---|---|---|---|
| CL | 0/113 | 4 (4 phantom / 0) | 7 | **1** — Jul-6 09:00 SELL entered on a phantom short signal |
| ES | 0/113 | 0/113 | 10 | 0 |
| NG | 6 (5 phantom / 1 missed) | 14 (4 phantom / **10 missed**) | 8 | **3** — Jul-7 12:00 BUY (correct signal was SHORT), Jul-8 16:00 BUY (phantom long), Jul-9 13:00 BUY (short-side flip same bar) |
| GC | 0/113 | 0/113 | 10 | 0 |
| SI | 2 (1/1) | 0/113 | 4 | 0 |

NG missed-short bars: Jul-6 03:00, Jul-7 12:00, and the 8-hour block Jul-9 02:00–09:00
(during NG's collapse) — the largest attributable PnL damage of the week.

Duration of exposure: 100% of fleet inference since launch 2026-07-06 (roll_history empty
since metadata creation; the JIT adjuster no-op'd on every bar). CL's exposure extends back
through its solo deployment (~2026-06-26) with the same seam inventory. Corrected basis is
on disk as of the 2026-07-11 02:47 migration; activates at next fleet launch.
