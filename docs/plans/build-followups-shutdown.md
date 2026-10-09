# Shutdown and restart

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `/shutdown` brings every Claude session of one account on natedev and the Mac to a safe stop, stops its update timers and records where each one was. `/shutdown restart` resumes each session there and restarts its timers. Every session shows which account it runs on.

> **As-built disposition: create**

> **Production: build-followups** — unit `shutdown-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

2026-10-09 14:49 PDT

The user's words, 2026-10-08: "For later I want a command that fully shuts down Claude safely and fully restarts later."

The user chose, 2026-10-09 14:5x PDT, the scope "both machines: every showrunner, unit director, seat and Codex server on natedev and the Mac first reaches a clean, pushed state, its update timers stop, and where it was is recorded; restart resumes each session where it was and restarts its timers; CI runners and system services keep running", adding in his words: "yes both machines - but also be aware that this command should be account aware - in the future if we can have machines (on machine or on different machines) running different accounts - we need to only e shutting down the account we've been asked to shutdown (we need a way to surface the account we are, easily)"

Context (showrunner): a proposed, not yet approved, plan for sessions on different Claude/Codex accounts is at ~/.local/state/plan-backlog/2026-10-09-per-session-accounts.md; read it for how accounts are found today (scripts/whoami/agent_accounts.py follows CLAUDE_CONFIG_DIR and CODEX_HOME) and design the account filter and the "which account am I" surface so they fit it, without building that plan.

## Delegation Context

- **Project:** `~/.claude` config repo: the scripts and commands every Claude Code and Codex session on natedev and the Mac runs. This plan adds `scripts/whoami/account.py`, `scripts/shutdown/` and `commands/shutdown.md`.
- **Project started:** 2026-10-09T21:49:10+00:00
- **Stack:** Python 3 standard library only (`argparse`, `json`, `subprocess`, `os`, `signal`, `fcntl`, `shlex`, `dataclasses`, `unittest`), checked by basedpyright; zsh (`scripts/message/notifier.sh`); jq (the status line); `ssh mac` / `ssh natedev` (Tailscale SSH); tmux; on natedev `ghostty`, `kdotool`, `systemd-run`; on the Mac `open`, `launchctl`, `ps eww`.
- **Layout:** `scripts/whoami/` (account resolver, `/whoami`) · `scripts/shutdown/` (new: `shutdown.py` CLI, `inventory.py`, `remote.py`, `record.py`, `settle.py`, `stop.py`, `restart.py`, tests beside them) · `scripts/statusline/statusline.jq` · `settings.json` (status line command, one SessionStart hook) · `commands/shutdown.md` (new), `commands/whoami.md`, `commands/message.md` · touched for one fix each: `scripts/hooks/conversation_pause.py`, `scripts/delegate/remove_seats.py`, `scripts/production/add_unit.py`, `scripts/nightly_review/nightly_review.py`, `scripts/fix/fix-trigger.sh` · `pyrightconfig.json` (one entry).
- **Definitions** (bind every phase):
  - **Account.** One Claude login. A process's account is the login in its Claude config dir: `CLAUDE_CONFIG_DIR` from that process's environment, else `~/.claude`, whose `.claude.json` is `~/.claude.json` (`scripts/whoami/agent_accounts.py:224-232`). The login is `oauthAccount.emailAddress` in that `.claude.json` (present on both machines; checked 2026-10-09). Its **label** is the stem of the hanadocs note in `~/rust/hanadocs/agents/` whose `login:` matches it, ignoring case (`claude 2`); with no matching note, the login itself. The account of a seat is its daemon's config dir, the director's; a Codex app-server belongs to the account of the session whose run folder it serves. Nothing new is stored: the label file Phase 1 writes is a cache, rewritten from the login. This fits the per-session accounts proposal (`~/.local/state/plan-backlog/2026-10-09-per-session-accounts.md`: one `CLAUDE_CONFIG_DIR` per extra account, "a directory's login is read from it and matched to its hanadocs note") and builds none of it.
  - **The set** for account A on one machine: every live Claude session record in `~/.claude/sessions/` whose process's account is A, each classed `showrunner` (its id is the `TARGET` of a `showrunner-<slug>` notifier instance), `unit` (its tmux pane carries the `SHOWRUNNER_UNIT` / `SHOWRUNNER_UNIT_ID` marks), `seat` (`kind == "bg"`) or `top-level` (every other one); plus the Codex app-servers in each one's run folders (`/tmp/claude/delegate/active/<session id>` names the folder on its first line), and every notifier instance whose `conf` has `TARGET=session:<id>` for an id in the set.
  - **Clean, pushed state.** The session and every seat it owns are idle (no turn running); no commit on its checkout's branch is ahead of its upstream; a showrunner also has no merge in flight and its merge branch pushed. Uncommitted edits stay on disk and are listed in the record. My call, stated so the user can overrule it: a commit mid-phase would put work-in-progress commits on unit branches, and pushing uncommitted work to a remote would publish it; the revert is one Spec line in the settle phase.
  - **Out of scope.** The user, 2026-10-09: "CI runners and system services keep running" — every systemd and launchd timer and service, the GitHub runners, steve, sccache, earlyoom, the session-notifier job and its machine-wide instances (`stall-watch`, `tmux-names`, `escalate`, `conversation-pause`). My calls, each a one-line revert: Codex TUI sessions and the Claude and ChatGPT/Codex desktop apps, with the Codex app's own `--managed-daemon` app-server (none is a Claude session, a seat or a mesh Codex server, the user's list).
- **Key files:**
  - `scripts/whoami/agent_accounts.py` — `claude_config_dir` 224, `claude_config_file` 228-232, `codex_home` 235, `claude_on_disk` 314-337 (reads `oauthAccount`), `codex_email_on_disk` 343-358 (decodes the `id_token` email). Both read the current environment only.
  - `scripts/whoami/agent_notes.py` — `AGENTS_DIR` 36, `Note` 42, `read_note` 60, `read_notes` 215; a note matches on `login:` (178). `scripts/whoami/whoami.py` — `render` prints `Account: <email>` (25-52). `scripts/whoami/test_agent_notes.py` — fixture for fake notes.
  - `scripts/statusline/statusline.jq` and `settings.json` `statusLine` (`exec jq -r --arg pwd "$PWD" -f …/statusline.jq`): two processes per refresh, on purpose (statusline.jq header). `settings.json` `hooks.SessionStart` holds three command hooks.
  - `scripts/message/sessions.py` — `SessionRecord` 15, `read_session` 34, `live_session` 72 (pid alive and socket present), `sessions_dir` 85 (`NOTIFIER_SESSIONS_DIR`), `live_sessions` 89. Record fields: `pid, sessionId, cwd, kind ("interactive" | "bg"), name, status ("idle" | "busy" | "shell"), tmux, messagingSocketPath, procStart, formerNames`.
  - `scripts/production/unit_lookup.py` — marks 25-26, `runs_dir` 66 (`PLAN_DELEGATE_HISTORY_DIR`), `run_state` 85, `tmux_binary` 116 (`UNIT_LOOKUP_TMUX`), `marked_units(slug)` 181. Run records `~/.local/state/plan-delegate/runs/<run>.jsonl`, first line `run_started` with `main_agent.session_id`, `working_dir`, `plan_doc`, `branch`.
  - `scripts/production/showrunners.py` — `registered_showrunners()` 177 (from `~/.local/state/notifier/showrunner-*`: `TARGET` session id and the production doc from `CHECK`), `production_slug` 196.
  - `scripts/message/notifier.sh` — state under `NOTIFIER_STATE_DIR` (13); `stop` keeps `NEXT_DUE`, `start` schedules from now, `resume` keeps `NEXT_DUE`; `new` on an existing instance never changes `ENABLED` (159-165); `launch_run` 417-429 is the pattern for a detached job on both platforms (`systemd-run --user --collect --quiet --no-block --unit <label>` / `launchctl submit -l <label>`).
  - `scripts/hooks/conversation_pause.py` — `state_root` 199 (`CONVERSATION_PAUSE_STATE_DIR`), `record_path` 208, `session_reports` 384, `_resume_items` 668, `resume` 711, `_advance` 911-916: when a paused session is gone, the tick re-enables the instances and footers its record lists. `scripts/hooks/test_conversation_pause.py`.
  - `scripts/agents/codex_mesh.py` — `stop --session-dir D` 2032-2071 (SIGTERM, SIGKILL after 5 s, deletes `mesh_server.json`, keeps `mesh_roster.json`); `list --session-dir D` 2017-2029; roster statuses `done | failed | running | starting | waiting_capacity | capacity_exhausted`; `follow` resumes a thread and starts a server when none runs.
  - `scripts/delegate/remove_seats.py` — `live_runs(root, listed_sessions)` 103-118: a run counts as live only when its director is listed by `claude agents` or its `heartbeat.log` moved in 600 s; otherwise the next run's `prepare_session.sh` removes its seats. `scripts/delegate/test_remove_seats.py`.
  - `scripts/production/add_unit.py` — argument checks 225-232 (`--resume` needs `--cwd` and `--plan`), `prompt_for` 570-593, `launch_session` 596-616 (`systemd-run --user --scope --unit=<name> tmux new-session … zsh -ic 'ENABLE_TOOL_SEARCH=true command claude … [--resume SID] --remote-control <name> -n <name> --settings {"disableAgentView": true} "<prompt>"; exec zsh'`, `CLAUDE_*` stripped), `wait_for_remote_control` 623-640, `main` 653-698. `scripts/production/test_add_unit.py`, `scripts/production/fake_tmux.py`.
  - `scripts/message/send.py` — `--to <session name | session:<id> | user>`, `--from <sender>`, `--summary`, `--need note|decision|blocked` (to the user: Pushover), `--machine <host>` runs it on the other machine (475-499); `pending` prints and clears messages queued for the caller while it was down; `machine()` 143.
  - `/etc/nixos/modules/linux/agent-sessions-snapshot.py` and `agent-sessions-restore.py` (natedev only; read, never edited by this plan) — `agent-sessions-snapshot` rewrites `~/rust/hanadocs/agent sessions.md`: `## desktop: <name>` headings, one line `` `<dir>` · <terminal> · <name> `` per session and a fenced `cd <dir> && claude --resume <id> -n "<name>"`; restore 108-137: `kdotool set_desktop <n>` (numbers from `~/.config/kwinrc` `[Desktops]` `Name_<n>`), then `ghostty -e zsh -ic "<command>; exec zsh"`, 1.2 s apart, then back to the starting desktop.
  - `scripts/production/test_merge_checkpoint.py` 44-58 — a stand-in `ssh` on `PATH` that logs argv and prints the remote status as text.
  - `scripts/nightly_review/nightly_review.py` — `quota_block` 62-70 and `launch` 89-95 (a skip reason becomes the `skipped:` line). `scripts/fix/fix-trigger.sh` — the scheduled entry to the fix pipeline; its `pgrep` guard (24-27) exits 0.
  - `commands/codex_winddown.md`, `commands/build_hold.md` — models for a short command doc with verbs.
