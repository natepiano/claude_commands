# Session notifier

## What it is

The session notifier sends a message to a live Claude session on a schedule: the scheduled-update prompt (a `/showrunner:dailies gantt` report) to a showrunner, and `/unit:report` ticks to a `/unit:direct` unit director. Each schedule is a named **instance** stored on disk. One declared 15 s job runs `notifier.sh tick` on both machines and sends every instance that is due. Before each send, the instance's own check command decides whether to send, skip or remove the instance. No agent arms or re-arms a timer. The schedule lives outside the session, so it keeps going through ended turns, compaction and restarts, and it works the same on Linux (natedev) and the Mac.

### Key files

| File | Role |

| --- | --- |

| `scripts/message/notifier.sh` | The notifier: instance state, every CLI verb, and `tick`. zsh; its only systemd or launchd call is `launch_run`, which starts a run-only job. |

| `scripts/message/sessions.py` | `socket <session:id\|name>` gives a live session's socket; `id <pid\|name>` gives its session id. `read_session(path) -> SessionRecord | UnreadableSessionRecord` keeps an unreadable registry entry distinct from no live match. |

| `scripts/message/send.py` | Delivery. A `--to uds:<socket>` send to a live registered session goes through the postmaster outbox first when its heartbeat is fresh, and otherwise through a headless `claude -p` relay whose one job is a `SendMessage` call (`docs/as-built/session-roster.md`). |

| `scripts/production/production_check.sh` | The showrunner instance's check: the production doc's status as an exit code. |

| `scripts/production/unit_status.sh` | The showrunner's per-unit status script; prints `TICKS FAILING (…)`. |


| `scripts/direct/prepare_session.sh` | Run start: writes the run-active marker, then creates the instance. |

| `scripts/direct/end_session.sh` | Run end: removes the instance, then the session's own marker. |

| `scripts/hooks/direct_run.py` | `check` CLI: the unit instance's check. Also a library the worker hooks import. |

| `scripts/direct/progress_history.py` | `progress` restarts the unit's instance on every call, refused ones included, and names its next tick in the report's clock line. |

| `scripts/production/dailies_input.py` | `--user-run` restarts the showrunner instance for a dailies the user runs. |

| `scripts/production/production_lifecycle.py` | `wrap` removes the showrunner instance. |

| `scripts/shutdown/settle.py`, `scripts/shutdown/restart.py` | `/shutdown` runs `stop` on each enabled instance targeting a session it stops; `/shutdown cancel` and `/shutdown restart` run `start` on those. Never `remove`. |

| `commands/showrunner/{produce,dailies,interval}.md` | Call those commands; `/showrunner:report_interval` retimes the showrunner instance. |


| `config/direct.conf` | `UNIT_PROGRESS_INTERVAL_SECONDS`, the Codex unit poll timeout. |

| `/etc/nixos/modules/common/session-notifier.nix` | The 15 s job. |

| `scripts/message/test_notifier.py`, `test_sessions.py`, `scripts/direct/test_worker_check.py` | CLI tests, run through the `NOTIFIER_*` variables. |

| `scripts/direct/test_progress_history.py` | The `progress` restart, refused calls included (missing `--cap-stage`, no active phase): `LAST_RESTART` at the call time and `NEXT_DUE` one interval later. |

### Instance state

The root is `$NOTIFIER_STATE_DIR`, default `~/.local/state/notifier`. Each instance is a directory named `^[A-Za-z0-9][A-Za-z0-9._-]*$` holding:

- `conf`: `TARGET` (`session:<id>` or a session name), `EVERY` (minutes), `COMMAND` or `PROMPT_FILE` (absolute), or `RUN` (a command line: a run-only instance, which has no `TARGET` and sends nothing), `FROM` (sender name, default the instance name), `CHECK` (a command line, may be empty), `HOLD` (0/1), `TIMEOUT` (seconds, default 120).
- `state`: `ENABLED`, `NEXT_DUE`, `LAST_SENT`, `LAST_RESTART`, `LAST_TARGET` (the socket of the last send).
- `lock`: the instance's flock. Every read-modify-write of `conf` or `state` holds it, and each file is rewritten whole (temp file, then `mv`).
- `fire.log`: one line per attempt, stamped in local time with UTC beside it: `<stamp> | exit <rc> | to <target> uds:<socket> | <send.py output>`, `<stamp> | skip <reason>` (`check timeout`, `check exit <rc>`, `session not running`, `session lookup exit <rc>`, `hold`), `<stamp> | hold released: socket changed|two intervals`, or, for a run-only instance, `<stamp> | run timeout` or `<stamp> | run exit <rc>` (a clean run logs nothing; its output replaces `run.log`).

