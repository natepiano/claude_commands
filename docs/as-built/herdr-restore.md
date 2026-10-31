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

## Pane restore (checkpoint 2)

- Each entry also holds `flags_read` (false when the hook found no Claude argv) and `ended`. `scripts/hooks/session-end-pane-record.py` sets `ended` at
  SessionEnd for any reason but `other`: `/exit` and logout mark it, a signal at a reboot or a
  crash does not. An end only marks the entry of the same conversation.
- `pane_record.py seed [--session NAME]` writes the entries of the Claudes running now: the argv
  and cwd from herdr's `pane process-info`, the environment of the process, and the
  conversation id from `<config>/sessions/<pid>.json`. It needs no
  `report-agent-session`: a restore takes the id from the entry and checks herdr's
  `agent_session` only when the pane has one.
- `scripts/shutdown/pane_restore.py [--session NAME] [--dry-run]` (the shared session by
  default) first waits for the server (`herdr.start_server`, which adopts it). For each pane with
  an entry, it resumes the conversation unless a reason below holds, and prints the reason:
  - herdr's `agent_session` names another conversation;
  - the entry is `ended` (closed by hand);
  - the conversation already runs (`sessions.live_sessions` or `running_sessions`);
  - `flags_read` is false (entries written before the field are read as true off the Mac);
  - the account has no transcript of the conversation;
  - the pane runs a program.
- A resume opens a tab with `herdr.open_tab` in the pane's workspace, with the pane's label and
  cwd, and the entry's marks plus `named_account_environment(config_dir)`. It then runs
  `start_agent` with `--resume <id>`, `-n`, `--remote-control`, `--model` from the entry, and
  `--settings '{"disableAgentView": true}'`, with no opening prompt. Then it closes the restored
  tab by its pane id: ordering the tabs keeps pane ids. A failed start closes the new tab and
  keeps the restored one. Any failure makes the exit status 1.
- `herdr.keeps_restored_layout(session)` is true for `shared_session()` only when herdr's
  config sets `[session] resume_agents_on_restore = false`. Else herdr resumes each pane itself
  on the server's account, so `_clear_restored_workspaces` clears the layout, as for every other
  session.
- `restart._herdr_launch` runs `pane_restore.restore` once, when its launch started the shared
  server (`ServerCreated` or `WindowServerCreated`) and the layout is kept. The restore resumes each eligible stopped conversation from its pane launch entry.
  Stale shutdown records do not prevent pane restore. A restore that leaves a pane not
  resumed prints a restart issue that names the command to run it again.
- `herdr._submit_server`, for the shared session only, starts `herdr-<name>.service` with
  `systemctl --user start` when a unit file defines it (`systemctl show -p FragmentPath`, not
  under `/transient/`). Otherwise it runs `systemd-run` as before. A transient unit cannot start
  under the boot unit's name. The Mac never reaches `_submit_server` (it uses
  `_start_darwin_server`), so the Mac has no boot start yet.

Settings entry for the user, one more SessionEnd item:

```json
{"hooks": [{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/session-end-pane-record.py\""}]}
```

Steps on natedev, in order, after promotion:

1. herdr `~/.config/herdr/config.toml`: add `[session]` with `resume_agents_on_restore = false`,
   then `herdr --session agents server reload-config`.
2. `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/pane_record.py seed` once.
3. The nixos units below, added to `modules/linux/herdr.nix`, then
   rebuild. The shared server running now is the transient unit `herdr-agents.service` that
   `systemd-run` made, the same name, and the transient file wins while it runs. Not tested: how
   the rebuild treats a unit file under the name of an active transient unit. After the rebuild,
   check `systemctl --user status herdr-agents.service` and that only one
   `herdr --session agents server` runs. The boot unit takes over at the next reboot. Linger is
   off, so both units start at login. Both take the user manager's PATH, as the transient unit
   does now.

```nix
{ inputs, pkgs, ... }:
{
  # environment.systemPackages as now.

  systemd.user.services.herdr-agents = {
    description = "The shared herdr session, from login";
    wantedBy = [ "default.target" ];
    serviceConfig = {
      ExecStart = "/run/current-system/sw/bin/herdr --session agents server";
      # The restore takes minutes; its own unit keeps it out of this unit's start time.
      ExecStartPost = "/run/current-system/sw/bin/systemctl --user start --no-block herdr-agents-restore.service";
      Restart = "on-failure";
      RestartSec = 5;
    };
  };
  systemd.user.services.herdr-agents-restore = {
    description = "Resume each herdr pane's Claude on its own account";
    after = [ "herdr-agents.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "%h/.claude/scripts/lib/py %h/.claude/scripts/shutdown/pane_restore.py";
      # Up to 90 s per pane; the 90 s default start timeout would kill it.
      TimeoutStartSec = "1h";
    };
  };
}
```

- The Mac has no `/proc`. There `pane_record` reads a parent with `ps -o ppid= -p <pid>`, and the
  arguments and environment from the kernel's KERN_PROCARGS2 record (`account.kern_procargs2`,
  split by `account.procargs2_parts`). On both machines an environment keeps only
  `CLAUDE_CONFIG_DIR` and the names the entry keeps; every other value, a token included, is
  dropped as it is read. The Mac hides the environment of a system binary such as `/bin/zsh`,
  never of Claude.

## Still to come

- The Mac: a launchd agent and the Mac desktop rule for herdr.
- Last: retire `agent-sessions-snapshot.timer` after one real reboot restores every pane.
