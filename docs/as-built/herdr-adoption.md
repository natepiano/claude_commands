# herdr adoption

## What it is

Every showrunner, unit director and standalone Claude session runs as a tab of one herdr session, `agents` (`shared_session()`), on natedev and on the Mac: one workspace per production, one tab per Claude session, and an `ad-hoc` workspace for standalone sessions. No launcher starts a herdr session of its own (user, 2026-10-26), and `start_server` and `restore_server` refuse any other name. The user opens `agents` with `herdr session attach agents` (`hsa agents`), or a tab on the other machine with `herdr_sessions.py attach-remote <host> <tab>`. Codex workers stay headless as `codex app-server` threads. Crash recovery reopens an interactive Codex conversation as a tab of `agents` too. tmux no longer hosts anything. Its only remaining traces are old shutdown records and snapshots, which parse into manual-restart states. Everything here sits in `scripts/herdr/`, with callers in `scripts/production/`, `scripts/message/roster.py` and `scripts/shutdown/`. The trial evidence behind the design is in [herdr-trial.md](herdr-trial.md).

## How it works

### Key files

| File | Role |
| --- | --- |
| `scripts/herdr/herdr.py` | The library: `Herdr` client, session names, server start and stop per platform, panes and agents, process placement, attached clients, server environment, Claude trust. |
| `scripts/herdr/herdr_sessions.py` | CLI: `list`, `attach`, `focus`, `start`, `resume`, `list --on`, `attach-remote`. |
| `scripts/herdr/test_one_session.py` | The check that fails when code outside `herdr.py` starts a herdr session or names one other than `agents`. |
| `scripts/herdr/trust_survey.py` | Read-only survey of trust keys in every account file. |
| `scripts/herdr/fake_herdr.py` | Test double for the herdr CLI. |
| `scripts/production/unit_lookup.py` | Units found by marks on herdr panes. |
| `scripts/production/session_pane.py` | Read, type, send keys and locate a unit or session pane. |
| `scripts/production/add_unit.py` | Starts and restores unit directors in herdr. |
| `scripts/production/add_showrunner.py`, `promote_showrunner.py` | Start a lower showrunner in herdr; turn a unit into one. |
| `scripts/production/fake_unit_host.py` | Production fixtures over the fake herdr CLI and a fake `/proc`. |
| `scripts/message/roster.py` | Joins marked units and interactive Codex panes into the roster. |
| `scripts/shutdown/session_types.py` | Host wire types, including the legacy tmux forms. |
| `scripts/shutdown/inventory.py`, `stop.py`, `settle.py`, `restart.py` | Host detection, unit session stop, screen reads, herdr restarts. |
| `scripts/lib/detached.py` | Detached starts on the Mac (herdr server, shutdown conductor, Remote Control fixer). |

Outside this repo, `/etc/nixos/modules/common/shell.nix` defines `herdr-claude` (runs `herdr_sessions.py start "$@"`) and the `hsa` alias. It also guards the `claude` alias with `HERDR_AGENT_PANE`. `/etc/nixos/modules/linux/memory.nix` puts `herdr` in earlyoom's `--avoid` list.

### The herdr library

**Binary and client.** `herdr_binary()` is `HERDR_BIN`, else `shutil.which("herdr")`, else `HerdrMissing`. `Herdr(session)` targets one session with `argv(*args) = [binary, "--session", session, *args]`. `run` returns stdout. `result` decodes the JSON `result` object. A non-zero exit raises `HerdrFailure(message, code)` carrying herdr's `error.code` and `error.message`, and a subprocess timeout (10 s default in `run_program`) becomes `HerdrFailure` too. Subclasses: `HerdrMissing`, `SessionTaken(name)`, `AgentBlocked` (herdr's `agent_blocked`, never retried), `AgentNotReady(screen)`, `AgentExited(session, pane)`, `MacDesktopUnavailable`, `MacDesktopTerminalRequired`. `Herdr` methods: `panes`, `lookup(label) -> PaneFound | NoPane | SeveralPanes`, `pane_id`, `pane_activity(pane) -> PaneAtShell | PaneRunning(commands)`, `open_tab`, `focus_workspace`, `close_startup_workspaces`, `start_agent`, `readiness`, `prompt`, `wait_until(pane, state, timeout_ms) -> ObservedAgentState | WaitTimedOut` (a `blocked` state raises `AgentBlocked`), `read(pane, lines, *, joined, ansi)` (`pane read --source recent|recent-unwrapped`), `send_keys`, `close_pane`, `agents`. Every herdr JSON schema has a `TypedDict`. Every timeout a caller passes is integer milliseconds. The library reads no account variables, so callers pass the pane environment.

**Session names.** A name matches `[A-Za-z0-9_.-]{1,64}` and is never `.` or `..`. `_validate_session_name` raises `ValueError` before any server starts or stops. `server_unit(name)` is `herdr-<name>.service`. `generated_session_name(text, suffix="")` maps other characters to `-` and fits `-<suffix>` inside 64 bytes. A session's socket is `$XDG_CONFIG_HOME/herdr/herdr.sock` for `default`, else `.../herdr/sessions/<name>/herdr.sock`. `live_sessions() -> list[SessionEntry]` returns only running sessions from `herdr session list --json`.

**The one session.** `shared_session()` is the name in `$XDG_CONFIG_HOME/herdr/shared-session`, else `SHARED_SESSION` (`agents`). It is never empty. `_require_shared(name)` raises `HerdrFailure("herdr session <n>: every Claude session runs as a tab in herdr session agents; …")` for any other name, and both server starts below call it first. `attach_hint(space, tab)` is the line launchers print: `herdr session attach agents, workspace <space>, tab <tab>`. `Herdr.focus_tab_of(pane)` shows that pane's workspace and tab in the attached clients.

**Server start.** A server is answering when `herdr --session <n> status server` exits 0 and prints `status: running`.

