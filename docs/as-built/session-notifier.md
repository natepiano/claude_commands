# Session notifier

## What it is

The session notifier sends scheduled messages to sessions.
Showrunner registration keeps a stopped timer for discovery and sends no automatic report.
Use `/showrunner:units` for production status.

### Key files

| File | Role |

| --- | --- |

| `scripts/message/notifier.sh` | The notifier: instance state, every CLI verb, and `tick`. zsh; its only systemd or launchd call is `launch_run`, which starts a run-only job. |

| `scripts/message/sessions.py` | `socket <session:id\|name>` gives a live session's socket; `id <pid\|name>` gives its session id. `read_session(path) -> SessionRecord | UnreadableSessionRecord` keeps an unreadable registry entry distinct from no live match. |

| `scripts/message/send.py` | Delivery. A `--to uds:<socket>` send to a live registered session goes through the postmaster outbox first when its heartbeat is fresh, and otherwise through a headless `claude -p` relay whose one job is a `SendMessage` call (`docs/as-built/session-roster.md`). |

| `scripts/production/production_check.sh` | The showrunner instance's check: the production doc's status as an exit code. |

| `scripts/production/unit_status.sh` | The showrunner's per-unit status script; prints `TICKS FAILING (…)`. |


| `scripts/direct/prepare_session.sh` | Run start: records the run in the director's staff row. |

| `scripts/direct/end_session.sh` | Attempts run cleanup, then clears each matching run pointer through `staff.py clear-run`. |

| `scripts/hooks/direct_run.py` | Supplies the open run and current work to worker hooks. |

| `scripts/direct/progress_history.py` | `progress` restarts the unit's instance on every call, refused ones included, and names its next tick in the report's clock line. |


| `scripts/production/production_lifecycle.py` | `wrap` removes the showrunner instance. |

| `scripts/agents/restart.py`, `restart_support.py` | Legacy record-driven restart restores the recorded notifier state. The shared session stop leaves notifier state unchanged. |



| `config/direct.conf` | `UNIT_PROGRESS_INTERVAL_SECONDS`, the Codex unit poll timeout. |

| `/etc/nixos/modules/common/session-notifier.nix` | The 15 s job. |

| `scripts/message/test_notifier.py`, `test_sessions.py`, `scripts/direct/test_worker_check.py` | CLI tests, run through the `NOTIFIER_*` variables. |

| `scripts/direct/test_progress_history.py` | The `progress` restart, refused calls included (missing `--cap-stage`, no active phase): `LAST_RESTART` at the call time and `NEXT_DUE` one interval later. |

### Instance state

The root is `$NOTIFIER_STATE_DIR`, default `~/.local/state/notifier`. Each instance is a directory named `^[A-Za-z0-9][A-Za-z0-9._-]*$` holding:

- `conf`: `TARGET`, `EVERY`, `COMMAND` or `PROMPT=scheduled`, `RUN`, `FROM`, `CHECK`, `HOLD`, `ALIGN`, and `TIMEOUT`.
  A scheduled showrunner target is `session:<staff-id>`.
  `COMMAND` holds fixed message text; `PROMPT=scheduled` selects a prompt built at delivery.
  `RUN` holds a command for an instance that sends no message.
  `EVERY` specifies minutes; `TIMEOUT` specifies seconds and defaults to 120.
  `FROM` defaults to the instance name; `CHECK` can be empty.
  `HOLD` and `ALIGN` use 0 or 1.
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
    sessions.py member-socket TARGET → none: skip "session not running"; records unreadable
                                      (exit 3): skip "session lookup exit 3"
    hold (HOLD=1 only)              → skip, or release and go on
    PROMPT=scheduled               → build prompt from the current owner row; failure skips this tick
    record LAST_SENT, LAST_TARGET
    send.py --to uds:<socket> --from FROM --key notifier-<instance>
            --summary 'scheduled update' --timeout TIMEOUT --text <fixed command or built prompt>
    append the outcome to fire.log
  wait for every subshell, release .tick.lock
