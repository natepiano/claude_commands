# herdr worker panes — next items

Approved items for a later plan. They are not planned or built in this plan.

- **Restore after a crash before the old tab closes.** A crash or a close failure after a restore starts the new session can leave both tabs open. `pane_restore.skip_reason()` then skips both, because the conversation already runs. Recovery must prove the replacement and the idle old pane before it closes the old tab, and keep ambiguous records. Source: Astra review of phase 4h round 3 (2026-11-02). Ruled `next` by the sdlc showrunner (2026-11-02).
- **Retire skips git-ignored files in the worktree.** Retire does not see files that git ignores, for example a `.unit.toml`. Source: style-unit, through natedev and the sdlc showrunner (2026-11-02). Ruled `next`: do not start it during 4h. Ask style-unit for the exact commands and output when this item is planned.
- **A pane stopped by hand blocks retire.** The pane of a unit that the user stopped by hand stops retire. Source: style-unit, through natedev and the sdlc showrunner (2026-11-02). Ruled `next`: do not start it during 4h. Ask style-unit for the exact commands and output when this item is planned.