- `start_server(name, timeout_ms, *, session_kind=...) -> ServerCreated | ServerAdopted` adopts a server that already answers. Every launcher adopts: the shared session is one server for all launches.
- On Linux it submits `systemd-run --user --unit=herdr-<n>.service --collect -p CPUWeight=1000 -p MemoryLow=1G -p MemorySwapMax=0 --setenv=SHELL=<wrapper> --setenv=HERDR_PANE_SHELL=<shell> herdr --session <n> server` (`SYSTEMD_RUN`), polls every 0.1 s, and judges ownership by `systemctl --user is-active` (`SYSTEMCTL`). The shared session's boot unit, when a unit file defines it, starts with `systemctl --user start` instead, with no probe, no properties and no wrapper.
- Pane scopes (Linux, transient unit only). The unit holds only the server process. Each pane shell runs in its own scope under `app-agents.slice` (`PANE_SLICE`). Reason: 54 claude, 18 codex and cargo shared one cgroup with the server, and 80% of the server was in swap (typing lag, builds-unit). The properties give the server CPU first and keep its memory out of swap.
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
- `start_agent(name, pane, kind, agent_arguments, opening: OpeningPrompt | NoOpeningPrompt, timeout_ms, *, agent)` exists as a module function, which names the agent `agent`, and as a `Herdr` method. Its limit is `timeout_ms` times `load_factor()`, the one-minute load per CPU from 1 to 6 (2026-10-18: values-unit missed its 90 s limit at load 125). The limit covers the start and the opening prompt; `agent start --timeout` takes at most herdr's 300000. A `PromptNotSubmitted` that the launcher never typed has `never_typed` set, and add_unit then closes that new session (`close_untyped_session`): its Units row and worktree stay, and the same command starts it again. It runs `agent start <agent name> --kind <kind> --pane <pane> --timeout <ms> -- <agent_arguments>`. herdr adds the binary itself, so an `agent_arguments` whose first element equals `kind` raises `ValueError` before any herdr call. Agent names are lowercased, other characters become `-`, `a-` is added in front when the name does not start with a letter, and the result is cut to 32 characters.
- `agent_pane_busy` is retried every 0.1 s against one shared deadline. A retry never passes a per-try timeout at or below `_AGENT_START_TIMEOUT_MINIMUM_MS` (3000).
- While the CLI waits, the library finds the pane shell (`pane process-info`) and walks its process tree on Linux (per-thread `children`). It follows the agent by pid plus start ticks across exec and reparenting. An agent that was seen and is gone, or herdr's `agent_start_failed` with `process exited`, raises `AgentExited`. When `/proc` cannot be read it falls back to herdr observations at most twice a second.
- `agent_not_ready` reads the visible screen and raises `AgentNotReady(screen)`. No key is sent.
- After a ready start, `OpeningPrompt(text)` is sent once with `agent prompt`. `NoOpeningPrompt` sends nothing.

**Placement.** `HerdrPlace(session, pane)` is where a process lives. `NotInHerdr` means the process has no place.

- Linux: `_live_server_processes` reads listening sockets (`00010000`) at herdr socket paths in `/proc/net/unix` (`HERDR_PROC`). It maps each one to its single holder through `/proc/<pid>/fd`. The socket names the session, never the argv. `_process_sessions` then gives each server's descendants that session. It also catches handoff survivors, which a handoff does not reparent to the new server:
  - members of the server's own `herdr-<n>.service` cgroup;
  - in a shared scope, the pane shell pids the server reports (`pane list` plus `pane process-info`) with their descendants;
  - processes in a pane scope under `app-agents.slice`. A pane scope belongs to the session of a matched process in it, as the unit cgroup did before pane scopes. Only when a pane scope has no matched process (after a handoff) do the unit servers also report their pane shell pids. Without a handoff the parent walk explains each scope, and herdr is not asked. A scope with two sessions (a server started inside a pane) gives its other processes neither.
- `herdr_place_of(pid) -> HerdrPlace | NotInHerdr` reads that pid's environment for `HERDR_PANE_ID` and needs no cwd. `herdr_processes() -> list[HerdrProcess(pid, place, marks, command, working_directory)]` lists this user's pane processes. `marks` holds only `SHOWRUNNER_UNIT` and `SHOWRUNNER_UNIT_ID`. A Linux process whose cwd no longer resolves, or that vanishes mid-read, is skipped.
- A process with a non-empty `TMUX_PANE` (a tmux the user runs by hand) is never a herdr place, even when it inherited herdr variables. An empty `TMUX_PANE` excludes nothing.
- Darwin: processes come from `ps -U <uid>` (`HERDR_PS`), and each row's uid is checked again. Arguments come from a separate `command=` column split after the `comm=` executable; unreadable arguments are `UnreadableProcessArguments`. Listeners come from `lsof -n -P -a -U -u <uid> -F pfn` (`HERDR_LSOF`). `darwin_process_environment(pid)` reads `ps -o command=`, then `ps eww`, then the command again, and parses only the text after a stable argv prefix. cwd comes from `lsof -d cwd`; an unreadable one is `UnreadableCwd` and keeps placement and marks. A failed `ps` with empty output whose pid `os.kill(pid, 0)` confirms gone raises the private `_DarwinProcessGone`, and that process is skipped. `darwin_process_identity(pid)` (`lstart`, `tty`) and `darwin_open_files(pid)` serve the shutdown snapshot.
- `_PLATFORM = sys.platform` is read once at import.

**Clients and server environment.** `attached_clients(name) -> ClientsAttached(count) | NoClientAttached | ClientsUnreadable(detail)` counts this user's `herdr session attach <name>` clients and bare `herdr` TUI clients for that session. The session comes from argv `--session` first, then the client's `HERDR_SESSION`, else `default`. A client that exits mid-read counts as gone. `server_environment(name)` returns the environment of the single process holding the session's listening socket. Callers refuse when it holds a variable that must be unset, since panes inherit the server's environment.

**Shared session layout (user, 2026-10-18).** When `herdr.shared_session()` names a session, every Claude launcher opens a tab in it, never a session of its own. `Herdr.open_tab(space, title, cwd, environment)` finds or creates the workspace `space` and gives the tab and its pane the name `title`. A production's workspace is `showrunners.herdr_space(slug, doc)`: the doc's `- **Showrunner session:**` line, else the slug, so it reads `natedev`, `hana` or `cargo-liner`. The name never depends on whether the showrunner runs, so a unit restarted first finds the same workspace. A session outside any production goes to `ad-hoc`. A tab's title is the Claude session's current name (user, 2026-10-18): a launcher titles the tab with the name it gives `-n`, and a rename later reaches the tab through the Stop hook `scripts/hooks/herdr_tab_title.py`. At the end of each turn in a pane of the shared session, that hook reads the session's record and calls `Herdr.retitle(pane, name)`. `retitle` reads the pane and its tab. When either name differs, it renames the pane and the tab, and runs `order_tabs` again after a tab rename. A fault never stops the turn. The pane has the same name as its tab, because herdr's Go to list shows pane names. No script finds a session by its pane name. The herdr agent name stays the full session name: `start_agent` takes it as `agent`. A stop closes a unit's tab through the pane that carries its marks (`unit_lookup.marked_unit`), never by a title. A title is short and can repeat across workspaces (`review`).