At the root: `.tick.lock`, `.last_tick` (epoch of the latest tick) and `notifier.log` (instance removals and lock or tick failures). `tick` creates the root only when it is absent.

Environment variables the tests set: `NOTIFIER_STATE_DIR`, `NOTIFIER_SESSIONS_DIR` (default `~/.claude/sessions`), `NOTIFIER_SEND` (run in place of `send.py`, same arguments), `NOTIFIER_NOW_EPOCH` (the clock).

### The tick job

`nate.jobs.session-notifier` runs `exec zsh "$HOME/.claude/scripts/message/notifier.sh" tick` every 15 s. On natedev it is a systemd user timer with `OnUnitInactiveSec` 15 s (counted from the end of the previous run) and `AccuracySec` 1 s, from the job option `accuracySeconds`. On the Mac it is a launchd user agent with `StartInterval` 15 (counted from the start of the previous run).

### One tick

```
job (15 s) → notifier.sh tick
  take .tick.lock without waiting (held → exit 0), write .last_tick
  for each instance with ENABLED=1 and now ≥ NEXT_DUE (a run-only instance: launch_run, below), in a background subshell:
    lock; claim the slot (NEXT_DUE = next one); unlock
    CHECK under a watchdog          → 0 go on, 2 remove instance, other skip
    sessions.py socket TARGET       → none: skip "session not running"; records unreadable
                                      (exit 3): skip "session lookup exit 3"
    hold (HOLD=1 only)              → skip, or release and go on
    record LAST_SENT, LAST_TARGET
    send.py --to uds:<socket> --from FROM --key notifier-<instance>
            --summary 'scheduled update' --timeout TIMEOUT (--text COMMAND | --file PROMPT_FILE)
    append the outcome to fire.log
  wait for every subshell, release .tick.lock
```

A run-only instance (`RUN`) is not run inside the tick. `launch_run` starts `notifier.sh run-due <name>` in its own transient service (`systemd-run --user --collect --quiet --no-block --unit session-notifier-<name>-<pid>-<random>` on Linux; on the Mac a `launchctl submit` job that runs `run-due-and-remove`, which removes its own label). The Mac job is then started with `launchctl kickstart gui/<uid>/<label>`: on macOS 27 a submitted job is only registered and never runs on its own (measured 2026-10-16). With neither launcher, or when the Mac kickstart fails (the tick then removes the label), the tick logs `run launcher unavailable: <name>; waiting after tick` to `notifier.log` and runs the job once it has released `.tick.lock`. The job claims its slot, runs `RUN` with output to `<instance>/run.log` under the `zselect` watchdog (TERM at `TIMEOUT`), and logs only `run timeout` or `run exit <rc>`. A background child of the tick would die with the tick's cgroup or launchd job.

The slot is claimed before the check runs, so a concurrent tick or `fire` cannot send the same slot twice, and a skip uses up its slot: the next try is one interval later. The check runs as a background process with a `zselect` watchdog that kills it after `TIMEOUT` seconds. `TIMEOUT` is also the relay's delivery limit.

### Delivery

`sessions.py socket` reads every `*.json` record in the sessions directory, matches `session:<id>` against `sessionId` or a bare name against `name`, keeps records whose pid is alive (`PermissionError` counts as alive) and whose `messagingSocketPath` is a socket, and prints the socket with the newest `updatedAt`. `sessions.py address` takes the same targets and prints that socket as `uds:<socket>`, the `to` that `SendMessage` accepts for a session on this machine. No match exits 1; a usage error exits 2; a registry that cannot be listed, or an unreadable entry with no live match, exits 3. The socket is resolved on every send, so a restarted process is found at its new socket. `send.py` then relays through a headless `claude -p` (sonnet) that makes one `SendMessage` call to that socket. A `COMMAND` is sent as text; a `PROMPT_FILE` is read at send time, so editing the file changes the next tick's text. The key `notifier-<instance>` means a tick that could not be delivered (`QUEUED`, exit 1) replaces the previous queued one, so each instance has at most one tick in `send.py`'s queue.

