# herdr trial: a fake production in herdr panes

## What it is

On 2026-10-13 a fake production ran inside one herdr 0.9.0 session on natedev. It had one showrunner and three unit directors, all Claude, plus six Codex workers (one writer and one reviewer per unit). Each agent ran in its own pane, and the user could watch them all in one ghostty window. The point was to find out what breaks before a real production moves off tmux. The work itself was tiny: three one-phase units, each adding one helper to a small Python package, `textkit`, in a scratch repository with a bare local origin. Nothing was promoted to a real main branch, and the real `~/.claude` main was checked afterwards and holds nothing from the trial.

`scripts/herdr/trial.py` drives the whole trial. Its 98 tests are in `test_trial.py`, where `fake_herdr.py` stands in for herdr and for every other program the driver calls. The trial is torn down; `scripts/herdr/` is kept as the starting point for moving productions to herdr (see "How `scripts/herdr/` works").

## What ran

| Step | When (EDT) | Result |
| --- | --- | --- |
| herdr installed via `nix shell`; server and client as systemd user units | 10:44 | `herdr-trial-server`, `herdr-trial-client` (Konsole at first, later ghostty titled `herdr-trial`) |
| Scratch production scaffolded | before 13:23 | `repo/` on `herdr-fake`, bare `origin.git`, three unit worktrees |
| 10 agents launched in workspace `w9` | 13:23 | showrunner `p1`; per unit a director plus `-impl` and `-review` Codex panes |
| Fake production ran to completion | 13:23–14:10 | each unit's phase 1, shrink and as-built merged into `herdr-fake` and pushed to origin (`c7c92f2`) |
| Teardown and leftovers check written and tested | 14:10–15:13 | 98 tests |
| Trial torn down at the user's word | 15:13 | teardown exit 0; leftovers exit 0 |

## What the user saw

- One ghostty window, titled `herdr-trial` and moved to the requested desktop, showing every agent's pane at once. The first client, Konsole, ran for two hours with no visible window (issue 4).
- Trust prompts on first contact: Claude's folder trust, then Codex's folder trust and its "Hooks need review" screen. The user answered each one once, and the answers were saved, so later panes did not ask (issues 1, 3, 9).
- Real postmaster quota alerts appearing in the fake showrunner's pane (issues 13, 20).

## Findings, ranked by what a real herdr production would have to change

1. **Session identity and unit lookup.** Every production tool finds sessions through tmux or registered state. In the trial:
   - `production_lifecycle.py load` reported every director as `session: gone`, and `waiting.py quota` could not reach any of them (issues 5, 8).
   - `roster.py` listed none of the 10 agents at first. Once the showrunner's state registered, it listed the directors but filed them as `freestanding`, and it never listed the Codex pane workers (issues 7, 17).

   Fix before adopting herdr: the roster, the showrunner lookup and every tmux lookup need a herdr source. `herdr agent list` gives each pane's agent, status and pane id.
2. **Delivering a Work Order.** herdr does not treat Codex's folder-trust screen as blocked. Two Work Orders were pasted into panes that were sitting at that screen and were lost there. Each director then waited the full 30 minutes before resending (issues 10–12, 15). `trial.py work` now:
   - reads `agent get` and refuses a worker that is not `interactive_ready`;
   - confirms the turn started within `HERDR_TRIAL_TURN_START_TIMEOUT`.

   A real launcher needs both checks.
3. **Trust.** Interactive Codex asks for folder trust, keyed on the repository root, and then for hooks trust. Headless workers (`codex app-server`) ask for neither. A herdr production has to settle both before launch, or every new repository puts a prompt in front of the user (issues 1, 3, 9).
4. **Launcher.** herdr refuses multi-line start arguments (issue 2), so opening prompts go through `agent prompt` once the agent is ready. Konsole showed no window, and ghostty did (issue 4).
5. **The showrunner's view of its units.**
   - The fake showrunner reported workers as running because it read its directors' holding lines; the workers had never started (issue 14). Inside herdr it could read worker states directly, but no rule tells it to.
   - The merge script pushes and then fails at its promote step under `Promote: none`, and it has no as-built mode (issue 16). This is being fixed separately by the showrunner's fixer.
