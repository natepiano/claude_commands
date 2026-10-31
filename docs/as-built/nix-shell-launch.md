# Nix shell launch and recovery

A worktree can have `DIRENV_*` variables while its `.envrc` is still blocked.
That process never entered the nix shell: native links can fail with errors
such as `mold: cannot find gbm`. A running Claude process cannot replace its
environment. Launches now prove the shell first; existing broken sessions
restart through the supported shutdown path when idle.

## Launch helper

`scripts/lib/nix_env.py` uses only Python's standard library on Linux and macOS.
`ensure(directory: Path, base_env: Mapping[str, str] = os.environ)` returns one
of three immutable named tuples:

| Result | Meaning |
| --- | --- |
| `Loaded(env, envrc)` | The nearest `.envrc` loaded and the result has a nonempty `IN_NIX_SHELL`. |
| `NoEnvrc(env)` | No ancestor has `.envrc`; `env` is a copy of the caller's environment. |
| `Refused(reason)` | Loading, approval, decoding, or nix shell proof failed. |

It searches the start directory and then its parents. Resolved `.envrc` files
under `~/rust`, `~/worktrees`, and `~/.claude*` are approved with
`direnv allow <envrc>`. Linked worktrees outside those roots are also approved
when their `.git`, `commondir`, and `gitdir` back pointer prove registration by
a repository under a trusted root, using file reads alone. This covers
style-sync lint snapshots made with `git worktree add` under `/tmp/claude/session`;
clones and forged links still require explicit approval. Files elsewhere require existing explicit approval;
a blocked-file refusal names the `direnv allow <path>` command. Each direnv
command has a 600-second timeout. `direnv export json` runs in the requested
directory with the supplied base environment. Strings replace variables;
JSON null removes them. Failures retain direnv's last stderr line. The proof
failure is `nix_env: <dir>: .envrc loaded but no nix shell (IN_NIX_SHELL unset)`.
An empty successful export means no variables changed: the caller's environment
is retained and still must supply the `IN_NIX_SHELL` proof.

`nix_env.py ensure <directory>` prints the full environment as one JSON object
and exits 0, or prints one `nix_env: ...` stderr line and exits 1.
`nix_env.py exec <absolute program> [args...]` ensures its current directory and
uses `os.execvpe` with that environment. A relative program is a usage error
(exit 2), preventing a PATH wrapper from finding itself by a bare name.
For `Loaded`, exec sets `NIX_ENV_ENVRC` to the resolved `.envrc` path; for
`NoEnvrc`, it removes an inherited `NIX_ENV_ENVRC`. The `ensure` API and its
JSON output keep their existing environment-diff behavior; only `exec` adds
or clears this provenance marker.
Launchers resolve Claude with `command -v` or the existing absolute-binary
resolver before calling `exec`.

## Launch paths

| Path | Integration |
| --- | --- |
| `herdr/herdr.py` | `launch_agent` checks `ensure(cwd, base_env)` before `open_tab`. It returns `AgentLaunched(pane, outcome)` or a typed `Refused`; refusal creates no pane and starts no agent. |
| `production/add_unit.py` | New units, resumes, renames, transfers, watchdog recovery and account moves use `launch_agent`. |
| `production/add_showrunner.py` | Child showrunners use `launch_agent`. |
| `shutdown/restart.py` | Claude restarts and Codex snapshot restores prepare the saved cwd and account environment before `restore_server` can open a desktop window, then use `launch_prepared_agent`. A refusal leaves the snapshot available and opens no window or tab. |
| `shutdown/pane_restore.py` | Saved Claude panes use `launch_agent` before replacing their old tab. |
| `herdr/herdr_sessions.py` | Standalone starts and resumes use `launch_agent`. |
| `berth_fix/launch.sh` | Proves the worktree before opening a window; both window and current-terminal starts use `nix_env.py exec`. Worktree approval failure is fatal. |
| `agents/agent_bg.sh` | Proves the cwd while making worker settings, passes the loaded env through settings for the long-lived background daemon, and starts the CLI through `exec`. |
| `agents/agent_exec.sh` | Claude print runs use `exec` after changing to their requested directory. |
| `agents/codex_mesh.py` | The shared Codex app-server calls `ensure(worktree, environment)` and starts Codex directly with the loaded env. Protected account, run, temporary-directory and held-role values come from the caller. `Refused` logs the helper's line, remembers the failed shell, and falls back to the caller env so other workers keep their shared server. The recorded pid is the server's pid. |
| `accounts/accounts.py` | `exec` calls `launch_shell_environment(cwd)` for every Claude and Codex label. That guard dynamically loads `nix_env` and calls `ensure(cwd, os.environ)`, puts the caller's HOME back, and exits 1 with the helper's reason on `Refused`. |
| `cli_agent/cli_agent.sh` | Interactive and command Claude launches use `exec` in the caller's directory. |
| `notify/review.py` | Loads the current directory's env before the Claude reviewer starts; refusal returns an unreviewed result. |
| `make_a_worktree/direnv_allow.sh` | Thin, silent-on-success `ensure` call; failure retains the helper's stderr and exit status. `commands/make_a_worktree.md` requires stopping on failure. |