### CLI

`zsh ~/.claude/scripts/message/notifier.sh <verb> …`. Exit codes: **0** done; **1** no such instance, or `health` failing; **2** usage error or refused.

| Verb | What it does |
| --- | --- |
| `new <instance> --every <min> (--run <cmd> \| --to <target> (--command <text> \| --prompt-file <path>)) [--from <sender>] [--check <cmd>] [--hold] [--timeout <s>]` | `--run` makes a run-only instance that sends nothing; it takes none of `--to`, `--from`, `--check`, `--command`, `--prompt-file`, `--hold` (usage error, exit 2), and `status` shows its command in place of a target. Fresh instance: writes `conf` and `state`, enabled, prints `next_due`. Existing instance: rewrites `conf` only, prints nothing, so the clock and `ENABLED` stay as they were. Refuses (2) an empty command or prompt file, both or neither, `--every` or `--timeout` not a whole number above 0, a relative prompt path. |
| `start <instance>` | `ENABLED=1`, schedules from now, prints `next_due`. |
| `resume <instance>` | `ENABLED=1`, keeps `NEXT_DUE` unchanged, and prints `next_due`. A missed slot runs on the next tick; a future slot runs at its existing time. |
| `stop <instance>` | `ENABLED=0`; `NEXT_DUE` stays. |
| `pause <instance>` | For the conversation pause: an enabled instance gets `ENABLED=0` and `PAUSED=1`; a stopped one is left alone. Prints nothing. |
| `unpause <instance>` | Only while `PAUSED=1` is there: `ENABLED=1`, `NEXT_DUE` unchanged, prints `next_due`. Otherwise changes nothing and prints nothing. `start`, `resume`, `stop` and `restart` clear the mark, so a timer someone stopped or started during a pause stays as they left it. |
| `restart <instance>` | `LAST_RESTART=now`, schedules from now, prints `next_due`. Leaves `ENABLED` alone. |
| `interval <instance> <min>` | Sets `EVERY`, then does what `restart` does. Bad minutes exit 2 before the instance lookup. |
| `fire <instance>` | Runs one tick now, ignoring `ENABLED`, `NEXT_DUE` and the hold. Still runs the check and needs a live socket. Prints `next_due` (the clock moves). Exits 0 whatever the send outcome; the outcome is in `fire.log`. |
| `remove <instance>` | Deletes the instance directory. Exits 0 when it is already gone. |
| `status <instance>` | Target, interval, enabled or stopped, `next_due`, `last_sent`, `last_restart`, `last_tick`, last five `fire.log` lines. |
| `status` | One line per instance (`<name> every N min next HH:MM enabled\|stopped`), then `last_tick`. |
| `health <instance>` | Exit 1 with `failing: no instance`, `failing: no tick since <time\|never>` (`.last_tick` missing or older than 120 s), or `failing: last two sends exit a, b` (the last two `exit` lines both nonzero). Otherwise exit 0 with `ok`, or `ok: stopped` for a stopped instance. |
| `tick` | The job's verb. Always exits 0. |

`restart`, `start`, `resume`, `interval`, `fire` and a fresh `new` print exactly one line `next_due=<epoch> (<YYYY-MM-DD HH:MM TZ>)`, in local time.

### The check contract

`CHECK` is split with zsh `(Q)(z)` and run as a command. Its exit code decides the tick:

- **0**: send.
- **2**: the owner is gone. The tick deletes the instance and logs `removed <name> (check exit 2)` in `notifier.log`.
- **any other nonzero**: skip, logged as `skip check exit <rc>`.
- Not finished within `TIMEOUT`: killed, logged as `skip check timeout`.

`production_check.sh <doc>` reads the doc's `Status: PRODUCTION — <word>` line: `running` 0, `wrapped` 2, anything else (or no such line, or no file) 1. For a running production it first runs `log_backup.py <doc>`, which keeps dated copies of the production's LOG (LOG backups in `docs/as-built/showrunner-automation.md`). A failed copy prints to stderr and does not change the exit code.