6. **Showing one unit.** The user asked for a command that opens the session showing one showrunner or unit. In herdr 0.9.0 every attached window keeps its own view. `agent focus slug-unit` moved herdr's recorded focus (`api snapshot` reported `focused_tab_id` `w9:t2`), but the open window did not move. A newly attached window opened on the showrunner's tab while herdr still reported slug-unit as focused. No CLI option, API field or config key selects the tab a window shows. A herdr production that wants `attach <unit>` needs one herdr session per unit and one for the showrunner. Within one session, the user switches in the window with Ctrl+b and the tab number, or Ctrl+b g for the goto picker.
7. **Relays reach trial sessions.** A Claude pane in herdr is an ordinary local session, so the machine's postmaster sends it real quota alerts (issues 13, 20). That is correct on a shared account. A trial that wants silence would need its own account.
8. **earlyoom and resume.** Every pane is a child of the one `herdr --session herdr-trial server` process. Losing that process (to earlyoom, a crash or a server stop) ends all 10 agents at once. Under tmux, each session is its own tree. Resuming after a server loss was not tried; the herdr Claude integration that would allow it was not installed, because it writes into `~/.claude/hooks/` and `settings.json`.

The showrunner's first lookup at 13:24 said "not running", one minute after launch. A later recheck found it. `showrunners.py` reads registered update-timer instances and session sockets, and neither depends on tmux, so the same first-minute miss is expected under tmux. That inference was not measured.

## Codex workers in panes compared with app-server threads

| | Interactive Codex in a herdr pane | Headless worker (`codex app-server` thread) |
| --- | --- | --- |
| Start | `codex -m gpt-6.1-sol -c model_reasoning_effort=high -s danger-full-access -a never` | thread on a per-session `codex app-server --listen ws://…` |
| Prompts on first use | folder trust, then hooks trust | none |
| Memory | 46–83 MB per pane, plus a 0.3–5 MB `codex-code-mode` child | shared server per session |
| State | herdr reads `idle`, `working` and `done` correctly; misses the trust screen | launcher reads turn status |
| Interrupt | `agent send-keys <name> esc` stopped a turn cleanly | steer/interrupt via the mesh |
| Daemon | uses the account's managed app-server daemon (`--managed-daemon`); none new started | one app-server per delegate session |

One Codex pane stalled with `Working` and a frozen timer on a one-line task. `trial.py wait` timed out as designed, and `esc` recovered the pane (issue 19). No worker hit a quota limit. Claude 1 used 5% of its 5-hour window across the run.

## What worked as the research said

- herdr labelled every agent correctly (`claude`, `codex`) and read their states correctly, except for the trust screen.
- `agent wait --until` and `agent prompt` behaved as documented, and herdr refused a prompt sent to a blocked agent.
- Closing a workspace's last pane removes the workspace.
- `status server` exits 0 even when the server is stopped, so the trial reads its `status: running` line instead.
- The server and client ran for the whole day as systemd user units.

## How `scripts/herdr/` works

The trial driver was removed when `herdr.py` replaced it; see [the adoption plan](../plans/build-followups-herdr-adoption.md).

| File | Role |
| --- | --- |
| `trial.py` | The driver: herdr wrapper, systemd units, client window, launch, `work`/`wait`, teardown, `leftovers` |
| `scaffold_files.py` | Trial-only texts: `UNITS` (slug, clamp, count), `TRIAL_RULE`, `TRIAL_SCRIPT`, the textkit package, production doc, log and unit plans |
| `fake_herdr.py` | Test double for herdr, `systemd-run`, `systemctl`, `kdotool`, the process stopper and the model resolver |
| `test_trial.py` | 98 tests in one class, `TrialTests` |

