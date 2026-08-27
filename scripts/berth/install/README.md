# cargo-berth hook material

The canonical, durable copies of the three hook shims. They run from this
directory; nothing copies them anywhere. Edit them here and every repository
picks the change up on its next hook invocation.

- `hooks/berth_pre_edit.sh` — `PreToolUse` edit authorization.
- `hooks/berth_post_bash.sh` — `PostToolUse` drift observation.
- `hooks/berth_session_start.sh` — `SessionStart` board read.

## Registration

Register the three absolute paths once, in `~/.claude/settings.json`:

    "hooks": {
      "PreToolUse":   [{ "matcher": "Edit|Write|NotebookEdit",
                         "hooks": [{ "type": "command",
                                     "command": "/Users/<you>/.claude/scripts/berth/install/hooks/berth_pre_edit.sh" }] }],
      "PostToolUse":  [{ "matcher": "Bash",
                         "hooks": [{ "type": "command",
                                     "command": "/Users/<you>/.claude/scripts/berth/install/hooks/berth_post_bash.sh" }] }],
      "SessionStart": [{ "hooks": [{ "type": "command",
                                     "command": "/Users/<you>/.claude/scripts/berth/install/hooks/berth_session_start.sh" }] }]
    }

That registration is global, so the shims run in every session. **Enrollment,
not registration, decides which repositories they act on**: an unenrolled
repository reports `unconfigured` and every shim stops there. `cargo-berth
enroll` is what opts a repository in, and it leaves `.claude/config/berth.toml`
— naming that repository's trunk — as the only per-repository file. A linked
worktree needs nothing of its own; it shares the enrolling repository's ledger
through the common git directory.

Do not copy these scripts into a repository. A copy stops receiving fixes made
here, and the `jq` validators in the shims and the engine's output contract move
together — a stale copy rejects output the current engine emits.

`/sync` is registered the same way and for the same reason: once, at
`~/.claude/commands/sync.md`, so a contract change is edited in one place.

## Removal

Delete the three entries from `~/.claude/settings.json`. Leave this directory
alone — the scripts are the canonical copies, not an installation artifact, and
removing them breaks any settings file still naming them.
