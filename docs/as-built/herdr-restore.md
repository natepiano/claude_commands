# Herdr restore

`herdr session attach agents` restores the saved workspaces, tabs, and panes.
The saved launch selects each conversation and account.

## Pane launch record

- `scripts/agents/pane_record.py` keeps one file per herdr session,
  `~/.local/state/herdr-panes/<session>.json` (`HERDR_PANE_RECORD_DIR` in tests), keyed by
  `HERDR_PANE_ID`.
- Each entry holds the pane's Claude launch: the account folder (`CLAUDE_CONFIG_DIR`), the cwd,
  the `-n` name, the `--model` value, whether `--remote-control` was given, the conversation id
  at that start (the fixed staff id for a staff member), and the `ENABLE_TOOL_SEARCH` and probe
  marks. It saves no staff labels: a launch computes them from staff. No credential
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

The [launch-name policy](shutdown-and-restart.md#launch-names) supplies current names without copied staff marks.

## Pane restore

- Each entry also holds `flags_read` (false when the hook found no Claude argv) and `ended`. `scripts/hooks/session-end-pane-record.py` sets `ended` at
  SessionEnd for any reason but `other`: `/exit` and logout mark it, a signal at a reboot or a
  crash does not. An end only marks the entry of the same conversation.
- `pane_record.py seed [--session NAME]` writes the entries of the Claudes running now: the argv
  and cwd from herdr's `pane process-info`, the environment of the process, and the
  conversation id from `<config>/sessions/<pid>.json`. It needs no
  `report-agent-session`: a restore takes the id from the entry and checks herdr's
  `agent_session` only when the pane has one.
- `scripts/agents/pane_restore.py [--session NAME] [--dry-run]` first starts or adopts the shared server.
  It resumes staff rows through `restart.start_row`.
  For a worker row, `start_row` resumes only that worker through `worker.restore` with the worker launch policy and the saved account. An idle worker prints `<name>: closed; its next assign resumes it` and stays closed.
  It resumes saved panes without staff rows through `herdr.launch_agent`.
  A pane without a saved entry or a staff row stays open.
- Restore keeps live conversations. Unknown conversation evidence prints one anomaly line and permits a guarded launch.
  Saved entries remain eligible after an intentional stop or when `flags_read` is false.
  The shared launch path checks the selected terminal before it sends keys.
- Restore reuses a retained shell when its terminal and folder match.
  A replacement uses the saved account and the current staff identity, name, and folder.
  A saved pane without a staff row uses its saved identity, account, and folder.
  Both routes send no opening prompt.
  They preserve the saved Remote Control setting and `disableAgentView`.
  Producers use the current `[producers]` model and effort; other sessions preserve the saved model.
  The launch-name reader supplies the current display name.
- A replacement closes its source only after resume publication proves the new conversation.
  A source pane that gains another conversation or program stays open and prints an anomaly.
  A failed start preserves the pane. A failed restore returns exit status 1.
- Herdr keeps the restored layout. Unless its config sets `[session] resume_agents_on_restore = false`,
  herdr also resumes each pane itself on the server's account, and without the pane's `--env`.
- Before a power-off, run `agents stop --all`. It saves each local live staff row before its stop.
- After login, run `agents restart`. Stop starts nothing.
- `agents restart --all` uses the same selection and stop loop, then starts the selected rows.
- The selection includes producers. A live session without a staff row prints one line and stays unsaved.
- `agents restart` starts the missing rows from the saved row ID list.
- Staff supplies the current session and worktree. Herdr saves tab order by staff row ID.
- Restart sends no work prompt and does not restore unrelated panes.
- A restore takes focus only when that launch created a window
  (`WindowServerCreated`). After focusing that window, it closes untouched startup
  workspaces. Restoring into an existing server leaves focus and startup workspaces alone.
- On natedev, `herdr-agents.service` is a transient unit that `systemd-run` creates.
  The upkeep timer restores producers through shared resume.
  It does not run `pane_restore.py`.
- The herdr startup path adopts an answering server. Otherwise it starts one with
  `systemd-run` on Linux. On the Mac it starts the server directly from a desktop
  session, or opens an attach window to start it from a non-desktop session.
  Herdr can replay saved pane commands when its configuration allows agent restore.
  Claude commands wrapped by `lib/relaunch.py` recover the matching conversation's
  session labels and resume note. This is separate from `agents restart`.
- `pane_restore.py` is an explicit command for staff rows and saved panes without staff rows.
  No service invokes it automatically.

Settings entry for the user, one more SessionEnd item:

```json
{"hooks": [{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/session-end-pane-record.py\""}]}
```

- The Mac has no `/proc`. There `pane_record` reads a parent with `ps -o ppid= -p <pid>`, and the
  arguments and environment from the kernel's KERN_PROCARGS2 record (`account.kern_procargs2`,
  split by `account.procargs2_parts`). On both machines an environment keeps only
  `CLAUDE_CONFIG_DIR` and the names the entry keeps; every other value, a token included, is
  dropped as it is read. The Mac hides the environment of a system binary such as `/bin/zsh`,
  never of Claude.