The berth fixer reserves its conversation id before launch. It uses `-r` only
when that id's transcript exists in `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects`;
otherwise a rerun keeps the id and uses `--session-id`. Claude encodes the
worktree path by replacing each character outside ASCII letters and digits
with `-`, including slashes, dots and underscores. Every launch also repairs
history left under the legacy worktree path after an interrupted checkout move,
using the selected account's projects directory. It moves the old history only
when that folder exists and the new one does not.

`herdr agent start --help` and the herdr adoption documentation specify that
`--kind claude` selects the executable itself. Prefixing its argument list with
a wrapper would pass that wrapper to Claude as an argument. The integration
therefore passes the loaded environment through the new pane's `--env` values.
Before loading, it removes inherited caller account, pane, Codex and unit
identity variables and the caller's `SHELL`; explicit launch account and unit
marks are then applied. Herdr retains ownership of its pane shell and identity.
Account moves also pass their explicit variable removals through the choke
point; inherited values and direnv exports cannot put those variables back.
The existing `start_agent` APIs retain their signatures and prompt proof.
`PreparedAgentEnvironment` binds a proved environment to its exact directory;
`launch_prepared_agent` opens that directory without another direnv load.

Before tool setup or record access, `verify.sh` repairs a missing nix shell.
When `IN_NIX_SHELL` is empty or unset, it sets `VERIFY_NIX_ENV_REEXEC` and re-execs
once through the sibling `lib/py -S` and `nix_env.py exec`, preserving the absolute
script path, cwd and every original argument, including empty arguments. The
helper owns the nearest `.envrc` search, including ancestors, and runs direnv
export at most once. In the re-run, shell proof triggers one stderr notice;
no `.envrc` means no direnv call or notice. A helper refusal exits nonzero with
only its `nix_env:` line before verification runs. A caller already in the nix
shell takes the original path silently. If the re-run loses shell proof while
`NIX_ENV_ENVRC` still names the loaded file, it refuses with one line instead
of another re-exec. The internal marker is removed before tool setup.
The final command's exit status passes through. This also covers Codex workers
whose shared mesh server cannot carry each worktree's environment.
`user_clock` runs `user_zone.py` with `python3 -S`; that helper and `/zone` share
the `stored_zone` configuration reader.

Launch scan: `scripts/lib/test_claude_launch_scan.py` checks Python process calls
whose argv starts Claude, Codex or `herdr agent start`, typed `start_agent`
calls for either agent kind, and shell launch lines. Queries such as `--help`
and `agents` do not count as starts. Each Python scope must call `nix_env.ensure`,
contain a `nix_env.py` command string, or call a same-module guard by bare name.
A top-level function is a guard when its own body calls `nix_env.ensure` or
`loader.ensure` with `loader` assigned from an expression containing
`import_module("...nix_env")`, including casts; calling another guard propagates
that status until no more guards are found. Nested functions and classes are
excluded from each body's scan, and methods and other-module attribute calls
do not establish module guards. Shell scripts must reference the helper.
No file is exempt.

## Existing sessions

`scripts/nix_env/heal.py` reads each `~/.claude*/sessions` registry through
`message/sessions.py`'s typed reader. It ignores headless print runs and records
without a confirmed process start. `HealSession` carries the session id, pid,
process start, name, cwd, status and config directory. `inspect` distinguishes
`Healthy`, `Broken`, `Unavailable(reason)` and `Gone`; `broken(session)` is true
only for `Broken`. No `.envrc` means no restart is needed. With `.envrc`, Linux
reads `/proc/<pid>/environ`; macOS uses the existing stdlib `ctypes` readers
`whoami/account.py:kern_procargs2` and `procargs2_parts`. They separate arguments
from environment, so a quoted argument cannot supply false nix proof. Identity is checked before
and after the environment read so a reused pid never becomes a restart target.

