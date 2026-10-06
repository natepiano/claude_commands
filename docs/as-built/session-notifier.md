# Session notifier

## What it is

The session notifier sends a message to a live Claude session on a schedule: the scheduled-update prompt (a `/showrunner:dailies simple` report) to a showrunner, and `/unit:report` ticks to a `/unit:delegate` unit director. Each schedule is a named **instance** stored on disk. One declared 15 s job runs `notifier.sh tick` on both machines and sends every instance that is due. Before each send, the instance's own check command decides whether to send, skip or remove the instance. No agent arms or re-arms a timer. The schedule lives outside the session, so it keeps going through ended turns, compaction and restarts, and it works the same on Linux (natedev) and the Mac.

## How it works

### Key files

| File | Role |
| --- | --- |
| `scripts/message/notifier.sh` | The notifier: instance state, every CLI verb, and `tick`. zsh; no systemd or launchd calls. |
| `scripts/message/sessions.py` | `socket <session:id\|name>` gives a live session's socket; `id <pid\|name>` gives its session id. |
| `scripts/message/send.py` | Delivery. A `--to uds:<socket>` send runs a headless `claude -p` relay whose only tool is `SendMessage`. |
| `scripts/production/production_check.sh` | The showrunner instance's check: the production doc's status as an exit code. |
| `scripts/production/unit_status.sh` | The showrunner's per-unit status script; prints `TICKS FAILING (…)`. |
| `scripts/delegate/unit_notifier.sh` | Makes or retargets one run's `delegate-<run id>` instance. |
| `scripts/delegate/prepare_session.sh` | Run start: writes the run-active marker, then creates the instance. |
| `scripts/delegate/end_session.sh` | Run end: removes the instance, then the marker. |
| `scripts/hooks/delegate_run.py` | `check` CLI: the unit instance's check. Also a library the delegate hooks import. |
| `scripts/delegate/progress_history.py` | `progress` restarts the unit's instance and names its next tick in the report's clock line. |
| `commands/showrunner/{produce,dailies,interval}.md` | Create, restart, retime and remove the showrunner instance. |
| `commands/unit/delegate.md` `<ProgressContract>`, `commands/unit/report.md`, `commands/unit/interval.md` | How a unit treats its ticks, and `/unit:interval`. |
| `config/delegate.conf` | `PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS`, the unit interval. |
| `/etc/nixos/modules/common/session-notifier.nix` | The 15 s job. |
| `scripts/message/test_notifier.py`, `test_sessions.py`, `scripts/delegate/test_delegate_check.py` | CLI tests, run through the `NOTIFIER_*` variables. |

### Instance state

The root is `$NOTIFIER_STATE_DIR`, default `~/.local/state/notifier`. Each instance is a directory named `^[A-Za-z0-9][A-Za-z0-9._-]*$` holding:

- `conf`: `TARGET` (`session:<id>` or a session name), `EVERY` (minutes), `COMMAND` or `PROMPT_FILE` (absolute), `FROM` (sender name, default the instance name), `CHECK` (a command line, may be empty), `HOLD` (0/1), `TIMEOUT` (seconds, default 120).
- `state`: `ENABLED`, `NEXT_DUE`, `LAST_SENT`, `LAST_RESTART`, `LAST_TARGET` (the socket of the last send).
- `lock`: the instance's flock. Every read-modify-write of `conf` or `state` holds it, and each file is rewritten whole (temp file, then `mv`).
- `fire.log`: one line per attempt, stamped in local time with UTC beside it: `<stamp> | exit <rc> | to <target> uds:<socket> | <send.py output>`, `<stamp> | skip <reason>` (`check timeout`, `check exit <rc>`, `session not running`, `hold`), or `<stamp> | hold released: socket changed|two intervals`.

At the root: `.tick.lock`, `.last_tick` (epoch of the latest tick) and `notifier.log` (instance removals and lock or tick failures). `tick` creates the root only when it is absent.