**Tab order (user, 2026-10-18; restart keeps it, user 2026-10-21).** A restart puts every session back in its old workspace and tab, in the old order. `layout_path(session)` (`$XDG_STATE_HOME/herdr/layout-<session>.json`, else `~/.local/state/herdr/`) holds the saved `SavedLayout{at, workspaces: [SavedSpace{name, seen, tabs: [SavedName{name, seen}]}]}`. `Herdr.record_layout()` lays the live layout over it with `merged_layout`: live order wins, and an entry no longer live stays after the saved entry before it (`merged_order`) until it is unseen for `LAYOUT_KEEP_SECONDS` (14 days). Placeholder tabs and workspaces with no other tab are never saved, so an empty server saves nothing. `open_tab` saves after each tab it adds, `retitle` after each rename, and the snapshot pass through `save_shared_layout(running)`, which also catches a tab renamed in herdr itself. A failed save is a stderr line (`save_layout`); the tab stays open. `_LayoutLock` (`flock` on the layout file's `.lock`) lets one launcher at a time change the shared session's workspaces and order. herdr 0.9.5 cannot move a workspace, so creation order is the only workspace order. When `open_tab` needs a workspace on a server that holds none of the saved ones (a fresh server after a reboot), `_place_saved_workspaces` first creates every workspace seen within `LAYOUT_RESTORE_SECONDS` (one day) of the last save, in saved order, each with one tab and pane titled `restoring` (`LAYOUT_PLACEHOLDER`) in the home folder. The day covers a restart cut short by a second reboot. The first real tab in a workspace closes its placeholder. A workspace whose sessions never come back keeps its `restoring` tab until the user closes it: restart runs one account per pass, so no pass can tell that another account will not fill it. `Herdr.order_tabs(workspace, saved)` uses `tab_order(space, titles, saved_tabs, unit_director_titles)`. Saved tabs keep their saved order. Other tabs use this rule: the showrunner's tab comes first, then unit directors A-Z, then all other tabs A-Z (user, 2026-10-28). The showrunner's tab has the workspace's name. The first tab stays first, also when its title differs (`fps` shows `performance`). `unit_director_panes(session, processes)` finds panes whose processes carry both non-empty unit marks. `has_unit_marks` checks the marks for this function, `unit_roles` and `open_tab`. `order_tabs` reads all panes in each tab. A tab is a unit-director tab when any of its panes has both unit marks. `open_tab` adds the new title to `unit_director_titles` when its environment has both marks. This puts a unit tab in its place before its process starts. `herdr_sessions.py sort-tabs` calls `Herdr.sort_tabs()`. The method holds `_LayoutLock`, ignores saved tab order, applies the rule to each workspace, then saves the new order. A workspace with a tab that has several panes keeps its order. The method returns its label in `WorkspacesKeptForSplitTabs.labels`. The command prints one line with each returned label. herdr 0.9.5 has no command to move a tab, but `pane move --new-tab` puts the pane in a new tab at the end. So `order_tabs` moves each tab from the first one out of place, in order, and gives each new tab its old title. The pane keeps its id and its process. Focus goes back to the workspace and tab that had it. `order_tabs` keeps the order when a tab that must move has several panes. A tab with several panes before the first one out of place does not stop the sort. The method returns each moved pane's new id by its old one.

**Workspaces and focus.** `Herdr.focus_workspace(id)` runs `workspace focus`. `Herdr.close_startup_workspaces(restored_workspace)` closes every other workspace that is still herdr's untouched startup shell, and lists workspaces and panes again before each close. Untouched means a workspace labelled `~` with one tab, one pane and `agent_status` `unknown`, whose pane has no agent, `agent_status` `unknown`, cwd and foreground cwd equal to home, `revision` 0, and `scroll.max_offset_from_bottom` 0. `_StartupShellEvidence` is `UNTOUCHED` or `PRESERVE`. Missing or mistyped evidence keeps the workspace.

### Session commands

`scripts/herdr/herdr_sessions.py`:

- `list` prints `<tab>\t<role>\t<claude name>` per pane of the shared session (nothing when it is not running). Role is `unit <slug>/<unit>` from pane marks, `showrunner` when the Claude name is a registered showrunner session, else `standalone`. The Claude name is the newest live session record that `herdr_place_of` places in that pane. Exit 0 done, 1 not found, 2 usage, 3 herdr unreadable.
- `sort-tabs` sorts every workspace by showrunner first, then unit directors A-Z, then other tabs A-Z. It ignores saved tab order and saves the result. It keeps focus. A workspace with a tab that has several panes keeps its order; the command prints its name.
- `attach <tab>` focuses that tab (`focus_tab_of`) and execs `herdr session attach agents`. `focus <tab>` only focuses it and prints the shared session's name. A tab that is not open exits 1 and lists the open ones.
- `list --on <host>` runs `ssh <host> ~/.claude/scripts/lib/py ~/.claude/scripts/herdr/herdr_sessions.py list` within `REMOTE_LIST_TIMEOUT` (15 s). A timeout, `OSError` or non-zero ssh exit prints one stderr line naming the host and returns 1. `attach-remote <host> <tab>` runs `focus <tab>` on the host over ssh, then execs `herdr --remote <host> --session <the name it printed>`. `RemoteHost(target)` refuses an empty, `-`-leading or whitespace-bearing target with `ValueError`, exit 2. Nothing is saved or synced between machines.
- `start [--name N] [--cwd D] [-- <claude args>]` and `resume <name> --session-id ID --cwd D [--config-dir D] [--restore] [--prompt-file P] [--attach] [-- <claude args>]` run these steps:
  1. Refuse outside the Mac desktop.
  2. Resolve the folder and validate the name.
  3. Pick the account with `accounts.account_for_unscoped_launch`. A `LegacyHomeLaunch` uses `~/.claude.json` and unsets the named-account variables. Otherwise the pane gets `named_account_environment(dir)`.
  4. Check trust.
  5. Take `launch_permission.launch_barrier()` and `launch_permission(...)` with `NewWorkLaunchPurpose`, or `RecordedSessionRestoreLaunchPurpose(session_id)` under `--restore`.
  6. Run `start_server(agents, 90_000)`, and refuse when `server_environment` holds `CLAUDE_CODE_OAUTH_TOKEN` (or the named-account variables for a legacy home), since a new tab inherits it.
  7. Open a tab in the `ad-hoc` workspace (`open_tab`). A `--name` that an open tab already has refuses. Without `--name`, the name is `generated_session_name(<folder>, "1")`, and the suffix rises past each name an open tab has.
  8. Run `start_agent` with `[--resume ID] -n <name>` and an `OpeningPrompt` from `--prompt-file`, else `NoOpeningPrompt`.
  9. `start` and `--attach` then exec the attach.

  Failures print one line and exit 1. Ctrl-C prints `herdr_sessions: stopped` and exits 130.

### Claude trust before every launch

A Claude session started in a folder the account has not trusted stops at the trust prompt. A launcher that sends it a prompt then waits on a session that never answers. Every launcher checks trust first and refuses with the one command the user runs, before it writes or launches anything.

- `herdr.claude_trust(folder, claude_json) -> Trusted | Untrusted | UnreadableTrustFile` is the one check. A folder is trusted when `projects[k].hasTrustDialogAccepted is True` in the account's `.claude.json` for a key `k` in: the folder itself, its `git rev-parse --show-toplevel` (`repository_root`), and its main checkout (`repository_trust_key`, the parent of `--git-common-dir`). Both git helpers share `_repository_key`, which strips inherited `GIT_*` variables and runs git with `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`.
  - `Untrusted` carries `keys_tried`.
  - `UnreadableTrustFile` prints `cannot read <file> to check Claude trust (<ErrorClass>)`: the path and the error's class name, never the file's contents.
- `accounts.trust_command(folder, selected: ClaudeLaunchAccount) -> str` is the line the user runs, accepts the prompt in, then leaves with `/exit`: `cd <folder> && <account prefix> claude`. The prefix is `named_account_prefix`, the numbered `CLAUDE_CONFIG_DIR` plus the shared memory directory. A legacy home launch in `herdr_sessions.py` prints `cd <folder> && env -u CLAUDE_CONFIG_DIR -u CLAUDE_CODE_REMOTE_MEMORY_DIR -u CLAUDE_CODE_OAUTH_TOKEN claude` instead.
- Callers. Each refuses with `<unit | child slug | session>: Claude is not trusted in <folder> on <account label>. Run: <trust_command>`, or `<prefix>: <UnreadableTrustFile text>`:
  - `add_unit.preflight(request, selected)` exits 2 before any stub, table row or worktree is written. A worktree that does not exist yet is checked by its main checkout's key, and its trust command runs in the main checkout.
  - `add_showrunner.checked`, and through it `promote_showrunner.py`. `ADD_SHOWRUNNER_CLAUDE_JSON` sets the account file in tests.
  - `herdr_sessions.py start` and `resume`. An untrusted folder prints one paste line, the trust command `&&` the same invocation rebuilt by `_launch_rerun` (with `--cwd` resolved, claude arguments kept after `--`, and paths with spaces quoted), and exits 1. Nothing writes `hasTrustDialogAccepted`.
  - `restart._herdr_launch`, on the trust file in the launch environment's `CLAUDE_CONFIG_DIR`, before any server starts.
  - The nightly review launches through `add_unit.py --permanent` on the units account, so `preflight` covers it.
- Launchers use the configured group account. Each check reads `<CLAUDE_CONFIG_DIR>/.claude.json` in that numbered folder. `~/.claude.json` stays the seed and MCP baseline.
- `scripts/herdr/trust_survey.py` is a read-only survey. For the seed and each account file (`~/.claude.json` and every `~/.claude[0-9]/.claude.json`) it prints `<account> <key> <accepted> <kind>`. The kind is main checkout, worktree of `<main>`, plain folder, or ancestor of `<n>` repositories (nested repositories included). Then, for each `~/worktrees` folder, it prints whether its own, toplevel or main-checkout key is accepted.
- `pyrightconfig.json` gives `scripts/herdr` an environment with `.` and the message, production and lib paths. It puts `scripts/herdr` in the production, message, shutdown and nightly review environments.

### Pane access

`session_pane.py` reads and types through `HerdrPlace(session, pane)`. `read_place(place, lines=60, *, joined=False, ansi=False)` uses herdr's recent pane text. `type_place` sends an agent prompt, `keys_place` sends keys, and `where_place` prints `herdr:<session>:<pane>`. `record_place(record)` resolves the registry pid through `herdr_place_of`. `session_place(id)` first picks the newest live record for that id.

The CLI takes `read|type|keys|where` and either `unit <slug> <unit>` or `session <id>` (`read` also takes `--lines N --joined --ansi`). A trust, permission or question prompt refuses typing with exit 4 and `blocked at a prompt; nothing typed`. A missing target exits 1, usage 2 and host failure 3.

These all use this path: effort changes (`unit_effort.py`), promotion, relays and dailies in the showrunner commands, the stall watch (400 joined lines), unit status (`unit_status.sh`, 400 joined lines), shutdown screen checks (`settle._screen`, 60 lines), and the Remote Control fixer. `settle._screen` checks that the registry record at the unit's pid still has the session's `sessionId` before it resolves the pane; a mismatch is `ScreenUnreadable("session registry identity changed")`. The fixer (`remote_control.py`) resolves the newly registered pid's pane with `record_place`. It confirms process identity and idle state, then types `/remote-control` unless the pane already shows `/remote-control is active`.

### Unit lookup and marks

`unit_lookup.py` has one source, herdr. `Place = HerdrPlace`. `MarkedUnit(unit, claude, place)` is frozen, and its session name is `place.session`. `Claude = LiveClaude(name, session_id, socket, pid) | ClaudeNotRunning | ClaudeUnknown(reason)`.

- `_claudes(processes)` joins every live session record to a place by pid through `herdr_processes()`. A live pid missing from that list (a deleted cwd) falls back to `herdr_place_of(pid)`. It returns `RecordsRead | RecordsPartlyUnread(reason)`, and a partial read makes unplaced units `ClaudeUnknown`.
- `_herdr_marked` yields each running pane carrying both marks, once per place.
- `every_marked_unit() -> MarkedProductions(units, faults)` reads all units once and raises `OSError` when herdr cannot be read. Two sessions carrying one unit's mark is a fault, and that production is left out. `marked_units(slug)` raises the fault as `OSError`. Slugs resolve through `canonical_slug`.
- CLI: `list <slug>` prints `unit, herdr:<session>:<pane>, session, live|unknown|stopped, name, session id, socket`. `pane <slug> <unit>` prints `place_text(place)`. Exit 0, 1 no such unit, 2 usage, 3 cannot tell.

Marks are `SHOWRUNNER_UNIT` (production slug) and `SHOWRUNNER_UNIT_ID` (unit). They are set in the pane environment when a unit launches; adopting an existing conversation requires a safe stop and marked relaunch with `add_unit.py --attach`.

### Launchers

**Units.** `add_unit.launch_session(request, director, prompt, selected, *, target="", model="")` starts every new or restored unit as a tab of the shared session:

- Pane environment: both marks, `ENABLE_TOOL_SEARCH=true`, and the account variables. A resume uses `accounts.prepared_account(selected.label, resume=True)` variables minus its `remove` names, and refuses a `target` other than the selected group account. Other launches use `named_account_environment(selected.dir)`. `PROBE_ENV=1` is added only for a `--probe` restore. The caller's `CLAUDE_*` never reaches the pane.
- Claude arguments: `--model`, `--effort` when set, `--resume <id>` on a resume, `--remote-control <session name> -n <session name>`, then the profile's flags or `--settings '{"disableAgentView": true}'`.
- The agent name is `host_session_name(request)`: `generated_session_name` of `--host-session` on a restore when it leads with `<slug>-`, else `<slug>-<session name>` (a session name already leading with `<slug>-` is not prefixed twice), so each production's `review-unit` has its own name (`build-followups-review`, `hana-review`).
- It runs `start_server(agents, --timeout * 1000)` (default 90 s), `open_tab(agents, herdr_space(<slug>, <doc>), <session name>, session_cwd, env)`, and `start_agent(agents, pane, ..., agent=<agent name>, opening=OpeningPrompt(prompt))`. On `AgentExited` it closes that tab (`close_agent`).

`add_unit.main` does this:

- It picks the account with `accounts.account_for_group_launch("unit", ...)`, checks it with `accounts.check`, and takes the launch barrier and permission (`RecordedSessionRestoreLaunchPurpose` for a restore). Then it runs `preflight`.
- Every `--resume`, including restores, refuses in `preflight` before any stub, Units row, commit, worktree or tab is written when the shared session registry identifies a running session (naming its pid), or when registry or process identity is unreadable (naming the reason).
- A plain `--resume` of an existing Units row, active or retired, continues its transcript without requiring a Production header in the row’s own plan document, so a condensed as-built remains usable; new units and attaches still require the header, and a resume with a different plan still refuses.
- A plain `--resume` of a retired Units row may use `--cwd` outside its worktree: the raw Plan cell supplies the retired state, the row keeps its worktree and branch, and the launch creates neither and leaves the row for resume_unit to unmark; other cwd mismatches retain their refusal.
- On a resume of a marked unit, `ClaudeUnknown` refuses, and `ClaudeNotRunning` closes that place's tab with `close_agent` before the relaunch.
- A restore records its prompt as scheduled (`record_scheduled_restore_prompt`). The resumed model is the transcript's last real assistant model unless `--model` is given.
- A new unit waits until `read_place` shows `/remote-control is active`.
- It prints `<unit> started: <attach_hint>` or `<unit> restore launched: <attach_hint>`, the unit's tab in its production's workspace.
- Exit 1 on `AgentNotReady` (prints the screen), 2 on a refusal, 1 on other failures.

`--attach <id> --cwd <dir>` accepts a running session with `--plan`, `--standby` or `--permanent`. It checks the row, worktree, trust and source account before requesting `shutdown.py down <account> --here --only <id>`, follows the request within shutdown's pause limit, and requires both a stopped record and no surviving process. It then writes the unit, resumes the same conversation once with unit marks, finishes the shutdown record through `clear_running_on_target`, and proves one new registered pid. Unknown liveness or account, an incomplete stop, a surviving old process, or an ambiguous final process count refuses; an incomplete stop prints its cancel command. Plain `--resume` refuses a running session and directs the caller to `--attach`.

**Lower showrunners.** `add_showrunner.launch(child, model, effort, resume, cwd, selected)` uses no marks. The pane gets `ENABLE_TOOL_SEARCH=true` and the account variables. Arguments are `--model --effort [--resume] --remote-control <slug> -n <slug> --settings '{"disableAgentView": true}'`. It runs `start_server(agents, 90_000)`, opens a tab titled `<slug>` in `herdr_space(<slug>, <doc>)` and sends the prompt `/showrunner:produce <doc>[ resume]`. `checked` refuses when a tab of the shared session already has the slug's title (`herdr_tab_open`). Exit 3 (`SESSION_NOT_READY`) on `AgentNotReady`. It prints `started <slug>: <attach_hint>`, then waits up to `ADD_SHOWRUNNER_WAIT_S` (180 s, poll 2 s) for the showrunner to appear before attaching it to the caller.

**Promotion.** `promote_showrunner.stop` types `/exit` with `type_place`. It waits up to `PROMOTE_STOP_WAIT_S` (60 s) for the Claude pid to go, then closes its tab with `close_agent(place.session, place.pane)`. `add_showrunner.main --resume <id> --cwd <worktree> --config-dir <showrunners account dir>` then resumes the same conversation as the child's showrunner. A failed start prints the `add_unit.py --resume` line that brings the unit back.

### Roster

`roster.py` reads every production's marks once (`_every_marked_unit`). A director with a `LiveClaude` joins by session id. A director with no messaging socket joins when `herdr_place_of(record pid) == marked.place`. Worktree ownership is the fallback only when the marks cannot be read. `_herdr_codex_panes` runs one `herdr agent list` per live session and lists each interactive Codex pane as a freestanding row named `herdr <session>:<pane>`, with `CannotReceiveMessages("interactive Codex in a herdr pane")`. `HerdrMissing` lists nothing, and any other herdr failure is a problem line.

### Shutdown, restart and crash recovery

[shutdown-and-restart.md](shutdown-and-restart.md) covers records, `ResumeParts`, snapshots, holds and the restart pass. The herdr parts:

- **Hosts.** `inventory._host` checks `TMUX_PANE` first (non-empty means `UnknownHost`). A marked unit with a live Claude is `UnitHost{production, unit, doc, session_name, session_host: "herdr", plan}`. A placed pid is `HerdrHost{herdr_session, pane_id, presentation}`, where `presentation` is `"attached window"` when `attached_clients` finds one and `"detached"` otherwise (a record without it reads as detached). Everything else is a window or terminal host. The snapshot pass (`_snapshot_host`) fails when a `HERDR_PANE_ID` process cannot be placed or a session's clients are unreadable. On Darwin, inventory reads environments through `herdr.darwin_process_environment` and the snapshot reads no `/proc`.
- **Stop.** `stop._stop_unit_session` runs `herdr.stop_session(<session_name>)` after the Claude exits. It judges the session stopped only when `live_sessions()` no longer lists it. Otherwise the record keeps `unit session left running{host: "herdr", session_name, cause}`.
- **Restart destinations.** Every restart lands as a tab in the one shared session, `shared_session()` (`agents`); no restart opens a herdr session of its own. `_herdr_launch(parts: ResumeParts, destination: RecordedHerdrPane(name, pane_id) | SharedHerdrTab(name), session, *, dry_run, session_kind) -> HerdrLaunch`.
  - Units restart only through `add_unit.py ... --host-session <session_name>` (`_unit_argv`), never `_herdr_launch`.
  - A `HerdrHost` recorded in the shared session restarts through `RecordedHerdrPane`; one recorded in any other session restarts into `SharedHerdrTab` and leaves its recorded session (`_leave_recorded_session`). The leave runs after the launch. A leave that fails (`HerdrFailure`, `OSError` or `SubprocessError`) prints `<name> restart issue: herdr session <recorded> left open: <error>` and the launch stands; the old session stays for the user to close.
  - A legacy tmux host restarts into `SharedHerdrTab`, with no window step.
  - Ghostty and Zed hosts, and a Mac `terminal` host, restart into `SharedHerdrTab`. For a Ghostty or Mac `terminal` host, a live supervisor then runs `herdr session attach <shared>` in the same window; a Zed host, or one whose supervisor is gone, gets a new window unless a client is already attached.
- **`_herdr_launch` steps.**
  1. Build the environment: a `MoveResumeAccount` takes `prepared_account(target, resume=True)` and refuses `AccountRefused`; a retained account takes `named_account_environment(config_dir)`.
  2. Check trust (`_heal_folder_trust`, also before the dry-run preview). `herdr.heal_claude_trust(folder, target, sources, dry_run=)` returns `Trusted`, `TrustCopied(folder, account_file, source)`, `Untrusted` or `UnreadableTrustFile`. `accounts.trust_sources(target_dir, source_dir)` lists the session's own source account first, then every numbered account. A copy (`copy_claude_trust`, one key) prints `<name>: copied folder trust for <folder> from <label> to <target>`, and a dry run prints `would copy` and writes nothing. With no trusting account, `FolderNotTrusted` (an `OSError` carrying `command`) refuses the launch; the dry run prints the same `restart failed` line, and the record saves `trust_command` for the `shutdown.restart_untrusted` push.
  3. Call `restore_server(name, 90_000, session_kind=...)`.
  4. Refuse when `server_environment` holds `CLAUDE_CODE_OAUTH_TOKEN` or a `prepared.remove` name.
  5. Refuse a live agent in the recorded pane. A recorded pane left at its shell is closed; one running a command is left open, with a printed line, because the session resumes in a new tab.
  6. Record the scheduled prompt, open a tab in the session's workspace (`herdr_space`: its production's, else `ad-hoc`), and start Claude with `OpeningPrompt(parts.opening_prompt)`. `AgentExited` closes that tab (`close_agent`); the shared server keeps running.
  7. An adopted or window-started server gets its restored workspace focused. After a successful focus, a window-started server also gets `close_startup_workspaces`. A failed focus or close prints `<name> restart issue: ...` and the restore stands.
  8. `StartFreshConversation` prints `<name>: no conversation to resume; started a new one`.

  `SessionTaken` becomes `HerdrDestinationTaken`, and `MacDesktopUnavailable` passes through.
