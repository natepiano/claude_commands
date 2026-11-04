# herdr worker panes — next items

Approved items for a later plan. They are not planned or built in this plan.

- **Retire skips git-ignored files in the worktree.** Retire does not see files that git ignores, for example a `.unit.toml`. Source: style-unit, through natedev and the sdlc showrunner (2026-11-02). Ruled `next`: do not start it during 4h. Ask style-unit for the exact commands and output when this item is planned.
- **A pane stopped by hand blocks retire.** The pane of a unit that the user stopped by hand stops retire. Source: style-unit, through natedev and the sdlc showrunner (2026-11-02). Ruled `next`: do not start it during 4h. Ask style-unit for the exact commands and output when this item is planned.
- **Proposal: ask Codex for a read-only current-thread query.** Phase 4r uses the resume argument when a Codex process has no open rollout or environment thread id. This identifies the launch thread for recovery. It does not prove the current thread after `/resume` or `/new`. Request a query that returns the client's current thread, including managed-daemon and idle clients. Source: phase 4n research and phase 4r implementation. Proposal only: file nothing outside our repos.