Environment variables the tests set: `NOTIFIER_STATE_DIR`, `NOTIFIER_SESSIONS_DIR` (default `~/.claude/sessions`), `NOTIFIER_SEND` (run in place of `send.py`, same arguments), `NOTIFIER_NOW_EPOCH` (the clock).

### The tick job

`nate.jobs.session-notifier` runs `exec zsh "$HOME/.claude/scripts/message/notifier.sh" tick` every 15 s. On natedev it is a systemd user timer with `OnUnitInactiveSec` 15 s (counted from the end of the previous run) and `AccuracySec` 1 s, from the job option `accuracySeconds`. On the Mac it is a launchd user agent with `StartInterval` 15 (counted from the start of the previous run).

### One tick

```
job (15 s) → notifier.sh tick
  take .tick.lock without waiting (held → exit 0), write .last_tick
  for each instance with ENABLED=1 and now ≥ NEXT_DUE, in a background subshell:
    lock; claim the slot (NEXT_DUE = next one); unlock
    CHECK under a watchdog          → 0 go on, 2 remove instance, other skip
    sessions.py socket TARGET       → none: skip "session not running"
    hold (HOLD=1 only)              → skip, or release and go on
    record LAST_SENT, LAST_TARGET
    send.py --to uds:<socket> --from FROM --key notifier-<instance>
            --summary 'scheduled update' --timeout TIMEOUT (--text COMMAND | --file PROMPT_FILE)
    append the outcome to fire.log
  wait for every subshell, release .tick.lock
```

The slot is claimed before the check runs, so a concurrent tick or `fire` cannot send the same slot twice, and a skip uses up its slot: the next try is one interval later. The check runs as a background process with a `zselect` watchdog that kills it after `TIMEOUT` seconds. `TIMEOUT` is also the relay's delivery limit.

### Delivery

`sessions.py socket` reads every `*.json` record in the sessions directory, matches `session:<id>` against `sessionId` or a bare name against `name`, keeps records whose pid is alive (`PermissionError` counts as alive) and whose `messagingSocketPath` is a socket, and prints the socket with the newest `updatedAt`. No match exits 1; a usage error exits 2. The socket is resolved on every send, so a restarted process is found at its new socket. `send.py` then relays through a headless `claude -p` (sonnet) that makes one `SendMessage` call to that socket. A `COMMAND` is sent as text; a `PROMPT_FILE` is read at send time, so editing the file changes the next tick's text. The key `notifier-<instance>` means a tick that could not be delivered (`QUEUED`, exit 1) replaces the previous queued one, so each instance has at most one tick in `send.py`'s queue.

### CLI

`zsh ~/.claude/scripts/message/notifier.sh <verb> …`. Exit codes: **0** done; **1** no such instance, or `health` failing; **2** usage error or refused.

| Verb | What it does |
| --- | --- |
| `new <instance> --to <target> --every <min> (--command <text> \| --prompt-file <path>) [--from <sender>] [--check <cmd>] [--hold] [--timeout <s>]` | Fresh instance: writes `conf` and `state`, enabled, prints `next_due`. Existing instance: rewrites `conf` only, prints nothing, so the clock and `ENABLED` stay as they were. Refuses (2) an empty command or prompt file, both or neither, `--every` or `--timeout` not a whole number above 0, a relative prompt path. |
| `start <instance>` | `ENABLED=1`, schedules from now, prints `next_due`. |
| `stop <instance>` | `ENABLED=0`; `NEXT_DUE` stays. |
| `restart <instance>` | `LAST_RESTART=now`, schedules from now, prints `next_due`. Leaves `ENABLED` alone. |
| `interval <instance> <min>` | Sets `EVERY`, then does what `restart` does. Bad minutes exit 2 before the instance lookup. |
| `fire <instance>` | Runs one tick now, ignoring `ENABLED`, `NEXT_DUE` and the hold. Still runs the check and needs a live socket. Prints `next_due` (the clock moves). Exits 0 whatever the send outcome; the outcome is in `fire.log`. |
| `remove <instance>` | Deletes the instance directory. Exits 0 when it is already gone. |
| `status <instance>` | Target, interval, enabled or stopped, `next_due`, `last_sent`, `last_restart`, `last_tick`, last five `fire.log` lines. |
| `status` | One line per instance (`<name> every N min next HH:MM enabled\|stopped`), then `last_tick`. |
| `health <instance>` | Exit 1 with `failing: no instance`, `failing: no tick since <time\|never>` (`.last_tick` missing or older than 120 s), or `failing: last two sends exit a, b` (the last two `exit` lines both nonzero). Otherwise exit 0 with `ok`, or `ok: stopped` for a stopped instance. |
| `tick` | The job's verb. Always exits 0. |