- **Windows after launch.** A `WindowServerCreated` restore needs nothing more. An `"attached window"` host gets a window only when `attached_clients` is `NoClientAttached`. Unreadable clients, or a `"detached"` host, print `herdr session attach <name>`. A failed window open appends to the record's `restart_issues` and leaves the agent running. Mac windows open `Ghostty.app` with `-e herdr session attach <name>`, spaced `WINDOW_DELAY_SECONDS` (1.2 s) apart.
- **Codex snapshots.** `_launch_codex_snapshot` uses the shared session, `restore_server(..., session_kind=...)`, a tab titled `<owner name>-codex` in the owner's workspace with `CODEX_HOME`, and `start_agent(kind="codex", ["resume", <id>?], NoOpeningPrompt())`. A failed start closes that tab. A legacy tmux owner restores the same way.
- **Detached starts on the Mac.** `detached.start_detached(argv, *, ready, within, output: DiscardOutput | AppendLog, environment: InheritEnvironment | ExplicitEnvironment) -> pid` starts the child in its own session with stdin closed. It returns only once `ready()` passes while the child lives, and leaves a thread to reap it. Failure raises `DetachedChildExited(pid)` or `DetachedStartFailure`, and a child that never became ready has its process group stopped (SIGTERM, then SIGKILL after 0.5 s). The conductor and the fixer start this way, with the caller's environment plus `DETACHED_STARTUP_SIGNAL=1` (`ExplicitEnvironment`). Each is ready when its log holds `shutdown conductor started` or `Remote Control fixer started`, within 15 s. They print that line only when `DETACHED_STARTUP_SIGNAL=1` is in their environment, and they pop it at once. Their start errors end `; log: <path>`, with logs under `shutdown_state_root()/logs`. Linux uses systemd.