`direct_run.py check <claude_session_id> <session_dir>` exits 2 when the marker `/tmp/claude/unit/active/<id>` (or `/tmp/claude/delegate/active/<id>` for a run started before the folder rename) is missing or names another directory (the run ended, or a new run in the same session replaced it); 0 when `running_work()` finds work in flight; 1 when the unit is idle; 3 on a usage error. Work in flight is an `impl_status` or `impl_status_*` file reading `implementing`, a `review_status*` file reading `reviewing`, or a `progress_history_state.json` activity with status `active`. That last one covers `verify.sh final` and UX review, which open activities.

### Hold and restarting the clock

Every schedule write sets `NEXT_DUE = now - now % 60 + EVERY * 60`: the next whole minute boundary one interval out. `restart` and `interval` also set `LAST_RESTART=now`.

With `HOLD=1`, a tick skips (`skip hold`) while `LAST_SENT > LAST_RESTART`, that is, while a sent tick has not yet been answered by a clock restart. The hold releases when the session's socket differs from `LAST_TARGET` (a new process: the waiting tick went with the old one) or when two intervals have passed since `LAST_SENT`. `fire` ignores the hold.


### The showrunner instance

`/showrunner:produce` owns `UPDATES` = `showrunner-<slug>`, where `<slug>` is the production doc's file name less `-production.md`. A production rename (`rename.py`, `docs/as-built/production-rename.md`) renames the instance directory with the doc and points its `CHECK` at the new doc. In `<StartUpdates>`, at start and on every resume, it runs `update_registration.py register --production <doc> --session <name>`, which writes the filled scheduled-update prompt to `PROMPT_FILE` (`~/.local/state/showrunner/<slug>/prompt.txt`) and runs:

```
notifier.sh new showrunner-<slug> --to session:$CLAUDE_CODE_SESSION_ID --every <N> \
  --prompt-file <PROMPT_FILE> --from showrunner-timer-<slug> \
  --check "zsh $HOME/.claude/scripts/production/production_check.sh <absolute doc path>"
```

N comes from the doc's `**Updates:** every N minutes` line, 15 when absent; `on the hour` in that line adds `--aligned`. There is no `--hold`. A repeated `new` on resume retargets the instance to the current session without moving the clock. The instance outlives the session. A tick arrives as a message from `showrunner-timer-<slug>` whose text starts `Scheduled update`; the showrunner treats it as the scheduled prompt and does not reply. A `/showrunner:dailies` the user types saves the `unit_status.sh` output and runs `dailies_input.py --user-run`, which checks the whole input, then runs `restart`, takes `next_due` for the report and appends it to `LOG`, so the next tick is N minutes after that report; a scheduled tick runs the builder without `--user-run` and restarts nothing. Every reply's `next dailies` time is read from `notifier.sh status`. `production_lifecycle.py wrap` runs `remove`, and the check removes the instance on its own once the doc says `wrapped`.

### Unit reports

A unit run has no notifier instance (user, 2026-10-19): `/unit:report` runs only when someone asks. All 12 `worker-*` instances had been `ENABLED=0` since 2026-10-12 22:04 EDT and were removed. `prepare_session.sh` creates none and `end_session.sh` removes none. A Codex unit's poll timeout composes no report; it only gives `<EarlyReviewArm/>` a point to run.

### Changing the interval

- `/showrunner:report_interval [minutes]` edits the doc's `**Updates:**` line and the `every N minutes` text in `PROMPT_FILE`, runs `notifier.sh interval UPDATES <minutes>`, commits the doc, logs the old and new interval, and tells the user the next tick in `ZONE` and UTC. No argument reports status only.
### TICKS FAILING

