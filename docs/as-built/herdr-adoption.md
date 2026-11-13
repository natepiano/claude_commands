# herdr adoption

## What it is

Every showrunner, unit director, and standalone Claude session uses a tab in the shared herdr session, `agents`, on both machines.
Each production has one workspace.
Each producer uses its own tab in `PRODUCER_WORKSPACE`, normally `producers`, for either family.
Other standalone sessions use a workspace with their tab name.
A unit uses its fixed parent row's production workspace.
No launcher starts a separate herdr session.
`start_server` and `restore_server` refuse other session names.
Open `agents` with `herdr session attach agents` (`hsa agents`).
Use `herdr_sessions.py attach-remote <host> <tab>` for a tab on the other machine.
Workers use interactive Claude or Codex panes under their staff parent.
The implementation uses `scripts/herdr/`, `scripts/production/`, `scripts/message/roster.py`, and `scripts/agents/`.

## How it works

### Key files

| File | Role |
| --- | --- |
| `scripts/herdr/herdr.py` | The library: `Herdr` client, session names, server start and stop per platform, panes and agents, process placement, attached clients, server environment, Claude trust. |
| `scripts/herdr/herdr_sessions.py` | CLI: `list`, `attach`, `focus`, `start`, `resume`, `list --on`, `attach-remote`. |
| `scripts/herdr/test_one_session.py` | The check that fails when code outside `herdr.py` starts a herdr session or names one other than `agents`. |
| `scripts/herdr/detection_rules.py` | The local override of herdr's Claude detection rules and its reload receipt (herdr behavior, below). |
| `scripts/herdr/fake_herdr.py` | Test double for the herdr CLI. |
| `scripts/production/unit_lookup.py` | Units selected by native runtime ids on herdr panes. |
| `scripts/production/session_pane.py` | Read, type, send keys and locate a unit or session pane. |
| `scripts/production/add_unit.py` | Starts and restores unit directors in herdr. |
| `scripts/production/add_showrunner.py`, `promote_showrunner.py` | Start a lower showrunner in herdr; turn a unit into one. |
| `scripts/production/fake_unit_host.py` | Production fixtures over the fake herdr CLI and a fake `/proc`. |
| `scripts/message/roster.py` | Joins units with session labels and interactive Codex panes into the roster. |
| `scripts/agents/session_types.py` | Host wire types, including the legacy tmux forms. |
| `scripts/agents/inventory.py`, `stop_resume.py`, `restart.py` | Host detection, unit session stop, screen reads, herdr restarts. |
| `scripts/lib/detached.py` | Detached starts on the Mac (herdr server, shutdown conductor, Remote Control fixer). |

Outside this repo, `/etc/nixos/modules/common/shell.nix` defines `herdr-claude` (runs `herdr_sessions.py start "$@"`) and the `hsa` alias. It also guards the `claude` alias with `HERDR_AGENT_PANE`. `/etc/nixos/modules/linux/memory.nix` puts `herdr` in earlyoom's `--avoid` list.

### The herdr library

**Binary and client.** `herdr_binary()` is `HERDR_BIN`, else `shutil.which("herdr")`, else `HerdrMissing`. `Herdr(session)` targets one session with `argv(*args) = [binary, "--session", session, *args]`. `run` returns stdout. `result` decodes the JSON `result` object. A non-zero exit raises `HerdrFailure(message, code)` carrying herdr's `error.code` and `error.message`, and a subprocess timeout (10 s default in `run_program`) becomes `HerdrFailure` too. Subclasses: `HerdrMissing`, `SessionTaken(name)`, `AgentBlocked` (herdr's `agent_blocked`, never retried), `AgentNotReady(screen)`, `AgentExited(session, pane)`, `MacDesktopUnavailable`, `MacDesktopTerminalRequired`. `Herdr` methods: `panes`, `pane_id`, `pane_activity(pane) -> PaneAtShell | PaneRunning(commands)`, `open_tab`, `focus_workspace`, `close_startup_workspaces`, `start_agent`, `readiness`, `prompt`, `wait_until(pane, state, timeout_ms) -> ObservedAgentState | WaitTimedOut` (a `blocked` state raises `AgentBlocked`), `read(pane, lines, *, joined, ansi)` (`pane read --source recent|recent-unwrapped`), `send_keys`, `close_pane`, `agents`. Every herdr JSON schema has a `TypedDict`. Every timeout a caller passes is integer milliseconds. The library reads no account variables, so callers pass the pane environment.

**Session names.** A name matches `[A-Za-z0-9_.-]{1,64}` and is never `.` or `..`. `_validate_session_name` raises `ValueError` before any server starts or stops. `server_unit(name)` is `herdr-<name>.service`. `generated_session_name(text, suffix="")` maps other characters to `-` and fits `-<suffix>` inside 64 bytes. A session's socket is `$XDG_CONFIG_HOME/herdr/herdr.sock` for `default`, else `.../herdr/sessions/<name>/herdr.sock`. `live_sessions() -> list[SessionEntry]` returns only running sessions from `herdr session list --json`.

**The one session.** `shared_session()` is the name in `$XDG_CONFIG_HOME/herdr/shared-session`, else `SHARED_SESSION` (`agents`). It is never empty. `_require_shared(name)` raises `HerdrFailure("herdr session <n>: every Claude session runs as a tab in herdr session agents; …")` for any other name, and both server starts below call it first. `attach_hint(space, tab)` is the line launchers print: `herdr session attach agents, workspace <space>, tab <tab>`. `Herdr.focus_tab_of(pane)` shows that pane's workspace and tab in the attached clients.

**Server start.** A server is answering when `herdr --session <n> status server` exits 0 and prints `status: running`.

- `start_server(name, timeout_ms, *, session_kind=...) -> ServerCreated | ServerAdopted` adopts a server that already answers. Every launcher adopts: the shared session is one server for all launches.
- On Linux it submits `systemd-run --user --unit=herdr-<n>.service --collect -p CPUWeight=1000 -p MemoryLow=1G -p MemorySwapMax=0 --setenv=SHELL=<wrapper> --setenv=HERDR_PANE_SHELL=<shell> herdr --session <n> server` (`SYSTEMD_RUN`), polls every 0.1 s, and judges ownership by `systemctl --user is-active` (`SYSTEMCTL`). The shared session's boot unit, when a unit file defines it, starts with `systemctl --user start` instead, with no probe, no properties and no wrapper.
- Pane scopes (Linux, transient unit only). The unit holds only the server process. Each pane shell runs in its own scope under `app-agents.slice` (`PANE_SLICE`). Reason: 54 claude, 18 codex and cargo shared one cgroup with the server, and 80% of the server was in swap (typing lag, builds). The properties give the server CPU first and keep its memory out of swap.
  - Before each start, `systemd-run --user --scope --quiet --collect -- true` (5 s limit) checks that scopes work. Then the wrapper is written to `$XDG_STATE_HOME/herdr/pane-shell` (default `~/.local/state`). The path is the same for every worktree. The write is a temp file, mode 0755, then a replace.
  - herdr 0.9.5 runs `$SHELL` with no arguments for a pane. A systemd-run service gets the user manager's environment, so the start sets both values: `SHELL` is the wrapper, and `HERDR_PANE_SHELL` is the caller's `SHELL` when it is an absolute, executable file that is not the wrapper, else `/bin/sh`.
  - The wrapper (`_PANE_SHELL_SCRIPT`) removes `HERDR_PANE_SHELL`, sets `SHELL` back to the real shell, and execs `systemd-run --user --scope --quiet --collect --slice=app-agents.slice -- <shell>`. The systemd-run path is absolute when it resolves. The scope runs in place, so the shell pid does not change and the scope is `run-p<pid>`. `SHELL` is reset because Claude's Bash tool reads it: the wrapper there would start a scope on each command.
  - When the probe or the write fails, one stderr line says so, and the server starts without the two values. Then panes run the plain shell inside the unit.