### Legacy tmux records

Old records and snapshots still parse. Do not remove this parsing.

- `session_types._parse_host`:
  - A unit host whose `session_host` is absent or `"tmux"`, or whose kind is `legacy tmux unit`, becomes `LegacyTmuxUnitHost{kind: "legacy tmux unit", production, unit, doc, session_name}`. The old key `tmux_session` is read.
  - A `tmux` or `legacy tmux` host becomes `LegacyTmuxHost{kind: "legacy tmux", session_name}`.
  - Any `session_host` other than `tmux` or `herdr` raises `InvalidInventory`. `UnitHost.session_host` is `Literal["herdr"]`.
- `record.py`: the old left-running kinds parse as `LegacyTmuxSessionLeftRunning{kind, at, session_name, cause}`. `legacy_tmux_leftovers(record)` returns every one, whatever its cause.
- `restart.py`: tmux is gone, so a legacy host restarts as a tab in the shared session like a desktop host (see Restart destinations). Only a record with legacy tmux leftovers (`legacy_tmux_leftovers`) is `ManualRestartRequired(legacy_resume_command(record))`, which is `tmux kill-session -t =<name>` per leftover, `&&` the resume shell from `_resume_command`. Such a record is never archived (`clear_running_on_target` refuses first) and never retried as cleanup. `status` and restart show it as a manual restart. It counts in `MachineRestartResult.manual_sessions`, so restart reports `partial` and exits 1.
- Other `tmux` text that stays on purpose: `sessions.SessionRecord.tmux`, the `/exit` guard in `pre-tool-use-showrunner-move-guard.py`, the old log line form in `waiting.py`, and user quotes in commands and `herdr-trial.md`.