`trial.py` must run from the full repository. It imports `scripts.herdr.scaffold_files` at load time, imports `scripts.accounts.accounts.role_account` when `HERDR_TRIAL_CODEX_HOME` is unset, and sources the checkout's `scripts/agents/agents_config.sh`.

### The herdr wrapper

The `Herdr` class runs `HERDR_BIN`, else the store path `/nix/store/pyqm5522kvk30b9vgb1ab976pjnjwn51-herdr-0.9.0/bin/herdr`, with `--session herdr-trial` on every call. A non-zero exit raises `TrialFailure` carrying herdr's JSON `error.message` when there is one. Panes are always addressed by id (`w3:p1`), looked up by label in `pane list`; `lookup` returns `PaneFound | NoPane | SeveralPanes`. Every herdr JSON shape has its own `TypedDict` (`PaneListResult`, `AgentListResult`, `CreatedWorkspace`, `CreatedTab`, `SplitPane`, `AgentWaitResult`, `WorkerReadiness`, `HerdrRefusal` and others).

### Commands

Exit 2 is a usage error (bad arguments, bad timeout values, bad `--desktop`) or a blocked worker. Exit 1 is any other failure. Every failure prints one stderr line starting `trial: `.

| Command | Behavior |
| --- | --- |
| `up [--desktop N]` | Validates `started.json`, then `server start`, then `client open`. |
| `server start` | Nothing if `status server` prints `status: running`. Else starts unit `herdr-trial-server` with `systemd-run --user`, records it, and polls until the start timeout; then exit 1, `server herdr-trial not answering after N seconds`. |
| `server stop` | `herdr server stop`, then stops the unit if still active. |
| `client open [--desktop N]` / `client close` | See "Client window". `close` stops `herdr-trial-client`. |
| `open <label> [--cwd D] [--command C]` | One new workspace per label; renames its root pane to the label; runs the command. Refuses an existing label. |
| `close <label>` | Closes that pane. |
| `pane-id <label>` | Prints the id; exit 1 on zero or several matches. |
| `read <label>` | Prints the visible pane text. |
| `type <label> <text>` | Sends the text and Enter. No readiness check. |
| `agents` | `label<TAB>agent<TAB>state` per agent. |
| `issue <session> <expected> <happened>` | Appends one line to `issues.jsonl`. |
| `trust-check <dir>` | Exit 0 only when both accounts trust the exact key (see "Trust"). |
| `scaffold` | Builds the scratch repository. Trial-only. |
| `launch` | Builds the 10-pane layout and starts the agents. |
| `work <unit> <impl\|review> <file>` | Sends one Work Order to a worker pane. |
| `wait <unit> <impl\|review>` | Waits for the worker's report and prints its path. |
| `teardown` / `leftovers` | See "Teardown". |

### Data files

- **`<root>/started.json`** (`StartedResources`): `systemd_units`, `notifier_instances`, `codex_daemons_before`, `codex_daemons_started`. A daemon is a `ProcessIdentity` `{pid, start_time}`, where `start_time` is field 22 of `/proc/<pid>/stat`. A malformed file stops `up` and `launch` before anything starts.
- **`<root>/work/<label>.json`** (`WorkerWorkOrder`): `{"file": "<abs path>"}`, one per worker, path from `work_record(label)`.
- **`<root>/issues.jsonl`** (`TrialIssue`): `{time, session, expected, happened}`, UTC ISO time.
- **The cleanup manifest** (`CleanupManifest`): see "Teardown".

`started.json`, work records and the manifest are written by temp file and `os.replace`.

### Launch

