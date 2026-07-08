# Blueprint — screen-cost-column_07072026_1744
**Ticket Directory:** `.agents/collab/tickets/screen-cost-column_07072026_1744/`
**Branch:** `training-update` (do NOT touch `stable-fleet`)

## Change Summary
The screen ranks *predictability* (AUC) but is **blind to per-symbol transaction costs**, so it
scored thin, low-tick markets (ZC/ZS grains) as high as ES/NQ — yet their Stage-2 backtests
netted ~0 (a cost mirage: a correctly-predicted 2×ATR winner on ZC is only ~$174 gross, and
round-trip slippage+commission eats ~17% of it). Add a **cost-awareness column + flag** so the
screen flags cost mirages BEFORE the expensive Stage-2 sweep. The metric is model-independent
(economics + holdout ATR + target name only).

## Target Files
- `gcp/vm_e2e_pipeline.py` — `_screen_one_target` (median holdout ATR), `run_screen` (cost calc
  from the symbol's economics), `write_auc_report` (2 columns + cost flag + meta/legend),
  a `_tp_mult_from_name` helper
- `tests/test_target_screen_core.py` — new-metric + flag tests
- (after commit) I regenerate the 8 fleet reports with the new column — not part of the code ticket

## Required Changes

### `_screen_one_target`
- Add `atr_median_holdout` = `float(median of df_vault["EXEC_ATR_14"])`, falling back to
  `ATR_14` if `EXEC_ATR_14` absent; `nan` if neither present or vault empty. (Raw exec-price
  ATR — cost is charged on raw prices.)

### `run_screen` (has `symbol`) — compute cost per row (import from `src.core.instrument_master`)
- Module constants: `COMMISSION_RT_USD = 4.0` (documented round-trip commission estimate),
  `COST_FRAC_MAX = 0.06` (cost-mirage flag threshold).
- `_tp_mult_from_name(name)`: regex-parse the TP multiplier (the numerator of `<TP>x<SL>` in
  `TARGET_TRIPLE_<TP>x<SL>_<H>H_<DIR>`), e.g. `2x1`→2.0, `6x2`→6.0; `nan` if no match. (Compute
  from the DISPLAYED `row["target"]` name, like `reward_risk`.)
- Per row, wrapped so an unknown symbol degrades to `nan` (never crash):
  - `dpp = dollars_per_point(symbol)`, `slip = default_slippage_points(symbol)`
  - `tp_mult = _tp_mult_from_name(target)`
  - `gross_tp_usd = tp_mult * atr_median_holdout * dpp`
  - `rt_cost_usd = 2.0 * slip * dpp + COMMISSION_RT_USD`
  - `cost_frac = rt_cost_usd / gross_tp_usd` (nan-guard div-by-zero / nan inputs)
  - attach `gross_tp_usd`, `rt_cost_usd`, `cost_frac` to the row.

### `_screen_flag` — add a cost gate (new `cost_frac` arg)
Evaluate in order:
1. `n_pos < 75` → `RARE`
2. `roc >= 0.53` (would be KEEP/~tune):
   - `cost_frac` is finite AND `cost_frac > COST_FRAC_MAX` → `cost?` (predictive but cost-mirage risk)
   - else `roc >= 0.55` → `KEEP`; else `~tune`
3. else → `drop`
(nan `cost_frac` must NOT override — fall through to the ROC verdict.) ASCII tokens only.

### `write_auc_report`
- Add two columns after `EV_flr`: **`$win`** (`gross_tp_usd`, integer dollars, nan→`-`) and
  **`cost%`** (`cost_frac * 100`, 1 dp, nan→`-`). Keep the padded alignment; the flag column
  stays last. (Column order: `… | RR | EV_flr | $win | cost% | flag`.)
- Meta header: add the symbol's `$/pt`, `slippage/side`, and `commission est ($RT)`.
- Legend: document `$win` (gross $ of a full TP winner = `tp_mult × median holdout ATR × $/pt`),
  `cost%` (round-trip slippage + est. commission as % of `$win`; uses the symbol's DEFAULT
  slippage + a flat $4 RT commission — approximate; the Stage-2 backtest is authoritative), and
  `cost?` (ROC says edge but `cost% > 6%` → likely a cost mirage; ZC/ZS grains are the canonical
  case — high AUC, untradeable after costs).

## Test Requirements (TDD-tester first; RED before code)
Reuse the synthetic fixture (`symbol="CL"`, 2x1 targets → `dollars_per_point("CL")=1000`,
`default_slippage_points("CL")=0.01`, `tp_mult=2`).
- `_screen_one_target` returns finite `atr_median_holdout` > 0.
- `run_screen`: `cost_frac` finite and equals `(2*0.01*1000 + 4) / (2 * atr_median * 1000)`
  computed independently from the fixture's holdout median ATR; `gross_tp_usd` matches.
- `_tp_mult_from_name`: `2x1`→2.0, `6x2`→6.0, `8x2`→8.0, junk→nan.
- `write_auc_report`: `$win` and `cost%` headers present; a hand-built row with `cost_frac=0.20`
  and ROC 0.64 → flag `cost?`; the same row at `cost_frac=0.01` → `KEEP`; `cost_frac=nan` +
  ROC 0.64 → `KEEP` (no override); `n_pos<75` still → `RARE` regardless of cost.
- Regression: full fast suite → only the 10 known pre-existing ES01B sentinels remain.

## Out of scope
No `run_screen` signature change beyond added row keys; no Stage-2/backtest changes; the fleet
report regeneration is a manual re-run after commit.
