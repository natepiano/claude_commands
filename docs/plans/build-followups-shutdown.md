# shutdown

> **Production: build-followups** — unit `shutdown-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-09 14:49 PDT

The user's words, 2026-10-08: "For later I want a command that fully shuts down Claude safely and fully restarts later."

The user chose, 2026-10-09 14:5x PDT, the scope "both machines: every showrunner, unit director, seat and Codex server on natedev and the Mac first reaches a clean, pushed state, its update timers stop, and where it was is recorded; restart resumes each session where it was and restarts its timers; CI runners and system services keep running", adding in his words: "yes both machines - but also be aware that this command should be account aware - in the future if we can have machines (on machine or on different machines) running different accounts - we need to only e shutting down the account we've been asked to shutdown (we need a way to surface the account we are, easily)"

Context (showrunner): a proposed, not yet approved, plan for sessions on different Claude/Codex accounts is at ~/.local/state/plan-backlog/2026-10-09-per-session-accounts.md; read it for how accounts are found today (scripts/whoami/agent_accounts.py follows CLAUDE_CONFIG_DIR and CODEX_HOME) and design the account filter and the "which account am I" surface so they fit it, without building that plan.