### Test doubles

- `fake_herdr.py`: `HERDR_BIN` points at it. It appends each argv to `FAKE_HERDR_LOG` as a JSON line. It answers from `FAKE_HERDR_RESPONSES`, keyed by the space-joined argv without the executable, else `"default"`. Fields: `stdout`, `code`, `stderr`, `write_files`, `require_paths` (a missing path exits 98) and `remove_paths`. A list replays in order and keeps the last entry. It refuses an `agent start` whose name fails `[a-z][a-z0-9_-]{0,31}` with `invalid_agent_name`. `herdr_listener(proc, pid, name, environment)` gives a fake server the session's listening socket in a fake `/proc/net/unix` plus an fd link.
- `fake_unit_host.py`: `environment(root)` sets `HERDR_BIN`, `HERDR_PROC`, `FAKE_HERDR_LOG` and `FAKE_HERDR_RESPONSES` for an empty host. `HostedUnit(session, pane, marks, screen)`. `write(env, units)` publishes a server process (pid `991000 + 2i`), its listener, a marked `zsh` pane shell, the session list, and pane reads (`recent` and `recent-unwrapped`; 3, 60 and 400 lines; with and without ANSI). `place_process(env, pid, pane)` puts a registry process under that pane's shell. `calls(env)` returns logged argv. `live_pid(test)` gives a real live pid for registry checks.
- Darwin cases fake `LAUNCHCTL`, `HERDR_PS`, `HERDR_LSOF`, `SHUTDOWN_OPEN` and `SHUTDOWN_SYSCTL`, and pin `herdr._PLATFORM`. Linux `/proc` cases use `scripts/shutdown/test_support.linux_process_fixture`, which pins `sys.platform` and `herdr._PLATFORM`.

