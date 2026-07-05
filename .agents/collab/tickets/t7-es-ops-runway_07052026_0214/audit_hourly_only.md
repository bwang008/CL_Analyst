# T7 AMENDMENT Audit — Hourly-Only Mode (`t7-hourly-only-amendment`)
**Ticket:** `t7-es-ops-runway_07052026_0214` | **Auditor:** Ticket-Auditor | 2026-07-05 PT
**HEAD:** `03218af` (branch `development`; verified src/ + scripts/ working tree == HEAD)
**Supersedes:** `audit.md` §2.2 (5m seed provisioning) and §8 Q1 — user ruling: NO 5m data
acquisition (no Databento purchase, no IBKR 5m pull). This audit designs the alternative:
for `bar_size: 1h` configs, skip the 5m DataManager + 5m brain subscription entirely.

**Verdict: FEASIBLE with ONE hard code blocker (`_check_trailing_stop` reads
`rolling_df_5m.iloc[-1]` unconditionally — AttributeError if the 5m frame is None) plus a
watchdog re-point. Recommended mechanism: explicit `live_config.enable_5m_stream`
(optional, default `true`, loudly logged) — CL byte-identical with zero config edits; ES
opts out via a 1-field surgical patch (T6 precedent). Severity MEDIUM. Regression: NO.**

---

## 1. Complete consumer map of the 5m artifacts for a 1h config at HEAD (Q1)

All references verified by grep + read at HEAD. "1h-config relevance" = what the artifact
actually does when `bar_size == "1h"` (ES01B and CL HS14B are both 1h).

### 1.1 `data_manager_5m` (always constructed, `live_trader.py:373-383`)
| Site | What it does for a 1h config |
|---|---|
| `:373-383` `__init__` | Constructed unconditionally with `seed_5m` (= `raw/{sym}-5m_bk.csv`, `data_manager.py:88`). Construction is disk-IO-free; only `initialize()` requires the seed. |
| `:726-728` start() Step 6 | `front_month_id` assignment (rollover-ratio tracking for the 5m cache). |
| `:1955` `_warm_start()` | `initialize()` — the ONLY place the seed hard-fail (`data_manager.py:293-308`) fires. Also runs the 5m IBKR backfill, **updates the 5m master training ledger** (`data_manager.py:336-338` → `{sym}_continuous_master.parquet`), saves roll metadata, cache backup. For CL this IS the live 5m training-data accrual pipeline. |
| `:2407` rollover | `front_month_id` update. |
| `:2618-2621` reconnect backfill | `append_bar` + `save_cache` for stitched 5m bars — **already `is not None`-guarded**. |
| `:2824` `_on_bar_update_5m` | `append_bar` (cache flush every 12 bars, `data_manager.py:380-383`). |
| `:889` `_shutdown` | `save_cache()` — NOT None-guarded (needs a guard). Note: `save_cache` on an un-initialized manager is already safe (`data_manager.py:392-394` warns + returns), but a None manager raises. |

### 1.2 `rolling_df_5m`
| Site | Reader | 1h-config relevance |
|---|---|---|
| `:1100-1104` | `_check_trailing_stop` — `last_bar = self.rolling_df_5m.iloc[-1]` High/Low extremes | **THE hard blocker.** Called from BOTH callbacks (5m `:2832-2833`, 1h `:2884-2885` post-957ced7). Read is UNGUARDED — `None.iloc` → AttributeError. At HEAD this is correct (5m frame is always fresher); in hourly-only it must read the 1h frame. |
| `:1468-1509` | `_recover_inherited_position` — bars-held estimate reference, entry-bar-time fallback, `_highest_high/_lowest_low` seeding | `is not None`-guarded, but with a None 5m frame the trailing extremes are NOT seeded (stay 0.0/inf) → must read the primary frame. |
| `:1955-1972` | `_warm_start` init + `_last_bar_time_5m` | Gate whole block in hourly-only. |
| `:2050-2051` | `_warmup_inference_state` source fallback | 1h configs use `data_manager_1h.get_ratio_adjusted_df()` (`:2048-2049`) — 5m fallback is 5m-config-only. Unaffected. |
| `:2609-2623` | reconnect backfill stitch | Gated on `_last_bar_time_5m is not None` (`:2583`) → naturally skipped when None. |
| `:2820-2822` | 5m callback append | Never fires without the subscription. |
| `:4073-4074` | `_check_naked_position` flatten price | `is not None`-guarded; falls back to 0.0 for a MARKET order (price unused, `close_position(exit_mode="market")`). Safe as-is; cosmetic improvement = primary frame. |
| `scripts/livetest_engine.py:213-241, 387-406` | Parity harness mirrors 1h bars INTO `rolling_df_5m` precisely because of `:1100` | See §6. |

