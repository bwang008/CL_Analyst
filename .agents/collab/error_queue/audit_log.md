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