For each unit with a live Claude, `unit_status.sh` takes its session id from `unit_lookup.py list` (column 6), reads the marker `<session id>` from `UNIT_ACTIVE_DIR` when set, else the first of `/tmp/claude/unit/active` and `/tmp/claude/delegate/active` that holds one, and, whenever the marker names a session directory, runs `zsh notifier.sh health <instance>` (`unit-<run id>`, or `worker-<run id>` for a directory under `/tmp/claude/delegate/`; run id = that directory's basename), idle or working. Health exit 1 prints `TICKS FAILING (<health line without "failing: ">)`: `no instance`, `no tick since <time>`, or `last two sends exit a, b`. No marker, or an empty one, prints nothing. The showrunner reads this line in every scheduled update and typed dailies.

## Invariants

- Every script runs on Linux and the Mac: no GNU-only `sed -i` or `0,/re/`, no GNU `timeout`. Clock, locks and waits use zsh modules (`zsh/datetime`, `zsh/system` `zsystem flock`, `zsh/zselect`). Python runs only through `scripts/lib/py`, never `python3`.

- Never `${PIPESTATUS[0]}`; use `setopt pipe_fail` or `set -o pipefail`. `prepare_session.sh` and `end_session.sh` stay bash; the notifier scripts are zsh.

- `notifier.sh` makes systemd or launchd calls only in `launch_run`, to start a run-only job in its own service. Only the declared nix job runs `tick`. A change to the job takes effect when the user runs `rebuild` on each machine; no session runs `rebuild`, `nixos-rebuild switch` or sudo.

- Every edit of `conf` or `state` is a read-modify-write under the instance's flock, written whole through a temp file and `mv`.

- A slot is claimed (`NEXT_DUE` advanced) before the check or send runs.

- Every schedule write sets `NEXT_DUE = now - now % 60 + EVERY * 60`.

- `new` on an existing instance rewrites `conf` and leaves `state` alone.

- `restart`, `start`, `resume`, `interval`, `fire` and a fresh `new` print exactly one `next_due=<epoch> (<local time>)` line; `progress_history.py` and `prepare_session.sh` parse it.

- CLI exit codes: 0 done, 1 no such instance or `health` failing, 2 usage error or refused.

- Check contract: 0 sends, 2 removes the instance, other nonzero skips, and the check finishes within `TIMEOUT`. `direct_run.py check` stays a quick file read and never treats an old run as gone.

- Every message, `--help` text, comment and docstring under `scripts/` that names a run of `/unit:direct` calls it a "unit run" (`Unit run`, `unit run`, `mid-unit-run`) (user, 2026-10-08); `phase_table.py --help` reads `Show or refresh a unit run's plan phases.` Nothing is called "delegate" (user, 2026-10-20): the directories are `scripts/direct/` and `docs/direct/`, the config is `config/direct.conf`, the variables are `UNIT_*`, the hook module is `scripts/hooks/direct_run.py`, a run's directory is the "unit session directory" (the session-start hook prints `Unit session directory:`), and the agent that receives the work is a "worker". Stored data moves to new names: the history folder is `~/.local/state/unit-history` and the run archive `usage-model/unit-archive` once `unit_runs.py move-history` moves them on each machine; the buildlog field is `unit_session`, renamed in the log files on their next index read; findings record `--caught-by worker`. Until then, and for records already written, these readers keep the old word: the `plan-delegate` and `delegate-archive` folder names, the `plan-delegate` skill in old events, the `delegate_session` field rename, `--caught-by delegate` (accepted and stored as `worker`, and counted in old history), the `delegate.` task names in the history ledger, a phase's `delegated_phase_reservation_state.json`, `/tmp/claude/delegate` and the `delegate-` notifier prefix. The last three go with the old run folder. Two more keep it for good: the old command name `/unit:delegate`, which the model study and the roles audit match in old transcripts and text, and the dated review `docs/direct_footprint_review.md`. The check: `git grep -niwE 'delegates?' -- scripts commands docs ':!docs/plans'`; any hit outside that list breaks the rule.

- **Run folders renamed (user, 2026-10-15).** A run started on or after 2026-10-15 keeps its folder in `/tmp/claude/unit/<run id>/`, its marker in `/tmp/claude/unit/active/<session id>` and its notifier instance in `~/.local/state/notifier/unit-<run id>/`. A run started before keeps `/tmp/claude/delegate/` and `worker-<run id>` until it ends; nothing moves a live run's folder, and finished runs' folders stay where they are. `scripts/lib/unit_runs.py` holds both roots, new first: every Python reader takes its roots, markers and instance names from it, and the shell readers (`unit_notifier.sh`, `end_session.sh`, `unit_status.sh`) check both folders the same way. A sweep `--root` naming either real root covers both, so one root's sweep never takes the other root's live servers for unused ones. **Removal step, on or after 2026-10-22.** The former name stops being read only in its own change, and only once no running session owns a marker under `/tmp/claude/delegate/active/`: the date alone never ends it, since runs such as two-accounts' and screenshot's stay open past it. `unit_runs.py removal-check` exits 0 when none does and 1 naming each owner (session name, id and run folder); on 2026-10-15 it named 18. The step runs it first and stops on 1, then drops `FORMER_RUN_ROOT` and the shell fallbacks, and clear the 17 stale markers that ended runs left in `/tmp/claude/delegate/active/` (dating from 2026-10-05).

- `prepare_session.sh`'s last line is `Session ready at <dir>`; the `next_due=` or `notifier instance not created:` line comes before it.

- `end_session.sh` removes the instance before the marker, ignoring errors.

- `progress` restarts the instance before it reads state or can refuse, so every call, refused ones included, restarts the clock. The restart never fails a report; any failure falls back.

- Hooks that import `direct_run.py` never fail the turn and skip subagents.

- Worker scripts run with `dangerouslyDisableSandbox: true`.

- `scripts/direct/progress_timer.sh` stays, and `settings.json` still allows it, but nothing in this repo launches it. `progress_history.py` reads its `progress_timer` marker only when one exists; a unit run's next report time comes from its notifier instance.

- Times shown to the user are in the machine's local zone, with UTC beside them where the logs show it.

## Calibration and gotchas

- **Interval cost.** Each report costs the unit director about 40 s of generation. Keep the interval at or above the median review, about 5 minutes.
- **Showrunner interval.** 15 minutes when the doc has no `**Updates:**` line.
- **On demand (user, 2026-10-10).** A doc whose `**Updates:**` line reads `on demand` gets a stopped `showrunner-<slug>` instance: `register` (`update_schedule` → `UpdatesOnDemand`) runs `new` at the default 15 minutes, then `stop`, and prints `on demand: no scheduled dailies`. The instance is kept because it is the showrunner's only record (`showrunners.py` and shutdown's inventory find showrunners through it; until 2026-10-11 `register` removed it, and every unit of an on-demand production was classed top-level). A stopped instance never fires, a conversation pause records only enabled instances, and `/shutdown` restarts only the instances it stopped itself. `new` keeps an existing instance's state, so `/showrunner:report_interval` turning an on-demand doc back to `every N minutes` runs `register` and then `start`. `dailies_input.py` runs no `status` or `restart` and leaves `next_run` out, so the footer reads `no dailies scheduled`. `UNIT_PROGRESS_UPDATES=on-demand` in `direct.conf` puts units on demand: `unit_notifier.sh` with no mode makes no `unit-<run id>` (`worker-<run id>` for a run started before the folder rename; old name read until no live marker remains) and prints `progress updates on demand: …`, which `prepare_session.sh` passes on; `on` makes the instance when there is none. The repo's `direct.conf` went back to `scheduled` the same day, hourly.
- **Quiet by default (user, 2026-10-13).** New productions get no timer unless asked; unit runs get none at all since 2026-10-19. A doc with no `**Updates:**` line is on demand too; only `every N minutes` schedules. Until 2026-10-18 `register` also turned a new production's reply footer off; the footer is gone (user: "i want footers gone for good"). `direct.conf` ships `on-demand`, and `unit_notifier.sh` reads a missing line as `on-demand`. An on-demand start that finds the run's instance runs `new` on it, which retargets it and keeps its clock and `ENABLED` (until 2026-10-13 it stopped the instance), so existing runs keep what they have. Unit directors have no reply footer.
- **Timing.** `next_due` is a whole minute; the tick that sends it lands within about 15 s after. On natedev a slow tick pushes the next one back (the interval counts from the end of the run); on the Mac an overlapping tick finds `.tick.lock` held and exits at once.
- **A skip uses its slot.** An idle unit, a missing session or a hold skips the whole interval, not 15 s.
- **Timeouts.** `TIMEOUT` (120 s default) bounds both the check and the relay. One relay takes about 9 s; a postmaster delivery about 1 s. `tick` waits for every instance's subshell before it releases `.tick.lock`, so a check must stay fast.
- **Health window.** `.last_tick` older than 120 s means the job is not running; `health` reports it even for a stopped instance.
- **Workers inherit `CLAUDE_CODE_SESSION_ID`.** A worker can inherit the unit director's id, so `prepare_session.sh` or `end_session.sh` run from a worker or a test acts on the unit director's marker and instance. Tests run them only with a fresh id and a temp `NOTIFIER_STATE_DIR`; `end_session.sh`'s `remove` inherits that variable.
- **`UNIT_ACTIVE_DIR`** is set by tests for a checkout's scripts; installed build scripts refuse to run with it set. `direct_run.py`, `unit_status.sh` and `end_session.sh` read it (unset, they read `/tmp/claude/unit/active` and then `/tmp/claude/delegate/active`). `prepare_session.sh` writes under the fixed `/tmp/claude/unit/active`, so a live build caller leaves it unset.
- **`progress` cost.** Every `progress` call, refused ones included, spawns one zsh for the restart, even for a run with no instance. Because the restart comes before any refusal, a call that exits with `No active phase to report` or a missing cap stage still moves the next tick one interval out.
- **`fire`** moves the clock and skips the hold, but a failing check or a missing session still skips it.