`restart`, `start`, `interval`, `fire` and a fresh `new` print exactly one line `next_due=<epoch> (<YYYY-MM-DD HH:MM TZ>)`, in local time.

### The check contract

`CHECK` is split with zsh `(Q)(z)` and run as a command. Its exit code decides the tick:

- **0**: send.
- **2**: the owner is gone. The tick deletes the instance and logs `removed <name> (check exit 2)` in `notifier.log`.
- **any other nonzero**: skip, logged as `skip check exit <rc>`.
- Not finished within `TIMEOUT`: killed, logged as `skip check timeout`.

`production_check.sh <doc>` reads the doc's `Status: PRODUCTION — <word>` line: `running` 0, `wrapped` 2, anything else (or no such line, or no file) 1.

`delegate_run.py check <claude_session_id> <session_dir>` exits 2 when the marker `/tmp/claude/delegate/active/<id>` is missing or names another directory (the run ended, or a new run in the same session replaced it); 0 when `running_work()` finds work in flight; 1 when the unit is idle; 3 on a usage error. Work in flight is an `impl_status` or `impl_status_*` file reading `implementing`, a `review_status*` file reading `reviewing`, or a `progress_history_state.json` activity with status `active`. That last one covers `verify.sh final` and UX review, which open activities.

### Hold and restarting the clock

Every schedule write sets `NEXT_DUE = now - now % 60 + EVERY * 60`: the next whole minute boundary one interval out. `restart` and `interval` also set `LAST_RESTART=now`.

With `HOLD=1`, a tick skips (`skip hold`) while `LAST_SENT > LAST_RESTART`, that is, while a sent tick has not yet been answered by a clock restart. The hold releases when the session's socket differs from `LAST_TARGET` (a new process: the waiting tick went with the old one) or when two intervals have passed since `LAST_SENT`. `fire` ignores the hold.

On the unit side, every `progress_history.py progress` call that renders a report runs `notifier.sh restart delegate-<run id>` (10 s limit, `PLAN_DELEGATE_NOW_EPOCH` copied to `NOTIFIER_NOW_EPOCH`) and reads `next_due` from its output. That restart releases the hold and puts the next tick one full interval after the latest report, whether a tick, the user or a completion caused it. The report's clock line, `**now <local time> - next report <time>**`, uses that `next_due`. If the restart fails or there is no instance, the clock line falls back to a still-future `progress_timer` marker deadline, then to now plus the configured interval, and drops the clause when neither exists. A failed restart never fails the report.

### The showrunner instance

`/showrunner:produce` owns `UPDATES` = `showrunner-<slug>`, where `<slug>` is the production doc's file name less `-production.md`. In `<StartUpdates>`, at start and on every resume, it writes the filled scheduled-update prompt to `PROMPT_FILE` (`~/.local/state/showrunner/<slug>/prompt.txt`) and runs:

```
notifier.sh new showrunner-<slug> --to session:$CLAUDE_CODE_SESSION_ID --every <N> \
  --prompt-file <PROMPT_FILE> --from showrunner-timer-<slug> \
  --check "zsh $HOME/.claude/scripts/production/production_check.sh <absolute doc path>"
```