- Workspace `herdr-fake`: tab `showrunner` (pane `showrunner`, cwd `repo/`), and per unit a tab with the director pane `<unit>`, `<unit>-impl` split right of it and `<unit>-review` split below `-impl`, cwd the unit's worktree.
- Unit panes carry `SHOWRUNNER_UNIT=herdr-fake` and `SHOWRUNNER_UNIT_ID=<unit>`; the showrunner pane neither. Claude panes add `CLAUDE_CONFIG_DIR` and `ENABLE_TOOL_SEARCH=true`; Codex panes add `CODEX_HOME`. The caller's `CLAUDE_*` variables are removed from every launch subprocess.
- Accounts: Claude from `HERDR_TRIAL_CLAUDE_CONFIG_DIR` (default `~/.claude1`); Codex from `HERDR_TRIAL_CODEX_HOME`, else the `workers` role account.
- Models: `agents_resolve_print` for `production.director` (must be `claude`), `delegate.impl` and `delegate.review`, run under bash (sourced into zsh it returns empty rows). It reads `~/.claude/config/agents.conf` unless `AGENTS_CONFIG_FILE` says otherwise. `HERDR_TRIAL_RESOLVE` swaps in another program.
- Argv: Codex `-m <model> -c model_reasoning_effort=<effort> -s danger-full-access -a never`. Claude `--model --effort`, plus `--remote-control <name>` for the showrunner and directors, then `-n <name>`. A worker that resolves to claude logs an issue and starts as Claude.
- Start: `agent start` with no prompt; once ready, `agent prompt` sends the opening text (`/showrunner:produce` or `/unit:direct` with its doc plus `TRIAL_RULE`; workers a one-line wait message). Order: six workers, three directors, the showrunner.
- Refusals before anything is created: server not answering, workspace `herdr-fake` exists, a start folder fails trust, or the director does not resolve to claude. `agent_not_ready` logs the visible screen and exits 1 with no keys sent.
- `codex_daemons_before` is recorded once (a retry keeps it); new managed daemons go to `codex_daemons_started` after the workers start or fail.

### Work Orders

- `work` resolves the file (exit 1 if missing) and reads `agent get` as `WorkerReadiness`. Unless `interactive_ready` is true and `agent_status` is `idle` or `done`, it logs an issue and exits 2 with nothing sent. Otherwise it removes a stale `<file>.done`, writes the work record and sends `agent prompt` once: "Read and carry out <file>. When done, write your report to <file>.done." It then waits `--until working` under `HERDR_TRIAL_TURN_START_TIMEOUT`. A started turn, or a report already written, returns 0; else it removes the record, logs an issue and exits 1.
- `wait` reads the work record (exit 1 when absent) and runs under one deadline, `HERDR_TRIAL_WAIT_TIMEOUT`. It runs a settled `agent wait`; when the worker is idle or done with no report, it waits `--until working` and loops. A `blocked` state or an `agent_blocked` refusal exits 2 with an issue line. At the deadline with no report it exits 1. It never sends the prompt again.

### Trust

One exact key serves both agents: the repository's main worktree root (the parent of `git rev-parse --path-format=absolute --git-common-dir`), or the folder itself outside git. Claude needs `projects[key].hasTrustDialogAccepted` true in `<claude>/.claude.json`; Codex needs `projects[key].trust_level = "trusted"` in `<codex>/config.toml`. Ancestor and sibling-prefix entries do not count. Nothing writes either file.

### Client window