### 1.3 `_live_bars_5m` + `_on_bar_update_5m`
Subscription: `:2244-2251` (sync `_subscribe`), `:2518-2525` (async `_deferred_resubscribe`)
— brain-symbol ContFuture, `bar_size="5 mins"`, keepUpToDate. Cancel/reset touchpoints:
`:842` (restart reset), `:872-876` (shutdown), `:2498-2502` (deferred cancel), `:2722-2727`
(sync resubscribe cancel) — **all four are `is not None`-guarded → None-clean already**.

`_on_bar_update_5m` (`:2784-2837`) drives, in order:
1. dedup gate + `_last_bar_time_5m` update (`:2809-2811`) — feeds watchdog/heartbeat/backfill anchor;
2. `NEW 5M BAR:` log + `_last_5m_bar_log` (`:2813-2818`) — Telegram heartbeat "Recent Activity" block (`:575-576`; skipped when empty string — None-clean);
3. `rolling_df_5m` append (`:2820-2822`);
4. `data_manager_5m.append_bar` (`:2824`) — 5m cache accrual;
5. `telemetry.log_bar` (`:2826-2830`) — the `market_bars` table. **In hourly-only, `market_bars` receives NOTHING** (the 1h callback does not call `log_bar`); `raw_front_month_bars` still fills via the hands stream. Telemetry-only gap — see §7 design note;
6. `_check_trailing_stop` under `_ledger_lock` (`:2832-2833`) — 5m-granularity trailing;
7. inference — `if self._bar_size == "5m"` ONLY (`:2835-2837`). **Never for a 1h config.**

### 1.4 Front-month "hands" stream (`_front_month_bars`, `_on_front_month_bar_update`)
- Subscribe: `:2271-2278` (`_subscribe_front_month`) — `subscribe_live_bars(symbol=execution_symbol, continuous=False, bar_size="5 mins")`. Adapter path (`adapters/ibkr_data_feed.py:126-149`): `get_front_month_contract` → qualify → subscribe. **NO DataManager, NO seed, NO dependency on the brain 5m stream** — it is a fully independent live subscription. It CAN and MUST run in hourly-only mode.
- Callback (`:2753-2782`): `telemetry.log_raw_bar` (raw_front_month_bars), `RAW BAR` debug log, and `_front_month_last_close = float(new_bar.close)`.
- **EVERY reader of `_front_month_last_close`** (exhaustive grep):
  - `:2391` — rollover: reset to None (staleness protection);
  - `:3013-3016` — `_on_new_bar`: `current_price = self._front_month_last_close` (fallback: brain-frame close). This `current_price` is **ORDER-PRICING-CRITICAL**: it flows into `strategy.evaluate(current_price=...)` (`:3194` — computes `signal.tp_price/sl_price`), the marketable-limit ENTRY `limit_price` (`:3425`), TP/SL offset computation (`:3411-3412`), `_entry_price` fallback (`:3441`), `_check_time_barrier(current_price=...)` (`:3055-3059` → time-barrier EXIT `close_position(..., current_price=...)` at `:1329/:1342` — ES01B uses `exit_mode: marketable_limit`, so barrier exits are PRICED off it), plus [PNL] display (`:3174`), shadow-replay (`:3496`) and telemetry (`:3544`).