Run `heal.py sweep`, or restrict it with `--only <session id>`. For each broken
session it checks this machine's build-target hold state, then requires `idle`
or `shell`. Unreadable hold state also prevents restart. It calls `ensure(cwd)`
before requesting `shutdown.py down <account> --here --only <id>` without a
restart continuation, using the account resolved from the config directory.
`down` hands off to a detached conductor; its exit does not prove a stop.
The healer saves a pending restart and uses the conductor's typed shutdown
record to confirm the exact process stopped, without waiting in a loop. It
persists `stop_request_sent: false` before calling `down`, then saves `true`
after `down` exits 0. A rerun with no shutdown record and an unsent request
resends through the same request function only if the original pid and start
still match. If that process is gone or its pid was reused, the pending stop is
cleared, logged and alerted; no relaunch follows. Sent requests keep waiting
for the conductor. Older journals without the mark are treated as sent.
The healer rechecks holds before stopping and before the separate
`shutdown.py restart <saved account> --only <id> --here` launch. A hold between these steps
keeps the restart pending. Each sweep processes pending recoveries first,
including sessions that have left the live registry, then new broken sessions.
After release, it proves the environment again when the stop is complete, then
checks the hold. It passes that loaded environment to the restart process with
`NIX_HEAL_PREPARED_SESSION` and `NIX_HEAL_PREPARED_DIRECTORY`. The restart requires
the same session, resolved cwd, and nix proof, applies the target account and its
variable removals, and consumes this handoff without another load. The markers
remain in that consuming process. Herdr's child-environment filter strips them
from server starts, pane overlays and agent-start CLI processes, including explicit
environment overrides. Later panes cannot inherit another session's proof from
the server. Unit restores through `add_unit` consume the same handoff
at `launch_agent`, matched against the resumed session id in the arguments.
A heal restores only its target, so restoring unrelated
saved panes cannot introduce another direnv load after the final hold check.
Failed stops never authorize relaunch. One bad session does
not abort the rest of a sweep. Nothing is loaded when all sessions are healthy
and no restart is pending.

`~/.local/state/nix-heal/state.lock` excludes overlapping sweeps.
`state.json` reserves a restart before shutdown starts, preserving the pid and
start time. A session id gets at most one restart in 60 minutes, including
failed attempts. A pending stop retains this reservation across holds. Immediately
before relaunch it becomes a durable `launching` reservation; the saved login,
cwd, config directory and original process identity remain in the journal.
The restart command has a 1320-second budget: two 600-second direnv budgets,
herdr's 90 seconds, and 30 seconds for overhead. A timeout, nonzero result or
failure to start retains the reservation and sends the blocked push. The timer
never repeats an unconfirmed launch, including after an interrupted sweep.
Even exit 0 keeps the reservation until a live replacement has the same session,
cwd and account directory, a different process identity, and a healthy nix shell.
A replacement that is still broken, or no healthy confirmation within the launch
budget, sends the blocked push. A late healthy replacement clears the pending
record without starting another copy.
A replacement still missing its nix shell, or an unchanged
broken session after the guard interval, requires user repair. Observing a
healthy session permits future independent recovery after the hour guard.
An unreadable journal prevents automatic restarts.

Every restart, refusal, and change in a skip reason appends one object to
`~/.local/state/nix-heal/log.jsonl`, with keys `time`, `machine`, `session_id`,
`name`, `cwd`, `action`, and `reason`. Times are UTC ISO timestamps. Repeated
unchanged skip reasons do not add lines.

A refused environment or failed recovery sends the registered STE push
`nix_heal.failed` once, with key `nix-heal-<id>`. Its `--clears-when` command is
`heal.py sweep --only <id> --check`: exit 0 requires a live healthy session;
a missing, broken or unreadable process keeps it active. No restart occurs in
check mode. The push catalog owns its title, action and text.

## Timer registration

There is no SessionStart hook. Phase 6 built one and phase 7 removed it before
it was registered (showrunner ruling, 2026-10-28): the five-minute sweep and the
PATH wrappers cover a session that starts outside its shell. Revisit only if a
failed build from such a session is seen.

The local five-minute sweep is a notifier timer, registered on main on natedev:

```sh
~/.claude/scripts/message/notifier.sh new nix-heal --every 5 --run "$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/nix_env/heal.py sweep"
```

The showrunner still owns the bare `claude` and `codex` PATH wrappers in
`/etc/nixos`; each must call this helper with the real absolute executable.
Accounts-unit owns `accounts.py exec`, which calls `launch_shell_environment`;
the scan recognizes that function as a guard.