## Why

- **The schedule lives outside the session.** An agent that must arm a timer before ending every turn misses one sooner or later. A file-backed instance ticked by a job keeps the schedule through turn ends, compaction and restarts, and the showrunner and the units use one mechanism.

- **One job ticks every instance.** A timer per instance would need systemd on Linux and launchd on the Mac from inside the script. One declared 15 s job keeps timers out of `notifier.sh`, and the script identical on each machine; its one launcher call only detaches a run-only job after the tick has decided it is due. `AccuracySec` is 1 s because systemd's default of 1 minute would spread a 15 s tick across a minute.

- **The check decides, not the notifier.** `notifier.sh` knows nothing about productions or `/unit:direct` runs. Each owner supplies a command, and exit 2 lets an instance remove itself when its owner is gone, so a crashed run or a missed wrap stops sending on its own.

- **Unit ticks only while work runs.** An idle unit, waiting on the user or between steps, has nothing new to report, and every report costs generation time. A unit parked overnight keeps its instance, so updates resume with the work.

- **Hold for units.** A unit in a long turn cannot read ticks; without the hold they would stack and each produce a report. The socket-change release covers a restarted session, which lost its waiting tick; the two-interval release keeps a lost tick from silencing the unit for good.

- **Each report restarts the clock.** The next tick comes one interval after the latest report from any source, the report's clock line names the real next tick, and the restart releases the hold.

