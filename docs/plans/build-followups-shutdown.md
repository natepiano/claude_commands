# Shutdown and restart

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `/shutdown` brings every Claude session of one account on natedev and the Mac to a safe stop, stops its update timers and records where each one was. `/shutdown restart` resumes each session there and restarts its timers. Every session shows which account it runs on.

> **As-built disposition: create**

> **Production: build-followups** — unit `shutdown-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

2026-10-09 14:49 PDT

The user's words, 2026-10-08: "For later I want a command that fully shuts down Claude safely and fully restarts later."

The user chose, 2026-10-09 14:5x PDT, the scope "both machines: every showrunner, unit director, seat and Codex server on natedev and the Mac first reaches a clean, pushed state, its update timers stop, and where it was is recorded; restart resumes each session where it was and restarts its timers; CI runners and system services keep running", adding in his words: "yes both machines - but also be aware that this command should be account aware - in the future if we can have machines (on machine or on different machines) running different accounts - we need to only e shutting down the account we've been asked to shutdown (we need a way to surface the account we are, easily)"

The user's words, 2026-10-09 (via the showrunner), on the status line account field: "yes but can it be on a second line - a friend of mine showed his in multiple lines and i think that's a good approach - ideally it would also show the remaining % in the 5 hour and remaining % in the week as well". The same message gave the go to promote the `settings.json` status line change once it does this.

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
  - `scripts/whoami/agent_accounts.py` — `claude_config_dir` 224, `claude_config_file` 228-236, `codex_home` 239, `claude_on_disk` 318-367 (reads `oauthAccount`), `codex_email_on_disk` 373-407 (decodes the `id_token` email). Both read the current environment only.
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
  - `scripts/nightly_review/nightly_review.py` — `quota_block` 59-68 and `launch` 90-96 (a skip reason becomes the `skipped:` line). `scripts/fix/fix-trigger.sh` — the scheduled entry to the fix pipeline; its `pgrep` guard (28-30) exits 0.
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

### Phase 2 — What runs on an account  · status: done

#### As-built

`shutdown.py status [account] [--json] [--here]` lists, for one Claude account on natedev and the Mac, every session with its kind, host, status, checkout, Codex servers and timers, plus the sessions it cannot attribute, and changes nothing.

- **Types** (`inventory.py`): `Host` (`kind`: unit | tmux | ghostty | zed | terminal | unknown; `NotRequired` `production`, `unit`, `doc`, `plan`, `tmux_session`, `desktop`, `window_shell: int`), `Checkout` (`path`, `branch`, `head`, `ahead: int | None`, `dirty`), `CodexServer` (`run_dir`, `pid`, `busy_seats`), `Session` (`session_id`, `pid`, `name`, `cwd`, `kind`: showrunner | unit | seat | top-level, `status`, `host`, `model: str | None`, `checkout: Checkout | None`, `run_dirs`, `codex_servers`, `timers`, `owner: str | None`), `Inventory` (`machine`, `login`, `label`, `sessions`, `unknown: list[str]` of `"<pid> <name>"`).
- **`inventory(login: str, only: frozenset[str] = frozenset()) -> Inventory`** keeps the `sessions.live_sessions()` whose `account.account_of(pid)` login matches case-insensitively. An unreadable account, or a record `procStart` unequal to the live process start (pid reuse), goes to `unknown`. A non-empty `only` keeps those session ids and their seats.
- **Kind:** showrunner from `showrunners.registered_showrunners()` `TARGET`, the only source of productions; unit from `unit_lookup.marked_units` marks on its tmux pane; seat when the record's `kind` is `bg`, owned through any `active/<director id>` run folder's `seats` ledger, so a seat keeps its owner after its director exits; else top-level.
- **Host:** unit from the marks (`tmux_session` from the live marked session, the record's `tmux` as fallback; `plan` from the newest run record whose `main_agent.session_id` matches); tmux from `TMUX_PANE` without marks; else a parent walk to ghostty, zed or, on the Mac, Ghostty/Terminal as terminal; else unknown. `window_shell` is the ancestor directly beneath the terminal process: the shell Ghostty started for the window. On natedev, ghostty and zed take `desktop` from the `agent-sessions-snapshot` file `~/rust/hanadocs/agent sessions.md`.
- **Servers and timers:** run folders from `SHUTDOWN_DELEGATE_ROOT` (default `/tmp/claude/delegate`) `/active/<session id>`; a live `mesh_server.json` pid makes a server; a `mesh_roster.json` seat in `running`/`starting` is busy with no `launcher_pid` key or a live launcher; timers are `NOTIFIER_STATE_DIR/*/conf` files with `TARGET=session:<id>`.
- **Remote:** `run_remote(args: list[str], stdin: str = "", timeout: float = 120) -> tuple[int, str]` runs `shutdown.py` on `other_machine()` over `ssh -o BatchMode=yes` and takes the trailing `rc=` line as status; no `rc=` line, ssh 255 or a timeout is 255.
- **CLI:** `_requested_account(text) -> Account | None` takes a note label whose stem starts with `claude` (case-folded), any text with `@` as a login, absent as this process's account, else `UnknownAccount` (exit 2). `status` prints label and login, then each machine's sessions as `<kind> <name> · <host> · <status> · <branch> ahead N, M dirty` with Codex servers and timers, `unknown account · <pid> <name>`, or `no sessions on <label>`. The peer is called with `--here --json` and its output checked recursively by `_remote_inventory(text) -> Inventory` (`InvalidInventoryResponse`). Exit 255 prints `<machine>: unreachable`; any other non-zero rc or invalid output prints `<machine>: unavailable (rc N)`; neither fails the local report. `--json` without `--here` prints a list: an `Inventory`, `{"machine", "unreachable": true}`, or `{"machine", "status": "unavailable", "rc", "reason"}`.

**Files:**
- `scripts/shutdown/inventory.py`, `remote.py`, `shutdown.py` — inventory, ssh call, CLI.
- `commands/shutdown.md` — `/shutdown status [account]` only, never with `--here`.
- `pyrightconfig.json` — `scripts/shutdown` environment, `extraPaths` `scripts/shutdown`, `scripts/whoami`, `scripts/message`, `scripts/production`.
- `scripts/shutdown/test_inventory.py`, `test_remote.py`, `test_shutdown_status.py`.

**Binds later work:** `unknown` sessions never enter `sessions`, so a verb acting on `sessions` leaves them alone by construction. `Host.window_shell` is the SIGHUP target for a Ghostty window close and `Host.tmux_session` the unit's tmux session to kill in "Shutdown stops every session, seat server and window". `_remote_inventory` and `_requested_account` are private to `shutdown.py`, which the settle, stop and restart modules cannot import without a cycle, and the `NotRequired` host fields and bare `None` fields break the type contract; "Shutdown's shared types, account names and record store" replaces both.

**Gotchas:** NixOS Ghostty's process comm is `.ghostty-wrappe` (the nix wrapper name cut to 15 characters); terminal matching strips a leading dot and accepts any prefix of `-wrapped`. `procStart` is Linux `/proc/<pid>/stat` field 22 and darwin `LC_ALL=C TZ=UTC ps -o lstart= -p <pid>`, the form Claude 2.1.296 writes; another locale or zone never matches. The darwin process-start match and Mac terminal walk have never run live; the first Mac live check must confirm its session is listed, not `unknown`.

**Ruled out:** treating a reachable peer's failure as unreachable (it is `unavailable` with rc and reason); trusting a peer inventory without a full recursive check; counting a Codex seat with a dead launcher as busy.

### Phase 3 — The account and its remaining quota on a second status line  · status: done

#### As-built

- `scripts/statusline/statusline.jq` prints line 1 `<dirname> | <tokens with thousands separators> | <model [effort]>` and, when any part is present, line 2: the account (`$ARGS.named.account // ""`, when non-empty), `5-hour <N>% left`, `weekly <M>% left`, joined by ` | ` — e.g. `claude_commands | 182,340 | Opus 5.5 high` then `claude 2 | 5-hour 73% left | weekly 41% left`. With no part present the output is line 1 alone, no empty second line.
- `def remaining($window)` gives `100 - used_percentage` floored and clamped to 0–100, so the line never shows more room than there is; a null or missing window, or a non-number `used_percentage`, yields `empty` and drops that part with no empty separator.
- The numbers come from the status line JSON's `rate_limits.five_hour` and `rate_limits.seven_day` (`{used_percentage, resets_at}`); no file is read and no process added, so a refresh stays sh plus jq. The labels `5-hour` and `weekly` match `/whoami`'s; `settings.json`'s `statusLine.command` already passes `--arg account` from `account-label`.

**Files:**
- `scripts/statusline/statusline.jq` — the two-line status line and `def remaining($window)`; the header `Output:` describes both lines.
- `commands/whoami.md` — says the status line's second line shows the Claude account and its remaining 5-hour and weekly percentages.
- `scripts/whoami/test_account.py` — status line cases run through `jq -r --arg pwd … [--arg account …] -f scripts/statusline/statusline.jq` on JSON stdin.

**Gotchas:** `--arg account` stays optional — a `settings.json` without it still gets line 1 and both windows. `rate_limits` windows appear only for a subscription login, from the first reply on, never for an API key; Claude Code 2.1.296 sends integer percentages though the field allows one decimal, so `remaining` accepts any number.

### Phase 4 — Shutdown's shared types, account names and record store  · status: done

#### As-built

`inventory.py` states every inventory case as a tagged `TypedDict` variant, every field required and no bare `None`; `account.py` holds the account-name rules; `record.py` keeps one live shutdown record per account per machine and imports only `Session` and `parse_session` from `inventory.py`, so the settle, stop and restart modules import all three without a cycle. `status` lists the same sessions as before, read from the new variants.

- **Inventory types** (`inventory.py`): `Host` = `UnitHost{production, unit, doc, tmux_session, plan}` (`UnitPlan` = `plan{path}` | `no run record`) | `TmuxHost{tmux_session}` | `WindowHost{kind: ghostty | zed, window_shell: int, desktop}` (`Desktop` = `named{name}` | `not in snapshot`) | `TerminalHost` | `UnknownHost`; `SessionHost` is every host but `UnitHost`. `CheckoutState` = `git{path, head, upstream, dirty}` | `not a checkout`, with `Head` = `branch{name}` | `detached{commit}` and `Upstream` = `tracking{ahead}` | `no upstream`. `LastModel` = `model{name}` | `no reply yet`; `SeatOwner` = `director{session_id}` | `no director`. `SessionFields` = `session_id, pid, proc_start, name, cwd, status, model, checkout, run_dirs, codex_servers, timers`; `Session` = `ShowrunnerSession` (`host: SessionHost`, `production`, `doc`) | `UnitSession` (`host: UnitHost`) | `SeatSession` (`host: SessionHost`, `owner: SeatOwner`) | `TopLevelSession` (`host: SessionHost`). `Inventory{machine, login, label, sessions, unattributed: list[UnattributedSession{pid, name, reason}]}`, reason `account unreadable` | `process start mismatch`. `inventory(login: str, only: frozenset[str] = frozenset()) -> Inventory` keeps its signature; the private marks dataclass is `UnitMarks`.
- **Conversions:** a `TMUX_PANE` whose session name `tmux display` cannot read is `UnknownHost`; the terminal walk starts at the session's own pid, so a `ghostty` or `zed` host always has `window_shell`; `rev-parse --show-toplevel`, `branch --show-current` or `rev-parse HEAD` failing is `not a checkout`, an empty branch is `detached` with the `HEAD` commit, a failing `rev-list --count @{u}..HEAD` is `no upstream`; a seat that no `seats` ledger lists is `no director`.
- **Decoders:** `parse_inventory(text: str) -> Inventory` and `parse_session(value: object, place: str) -> Session` check every `kind` against its union's literals and every variant field with its JSON type, raising `InvalidInventory` (a `ValueError`) whose message names the field path, e.g. `inventory.sessions[0].host.kind is invalid`. `shutdown.py` holds no decoder; an `unavailable` report's `reason` is the `InvalidInventory` message.
- **Account names** (`account.py`): `own_claude_account() -> Account` raises `UnreadableAccount` ("this process's Claude account is unreadable"). `named_claude_account(text: str) -> Account` returns the note whose stem, case-folded, starts with `claude` and equals the text and whose `login:` is a non-empty string; else text holding `@` and no `/`, NUL or whitespace, not starting with `.`, is a login labelled by `label_for`; else it raises `UnknownAccountName` (a `ValueError`: "unknown account <text>: give a note label such as claude 2, or a login"). Every `[account]` verb resolves absent → `own_claude_account()`, given → `named_claude_account(text)`; either exception prints `shutdown: <message>` on stderr and exits 2.
- **Record types** (`record.py`): `Record{login, label, machine, state: RecordState, requested_at, requested_by: RequestOrigin, scope: Scope, conductor: Conductor, force: Force, entries: list[Entry]}`; `Entry{session: Session, timers: list[TimerRestore], settle_message: SettleMessage, where: Where, progress: Progress}`; `TimerRestore{instance, was_enabled, footer}`, footer `footer{slug}` | `no footer`; `Scope` = `all account sessions` | `selected{session_ids}`; `RequestOrigin` = `session{session_id}` | `terminal`; `Conductor` = `systemd{unit}` | `launchd{label}` | `not started`; `Force` = `"wait for ready" | "now"`; `SettleMessage` = `not sent` | `sent{at}`; `Where` = `said{text, at}` | `not said`; `Progress` = `waiting` | `ready{at}` | `passive seat ready{at}` | `stopped{at}` | `already gone{at}` | `process identity lost{at}` | `stop failed{at, reason}` | `restarted{at}` | `restart failed{at, reason}` | `manual restart{command}`; `RecordState` = `settling`, `stopping`, `down`, `stop partial`, `restarting`, `restart partial`, `cancelled`, `up`.
- **Store:** the live record is `SHUTDOWN_STATE_DIR` (default `~/.local/state/shutdown`) `/<login case-folded>/record.json`; every read and write holds `fcntl.flock` on `<login>/lock`, and a write goes through a temp file in the same directory, `fsync` and `os.replace`. `find_live(login) -> LiveRecord | NoShutdown` (`live{record}` | `no shutdown`); `create(record) -> None` raises `ShutdownInProgress`, whose `.live` is the record already there; `update(login, change: Callable[[Record], None]) -> Record` runs `change` under the lock, revalidates, writes and returns the record, raising `NoLiveRecord` with none live; `archive(login) -> None` moves the record to `<login>/history/<requested_at>.json` and raises `ValueError` unless its state is `cancelled` or `up`; `live_records() -> list[Record]` sorted by login; `parse_records(text) -> list[Record]` checks every tagged field and each entry's `session` through `inventory.parse_session`. Every read raises `InvalidRecord` (a `ValueError`) naming the file or field path.
- **CLI** (`shutdown.py`): `status` prints hosts `unit <production>/<unit>`, `tmux <name>`, `<ghostty|zed> on <desktop>` (the bare kind when not in snapshot), `terminal`, `unknown`; a checkout as `<branch, or the commit's first 12 characters> ahead N` or `no upstream`, then `, M dirty`, or `not a checkout`; each unattributed session as `  unknown account · <pid> <name> · <reason>`. The internal verb `records [--json] [--here]` prints `<machine> <label> <state> since <requested_at in America/Los_Angeles>` per live record and nothing when there is none; `--json` prints a list of records; without `--here` it adds the other machine's `records --json --here` through `run_remote`, decoded with `parse_records`, or that machine's `unreachable` / `unavailable` report as `status` does. A local `InvalidRecord` prints `shutdown: <message>` on stderr and exits 3. `commands/shutdown.md` does not name `records`.

**Files:**
- `scripts/shutdown/inventory.py` — the tagged wire types, `parse_inventory`, `parse_session`.
- `scripts/shutdown/record.py` — the record types and the per-account store.
- `scripts/shutdown/shutdown.py` — `status` on the new types and the `records` verb.
- `scripts/whoami/account.py` — `own_claude_account`, `named_claude_account`, `UnreadableAccount`, `UnknownAccountName`.
- `scripts/shutdown/test_inventory.py`, `test_record.py`, `test_shutdown_status.py`, `scripts/whoami/test_account.py`.

**Binds later work:** the store refuses a login that is empty, holds `/` or NUL, or starts with `.`: `ValueError` from `find_live`, `update` and `archive`, `InvalidRecord` from `create` and when parsed, `UnknownAccountName` from `named_claude_account`; "Nothing new starts on a down account" calls `find_live` and meets that `ValueError`. `update` raises `ValueError`, writing nothing, when its `change` alters `login` or `machine`. Every `requested_at` and `at` must equal its own `datetime.isoformat()` and carry a zero UTC offset, so the settle, stop and restart modules write `datetime.now(timezone.utc).isoformat(timespec="seconds")` (`2026-10-09T21:49:10+00:00`); any other form fails validation. "Restart resumes every session where it was" reads the other machine's records through `records --json --here`. `records` stays out of `--help`.

**Gotchas:** `datetime.fromisoformat` accepts any one-character date-time separator, so a timestamp check needs the `isoformat()` round trip, not a parse alone. `argparse.SUPPRESS` as a subparser's `help` prints `==SUPPRESS==` instead of hiding it; the subparsers `metavar` (`{status}`) lists only public verbs, so a new public verb is added to it by hand.

**Ruled out:** `records` printing a `none` line when empty, and separating `--json` records from machine reports: an internal verb, and no consumer needs either.

### Phase 5 — Shutdown begins: timers stop and every session settles  · status: done

#### As-built

`settle.py` holds the settle half of a shutdown, exposed as `shutdown.py` verbs. Nothing is stopped yet: the conductor exits once every entry counts ready.

- **`down [account] [--here] [--only IDS]`** (`settle.down`): refuses, exit 1 and nothing changed, when `github-warm-status` fails ("GitHub keys are cold: run `github-warmup` in a terminal on <machine>, then `/shutdown` again"), when the other machine's `status <login> --json --here` (skipped under `--here`) fails or does not decode (`shutdown: <machine> is unreachable` | `unavailable (rc N)` `: nothing was shut down; /shutdown --here shuts down only this machine`), or when either machine has a live record (`shutdown: <machine> is already <state>`). It prints `Account: <label> (<login>)` and each machine's sessions, runs `begin` here, then there with the same `--requested-by` (`CLAUDE_CODE_SESSION_ID` when set) and `--only`, cancelling this machine when the remote `begin` fails (`shutdown: begin failed on <machine>: nothing was shut down`); then launches `shutdown.py conduct <login> [--here]` as `systemd-run --user --collect --quiet --no-block --unit shutdown-<label slug>-<pid>` (darwin: `launchctl submit -l` with the same name) and saves this machine's `conductor`; the other's stays `not started`. A failed launch cancels both machines.
- **Scope:** `--only` (help suppressed) is a comma list; none → `all account sessions`, else `selected{session_ids}`, stored in each record and applied by `run_inventory(login, scope)` to every later inventory.
- **`begin <login> [--requested-by SID] [--only IDS]`** (one machine, internal): `create`s a `settling` record (`force: "wait for ready"`, one `waiting` / `not sent` / `not said` entry per session, none when the machine has no session), then per entry one `update` records its timers and stops them inside the callback. `conversation_pause.release(session_id)` supplies the paused instances (`was_enabled` unless the phase is `KeptOff`; a `showrunner-<slug>` instance whose slug is a paused footer gets `footer{slug}`); each other instance in `session.timers` takes `was_enabled` from its notifier state holding `ENABLED=1`. Only `was_enabled` instances get `notifier.sh stop`; a stop failure cancels the record and exits 1. `begin` sends no message.
- **`refresh <login> [--message IDS...]`** (one machine, internal): exits 1 (`NoLiveRecord`) unless the record is live and `settling`. Under the scope's fresh inventory, one `update` adds a new session as a `waiting` entry with its timers recorded and stopped, marks an entry missing from the inventory `already gone` unless its pid is unattributed, returns a reappearing one to `waiting`, advances seats, and sends the settle message to each listed entry that is present, not a seat, and due. It prints `[{"record", "verdicts"}]` (`RefreshReport`; `EntryVerdict{session_id, verdict}`, verdict `counts ready` | `holdout{line}`), so each entry's verdict is decided on its own machine. An entry counts ready when `already gone`, `stopped` or `process identity lost`; a seat when `passive seat ready`; any other when `ready` and the fresh session is not `tracking` with `ahead > 0`, lists no busy Codex seat, and, for a showrunner, `git -C <cwd> rev-parse -q --verify MERGE_HEAD` fails. A missing entry whose pid is unattributed is a holdout carrying that reason.
- **Seats** get no message; a seat goes `passive seat ready` when its owner counts ready, is `no director`, is not an entry or is `already gone`, and the seat is `idle`; otherwise back to `waiting`.
- **Settle message:** `send.py --to session:<id> --from shutdown --summary "Shutdown of <label>: reach a safe stop"`. The text differs for a unit director (ends its turn with `— blocked: shutdown requested by the user`), a showrunner (merges, pushes, appends `### STATE` to its LOG) and any other session; each ends with `shutdown.py ready --where "…"`. Exit 0 records `sent{at}`; exit 1 records `queued{at, reason}` (the `QUEUED:` text), due again 300 s later (`SETTLE_RETRY_SECONDS`); any other exit raises.
- **`conduct <login> [--here]`** (`conduct_cycle(login, unreached_since, here) -> tuple[bool, list[RefreshReport]]`, every 15 s): `refresh` here and, unless `--here`, there through `run_remote`; a failed remote refresh (rc 255 unreachable, else unavailable) keeps that machine in `unreached_since` until one succeeds. Due a message: not a seat, not counting ready, not the requesting session, not `sent`; a showrunner only when every unit of its production counts ready and no machine is unreached. Each machine with any gets `refresh --message <ids>`. `conduct` exits 0 when every entry on both machines counts ready with none unreached, or this machine's record is gone or no longer `settling`.
- **Holdout alert:** 20 minutes after `requested_at`, then at most every 60 minutes, `send.py --to user --need decision --summary "Shutdown of <label>: N not ready"` lists each holdout as `<machine> <kind> <name>: <status>` plus `busy`, `showing a form` (a tmux or unit pane holding `form` or `esc to cancel`), `ahead N on <branch>` or `merge in progress`, each unreached machine as `<machine>: not reached since <time PDT>`, and the choices `/shutdown now stops them anyway`, `/shutdown cancel undoes the shutdown`; the same text goes to each requesting session.
- **`ready --where TEXT`** (`settle.ready`): finds this machine's `settling` record holding `CLAUDE_CODE_SESSION_ID` and re-runs inventory; exit 2 with the reason on stderr when the session is missing (`this session is not in a fresh inventory of <label>: <reason>`), is a seat, or is blocked (`push <branch> first`, `Codex seat <name> is still running`, `finish the merge in progress first`); else one `update` sets `where: said{text, at}` and `progress: ready{at}` and prints `ready for shutdown: <where>`. No such record: `no shutdown in progress for this session`, exit 1.
- **`cancel [account] [--here]`**: `_cancel_local(login)` stops the conductor the record names (`systemctl --user stop` / `launchctl remove`), sets `cancelled` inside `update`, restores (`notifier.sh start` per `was_enabled` timer, `showrunner_footer.set_footer_state(slug, FooterState.ON)` per `footer{slug}`, and "Shutdown of <label> cancelled by the user: continue where you were." to each entry `sent` or `queued`) and `archive`s; no live record does nothing. Without `--here` it runs `cancel <login> --here` there; a failure prints `<machine> not reached: run /shutdown cancel there when it is back`, exit 1.
- **`status`** appends each machine's live record (there through `records --json --here`): `<machine>: shutdown <state>`, `  selected: <ids>`, and per entry `  <name>: <progress>` (`ready (<where text>)`), then ` · message sent` or ` · message queued: <reason>`; a record not read prints `<machine>: shutdown record not reached (…)` or `… unreadable: <reason>`. The subparsers `metavar` is `{down,status,cancel}`.
- **Messaging:** `send.py` `relay_plan(message) -> RelayAttempt{message} | SessionIdWithoutLiveRecipient{address}` relays a live `session:<id>` to `uds:<messagingSocketPath>` and queues an offline one under the literal address (exit 1, "no live session answers to session:<id>, so it is kept under that name"). `conversation_pause.JOB_SENDERS` includes `shutdown`, so its messages do not pause scheduled updates; `release(session_id) -> PauseRecord | NoPauseRecord` deletes the record under `record_lock()` without resuming anything.
- **Commands:** `commands/shutdown.md` maps `/shutdown [account]` (`down`, then `ready --where`, end the turn: this session is stopped last), `--here`, `status` and `cancel`, leads with the account's label, and leaves `--only` out. `commands/message.md` Receiving: an instruction from sender `shutdown` is the user's shutdown request.
- **Live check** (natedev, two scratch sessions, `down --only`): delivery, both `ready` with their `where`, scope `selected`, a timer stopped; `cancel` restored it and messaged both.

**Files:**
- `scripts/shutdown/settle.py` — `down`, `begin`, `refresh`, `conduct`, `ready`, `cancel`, `status_record_lines`.
- `scripts/shutdown/shutdown.py` — verbs `down`, `begin`, `refresh`, `conduct`, `ready`, `cancel`; record lines in `status`.
- `scripts/shutdown/record.py` — `Queued{at, reason}` in `SettleMessage`.
- `scripts/hooks/conversation_pause.py` — `release`; `shutdown` in `JOB_SENDERS`.
- `scripts/message/send.py` — `relay_plan` and the `session:<id>` address.
- `commands/shutdown.md`, `commands/message.md`.
- `pyrightconfig.json` — `scripts/hooks` in the `scripts/shutdown` environment's `extraPaths`.
- `scripts/shutdown/test_settle.py`, `test_record.py`, `test_shutdown_status.py`, `scripts/hooks/test_conversation_pause.py`, `scripts/message/test_send.py`.

**Binds later work:** "Shutdown stops every session, seat server and window" replaces `conduct_cycle`'s `tuple[bool, list[RefreshReport]]` and reorders `_cancel_local`, which today stops the conductor before marking `cancelled`. "Restart resumes every session where it was" reads each entry's `where` (`not said` when `ready` never ran). The two-machine live check waits until both machines run the merged code.

**Gotchas:** `send.py` resolves `session:<id>` only against this machine's live sessions, so the other machine's settle messages go out from its own `refresh --message`. `_stop_conductor` does not capture output: `cancel` after the conductor exited prints systemd's `Failed to stop … not loaded`. Tests replace `settle.now_utc()` and set `SHUTDOWN_NOTIFIER`, `SHUTDOWN_SEND`, `SHUTDOWN_TMUX` and `NOTIFIER_STATE_DIR`.

### Phase 6 — Shutdown stops every session, seat server and window  · status: done

#### As-built

- **Type names:** the record types say what they hold without their module — `ShutdownRecord`, `ShutdownSessionEntry`, `ShutdownState`, `LiveShutdownRecord`, `ShutdownScope`, `StopTiming` (`"wait for ready"` | `"now"`), `SettleMessageNotSent`/`SettleMessageSent`/`SettleMessageQueued`, `WhereSaid`/`WhereNotSaid`, and `SessionProgress` over `SessionWaiting`, `SessionReadyToStop`, `PassiveSeatReadyToStop`, `SessionStopped`, `SessionAlreadyGone`, `SessionStopFailed`, `SessionRestarted`, `SessionRestartFailed`, `SessionNeedsManualRestart`. On-disk `kind` strings and field names are unchanged.
- **The claim** (`stop.py`), one `update` each: `claim_stop(login, force: StopTiming | None = None) -> bool` moves `settling` to `stopping` and also accepts a record already `stopping` with the same explicit force, so a retry after an ssh 255 succeeds; `claim_cancel(login) -> CancelClaim` (`CancelClaimed` | `CancelAlreadyStopping` | `CancelEnded`) moves `settling` to `cancelled`. The loser changes nothing. `cancel` claims first on the machine whose conductor is started; a lost claim prints `shutdown of <label> is already stopping; /shutdown restart brings it back once it is down` and exits 1. `stop_conductor` captures the service manager's output.
- **`stop(login, …) -> StopReport`** (`record`, `failures`, `left_running`, `unattributed`, `counts`; every live effect injected) enters `stopping` and walks units → showrunners → top-level → the `requested_by` session, then seats. A fresh inventory under the record's scope decides identity before readiness: absent → `already gone`; session id with another pid or `proc_start`, or pid unattributed as `process start mismatch` → `process identity lost`, never signaled, its servers and tmux session reported left running; `account unreadable` → a failure. A present entry not `ready` (seat: `passive seat ready`) and `idle` is refused with a failure line unless `force` is `now`, which first ends each busy Codex seat (`codex_mesh.py end`). A rerun skips terminal entries and never re-signals a `stop failed` one.
- **Signals and resources:** `SIGTERM`, wait 10 s (`STOP_WAIT`) for the pid and `~/.claude/sessions/<pid>.json` to go, match identity again, `SIGTERM`, wait 10 s; still alive → `stop failed` (`alive after two SIGTERMs`), never `SIGKILL`. After an owner exits or is `already gone`: `codex_mesh.py stop` once per run folder, confirmed by exit 0 and no live pid in `mesh_server.json` / `mesh_retired.json`; a unit's `tmux kill-session -t =<tmux_session>`, confirmed by `has-session` failing. A Ghostty `window_shell` gets `SIGHUP` only after this stop ended its Claude and while its parent is Ghostty by the now-public `inventory.terminal_kind`; other hosts are left as they are.
- **Seats** get no signal: `stopped` when the owner entry is `stopped` or `already gone`, or the owner is absent and fresh inventory shows no live seat process. **Completion:** every entry `stopped`, `already gone` or `process identity lost` with no failure → `down` (an empty record too); otherwise `stop partial`.
- **Conductor** (`settle.py`): `conduct_cycle` returns `ConductCycleOutcome` = `SettlementPending` | `ReadyToStop` (every entry counts ready, or `force` is `now`, with both machines reached) | `SettlementEnded`. On ready, `conduct` claims here, then the peer through `claim_remote_stop` (hidden verb `claim-stop <login> --force <timing>`), so both records are `stopping` before a session is touched; a failed peer claim alerts and exits 0. `_stop_machines` runs `stop` here and there through `stop_remote` (hidden verb `stop <login>`, JSON checked by `parse_stop_report`, ssh timeout `max(120, 60 + 20 × owners)` s), the requester's machine last; both remote calls retry rc 255 with no overall limit.
- **Alert** (`send_stop_alert`, `--need note`): `<label> is down` or `<label>: stop partial`; per machine the counts by kind, failures, resources left running and `left running, not attributed to <label>: N (<count> <reason>, …)`; then the restart line. From the Mac it routes `--machine natedev`.
- **Verbs:** `now [account]` sets `force: "now"` on each reachable `settling`/`stopping` record. Hidden `held-sessions` prints the session ids of every entry in a live record that is `settling`, `stopping`, `down`, `stop partial`, `restarting` or `restart partial`; exit 3 on an unreadable record. `remove_seats.shutdown_held_sessions() -> HeldSessions | ShutdownStateUnreadable` calls it (30 s): a held `active/<id>` run counts as live, and on any failure every `active/<id>` run counts as live with one stderr line.

**Files:**
- `scripts/shutdown/stop.py` — the claims, `stop`, `StopReport`, `parse_stop_report`, `stop_conductor`.
- `scripts/shutdown/record.py` — the renamed types.
- `scripts/shutdown/settle.py` — `ConductCycleOutcome`, `conduct`'s stop, `claim_remote_stop`, `stop_remote`, `send_stop_alert`, `now`, claim-first `cancel`.
- `scripts/shutdown/shutdown.py` — verbs `now`, `stop`, `claim-stop`, `held-sessions`.
- `scripts/shutdown/inventory.py` — public `terminal_kind`.
- `scripts/delegate/remove_seats.py` — `shutdown_held_sessions` and the held-run rule.
- `commands/shutdown.md` — `/shutdown now` and what `down` means for each kind.
- `scripts/shutdown/test_stop.py`, `scripts/delegate/test_remove_seats.py` — new cases; the other shutdown tests use the new names.

**Binds later work:** There is no `refused` progress: a refused entry keeps `waiting` or `ready`, and stop failures live only in `StopReport` and the alert, not on the record. A claimed stop that then fails leaves its records `stopping`; "A stop that fails or runs long still ends in a known state" owns that, `stop_remote`'s timeout and `claim_stop`'s two roles. "Restart resumes every session where it was" reads a peer record set `stopping` by `claim-stop`, then `down` or `stop partial` by its own `stop`.

**Gotchas:** `record.archive` accepts only `cancelled` or `up`, so a scratch `down` stays the live record until restart exists; a live check moves it into `history/` by hand. `send.py --to user` logs the text's first line as the summary; the title goes to the user channel. Ghostty launched while the KDE session is locked starts no shell. Not yet run live: the Ghostty window close, every Mac leg (`claim-stop`, remote `stop`, the `--machine natedev` route) and Phase 5's two-machine `cancel`.

### Phase 7 — A stop that fails or runs long still ends in a known state  · status: done

#### As-built

- **Remote calls.** `remote.run_remote(args, stdin="", limit: RemoteCallLimit = STANDARD_TIME_LIMIT)`, with `RemoteCallLimit = TimeLimit{kind: "time limit", seconds} | WhileLinkAlive{kind: "while link alive"}` and `STANDARD_TIME_LIMIT` a 120 s `TimeLimit` that every caller but the stop keeps. `WhileLinkAlive` adds `-o ServerAliveInterval=15 -o ServerAliveCountMax=4` before the host and no timeout, so the call ends when the remote command ends or ssh drops a link that missed four keepalives (255). `settle.stop_remote(login)` uses it and retries 255 every `SETTLE_INTERVAL_SECONDS`.
- **Stop lock.** `record.stop_lock(login)` holds an exclusive `fcntl.flock` on `<SHUTDOWN_STATE_DIR>/<login case-folded>/stop.lock`, a separate file from the record's `lock`, always taken before it, and held for a whole `stop` (before it enters `stopping` until its report is built); `status`, `records` and `refresh` never wait on it. A second stop on the same machine waits; the kernel frees the lock when its holder dies, and the next stop skips entries already terminal. `_enter_stopping` returns `StopUnderway{record}` for `settling`/`stopping` and `StopFinished{record}` for `down`/`stop partial`, else raises `NoLiveRecord`; on `StopFinished` `stop` signals nothing, runs no `codex_mesh.py` or `tmux` command and returns that record's report.
- **Claims.** `stop.claim_stop_as_conductor(login) -> bool`: `settling` → `stopping`, keeping the record's own `force`; `conduct` uses it. `stop.claim_stop_as_peer(login, timing) -> bool`: `settling` → `stopping` with `force: timing`; a record already `stopping` replays true and its force upgrades to `now`, never down. It sits behind the internal verb `claim-stop <login> --force <timing>`.
- **Failure closes.** `stop.close_failed_stop(login, issue: OrchestrationStopIssue) -> None`, under the stop lock in one `update`: `stopping`/`down` → `stop partial` with the issue appended; `stop partial` keeps its state and takes the issue; any other state, or no record, is unchanged; issues dedupe by kind, machine and reason. The hidden verb `close-failed-stop <login> --reason TEXT` calls it with `MachineStopFailed` for its own machine. Once `conduct` wins its claim it closes a failed peer claim (`StopClaimFailed`; no stop runs), a raised local stop (`MachineStopFailed`), and a remote stop that answers non-zero or with a report `parse_stop_report` refuses (`MachineStopFailed`, closed on the peer through `close-failed-stop` over `run_remote`, 255 retried; a non-zero answer adds `; its record could not be closed (rc N)` to the reason). Local closes run after both machines' stops ran, then the alert goes out. Every claimed record ends `down` or `stop partial`, never `stopping`.
- **Issues on the record** (`record.py`):

```python
StopLeftRunningCause = Literal["stop not confirmed", "owner identity lost"]

class NotReadyToStop(TypedDict):          # present, not stopped: `now` not given and not ready and idle
    kind: Literal["not ready to stop"]
    at: str
    status: str                           # the fresh inventory's status: busy, idle, shell
    progress: Literal["waiting", "ready", "passive seat ready"]

class StillRunningAfterStop(TypedDict):   # its progress is `stop failed`
    kind: Literal["still running"]
    at: str
    reason: str                           # "alive after two SIGTERMs", or the signal's OSError text

class AccountUnreadableAtStop(TypedDict): # its pid was unattributed as `account unreadable`, so never signalled
    kind: Literal["account unreadable"]
    at: str

class SeatStillLive(TypedDict):           # a seat not marked stopped: its owner or the seat is still live
    kind: Literal["seat still live"]
    at: str

class CodexServerLeftRunning(TypedDict):
    kind: Literal["codex server left running"]
    at: str
    run_dir: str
    cause: StopLeftRunningCause

class UnitTmuxLeftRunning(TypedDict):
    kind: Literal["unit tmux session left running"]
    at: str
    tmux_session: str
    cause: StopLeftRunningCause

SessionStopIssue = (NotReadyToStop | StillRunningAfterStop | AccountUnreadableAtStop
                    | SeatStillLive | CodexServerLeftRunning | UnitTmuxLeftRunning)

class StopClaimFailed(TypedDict):
    kind: Literal["stop claim failed"]
    at: str
    machine: str
    reason: str

class MachineStopFailed(TypedDict):       # stop raised, answered non-zero, or its report was unreadable
    kind: Literal["machine stop failed"]
    at: str
    machine: str
    reason: str

OrchestrationStopIssue = StopClaimFailed | MachineStopFailed
StopIssue = SessionStopIssue | OrchestrationStopIssue
```

  `ShutdownSessionEntry.stop_issues: list[SessionStopIssue]` and `ShutdownRecord.stop_issues: list[OrchestrationStopIssue]`; `begin` writes both empty, `parse_records` checks every variant and its `at`, and a record or entry without the field reads as `[]`. `stop` writes `not ready to stop` (fresh status and progress) for a present entry refused without `now`, `still running` for `stop failed`, `account unreadable` for an unattributed pid of that kind, `seat still live` for a seat not marked stopped, cause `stop not confirmed` for an unconfirmed Codex server or unit tmux session, and cause `owner identity lost` once per run folder and unit tmux session of an owner whose process identity is lost. Each pass of `stop` over an entry replaces its issues.
- **`_finish`.** `down` when every entry is `stopped`, `already gone` or `process identity lost`, no entry holds an issue other than a left-running one with cause `owner identity lost`, and the record holds no orchestration issue; else `stop partial`. `StopReport{record, unattributed, counts}` (and `parse_stop_report`): the record's issues are the one source.
- **Rendering.** `settle.stop_issue_text(issue: StopIssue) -> str` serves the alert and `/shutdown status`: `not stopped: <status>, <progress>`; `still running: <reason>`; `not stopped: account unreadable`; `not stopped: owner or seat still live`; `Codex server <run_dir> left running: <cause>`; `tmux session <tmux_session> left running: <cause>`; `<machine>: stop claim failed: <reason>`; `<machine>: stop failed: <reason>`. `status_record_lines` prints entry issues indented four under the entry, then record issues indented two under the machine. `send_stop_alert` takes the orchestration issues and prints, per machine, `  <name>: <issue text>` for each session issue (a machine whose stop failed included), then each orchestration issue.
- `settle.ConductorDecision = SettlementPending{reports} | ReadyToStop{reports} | SettlementEnded{reason}`, kinds `settlement pending`, `ready to stop`, `settlement ended` (the former `ConductCycleOutcome`, kinds unchanged).
- `settle.record_time()` writes every canonical record timestamp; `settle.now_utc()` is the one clock tests replace; record parsing refuses sub-second timestamps.
- `commands/shutdown.md` documents what `stop partial` keeps and that `/shutdown status` shows it under each machine, and that a stop runs to its end and a second one on the same machine waits.

**Files:**
- `scripts/shutdown/remote.py` — `RemoteCallLimit`, `STANDARD_TIME_LIMIT`, the `WhileLinkAlive` keepalive options.
- `scripts/shutdown/record.py` — `stop_lock`, the `StopIssue` types, both `stop_issues` fields and their parsing.
- `scripts/shutdown/stop.py` — the stop lock around `stop`, `StopUnderway` / `StopFinished`, the two claims, session issue writes, `close_failed_stop`, `_finish`, `StopReport`.
- `scripts/shutdown/settle.py` — `stop_remote` under `WhileLinkAlive`, the failure closes in `conduct`, `ConductorDecision`, `stop_issue_text`, issue lines in `status_record_lines` and `send_stop_alert`, `record_time` / `now_utc`.
- `scripts/shutdown/shutdown.py` — `claim-stop` on `claim_stop_as_peer`; hidden verb `close-failed-stop`.
- `commands/shutdown.md` — what `stop partial` keeps and shows; a stop runs to its end and a second one waits.
- `scripts/shutdown/test_remote.py`, `test_settle.py`, `test_stop.py`, `test_record.py`, `test_shutdown_status.py` — call limits, failure closes and `ConductorDecision`, stop lock, claims and issue persistence, issue parsing, issue rendering.

**Binds later work:** a claimed stop that fails ends `stop partial`, never `stopping`, so restart (`up`) claims it and the launch block (`is-down`) sees it; stop issues stay on the record and entries until it is archived; the peer claim replays any `stopping` record and only upgrades its force; `close-failed-stop` is a hidden conductor-to-peer verb; every new record timestamp goes through `settle.record_time()`, and tests replace `settle.now_utc`; `stop.lock` is separate from the record lock, so a reader holding only the record lock never waits on a running stop.

**Gotchas:**
- Owner cleanup runs before an entry's terminal progress is written, and progress and issues are written in one update, so a stop killed between them is continued by the next.
- Owner absence is decided from the fresh inventory, never the recorded pid alone.
- `conduct` closes failed stops only after both machines' stops ran: a record already `stop partial` makes a later `stop` signal nothing.
- A timestamp with sub-second precision makes the record unreadable; write timestamps only through `settle.record_time()`.

**Ruled out:**
- A computed time limit on the remote stop (`max(120, 60 + 20 × owners)`): a working stop is cut off only by a dead link.
- Peer claim replay for the same timing only: a `now` landing on a peer already stopping must take effect, and only one conductor can claim, so a timing mismatch is never a second conductor.

### Phase 8 — Restart resumes every session where it was  · status: done

#### As-built

`/shutdown restart [account] [--dry-run]` runs `up <login> [--dry-run]` here and on the peer through `run_remote`, bringing each stopped session back in its host, on its desktop, told where it was, with its timers.
- **Account:** `named_claude_account(text)`, else `own_claude_account()`, else the only `down`/`stop partial`/`restart partial` record across `live_records()` and the peer's `records --json --here`; otherwise it lists them, exit 1. An unreached machine keeps its record; output and alert carry `restart incomplete: <machine> not reached — run /shutdown restart again when it is back`.
- **Claim:** one `update` moves `down`/`stop partial`/`restart partial` to `restarting`; only that call launches or messages. Already `restarting` → `already running`, exit 0; no live record → `<machine>: no shutdown of <label> to restart`, exit 0, counted done; other states → `… is <state>; nothing restarted`, exit 1. An exception after the claim sets `restart partial`, so a `restarting` record always has a running `up`. Failures print on stdout (`<machine>: restart failed: <reason>`, or `<other>: restart failed (exit N)` for an empty failed remote run) and reach the alert.
- **Entries:** units, showrunners, top-level, requester; each unit is live before any showrunner launches. `restarted` and `seat available on demand` entries stay. A live session id, whatever its progress, gets the note through `send.py --to session:<id> --from shutdown`, its timers, and `restarted`; any other entry is launched. Stop issues stay and change nothing. A `manual restart` entry is rechecked each `up` (live → timers, footer, `restarted`; else its command prints again). Seats never launch: `seat available on demand{at}` when the owner is back, live, `manual restart` or absent; `restart failed{reason: "owner <name> not back"}` when it failed.
- **Hosts:** every launcher is an argv list; the one shell string is the resume command, one argv item to `zsh -ic`: `cd <shlex.quote(cwd)> && ENABLE_TOOL_SEARCH=true command <shlex.join([claude, --resume, id, -n, name, --remote-control, name, (--model, name for model{name}), --settings, '{"disableAgentView": true}', prompt])>; exec zsh`, so spaces, quotes and `$()` reach Claude unchanged. Unit → `add_unit.py … --resume <id> --cwd <cwd>` plus the three restore flags (plan `no run record` → `manual restart`). Showrunner → prompt `/showrunner:produce <doc> resume`, the note through `send.py` once live. `WindowHost` (Ghostty, Zed) → `kdotool set_desktop <n>` from `~/.config/kwinrc` (else the current desktop), `systemd-run --user --collect --quiet -- ghostty -e zsh -ic …`, 1.2 s apart, then the starting desktop; Zed sessions return in Ghostty, since nothing outside Zed types into its terminal. `TerminalHost` (Mac) → `open -na "/Applications/Nix Apps/Ghostty.app" --args -e zsh -ic …`. `TmuxHost` → `tmux new-window -t =<s>:` when `has-session -t =<s>` succeeds, else `new-session -d -s <s>`. `UnknownHost` → `manual restart`.
- **Note:** the first prompt gives label, stop and restart times (`%Y-%m-%d %H:%M %Z`, America/Los_Angeles), the recorded `where` (for `not said`: read branch and plan), `send.py pending` first, and keep waiting if it was waiting on the user. Before each tmux and window launch it is recorded through `conversation_pause.record_scheduled_prompts`; a failed record fails the entry (`restart note not recorded: <reason>`).
- **Completion:** up to 90 s per session for `sessions.live_sessions()`, then `notifier.sh start` per `was_enabled` timer and footers on; not back → `restart failed{reason}`, record `restart partial`, a rerun retries only those. All back → record `up`, archived, one alert `send.py --to user --need note --summary "<label> is back"` with per-machine counts. A `manual restart` keeps `restart partial`, timers stopped, seats held through `held-sessions`. Times go through `settle.record_time()`. `--dry-run` prints every argv and changes nothing.
- **`add_unit.py`:** `--restart-note FILE --session-name NAME --tmux-session NAME`, all three and only with `--resume`, parse into `UnitRestoreLaunch{session_id, note, session_name, tmux_session}`, every other launch into `NewWorkLaunch`; any other mix is refused. The note replaces `Run /unit:direct <plan>.`; the session name feeds `-n`/`--remote-control`; the tmux name feeds `tmux new-session -s` and `tmux_live`. Every scope is `--unit=<sanitized tmux session name>-<epoch seconds>` (outside `[A-Za-z0-9_.-]` → `-`). Over a marked unit: `ClaudeUnknown` refuses (exit 2); `ClaudeNotRunning` kills the stale tmux, then launches.
- **Queue:** `queue_for` keeps `session:<id>` under `session-<id>` while no session is live, for the resumed session's `pending`. Settle, cancel and restart-note messages carry `--key shutdown-<login>-<session id>`; a delivered keyed send removes its queued copy; `send.py retire --to --from --key` removes that key's messages and unkeyed ones from that sender, printing the count. The on-disk `key: string | null` reads as `KeyedMessage{key}` | `UnkeyedMessage`. `up` retires each entry's messages before acting on it, and cancel does the same (`settle.retire_command`), so `pending` never prints a stale settle or cancel.

**Files:**
- `scripts/shutdown/restart.py` — `restart` and `up`.
- `scripts/shutdown/shutdown.py` — verbs `restart`, `up`.
- `scripts/shutdown/record.py` — progress `seat available on demand{at}`.
- `scripts/shutdown/settle.py` — the message key, `retire_command`.
- `scripts/production/add_unit.py` — restore flags, launch kinds, unique scope.
- `scripts/message/send.py` — session-id queue, `retire`, `KeyedMessage` / `UnkeyedMessage`.
- `commands/shutdown.md` — `/shutdown restart [account]` and the terminal line.
- `scripts/shutdown/test_restart.py`, `scripts/production/test_add_unit.py`, `scripts/message/test_send.py` — tests.

**Binds later work:** Nothing new starts on a down account grants the shutdown exemption to a `UnitRestoreLaunch` only, after checking its session against the live record, beside `add_unit.py`'s marked-unit handling; it also owns corrupt record paths and the check-to-spawn race. Restored sessions keep their timers replaces the recorded bare note with the unit's `prompt_for` restore prompt and the showrunner's `/showrunner:produce <doc> resume`, and owns retrying a timer that fails to start (now shown only in the alert). A restarted tmux session takes back the pane its shutdown left: the resume command ends in `exec zsh`, so a later shutdown leaves a shell and the next restart opens a window beside it.

**Gotchas:**
- `conversation_pause.prompt_source` classes the CLI's first prompt as typed, pausing timers and later asking the user, unless it is recorded with `record_scheduled_prompts`.
- `run_remote` drops the peer's stderr; a failure meant for the other machine must reach stdout.
- A tmux server from an earlier launch can still hold `<name>.scope`, which is why the scope name carries epoch seconds.
- The merge gate runs basedpyright on each touched `scripts/` directory alone, each 0/0/0; `scripts/message`'s pyright config lists `scripts/shutdown` in `extraPaths`.

**Ruled out:** dropping `exec zsh` from tmux launches — it still leaves the user's original shell window beside every restart.

### Phase 9 — A restarted tmux session takes back the pane its shutdown left  · status: done

#### As-built

A tmux session that `/shutdown restart` brings back resumes in the pane it ran in when that pane is still there and idle, so shutdown and restart cycles add no windows to the user's tmux sessions; a pane put to other use is left alone.
- **Recorded pane:** `TmuxHost` carries a required `pane: TmuxPane | PaneNotRecorded`. `TmuxPane{kind, pane_id, pane_pid}` (`kind: "pane"`) holds tmux's `%N` id, the session process's `TMUX_PANE`, and the pane's first process (`#{pane_pid}`); `PaneNotRecorded` (`kind: "not recorded"`) stands for an older record or a tmux that did not answer the pid. `_tmux_session(pane)` reads both with one `tmux display -p -t <pane> '#{session_name}\t#{pane_pid}'` (a literal tab); session names keep their spaces, only the line ending is stripped. The record parser reads a missing `pane` as `not recorded` and validates a present one like every other field.
- **Reuse:** `_tmux_launch` probes `tmux display -p -t <pane_id> '#{session_name}\t#{pane_pid}\t#{pane_dead}'`. The pane is reusable when the probe succeeds, its session is the recorded `tmux_session`, and it is dead (`#{pane_dead}` 1) or its pane pid is the recorded `pane_pid` with no child process (`pgrep -P <pane_pid>` exits 1). A reusable pane gets `respawn-pane -k -t <pane_id> -c <cwd> zsh -ic <command>` and no window opens; anything else (pane gone, in another session, a different pid, a child under its shell, `not recorded`) gets `new-window` in the surviving session, else `new-session`.
- **Cycle:** the respawned pane keeps its id, and its new first process is the `zsh -ic` that `exec zsh`s after claude, so the next shutdown records that pid and the next restart takes the same pane again. The restart note is recorded as a scheduled prompt before a respawn, as before every other launch. `--dry-run` runs the read-only probes and prints them beside the argv it would run.

**Files:**
- `scripts/shutdown/inventory.py` — `TmuxPane`, `PaneNotRecorded`, `_tmux_session`, the parser's `pane` branch.
- `scripts/shutdown/restart.py` — `_recorded_pane`, `_pane_is_reusable`, `_respawn_pane_argv`, `_tmux_launch`.
- `scripts/shutdown/test_inventory.py`, `scripts/shutdown/test_restart.py` — tests; `scripts/shutdown/test_stop.py`, `scripts/shutdown/test_settle.py` — fixtures carry `PaneNotRecorded`.

**Binds later work:** Restored sessions keep their timers relies on: a tmux session may come back in its recorded pane through `respawn-pane`, with the restart prompt recorded before it. Every `TmuxHost` built in code or tests names its `pane`.

**Gotchas:**
- The Mac has no tmux, so a `TmuxHost` exists only on natedev.
- The dry run on the reusable-pane path also prints a `has-session` probe it does not use.

**Ruled out:** a `NotRequired` `pane` — a required field, with `PaneNotRecorded` in fixtures, keeps every host explicit.

### Phase 10 — Restored sessions keep their timers  · status: todo

#### Work Order

**Goal:** a unit or showrunner that `/shutdown restart` brings back keeps the timers restart turns on: its first prompt is recorded as expected, so its conversation-pause hook neither pauses those timers nor asks the user to turn them back on. A timer that fails to start leaves the restart unfinished, so running `restart` again starts just that timer without relaunching or re-noting the session.

**Spec:**
- `conversation_pause.prompt_source` returns `SCHEDULED` for a prompt recorded for the session with `record_scheduled_prompts`; Phase 8 records the restart note before each tmux and window launch, where the note is the whole first prompt.
- `restart.py` names the recording for what it records: `_record_restart_note` (292) becomes `_record_scheduled_restart_prompt(session_id, prompt, *, dry_run)`, `RestartNoteNotRecorded` (85) becomes `ScheduledRestartPromptNotRecorded`, and the entry's failure reason reads `restart prompt not recorded: <reason>`. `_launch_session(...) -> str | None` (539) becomes `-> SessionLaunched | ManualRestartRequired` (frozen dataclasses; `ManualRestartRequired.command` is today's returned command), so no caller reads a bare `None` as "launched".
- A showrunner's first prompt is `_showrunner_prompt(session)` (259, `/showrunner:produce <doc> resume`), not the note: `restart.py` records the prompt it launches, appending it to the session's recorded prompts as Phase 8 does, before the launch. Its note still arrives afterwards as a `shutdown` message.
- A unit's first prompt is built by `add_unit.py` (`prompt_for`, 611-638: the identity line, a space, then the note file's text). For a `UnitRestoreLaunch`, `main` computes that prompt once, appends it to the session's recorded prompts, and passes the same string to `launch_session` (641-669, which gains a `prompt: str` parameter in place of calling `prompt_for` itself), so the recorded and launched prompts cannot differ. `add_unit.py` imports `conversation_pause` from `scripts/hooks` at run time as `settle.py` does; `pyrightconfig.json` gains `scripts/hooks` in the `scripts/production` environment's `extraPaths`. A failure to record refuses the launch with exit 1 and one stderr line naming why. `restart.py` stops recording the bare note for unit entries. `NewWorkLaunch` records nothing.
- Prompts already recorded for the session are kept; a dry run records nothing.
- **A timer that does not start.** `record.py` progress gains `timers pending`:

```python
class PendingTimer(TypedDict):
    instance: str
    reason: str

class SessionLiveTimersPending(TypedDict):
    kind: Literal["timers pending"]
    at: str                        # canonical UTC seconds, from settle.record_time()
    timers: list[PendingTimer]     # recorded was_enabled timers that did not start
```

  An entry whose session came back but one of whose `was_enabled` timers did not start (`TimerNotStarted`, 101-104) is `timers pending`, not `restarted`. `_finish_record` (914-) counts it as unfinished, as it counts `manual restart`: the record stays `restart partial`, is not archived, and the output and the alert list each pending timer with its reason. A later `restart` claims the record as usual, finds the session live, launches nothing, sends no second note, starts only the pending timers and their footers, and marks the entry `restarted`; a timer that fails again stays pending with the new reason.

**Files:**
- `scripts/shutdown/restart.py`, `scripts/shutdown/test_restart.py`
- `scripts/shutdown/record.py`, `scripts/shutdown/test_record.py`
- `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py`
- `pyrightconfig.json`

**Seats:** `2 writers` — Phase 8's repair split cleanly the same way.
- `restart` — `scripts/shutdown/restart.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/record.py`, `scripts/shutdown/test_record.py`
- `unit` — `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py`, `pyrightconfig.json`

**Constraints from prior phases:** Phase 8: `restart.py` `_record_restart_note`, which appends to `conversation_pause.read_scheduled_prompts(session_id)` and fails the entry without launching; `add_unit.py`'s `UnitRestoreLaunch{session_id, note, session_name, tmux_session}` and its restore prompt; `conversation_pause.record_scheduled_prompts`, which replaces the whole list, and the Stop hook, which rewrites it after each reply; `COMPLETE_PROGRESS` (68) is `restarted` and `seat available on demand`; an already-live session gets its recorded timers back without a launch; `restart partial` keeps run folders held through `held-sessions`. Tests point the conversation-pause state root at a temp dir and start no `claude`. Phase 9: a tmux session may come back in its recorded pane through `respawn-pane`, with the restart prompt recorded before it.

**Acceptance gate:** `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py'` and `cd scripts/production && python3 -m unittest test_add_unit test_waiting` green; `basedpyright scripts/shutdown` and `basedpyright scripts/production` each 0 errors, 0 warnings, 0 notes, each directory run alone as the merge gate runs it. Tests show, for a showrunner entry and for a unit restore, that the prompt the launch passes to `claude` is recorded for that session before the launch and that `conversation_pause.prompt_source` classes it `SCHEDULED`; for a unit, that the recorded and launched prompts are the same string; that a timer which fails to start leaves the entry `timers pending` and the record `restart partial` and listed in the alert, and that a second `restart` with the timer now starting launches nothing, sends no note, starts only that timer and archives the record `up`. Post-merge, with Phase 8's post-merge gate: a restarted unit's timers are still enabled one minute after its restart, and it has asked the user nothing.

### Phase 11 — Nothing new starts on a down account  · status: todo

#### Work Order

**Goal:** while an account is down on a machine, or its shutdown state there cannot be read, the timed jobs that start Claude work there skip their run and say why, and a unit cannot be launched on it except by restart. No launch can pass its check and then spawn after a shutdown has begun.

**Spec:**
- `shutdown.py is-down [account] [--restart-of SESSION_ID]` — the account resolves as every verb's does (exit 2 on `UnreadableAccount` / `UnknownAccountName`). Its exit status is the contract: 0, printing `<label> is shut down (<state> since <time>)`, when this machine's live record for the account is in `settling`, `stopping`, `down`, `stop partial`, `restarting` or `restart partial`; 1, silent, when there is none; 3, printing `shutdown state unreadable: <detail>` on stderr, when `find_live` raises `InvalidRecord`, `ValueError` or `OSError`, or when the live record is `cancelled` or `up` (`live record in state <state> was not archived`). With `--restart-of`, it exits 1, silent, only for a `unit` entry with that session id that the record's state still lets be launched: in `restarting`, an entry `up` would launch (its progress is none of `restarted`, `seat available on demand`, `timers pending`, `manual restart`); in `restart partial`, an entry whose progress is `manual restart`, whose command a person runs. An entry already restored, a session not in the record, and every other state are as without it. Every status other than 1 means do not launch (fail closed). `<time>` is `requested_at` as `%Y-%m-%d %H:%M %Z` in America/Los_Angeles (`2026-10-09 14:49 PDT`, `2026-01-15 09:30 PST`), never a literal zone name.
- **Corrupt state reads as unreadable, never as absent.** `record.find_live` (690-699) tells absent from unreadable with `os.lstat`, not `Path.is_dir()` / `Path.exists()`: no entry at the account directory, or no `record.json` in it, is `no shutdown`; an account path that is a regular file, a symlink (dangling or looping included) or anything else but a directory, a `record.json` that is not a regular file, and an `ENOTDIR` or other `OSError` on the way raise `InvalidRecord` naming the path and what was found. Every caller already handles `InvalidRecord`; `is-down` turns it into exit 3.
- `scripts/shutdown/launch_permission.py` — new, standard library only, so the nightly review and `add_unit.py` import it without the inventory's imports:

```python
@dataclass(frozen=True)
class NewWorkLaunchPurpose: ...    # a launch of new work: allowed only while no shutdown holds the account

@dataclass(frozen=True)
class ShutdownUnitRestoreLaunchPurpose:
    session_id: str                # a restart bringing back this unit entry: allowed only while its record lets it

LaunchPurpose = NewWorkLaunchPurpose | ShutdownUnitRestoreLaunchPurpose

@dataclass(frozen=True)
class AllowedByShutdownState: ...

@dataclass(frozen=True)
class BlockedByShutdown:
    reason: str            # is-down's stdout line

@dataclass(frozen=True)
class ShutdownStateUnreadable:
    detail: str            # why the state could not be read, never prefixed "shutdown state unreadable: "

LaunchPermission = AllowedByShutdownState | BlockedByShutdown | ShutdownStateUnreadable

def shutdown_state_root() -> Path                 # SHUTDOWN_STATE_DIR, else ~/.local/state/shutdown
def launch_barrier() -> AbstractContextManager[None]
def launch_permission(environment: Mapping[str, str], purpose: LaunchPurpose) -> LaunchPermission
```

  `launch_permission` runs `sys.executable <its directory>/shutdown.py is-down` (with `--restart-of <id>` for `ShutdownUnitRestoreLaunchPurpose`; timeout 30 s) in `environment`, which every caller passes explicitly, and maps 1 → `AllowedByShutdownState`, 0 → `BlockedByShutdown`, any other status, an `OSError` or a timeout → `ShutdownStateUnreadable`. Its `detail` is is-down's first stderr line with one leading `shutdown state unreadable: ` removed; with empty stderr, `is-down exited N`; for an `OSError`, its text; for a timeout, `is-down timed out after 30 s`. Each caller writes the `shutdown state unreadable: ` prefix once.
- **The launch barrier.** `launch_barrier()` holds an exclusive `fcntl.flock` on `<shutdown_state_root()>/launch.lock`, creating the directory and file; an `OSError` opening or locking it propagates. `record._state_root` (333-338) calls `shutdown_state_root()`, so the root rule lives in one place, and `record.create` (702-709) holds `launch_barrier()` around its existing check and write; the lock order is always the barrier, then the account's `lock`. Every launcher holds the barrier from just before its `launch_permission` call through the return of its spawn, never through the work it starts: so a shutdown record cannot appear between a launch's check and its spawn, and a launch whose check comes after the record is refused. A barrier that cannot be opened refuses the launch as `ShutdownStateUnreadable` with `detail` `launch barrier: <OSError text>`.
- `scripts/nightly_review/nightly_review.py` `launch` (90-96): holding `launch_barrier()` through both `start` calls (each returns once `systemd-run` has started its tmux), first `launch_permission(os.environ, NewWorkLaunchPurpose())`: `BlockedByShutdown` → `skipped: <reason>`; `ShutdownStateUnreadable` → `skipped: shutdown state unreadable: <detail>`; either becomes the existing `skipped:` line and no review starts. Then `quota_block() -> str | None` (59-68) becomes `quota_permission() -> QuotaClear | QuotaBelowFloor` (frozen dataclasses; `QuotaBelowFloor.reason` is today's returned text), and `launch` writes `skipped: <reason>` for `QuotaBelowFloor`.
- `scripts/fix/fix-trigger.sh`, after the `pgrep` guard (28-30), holds the same lock file with `flock` through the is-down check and replaces today's `exec` (32-33) with one that closes the lock's descriptor, so a down account and an unreadable state both skip the run and say why in the timer's journal (is-down's own line already carries its prefix once):

```bash
launch_lock="${SHUTDOWN_STATE_DIR:-$HOME/.local/state/shutdown}/launch.lock"
if ! { mkdir -p "${launch_lock%/*}" && exec 9>"$launch_lock" && flock 9; }; then
    echo "fix-trigger: skipped: shutdown state unreadable: launch barrier: cannot lock $launch_lock"
    exit 0
fi
shutdown_status=0
shutdown_reason=$("$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/shutdown/shutdown.py" is-down 2>&1) || shutdown_status=$?
if [ "$shutdown_status" -ne 1 ]; then
    echo "fix-trigger: skipped: ${shutdown_reason:-is-down exited $shutdown_status}"
    exit 0
fi

export FIX_SCHEDULED=1
# fix.sh starts with the lock's descriptor closed, which releases the barrier:
# it covers the start of a run, never the run itself.
exec "$FIX_ORCHESTRATOR_PATH" 9>&-
```

- `scripts/production/add_unit.py` `main` (706-760), after `launch_request` and before `preflight`: holding `launch_barrier()` from here through the return of `launch_session` (preflight, the repository steps, Phase 10's prompt recording and the spawn all inside it; `wait_for_remote_control` after it is released), `launch_permission(<the CLAUDE_*-stripped environment launch_session uses>, <ShutdownUnitRestoreLaunchPurpose(resume id) for a UnitRestoreLaunch, else NewWorkLaunchPurpose()>)`, so it checks the account the unit will run on. `BlockedByShutdown` → refuse (exit 2, `add_unit: <reason>; /shutdown restart first`) before any marked-unit handling, so a refused launch kills no tmux session and writes nothing; a `UnitRestoreLaunch` passes only when `is-down --restart-of` lets that session through, so the restart flags alone never open the way; `ShutdownStateUnreadable` → refuse every launch (exit 2, `add_unit: shutdown state unreadable: <detail>`). `launched_unit(request) -> unit_lookup.MarkedUnit | None` (672) becomes `-> MarkedUnitFound | NoMarkedUnit` (frozen dataclasses; `MarkedUnitFound.unit` is the `MarkedUnit`), and its callers match the two.
- `pyrightconfig.json` — `scripts/shutdown` joins the `extraPaths` of the `scripts/nightly_review` and `scripts/production` environments (the `scripts/shutdown` environment already reaches `scripts/hooks` since Phase 5); both scripts insert `scripts/shutdown` on `sys.path` as `nightly_review.py` does for `whoami` (28-29).
- The timers themselves keep running (the user: "system services keep running"); a run already in progress when the shutdown starts is left to finish (my call: these are services, and they end on their own).
- `commands/shutdown.md` — one sentence: while an account is shut down or restarting on a machine, or its shutdown state cannot be read, the nightly review and the fix pipeline skip their runs there and no unit launches except by restart.

**Files:**
- `scripts/shutdown/shutdown.py` — verb `is-down`.
- `scripts/shutdown/record.py` — `find_live` tells absent from unreadable; `create` holds the barrier; the state root from `launch_permission`.
- `scripts/shutdown/launch_permission.py` — new.
- `scripts/nightly_review/nightly_review.py` — the shutdown check, the barrier and `quota_permission` (also touches; no owner).
- `scripts/fix/fix-trigger.sh` — the guard and the barrier (also touches; no owner).
- `scripts/production/add_unit.py` — the refusal, the barrier and `launched_unit`'s result (also touches; as-built owner enh-showrunner-unit).
- `pyrightconfig.json` — two `extraPaths` entries.
- `commands/shutdown.md`
- `scripts/shutdown/test_record.py` — corrupt paths and the barrier around `create`.
- `scripts/shutdown/test_shutdown_cli.py` — new (`is-down`, `launch_permission`, `launch_barrier`).
- `scripts/nightly_review/test_nightly_review.py` — the skip and `quota_permission`.
- `scripts/production/test_add_unit.py` — the refusal and the barrier.
- `scripts/fix/tests/test_fix_trigger_shutdown.py` — new; hermetic pgrep, is-down and fix.sh stand-ins.

**Seats:** `1 writer + 1 tester` — five one-place checks, one lock and one small module; the tester writes each case from the exit-status contract and the rules above.
- `impl` — `scripts/shutdown/shutdown.py`, `scripts/shutdown/record.py`, `scripts/shutdown/launch_permission.py`, `scripts/nightly_review/nightly_review.py`, `scripts/fix/fix-trigger.sh`, `scripts/production/add_unit.py`, `pyrightconfig.json`, `commands/shutdown.md`
- `test` — `scripts/shutdown/test_record.py` (an account path that is a regular file, a dangling symlink, a looping symlink and a `record.json` that is a directory each raise `InvalidRecord`, never `no shutdown`; a missing account directory and a missing `record.json` are `no shutdown`; while another process holds `launch_barrier()`, `record.create` does not return until it is released); `scripts/shutdown/test_shutdown_cli.py` (`is-down` exits 0 for each of the six states, 1 with no record, 3 for a malformed record, a regular file or broken symlink in place of the account directory, a live `cancelled` or `up` record, and a login `find_live` refuses; its exit-3 stderr is exactly one line starting `shutdown state unreadable: ` once; its exit-0 line shows `PDT` for an October `requested_at` and `PST` for a January one; `--restart-of` exits 1 for a unit entry `up` would launch in a `restarting` record and for a `manual restart` unit entry in a `restart partial` record, and 0 for a `restarted` or `timers pending` entry, a non-`manual restart` entry of a `restart partial` record, a session not in the record, and a `down` record; `launch_permission` maps 1, 0, 3 and a missing interpreter, passes `--restart-of` for `ShutdownUnitRestoreLaunchPurpose`, and against the real `shutdown.py is-down` on a malformed record gives a `detail` without the prefix; a barrier that cannot be opened gives `ShutdownStateUnreadable`); `scripts/nightly_review/test_nightly_review.py` (a down record gives `skipped: claude 2 is shut down …`; the real exit-3 output of a malformed record gives `skipped: shutdown state unreadable: <detail>` with the prefix once; neither starts a review; both starts run inside the barrier; `quota_permission` gives `QuotaBelowFloor` with today's text under the floor and `QuotaClear` otherwise); `scripts/production/test_add_unit.py` (refused while down; a `UnitRestoreLaunch` allowed when `is-down --restart-of` lets its session through, refused when the record is `down` or does not hold it; refused with an unreadable state even as a `UnitRestoreLaunch`, with `add_unit: shutdown state unreadable: ` once; a refused restore over a marked unit kills no tmux session and records no prompt; a shutdown record created after preflight is never followed by a prompt recording or a `launch_session` call, because the barrier is held from the check through `launch_session`, shown with a stand-in `launch_session` that finds the barrier held); `scripts/fix/tests/test_fix_trigger_shutdown.py` (a temp `HOME` and `SHUTDOWN_STATE_DIR` with stand-in `.claude/scripts/lib/py` and `.claude/scripts/fix/fix.sh` and a stand-in `pgrep` on `PATH`: is-down 1 runs `fix.sh` with `FIX_SCHEDULED=1` and the lock is free while `fix.sh` runs; 0 and 3 exit 0 without running it and print `fix-trigger: skipped: <is-down's line>`; a `pgrep` match exits before is-down)

**Constraints from prior phases:** Phase 4: `record.find_live(login)` → `live{record}` | `no shutdown`, raising `InvalidRecord`; the record states; the per-account record path under `SHUTDOWN_STATE_DIR`; `own_claude_account()` / `named_claude_account(text)` with exit 2; `find_live` raises `ValueError` for a login that is empty, holds `/` or NUL, or starts with `.`. Phase 5: the `scripts/shutdown` pyright environment already has `scripts/hooks` in `extraPaths`. Phase 6: the record types' new names (`ShutdownRecord`, `ShutdownState`, `ShutdownSessionEntry`); the internal verb `claim-stop`; record states `stopping`, `down` and `stop partial`. Phase 7: the two stop claims (`claim_stop_as_conductor`, `claim_stop_as_peer`) and the stop lock `stop.lock`, a separate file from the record's `lock`, so `is-down` reading the record never waits on a running stop; `ConductorDecision`; a stop that fails after the claim ends `stop partial` with its `StopIssue`s persisted, never `stopping`, so a failed stop still reads as shut down here and blocks launches until restart. Phase 8: `add_unit.py`'s `UnitRestoreLaunch` (`--restart-note`, `--session-name`, `--tmux-session` together, with `--resume`; parsed at 237-271) and `NewWorkLaunch`; a restore over a marked unit refuses on `ClaudeUnknown` (exit 2) and kills the stale marked tmux on `ClaudeNotRunning` before relaunching, so the permission check comes before both; `up` claims a `down`, `stop partial` or `restart partial` record as `restarting` before it launches any unit, so automatic `add_unit.py` calls always run while the record is `restarting`; an exception after the claim leaves `restart partial`; a `manual restart` entry keeps the record `restart partial` until a later `restart` finds that session live; `prompt_for` 611-638, `launch_session` 641-669, `wait_for_remote_control` 677-693. Phase 10: a `UnitRestoreLaunch` records its prompt as a scheduled prompt before `launch_session`, and `launch_session` takes that prompt; progress `timers pending` is a live session whose timers did not start, never launched again.

**Acceptance gate:** `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py'`, `python3 -m unittest scripts/nightly_review/test_nightly_review.py scripts/production/test_add_unit.py` and `python3 -m unittest scripts/fix/tests/test_fix_trigger_shutdown.py` green; `basedpyright scripts/shutdown`, `basedpyright scripts/nightly_review` and `basedpyright scripts/production` each 0 errors, 0 warnings, 0 notes, each directory run alone as the merge gate runs it; `bash -n scripts/fix/fix-trigger.sh`; live: with a scratch `down` record in a temp `SHUTDOWN_STATE_DIR`, `is-down` exits 0, with a malformed one, and with a regular file in place of the account directory, it exits 3; in each, `fix-trigger.sh`'s guard exits before `fix.sh` (checked with `bash -x` and a stand-in `fix.sh`).