`client open` launches `HERDR_TRIAL_TERMINAL` (default `ghostty`) `--gtk-single-instance=false --title=herdr-trial -e <herdr> --session herdr-trial` as unit `herdr-trial-client`. It polls `kdotool search --name ^herdr-trial$` until the start timeout and, with `--desktop N`, moves the window there. No window, or a failed move, stops the unit and exits 1. An active client unit that has a window is moved and kept; one with no window is replaced.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `HERDR_BIN` | the store path | herdr binary |
| `HERDR_TRIAL_ROOT` | `/home/natepiano/rust/herdr-trial-scratch` | scratch root |
| `HERDR_TRIAL_START_TIMEOUT` | `10` s for server and client; `90000` ms for `launch` | readiness deadlines |
| `HERDR_TRIAL_TURN_START_TIMEOUT` | `60000` ms | `work`: time for the turn to start |
| `HERDR_TRIAL_WAIT_TIMEOUT` | `1800000` ms | `wait`: overall deadline |
| `HERDR_TRIAL_MANIFEST` | `~/.local/state/herdr-trial/cleanup.json` | cleanup manifest; must sit outside the scratch root |
| `HERDR_TRIAL_TERMINAL` | `ghostty` | client terminal |
| `HERDR_TRIAL_KDOTOOL` | `kdotool` | window search and move |
| `HERDR_TRIAL_SYSTEMD_RUN`, `HERDR_TRIAL_SYSTEMCTL` | `systemd-run`, `systemctl` | unit start, status and stop (always `--user`) |
| `HERDR_TRIAL_GIT` | `git` | scaffold git and the trust key lookup |
| `HERDR_TRIAL_RESOLVE` | bash plus `agents_config.sh` | model resolver |
| `HERDR_TRIAL_CLAUDE_CONFIG_DIR` | `~/.claude1` | Claude account for launch and the manifest |
| `HERDR_TRIAL_CODEX_HOME` | the `workers` role account | Codex account for launch and the manifest |
| `HERDR_TRIAL_PROC` | `/proc` | process inspection root |
| `HERDR_TRIAL_STOP_PROCESS` | `os.kill(pid, SIGTERM)` | program that stops a pid |
| `HERDR_TRIAL_NOTIFIER_ROOT` | `~/.local/state/notifier` | notifier instance folders |
| `HERDR_TRIAL_SHOWRUNNER_STATE` | `~/.local/state/showrunner` | showrunner state |
| `HERDR_TRIAL_HERDR_CONFIG` | `~/.config/herdr` | herdr config and session state |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME` | `~/.claude1`, `~/.codex1` | `trust-check` only |

The fixture boundaries are `HERDR_TRIAL_PROC`, `HERDR_TRIAL_STOP_PROCESS`, `HERDR_TRIAL_NOTIFIER_ROOT`, `HERDR_TRIAL_SHOWRUNNER_STATE` and `HERDR_TRIAL_HERDR_CONFIG`; the tests point every variable above at temp fixtures.

### Tests

- Run: `python3 -m unittest discover -s scripts/herdr -p 'test_*.py'` from the repository root. Tests never start herdr, Claude or Codex.
- Lint: `basedpyright scripts/herdr`. A pass is its `0 errors, 0 warnings` line; it exits 3 on the missing `.venv`.
- `fake_herdr.py` is copied into a temp folder with the test interpreter's shebang. Each call appends its argv as one JSON line to `FAKE_HERDR_LOG` and answers from `FAKE_HERDR_RESPONSES`, keyed by the space-joined argv, else `"default"`. A list of responses replays in order and repeats the last. Response fields: `stdout`, `code`, `stderr`, `write_files` (path to text, for report fixtures), `require_paths` (exit 98 when a path is missing) and `remove_paths`.
- Two tests parse the scaffolded production doc with the real parsers, imported from the live `/home/natepiano/.claude/scripts/production`.

### Reusable and trial-only parts

Reusable: the `Herdr` wrapper, systemd unit start, record and stop, the `status: running` check, the ghostty and kdotool client, `worker_readiness` with the `work`/`wait` pattern and turn-start confirmation, exact-key trust, pane environment and argv building, process identity and the daemon-stop checks, atomic JSON writes and `remove_exact_path`.

Trial-only: `scaffold` and its scratch git setup, all of `scaffold_files.py`, `SCRATCH_ROOT`, the worker labels and `work`/`wait` choices taken from `UNITS`, the names `herdr-fake` and `herdr-fake-*`, the fixed manifest state paths, the retained-grant report keyed on `<scratch>/repo`, and the `trial_process` markers.

## Invariants

- Nothing sends a key to a trust or permission prompt. `launch` reads and logs an unready screen; `work` refuses a worker that is not `interactive_ready` and `idle`/`done`; `agent_blocked` and a `blocked` state send nothing. The manual `type` command is the one exception.
- One prompt per Work Order, never sent again. `wait` only observes.
- Opening prompts follow a successful `agent start`; `agent start` carries no prompt.
- Teardown sends no input: it runs only `workspace list`, `workspace close`, `server stop` and `status server`. A test asserts no `send-text`, `send-keys`, `prompt`, `run` or `start` call.
- No kill by pattern. Only recorded systemd units and recorded, still-matching daemons are stopped.
- Exact-path removal only. Notifier names must be plain folder names.
- Fail closed: a shutdown failure, a server that still answers or a surviving trial process keeps the scratch tree and all state.
- Trust grants are reported, never changed.
- Scratch git clears every `GIT_*` variable and sets `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `core.hooksPath=/dev/null` and no signing, so the user's global hooks and signing never fire.
- Python: basedpyright 0/0, no file-level ignores, a `TypedDict` for every herdr JSON shape, no `Any`.

