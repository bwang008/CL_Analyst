# .agents/collab — Ticket & TDD Collaboration Workspace

Shared workspace for the `/ticket-manager` and `/tdd-manager` multi-agent workflows.
Everything these agents produce lives here — **never** the repo root — so multiple
tickets can be worked in parallel without clobbering each other.

## Ticket ID
Every ticket is keyed by a collision-free, human-readable **Ticket ID**:

```
<slug>_<MMDDYYYY_HHMM>          e.g.  oca-cancel-order_07022026_1842
```

The `/ticket-manager` mints it at the start of a ticket and threads it through every
subagent, dashboard line, and audit-log line. To reconstruct a ticket's full history:

```
grep "oca-cancel-order_07022026_1842" .agents/collab/*.md
```

## Layout
```
.agents/collab/
├── tickets/
│   └── <TICKET_ID>/
│       ├── blueprint.md      ← /ticket-manager output (approved fix plan)
│       └── tdd_result.md     ← /tdd-manager output (final test outcome + files changed)
├── ticket_status.md          ← /ticket-manager dashboard (append-only)
├── ticket_audit_log.md       ← Ticket-Auditor + Impact-Reviewer log (append-only)
├── tdd_status.md             ← /tdd-manager dashboard (append-only)
└── tdd_audit_log.md          ← TDD-Tester + TDD-Coder log (append-only)
```

## Flow
1. `/ticket-manager` mints the Ticket ID, creates `tickets/<TICKET_ID>/`, and (via the
   Auditor + Impact-Reviewer) writes `blueprint.md`.
2. It hands the **Ticket ID** to the user.
3. The user passes that Ticket ID to `/tdd-manager`, which reads `blueprint.md` from the
   same folder, drives the Tester/Coder loop, and writes `tdd_result.md` back into it.

Append-only logs use the field order `[TIMESTAMP] | <TICKET_ID> | <ROLE> | <message>`.

`tickets/_legacy/` holds pre-migration blueprints kept for reference.