- On Darwin it goes through `_start_darwin_server`. It starts `herdr --session <n> server` with `detached.start_detached`. The readiness check is that the server answers and the only holder of the session socket (from `lsof`) is our own child, within `min(10 s, timeout)`. A different holder means our child gets SIGTERM, and the start returns `ServerAdopted`. Outside the desktop it raises `MacDesktopTerminalRequired` before starting anything.
- Every server this code spawns has its restored workspaces closed at once (`_clear_restored_workspaces`). herdr reopens the panes saved in `session.json` when a server starts, and a new server holds no live work.
- A start that fails names `herdr-server.log` beside the socket when it exists.

**Restore and the Mac desktop rule.** `security_session() -> DesktopSession | NonDesktopSession | SessionKindUnreadable | NotMacSession` runs `launchctl managername` (`LAUNCHCTL`) once per command. `Aqua` is the desktop, an unreadable answer counts as non-desktop, and off the Mac it is `NotMacSession`. Callers carry the value through, and `SessionKindNotRead` is the default that makes the library read it. `restore_server(name, timeout_ms, *, session_kind) -> ServerCreated | ServerAdopted | WindowServerCreated` starts directly off the Mac or on the desktop. A Mac outside the desktop takes these steps instead:

1. Within `min(15 s, timeout)`, an answering server is adopted, and a listener that has not answered yet raises `SessionTaken` (another launch is starting it).
2. A timeout or failure while checking raises `HerdrFailure("herdr session <n>: could not check existing listener: ...")`.
3. It opens `open -na "/Applications/Nix Apps/Ghostty.app" --args -e <herdr> session attach <n>` (`SHUTDOWN_OPEN`) and waits for that window's client to start the server. Success clears the restored workspaces and returns `WindowServerCreated`. Failure raises `MacDesktopUnavailable`, whose text is `MAC_DESKTOP_REQUIRED`: `the Mac desktop is locked or signed out: unlock it and restart`.

Direct starts outside the desktop (`herdr_sessions.py start` and `resume`) refuse with `MAC_DESKTOP_TERMINAL_REQUIRED`: `herdr must start from the Mac desktop: run this from a Mac terminal`.

**Stop.** `stop_session(name)` runs `herdr --session <n> server stop`. On Linux, if the unit is still active, it then runs `systemctl --user stop herdr-<n>.service`.

**Panes and agents.**

- `open_tab(name, space, title, cwd, environment) -> pane_id` opens a tab in the workspace `space`, then renames its pane to the title. Every pane also gets `HERDR_AGENT_PANE=1`. herdr 0.9.0 sets these variables in each pane: `HERDR_PANE_ID`, `HERDR_SESSION`, `HERDR_SOCKET_PATH`, `HERDR_TAB_ID`, `HERDR_WORKSPACE_ID`, `HERDR_BIN_PATH`, `HERDR_ENV=1`.
- `start_agent(name, pane, kind, agent_arguments, opening: OpeningPrompt | NoOpeningPrompt, timeout_ms, *, agent)` exists as a module function, which names the agent `agent`, and as a `Herdr` method. Its limit is `timeout_ms`. The limit covers the start and the opening prompt; each `agent start --timeout` attempt takes at most herdr's 300000. A `PromptNotSubmitted` that the launcher never typed has `never_typed` set, and add_unit then closes that new session (`close_untyped_session`): its staff row and worktree stay, and the same command starts it again. It runs `agent start <agent name> --kind <kind> --pane <pane> --timeout <ms> -- <agent_arguments>`. herdr adds the binary itself, so an `agent_arguments` whose first element equals `kind` raises `ValueError` before any herdr call. Agent names are lowercased, other characters become `-`, `a-` is added in front when the name does not start with a letter, and the result is cut to 32 characters.
- `agent_pane_busy` is retried every 0.1 s against one shared deadline. A retry never passes a per-try timeout at or below `_AGENT_START_TIMEOUT_MINIMUM_MS` (3000).
- While the CLI waits, the library finds the pane shell (`pane process-info`) and walks its process tree on Linux (per-thread `children`). It follows the agent by pid plus start ticks across exec and reparenting. An agent that was seen and is gone, or herdr's `agent_start_failed` with `process exited`, raises `AgentExited`. When `/proc` cannot be read it falls back to herdr observations at most twice a second.
- `agent_not_ready` reads the visible screen and raises `AgentNotReady(screen)`. No key is sent.
- After a ready start, `OpeningPrompt(text)` is sent once with `agent prompt`. `NoOpeningPrompt` sends nothing.

**Placement.** `HerdrPlace(session, pane)` is where a process lives. `NotInHerdr` means the process has no place.

- Linux: `_live_server_processes` reads listening sockets (`00010000`) at herdr socket paths in `/proc/net/unix` (`HERDR_PROC`). It maps each one to its single holder through `/proc/<pid>/fd`. The socket names the session, never the argv. `_process_sessions` then gives each server's descendants that session. It also catches handoff survivors, which a handoff does not reparent to the new server:
  - members of the server's own `herdr-<n>.service` cgroup;
  - in a shared scope, the pane shell pids the server reports (`pane list` plus `pane process-info`) with their descendants;
  - processes in a pane scope under `app-agents.slice`. A pane scope belongs to the session of a matched process in it, as the unit cgroup did before pane scopes. Only when a pane scope has no matched process (after a handoff) do the unit servers also report their pane shell pids. Without a handoff the parent walk explains each scope, and herdr is not asked. A scope with two sessions (a server started inside a pane) gives its other processes neither.
- `herdr_place_of(pid)` reads the pane location. `herdr_processes()` returns process evidence with native runtime ids.
- A process with a non-empty `TMUX_PANE` (a tmux the user runs by hand) is never a herdr place, even when it inherited herdr variables. An empty `TMUX_PANE` excludes nothing.
- Darwin: processes come from `ps -U <uid>` (`HERDR_PS`), and each row's uid is checked again. Arguments come from a separate `command=` column split after the `comm=` executable; unreadable arguments are `UnreadableProcessArguments`. Listeners come from `lsof -n -P -a -U -u <uid> -F pfn` (`HERDR_LSOF`). `darwin_process_environment(pid)` reads `ps -o command=`, then `ps eww`, then the command again, and parses only the text after a stable argv prefix. cwd comes from `lsof -d cwd`; an unreadable one is `UnreadableCwd` and keeps placement and marks. A failed `ps` with empty output whose pid `os.kill(pid, 0)` confirms gone raises the private `_DarwinProcessGone`, and that process is skipped. `darwin_process_identity(pid)` (`lstart`, `tty`) and `darwin_open_files(pid)` serve the shutdown snapshot.
- `_PLATFORM = sys.platform` is read once at import.

**Clients and server environment.** `attached_clients(name) -> ClientsAttached(count) | NoClientAttached | ClientsUnreadable(detail)` counts this user's `herdr session attach <name>` clients and bare `herdr` TUI clients for that session. The session comes from argv `--session` first, then the client's `HERDR_SESSION`, else `default`. A client that exits mid-read counts as gone. `server_environment(name)` returns the environment of the single process holding the session's listening socket. Callers refuse when it holds a variable that must be unset, since panes inherit the server's environment.