## Gotchas

herdr 0.9.0:
- `workspace create --label` labels only the workspace; the root pane needs `pane rename`.
- `pane read` and `status server` print plain text, not JSON.
- herdr creates `~/.config/herdr/` (`.plugins.lock`, `release-notes.json`, `sessions/<name>/`).
- A standalone `agent wait` tracks the agent's lifecycle, not a turn: a worker that has not yet started its prompt reads as idle and returns at once. That is why `work` and `wait` wait for `working`.
- A typed `/quit` does not exit Codex; close the pane. `/exit` stops Claude.
- A Claude pane on claude1 turns on Remote Control by itself.
- Interactive Codex on `~/.codex1` uses the account's managed `codex app-server --managed-daemon`, which outlives its pane.

In the code:
- `HERDR_TRIAL_START_TIMEOUT` has two units: float seconds for server and client, integer milliseconds for `launch`. `0.5` makes `launch` exit 2; `90000` makes `server start` wait 25 hours.
- `launch` addresses agents by the name given to `agent start`; `work` and `wait` use the pane id.
- `trust-check` reads `CLAUDE_CONFIG_DIR` and `CODEX_HOME`; `launch` reads `HERDR_TRIAL_CLAUDE_CONFIG_DIR` and `HERDR_TRIAL_CODEX_HOME`. They can disagree.
- `scaffold_files.TRIAL_SCRIPT` hard-codes this worktree's path to `trial.py`. It breaks when the worktree moves or is deleted.
- A manifest already at the manifest path is reused as is. Delete it once `leftovers` passes, or it drives the next teardown.
- Sourcing `agents_config.sh` can run `sync_codex_catalog.sh` when the catalog is stale.
- The launch daemon scan matches `codex` as a substring of any argv element; `codex_process` matches it in an argv basename.
- `production_check.sh` is zsh; under bash it fails.

## Why it is built this way

- **Scratch root under `~/rust`.** No session may face a trust prompt, and none is answered for the user. The scratch root `/home/natepiano/rust/herdr-trial-scratch/` sits where the user can grant trust once on `repo/`, which then covers every scratch worktree.
- **One named herdr session, `herdr-trial`.** Nothing joins or disturbs a default herdr session.
- **Units launched by `trial.py launch`, not `add_unit.py`.** `add_unit.py` started tmux sessions when the trial was built (herdr sessions since herdr-adoption Phase 7). Each place the production commands assume tmux became a finding.
- **`Promote: none`.** The fake production merges to its own branch and pushes to the bare origin; nothing reaches `~/.claude`.
- **One work record per worker.** Three directors call `work` at once; one file each needs no lock.
- **The manifest outside the scratch tree.** Retries and `leftovers` still work after the scratch tree is deleted.

## Ruled out

- A shared `work.json` with a lock: concurrent `work` calls from three directors lost an assignment.
- Konsole as the client: no window appeared.
- Opening prompts as `agent start` arguments: herdr refuses multi-line text.
- Trust from an ancestor folder: both agents still prompt.
- Removing the trust grants automatically.
- Counting any `codex app-server`, rather than `--managed-daemon`, as trial-started.
- Treating another user's unreadable processes as Codex clients.
- A `herdr-show` command over one shared session: no CLI, API or config way chooses an attached window's tab.