## Codex panes

- A Codex pane launch passes `--no-daemon`. A daemon-attached Codex reports no `agent_session`.
- A fresh Codex worker gets one short first turn, `WORKER_FIRST_TURN_TEXT`, with the limit
  `WORKER_FIRST_TURN_SECONDS` (`config/direct.conf`). `worker_panes.start_or_resume` then publishes
  the staff row from the pane's `agent_session`.
- A known identity launches with `resume <id>` and gets no first turn.
- A Codex launch entry holds `codex_policy`: the `--sandbox`, `--ask-for-approval` and `-c` option
  and value pairs, in launch order (`pane_launch.launch`, paired and `--opt=<v>` forms, stopping at
  `--`). A Claude entry has no such field.
- `pane_record.resume_arguments` gives `resume <id>`, then `--model <m>` when set, then the recorded
  pairs unchanged. So a restored worker keeps its `model_reasoning_effort`, `service_tier`,
  rate-limit nudge and `notify` settings. An entry without the field resumes with `resume <id>` and
  `--model` only.
- The record leaves out a `-c projects=...` pair. `codex_trust.trust_arguments` puts its own
  `projects` pair before every Codex resume, and a recorded pair after it would win after a folder move.
- `herdr.pane_native_process` reads pane membership for one foreground `codex`:
  - an `agent_session` equal to the row's `current_session` proves the pane;
  - a `resume` argument proves the pane when the report is empty (`UnreportedResume`: a resumed
    Codex reports nothing before its first prompt) or equals the argument;
  - a report that names another session proves nothing, and so does a pane row that cannot be read.
- `herdr.native_process` uses the resume argument or the pane report. It reads no open rollout file.
- Callers without a report use the process's own pane report through the same reader.
- `PANE_AGENT_START_SECONDS` is the one launch deadline, for a worker and for a side pane.
- `herdr.child_environment` removes `NO_COLOR`. With it, Claude draws no SGR, and `_composer_input`
  reads the faint placeholder as typed text.
- The pane's terminal owns `TERM` and `COLORTERM`. `herdr._pane_environment` removes them at a new
  tab, a split and an existing-pane launch. A Codex tool shell holds `TERM=dumb`. With it, `/etc/zshrc`
  turns off the line editor, so zsh keeps the raw tty that a stopped Codex leaves, and the Enter
  from `pane run` never ends a line.

## Restore services

`scripts/maintenance/herdr_upkeep.py` restores admitted producers through the shared resume path.
`config/herdr_upkeep.toml` sets a 120-second interval and a 60-second run limit.

Each upkeep run uses this order:

1. Restore or adopt the shared herdr server.
2. Save the shared layout.
3. Refresh the detection rules.
4. Resume each selected producer through `stop_resume.resume(str(row.id))`.

`accounts.producer_upkeep_members()` reads the local staff snapshot.
It selects active, local, unpaused producers from both families, without exit requests.
Absent staff returns `()`.
One `accounts.settings_report_scope()` covers the operation.
Invalid producer settings exclude that producer and produce one warning per operation.

Each producer has its own tab in the configured `PRODUCER_WORKSPACE`, normally `producers`.
Resume uses its current conversation, current `[producers]` settings, and family account.
A live producer starts nothing.
A paused producer stays stopped.
An exit request also excludes the producer from upkeep.
Upkeep does not admit a configured name that has no staff row.
The [first-start contract](shutdown-and-restart.md#producer-launch-and-admission) defines admission.

A failed start, `ResumeFailed`, unknown session, or exception prints one stderr line for that producer.
The loop continues with the other producers.
The next upkeep run retries the failure without a stored failure record.
The start order rotates by `int(time.time() // interval_seconds) % len(producers)`.
Run spacing that repeats this remainder can repeat the same first producer.

`run_job` passes the run deadline as `HERDR_UPKEEP_DEADLINE`.
Each resume uses the earlier run deadline or `now + [shutdown] START_SECONDS`.
At the run deadline, the loop prints `producer budget ended` and ends.
The next run selects the remaining stopped producers again.

After a herdr restart, upkeep restores missing producers through this same path.
After a reboot, the timer provides the same recovery when the user service starts.
On natedev, the NixOS upkeep module starts with a graphical login.
Recovery before login requires `users.users.${username}.linger = true` and `nate.jobs.herdr-upkeep.needsDesktop = false`.
No service automatically invokes the explicit `pane_restore.py` command for all saved panes.

Claude launches write folder trust in the target account before the process starts.
The write uses the repository trust key, or the folder itself outside a repository.
A failed write prints one anomaly with the folder, account file, and error.
The launch then returns its failure result without a process start.
A composer with a draft waits until the deadline without changes to the draft.
