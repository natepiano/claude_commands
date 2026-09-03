# cargo-berth hook material

The canonical, durable copies of the three hook wrappers. They run from this
directory; nothing copies them anywhere. Edit them here and every repository
picks the change up on its next hook invocation.

- `hooks/berth_pre_edit.sh` — `PreToolUse` edit authorization.
- `hooks/berth_post_bash.sh` — `PostToolUse` drift observation.
- `hooks/berth_session_start.sh` — `SessionStart` board read.

Each one checks that `cargo-berth` is on `PATH` and then `exec`s
`cargo-berth hook <event>`. The engine decides every outcome and writes every
byte of the harness protocol response. The one thing a wrapper states on its own
is what it states when there is no engine to ask: the pre-edit wrapper fails
closed, and the other two publish a static repair notice.

## Engine

This directory owns the engine refresh as well as the canonical wrappers. Run
one command from any checkout of the `cargo-liner` repository:

```sh
$HOME/.claude/scripts/berth/install/install.sh /path/to/cargo-liner
```

The command builds `cargo-berth`, installs it in
`${CARGO_HOME:-$HOME/.cargo}/bin`, and restores the preceding engine if
publication fails.

## Registration

Register the three absolute paths once, in `~/.claude/settings.json`:

    "hooks": {
      "PreToolUse":   [{ "matcher": "Edit|Write|NotebookEdit",
                         "hooks": [{ "type": "command",
                                     "command": "$HOME/.claude/scripts/berth/install/hooks/berth_pre_edit.sh" }] }],
      "PostToolUse":  [{ "matcher": "Bash",
                         "hooks": [{ "type": "command",
                                     "command": "$HOME/.claude/scripts/berth/install/hooks/berth_post_bash.sh" }] }],
      "SessionStart": [{ "hooks": [{ "type": "command",
                                     "command": "$HOME/.claude/scripts/berth/install/hooks/berth_session_start.sh" }] }]
    }

That registration is global, so the wrappers run in every session. **Enrollment,
not registration, decides which repositories they act on**: an unenrolled
repository reports `unconfigured` and every wrapper stops there. `cargo-berth
init` is what opts a repository in, and it leaves `.claude/config/berth.toml`
— naming that repository's trunk — as the only per-repository file. A linked
worktree needs nothing of its own; it shares the enrolling repository's ledger
through the common git directory.

Do not copy these scripts into a repository. A copy stops receiving fixes made
here, and a wrapper naming an engine that is no longer the installed one is the
condition these three exist to report rather than to reproduce.

`/sync` is registered the same way and for the same reason: once, at
`~/.claude/commands/sync.md`, so a contract change is edited in one place.

## Removal

Delete the three entries from `~/.claude/settings.json`. Leave this directory
alone — the scripts are the canonical copies, not an installation artifact, and
removing them breaks any settings file still naming them.
