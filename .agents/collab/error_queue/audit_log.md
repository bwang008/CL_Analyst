# Fleet Error Queue â€” Audit Log (append-only)

Every event that moves through the queue gets lines here: infra filings by
`error_watcher.ps1`, and investigation/fix/deploy/commit records by the
fleet-error-monitor agent. The human reviews this log to validate fixes
after the fact â€” entries must name the root cause, the files changed, the
test evidence, and the deploy/commit SHAs. Format:

```
[TIMESTAMP UTC] | <event_id> | <ROLE> | <message>
```

---
[2026-07-06T09:33:49Z] | ES01B_Sharpe_E03_07042026_61c556b44163 | WATCHER | INFRA auto-filed to done/ (pattern=data-farm-broken, model=ES01B_Sharpe_E03_07042026, occurrences=1, gave_up=False) -- no ticket created
[2026-07-06T09:33:49Z] | GC01B_Sharpe_E04_07052026_fd386794b6e8 | WATCHER | INFRA auto-filed to done/ (pattern=data-farm-broken, model=GC01B_Sharpe_E04_07052026, occurrences=1, gave_up=False) -- no ticket created
[2026-07-06T09:33:49Z] | ES01B_Sharpe_E03_07042026_c944a33940c4 | WATCHER | INFRA auto-filed to done/ (pattern=data-farm-broken, model=ES01B_Sharpe_E03_07042026, occurrences=1, gave_up=False) -- no ticket created
[2026-07-06T09:33:49Z] | GC01B_Sharpe_E04_07052026_6baaf6c060d2 | WATCHER | INFRA auto-filed to done/ (pattern=data-farm-broken, model=GC01B_Sharpe_E04_07052026, occurrences=1, gave_up=False) -- no ticket created
[2026-07-06T10:33:48Z] | ES01B_Sharpe_E03_07042026_5cbe3d5ed6ba | WATCHER | INFRA auto-filed to done/ (pattern=data-farm-broken, model=ES01B_Sharpe_E03_07042026, occurrences=1, gave_up=False) -- no ticket created
[2026-07-06T10:33:48Z] | GC01B_Sharpe_E04_07052026_c4321d29d597 | WATCHER | INFRA auto-filed to done/ (pattern=data-farm-broken, model=GC01B_Sharpe_E04_07052026, occurrences=1, gave_up=False) -- no ticket created
[2026-07-06T10:33:48Z] | ES01B_Sharpe_E03_07042026_638952b173c0 | WATCHER | INFRA auto-filed to done/ (pattern=data-farm-broken, model=ES01B_Sharpe_E03_07042026, occurrences=1, gave_up=False) -- no ticket created
[2026-07-06T10:33:48Z] | GC01B_Sharpe_E04_07052026_5e897bf39b31 | WATCHER | INFRA auto-filed to done/ (pattern=data-farm-broken, model=GC01B_Sharpe_E04_07052026, occurrences=1, gave_up=False) -- no ticket created
[2026-07-06T21:25:07Z] | health-check-14:23PT | MONITOR | HEALTH — log-error cluster (41 lines, all children, 14:15:00-14:15:27 PT): IBKR Gateway daily reset at 17:15 ET during the 5-6pm ET futures halt; Peer-closed + ConnectionRefused while Gateway restarted; all 4 children Reconnected successfully on attempt 2 within 30s, all streams resubscribed, positions intact (CL +1, NG -1, MGC +1, MES flat), heartbeats connected=True. INFRA noise — no ticket. Bars expected to resume at 18:00 ET reopen.
[2026-07-06T22:23:55Z] | NG01B_Sharpe_E03_07052026_7ec731732b53 | WATCHER | moved pending/ -> processing/ for agent investigation (model=NG01B_Sharpe_E03_07052026, exit=, occurrences=1, gave_up=False)
[2026-07-06T22:23:55Z] | GC_Sharpe_E04_07052026_250b57a807e9 | WATCHER | moved pending/ -> processing/ for agent investigation (model=GC_Sharpe_E04_07052026, exit=, occurrences=1, gave_up=False)
[2026-07-06T22:23:55Z] | HS14B_Sharpe_E01_06262026_1349681634cb | WATCHER | moved pending/ -> processing/ for agent investigation (model=HS14B_Sharpe_E01_06262026, exit=, occurrences=1, gave_up=False)
[2026-07-06T22:25:03Z] | NG01B_Sharpe_E03_07052026_7ec731732b53 | MONITOR | INVESTIGATING — stale-bars-watchdog health event, no-bars count spans the 5-6pm ET daily halt
[2026-07-06T22:25:03Z] | NG01B_Sharpe_E03_07052026_7ec731732b53 | MONITOR | DONE — infra/noise: DOCUMENTED reopen false-positive (cl-watchdog-reopen-grace_07052026_0001, pinned as-is in _check_stale_bars T5): market reopened 18:00 ET, stale clock counted the halt, watchdog force-reconnected; child recovered on attempt 1, bars flowing (22:00-22:15Z), position intact. Error-366 log lines = same reconnect cycle. NOTE: will recur DAILY at reopen until the grace fix is implemented.
[2026-07-06T22:25:03Z] | GC_Sharpe_E04_07052026_250b57a807e9 | MONITOR | INVESTIGATING — stale-bars-watchdog health event, no-bars count spans the 5-6pm ET daily halt
[2026-07-06T22:25:03Z] | GC_Sharpe_E04_07052026_250b57a807e9 | MONITOR | DONE — infra/noise: DOCUMENTED reopen false-positive (cl-watchdog-reopen-grace_07052026_0001, pinned as-is in _check_stale_bars T5): market reopened 18:00 ET, stale clock counted the halt, watchdog force-reconnected; child recovered on attempt 1, bars flowing (22:00-22:15Z), position intact. Error-366 log lines = same reconnect cycle. NOTE: will recur DAILY at reopen until the grace fix is implemented.
[2026-07-06T22:25:03Z] | HS14B_Sharpe_E01_06262026_1349681634cb | MONITOR | INVESTIGATING — stale-bars-watchdog health event, no-bars count spans the 5-6pm ET daily halt
[2026-07-06T22:25:03Z] | HS14B_Sharpe_E01_06262026_1349681634cb | MONITOR | DONE — infra/noise: DOCUMENTED reopen false-positive (cl-watchdog-reopen-grace_07052026_0001, pinned as-is in _check_stale_bars T5): market reopened 18:00 ET, stale clock counted the halt, watchdog force-reconnected; child recovered on attempt 1, bars flowing (22:00-22:15Z), position intact. Error-366 log lines = same reconnect cycle. NOTE: will recur DAILY at reopen until the grace fix is implemented.
