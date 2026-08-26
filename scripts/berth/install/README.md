# cargo-berth install material for a consuming repository

Canonical, durable copies of the files a repository installs to use cargo-berth
through the `claim_state.py` coordinator. Edit them here; a consuming repository
gets a copy, never the original.

- `commands/sync.md` — the `/sync` command contract.
- `hooks/berth_pre_edit.sh` — `PreToolUse` edit authorization.
- `hooks/berth_post_bash.sh` — `PostToolUse` drift observation.
- `hooks/berth_session_start.sh` — `SessionStart` board read.

Install into a repository:

    mkdir -p <repo>/.claude/hooks
    cp hooks/*.sh <repo>/.claude/hooks/
    cp commands/sync.md <repo>/.claude/commands/

Remove them the same way. Registering the hooks is a separate step that edits
`<repo>/.claude/settings.local.json`.

`/Users/natemccoy/rust/hana` is the proving repository. Its worktree must be left
clean: install for a proof, then remove. Its `.claude/settings.local.json` and its
tracked `.claude/config/berth.toml` are not managed from here.