```

A run-only instance (`RUN`) is not run inside the tick. `launch_run` starts `notifier.sh run-due <name>` in its own transient service (`systemd-run --user --collect --quiet --no-block --unit session-notifier-<name>-<pid>-<random>` on Linux; on the Mac a `launchctl submit` job that runs `run-due-and-remove`, which removes its own label). The Mac job is then started with `launchctl kickstart gui/<uid>/<label>`: on macOS 27 a submitted job is only registered and never runs on its own (measured 2026-10-16). With neither launcher, or when the Mac kickstart fails (the tick then removes the label), the tick logs `run launcher unavailable: <name>; waiting after tick` to `notifier.log` and runs the job once it has released `.tick.lock`. The job claims its slot, runs `RUN` with output to `<instance>/run.log` under the `zselect` watchdog (TERM at `TIMEOUT`), and logs only `run timeout` or `run exit <rc>`. A background child of the tick would die with the tick's cgroup or launchd job.

The slot is claimed before the check runs, so a concurrent tick or `fire` cannot send the same slot twice, and a skip uses up its slot: the next try is one interval later. The check runs as a background process with a `zselect` watchdog that kills it after `TIMEOUT` seconds. `TIMEOUT` is also the relay's delivery limit.

### Delivery

The notifier resolves `TARGET` through `sessions.py member-socket` for every send.
For `session:<staff-id>`, the staff row supplies the current session.
A restarted session can therefore receive messages at its current socket.

`COMMAND` supplies fixed message text.
`codex_winddown.start_count` passes its `TICK` text through `--command` and creates no prompt file.
For `PROMPT=scheduled`, the notifier runs `update_registration.py prompt --owner <staff-id>` before delivery.
It removes the `session:` prefix from `TARGET` for that command.
The command resolves the owner's current name and document through staff.
It reads the update interval from that document.
`scheduled_prompt(minutes, production)` fills the template's interval, zone, name, and state directory.
The output contains the template text without an added document-path line.
A failed build skips the tick and prints one stderr line.
It does not record `LAST_SENT` or send a message.

The notifier sends the prompt with `send.py --text`.
The key `notifier-<instance>` limits each instance to one queued tick.
An undelivered tick replaces the previous queued tick for that instance.

### CLI

`zsh ~/.claude/scripts/message/notifier.sh <verb> …`. Exit codes: **0** done; **1** no such instance, or `health` failing; **2** usage error or refused.

| Verb | What it does |
| --- | --- |
| `new <instance> --every <min> (--run <cmd> \| --to <target> (--command <text> \| --prompt scheduled)) [--from <sender>] [--check <cmd>] [--hold] [--timeout <s>]` | Creates an enabled instance with `conf` and `state`. An existing instance retains its clock and enabled setting. `--run` accepts no message options. Message instances require exactly one source: `--command` or `--prompt scheduled`. Empty commands and invalid intervals or timeouts return exit 2. |
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

`production_check.sh <staff id>` reads the document through the showrunner's staff row. It reads `Status: PRODUCTION — <word>` and exits 0 for `running`, 2 for `wrapped`, or 1 otherwise. A missing document or status line also exits 1. For a running production, the check first runs `log_backup.py <staff id>` with the same argument. That command keeps dated copies of the production's LOG; see LOG backups in `docs/as-built/showrunner-automation.md`. A failed copy prints to stderr and does not change the check's exit code.

`direct_run.active_run(session_id)` reads the director's open run from staff.
It returns no run when the row has no pointer or the run has no recent writes.
`running_work()` identifies work in progress.
Worker status values `running`, `implementing`, and `reviewing` indicate work in progress.

### Hold and restarting the clock

Every schedule write sets `NEXT_DUE = now - now % 60 + EVERY * 60`: the next whole minute boundary one interval out. `restart` and `interval` also set `LAST_RESTART=now`.

With `HOLD=1`, a tick skips (`skip hold`) while `LAST_SENT > LAST_RESTART`, that is, while a sent tick has not yet been answered by a clock restart. The hold releases when the session's socket differs from `LAST_TARGET` (a new process: the waiting tick went with the old one) or when two intervals have passed since `LAST_SENT`. `fire` ignores the hold.


### The showrunner instance

`update_registration.py register` creates or retargets `showrunner-<staff id>` and stops it.
The timer retains its target session for discovery.
Its configuration stores `PROMPT=scheduled` and targets `session:<staff id>`.
Registration creates no prompt file.
The staff id preserves the instance across production renames and session changes.
`notifier.sh` builds each prompt through `update_registration.py prompt --owner <staff id>` before it records `LAST_SENT`.
The builder reads the current owner name and document through staff.
It reads the interval from the document and the template from `commands/showrunner/produce.md`.
The template uses `/showrunner:units`.
A failed build prints one stderr line and skips delivery.
The stopped timer sends no automatic report.
`adopt_moved_state` copies missing scratchpad state and writes no prompt file.
The document's old Updates interval has no effect.
Registration creates stall-watch only when absent.
Production wrap removes the timer and retains reminder history.

### Unit reports

A unit run has no notifier instance (user, 2026-10-19). `/unit:report` runs only when someone asks. All 12 `worker-*` instances had been `ENABLED=0` since 2026-10-12 22:04 EDT. They were removed. `prepare_session.sh` creates none. `end_session.sh` removes none. A Codex unit's poll timeout composes no report. It lets `<EarlyReviewArm/>` run, as defined in `docs/direct/early_review.md`.

### TICKS FAILING

`unit_status.sh` reads each unit's open run from its staff row.
It runs `zsh notifier.sh health <instance>` when the unit has a live session and a recorded run.
The instance is `unit-<run id>`, or `delegate-<run id>` for a run under the former root.
Health exit 1 prints `TICKS FAILING (<health line without "failing: ">)`.
A row without a run produces no health check.

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

- **Run folders and staff.** Current runs use `/tmp/claude/unit/<run id>/`.
  `scripts/lib/unit_runs.py` reads the former `delegate` root for earlier run folders.
  The director's staff row stores the open run in `run`.
  `prepare_session.sh` sets the pointer.
  `end_session.sh` clears the pointer only while it still names that run.

- `prepare_session.sh`'s last line is `Session ready at <dir>`; the `next_due=` or `notifier instance not created:` line comes before it.

- `end_session.sh` attempts role, worker, and scratch cleanup before it clears each selected run pointer.
  A failed cleanup step does not prevent pointer cleanup. The script reports the step and returns nonzero.
  `copies.py remove` can keep a busy copy without failing the end step.

- `progress` restarts the instance before it reads state or can refuse, so every call, refused ones included, restarts the clock. The restart never fails a report; any failure falls back.

- Hooks that import `direct_run.py` never fail the turn and skip subagents.

- Worker scripts run with `dangerouslyDisableSandbox: true`.

- `scripts/direct/progress_timer.sh` stays, and `settings.json` still allows it, but nothing in this repo launches it. `progress_history.py` reads its `progress_timer` marker only when one exists; a unit run's next report time comes from its notifier instance.

- Times shown to the user are in the machine's local zone, with UTC beside them where the logs show it.

## Calibration and gotchas

## Why

- **The schedule lives outside the session.** An agent that must arm a timer before ending every turn misses one sooner or later. A file-backed instance ticked by a job keeps the schedule through turn ends, compaction and restarts, and the showrunner and the units use one mechanism.

- **One job ticks every instance.** A timer per instance would need systemd on Linux and launchd on the Mac from inside the script. One declared 15 s job keeps timers out of `notifier.sh`, and the script identical on each machine; its one launcher call only detaches a run-only job after the tick has decided it is due. `AccuracySec` is 1 s because systemd's default of 1 minute would spread a 15 s tick across a minute.

- **The check decides, not the notifier.** `notifier.sh` knows nothing about productions or `/unit:direct` runs. Each owner supplies a command, and exit 2 lets an instance remove itself when its owner is gone, so a crashed run or a missed wrap stops sending on its own.

- **Unit ticks only while work runs.** An idle unit, waiting on the user or between steps, has nothing new to report, and every report costs generation time. A unit parked overnight keeps its instance, so updates resume with the work.

- **Hold for units.** A unit in a long turn cannot read ticks; without the hold they would stack and each produce a report. The socket-change release covers a restarted session, which lost its waiting tick; the two-interval release keeps a lost tick from silencing the unit for good.

- **Each report restarts the clock.** The next tick comes one interval after the latest report from any source, the report's clock line names the real next tick, and the restart releases the hold.

- **`new` leaves state alone on an existing instance.** A resume retargets the instance without moving the clock or re-enabling updates the user stopped.

- **`zselect` watchdog.** GNU `timeout` is absent on the Mac, and an external `sleep` would outlive the subshell; the builtin dies with it. The bound matters because a hung check would hold `.tick.lock` and stall every instance.



- **`TICKS FAILING` for every active run, idle or not.** An idle unit gets no ticks by design, but its instance must be there when work resumes. Checked only while work ran, a unit with no instance dropped out of every idle status: hana's geometry-material reported `no instance` from 2026-09-29 and no status showed it.

- **A refused report still restarts the clock.** The hold keeps at most one tick waiting and releases on the next restart. A refused call that skipped the restart would leave the next slot held until the two-interval release, a 30-minute gap at the default interval.

## Production registration

Production registration creates the showrunner update and the run-only
stall-watch job. Session names come from herdr; there is no name-refresh job.
Existing retired jobs are removed by their production's showrunner.

### Open runs in staff

`StaffRow.run` holds `OpenRun(path)` or `NoRun`.
`unit_runs.open_run(session_id)` reads the row for the current session.
Readers use this pointer without an age check or a session liveness check.

The automatic temp sweep retains run folders named by staff rows.
It also retains runs with workers that have not ended.
The removed mark folders have no special protection.

The staff JSON key `run` holds the path string. An absent key reads as `NoRun`.

`prepare_session.sh` publishes the pointer through `staff.py set-run`.
A session with no staff row prints one line and continues. An unreadable staff record stops the script.
`progress_history.py start-run` and its reopen path publish the pointer for unit run folders.
A rerun after a failed write publishes the pointer again.
A helper folder never changes a staff pointer.
`unit_runs.run_is_open(folder)` reads the unit owner's pointer.
Lifecycle commands supply the current staff owner's record instead of using a retained local copy.
The reopen path writes the cleared state and the `reopened` event before it claims the pointer.
Run state stores an outcome and `finished_at` after the run finishes.
Open run state has no `status`.
Folders outside `unit_runs.run_roots()` are helper runs.
They use the absence of an outcome because staff does not record their runs.
`finish-run` and `end_session.sh` clear matching pointers for every staff row kind.
A repeated finish also attempts to clear matching pointers.
An old run cannot clear a pointer that names a newer run.
`direct_run.active_run` keeps its 12-hour rule for recent writes in the run folder.
That rule controls turn continuation and does not permit deletion.