- **No hold for the showrunner.** A scheduled showrunner update does not restart its own clock, so a hold would skip every other tick.

- **`new` leaves state alone on an existing instance.** A resume retargets the instance without moving the clock or re-enabling updates the user stopped.

- **`zselect` watchdog.** GNU `timeout` is absent on the Mac, and an external `sleep` would outlive the subshell; the builtin dies with it. The bound matters because a hung check would hold `.tick.lock` and stall every instance.



- **`TICKS FAILING` for every active run, idle or not.** An idle unit gets no ticks by design, but its instance must be there when work resumes. Checked only while work ran, a unit with no instance dropped out of every idle status: hana's geometry-material reported `no instance` from 2026-09-29 and no status showed it.

- **A refused report still restarts the clock.** The hold keeps at most one tick waiting and releases on the next restart. A refused call that skipped the restart would leave the next slot held until the two-interval release, a 30-minute gap at the default interval.

## Production registration

Production registration creates the showrunner update and the run-only
stall-watch job. Session names come from herdr; there is no name-refresh job.
Existing retired jobs are removed by their production's showrunner.

### Other sessions' run markers (2026-10-29)

`unit_runs.marker_must_stay(path)` protects another session's marker. It uses the
marker filename as the session id and delegates to
`scripts/message/sessions.py:session_liveness`, which reads the session registry
and checks the saved process identity (or an older record's socket). A live
session or unknown liveness keeps the marker, regardless of its age or whether
its run folder still exists. Tests redirect that registry with
`NOTIFIER_SESSIONS_DIR` or patch the shared liveness observation.

`unit_runs.py removal-check` does not offer the former `delegate` folder for
removal while any marker has a live or unknown owner, including owners absent
from the initial running-session listing. Confirmed-gone owners retain the
previous behavior. The automatic temp sweep already retains both `active/`
directories and every marker in them; the folder audit excludes the run roots.

A session's own lifecycle is separate: `end_session.sh` removes its own marker
when ending its run, and `prepare_session.sh` removes its own former `delegate`
marker after writing its new `unit` marker. This prevents an obsolete former
marker from pointing the mover at an old run. `direct_run.active_run` still uses
its unchanged 12-hour run-content age rule for the hook's own session; that rule
controls turn continuation and does not authorize deletion.