## Teardown

`trial.py teardown` reads the cleanup manifest at `~/.local/state/herdr-trial/cleanup.json` (overridden by `HERDR_TRIAL_MANIFEST`), writing it on its first run from `started.json`. The manifest (`CleanupManifest`) holds the scratch root, the systemd units, the notifier instances (always `showrunner-herdr-fake`), the exact state paths, the herdr config, both account paths and the daemon baseline and started list. It sits outside the scratch tree so that retries and `leftovers` still work after the scratch tree is deleted. Teardown then:
- copies `issues.jsonl` beside the manifest;
- closes every workspace in session `herdr-trial` without sending a single key, then stops the server;
- stops the recorded systemd units that are still active;
- refuses to go on while the server still answers;
- stops only a managed Codex daemon that it recorded as started, that is absent from the baseline, and that still runs `codex app-server --managed-daemon` on the same account with the same pid and start time, and only when no other Codex client or unreadable process of the current user uses the account;
- refuses to delete anything while a trial process survives: environment `SHOWRUNNER_UNIT=herdr-fake` or `HERDR_SESSION=herdr-trial`, argv `--title=herdr-trial` or `--session herdr-trial|herdr-devcheck`, or a cwd under the scratch root;
- removes the recorded state paths by exact name (the notifier instance, `~/.local/state/showrunner/herdr-fake/`, herdr's `sessions/herdr-trial` and `sessions/herdr-devcheck`, `.plugins.lock`, `release-notes.json`), then `sessions/` and `~/.config/herdr/` only if empty, then the scratch root;
- runs the leftovers check and exits 1 if anything remains.

`trial.py leftovers` exits 0 only when no trial process, unit, timer, state folder or scratch path remains, and names each survivor:
- It prints a surviving daemon as `trial-started` or `shared`.
- It ignores Codex daemons from the baseline, daemons it never recorded, and delegate mesh servers.
- It reports the retained trust grants without changing them.

Review found that the first version would have recorded five other sessions' delegate mesh servers as trial-started. It would also have treated other users' unreadable processes as Codex clients. Both are fixed, with regression tests. Two narrower edges remain, found by the last review and left as written because this trial recorded no managed daemon, so neither can stop anything here: the outside-client scan runs once before all daemon stops rather than before each one, and any process with an argv element exactly `app-server` (a `codex exec app-server` call, not only the subcommand) is left out of the outside-client check. Fix both before reusing this teardown elsewhere.

**Retained, by design:** Claude's `hasTrustDialogAccepted` for `repo/`, Codex's `trust_level=trusted` for `repo/`, and the Codex hooks trust in `~/.codex1/config.toml`. Teardown reports these and leaves them for the user to remove.

**Status:** ended 2026-10-13 15:13 EDT, when the user said they were done and want herdr for every production and standalone session.
- `trial.py teardown` exited 0. It closed workspaces `w8` and `w9` without sending a key, stopped the herdr server and both systemd units, and removed the scratch tree plus the recorded state:
  - `~/.config/herdr/sessions/herdr-trial` and `herdr-devcheck`;
  - the fake showrunner's notifier and showrunner state.
- It named the five delegate mesh servers as `not a managed Codex daemon; retained` and left them running. All five were confirmed alive afterwards.
- `trial.py leftovers` then exited 0, listing only the three retained trust grants above.
- The 20 logged issues are in this document. Their archive and the cleanup manifest under `~/.local/state/herdr-trial/` were deleted after the checkpoint notice.
- **`scripts/herdr/` is kept.** The user is moving productions and standalone sessions to herdr, and these helpers are the tested starting point: server and client as systemd units, readiness before a prompt, turn-start confirmation, and a teardown that never signals another session's processes. The trial-only parts (scaffold, fake units, scratch paths) go when that work replaces them.