- **Conclusion: the hands stream STAYS in hourly-only mode.** Without it, every marketable-limit price would fall back to the continuous-contract close — exactly the rollover-window mispricing the Two-Stream design exists to avoid.

### 1.5 `_check_stale_bars` (`:3921-3986`)
Watchdogs ONLY `_last_bar_time_5m` (`:3944`, getattr-defaulted) against
`_STALE_BAR_THRESHOLD_MINUTES = 15` (`:149`), gated on `market_status != "OPEN"` and the T5
session-open anchor. Two consequences:
- **Hourly-only with no re-point = watchdog permanently DEAD** (`_last_bar_time_5m` stays None → returns False forever). Must monitor `_last_bar_time_1h` instead.
- **HEAD irony:** for a 1h config the INFERENCE stream (1h) is unwatched today — a dead 1h subscription with a live 5m one is invisible to the watchdog. Re-pointing at 1h gives ES *stronger* protection of the stream that matters than CL has. (CL residual: 1h-stream death still unwatched while 5m flows — pre-existing, out of scope, micro-ticket candidate.)

### 1.6 `_virtual_ledger` (`:439-442`, `:3236-3248`)
`{"5m": 0, "1h": 0}`; only `_on_new_bar(stream=...)` writes it, and for a 1h config only
`"1h"` is ever written (5m inference is bar_size-gated). The `"5m"` key is a permanent 0 at
HEAD for 1h configs — hourly-only changes NOTHING. Log line prints both keys — unchanged.

### 1.7 `_warmup_inference_state` (`:2040-2147`)
1h configs source from `data_manager_1h.get_ratio_adjusted_df()`; needs exec-side
`get_position`/`get_account_summary` only. **Zero 5m dependency.** Unaffected.

---

## 2. Opt-in mechanism decision (Q2)

### Options evaluated
| Mechanism | Verdict | Why |
|---|---|---|
| **bar_size-driven** (all 1h configs lose the 5m stream) | **REJECT** | Changes CL HS14B production: (a) trailing granularity 5m→1h (activation timing changes → ledger changes); (b) watchdog stream/threshold change; (c) **stops the live 5m master-ledger accrual** (`cl_continuous_master.parquet` — the training-data by-product, §1.1); (d) empties `market_bars` telemetry. Violates constraint "CL byte-identical without config edits". |
| **Seed-presence-driven** (no seed → hourly-only) | **REJECT** | Textbook silent behavior fork: a mislaid `cl-5m_bk.csv` would silently flip production CL to 1h trailing instead of crashing. Directly violates the no-silent-defaults rule AND deletes the No-Silent-Bootstrap hard fail's purpose. |
| **`live_config.enable_5m_stream`, REQUIRED for non-CL** | Runner-up | Strictest reading of no-silent-defaults, but: ES01B's `live_config` at HEAD carries only `client_id/entry_mode/exit_mode` (verified) — a required field breaks the T6-frozen shipped config anyway, and neither generator script writes `live_config` AT ALL (grep: 0 hits in `generate_ensemble_artifacts.py` and `batch_post_optimizer.py` — it is inherited from the deep-copied base config), so REQUIRED-for-non-CL forces a T8 generator/base-config change as a hard dependency of T7. Asymmetric per-symbol requiredness is also a new rule-shape in the config schema. |
| **`live_config.enable_5m_stream`, OPTIONAL, default `true`, loudly logged** | **RECOMMENDED** | See argument below. |