N comes from the doc's `**Updates:** every N minutes` line, 15 when absent. There is no `--hold`. A repeated `new` on resume retargets the instance to the current session without moving the clock. The instance outlives the session. A tick arrives as a message from `showrunner-timer-<slug>` whose text starts `Scheduled update`; the showrunner treats it as the scheduled prompt and does not reply. A `/showrunner:dailies` the user types runs `unit_status.sh`, gives the report, then runs `restart`, so the next tick is N minutes after that report; a scheduled tick skips both. Every reply's `next dailies` time is read from `notifier.sh status`. `<Wrap>` runs `remove`, and the check removes the instance on its own once the doc says `wrapped`.

### The unit instance

Each Claude delegate run gets `delegate-<run id>`, where the run id is the basename of `SESSION_DIR` (`/tmp/claude/delegate/<uuid>`). `prepare_session.sh`, when `CLAUDE_CODE_SESSION_ID` is set, writes the marker under the fixed `/tmp/claude/delegate/active`, then runs `PLAN_DELEGATE_ACTIVE_DIR=/tmp/claude/delegate/active zsh unit_notifier.sh <id>`. On success it prints the `next_due=` line; on failure it prints `notifier instance not created: <output>` and goes on. `Session ready at <dir>` is always its last line.

`unit_notifier.sh <claude_session_id>` reads `SESSION_DIR` from the marker (exit 1 when missing or empty, 2 on a usage error) and `exec`s:

```
notifier.sh new delegate-<run id> --to session:<id> --every <minutes> \
  --command /unit:report --hold \
  --check "<repo>/scripts/lib/py <repo>/scripts/hooks/delegate_run.py check <id> <SESSION_DIR>"
```

So the unit gets `/unit:report` every interval while work runs, from sender `delegate-<run id>`, with at most one tick waiting. The unit director arms nothing. On each tick it reads `report.md` and composes `<ProgressReport/>`; ticks that arrive during a report, or several at once, get one report. A tick never replaces the completion report. If the user stops updates, the unit runs `notifier.sh stop delegate-<run id>`, and `start` to resume; `restart` from later reports keeps a stopped instance stopped.

`end_session.sh` runs `notifier.sh remove delegate-<run id>` (errors ignored) before it deletes the marker. A run that dies without `end_session.sh` loses its instance at the next due slot after its marker is gone or replaced (check exit 2). A unit parked on the user keeps its instance; its check exits 1 and each slot is skipped.

A Codex unit has no `CLAUDE_CODE_SESSION_ID`, so it gets no marker and no instance. Its poll timeout, set from the same interval key, is its tick.

### Changing the interval

- `/unit:interval [minutes]` finds `delegate-<run id>` from `/tmp/claude/delegate/active/$CLAUDE_CODE_SESSION_ID`. No argument runs `notifier.sh status`; a positive integer runs `notifier.sh interval` (exit 2 means refused) and reports the next tick from `next_due` in local time. It changes this run only and is unavailable on a Codex unit.
- `/showrunner:interval [minutes]` edits the doc's `**Updates:**` line and the `every N minutes` text in `PROMPT_FILE`, runs `notifier.sh interval UPDATES <minutes>`, commits the doc, logs the old and new interval, and tells the user the next tick in `ZONE` and UTC. No argument reports status only.

### TICKS FAILING

For each unit with a running Claude pid, `unit_status.sh` runs `sessions.py id <pid>`, reads the marker for that session id, and, whenever the marker names a run, runs `notifier.sh health delegate-<run id>`, idle or not. Health exit 1 prints `TICKS FAILING (<health line without "failing: ">)`: `no instance`, `no tick since <time>`, or `last two sends exit a, b`. No marker, or an empty one, prints nothing. The showrunner reads this line in every scheduled update and typed dailies.

## Invariants

