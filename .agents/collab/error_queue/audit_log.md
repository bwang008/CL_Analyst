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