Use these fixture rules on Linux and macOS:

- Resolve the temporary root in `test_add_unit.py` and `test_add_showrunner.py`. On macOS, `/tmp` points to `/private/tmp`. Trust keys and worktree paths must use the same resolved root as the code under test.
- Set `HERDR_PLATFORM=linux` in each Linux launcher fixture's environment. Do not set it for the whole run. Darwin tests must still select the Darwin path.
- Keep `HERDR_BIN=/nonexistent/herdr` in `PromptSubmissionTests`' cleared environment. Its fake pane calls do not need herdr on `PATH`.
- Build a herdr client only in `setUp`, never as a class attribute: the class body runs at import, before any patched environment. The run-wide `HERDR_BIN` hides such a lookup, so check a new fixture once with an empty `PATH` and no `HERDR_BIN`.

Run the nine suites on the Mac with a scratch home. Run the command from the root of a copy of the tree under test, not from the Mac's live `~/.claude`, which holds main. Remove the scratch home when the run ends:

```sh
(
  test_home=$(mktemp -d) || exit
  trap 'rm -rf "$test_home"' EXIT
  env -u CLAUDE_CONFIG_DIR -u CLAUDE_CODE_OAUTH_TOKEN -u CODEX_HOME \
    HOME="$test_home" TMPDIR=/private/tmp HERDR_BIN=/nonexistent/herdr \
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
    scripts.herdr.test_herdr scripts.herdr.test_herdr_sessions \
    scripts.shutdown.test_restart scripts.shutdown.test_account_move \
    scripts.shutdown.test_shutdown_cli scripts.shutdown.test_settle \
    scripts.production.test_add_unit scripts.production.test_add_showrunner \
    scripts.notify.test_push_texts
)
```

## Invariants