- Every script runs on Linux and the Mac: no GNU-only `sed -i` or `0,/re/`, no GNU `timeout`. Clock, locks and waits use zsh modules (`zsh/datetime`, `zsh/system` `zsystem flock`, `zsh/zselect`). Python runs only through `scripts/lib/py`, never `python3`.
- Never `${PIPESTATUS[0]}`; use `setopt pipe_fail` or `set -o pipefail`. `prepare_session.sh` and `end_session.sh` stay bash; the notifier scripts are zsh.
- `notifier.sh` makes no systemd or launchd calls. Only the declared nix job runs `tick`. A change to the job takes effect when the user runs `rebuild` on each machine; no session runs `rebuild`, `nixos-rebuild switch` or sudo.
- Every edit of `conf` or `state` is a read-modify-write under the instance's flock, written whole through a temp file and `mv`.
- A slot is claimed (`NEXT_DUE` advanced) before the check or send runs.
- Every schedule write sets `NEXT_DUE = now - now % 60 + EVERY * 60`.
- `new` on an existing instance rewrites `conf` and leaves `state` alone.
- `restart`, `start`, `interval`, `fire` and a fresh `new` print exactly one `next_due=<epoch> (<local time>)` line; `progress_history.py` and `prepare_session.sh` parse it.
- CLI exit codes: 0 done, 1 no such instance or `health` failing, 2 usage error or refused.
- Check contract: 0 sends, 2 removes the instance, other nonzero skips, and the check finishes within `TIMEOUT`. `delegate_run.py check` stays a quick file read and never treats an old run as gone.
- Instance names are `showrunner-<slug>` and `delegate-<run id>`, run id = basename of `SESSION_DIR`; the send key is `notifier-<instance>`.
- `prepare_session.sh`'s last line is `Session ready at <dir>`; the `next_due=` or `notifier instance not created:` line comes before it.
- `end_session.sh` removes the instance before the marker, ignoring errors.
- The `progress` restart never fails a report; any failure falls back.
- Hooks that import `delegate_run.py` never fail the turn and skip subagents.
- Delegate scripts run with `dangerouslyDisableSandbox: true`.
- `scripts/delegate/progress_timer.sh` stays: `/clippy` launches it.
- Times shown to the user are in the machine's local zone, with UTC beside them where the logs show it.

## Calibration and gotchas

- **Unit interval.** `PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=900` gives 15 minutes. `unit_notifier.sh` reads the leading digits, so a trailing `# comment` keeps the value. A missing key, unreadable file or non-numeric value gives 900 s. Minutes round up with a floor of 1, so `0` means 1 minute. Codex requires a positive integer. `progress_history.py` reads the key on its own and drops the next-report clause on a bad value; that only shows when there is no instance.
- **Interval cost.** Each report costs the unit director about 40 s of generation. Keep the interval at or above the median blind review, about 5 minutes.
- **Showrunner interval.** 15 minutes when the doc has no `**Updates:**` line.
- **Timing.** `next_due` is a whole minute; the tick that sends it lands within about 15 s after. On natedev a slow tick pushes the next one back (the interval counts from the end of the run); on the Mac an overlapping tick finds `.tick.lock` held and exits at once.
- **A skip uses its slot.** An idle unit, a missing session or a hold skips the whole interval, not 15 s.
- **Timeouts.** `TIMEOUT` (120 s default) bounds both the check and the relay. One relay takes about 9 s. `tick` waits for every instance's subshell before it releases `.tick.lock`, so a check must stay fast.
- **Health window.** `.last_tick` older than 120 s means the job is not running; `health` reports it even for a stopped instance.
- **Seats inherit `CLAUDE_CODE_SESSION_ID`.** A seat can inherit the unit director's id, so `prepare_session.sh` or `end_session.sh` run from a seat or a test acts on the unit director's marker and instance. Tests run them only with a fresh id and a temp `NOTIFIER_STATE_DIR`; `end_session.sh`'s `remove` inherits that variable.
- **`PLAN_DELEGATE_ACTIVE_DIR`** is a test-only variable honoured only by `delegate_run.py`, `unit_notifier.sh` and `unit_status.sh`. `prepare_session.sh` and `end_session.sh` use the fixed `/tmp/claude/delegate/active`, so a live caller leaves it unset or pointed where the marker is.
- **`CHECK` quoting.** `unit_notifier.sh` `(q)`-quotes each word of the check, so a repo path with spaces survives the `(Q)(z)` split.
- **`progress` cost.** Every rendered `progress` call spawns one zsh, even for a run with no instance. A `progress` call restarts the clock before it can refuse, so a call that exits with `No open window to report` or a missing cap stage still moves the next tick one interval out.
- **`/unit:interval` lasts for the run.** Rerunning `unit_notifier.sh` resets `EVERY` to the config value.
- **`fire`** moves the clock and skips the hold, but a failing check or a missing session still skips it.

