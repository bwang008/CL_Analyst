# Follow-up tickets — spawned from `fred-macro-vol-column-missing_07152026_1836`

These were identified during the audit/review of the FRED silent-partial-write bug and deliberately
scoped OUT of that ticket. Recorded here so the scope boundary is a decision, not an oversight.
Raise each as its own ticket via `/ticket-manager`.

---

## FU-1 — Live trader: feature failures die silently inside eventkit (was "F4")
**Priority: HIGH** — deferred by operator 2026-07-15 only because F1+F2 fix the *cause*; this is the
detector, and it is still absent. The next unrelated feature bug will be just as invisible.

**Problem.** `eventkit/event.py:213-218` catches all handler exceptions and only logs, so an exception in
`_on_new_bar` leaves the child a **zombie**: process alive, bars arriving, heartbeat logging "alive",
bar-arrival watchdog never firing — but no inference, no entries, no signal/EOD exits, indefinitely.
The error queue's `extract_traceback` reads child stderr **on exit**, so a zombie that never exits files
nothing. This is exactly what happened to GC on 2026-07-15 and it was caught only because a human read a log.

**Proposal.** Guard the `_on_new_bar` dispatch (`live_trader.py:4429-4449`): log CRITICAL,
`_emit_health_event("bar-processing-failed", ...)` so the error queue sees it, Telegram alert, set `_data_mute`.
Separately make the heartbeat's post-refresh validation unconditional (`:5465`, currently gated on
`if self._data_mute:`) — a 1-line change that would have caught the 17:00 corruption at 17:00, not 18:00.

**Known defect that must be designed around (found by Impact-Reviewer, not yet solved).**
`_data_mute` is cleared **only** at `:5475`, inside the `if _needs_macro` gate at `:5454`. Setting it on a
generic feature failure in a **non-macro** model would be a **permanent silent no-trade, unclearable without
a restart** — precisely the failure mode this ticket exists to prevent. There is also a set/clear evidence
mismatch: an ATR failure would be cleared by an unrelated FRED check. Must either gate on `_needs_macro`
or carry its own clear-on-next-successful-build path. **This is the design work the deferral was for.**

**Settled, do not re-litigate:** mute-not-flatten is correct. Verified `:4774` blocks only
`BUY/SELL/ENTER/SHORT`, leaving exits and resting SL/TP untouched. Flattening on a *data* error is a
trading decision made on no signal; resting SL/TP already bound the risk; muting entries is reversible.
Also note `_on_new_bar:4517` **already** has the exact catch→mute→alert→return pattern to extend in place.

---

## FU-2 — COT twin of the FRED silent-partial-write bug
**Priority: MEDIUM** — same bug class as the ticket above, untouched by it.

- `download_macro_data.py:617-620` — `main()` has the identical dead inline-writer pattern, and writes
  **without `index=False`**, unlike `save_cot_data:521`. That is a real latent bug, not just duplication.
- `_download_cot_zip:204-209` — silently drops individual years, same swallow-and-continue shape as the
  FRED per-series handler.

F3 deliberately deleted the FRED inline writer while leaving this twin two lines below it. Fix it the same
way: track failures, raise on partial, route through the real writer.

---

## FU-3 — `_build_fred_features` base-column silent skips
**Priority: MEDIUM** — this is why SI degraded *silently* while GC crashed loudly.

- `macro_features.py:627` — `if "FED_FUNDS" in df.columns:` → silent skip.
- `macro_features.py:601-603` — warn-continue for DXY / YIELD_CURVE.

Both are silent-null-defaults in disguise, violating the project's no-silent-null-defaults rule. Consequence
observed: `data_processor.py:3400` reads the same CSVs with `check_staleness=False`, so SI training/backtest
silently trained on a reduced feature set and would propagate corrupt → parquet → GCS → every sweep VM.
Only the `:588` vol-column raise (added by T4 `669b9ce`) stopped GC from doing the same.

**Impact-Reviewer's constraint:** do **not** fix by adding base-column raises inside `_build_fred_features` —
that is consumer-side creep into a core utility, and F2's producer-side gate already makes it unnecessary
for the write path. Design the guard where it belongs, and consider what should happen for historical files
that legitimately predate a series.

---

## FU-4 — Fleet preflight validates existence, not schema
**Priority: LOW-MEDIUM** — cheap, and would have caught this before a child ever started.

`fleet_runner.py:441-460` checks that the macro CSVs *exist* but never validates their headers — the corrupt
GC file sailed straight through preflight into a live child. F2's schema check
(`_FRED_BASE_COLS | {vol_label_for(instrument)}`) is directly reusable here. Do this after F2 lands so there
is one schema authority, not two.