### Recommended: `live_config.enable_5m_stream: bool` — optional, default `true`
Honest no-silent-defaults argument (the rule targets **silent WRONG behavior**, per the
user's rule memo):
1. **The default reproduces today's behavior for every config that exists** — it is not a
   guess, it is the status quo. CL/HS14B needs zero edits (constraint 1 satisfied).
2. **The default's failure mode for a misconfigured future symbol is a HARD CRASH, never
   silence**: a non-CL 1h config without the flag defaults to `true` → 5m seed required →
   the deliberate No-Silent-Bootstrap `FileNotFoundError` (`data_manager.py:293-308`) kills
   startup pre-event-loop. Nothing silently degrades.
3. **The resolved mode is explicit and logged** (constraint 3): one loud startup line
   (`5M STREAM: ENABLED (default)` / `HOURLY-ONLY MODE: enable_5m_stream=false — 5m
   DataManager/seed/subscription disabled; trailing evaluates on 1h bars`) plus the mode
   stamped into the startup Telegram payload.
4. **Validation makes misuse impossible**: `enable_5m_stream=false` with
   `bar_size == "5m"` → `ValueError` in `__init__` (the 5m stream IS the inference stream
   for 5m configs). `false` is only legal for `bar_size in ("1h","2h","4h")`.
5. **T6/T8 boundary**: ES01B gets a 1-field surgical patch (`"enable_5m_stream": false`) —
   the exact precedent T6 set with its 10-field surgical patch of the same file. Generator
   emission of the flag for future non-CL configs (via base-config or manifest stamping) is
   **T8 territory** (T6 already routed the generator's live_config/base-config residuals to
   T8; this adds one item to that list).

---

## 3. Front-month/hands stream in hourly-only mode (Q3)
**STAYS — it is execution-price telemetry AND order pricing (§1.4).** Precise map:
- It is a 5m-*cadence* live-bar subscription on the front-month **contract** — "5m" here is
  the update cadence of the pricing stream, not the brain 5m dataframe. It has no seed, no
  DataManager, no warm start; only `get_front_month_contract` (start Step 4) must succeed.
- It runs fine with `data_manager_5m = None`: construction (`:2271-2278`), rollover
  resubscribe (`:2412-2428`), reconnect paths (`:2508-2512`, `:2543-2550`, `:2735-2740`)
  never touch the brain 5m artifacts.
- Side benefit: in hourly-only mode the marketable-limit price still refreshes every 5
  minutes intra-hour via `_front_month_last_close` — order pricing quality is unchanged.
- No watchdog exists for the hands stream at HEAD (no `_last_front_month_bar_time`
  staleness check) — true for CL today too; unchanged by this amendment.

## 4. Trailing semantics for ES (Q4)
With no 5m stream, trailing evaluates ONLY on 1h bar closes via the post-957ced7 path.
**The 1h path is complete:**
- `_on_bar_update_1h` calls `_check_trailing_stop()` under `_ledger_lock` (`:2884-2885`)
  BEFORE `_on_new_bar` — pinned by `tests/test_trailing_stop_1h.py` (call, lock-held,
  ordering) and `tests/test_trailing_stop_5m_scheduling.py` (decoupled from inference).
- Activation → SL modify uses the transmit-then-commit `modify_order` block
  (`:1171-1196`; the live-trailing-modify-order-dead fix), fenced by
  `tests/test_modify_order_transmit.py` (transmit-failure rollback, retry-next-bar) and
  tick-grid rounding by `tests/test_tick_order_pricing.py` (S6 — ES 0.25 grid via T3
  `round_to_tick`, `:1142`).
- The ONLY missing piece is the `:1100` extremes read (§1.2) — the frame source, not the
  logic.
**Backtest-consistency argument:** the 1h BacktestEngine models trailing at 1h resolution —
`scripts/ledger_parity_check.py:64-68` states it verbatim ("the backtest trails at 1h
resolution — an unavoidable asymmetry" vs live 5m trailing). Live-with-5m activates
trailing EARLIER than the backtest models (intrabar 5m extremes). Hourly-only live matches
the backtest EXACTLY (bar-close extremes, same bars). **For ES this is a parity
improvement, not a degradation.** CL keeps its production 5m-granularity trailing via the
flag default — an intended, documented asymmetry (CL production-pinned; ES backtest-exact).

## 5. Watchdog / reconnect with a None 5m stream (Q5)
Touchpoint enumeration (complete):
| Path | Site | Hourly-only behavior |
|---|---|---|
| restart reset | `:842-844` | sets `_live_bars_5m=None` — fine |
| `_shutdown` | `:872-876` cancel (guarded); **`:889` `data_manager_5m.save_cache()` — needs `is not None` guard** | 1 guard |
| `_deferred_resubscribe` | `:2498-2502` cancel (guarded); **`:2517-2525` 5m resubscribe — must gate on mode** | 1 gate |
| `_resubscribe_and_backfill` | `:2722-2727` cancel (guarded); resubscribes via `_subscribe()` (gated internally) | clean |
| `_backfill_reconnect_gap_async` | `:2583` `if self._last_bar_time_5m is not None` → 5m block naturally skipped; `:2618` manager guard already present | clean — 1h block still backfills |
| `_check_contract_rollover` | **`:2407` `data_manager_5m.front_month_id = ...` — needs guard**; hands resubscribe unaffected | 1 guard |
| `_check_stale_bars` | `:3944` — **re-point to `_last_bar_time_1h` with a 1h threshold** | see below |
| `_on_ib_error` | stream-agnostic flags | clean |
| start() Step 6 | **`:726-728` — needs guard** | 1 guard |

**Reconnect semantics change:** none structurally — the same 10182/1100/2103 → resubscribe
→ backfill machinery runs; it simply re-subscribes one fewer stream and skips the 5m
stitch. The watchdog becomes the 1h stream's guardian (an upgrade for 1h configs, §1.5).

**Threshold math (1h bars are stamped with OPEN time; `_on_bar_update_1h` receives bar T at
wall-time T+60):** normal staleness oscillates 60→120 min → the 15-min constant would
false-positive EVERY hour. New constant `_STALE_BAR_THRESHOLD_MINUTES_1H = 135`
(= 120 max normal + 15 legacy margin). **Sequencing caveat:** ES at HEAD uses
`_GLOBEX_SESSION` (equity calendar of audit.md §1 NOT landed; verified
`instrument_master.py:94`, `session_calendar.py:176-198` — no EQUITY arm). Across the
Mon-Thu 16:00-17:00 CT maintenance break, staleness reaches 180 min while GLOBEX reports
OPEN from 17:00 CT → a 135-min threshold false-positives daily ~17:15-18:00 CT. Remedies:
land audit.md §1 FIRST (its `session_open_anchor` 17:00/15:30 CT reopen anchors cap
measured staleness — then 135 is safe; this is already T7's plan), OR ship with 195 min
(180+15) until §1 lands. CL is untouched either way (flag true → 5m stream + 15 min,
byte-identical). The 15:15-15:30 CT equity halt is a non-issue for the 1h watchdog even
without §1 (staleness during it ≤ ~95 min < 135).

## 6. Tests / parity census (Q6)
**Existing tests constructing/stubbing the 5m path** (grep census, 14 files):
`test_trailing_stop_5m_scheduling.py` (5m callback drives trailing — CL pins),
`test_trailing_stop_1h.py` (957ced7 fence), `test_modify_order_transmit.py` (real
`_check_trailing_stop` + `rolling_df_5m` stub), `test_tick_order_pricing.py:781+` (S6
trailing rounding via 5m stub), `test_trailing_stop_log_format.py`,
`test_session_watchdog_rollover.py` (watchdog pins on `_last_bar_time_5m` `:450-502`,
`:1284-1340`; `_warm_start` stubs mock `data_manager_5m` `:1082-1083`),
`test_reconnection.py` (5m backfill anchors), `test_cooldown.py`,
`test_live_macro_refresh.py`, `test_exit_bar_semantics.py`, `test_live_trader_bugs.py`,
`test_config_generator_symbols.py:759`, `test_symbol_data_paths.py:828`. All represent
CL-default behavior → must stay green untouched (assertions), modulo the stub-helper note
in §7.

**The parity harness ALREADY runs hourly-only — the irony confirmed.**
`scripts/livetest_engine.py` in 1h mode wires ONLY `_live_bars_1h` (`:233-236`) — **no 5m
subscription exists in the harness at all** — and mirrors each 1h bar into `rolling_df_5m`
(`:223-227`, `:387-406`) solely to satisfy the `:1100` read. The ledger parity gate
($0.00-delta fence re-verified T5/T6) therefore already validates the trade path with zero
live 5m bars. Caveats: (a) the gate config runs `--disable-trailing`
(`ledger_parity_check.py:145`) so trailing itself is fenced by the §4 unit tests, not the
gate; (b) the `_disable_trailing` docstring (`:64-68`, "the live trailing stop is INERT in
the 1h harness (bound to the 5m callback)") is **STALE post-957ced7** — comment-only
residual, fix in passing or route to the cosmetic sweep; (c) after this amendment the
harness's 5m mirror could theoretically be simplified — **DO NOT touch it** (harness is the
parity fence; scope guard).

---

## 7. File-by-file design (implementation ticket input)

**ALL changes in `src/live_execution/live_trader.py` + 1 config field + tests. NO changes
to `data_manager.py`, `ibkr_client.py`, adapters, cli.py, fleet_runner, generators,
backtest engine, or livetest harness.** (cli.py's `resolved_seed_path` string derivation
(`cli.py:249`) touches no disk and is ignored by a None manager — no edit needed.)

1. `live_trader.py` `__init__` (near the `_bar_size` extraction `:324`):
   ```python
   _live_cfg_all = strategy_config.get("live_config", {})
   self._enable_5m_stream: bool = bool(_live_cfg_all.get("enable_5m_stream", True))
   if not self._enable_5m_stream and self._bar_size not in ("1h", "2h", "4h"):
       raise ValueError(
           f"live_config.enable_5m_stream=false requires an hourly bar_size "
           f"(got {self._bar_size!r}) — the 5m stream IS the inference stream "
           f"for 5m configs."
       )
   ```
   Loud mode log (and include the mode in the startup Telegram payload). At `:373`:
   `self.data_manager_5m = DataManager(...) if self._enable_5m_stream else None`
   (+ the HOURLY-ONLY banner in the else arm).
2. `_warm_start` (`:1952-1972`): wrap the 5m block in `if self._enable_5m_stream:`; log
   `"HOURLY-ONLY MODE: 5m warm start skipped"` otherwise. (`rolling_df_5m`/
   `_last_bar_time_5m` stay None.)
3. `_subscribe` (`:2243-2251`) and `_deferred_resubscribe` (`:2517-2525`): gate the 5m
   subscription blocks on the flag (log the skip). Cancel paths already None-safe.
4. `_check_trailing_stop` (`:1100`): extremes-frame selection —
   `df = self.rolling_df_5m if self.rolling_df_5m is not None else self.rolling_df_1h`
   (or a tiny `_primary_bar_df()` helper reused by items 5/9). CL byte-identical (5m frame
   always present when flag true). This form needs NO flag read → zero `__new__`-stub churn
   in the trailing test files. Not a silent fork: `rolling_df_5m is None` ⟺ hourly-only by
   construction, and the mode was declared loudly at startup.
5. `_recover_inherited_position` (`:1468-1509`): same primary-frame source for bars-held
   reference, entry-bar-time fallback, and extremes seeding.
6. `_check_stale_bars` (`:3944-3962`): stream + threshold selection:
   ```python
   if getattr(self, "_enable_5m_stream", True):
       last_bar_time = getattr(self, "_last_bar_time_5m", None)
       threshold = _STALE_BAR_THRESHOLD_MINUTES
   else:
       last_bar_time = getattr(self, "_last_bar_time_1h", None)
       threshold = _STALE_BAR_THRESHOLD_MINUTES_1H
   ```
   New module constant `_STALE_BAR_THRESHOLD_MINUTES_1H = 135` (§5; 195 if shipped before
   audit.md §1). The `getattr(..., True)` mirrors the function's OWN existing
   `getattr(self, "_last_bar_time_5m", None)` seam for `__new__` watchdog stubs — the
   attribute is ALWAYS set explicitly in `__init__`, so production never exercises the
   fallback (test-seam-only; flagged for TDD-manager judgment; alternative = add the attr
   to the ~4 watchdog stub helpers — helper edits, not pin evolutions).
7. `_log_heartbeat` (`:3802-3807`): when flag false, report `_last_bar_time_1h` as
   `last_bar` (else the heartbeat lies "no bars received yet" forever). CL branch
   byte-identical.
8. Guards: `:726-728` (Step 6), `:889` (`_shutdown` save), `:2407` (rollover
   front_month_id) — `if self.data_manager_5m is not None:`.
9. (Optional, recommended) `_check_naked_position` `:4073` → primary frame (market order —
   cosmetic).
10. `configs/strategies/ES01B_Sharpe_E03_07042026.json`: surgical +1 field —
    `live_config.enable_5m_stream: false` (T6 surgical-patch precedent; needs approval).
11. **Deliberate non-changes:** `market_bars` telemetry stays empty in hourly-only (adding
    1h rows = new behavior; defer, note in runbook — `raw_front_month_bars` still fills);
    livetest harness untouched; `ledger_parity_check.py:64-68` stale docstring — comment
    fix in passing or cosmetic-sweep route; generator/base-config emission of the flag = T8.

### Amended canary expectations (replaces audit.md §6 items 2/6 for ES)
- `DATA PATHS: 5m seed=` line ABSENT (or explicitly "5m stream disabled"); expect the
  `HOURLY-ONLY MODE` banner instead. No `warm_start_cache_ES.parquet` /
  `es_continuous_master.parquet` (5m) artifacts are created — only the `_1h` pair.
- Success criterion 6 becomes: `Subscribed to 1-hour...` + `Subscribed to front-month...`
  and NO `Subscribed to 5-min continuous` line.
- Criterion 8 (bars flowing) moves to `RAW BAR`/front-month + `NEW 1H BAR:` lines.
- audit.md §2.2 (5m seed) and §8 Q1 are **CLOSED — OBE by user ruling**; §2.1 (1h seed
  copy), §5 (entitlement), §6 preconditions otherwise stand.

---

## 8. TDD test list (Strict-Lock; new file e.g. `tests/test_hourly_only_mode.py`)
1. **ES hourly-only boots with ZERO 5m artifacts:** ES-shaped 1h config +
   `enable_5m_stream: false`, tmp data root containing ONLY a 1h seed/cache — `__init__`
   yields `data_manager_5m is None`; `_warm_start()` succeeds (no `es-5m_bk.csv`
   FileNotFoundError); `rolling_df_1h` populated, `rolling_df_5m is None`.
2. **CL default pins (anti-drift):** config WITHOUT the flag → `_enable_5m_stream is True`;
   `data_manager_5m` constructed with the byte-identical legacy CL paths; `_subscribe()`
   issues 5m AND 1h subscriptions with today's exact kwargs (mocked feed).
3. **Validation:** `bar_size: "5m"` + `enable_5m_stream: false` → `ValueError` in
   `__init__` (message pinned shape-wise).
4. **Trailing-on-1h:** hourly-only trader (`rolling_df_5m=None`, `rolling_df_1h` real) in
   position → `_check_trailing_stop` reads 1h High/Low, activates, transmits via
   `modify_order` (reuse the `test_modify_order_transmit` seam); companion CL pin: when
   `rolling_df_5m` is present it is still the extremes source (bit-compare vs HEAD).
5. **Watchdog semantics:** hourly-only + market OPEN: staleness 90 min → False; 140 min →
   True (threshold 135); `_last_bar_time_1h=None` → False; market CLOSED → False. CL pin:
   5m stream + 15-min threshold unchanged (existing T5 pins must stay green untouched).
6. **Resubscribe/reconnect:** `_deferred_resubscribe` and `_resubscribe_and_backfill` in
   hourly-only never call `subscribe_live_bars(*bar_size="5 mins"*, continuous=True)`
   (mock-assert), still re-subscribe 1h + front-month; `_backfill_reconnect_gap_async`
   skips the 5m block and stitches 1h.
7. **Shutdown:** hourly-only `_shutdown()` completes (no AttributeError on the None
   manager), saves the 1h cache.
8. **Hands stream integrity:** `_subscribe_front_month` still called in hourly-only;
   `_front_month_last_close` still preferred for `current_price` in `_on_new_bar`.
9. **Recovery extremes:** `_recover_inherited_position` with `rolling_df_5m=None` seeds
   `_highest_high/_lowest_low` from the last 1h bar.
10. **Loud logging:** startup emits the HOURLY-ONLY banner (shape pin: contains
    "HOURLY-ONLY" + "enable_5m_stream").
Post-green: HS14B ledger parity gate re-run (expect $0.00 — convention; the harness
bypasses `start()` so the gate exercises items 4's CL branch implicitly).

## 9. Severity + regression classification
- **Severity: MEDIUM.** Multi-site but purely additive gating inside one file
  (live_trader.py: ~7 gates/guards + 1 frame-selection change + 1 constant), one 1-field
  config patch, one new test file. No refactor; no ibkr_client/data_manager/adapters/
  generator/harness edits. NOT a LOW: the trailing-frame and watchdog changes are
  behavior-bearing for the new mode and CL-pin-sensitive.
- **Regression: NO.** Nothing at HEAD is broken for CL; the `:1100` unguarded read is
  correct at HEAD (the 5m frame always exists in production) and only becomes a defect in
  the new mode. 957ced7's 1h trailing path is confirmed complete.
- **Hard-constraint check:** (1) CL byte-identical, zero config edits — flag default true;
  parity-gate convention re-run. (2) ES runs with zero 5m data — no seed/cache/ledger/
  subscription/backfill; hands stream is live-only. (3) No silent forks — explicit flag,
  loud log + Telegram, crash-on-misuse. Scope guards honored: no fleet_runner/generator/
  backtest/harness changes; T8 owns generator/base-config emission of the flag.

## 10. Open questions requiring HUMAN AUTHORIZATION
1. **Mechanism sign-off:** optional `live_config.enable_5m_stream` default `true`
   (recommended, §2) vs required-for-non-CL (stricter but forces the T8 generator change
   into T7's critical path). Either way: approve the 1-field surgical patch to the
   T6-frozen `ES01B_Sharpe_E03_07042026.json`.
2. **Watchdog sequencing:** land audit.md §1 (equity session calendar) BEFORE hourly-only
   and use `_STALE_BAR_THRESHOLD_MINUTES_1H = 135`; or ship hourly-only first with 195 min
   and drop to 135 when §1 lands (§5).
3. **TDD-manager sanction:** the `getattr(self, "_enable_5m_stream", True)` test-seam in
   `_check_stale_bars` (precedented in-function) vs editing the ~4 watchdog `__new__` stub
   helpers; no assertion/pin changes required either way.
4. **Intended asymmetry acknowledgment:** CL keeps 5m-granularity trailing (production-
   pinned, earlier-than-backtest activation); ES gets backtest-exact 1h trailing. Confirm
   this is the accepted end-state (it is the amendment's premise).
5. **Deferred:** 1h rows into `market_bars` telemetry for hourly-only instances (v2);
   1h-stream watchdog for 5m-enabled CL instances (pre-existing gap, micro-ticket).