## Why

- **The schedule lives outside the session.** An agent that must arm a timer before ending every turn misses one sooner or later. A file-backed instance ticked by a job keeps the schedule through turn ends, compaction and restarts, and the showrunner and the units use one mechanism.
- **One job ticks every instance.** A timer per instance would need systemd on Linux and launchd on the Mac from inside the script. One declared 15 s job keeps `notifier.sh` free of both and identical on each machine. `AccuracySec` is 1 s because systemd's default of 1 minute would spread a 15 s tick across a minute.
- **The check decides, not the notifier.** `notifier.sh` knows nothing about productions or delegate runs. Each owner supplies a command, and exit 2 lets an instance remove itself when its owner is gone, so a crashed run or a missed wrap stops sending on its own.
- **Unit ticks only while work runs.** An idle unit, waiting on the user or between steps, has nothing new to report, and every report costs generation time. A unit parked overnight keeps its instance, so updates resume with the work.
- **Hold for units.** A unit in a long turn cannot read ticks; without the hold they would stack and each produce a report. The socket-change release covers a restarted session, which lost its waiting tick; the two-interval release keeps a lost tick from silencing the unit for good.
- **Each report restarts the clock.** The next tick comes one interval after the latest report from any source, the report's clock line names the real next tick, and the restart releases the hold.
- **No hold for the showrunner.** A scheduled showrunner update does not restart its own clock, so a hold would skip every other tick.
- **Target by session id; resolve the socket each send.** A session's `name` can differ from its tmux name, and one id can sit in two live records; the newest live `updatedAt` wins. A restarted process has a new socket, which the next send finds.
- **`new` leaves state alone on an existing instance.** A resume retargets the instance without moving the clock or re-enabling updates the user stopped.
- **`zselect` watchdog.** GNU `timeout` is absent on the Mac, and an external `sleep` would outlive the subshell; the builtin dies with it. The bound matters because a hung check would hold `.tick.lock` and stall every instance.
- **Pinned marker directory in `prepare_session.sh`.** It writes the marker in the fixed directory, so it pins `PLAN_DELEGATE_ACTIVE_DIR` for `unit_notifier.sh`; an inherited test value would point it at another marker. `end_session.sh` does not honour that variable because `prepare_session.sh` never writes there.
- **`Session ready at <dir>` stays last.** The unit director reads `SESSION_DIR` from the last line. An instance that fails to be created does not stop the run; the unit says the run gets no ticks until `unit_notifier.sh "$CLAUDE_CODE_SESSION_ID"` succeeds.
- **`TICKS FAILING` for every active run, idle or not.** An idle unit gets no ticks by design, but its instance must be there when work resumes. Checked only while work ran, a unit with no instance dropped out of every idle status: hana's geometry-material reported `no instance` from 2026-09-29 and no status showed it.
- **A refused report still restarts the clock.** The hold keeps at most one tick waiting and releases on the next restart; a refused call that skipped the restart left the next slot held, a 30-minute gap.
