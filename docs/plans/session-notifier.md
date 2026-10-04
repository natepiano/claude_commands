# Session notifier

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** One notifier sends `/showrunner:dailies` and `/unit:delegate_report` ticks to Claude sessions from a declared 15 s nix job, replacing the showrunner's `systemd-run` timer and the unit's agent-armed `progress_timer.sh`.

> **Production: tool-based-ui** — unit `notifier-unit`; production doc `/home/natepiano/rust/hana_catalyst/docs/hana/tool-based-ui-production.md`

## Delegation Context

- **Project:** `~/.claude` (git, `main`, pushed after each checkpoint) — Claude Code config: the commands, hooks and scripts behind `/unit:delegate`, `/showrunner:*` and messages between sessions. Plus the nixos repo `/etc/nixos` (git, `main`, remote `git@github.com:natepiano/nixos`), for the tick job only.
- **Project started:** 2026-10-04T10:02:50.719+00:00
- **Stack:**
  - zsh 5.9 for new scripts, in the idiom of `scripts/production/showrunner_timer.sh` and `scripts/reports/report_timer.sh`: `#!/usr/bin/env zsh`, `setopt no_unset pipe_fail extended_glob`, `SCRIPT=${0:A}`, `typeset -A conf` filled by a `KEY=VALUE` read loop, `print -r --`, `[[ $x == <1-> ]]`, `${(f)…}` / `${(j: :)…}`, `die(){ print -u2 -r -- "$*"; exit 2 }`. Clock and dates come from `zmodload zsh/datetime` (`$EPOCHSECONDS`, `strftime`), locks from `zmodload zsh/system` (`zsystem flock`): both work the same on Linux and the Mac.
  - The existing delegate scripts `prepare_session.sh` and `end_session.sh` are bash (`set -euo pipefail`) and stay bash.
  - Python ≥ 3.10 through the shim `scripts/lib/py` (Linux 3.13; the Mac's PATH `python3` is Apple 3.9, so never call `python3` from a script). Tests use `unittest`; pytest is not installed. Type checks use basedpyright with `pyrightconfig.json`.
  - Nix: home-manager `nate.jobs` option, rendered as a systemd user service + timer on Linux and a launchd user agent on the Mac.
- **Layout:**
  - `scripts/message/`: `send.py` (every script-sent message), `top_level.py`, `test_send.py`, `test_top_level.py`; new `notifier.sh`, `sessions.py`, `test_notifier.py`, `test_sessions.py`
  - `scripts/production/`: `showrunner_timer.sh` (deleted in Phase 3), `unit_status.sh`; new `production_check.sh`
  - `scripts/delegate/`: `prepare_session.sh`, `end_session.sh`, `progress_timer.sh` (stays: `/clippy` uses it), `progress_history.py`, `test_progress_history.py`; new `unit_notifier.sh`, `test_delegate_check.py`
  - `scripts/hooks/`: `delegate_run.py` (library imported by the delegate hooks), `stop-delegate-progress-timer.py` (not edited; removed by the showrunner after Phase 3)
  - `commands/showrunner/{produce,dailies,interval}.md`, `commands/unit/{delegate,delegate_report,eta}.md`, new `commands/unit/interval.md`
  - `config/delegate.conf`, `config/README.md`, `docs/as-built/plan-delegate-progress-history.md`
  - `/etc/nixos/modules/common/{options.nix,default.nix}`, new `/etc/nixos/modules/common/session-notifier.nix`, `/etc/nixos/modules/linux/nate-backend.nix`, `/etc/nixos/modules/darwin/nate-backend.nix` (read only)
- **Key files:**
  - `scripts/message/send.py` — delivery. Docstring usage `:7-12`; outcomes `:30-37`: SENT/SKIPPED exit 0, QUEUED 1, FAILED 3, usage 2. `parse()` `:460-486`: `--to` (required), `--from`, `--summary`, `--key`, `--timeout` (default 40, `:67`), `--text` / `--file` / stdin. A `--to uds:<socket>` target skips the ListAgents fallback (`:303-306`) and relays with `--tools SendMessage` (`:374`). `enqueue()` `:194-202`: a QUEUED message replaces the queued one with the same `--key`. `claim()` `:219-236`: `send.py ack <key>` silences later sends on that key until `reopen`. Relay model sonnet, about 9 s (`:64-66`). Scripts invoke it as `"$PY" "$SEND" …` with `PY=<repo>/scripts/lib/py` (`showrunner_timer.sh:61-62`).
  - `scripts/production/showrunner_timer.sh` — today's showrunner timer, the model for `notifier.sh`. Conf keys `:39-51` and parse `:79-100`. `read_doc` `:103-128`: `Status: PRODUCTION — <word>` (`:118-119`), `**Showrunner session:**` (`:120-123`), `**Updates:** every N min` (`:124-125`). `systemd-run` `:152-157`. `cmd_interval` `:194-212` with GNU-only `sed -i` at `:205`, `:207`. `cmd_fire` `:214-249`; fire.log line `:216` + `:247`: `fired="$(date '+%Y-%m-%d %H:%M:%S %Z') / $(date -u '+%H:%M:%S UTC')"`, then `print -r -- "$fired | exit $rc | to $doc_session | ${(j: :)${(f)result}}" >> $LOG`. State `~/.local/state/showrunner/<slug>/{timer.conf,prompt.txt,fire.log}`.
  - `commands/showrunner/produce.md` — state `TIMER_CONF` `:30-33`, `TIMER` `:34`; next-dailies rule `:95-97` (`systemctl --user list-timers`); Resume `:127-128`; `<StartUpdates>` `:186-271` (steps `:199-216`, step 3 config `:206-213`, step 4 `TIMER start` `:214-216`, prompt text `:218-231`, tick sender `:233-235`, typed dailies restart `:256-259`, log line `:261`); `<Wrap>` `:774-807`, step 4 `TIMER stop` `:792`.
  - `commands/showrunner/dailies.md` — state list `:11-12`; clock step `:38-41` (`TIMER stop` then `TIMER start`); reference `:252-253`.
  - `commands/showrunner/interval.md` — 29 lines: no-argument status `:12-14`, `TIMER interval` step `:16-19` (exit 2 = refused), commit `:20-22`, log `:23`, tell the user `:24-25`, persistence `:27-29`.
  - `commands/unit/delegate.md` (1333 lines) — `PROGRESS_UPDATES_ENABLED` `:35-36`; `PROGRESS_TIMER_HANDLE` `:39-40`; `<ToolingContract>` `:179-190` (launchers `run_in_background`, `dangerouslyDisableSandbox`); DispatchContract step 4 arm / end turn / re-arm `:199-206`; Codex `<CodexDispatchWait/>` `:207-208`, `:255-279` (poll timeout = interval `:258-265`); BackgroundVerification arm `:287-290`; compaction handoff carries both timer variables `:296-298`; Stop hook stands down when only waiting `:306-308`; `<ProgressContract>` `:479-540` (interval key `:480-483`, launch `progress_timer.sh` `:485-494`, never end a turn unarmed `:496-499`, pass/activity bookkeeping `:501-512`, tick composes `<ProgressReport/>` `:514-520`, re-arm `:522-525`, timer is no substitute for the report `:527-534`, user stops updates `:536-539`); PrepareSession `:638-641`; EarlyReviewArm `:864-865`; "after the timer is armed" `:933-937`; UXReview arming `:1089`; PhaseCleanup "progress timers" `:1197`; `end_session.sh` `:1217`, `:1330-1331`.
  - `commands/unit/delegate_report.md` — read at every tick (`:11`); timer clause `:24-25`; runs `progress_history.py calibrate` `:56` then `progress …` `:62`.
  - `commands/unit/eta.md` — 36-line model for a small `/unit:` command (frontmatter `description`, `## Steps`, `## Answer`).
  - `scripts/delegate/prepare_session.sh` — Mac python `:21-25`; `SESSION_DIR=/tmp/claude/delegate/<uuid>` `:27-28`; active marker written only when `CLAUDE_CODE_SESSION_ID` is set `:32-35` (`/tmp/claude/delegate/active/<claude session id>`, one line: SESSION_DIR); prints `Session ready at <dir>` `:37`.
  - `scripts/delegate/end_session.sh` — `PY` shim `:24-27`; exits 0 without `CLAUDE_CODE_SESSION_ID` `:29-32`; marker `:34`; SESSION_DIR from the marker `:38`; seat cleanup `:39-49`; `rm -f "${MARKER}"` `:50`.
  - `scripts/hooks/delegate_run.py` — library, no CLI. `ACTIVE_DIR` `:20`, `MAX_AGE_SECONDS` `:26`, `LIVE_HEARTBEAT_SECONDS` `:30`, `marker_path` `:33-34`, `_recently_active()` `:37-56`, `active_run()` `:59-68`, `delegate_working()` `:71-83`. Imported by `session-start-delegate-resume.py:25`, `stop-delegate-continue.py:50`, `stop-delegate-progress-timer.py:35`.
  - `scripts/hooks/stop-delegate-progress-timer.py` — `running_work()` `:62-86`: unsuffixed `impl_status == "implementing"` (`:64`; launchers write `impl_status_<role>`, `implement.sh:112,148`, so it misses them), any `review_status*` reading "reviewing" (`:69-73`), `progress_history_state.json` `activity.status == "active"` (`:74-85`).
  - `scripts/delegate/progress_history.py` (4201 lines) — `TIMER_MARKER_FILENAME` `:188`; config `:186-187`, `PLAN_DELEGATE_CONFIG` `:202-204`, `_progress_interval_seconds()` `:231-251`, `PLAN_DELEGATE_NOW_EPOCH` `:175-179`; `_start_run` `:1071-1127` (`run_id = session_dir.name` `:1088`); `_next_report_at()` `:1914-1939` (marker `:1923-1937`, interval fallback `:1938-1939`); `_clock_line` `:1942-1959`; `_progress` `:3533-3875` ("No open window" exit `:3552-3572`, `next_report_at` `:3692`, event field `:3696`, clock line `:3858`, `:3873`); subparsers from `:4024`; per-session flock in `main()` `:4179-4197`.
  - `scripts/delegate/test_progress_history.py` — clock test `test_the_clock_line_names_the_armed_timer_then_falls_back_to_the_interval` `:642-682` (drives the CLI by subprocess with `PLAN_DELEGATE_CONFIG` / `PLAN_DELEGATE_NOW_EPOCH`).
  - `config/delegate.conf:47-58` — `PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=900` (`:58`) and its comment (`:47-57`); also described in `config/README.md:98-104` and `docs/as-built/plan-delegate-progress-history.md:438-445`.
  - `scripts/production/unit_status.sh` (zsh, no pipefail by design `:7-8`) — args `<state-dir> <zone> <tmux-session>…`; per-unit loop `:85-98`: `SESSION GONE` `:87`, pid by `pgrep -f "^claude --resume .* --remote-control $u|^claude --remote-control $u"` `:88`, `CLAUDE NOT RUNNING` `:89`, waiting lines `:91`, then `:93-97`.
  - `~/.claude/sessions/<pid>.json` — one live-session record per file (other files sit beside them; glob `*.json`). Keys used: `pid` (int), `sessionId` (str), `name` (str), `messagingSocketPath` (str; sockets under `/run/user/<uid>/cc-socks/` or `/tmp/cc-socks/`), `updatedAt` (int, epoch ms). One `sessionId` can sit in two live records; `name` can differ from the tmux session name.
  - `commands/clippy.md:651` — launches `progress_timer.sh` (WaveSupervision `:645-704`), so that script stays.
  - `settings.json` — the showrunner's file in this lane: allow `showrunner_timer.sh` `:54`, `verify.sh` `:55`, `progress_timer.sh` `:56` (stopgap, 4830f54); Stop hook entry for `stop-delegate-progress-timer.py` `:190-193`.
  - `/etc/nixos/modules/common/options.nix` — `job` submodule `:49-129`: `description` `:51`, `script` `:55-62` (bash, user's PATH), `runAtLogin` `:63`, `needsDesktop` `:68`, `intervalSeconds` `:78-89` (Linux: after the previous run finished, `OnUnitInactiveSec`; Mac: `StartInterval`, after it started), `dailyAt` `:90-99`, `watchPaths` `:100-109`, `keepAlive` `:110-118`, `ignoreFailure` `:119-127`; `jobs` `:225-229`; assertions `:304-328` (a trigger is required `:315-321`).
  - `/etc/nixos/modules/linux/nate-backend.nix` — `jobPath` `:40` (PATH holds zsh, python3, `~/.local/bin/claude`); `seconds n` `:44`; service `:53-63` (oneshot, `Environment = [ "PATH=${jobPath}" ]` `:60`); timers `:65-81`, interval block `:76-79` (`OnStartupSec`, `OnUnitInactiveSec`), `dailyAt` `:80`.
  - `/etc/nixos/modules/darwin/nate-backend.nix` — `launchd.user.agents` `:80-111`: label `org.nixos.<name>`, `ProgramArguments` `:82`, `RunAtLoad` `:83`, `EnvironmentVariables` with `PATH = jobPath` `:88-92`, logs `~/Library/Logs/nate-jobs/<name>.log` `:93-94`, `StartInterval` `:97`.
  - `/etc/nixos/modules/common/default.nix:7-32` — the import list for `modules/common`.
  - `/etc/nixos/modules/linux/disk-floor.nix:18` — the closest model: an `intervalSeconds` job running `"$HOME/.claude/scripts/lib/py" …`.
- **Test lanes:** `scripts/message/` → `scripts/message/test_*.py`; `scripts/delegate/` → `scripts/delegate/test_*.py`; `scripts/hooks/`, `scripts/production/`, `commands/`, `/etc/nixos` → none.
- **Build:** `zsh -n <each changed zsh script>`; `bash -n <each changed bash script>`; in `/etc/nixos`, `check` (nixfmt, deadnix, both hosts' derivations; no sudo, README:489-497).
- **Test:** from `~/.claude`: `scripts/lib/py -m unittest discover -s scripts/message -p 'test_*.py'` and `scripts/lib/py -m unittest scripts.delegate.test_progress_history scripts.delegate.test_delegate_check` (add `-k <pattern>` to filter).
- **Lint:** from `~/.claude`: `basedpyright <each changed .py file>` reports 0 errors, 0 warnings, 0 notes.
- **Invariants:**
  - Every script runs on Linux and the Mac: no GNU-only `sed -i` or `0,/re/`; edit state files in zsh and replace them whole (temp file + `mv`). Python runs only through `scripts/lib/py`.
  - Never `${PIPESTATUS[0]}`: zsh's `pipestatus` is lowercase and 1-indexed; prefer `setopt pipe_fail` / `set -o pipefail`.
  - basedpyright: zero errors, zero warnings. No file-level ignores; no `Any`; `TypedDict` for JSON records; a line-level `# pyright: ignore[rule]` only as a last resort. Repo style: `from __future__ import annotations`, `_ =` for discarded returns. `uv pip install` only.
  - Hooks never fail the turn and skip subagents.
  - Run delegate scripts with `dangerouslyDisableSandbox: true` (`delegate.md:180-183`).
  - Read `commands/succinct_style.md` before editing a command file. The forbidden-words hooks check code, comments and prose.
  - This lane edits no `settings.json` (showrunner decision 2, user-approved 2026-10-04): each checkpoint notice names the exact edits and the showrunner makes and pushes them.
  - The user runs `rebuild` on each machine; no session runs `rebuild`, `nixos-rebuild switch` or sudo (nixos README:34). The checkpoint notice carries the rebuild for the showrunner to relay.
  - In `~/.claude`, commit only this lane's files on `main`, never `git stash`, push after each checkpoint (`commands/unit/delegate_checkpoint.md` is another session's uncommitted edit). In `/etc/nixos`, `git add -N` a new `.nix` file before any eval (a flake ignores untracked files), commit with the paths named (`git commit -m … -- <paths>`), push at once, and leave `docs/inbox/natedev.md` (another session's) alone.
  - `progress_timer.sh` stays: `commands/clippy.md:651` launches it.
  - Times shown to the user are in the machine's local zone, with UTC beside them where today's logs show it.

## Phases

<!-- PHASE NUMBERING — binds every command that edits this doc.
     N is a bare integer, numbered from 1, contiguous, in execution order.
     Never a letter suffix (`4a`, `2b1a`) — those stop sorting and make ranges
     unreadable. Any spine edit (insert, split, merge, reorder, delete)
     resequences from the edit point to the end AND updates every cross-reference:
     substitute highest-first so `Phase 8` does not corrupt `Phase 12`, and
     re-check that each `Phases X–Y` range still spans the same set.
     Insert after the last `done` phase where possible — a done phase's number
     is baked into its checkpoint commit message and cannot be rewritten; if one
     must be renumbered, add an old→new mapping note to the doc.
     Full procedure: /plan:to_phased_plan → <PhaseNumbering/>. -->

### Phase 1 — Notifier, tick job and the showrunner's updates  · status: done

#### As-built

- `notifier.sh` (zsh, no systemd or launchd calls) sends a command or a prompt file to one Claude session every N minutes per named instance (`^[A-Za-z0-9][A-Za-z0-9._-]*$`). Verbs: `new <instance> --to <target> --every <min> (--command <text> | --prompt-file <path>) [--from <sender>] [--check <cmd>] [--hold] [--timeout <s>]`, `start|stop|status|fire|restart|remove|health <instance>`, `interval <instance> <min>`, bare `status`, and `tick`.
- Per instance under `$NOTIFIER_STATE_DIR/<instance>/`: `conf` (`TARGET`, `EVERY`, `COMMAND` or `PROMPT_FILE`, `FROM`, `CHECK`, `HOLD`, `TIMEOUT` default 120 s), `state` (`ENABLED`, `NEXT_DUE`, `LAST_SENT`, `LAST_RESTART`, `LAST_TARGET`; every edit a read-modify-write under `zsystem flock`) and `fire.log` (`<stamp> | exit <rc> | to …`, `<stamp> | skip <reason>`, `<stamp> | hold released: …`). `notifier.log` and `.last_tick` sit at the root, and `tick` creates the root only when absent.
- Every schedule write sets `NEXT_DUE = now - now % 60 + EVERY*60`. `new` refuses an empty `--command`/`--prompt-file` (exit 2); on an existing instance it rewrites `conf` and leaves `state` alone, so a resume retargets without moving the clock or re-enabling a stopped instance.
- `tick` takes `.tick.lock` without waiting, writes `.last_tick`, and runs each enabled, due instance in a background subshell: claim the slot, run the check under a `zselect` watchdog bounded by `TIMEOUT` (killed → `skip check timeout`), resolve the socket (none → `skip session not running`), apply the hold, write `LAST_SENT`/`LAST_TARGET` before the send, then send through `send.py --to uds:<socket> --key notifier-<instance>`. `fire` runs the same steps now, ignoring `NEXT_DUE` and the hold.
- `sessions.py` matches the target among `$NOTIFIER_SESSIONS_DIR/*.json` first, then requires a live pid (`PermissionError` alive, `OverflowError` dead) and an existing socket; the newest `updatedAt` wins; no match exits 1, a usage error 2.
- `/showrunner:produce`, `/showrunner:dailies` and `/showrunner:interval` run the updates through `NOTIFIER` = `zsh ~/.claude/scripts/message/notifier.sh` and `UPDATES` = `showrunner-<slug>`, created with `--prompt-file PROMPT_FILE --from showrunner-timer-<slug> --check "zsh …/production_check.sh <doc>"` and no `--hold`. A typed dailies runs `restart`, Wrap runs `remove`, and `/showrunner:interval` edits the doc's `**Updates:**` line and the `every N minutes` in PROMPT_FILE before `interval`. `showrunner_timer.sh` still exists; no showrunner command references it.

**Files:**
- `scripts/message/notifier.sh` — the notifier.
- `scripts/message/sessions.py` — session id or name → live socket; pid or name → session id.
- `scripts/production/production_check.sh` — production doc status as an exit code.
- `commands/showrunner/{produce,dailies,interval}.md` — updates through the notifier instance.
- `scripts/message/test_notifier.py`, `scripts/message/test_sessions.py` — CLI tests driven through the `NOTIFIER_*` variables.
- `/etc/nixos/modules/common/options.nix` — `accuracySeconds` (Linux only; asserts `intervalSeconds` or `dailyAt`).
- `/etc/nixos/modules/linux/nate-backend.nix` — `AccuracySec` from `accuracySeconds`.
- `/etc/nixos/modules/common/session-notifier.nix` — the job, imported in `modules/common/default.nix`.

**Binds later work:**
- The CLI verbs above; `restart`, `start`, `interval`, `fire` and a fresh `new` print exactly one `next_due=<epoch> (<local time>)` line.
- `health <instance>` exits 1 with `failing: no instance`, `failing: no tick since …` (`.last_tick` absent or older than 120 s) or `failing: last two sends exit a, b`; otherwise 0 with `ok` or `ok: stopped`.
- Exit codes: 0 done; 1 no such instance, or `health` failing; 2 usage error or refused.
- Check exit 0 sends, 2 removes the instance (logged in `notifier.log`), any other nonzero skips; the check must finish within `TIMEOUT`.
- `--hold`: while `LAST_SENT > LAST_RESTART`, ticks log `skip hold`, released when the socket changes or two intervals elapse.
- Environment: `NOTIFIER_STATE_DIR`, `NOTIFIER_SESSIONS_DIR`, `NOTIFIER_SEND` (run in place of `send.py` with the same arguments), `NOTIFIER_NOW_EPOCH` (the clock).
- `sessions.py socket <session:id|name>` and `id <pid|name>`; `production_check.sh <doc>` exits 0 running, 2 wrapped, 1 otherwise.
- Instance names: `showrunner-<slug>` and `delegate-<run id>`.
- `nate.jobs.session-notifier` runs `notifier.sh tick` every 15 s (AccuracySec 1 s on Linux, launchd StartInterval 15 on the Mac) and ticks only after the user rebuilds; the `accuracySeconds` option lives in `/etc/nixos/modules/common/options.nix`.

**Gotchas:**
- basedpyright exits 3 in `~/.claude` because `pyrightconfig.json` names a missing `.venv`; read its `0 errors, 0 warnings, 0 notes` line, not the exit code.
- A watchdog uses the builtin `zselect`, never an external `sleep`, so it dies with its subshell.
- No GNU `sed -i`: it fails on the Mac.

**Ruled out:**
- GNU `timeout` for the check — absent on the Mac.
- A systemd timer per instance — one 15 s job ticks every instance.

### Phase 2 — Unit plumbing  · status: done

#### As-built

- `scripts/hooks/delegate_run.py`: `ACTIVE_DIR` reads `PLAN_DELEGATE_ACTIVE_DIR` (default `/tmp/claude/delegate/active`). `running_work(session_dir: Path) -> str | None` labels work in flight: an `impl_status` or `impl_status_*` file reading `implementing`, a `review_status*` file reading `reviewing`, or a `progress_history_state.json` activity with status `active`; otherwise `None`. `check(claude_session_id: str, session_dir: Path) -> int` returns 2 when the marker is missing or names another dir, 0 with running work, 1 when idle. CLI `delegate_run.py check <claude_session_id> <session_dir>` exits with that code; a usage error exits 3.
- `scripts/delegate/unit_notifier.sh <claude_session_id>` (zsh) reads the marker into `SESSION_DIR`, takes `run_id = ${SESSION_DIR:t}` and repo `${SCRIPT:h:h:h}`, then `exec`s `notifier.sh new delegate-<run_id> --to session:<id> --every <min> --command /unit:delegate_report --check "<repo>/scripts/lib/py <repo>/scripts/hooks/delegate_run.py check <id> <SESSION_DIR>" --hold`. It exits 1 on a missing or empty marker, 2 on a usage error, and otherwise with `notifier.sh`'s code.
- `scripts/delegate/end_session.sh` runs `notifier.sh remove delegate-<run id>` (path relative to the script, errors ignored) before it removes the marker.
- `scripts/delegate/progress_history.py`: `_restart_unit_notifier(session_dir: Path) -> int | None` runs `zsh notifier.sh restart delegate-<run id>` on every `progress` call (10 s timeout, `PLAN_DELEGATE_NOW_EPOCH` copied to `NOTIFIER_NOW_EPOCH`) and parses `^next_due=(\d+)`; any failure gives `None`. `_next_report_at(session_dir, now, notifier_due)` prefers that value, then the `progress_timer` marker, then the interval.

**Files:**
- `scripts/hooks/delegate_run.py` — the `ACTIVE_DIR` override, `running_work()`, `check()`, the `check` CLI.
- `scripts/delegate/unit_notifier.sh` — makes or retargets the unit's instance.
- `scripts/delegate/end_session.sh` — removes the instance at run end.
- `scripts/delegate/progress_history.py` — restarts the instance on each report; clock line from `next_due`.
- `scripts/delegate/test_delegate_check.py` — `check` exit codes and the `unit_notifier.sh` conf, by subprocess.
- `scripts/delegate/test_progress_history.py` — `test_the_clock_line_uses_the_restarted_unit_notifier`; helper `run_unowned_command` clears an ambient `PLAN_DELEGATE_TEAM_ROLE`.

**Binds later work:** `unit_notifier.sh <claude_session_id>` writes a conf with `TARGET=session:<id>`, `EVERY=<minutes>`, `COMMAND=/unit:delegate_report`, `HOLD=1` and a `CHECK` naming `delegate_run.py check <id> <dir>`; it exits 1 with no marker and 2 on a usage error. `check` exits 0 with running work, 1 idle (the tick skips), 2 when the run ended or a new run replaced it (the tick removes the instance), 3 on a usage error (the tick skips). `end_session.sh` removes `delegate-<run id>`, and `notifier.sh remove` inherits its environment, so `NOTIFIER_STATE_DIR` sandboxes it. Each `progress` call restarts the instance, and the report's clock line names the instance's `next_due`; with no instance it falls back to the `progress_timer` marker, then the interval. Interval: the leading digits of `PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS` in `${PLAN_DELEGATE_CONFIG:-$HOME/.claude/config/delegate.conf}`, so a trailing `# comment` keeps the value; an unreadable file or missing key gives 900 s; minutes round up, at least 1. `PLAN_DELEGATE_ACTIVE_DIR` is a test override honoured only by `delegate_run.py` and `unit_notifier.sh`; `prepare_session.sh` and `end_session.sh` use the fixed `/tmp/claude/delegate/active`, so a live caller must leave it unset or pointed where the marker is written.

**Gotchas:** every `CHECK` word is `(q)`-quoted, so a repo path with spaces survives `notifier.sh`'s `(Q)(z)` split. `progress` spawns one zsh per call even for a run with no instance. `check` runs under the notifier's check watchdog (instance `TIMEOUT`, default 120 s) and stays a quick file read. `stop-delegate-progress-timer.py` keeps its own copy of the running-work logic, without the `impl_status_*` match. Tests that run `progress_history.py` commands must clear an ambient `PLAN_DELEGATE_TEAM_ROLE`, or they fail when the suite runs under a team role.

**Ruled out:** honouring `PLAN_DELEGATE_ACTIVE_DIR` in `end_session.sh`, since `prepare_session.sh` never writes a marker there; removing a stale run's instance in `check`, since a unit parked overnight on the user keeps its instance (matching marker, no running work, exit 1).

### Phase 3 — Units on the notifier  · status: done

#### As-built

- `scripts/delegate/prepare_session.sh`, when `CLAUDE_CODE_SESSION_ID` is set, writes the marker under the fixed `ACTIVE_DIR=/tmp/claude/delegate/active`, then runs `PLAN_DELEGATE_ACTIVE_DIR=$ACTIVE_DIR zsh unit_notifier.sh <id>` (pinned, so an inherited test variable cannot point it at another marker). It prints the `next_due=…` line, or `notifier instance not created: <output>` and carries on; `Session ready at <dir>` stays the last line. A Codex run has no id and gets no instance; its poll timeout is its tick.
- `commands/unit/delegate.md` arms no timer: `PROGRESS_UPDATES_ENABLED`, `PROGRESS_TIMER_HANDLE` and every `progress_timer.sh` clause are gone. `<ProgressContract>`: the interval key sets the Codex poll timeout and the Claude instance interval; `<PrepareSession>` creates `delegate-<run id>` (run id = basename of `SESSION_DIR`), `end_session.sh` removes it; the notifier's `/unit:delegate_report` from `delegate-<run id>` is the Claude tick; each `progress_history.py progress` call restarts the clock and `--hold` keeps at most one tick waiting. On `notifier instance not created` the unit says the run gets no ticks until `unit_notifier.sh "$CLAUDE_CODE_SESSION_ID"` succeeds. User stops updates → `notifier.sh stop delegate-<run id>`, resumes → `start`. A tick never replaces the completion report.
- `commands/unit/delegate_report.md`: `<ProgressReport/>` step 1 counts a dispatch, a background verification or an open activity as running work, so ticks during `verify.sh final` and UX review still report; ticks arriving during a report, or several together, get one report.
- `commands/unit/interval.md`: `/unit:interval [minutes]` resolves `delegate-<run id>` from `/tmp/claude/delegate/active/$CLAUDE_CODE_SESSION_ID`; no argument runs `notifier.sh status`, a positive integer runs `notifier.sh interval` (exit 2 = refused) and reports the next tick from `next_due` in local time. Unavailable on a Codex unit.
- `scripts/production/unit_status.sh`, for a unit with a pid: `sessions.py id <pid>` → marker → `SESSION_DIR`; when `delegate_run.py check` exits 0 and `notifier.sh health delegate-<run id>` exits 1, it prints `TICKS FAILING (<health line without "failing: ">)`. No marker prints nothing.

**Files:**
- `scripts/delegate/prepare_session.sh` — creates the instance.
- `commands/unit/{delegate,delegate_report}.md` — ticks come from the notifier.
- `commands/unit/interval.md` — `/unit:interval`.
- `scripts/production/unit_status.sh` — `TICKS FAILING`.
- `config/delegate.conf`, `config/README.md`, `docs/as-built/plan-delegate-progress-history.md`, `docs/delegate/{write_prompt_contract,dual_review}.md` — the notifier instance in place of the progress timer.
- `scripts/production/showrunner_timer.sh` — deleted; no tracked reference remains outside `settings.json` and `docs/plans/`.

**Binds later work:** `prepare_session.sh`'s last line is `Session ready at <dir>`, from which the unit director reads `SESSION_DIR`; a `next_due=` or `notifier instance not created:` line comes before it. The showrunner reads `TICKS FAILING (<reason>)` from `unit_status.sh`; a live unit not yet moved to the notifier reads `TICKS FAILING (no instance)` while it has running work.

**Gotchas:** a seat can inherit the unit director's `CLAUDE_CODE_SESSION_ID`, so tests run `prepare_session.sh` and `end_session.sh` only with a fresh id and a temp `NOTIFIER_STATE_DIR`. `unit_notifier.sh` reads `0` as 1 minute and only a missing or non-numeric key as 900 s; Codex requires a positive integer. Until `settings.json` drops the `stop-delegate-progress-timer.py` Stop hook (a showrunner edit), that hook can block the turns of a unit that arms no timer.

**Ruled out:** printing `next_due` after `Session ready at <dir>`, since the unit director reads `SESSION_DIR` from the last line; removing `<CompactionContract>`'s Stop-hook sentence, since it describes `stop-delegate-continue.py`, not the progress-timer hook.