- **Test lanes:** `scripts/whoami/` → `scripts/whoami/test_account.py` (new) · `scripts/shutdown/` → `scripts/shutdown/test_*.py` (new) · `scripts/hooks/test_conversation_pause.py` · `scripts/delegate/test_remove_seats.py` · `scripts/production/test_add_unit.py` · `scripts/nightly_review/test_nightly_review.py`. Tests sit beside the code and import their sibling by bare name.
- **Build:** none; the code is interpreted.
- **Test:** `python3 -m unittest discover -s <directory> -p '<pattern>'` from the repo root, with the directory and pattern each phase names.
- **Lint:** `basedpyright <files>`. A pass is its last line `0 errors, 0 warnings, 0 notes`; it exits 3 because `pyrightconfig.json` names a `.venv` no checkout has. Shell: `zsh -n` / `bash -n <file>`. The status line: `jq -n -f scripts/statusline/statusline.jq --arg pwd x --arg account x` parses.
- **Invariants:**
  - **Account isolation** (the user, 2026-10-09: "we need to only e shutting down the account we've been asked to shutdown"). A command acting for account A never messages, stops or changes a timer of a session whose account is not A, nor stops a Codex server whose owning session is not A's. An environment or config that cannot be read means "not A": the session is listed as `unknown account` and left alone.
  - Never stop a busy session (`status` other than `idle`) unless the user ran `/shutdown now`. Never type C-c or Escape into a pane (`commands/showrunner/produce.md` 70-76).
  - **Stopping a Claude session is SIGTERM while idle.** Checked 2026-10-09 by this unit: an idle interactive session exits 143 within 0.5 s, prints `Resume this session with:`, its record leaves `~/.claude/sessions/`, and `claude --resume <id>` restores the whole conversation.
  - **Claude seats hold no process between turns.** A seat's job stays in `~/.claude/jobs/<short>/state.json` (`state: "stopped"`, `respawnFlags`, `daemonShort` = the first 8 hex of its session id) and a message respawns it; the daemon roster was empty with seats listed (checked 2026-10-09). Shutdown never runs `claude rm` or `claude stop` on a seat.
  - **Codex seats come back on demand:** `codex_mesh.py follow` starts a server when none runs and resumes the thread (`docs/as-built/agent-registry.md` 239-243). Shutdown stops servers only with `codex_mesh.py stop --session-dir`, never `sweep --stop`, and restart starts none.
  - Notifier instances are stopped, never removed: the `showrunner-*` instance is the only record that a production exists (`showrunners.py` 177).
  - Never trust ssh's exit status: Tailscale SSH on the Mac returns 0. Every remote command prints `rc=<n>` as its last line, and that line is the status; ssh's own 255 or a timeout means unreachable.
  - The Mac: user `natemccoy`, home `/Users/natemccoy`, hostname `Mac`, ssh alias `mac` (from the Mac: `natedev`); no `/proc` (a process's environment is `ps eww -o command= -p <pid>`, checked 2026-10-09), no tmux on `PATH` (`unit_lookup.tmux_binary` falls back to nix), no KWin; Python only through `~/.claude/scripts/lib/py`.
  - Tests start no Claude or Codex, touch no live notifier instance, open no window and reach no machine: every state dir comes from an environment variable (`SHUTDOWN_STATE_DIR`, `NOTIFIER_STATE_DIR`, `NOTIFIER_SESSIONS_DIR`, `CONVERSATION_PAUSE_STATE_DIR`, `PLAN_DELEGATE_HISTORY_DIR`, `SHUTDOWN_DELEGATE_ROOT`, `AGENT_NOTES_DIR`), and `ssh`, `tmux`, `claude`, `ghostty`, `kdotool`, `systemd-run`, `open` and `kill` are stand-ins on `PATH` or injected functions.
  - Python: basedpyright zero errors and zero warnings, no file-level ignores, no `Any` (a `TypedDict` for each JSON shape).
  - Every time shown to the user is in America/Los_Angeles (the production's **User zone**).
  - A Work Order runs lint once after the seat's edits; when lint fails, fix the error it names, then lint once (ProductionUnit item 15).

## Phases

### Phase 1 — Which account am I  · status: done

#### As-built

- `scripts/whoami/account.py` resolves the login behind a config directory or a process: frozen dataclass `Account(tool: Literal["claude", "codex"], login: str, label: str)`; `claude_account(config_dir: Path) -> Account | None`, `codex_account(home: Path) -> Account | None`, `process_config_dir(pid: int) -> Path | None`, `parse_darwin_config_dir(output: str, expected_uid: int) -> Path | None`, `account_of(pid: int) -> Account | None`, `label_for(tool: str, login: str) -> str`, `write_label(config_dir: Path, account: Account) -> None`.
- `label_for` returns the stem of the note in `AGENT_NOTES_DIR` (default `agent_notes.AGENTS_DIR`) whose stem starts with the tool and whose `login:` equals the login, case-folded; else the login. `write_label` writes `<config dir>/account-label` (label plus newline) only when the content differs, through a temp file and `os.replace`. `process_config_dir` reads `/proc/<pid>/environ` on Linux and `ps eww -o uid=,command= -p <pid>` on darwin; an unset `CLAUDE_CONFIG_DIR` gives `~/.claude`, an unreadable environment gives `None`, never the default.
- CLI `account.py [--json] [--pid PID] [--write-label]`: plain output `claude 2 (<email>) · codex 2 (<email>)`, with `unavailable` for a missing account; `--pid` reports the Claude account only (`codex` is `null` under `--json`); `--json` prints `{"claude": {"login", "label"} | null, "codex": … | null}`; `--write-label` refreshes the label file of this process's config dir, or deletes it when the Claude account is unreadable. Exit 1 when the Claude account cannot be read, else 0.
- `agent_accounts.claude_on_disk(config_dir: Path | None = None)` and `codex_email_on_disk(home: Path | None = None)` take an optional directory; `claude_config_file(config_dir)` maps the default `~/.claude` to `~/.claude.json` and any other directory to `<dir>/.claude.json`. Both readers type-check the decoded JSON and report the account unreadable instead of raising.
- The `statusLine` command reads `account-label` with the `read` builtin (two processes: sh and jq) and passes `--arg account`; `statusline.jq` binds `$ARGS.named.account // ""` and appends ` | <account>` when non-empty. A `SessionStart` hook runs `account.py --write-label`. `whoami.py` `render` prints `Account: <label> — <email>` when a note matches, else `Account: <email>`.

**Files:**
- `scripts/whoami/account.py` — the resolver, label cache and CLI.
- `scripts/whoami/agent_accounts.py` — the Claude and Codex on-disk readers, each taking an optional directory.
- `scripts/whoami/whoami.py` — the label on the Account line.
- `scripts/statusline/statusline.jq` — the trailing account field.
- `settings.json` — the status line command and the `account.py --write-label` `SessionStart` hook.
- `commands/whoami.md` — names the status line's last field and `account.py`.
- `scripts/whoami/test_account.py` — resolver, darwin parser, label cache, CLI and status line tests.

**Binds later work:** the `/shutdown` session inventory filters by `account.account_of(pid)`; `None` means unknown account, and that process is left alone. `account.parse_darwin_config_dir(output, expected_uid)` is the pure Mac parser over `ps eww -o uid=,command=` output: it returns `None` unless the uid matches and a `HOME=` token is present. macOS `ps eww` shows no environment for platform binaries (`/bin/zsh`, `/bin/sleep`) or other-uid processes yet exits 0, so the lookup returns `None` for them; Claude itself, not a platform binary, shows its environment. The label file is `<config dir>/account-label`; a missing file means no account field.

**Gotchas:** `ps eww` output is unquoted argv followed by the environment, split on whitespace, so the last `CLAUDE_CONFIG_DIR=` token wins and a config dir path containing whitespace is truncated. `statusline.jq` must keep working without `--arg account`, for a `settings.json` that predates the account field. Running `account.py` on the Mac from a temp dir needs a full copy of `~/.claude/scripts`: `agent_notes` imports reach `codex_pacer`, `quota_alert` and `..production` relative imports.

**Ruled out:** `shlex` parsing of `ps` output (the output is unquoted); defaulting to `~/.claude` when the environment is hidden (it would attribute an unknown process to the default account).

### Phase 2 — What runs on an account  · status: todo

#### Work Order

**Goal:** `/shutdown status` lists, for one account on natedev and the Mac, every session it would stop with its kind, host and checkout state, every Codex server and every timer — and what is already down.

**Spec:**

`scripts/shutdown/inventory.py`:

```python
class Host(TypedDict):          # how to bring the session back
    kind: Literal["unit", "tmux", "ghostty", "zed", "terminal", "unknown"]
    production: NotRequired[str]   # unit: production slug
    unit: NotRequired[str]         # unit: unit id, e.g. "shutdown-unit"
    doc: NotRequired[str]          # unit and showrunner: production doc path
    plan: NotRequired[str]         # unit: plan path from its newest run record
    tmux_session: NotRequired[str] # tmux: session name
    desktop: NotRequired[str]      # ghostty and zed on natedev: KWin desktop name

class Checkout(TypedDict):
    path: str; branch: str; head: str
    ahead: int | None              # None: no upstream
    dirty: list[str]               # `git status --porcelain` paths

class CodexServer(TypedDict):
    run_dir: str; pid: int; busy_seats: list[str]   # roster entries running or starting with a live launcher

class Session(TypedDict):
    session_id: str; pid: int; name: str; cwd: str
    kind: Literal["showrunner", "unit", "seat", "top-level"]
    status: str                    # the record's idle | busy | shell
    host: Host
    model: str | None              # the transcript's last assistant message.model
    checkout: Checkout | None      # None: cwd is not in a git work tree
    run_dirs: list[str]
    codex_servers: list[CodexServer]
    timers: list[str]              # notifier instance names targeting it
    owner: str | None              # seat: the session id of its director

class Inventory(TypedDict):
    machine: str; login: str; label: str
    sessions: list[Session]
    unknown: list[str]             # "<pid> <name>": account unreadable, left alone

def inventory(login: str, only: frozenset[str] = frozenset()) -> Inventory
```

- Sessions: `sessions.live_sessions()`; keep those whose `account.account_of(pid)` login equals `login` (ignoring case); an unreadable one goes to `unknown`. `only`, when not empty, keeps those session ids and the seats they own (a test and live-check aid; no command doc names it).
- Kind: `showrunner` when `showrunners.registered_showrunners()` has it as `TARGET` (host `doc` from there); `unit` when the pane in its record's `tmux` field carries the unit marks (`unit_lookup.marked_units`, read for every slug of a registered showrunner); `seat` when `kind == "bg"` — its owner is the session whose run folder's `seats` ledger lists `daemonShort`; else `top-level`.
- Host: `unit` from the marks, with `plan` from the newest run record whose `main_agent.session_id` is the session; `tmux` when `/proc/<pid>/environ` has `TMUX_PANE` and no marks (`tmux display -p -t <pane> '#S'` gives the name); otherwise walk parents (`ps -o ppid=,comm= -p`) to `ghostty`, `zed`/`zed-editor` or, on the Mac, `Ghostty`/`Terminal` → `terminal`; else `unknown`. On natedev, `ghostty` and `zed` take `desktop` from the snapshot: run `agent-sessions-snapshot` (skip when missing), then read `~/rust/hanadocs/agent sessions.md` with the restore script's grammar (`## desktop: <name>`, the session line, the fenced `claude --resume <id>`), matching on the session id.
- Checkout: `git -C <cwd>` `rev-parse --show-toplevel`, `branch --show-current`, `rev-parse HEAD`, `rev-list --count @{u}..HEAD` (failure → `None`), `status --porcelain`.
- Run folders and servers: `SHUTDOWN_DELEGATE_ROOT` (default `/tmp/claude/delegate`) `/active/<session id>` first line; a folder with `mesh_server.json` whose pid is alive is a server; `busy_seats` from `mesh_roster.json`.
- Timers: every `NOTIFIER_STATE_DIR/*/conf` with `TARGET=session:<id>`.

`scripts/shutdown/remote.py`:

```python
def other_machine() -> str            # "mac" on Linux, "natedev" on darwin
def run_remote(args: list[str], stdin: str = "", timeout: float = 120) -> tuple[int, str]
```

`run_remote` runs `ssh -o BatchMode=yes -o ConnectTimeout=10 <host> '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/shutdown/shutdown.py" <args>; printf "rc=%s\n" $?'`, parses the last `rc=` line as the status and returns the output above it; no `rc=` line, ssh's 255 or a timeout → status 255 ("unreachable").

`scripts/shutdown/shutdown.py` (CLI; later phases add verbs):
- `status [account] [--json] [--here]` — `account` is a note label (`claude 2`), a login, or absent for this process's Claude account (`account.py`). Prints the label and login on the first line, then per machine (this one, then the other through `run_remote` with `--here --json`): each session as `<kind> <name> · <host> · <status> · <branch> ahead N, M dirty`, Codex servers per session, timers, and the `unknown` list. An unreachable machine is one line, `mac: unreachable`, not a failure. Phase 3 adds the shutdown state to this output.
- `--here` limits any verb to this machine; the cross-machine call always passes it.

`commands/shutdown.md` — new, `description:` "Shut down every Claude session of one account on natedev and the Mac safely and restart them later; show what runs on an account." This phase documents only `/shutdown status [account]`; later phases add the other verbs.

`pyrightconfig.json` — an `executionEnvironments` entry for `scripts/shutdown` with `extraPaths` `scripts/whoami`, `scripts/message`, `scripts/production`, as the other script directories have.

**Files:**
- `scripts/shutdown/inventory.py`, `scripts/shutdown/remote.py`, `scripts/shutdown/shutdown.py` — new.
- `commands/shutdown.md` — new.
- `pyrightconfig.json` — one entry.
- `scripts/shutdown/test_inventory.py`, `scripts/shutdown/test_remote.py` — new.

**Seats:** `1 writer + 1 tester` — one module chain, so nothing splits; the types and rules above are enough to test against.
- `impl` — `scripts/shutdown/inventory.py`, `scripts/shutdown/remote.py`, `scripts/shutdown/shutdown.py`, `commands/shutdown.md`, `pyrightconfig.json`
- `test` — `scripts/shutdown/test_inventory.py`: fake sessions dir with two accounts (one session per account; the other account's session is never listed), an unreadable environment going to `unknown`, a showrunner from a fake notifier instance, a unit from `fake_tmux.py` marks, a seat matched to its owner through a `seats` ledger, a Codex server with a busy seat, a checkout ahead 2 with one dirty file, a snapshot file giving a desktop; `scripts/shutdown/test_remote.py`: stand-in `ssh` printing `rc=3` → 3, no `rc=` → 255

**Constraints from prior phases:** Phase 1 built `scripts/whoami/account.py`: `account_of(pid) -> Account | None`, `claude_account(config_dir)`, `label_for(tool, login)`, `Account(tool, login, label)`; notes dir from `AGENT_NOTES_DIR`. On the Mac, `process_config_dir` runs `ps eww -o uid=,command= -p <pid>` and returns `None` for another uid or when no `HOME=` token shows (macOS hides the environment of platform binaries such as `/bin/zsh` and still exits 0); Claude's own binary shows its environment; the last `CLAUDE_CONFIG_DIR=` token wins; `parse_darwin_config_dir(output, expected_uid)` is the pure parser. The Mac's own `~/.claude` lacks Phase 1 until the production promotes, so a live Mac check before then runs from a temp copy of the whole `~/.claude/scripts` tree (the whoami imports reach `production/` by relative import). `settings.json` changes go in their own commit, and the checkpoint notice names each key.

**Acceptance gate:** `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py'` green; `basedpyright scripts/shutdown` clean; live: `shutdown.py status` on natedev lists the running showrunners, unit directors and top-level sessions with correct kinds and hosts and the Mac section (empty or listed), and changes nothing (notifier `ENABLED` values and session records identical before and after).

### Phase 3 — Shutdown begins: timers stop and every session settles  · status: todo

#### Work Order

**Goal:** `/shutdown [account]` records the set on each machine, stops its timers, asks every session to reach a clean, pushed state, collects each one's "where I am" line, tells the user who holds out, and `/shutdown cancel` puts everything back.

**Spec:**

Record — `scripts/shutdown/record.py`. One per account per machine: `SHUTDOWN_STATE_DIR` (default `~/.local/state/shutdown`) `/<login>/record.json`, written atomically under `flock` on `<login>/lock`:

```python
class TimerState(TypedDict):
    instance: str; enabled: bool          # ENABLED before the shutdown (after a released pause: True)
    footer: NotRequired[str]              # a released pause's showrunner footer slug

class Entry(TypedDict):
    session: Session                      # from inventory, as found
    timers: list[TimerState]
    where: str | None                     # the session's own line, from `ready`
    ready_at: str | None
    stopped_at: str | None                # Phase 4
    restarted_at: str | None              # Phase 5

class Record(TypedDict):
    login: str; label: str; machine: str
    state: Literal["settling", "stopping", "down", "restarting", "partial", "cancelled", "up"]
    requested_at: str; requested_by: str | None    # the session id that ran /shutdown
    entries: list[Entry]
```

A record in `cancelled` or `up` is moved to `<login>/history/<requested_at>.json`; at most one live record per account per machine, so a second `/shutdown` while one runs prints its state and stops.

Settle — `scripts/shutdown/settle.py`, with the `shutdown.py` verbs:
- `down [account] [--here]` — the user-facing start. In order: refuse when `github-warm-status` exits non-zero ("GitHub keys are cold: run `github-warmup` in a terminal on <machine>, then `/shutdown` again"; the user's CLAUDE.md, cold gpg-agent); print the `status` of both machines; then on each machine run `begin` (this one directly, the other through `run_remote`); then start the conductor detached (`launch_run`'s pattern: `systemd-run --user --collect --quiet --no-block --unit shutdown-<label slug>-<pid>` on Linux, `launchctl submit -l …` on darwin) running `shutdown.py conduct <login>`; print `shutdown of claude 2 started; /shutdown status to watch, /shutdown cancel to undo`.
- `begin <login> [--requested-by SID] [--only IDS]` (one machine) — take the inventory, write the record (`settling`), then for each entry: (1) release its conversation pause: new `conversation_pause.release(session_id) -> PauseRecord | NoPauseRecord`, which deletes the record under `record_lock()` without resuming anything and returns it; its instances are recorded `enabled: True` unless its phase is `KeptOff`, and its footers recorded; (2) `notifier.sh stop` every instance in `session.timers` that is enabled, recording the prior state. `begin` sends no messages.
- `conduct <login>` (detached, on the machine that ran `down`) — loop every 15 s until every entry on both machines is ready or the user cancels: re-run inventory on each machine and add any new session of the account as an entry (timers stopped as in `begin`); send each entry its settle message once, in this order: unit directors and top-level sessions at once; a showrunner only after every unit of its production is ready; the requesting session gets none. Messages go through `send.py --to session:<id> --from shutdown --summary "Shutdown of <label>: reach a safe stop"` (on the other machine with `--machine`).
- Settle messages (the text carries the whole instruction):
  - unit director: "Shutdown of account <label> requested by the user with /shutdown (<time PDT>). Reach a safe stop: let any seat turn already running finish, dispatch nothing new, commit nothing new, push your branch if it has unpushed commits, and leave uncommitted work in place. Then run `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py ready --where "<phase N: the step you finished and the step that comes next>"` and end your turn with `— blocked: shutdown requested by the user`. You will be resumed with that line."
  - showrunner: the same opening; then "Merge any checkpoint already sent to you, push the merge branch and main as your production rules say, append a `### STATE` block to your LOG, then run `… ready --where "<one line>"` and end your turn."
  - top-level: "Finish the turn you are in, start nothing new, push any commits you made that are ahead of their upstream, then run `… ready --where "<what you were doing and what comes next>"` and end your turn."
- `ready --where TEXT` (run by a session, identified by `CLAUDE_CODE_SESSION_ID`) — finds the live record on this machine whose entries include the session, sets `where` and `ready_at`. Refuses, with the reason on stderr and exit 2, when its checkout is `ahead > 0` ("push <branch> first") or a Codex server in its run folders still has a busy seat; with no record naming the session it prints `no shutdown in progress for this session` and exits 1.
- Holdouts: 20 minutes after `begin`, and every 60 minutes after, `conduct` sends one alert for the entries not yet ready: `send.py --to user --need decision --summary "Shutdown of <label>: N not ready"` with one line per holdout (`<machine> <kind> <name>: <status>`, plus `busy`, `showing a form` from the pane for tmux hosts, or `ahead N on <branch>`) and the two choices: `/shutdown now` stops them anyway, `/shutdown cancel` undoes the shutdown; the same text also goes to the requesting session.
- `cancel [account]` — on each machine: restore every recorded timer (`notifier.sh start` for `enabled: True`; footers back on with `showrunner_footer.set_footer_state(slug, FooterState.ON)`), send each entry that got a settle message "Shutdown of <label> cancelled by the user: continue where you were.", set `cancelled`, move the record to history, and stop the conductor (`systemctl --user stop <unit>` / `launchctl remove <label>`; the unit name is in the record).
- `status` adds per machine the record state, and per entry `ready (<where>)` or `waiting`.

`commands/shutdown.md` — documents `/shutdown [account]` (run `shutdown.py down`, then `shutdown.py ready --where "<what this session was doing before /shutdown>"`, report its output, end the turn: this session is stopped last), `/shutdown status`, `/shutdown cancel`. It names the account on its first line and says that a session on another account is never touched.

`commands/message.md` — Receiving gets one bullet: a message from sender `shutdown` is the user's `/shutdown`; follow it.

**Files:**
- `scripts/shutdown/record.py`, `scripts/shutdown/settle.py` — new.
- `scripts/shutdown/shutdown.py` — verbs `down`, `begin`, `conduct`, `ready`, `cancel`; `status` shows record state.
- `scripts/hooks/conversation_pause.py` — `release(session_id)` (also touches; as-built owner enh-showrunner-unit).
- `commands/shutdown.md`, `commands/message.md`
- `scripts/shutdown/test_record.py`, `scripts/shutdown/test_settle.py` — new; `scripts/hooks/test_conversation_pause.py` — `release` cases.

**Seats:** `1 writer + 1 tester` — the verbs share the record module, so one writer holds them; the record types and message rules are enough to test against.
- `impl` — `scripts/shutdown/record.py`, `scripts/shutdown/settle.py`, `scripts/shutdown/shutdown.py`, `scripts/hooks/conversation_pause.py`, `commands/shutdown.md`, `commands/message.md`
- `test` — `scripts/shutdown/test_record.py` (one live record per account, history move, lock); `scripts/shutdown/test_settle.py` (fake notifier dir: only the set's instances stop and an other-account session's instance stays enabled; a released `KeptOff` pause stays off at cancel; a showrunner is messaged only after its units are ready; `ready` refuses when ahead; holdout alert at 20 minutes with a fake clock; `cancel` restores exactly the recorded states); `scripts/hooks/test_conversation_pause.py` (`release` returns the record and a later tick resumes nothing)

**Constraints from prior phases:** Phase 1: `account.py` resolves labels and logins. Phase 2: `inventory(login, only) -> Inventory` with `Session` (kind, host, checkout, run_dirs, codex_servers, timers, owner), `remote.run_remote(args)` with the `rc=` rule, `shutdown.py status [account] [--json] [--here]`, `commands/shutdown.md`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py'` and `python3 -m unittest scripts/hooks/test_conversation_pause.py` green; `basedpyright scripts/shutdown scripts/hooks/conversation_pause.py` clean; live on natedev, limited to two scratch sessions started for the check (a tmux-hosted `claude --model haiku` and a second one), through `--only`: `down`, both reply `ready` with their `where`, a notifier instance created for one of them goes from enabled to stopped, `status` shows both ready, `cancel` re-enables it and messages both; no other session or instance changes.

### Phase 4 — Shutdown stops every session, seat server and window  · status: todo

#### Work Order

**Goal:** once every session of the account is ready, or the user says `/shutdown now`, the conductor stops the Codex servers, the unit directors, the showrunners, the other sessions and the requesting session last, on both machines, and tells the user the account is down and how to restart it.

**Spec:**

`scripts/shutdown/stop.py`, verbs `stop <login> [--now]` (one machine) and `now [account]` (user-facing: sets `now` in the record on each machine; the conductor then stops every entry, busy or not):
- Order on each machine: units, then showrunners, then top-level sessions, then the requesting session. Per entry, refuse unless `ready_at` is set and the record's `status` is `idle`, except under `now`.
- Stop a session: `SIGTERM` its pid; wait up to 10 s for the pid to exit and its `~/.claude/sessions/<pid>.json` to go; then `SIGTERM` again and wait 10 s more; still alive → the entry is listed `failed to stop` and the conductor alerts the user (never `SIGKILL`). Under `now`, a busy unit's busy Codex seats are first ended with `codex_mesh.py end --session-dir D --to <seat>`.
- After a unit's session exits: `codex_mesh.py stop --session-dir D` for each run folder in the entry, then `tmux kill-session -t <its tmux session>` (the `exec zsh` shell left in the pane).
- A `ghostty`-hosted session on natedev: after the exit, close its window by sending `SIGHUP` to the session's parent shell when that shell is the Ghostty window's own child (the ancestry recorded by inventory); otherwise leave the window. `zed`, `terminal`, `unknown`: leave the host as it is.
- Seats: nothing (Invariants: seats hold no process between turns). Mark each seat entry stopped once its owner is stopped.
- Each stop sets the entry's `stopped_at`; when all are stopped the record goes `down`; any failure leaves `partial` with the failed entries.
- `conduct` runs `stop` on each machine (here directly, there through `run_remote`) once every entry on both is ready or `now` is set, the requesting session's machine last; then sends `send.py --to user --need note --summary "<label> is down"` with per machine the counts by kind, any failure, and the restart line: `/shutdown restart` in any Claude session on <label>, or in a terminal `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py restart`. On the Mac the alert goes through `--machine natedev` (Pushover keys are on natedev).
- `scripts/delegate/remove_seats.py` `live_runs`: a run folder named by `active/<id>` also counts as live when `<id>` is an entry of any record under `SHUTDOWN_STATE_DIR` whose state is `settling`, `stopping`, `down` or `partial`, so another run's start never removes a shut-down unit's seats.
- `commands/shutdown.md` — documents `/shutdown now` and what `down` means for each kind.

**Files:**
- `scripts/shutdown/stop.py` — new.
- `scripts/shutdown/settle.py`, `scripts/shutdown/shutdown.py` — `conduct` stops; verbs `stop`, `now`.
- `scripts/delegate/remove_seats.py` — the shutdown rule (also touches; owner followups-unit, run done).
- `commands/shutdown.md`
- `scripts/shutdown/test_stop.py` — new; `scripts/delegate/test_remove_seats.py` — the shutdown case.

**Seats:** `1 writer + 1 tester` — the stop order lives in one module and the conductor; the tester works from the order and refusal rules above.
- `impl` — `scripts/shutdown/stop.py`, `scripts/shutdown/settle.py`, `scripts/shutdown/shutdown.py`, `scripts/delegate/remove_seats.py`, `commands/shutdown.md`
- `test` — `scripts/shutdown/test_stop.py` (injected kill and liveness: order units → showrunners → top-level → requester; a busy entry is refused without `now` and stopped with it; a pid that survives two SIGTERMs is `failed to stop` and never SIGKILLed; `codex_mesh.py stop` called once per run folder after its unit; the Mac alert routes through `--machine natedev`); `scripts/delegate/test_remove_seats.py` (a run whose director is in a `down` record is live)

**Constraints from prior phases:** Phase 2: `Session.host` (`unit` with `tmux_session` from the marks, `ghostty` with the parent chain), `run_dirs`, `codex_servers`. Phase 3: `record.py` (`Record`, `Entry` with `ready_at`, `stopped_at`; states; history), `settle.py` `conduct` loop, `begin`, `ready`, `cancel`, the settle messages and the holdout alert, `conversation_pause.release`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py'` and `python3 -m unittest scripts/delegate/test_remove_seats.py` green; `basedpyright scripts/shutdown scripts/delegate/remove_seats.py` clean; live, through `--only` on scratch sessions only: on natedev a tmux-hosted session and a Ghostty-hosted session (opened for the check on the current desktop), on the Mac a session in a nix tmux (`nix run nixpkgs#tmux`) — each settles, stops, leaves `~/.claude/sessions/`, its tmux session or window closes, the record says `down` on both machines, and the alert arrives; no other session changes.

### Phase 5 — Restart resumes every session where it was  · status: todo

#### Work Order

**Goal:** `/shutdown restart [account]` brings back every stopped session of the account on both machines, in its host and on its desktop, tells each one where it was, and restarts its timers.

**Spec:**

`scripts/shutdown/restart.py`, verbs `restart [account] [--dry-run]` (user-facing; account defaults to this process's account, else the only down record on either machine, else it lists the down records and stops) and `up <login> [--dry-run]` (one machine). The record goes `restarting`; per entry, in order showrunners, units, top-level, the requester; an entry whose session id is already live is skipped and marked restarted.

- Note, given to each session as its first prompt: "Restarted after /shutdown of <label> (stopped <time PDT>, restarted <time PDT>). Before it you wrote: <where>. First run `~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py pending` for messages kept while you were down, then continue from there; if you were waiting on the user, keep waiting."
- Unit (`host.kind == "unit"`): `add_unit.py --production <doc> <unit name without -unit> --plan <plan> --resume <id> --cwd <cwd> --session-name <recorded name> --restart-note <file>`. Add both flags to `add_unit.py` (allowed only with `--resume`): `--restart-note` makes `prompt_for` return the identity sentence of the resumed prompt followed by the note instead of `Run /unit:direct <plan>.`; `--session-name` replaces the launch name in `-n`, `--remote-control`, the tmux session name and the scope. Also in `launch_session`: the scope gets a unique name, `--unit=<name>-<epoch seconds>`, because a tmux server started by an earlier launch can still hold `<name>.scope` and the relaunch would fail.
- Showrunner (`ghostty` on natedev): the Ghostty launch below with the prompt `/showrunner:produce <doc> resume`; once its session is live, the note goes through `send.py --to session:<id> --from shutdown`.
- `ghostty` and `zed` on natedev: `kdotool set_desktop <n>` for the recorded desktop (numbers from `~/.config/kwinrc`, as the restore script; a missing desktop → the current one), then `systemd-run --user --collect --quiet -- ghostty -e zsh -ic 'cd <cwd> && ENABLE_TOOL_SEARCH=true command claude --resume <id> -n <name> --remote-control <name> [--model <model>] --settings '"'"'{"disableAgentView": true}'"'"' <note>; exec zsh'` (through `systemd-run`, so it works from ssh), 1.2 s between windows, then back to the starting desktop. A Zed-hosted session comes back in Ghostty on its desktop, since nothing outside Zed can type into Zed's terminal (my call; a one-line revert prints the command instead).
- `terminal` on the Mac: `open -na "/Applications/Nix Apps/Ghostty.app" --args -e zsh -ic '<the same command>; exec zsh'`.
- `tmux` (not a unit): `tmux new-session -d -s <tmux session> -c <cwd> zsh -ic '<the same command>; exec zsh'` (on the Mac, the nix tmux).
- `unknown`: print the command for the user and mark the entry `manual`.
- Wait up to 90 s for each session's record to be live (`sessions.live_sessions()` holds its id); then `notifier.sh start` each timer recorded `enabled: True` and turn recorded footers back on. A session that did not come back → `partial`, listed in the output and the alert; running `restart` again retries only those.
- All back → record `up`, moved to history; one alert `send.py --to user --need note --summary "<label> is back"` with the counts per machine and any manual lines.
- `--dry-run` prints every command and changes nothing.
- `commands/shutdown.md` — documents `/shutdown restart [account]` and the terminal line.

**Files:**
- `scripts/shutdown/restart.py` — new.
- `scripts/shutdown/shutdown.py` — verbs `restart`, `up`.
- `scripts/production/add_unit.py` — `--restart-note`, `--session-name`, the unique scope name (also touches; as-built owner enh-showrunner-unit).
- `commands/shutdown.md`
- `scripts/shutdown/test_restart.py` — new; `scripts/production/test_add_unit.py` — the new flags and scope name.

**Seats:** `1 writer + 1 tester` — one restart module plus one launcher change; the commands above are fixed enough to assert on.
- `impl` — `scripts/shutdown/restart.py`, `scripts/shutdown/shutdown.py`, `scripts/production/add_unit.py`, `commands/shutdown.md`
- `test` — `scripts/shutdown/test_restart.py` (`--dry-run` argv per host kind on both platforms; an already-live session is skipped; timers start only after the session is live and only those recorded enabled; a session that never comes back leaves `partial` and a second run retries only it); `scripts/production/test_add_unit.py` (`--restart-note` prompt, `--session-name` in `-n`, `--remote-control`, tmux and scope, scope name unique, both refused without `--resume`)

**Constraints from prior phases:** Phase 2: hosts and their fields (`unit`: `production`, `unit`, `doc`, `plan`, `tmux_session`; `ghostty`/`zed`: `desktop`; `tmux`: `tmux_session`; `terminal`; `unknown`), `Session.model`. Phase 3: `Record`/`Entry`/`TimerState` (`enabled`, `footer`), `where`, the history move. Phase 4: entries carry `stopped_at`; states `down` and `partial`; the down alert names `/shutdown restart` and the terminal line.

**Acceptance gate:** `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py'` and `python3 -m unittest scripts/production/test_add_unit.py` green; `basedpyright scripts/shutdown scripts/production/add_unit.py` clean; live, continuing Phase 4's scratch shutdown: `restart --dry-run` prints one command per session, then `restart` brings back the natedev tmux session, the Ghostty session on its desktop and the Mac tmux session; each answers what it was doing before the shutdown; the scratch timer is enabled again; the record is in history; no other session changes.

### Phase 6 — Nothing new starts on a down account  · status: todo

#### Work Order

**Goal:** while an account is down on a machine, the timed jobs that start Claude work there skip their run and say why, and a unit cannot be launched on it except by restart.

**Spec:**
- `shutdown.py is-down [account]` — exits 0 and prints `down since <time PDT>` when this machine has a record for the account in `settling`, `stopping`, `down` or `partial`; else exits 1 silently. The account defaults to this process's.
- `scripts/nightly_review/nightly_review.py` `launch`: a `down_block()` beside `quota_block()` returns `"<label> is shut down (since <time>)"` from `is-down`; a reason becomes the existing `skipped:` line.
- `scripts/fix/fix-trigger.sh`: after the `pgrep` guard, `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/shutdown/shutdown.py" is-down >/dev/null 2>&1 && exit 0`.
- `scripts/production/add_unit.py` `main`: refuse (exit 2, `add_unit: <label> is shut down on this machine; /shutdown restart first`) when `is-down` exits 0, unless `--restart-note` is given.
- The timers themselves keep running (the user: "system services keep running"); a run already in progress when the shutdown starts is left to finish (my call: these are services, and they end on their own).
- `commands/shutdown.md` — one sentence: while down, the nightly review and the fix pipeline skip their runs on that machine.

**Files:**
- `scripts/shutdown/shutdown.py` — verb `is-down`.
- `scripts/nightly_review/nightly_review.py` — `down_block` (also touches; no owner).
- `scripts/fix/fix-trigger.sh` — one line (also touches; no owner).
- `scripts/production/add_unit.py` — the refusal (also touches; as-built owner enh-showrunner-unit).
- `commands/shutdown.md`
- `scripts/shutdown/test_shutdown_cli.py` — new (`is-down`); `scripts/nightly_review/test_nightly_review.py`, `scripts/production/test_add_unit.py` — the skip and the refusal.

**Seats:** `1 writer + 1 tester` — four one-place checks; the tester writes each case from the rules above.
- `impl` — `scripts/shutdown/shutdown.py`, `scripts/nightly_review/nightly_review.py`, `scripts/fix/fix-trigger.sh`, `scripts/production/add_unit.py`, `commands/shutdown.md`
- `test` — `scripts/shutdown/test_shutdown_cli.py`, `scripts/nightly_review/test_nightly_review.py` (a down record gives `skipped: claude 2 is shut down …`), `scripts/production/test_add_unit.py` (refused while down, allowed with `--restart-note`)

**Constraints from prior phases:** Phase 3: record states and the per-account record path under `SHUTDOWN_STATE_DIR`. Phase 5: `add_unit.py --restart-note` marks a restart launch.

**Acceptance gate:** `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py'`, `python3 -m unittest scripts/nightly_review/test_nightly_review.py scripts/production/test_add_unit.py` green; `basedpyright scripts/shutdown scripts/nightly_review scripts/production/add_unit.py` clean; `bash -n scripts/fix/fix-trigger.sh`; live: with a scratch `down` record in a temp `SHUTDOWN_STATE_DIR`, `is-down` exits 0 and `fix-trigger.sh`'s guard exits before `fix.sh` (checked with `bash -x` and a stand-in `fix.sh`).
