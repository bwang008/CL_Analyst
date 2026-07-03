# Ticket parity-exit-signal_07022026_1930

Residual backtest-vs-live exit/signal parity divergence (post-OCA).

- Phenomenon A: signal/entry sequencing divergence (18-vs-17 trade-count gap), 2026-05-28 → 05-29.
- Phenomenon B: exit-reason flip w/ material PnL gap, 2026-06-02 07:00 LONG (TIME_BARRIER vs SL_HIT, $420). Check PARALLEL-WORK GUARD (trailing-stop 5m ticket) first.
- Phenomenon C: small near-tolerance residuals (LOW).

Workspace for Ticket-Manager blueprint.md and TDD-Manager tdd_result.md.