- Tests never start real herdr, Claude or Codex. They use `fake_herdr.py`, `fake_unit_host.py`, a fake `/proc` and fake session records, in temporary folders.
- Nothing answers a trust, permission or question prompt. Launchers refuse an untrusted folder before any write or launch, and the refusal names the exact command that fixes it. `agent_not_ready` reports the screen and sends no key. New launchers call `claude_trust` and `accounts.trust_command`, not a check of their own.
- Trust counts only on an exact key: the folder, its toplevel, or its main checkout. An accepted ancestor folder never counts.
- The trust survey prints only the key, accepted and kind fields of `.claude.json`, never any other field.
- One prompt per launch, never sent twice. `agent start` carries no prompt; the opening prompt goes through `agent prompt` once the agent is ready. Callers choose `OpeningPrompt` or `NoOpeningPrompt` explicitly.
- The prompt goes in only when the composer is empty. Every composer check reads `pane read --source visible --format ansi` and drops faint text (SGR 2, until 22 or 0; colour arguments of 38/48/58 are skipped), because a new session shows a faint hint such as `Try "fix lint errors"` in its empty composer. Read as plain text, that hint looked like input, and add_unit's efficiency launch waited out its limit twice (2026-10-19).
- `start_agent` gets the agent's arguments only, never the binary. Every herdr launcher builds from structured parts (`ResumeParts`, argument lists), never a shell string.
- One herdr session, `agents` (user, 2026-10-26). Every launcher opens a tab there with `start_server(shared_session())` and `open_tab`. Probes and live checks run as tabs there too, never in a server of their own. `_require_shared` refuses a server start under any other name, and `test_one_session.py` fails on code that starts one or tells the user to attach to one.
- Restored workspaces are cleared on every server spawn. Restart closes only workspaces that pass the untouched startup-shell test, never one it did not create.
- Kill nothing by pattern. Stop a named herdr session, a systemd unit by its recorded name, or, on the Mac, only our own detached child. `stop_created_server` never stops an adopted or window-started server.
- Session identity comes from the listening socket's owner. Handoff survivors come from the server's unit cgroup, the shell pids it reports, or the pane scope of a matched process. Never use argv or an inherited `HERDR_SESSION` for server identity, and never match pane ids across environments.
- A non-empty `TMUX_PANE` means the process is never a herdr place.
- Unit marks are set only at launch. There is no adoption of existing sessions.
- Every unit restarts through `add_unit.py --host-session <name>`, which is restart's only restore flag for units.
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
- herdr's downloaded Claude detection rules read a permission dialog as idle when the screen above it holds a stray `─` rule and a `❯` line. The idle rule (`live_prompt_box`, priority 950) takes the text between the last two rules as the input box, and while a dialog is open the last rule is the dialog's top border. Idle then outranks the dialog's blocked rule (`bash_permission_prompt`, 850). Seen 2026-10-21: cargo-liner-style-types sat at a Bash permission dialog from 10:54 to 16:21 AST and herdr showed it idle. `scripts/herdr/detection_rules.py` builds a local override, `~/.config/herdr/agent-detection/claude.toml`, from the downloaded rules (`~/.local/state/herdr/agent-detection/remote/claude.toml`) with two edits. `live_prompt_box` matches only a box that opens with its `❯` line, and a new rule, `live_permission_prompt` (blocked, 980, region `after_last_horizontal_rule`), reads a dialog at the bottom of the screen. The version gets an added `.1` segment (herdr takes only dotted numeric versions). herdr uses an override in place of the downloaded rules as a whole, so each snapshot pass rebuilds it (`detection_rules.refresh`). When the build differs from the file, the pass writes the file and runs `herdr server reload-agent-manifests` in each running session. An upstream rules update so gets the edits again within one pass. The reload resets herdr's per-pane detection, so a pane marked `done` (finished, not yet seen) reads `idle` after it; this happens only when the override changes. If upstream changes either edited rule, the pass keeps the old override and writes `kept the old override` on stderr. `herdr agent explain <pane>` (or `--file <screen> --agent claude`) names the rule that set a pane's state and the rules file in use. Nothing is reported upstream yet; that needs the user's approval.
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
- An account move cannot run `accounts.py exec` inside herdr, since it removes credentials and checks readiness, which pane variables cannot do. Moves pass `prepared_account(target, resume=True)` variables and removals instead.
- A detached run is not a unit's second pane. The supervisor of a worker build that `mesh_runs.py supervise` runs stays in its pane's scope (in the herdr service's cgroup when pane scopes are not available), so herdr places it in the session; the command itself runs in a `builds.slice` scope. It inherits the unit's marks and a pane id that can be out of date. `unit_lookup` keeps only the places that can hold the unit's Claude when one does. Before this, three tool-based-ui units read as marked twice and `roster.py` exited 1.
- `herdr_place_of` reads every process for each call. `herdr_places()` places every pid at once; `roster.py` took 10.7 s for 40 calls and now takes about 1 s in total.

**Mac.**

- `launchctl submit` and launchd plists ran nothing on this Mac when tried for herdr: no pid, no output, exit status 0, even for `/bin/sh`. `scripts/message/notifier.sh` still uses `launchctl submit` for Mac run-only jobs.
- A herdr server started from an ssh login cannot read the login keychain, so Claude in it comes back signed out. A natedev-driven restart of Mac sessions over ssh hits the same trap, which is why the window bootstraps the server. A locked or signed-out Mac makes Ghostty start with no child, so the window never starts the server.
- Mac start readiness is "the socket listener is our own child", not "a socket exists". Otherwise a start counts a concurrent launch's server as its own `ServerCreated`.
- macOS `ps -o command=` prints arguments unquoted, so `/Applications/Nix Apps/Ghostty.app/...` splits at its space, and `comm=` is the executable. `(codex)` means macOS cannot read the arguments: liveness is unknown, never absent.
- macOS `ps -p <gone pid>` exits 1 with no output.
- `/tmp` is `/private/tmp`, so compare resolved paths.
- Patching `sys.platform` after import does not change `herdr._PLATFORM`.
- `test_support.py` sets the import path before importing `herdr`. Importing first breaks other suites.
- `merge_gate.py` refuses on the Mac (no `/dev/shm`). Run Mac suites as `PYTHONPATH=<clone> ~/.claude/scripts/lib/py -m unittest discover -s scripts/<suite> -p 'test_*.py'`.
- `list --on` runs the other machine's installed `~/.claude` code, so both machines need the promoted code before a remote list shows a change.

**Trust.**

- A survey of the account files found that worktrees are trusted through their main checkout's key, never their own or toplevel key.
- An absent key alone does not show that a folder launched without a prompt. That needs runtime evidence such as a Claude project directory.

## Ruled out

- Interactive Codex panes for workers. They stalled at the trust screen, lost Work Orders and hid from the roster, while headless workers ask nothing and the roster already finds them.
- One shared herdr session with many panes. An attached window keeps its own view, so attach could not open a given session.
- Installing herdr's Claude integration. It writes `~/.claude` hooks and settings that every session loads.
- Ancestor folder keys as trust, and a trusted default start folder. Nothing records which folder the user last worked in, and a session opened somewhere unasked is worse than one prompt.
- A Codex trust check. No launcher starts new Codex work in herdr, and crash restore only resumes a Codex session in the folder it already ran in.
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

- **One herdr session per Claude session.** Attach should open exactly the session the user names, from any terminal, and stopping one session should touch nothing else. Session names are the launch names, so `list` and attach lines need no lookup table.
- **Codex workers stay headless.** App-server threads ask for no trust and already report through the worker ledger. Only Claude sessions, which the user watches, need a terminal.
- **Trust is checked, never answered.** Answering grants trust the user never gave. Sending a prompt into a trust screen waits forever. A refusal with one paste line costs the user one prompt, once per repository.
- **Exclusive starts.** Two launches that chose the same name must never share one server. A taken name refuses, or for generated names moves to the next suffix. Adoption is kept for restoring into a server a record names.
- **Socket ownership as identity.** Argv and `HERDR_SESSION` both mislead (attach-started servers, inherited variables). The process holding the listening socket cannot.
- **Resume from our own ids.** Shutdown records and recovery snapshots already hold session ids, accounts and cwds. herdr's Claude integration would add hooks to every session to learn the same thing.
- **Server environment check.** Panes inherit the server's environment. An adopted server started from a shell with another account's variables would launch Claude on the wrong account.
- **No client window from Linux launchers.** Launchers start only the server, as a systemd user unit, and print the attach command. Restart opens a window only for a session that had one and has no client now.
- **The Mac starts servers from the desktop.** The keychain is only readable in the Aqua session. Outside it, the Ghostty window starts the server, or the restart refuses with an instruction rather than bringing Claude back signed out.
- **Legacy records stay parseable.** A crash or an old shutdown record can still name a tmux host. Turning it into a manual command keeps the conversation recoverable without bringing tmux back as a host.
