# herdr restore (as built)

Goal (user, 2026-10-19): after a crash or a reboot, `herdr session attach agents` shows every
workspace, tab and pane as before, each Claude conversation resumed by its id on its own account.

## Pane launch record (checkpoint 1)

- `scripts/shutdown/pane_record.py` keeps one file per herdr session,
  `~/.local/state/herdr-panes/<session>.json` (`HERDR_PANE_RECORD_DIR` in tests), keyed by
  `HERDR_PANE_ID`.
- Each entry holds the pane's Claude launch: the account folder (`CLAUDE_CONFIG_DIR`), the cwd,
  the `-n` name, the `--model` value, whether `--remote-control` was given, the conversation id
  at that start, and the `SHOWRUNNER_*`, `ENABLE_TOOL_SEARCH` and probe marks. No credential
  enters it: a resume rebuilds the account variables from the account folder.
- `scripts/hooks/session-start-pane-record.py` writes the entry at SessionStart
  (startup, resume, clear, compact) from its own Claude process: the environment it inherits, and
  the flags from the nearest ancestor whose argv is Claude (`herdr.is_claude_process`). So a pane
  started by hand is covered the same as one restart or add_unit started. A session outside
  herdr (`HERDR_ENV` not 1, or no `HERDR_PANE_ID`) writes nothing.
- A start replaces its own pane's entry and keeps every other pane's, under a file lock.
- The conversation id that counts is herdr's own `agent_session` for the pane, kept by herdr's
  hook; the id in the entry is a check on it. A pane herdr no longer has is ignored, so a stale
  entry needs no cleanup.

The settings.json entry, one more SessionStart item:

```json
{"matcher": "startup|resume|clear|compact", "hooks": [{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/session-start-pane-record.py\""}]}
```

## Still to come (checkpoint 2)

- `shutdown.py restore-panes`: for each restored pane with `agent_session` and an entry, open a
  tab in the same workspace with the old label (`open_tab` gives the environment and
  `HERDR_AGENT_PANE`), start Claude with `--resume <id>`, close the restored tab, order the tabs.
  A down marker leaves the pane down; an id with no transcript is skipped and named.
- `_clear_restored_workspaces` skips the shared session, at all four places it runs.
- herdr `[session] resume_agents_on_restore = false`, applied with `herdr server reload-config`.
- The boot unit `herdr-agents.service` (nixos repo), started through `systemctl --user start`
  when its unit file exists, with `Restart=on-failure`.
- The Mac: a launchd agent and the Mac desktop rule for herdr, a later checkpoint.
- Last: retire `agent-sessions-snapshot.timer` after one real reboot restores every pane.