**Shared session layout.** Every Claude launcher opens a tab in `herdr.shared_session()`.
`Herdr.open_tab(space, title, cwd, environment)` finds or creates the named workspace.
The showrunner staff row's `name` is the workspace name.
A unit uses its showrunner parent's `name`.
The workspace label does not depend on a running showrunner.
A standalone session uses a workspace with its tab name.
The [launch-name reader](shutdown-and-restart.md#launch-names) supplies member titles.
The title hook preserves worker tab titles and updates their pane labels.
A stop locates the unit's pane through `unit_lookup.labeled_session`, then calls `Herdr.close_tab_of`.
Short tab titles can repeat across workspaces.

**Tab order (user, 2026-10-18; restart keeps it, user 2026-10-21).** A restart puts every session back in its old workspace and tab, in the old order. `layout_path(session)` (`$XDG_STATE_HOME/herdr/layout-<session>.json`, else `~/.local/state/herdr/`) holds the saved `SavedLayout{at, workspaces: [SavedSpace{name, seen, tabs: [SavedName{name, seen}]}]}`. `Herdr.record_layout()` lays the live layout over it with `merged_layout`: live order wins, and an entry no longer live stays after the saved entry before it (`merged_order`) until it is unseen for `LAYOUT_KEEP_SECONDS` (14 days). Placeholder tabs and workspaces with no other tab are never saved, so an empty server saves nothing. `open_tab` saves after each tab it adds, `retitle` after each rename, and the snapshot pass through `save_shared_layout(running)`, which also catches a tab renamed in herdr itself. A failed save is a stderr line (`save_layout`); the tab stays open. `_LayoutLock` (`flock` on the layout file's `.lock`) lets one launcher at a time change the shared session's workspaces and order. herdr 0.9.5 cannot move a workspace, so creation order is the only workspace order. When `open_tab` needs a workspace on a server that holds none of the saved ones (a fresh server after a reboot), `_place_saved_workspaces` first creates every workspace seen within `LAYOUT_RESTORE_SECONDS` (one day) of the last save, in saved order, each with one tab and pane titled `restoring` (`LAYOUT_PLACEHOLDER`) in the home folder. The day covers a restart cut short by a second reboot. The first real tab in a workspace closes its placeholder. A workspace whose sessions never come back keeps its `restoring` tab until the user closes it: restart runs one account per pass, so no pass can tell that another account will not fill it. `Herdr.order_tabs(workspace, saved)` uses `tab_order(space, titles, saved_tabs, unit_director_titles)`. Saved tabs keep their saved order. Other tabs use this rule: the showrunner's tab comes first, then unit directors A-Z, then all other tabs A-Z (user, 2026-10-28). The showrunner's tab has the workspace's name. The first tab stays first, also when its title differs. `unit_director_panes(session, processes)` selects staff rows through the native runtime ids of pane processes. A selected unit row identifies a unit director tab. `order_tabs` reads every pane in each tab. `open_tab` uses the current native runtime id to classify a new unit tab. `herdr_sessions.py sort-tabs` calls `Herdr.sort_tabs()`. The method holds `_LayoutLock`, ignores saved tab order, applies the rule to each workspace, then saves the new order. A workspace with a tab that has several panes keeps its order. The method returns its label in `WorkspacesKeptForSplitTabs.labels`. The command prints one line with each returned label. herdr 0.9.5 has no command to move a tab, but `pane move --new-tab` puts the pane in a new tab at the end. So `order_tabs` moves each tab from the first one out of place, in order, and gives each new tab its old title. The pane keeps its id and its process. Focus goes back to the workspace and tab that had it. `order_tabs` keeps the order when a tab that must move has several panes. A tab with several panes before the first one out of place does not stop the sort. The method returns each moved pane's new id by its old one.

**Workspaces and focus.** `Herdr.focus_workspace(id)` runs `workspace focus`. `Herdr.close_startup_workspaces(restored_workspace)` closes every other workspace that is still herdr's untouched startup shell, and lists workspaces and panes again before each close. Untouched means a workspace labelled `~` with one tab, one pane and `agent_status` `unknown`, whose pane has no agent, `agent_status` `unknown`, cwd and foreground cwd equal to home, `revision` 0, and `scroll.max_offset_from_bottom` 0. `_StartupShellEvidence` is `UNTOUCHED` or `PRESERVE`. Missing or mistyped evidence keeps the workspace.

### Session commands

`scripts/herdr/herdr_sessions.py`:

- `list` prints `<tab>\t<role>\t<claude name>` per pane of the shared session (nothing when it is not running). Role is `unit <showrunner name>/<unit>` from pane marks, `showrunner` when the Claude name is a registered showrunner session, else `standalone`. The Claude name is the newest live session record that `herdr_place_of` places in that pane. Exit 0 done, 1 not found, 2 usage, 3 herdr unreadable.
- `sort-tabs` sorts every workspace by showrunner first, then unit directors A-Z, then other tabs A-Z. It ignores saved tab order and saves the result. It keeps focus. A workspace with a tab that has several panes keeps its order; the command prints its name.
- `attach <tab>` focuses that tab (`focus_tab_of`) and execs `herdr session attach agents`. `focus <tab>` only focuses it and prints the shared session's name. A tab that is not open exits 1 and lists the open ones.
- `list --on <host>` runs `ssh <host> ~/.claude/scripts/lib/py ~/.claude/scripts/herdr/herdr_sessions.py list` within `REMOTE_LIST_TIMEOUT` (15 s). A timeout, `OSError` or non-zero ssh exit prints one stderr line naming the host and returns 1. `attach-remote <host> <tab>` runs `focus <tab>` on the host over ssh, then execs `herdr --remote <host> --session <the name it printed>`. `RemoteHost(target)` refuses an empty, `-`-leading or whitespace-bearing target with `ValueError`, exit 2. Nothing is saved or synced between machines.
- `start [--name N] [--cwd D] [-- <claude args>]` and `resume <name> --session-id ID --cwd D [--config-dir D] [--restore] [--prompt-file P] [--attach] [-- <claude args>]` run these steps:
  1. Refuse outside the Mac desktop.
  2. Resolve the folder and validate the name.
  3. Pick the account with `accounts.account_for_unscoped_launch`. A `LegacyHomeLaunch` uses `~/.claude.json` and unsets the named-account variables. Otherwise the pane gets `named_account_environment(dir)`.
  4. Write Claude folder trust in the target account before starting Claude. A failed write prints one anomaly and prevents the start.
  5. Launch without a shutdown hold or admission claim.
  6. Run `start_server(agents, 90_000)`, and refuse when `server_environment` holds `CLAUDE_CODE_OAUTH_TOKEN` (or the named-account variables for a legacy home), since a new tab inherits it.
  7. Open a tab in a workspace with the tab name (`open_tab`). Refuse a supplied `--name` when an open tab already uses it. Otherwise, generate a name with `generated_session_name(<folder>, "1")`. Increase the suffix until the name is free.
  8. Run `start_agent` with `[--resume ID] -n <name>` and an `OpeningPrompt` from `--prompt-file`, else `NoOpeningPrompt`.
  9. `start` and `--attach` then exec the attach.

  Failures print one line and exit 1. Ctrl-C prints `herdr_sessions: stopped` and exits 130.

### Claude trust before every launch

`copies.py clone` checks source trust through `herdr.claude_trust` in every Claude account home.
Trust in any home authorizes the clone in every existing account file.
The clone record stores its resolved `claude_trust_folder` before the locked accounts writer grants trust.
Removal deletes that folder's complete project entry after the external removal mark exists.
It preserves other project entries and settings and skips missing account files.
Malformed or unreadable account files produce an anomaly while the other homes continue.

Every Claude launch writes trust before starting Claude. The target account does not need an existing trust entry.

- `trust_claude_launch(folder, config_dir)` calls the shared `_write_claude_trust` writer.
  The writer sets `projects[key].hasTrustDialogAccepted` in the target account's `.claude.json`.
  It holds `accounts.claude_config_lock` across the read and atomic replacement.
  It preserves other settings and writes the file with private permissions.
  `claude_config_file` selects the account file. The legacy home uses `~/.claude.json`; named accounts use their config directory.
- `repository_trust_key(folder)` gives the exact repository key from `git rev-parse --git-common-dir`.
  A folder outside Git uses its own resolved path.
  The Git call removes inherited `GIT_*` overrides and disables global and system configuration.
- `launch_agent`, `restart.start_row`, `add_unit`, and `add_showrunner` use `launch_prepared_agent` for the trust write.
  The direct `stop_resume` path calls the same trust helper before running its pane command.
  Preflight does not refuse a folder because the account lacks trust.
- A failed write returns the existing launch failure result and starts no Claude process.
  One anomaly line names the folder, account file, and error.
  The line excludes account settings and file contents.
- `claude_trust` remains a reader with `Trusted`, `Untrusted`, and `UnreadableTrustFile` results.
  `copy_claude_trust` retains its source-account behavior and uses the same JSON writer.

### Pane access

`session_pane.py` reads and types through `HerdrPlace(session, pane)`. `read_place(place, lines=60, *, joined=False, ansi=False)` uses herdr's recent pane text. `type_place` sends an agent prompt, `keys_place` sends keys, and `where_place` prints `herdr:<session>:<pane>`. `record_place(record)` resolves the registry pid through `herdr_place_of`. `session_place(id)` first picks the newest live record for that id.

The CLI takes `read|type|keys|where` and either `unit <showrunner name> <unit>` or `session <id>` (`read` also takes `--lines N --joined --ansi`). A trust, permission or question prompt refuses typing with exit 4 and `blocked at a prompt; nothing typed`. A missing target exits 1, usage 2 and host failure 3.

These consumers use this path:

1. Effort changes through `unit_effort.py`, showrunner promotion, and relays.
2. The stall watch and `unit_status.sh`, each with 400 joined lines.
3. Shutdown screen checks through `restart_support._screen`, with 60 lines, and the Remote Control fixer.

`restart_support._screen` checks the registry session identity at the unit's pid before resolving its pane.
A mismatch returns `ScreenUnreadable("session registry identity changed")`.
`remote_control.py` finds the newly registered pid's pane through `record_place`.
It confirms process identity and idle state.
It then types `/remote-control` unless the pane already shows `/remote-control is active`.

### Worker questions

A worker puts its question in its reply and ends the turn.
The turn hook sends that reply to the director.
An unexpected screen prompt ends no turn, and the director finds it only when it reads the pane.

`worker.py assign` returns exit 4 when a worker asks a question or is blocked, including before dispatch submission.
It prints `question: <worker> pane <pane>` and the pane's recent visible lines.
Exit 6 means busy before submission.

1. Read the question text after the `question:` line.
2. Run `worker.py answer <name> --text "<answer>"` from the director's session.
3. End the director turn until the completion hook sends its next message.

A blocked Codex worker receives `shift+left`, the answer through `pane send-text`, and two `enter` keys.
Herdr checks the target before each key.
Idle workers and blocked Claude workers use `agent prompt`.
For an active pass, that prompt includes the saved DONE instruction without changing the pass or saved delivery.
Use `answer` to resubmit work when compaction consumed the assignment prompt.
A turn without an assistant reply reports `no DONE` when the transcript records compaction after the accepted dispatch prompt.
The pass stays active, and the pane stays open, including a managed worker with `end_worker` set.
The matching DONE reply then uses the normal completion hook.
The command refuses workers held by another parent.
A repeated saved launcher reads the transcript and returns without another submission.
For a completed dispatch, each explicit rerun sends one completion notice through the shared hook code.
Each completion notice ends its first line with `; pass <dispatch instance id>`.
An explicit rerun repeats the saved completion text with the same pass id.
This rule also applies to a completed managed Astra review.
The saved completion preserves the reason and pane for a close anomaly.
A successful completed rerun sets its status file to `done` after it sends the notice.
Each notice names the worker and uses its native identity, including reruns from the director.
The saved completion supplies the sender identity for a retired managed Astra reviewer.
The sender identity remains the same after a session change and retirement.
Turn and run events send one notice when the outcome, reply, or run ids change.
An unchanged outcome, reply, and run ids send no notice.
Waiting notices add `; runs <run ids>` to the first line.
The run ids include received results that still await a worker reply.
An empty turn reply writes no reply file and adds no `; reply <path>` text.
Run events retain the last worker reply while a result awaits a reply.
`reap_orphans` retries a pending completion only after its supervisor is gone.
A live run reports `waiting on runs`, including when an earlier result awaits a reply.
A turn without DONE sends one `no DONE` notice when no pending work explains it.
The notice includes the worker's last reply and leaves the pass active.
Named adversary and contract reviews record `done` through the same hook.
The hook also saves an assigned style review result.
Each ignored event appends one JSON line to `$XDG_STATE_HOME/herdr/turn-end-ignored.jsonl`.
The default path is `~/.local/state/herdr/turn-end-ignored.jsonl`.
Each line records the time, exit reason, event kind, thread id, and worker name when known.
This diagnostic changes no outcome and sends no notice.
A refused launcher retry retains the existing pass and its run, including a run with one worker.
A later DONE reply corrects a launcher error for the same pass and permits pane closure.
The review trial uses the latest completion duration for each pass.
The hook follows the worker's link to its existing pass state, including runs outside the run root.
Exit 0 means submitted. The turn hook records completion and sends the director its reply path.

Pane starts record the account folder in the staff row's `home` through `staff.set_home`.
Account moves and restarts use the same pane publication call.
A pane launch retains `config_dir` only when the session has no staff row.
`pane_launch.config_home` reads a staffed session's `home` or the unstaffed launch's `config_dir`.
`stop_resume.py` uses this reader for retained accounts.
The launch environment carries the selected target account until the start hook publishes its home.
`add_unit.ensure_staff_unit` calls `pane_launch.staff_admitted` after unit admission.
Worker pane admission and `staff_sources.ensure_staff_showrunner` use the same completion call.
These calls complete a pane publication that preceded its staff row.

### Matching a running session to its staff row

`unit_lookup.py` reads process evidence through herdr.
`Place = HerdrPlace` identifies the session and pane.
`Claude = LiveClaude | ClaudeNotRunning | ClaudeUnknown` describes the observed process state.

1. Read the native runtime id from the process evidence.
2. Select the staff row through `StaffSnapshot.by_current_session`.
3. Read the name and parent from the selected row.

Claude processes use their Claude session id.
Codex processes use their Codex thread id.
A fresh Codex uses its pane session report before it has a resume argument.
The identity reader excludes only `scripts/herdr/turn_end.py` from the foreground process count.
Every other foreground process prevents the pane report from proving a sole Codex process.
Copied `STAFF_ID` values, folders, names, and retired pane marks do not select rows.
`RecordedLabels.resolve` and `StaffLabels.environment` remove retired identity from the environment.
`session_staff.LaunchMembership` resolves a saved staff reference to the current native runtime id.
New launches carry no copied staff identity.

Unknown process evidence produces one anomaly line.
Healthy units retain their status output.
Ambiguous process evidence preserves the unknown state.

The CLI accepts `list <showrunner name>` and `pane <showrunner name> <unit>`.
`list` prints the unit, place, session, state, name, session id, and socket.
`pane` prints the selected place.
Exit codes are 0 for success, 1 for an absent unit, 2 for usage, and 3 for unavailable evidence.

### Launchers

**Units.** `add_unit.launch_session` starts each new or restored unit through `herdr.launch_agent` in the shared session.

- The pane receives `ENABLE_TOOL_SEARCH=true` and its selected account variables.
  A resume applies `prepared_account` and removes the account's named exclusions.
- Claude receives the model, optional effort, and native resume id.
  The [launch-name reader](shutdown-and-restart.md#launch-names) supplies `-n`, `--remote-control`, and the tab title.
- `host_session_name(request)` generates the herdr agent name from the production slug and requested session name.
- The launcher uses the production workspace and the requested directory.
  `AgentExited` closes the new tab.

Before starting the agent, add_unit saves its exact opening, conversation id, account directory and start time in `add-unit-openings/<unit>.json` in the checkout's Git directory. It clears this pending record only after prompt submission is proved. A rerun with that same marked conversation checks the live transcript and submits an absent opening to its existing pane, including a live `--resume` and a retry through `agents resume NAME --retired`. A pending record for a closed conversation is removed; a different live conversation still refuses. It starts no second session. Brief stubs and unit configuration writes use the shared atomic writer.

`add_unit.main` selects the account and runs preflight before it writes launch state.
A running native session refuses a plain resume and names `--attach` as the next action.
Unreadable registry or process evidence also refuses the resume.
An existing unit resumes its recorded plan without requiring a Production header in that plan.
Its requested directory must match the worktree in its staff row.
A different recorded plan refuses reuse.

A restore uses the script prompt envelope and the transcript's last assistant model unless `--model` overrides it.
A new unit waits for Remote Control readiness.
The launcher prints the unit's attach hint after launch.
`AgentNotReady` and other failures exit 1; a refusal exits 2.

`--attach <id> --cwd <dir>` accepts a running session with `--plan`, `--standby` or `--permanent`. It checks the row, worktree and source account, calls `stop_resume.stop`, and requires the old process to be gone. It then writes the unit, resumes the same conversation once with session labels, checks prompt submission, and proves one new registered pid. Unknown liveness or account, a surviving old process, or an ambiguous final process count refuses. Plain `--resume` refuses a running session and directs the caller to `--attach`.

**Lower showrunners.** `add_showrunner.launch` starts the selected model and effort through `herdr.launch_agent`.
It uses the production workspace and selected account environment.
The [launch-name reader](shutdown-and-restart.md#launch-names) supplies the tab title, `-n`, and `--remote-control`.
The opening is `/showrunner:produce <doc>` with `resume` when the conversation resumes.
The launcher proves prompt submission before it completes the start.
The command waits for the showrunner's registration before it attaches the child to its caller.

**Promotion.** `promote_showrunner.stop` types `/exit` with `type_place`. It waits up to `PROMOTE_STOP_WAIT_S` (60 s) for the Claude pid to go, then closes its tab with `close_agent(place.session, place.pane)`. `add_showrunner.main --resume <id> --cwd <worktree> --config-dir <showrunners account dir>` then resumes the same conversation as the child's showrunner. A failed start prints the `add_unit.py --resume` line that brings the unit back. Before stopping, promotion saves the unit, conversation, child doc and target account in `promotions/<unit>.json` in the parent's Git directory. A rerun uses that record when the unit is gone and resumes the saved conversation. If the child tab already holds that conversation, it finishes its opening and waits for its update timer without a second start. It repeats the same-parent attach so routing notices can finish. It clears the record after attachment succeeds and its retirement is reported.

### Roster

`roster.py` reads every production's marks once (`_every_labeled_session`). A director with a `LiveClaude` joins by session id. A director with no messaging socket joins when `herdr_place_of(record pid) == marked.place`. Worktree ownership is the fallback only when the marks cannot be read. `_herdr_codex_panes` runs one `herdr agent list` per live session and lists each interactive Codex pane as a freestanding row named `herdr <session>:<pane>`, with `CannotReceiveMessages("interactive Codex in a herdr pane")`. `HerdrMissing` lists nothing, and any other herdr failure is a problem line.

### Shutdown, restart and crash recovery

The [shutdown design](shutdown-and-restart.md) defines shared stop, resume, move, and restart.
`agents stop --all` saves live local staff row IDs before stopping their processes.
The selection includes producers from both families.
`agents restart` resumes missing rows from that list through `restart.start_row`.
Start reads the current conversation and folder from staff.
It uses `herdr.row_workspace` for the destination.
Producers use `PRODUCER_WORKSPACE`; units use their parent showrunner's workspace.
Other sessions use their own name or the shared session name.
Worker rows use `worker.restore` and the shared worker launch policy.
An idle worker remains closed until assignment.
A live session stays unchanged.
A failed start retains the row on the restart list.
Restart sends no work prompt.

Restart waits within `START_SECONDS` for an exiting agent in its retained pane.
It reuses that pane when fresh reads prove a shell with no native agent.
After that budget, restart opens the replacement tab.
The existing resume-check loop closes the old shell through the restore path when its agent exits.
The loop uses `RESUME_CHECK_SECONDS` and `RESUME_CHECK_INTERVAL_SECONDS`.
A caller without shared resume checks runs the same cleanup loop before `start_row` returns.
Failed destination cleanup uses the destination session.
Source cleanup uses the source session and its original terminal.
If the old agent outlives that check, one anomaly names the pane, which stays for retire.
A pane with a live agent, another native owner, or a changed terminal stays open.
The source record stays until closure, absence, or another native owner is proved.
Producer publication also keeps a prior binding while its pane remains open.
It removes a prior binding when a read proves absence or another native owner.
Consumers select a proved native pane before retained bindings.
When no native pane exists, consumers select the newest retained publication and keep the older records.

Retirement stops the unit and its workers before it closes their panes.
It closes each owned retained shell across tabs, even when the director pane is absent.
It keeps foreign panes and live agents.
herdr removes a tab when its last pane closes.
Retirement cannot find an idle shell whose launch record was lost before this fix.

Producers use their current `[producers]` model and effort through the shared launch code.
Codex producers select the workers account; Claude producers select the showrunners account.
An explicit target account overrides that selection.
The [producer admission contract](shutdown-and-restart.md#producer-launch-and-admission) defines first start and interrupted admission recovery.
The [upkeep service](herdr-restore.md#restore-services) restores admitted producers after server loss and reboot.
It excludes paused and exited producers.

A restore adopts an answering shared server.
On Linux, it starts a missing server through `systemd-run`.
On the Mac, a desktop session starts the server directly.
A non-desktop caller opens an attach window to start the server with desktop access.
A launch focuses a workspace only when it creates the window.
It then closes untouched startup workspaces.
An existing server retains focus and startup workspaces.

### Test doubles

- `fake_herdr.py`: `HERDR_BIN` points at it. It appends each argv to `FAKE_HERDR_LOG` as a JSON line. It answers from `FAKE_HERDR_RESPONSES`, keyed by the space-joined argv without the executable, else `"default"`. Fields: `stdout`, `code`, `stderr`, `write_files`, `require_paths` (a missing path exits 98) and `remove_paths`. A list replays in order and keeps the last entry. It refuses an `agent start` whose name fails `[a-z][a-z0-9_-]{0,31}` with `invalid_agent_name`. `herdr_listener(proc, pid, name, environment)` gives a fake server the session's listening socket in a fake `/proc/net/unix` plus an fd link.
- `fake_unit_host.py`: `environment(root)` sets `HERDR_BIN`, `HERDR_PROC`, `FAKE_HERDR_LOG` and `FAKE_HERDR_RESPONSES` for an empty host. `HostedUnit(session, pane, marks, screen)`. `write(env, units)` publishes a server process (pid `991000 + 2i`), its listener, a marked `zsh` pane shell, the session list, and pane reads (`recent` and `recent-unwrapped`; 3, 60 and 400 lines; with and without ANSI). `place_process(env, pid, pane)` puts a registry process under that pane's shell. `calls(env)` returns logged argv. `live_pid(test)` gives a real live pid for registry checks.
- Darwin cases fake `LAUNCHCTL`, `HERDR_PS`, `HERDR_LSOF`, `SHUTDOWN_OPEN` and `SHUTDOWN_SYSCTL`, and pin `herdr._PLATFORM`. Linux `/proc` cases use `scripts/agents/test_support.linux_process_fixture`, which pins `sys.platform` and `herdr._PLATFORM`.

Use these fixture rules on Linux and macOS:

- Resolve the temporary root in `add_unit_test_support.py` and `test_add_showrunner.py`. The add-unit launch, account, resume and restore suites share this fixture. On macOS, `/tmp` points to `/private/tmp`. Trust keys and worktree paths must use the same resolved root as the code under test.
- Set `HERDR_PLATFORM=linux` in each Linux launcher fixture's environment. Do not set it for the whole run. Darwin tests must still select the Darwin path.
- Keep `HERDR_BIN=/nonexistent/herdr` in `PromptSubmissionTests`' cleared environment. Its fake pane calls do not need herdr on `PATH`.
- Build a herdr client only in `setUp`, never as a class attribute: the class body runs at import, before any patched environment. The run-wide `HERDR_BIN` hides such a lookup, so check a new fixture once with an empty `PATH` and no `HERDR_BIN`.
- Pin the local machine in each fixture that stores staff locations on `natedev`. `HERDR_PLATFORM=linux` selects herdr process evidence, not the staff machine. A subprocess launcher uses `add_unit_test_support.LINUX_LAUNCHER_CLI`. A direct call patches the name the module imported, such as `add_showrunner.local_machine`.

Run these suites on the Mac with a temporary home.
Run the command from the root of a copy of the tree under test.
The block removes the temporary home when the tests end:

```sh
(
  set -e
  test_home=$(mktemp -d)
  trap 'rm -rf "$test_home"' EXIT
  env -u CLAUDE_CONFIG_DIR -u CLAUDE_CODE_OAUTH_TOKEN -u CODEX_HOME \
    HOME="$test_home" TMPDIR=/private/tmp HERDR_BIN=/nonexistent/herdr \
    PYTHONDONTWRITEBYTECODE=1 ./scripts/lib/py -m unittest \
    scripts.herdr.test_herdr scripts.herdr.test_herdr_sessions \
    scripts.agents.test_restart scripts.agents.test_account_move \
    scripts.production.test_add_unit scripts.production.test_add_unit_accounts \
    scripts.production.test_add_unit_resume scripts.production.test_add_unit_restore \
    scripts.production.test_add_showrunner \
    scripts.notify.test_push_texts
)
```

## Invariants

- Tests never start real herdr, Claude or Codex. They use `fake_herdr.py`, `fake_unit_host.py`, a fake `/proc` and fake session records, in temporary folders.
- Launchers write trust before starting Claude. They send no key to a trust, permission or question prompt.
  `agent_not_ready` reports the screen and sends no key.
- Trust counts only on an exact key: the folder, its toplevel, or its main checkout. An accepted ancestor folder never counts.
- The trust survey prints only the key, accepted and kind fields of `.claude.json`, never any other field.
- One prompt per launch, never sent twice. `agent start` carries no prompt; the opening prompt goes through `agent prompt` once the agent is ready. Callers choose `OpeningPrompt` or `NoOpeningPrompt` explicitly.
- Prompt input requires an agent status of `working`, `idle`, or `done` and an empty visible composer. An absent `interactive_ready` permits input. A boolean value controls input. A non-boolean value prints one readiness anomaly and prevents input. No Remote Control turn is required. A Codex worker prompt delivery returns the launcher only after its transcript records the prompt, within `WORKER_SETTLE_SECONDS`; without that proof the launcher returns 8. A Claude delivery returns before its transcript must contain the prompt. A matching transcript prompt prevents replay. A matching final transcript reply proves completion. With a matching transcript prompt, the hook can use its own DONE reply before the final transcript record appears. A live detached run still holds completion. A failed delivery command returns an error. An unreadable transcript prevents a retry. Both `idle` and `done` permit retyping after the settle time.
- Each composer read uses `pane read --source visible --format ansi` and removes faint text. SGR 2 starts faint text. SGR 22 or 0 ends faint text. The parser skips colour arguments for SGR 38, 48, and 58. A faint hint can appear in an empty composer.

The turn hook closes a completed pane at once after DONE.
The close retains the staff row and conversation.
The next assign resumes the conversation in a new pane.
The close reads no agent state, dispatch state, transcript, detached run, or foreground activity to permit closure.
It sends no graceful quit and calls no process check before closure.

The close keeps these identity and completion rules:

1. Match the pane's terminal id to the terminal from the evidence read.
2. Exclude a labelled pane that another staff session owns.
3. Acquire the worker lock with blocking enabled.
4. Confirm removal with `_pane_closed`; report "pane close is incomplete" if the pane remains.

A labelled pane in the parent's tab is a stopped worker when no other staff session owns it.
Its foreground process does not change that state.
A stopped worker resumes in its existing pane without a foreground activity read.
A failed family change closes a new destination pane after it stops the agent.
Restore resumes only workers with active dispatches.
Idle workers remain closed until their next assign.
Shutdown restart uses the shared worker restore.
Account moves stop and resume open workers in the same pane, including workers without active dispatches.
An account move closes no worker pane and runs no close check.
A closed worker without an active dispatch has nothing to move.
A busy worker receives one prompt to continue the same pass.
An idle worker receives no prompt.
The resume message builder retains the launcher prompt path for restored workers.
Every pane keeps its shell when its agent ends.

- `start_agent` gets the agent's arguments only, never the binary. Every herdr launcher builds from structured parts (`ResumeParts`, argument lists), never a shell string.
- One herdr session, `agents` (user, 2026-10-26). Every launcher opens a tab there with `start_server(shared_session())` and `open_tab`. Probes and live checks run as tabs there too, never in a server of their own. `_require_shared` refuses a server start under any other name, and `test_one_session.py` fails on code that starts one or tells the user to attach to one.
- Restored workspaces are cleared on every server spawn. Restart closes only workspaces that pass the untouched startup-shell test, never one it did not create.
- Kill nothing by pattern. Close one tab, stop a named herdr session other than the shared one, stop a systemd unit by its recorded name, or, on the Mac, stop only our own detached child. `stop_session` refuses the shared session, and no code stops an adopted or window-started server.
- Session identity comes from the listening socket's owner. Handoff survivors come from the server's unit cgroup, the shell pids it reports, or the pane scope of a matched process. Never use argv or an inherited `HERDR_SESSION` for server identity, and never match pane ids across environments.
- A non-empty `TMUX_PANE` means the process is never a herdr place.
- Native session ids select staff rows for launch and restore names.
  The Stop hook keeps member titles consistent with their rows.
  A unit name mismatch prints `/agents rename` and never changes staff.
- Staff restart uses `restart.start_row` and the shared pane launch path.
  Producers use their current conversation and configured model settings.
  Workers use `worker.restore`.
- Callers refuse a server whose inherited environment holds a must-be-unset variable (`CLAUDE_CODE_OAUTH_TOKEN`, the named-account variables for a legacy home, or `PreparedAccount.remove`).
- Every pane carries `HERDR_AGENT_PANE=1`. The shell's `claude` alias depends on it.
- Restart reads `attached_clients` before opening any window, and records a failed window in `restart_issues` without undoing the launch.
- Mac servers start only from the desktop session. Text quoting the refusals uses `MAC_DESKTOP_REQUIRED` and `MAC_DESKTOP_TERMINAL_REQUIRED` exactly. In herdr and shutdown code, `launchctl managername` is the only launchctl call.
- `start_detached` callers pass `DiscardOutput | AppendLog` and `InheritEnvironment | ExplicitEnvironment`, never raw `subprocess` arguments. Readiness lines are requested by `DETACHED_STARTUP_SIGNAL`, never by a platform check.
- Legacy tmux hosts and leftovers never become herdr destinations, are never archived, and always print a manual command.
- Edits to `/etc/nixos` (shell functions, earlyoom, packages) are outside this repo. Name them for the user.
- Python: basedpyright reports 0 errors and 0 warnings, with no file-level ignores, a `TypedDict` for every herdr JSON schema, and no `Any`.
- Tests that fake Linux process state use `linux_process_fixture`. Darwin tests pin `herdr._PLATFORM`. Temp roots are `.resolve()`d. Fakes of herdr JSON come from captured herdr output.

## Calibration and gotchas

**Numbers.**

- herdr rejects an agent-start timeout at or below 3000 ms (`_AGENT_START_TIMEOUT_MINIMUM_MS`).
- A new pane's zsh takes about 2.3 s to start (direnv, `gpg-connect-agent` in the foreground). `agent start` before then fails `agent_pane_busy`, which is retried every 0.1 s, on that error only.
- Each try's CLI subprocess gets 10 s of grace past its timeout, so the shared deadline can overrun by up to 10 s. `wait_until` has the same 10 s grace.
- `run_program` defaults to a 10 s timeout. Darwin `ps` reads use 5 s.
- Launch timeouts are 90 s (`90_000` ms) in `add_showrunner`, `herdr_sessions` and restart. `add_unit --timeout` defaults to 90 s.
- Darwin server readiness waits `min(10 s, timeout)`. `restore_server`'s Mac window waits `min(15 s, timeout)`. Conductor and fixer readiness is 15 s. `list --on` is 15 s.
- Agent exit detection polls the CLI every 0.05 s and asks herdr at most every 0.5 s.
- Session names are at most 64 bytes. Agent names are at most 32 characters.
- `add_showrunner` waits 180 s and polls every 2 s. Promotion waits 60 s for `/exit`. Mac restart windows are spaced 1.2 s apart.
- `fake_herdr.py` startup can pass 100 ms under load, so tests that bound a herdr call need at least 10 000 ms. Prove that launches overlap with a barrier, never a wall-clock bound.

**herdr behavior.**

- herdr 0.9.5 on natedev carries a local input patch (`herdr-mouse.patch`, overlay in `/etc/nixos`, commit 7755470). Over ssh a mouse wheel report can stop right after `ESC [`. herdr's timeout then sent Alt+[, and the rest (`<65;80;73M`) reached the pane as typed text. The patch sets `csi_introducer_recently_flushed` when a timeout flushes `ESC [`, and the next input drops a complete SGR mouse tail or holds a partial one, as herdr already did after a lone ESC. The test is `sgr_wheel_report_split_at_any_byte_after_timeout_never_reaches_the_pane`. The input parser runs in the `herdr session attach` client, so a new attach after the rebuild has the fix and no server restart is needed. It does not reach a program run in a plain ssh shell outside herdr.
- herdr's downloaded Claude detection rules read a permission dialog as idle when the screen above it holds a stray `─` rule and a `❯` line. The idle rule (`live_prompt_box`, priority 950) takes the text between the last two rules as the input box, and while a dialog is open the last rule is the dialog's top border. Idle then outranks the dialog's blocked rule (`bash_permission_prompt`, 850). Seen 2026-10-21: cargo-liner-style-types sat at a Bash permission dialog from 10:54 to 16:21 AST and herdr showed it idle. `scripts/herdr/detection_rules.py` builds a local override, `~/.config/herdr/agent-detection/claude.toml`, from the downloaded rules (`~/.local/state/herdr/agent-detection/remote/claude.toml`) with two edits. `live_prompt_box` matches only a box that opens with its `❯` line, and a new rule, `live_permission_prompt` (blocked, 980, region `after_last_horizontal_rule`), reads a dialog at the bottom of the screen. The version gets an added `.1` segment (herdr takes only dotted numeric versions). herdr uses an override in place of the downloaded rules as a whole, so each `herdr-upkeep` pass (`scripts/maintenance/herdr_upkeep.py`, scheduled and bounded at runtime by `config/herdr_upkeep.toml`) calls `detection_rules.refresh(running: set[str], reload: Callable[[str], bool]) -> bool` with the running herdr sessions and `herdr.reload_agent_rules`. An upstream rules update gets the edits again within one pass. If upstream changes either edited rule, refresh keeps the old override and writes `kept the old override` on stderr. With no downloaded rules file, refresh does nothing. `herdr agent explain <pane>` (or `--file <screen> --agent claude`) names the rule that set a pane's state and the rules file in use. The upstream issue is a draft, `docs/herdr-issue-draft.md`; nothing is filed until the user approves it.
  - **The reload receipt.** A reload resets herdr's per-pane detection: a pane marked `done` (finished, not yet seen) reads `idle` after it. So refresh reloads a session only when that session has not loaded the current override. Beside the override it keeps a receipt, `claude.loaded`. Line 1 is the SHA-256 of the built override, and each next line names a session that reloaded that version.
  - When the override file and the receipt hash both match the build, refresh reloads only the running sessions that are not in the receipt. When no session is missing, refresh changes nothing.
  - When the build differs from the override file, refresh writes the file and reloads every running session. A missing receipt, or a receipt with an old hash, also reloads every running session, also when the override file already matches.
  - Before refresh replaces the override or starts a reload, it deletes the old receipt. It writes the override and the new receipt each to a staged file and renames that file into place. A stop or an exception before the receipt rename makes the next refresh reload every running session.
  - Each reload is `herdr.reload_agent_rules(name: str) -> bool`, which runs `herdr --session <name> server reload-agent-manifests`. It returns True after a reload. On a `HerdrFailure` it writes `herdr <name>: detection rules not reloaded: <error>` to stderr and returns False. A failed reload stays out of the receipt, so the next pass tries that session again. Upkeep finishes and the next scheduled refresh can retry.
  - Refresh returns True only when it writes the override file. The upkeep job does not use the result.
  - The `reload` callback must return True only for a confirmed reload. A callback that catches the error and returns True puts a failed session in the receipt, and that session never tries again.
  - herdr reads only its own rule files in `~/.config/herdr/agent-detection/`, so the receipt there does not change detection.
- `herdr agent start --kind <kind>` adds the binary itself. An `agent_arguments` starting with it would reach Claude as a stray first prompt.
- herdr refuses multi-line start arguments, which is why the prompt is sent separately.
- A failed `herdr server stop` does not mean the session still runs. Read `live_sessions()`.
- An attach-started server's argv says `default`, so only the socket names its session. `HERDR_SESSION` is inherited and only selects a bare client's session.
- systemd adds `.service` to a bare unit name, so the suffix is explicit, or `a` and `a.service` would share a unit.
- herdr's `launch_pending` can stay true after the agent exits. A name-based `/proc` watch reports a false exit on a healthy start, because the Nix wrapper execs and forks. Follow the pid and its start ticks instead.
- herdr reopens `session.json` panes when a server starts. A window still attached through `server stop` gets a new empty shell workspace in front, so anything restored afterwards sits behind it until focused. The attach client makes its `~` workspace after the server answers, so `_clear_restored_workspaces` never sees it, which is why `close_startup_workspaces` exists.
- herdr 0.9.0 and 0.9.5 nest the scroll offset under `scroll`, and 0.9.5 omits `scroll` when unset. Fakes in the flat form passed while the real run failed.
- Linux discovery skips a process whose cwd no longer resolves, so callers that hold a registry pid place it with `herdr_place_of`.
- natedev's `claude` alias adds `--remote-control` and `--settings`. herdr types a bare `claude` at the pane prompt, so without the `HERDR_AGENT_PANE` guard every session would get those flags twice.
- Account moves use `stop_resume.resume` and run the account exec command through `Herdr.run_in_pane`.
  The exec path prepares the target environment and removes conflicting account variables.
  Pane variables alone do not replace account admission.
  The [shutdown design](shutdown-and-restart.md) owns this shared move path.
- A detached run is not a unit's second pane. The supervisor of a worker build that `detached_runs.py supervise` runs stays in its pane's scope (in the herdr service's cgroup when pane scopes are not available), so herdr places it in the session; the command itself runs in a `builds.slice` scope. It inherits the unit's marks and a pane id that can be out of date. `unit_lookup` keeps only the places that can hold the unit's Claude when one does. Without that rule, a unit with a detached run reads as marked twice, and `roster.py` exits 1.
- `herdr_place_of` reads every process for each call, so 40 calls took 10.7 s. `herdr_places()` places every pid in one read. `roster.py` uses it and takes about 1 s in total.

**Mac.**

- `launchctl submit` and launchd plists ran nothing on this Mac when tried for herdr: no pid, no output, exit status 0, even for `/bin/sh`. `scripts/message/notifier.sh` still uses `launchctl submit` for Mac run-only jobs.
- A herdr server started from an ssh login cannot read the login keychain, so Claude in it comes back signed out. A natedev-driven restart of Mac sessions over ssh hits the same trap, which is why the window bootstraps the server. A locked or signed-out Mac makes Ghostty start with no child, so the window never starts the server.
- Mac start readiness is "the socket listener is our own child", not "a socket exists". Otherwise a start counts a concurrent launch's server as its own `ServerCreated`.
- macOS `ps -o command=` prints arguments unquoted, so `/Applications/Nix Apps/Ghostty.app/...` splits at its space, and `comm=` is the executable. `(codex)` means macOS cannot read the arguments: liveness is unknown, never absent.
- macOS `ps -p <gone pid>` exits 1 with no output.
- `/tmp` is `/private/tmp`, so compare resolved paths.
- Patching `sys.platform` after import does not change `herdr._PLATFORM`.
- `test_support.py` sets the import path before importing `herdr`. Importing first breaks other suites.
- `merge_gate.py` refuses on the Mac because `/dev/shm` is absent.
  Run Mac suites from the clone root with `<clone>/scripts/lib/py -m unittest discover -s scripts/<suite> -p 'test_*.py'`.
  Set no `PYTHONPATH`.
- `list --on` runs the other machine's installed `~/.claude` code, so both machines need the promoted code before a remote list shows a change.

**Trust.**

- A survey of the account files found that worktrees are trusted through their main checkout's key, never their own or toplevel key.
- An absent key alone does not show that a folder launched without a prompt. That needs runtime evidence such as a Claude project directory.

## Ruled out

- The earlier worker trial used headless Codex threads. Current workers use staff membership and interactive panes.
- Shell waits for pane workers. Launch-line Codex notify and Claude Stop hooks now report turn results.
- Installing herdr's Claude integration. It writes `~/.claude` hooks and settings that every session loads.
- Ancestor folder keys as trust, and a trusted default start folder. Nothing records which folder the user last worked in, and a session opened somewhere unasked is worse than one prompt.
- A separate Codex trust check during crash restore. Restore resumes the saved conversation in its recorded folder.
- Probing pane process info to detect shell readiness, because herdr sends no field that tells them apart. Killing an in-flight `agent start` at the deadline, because it leaves a half-started agent.
- Copying the caller's environment into a pane. Only named marks and account variables pass.
- A store-path fallback in `herdr_binary`, because a garbage-collected path fails late.
- Matching pane ids from environments, because sessions sharing one scope can reuse ids.
- Opening a window on every attached herdr restart, because the existing client already shows the session.
- A herdr form of unit adoption, because marks are set only at launch.
- launchd plist bootstrap and `launchctl submit` on the Mac, because launchd never starts herdr there.
- An ownership token for a window-started server. Such a server is never stopped instead.
- herdr's `machine add` registry, which changes the remote server, and any two-way session sync.
- Clearing a legacy leftover by its cause. Every leftover holds until the user runs the printed cleanup.

## Why

- **Shared session and named tabs.** One attach shows each production workspace and the configured producer workspace.
  `attach <tab>` and `attach-remote` focus the named tab first.
  A tab uses its session's name.
  A process stop retains its tab and shell.
- **Native pane workers report through hooks.** Assignment returns after submission. The hook completes the pass and resumes the director with one message.
- **Launches write trust.** The shared writer grants the exact repository key before Claude starts. A failed write prints one anomaly and prevents the start.
- **Unique tab names.** A tab name is a session name, so two sessions must never share one. A taken `--name` refuses, and a generated name moves to the next suffix. Every launcher adopts the one shared server, so only the tab name must be unique.
- **Socket ownership as identity.** Argv and `HERDR_SESSION` both mislead (attach-started servers, inherited variables). The process holding the listening socket cannot.
- **Resume from our own ids.** Shutdown records and pane launch entries already hold session ids, accounts and cwds. herdr's Claude integration would add hooks to every session to learn the same thing.
- **Server environment check.** Panes inherit the server's environment. An adopted server started from a shell with another account's variables would launch Claude on the wrong account.
- **No client window from Linux launchers.** Launchers start only the server, as a systemd user unit, and print the attach command. Restart opens a window only for a session that had one and has no client now.
- **The Mac starts servers from the desktop.** The keychain is only readable in the Aqua session. Outside it, the Ghostty window starts the server, or the restart refuses with an instruction rather than bringing Claude back signed out.
- Shutdown restart reads staff row IDs. It does not read old host records.
