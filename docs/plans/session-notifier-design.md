# Proposal: one notifier for dailies and unit reports

## Summary

1. A new script, `~/.claude/scripts/message/notifier.sh`, sends a command you choose (`/showrunner:dailies`, `/unit:delegate_report`) to one Claude session every N minutes. Each instance has its own interval, state and log.
2. One declared job per machine, `nate.jobs.session-notifier`, checks every instance every 15 s. It is a systemd timer on Linux and a launchd agent on the Mac. The script makes no systemd calls, and nothing has to be started again after a reboot.
3. Units stop arming timers. `prepare_session.sh` creates the unit's instance and `end_session.sh` removes it. A tick fires only while delegate work runs, and every report pushes the next tick out.
4. Ticks reach units on time. Launchers already run in the background and the unit ends its turn, so an idle unit takes a message in under a second (measured on trunk). `progress_timer.sh`, its Stop hook and about 50 lines of `delegate.md` go.
5. Trunk's timer lapsed at 17:42 PDT on Oct 3. Auto mode refused a `progress_timer.sh` launch (it has no allow rule), and the agent never armed one again. In the new design the agent launches no timer, so there is nothing to refuse.

## Why the unit timer lapsed

Trunk (`b83cfc96…`, Phase 59) is lapsed right now. Its fix seat has run since 01:00 PDT. The last report was at 01:01 PDT, and there is no `progress_timer` marker in its session dir.

| Cause | Where | Evidence |
| --- | --- | --- |
| Every tick needs the agent to arm the timer again | `delegate.md:199-206`, `:520-525` | 156 to 1,897 `progress_timer.sh` launches per unit transcript, 1,079 on trunk. Each one is a chance to drop it. |
| No allow rule, so auto mode judges every launch | `settings.json:54-55` allows `showrunner_timer.sh` and `verify.sh` only | Trunk, 17:42 PDT Oct 3: refused as "[Instruction Poisoning]". The agent said only you could allow it. |
| The Stop hook blocks once per lapse, and a one-line reply clears it | `stop-delegate-progress-timer.py:121-131` | 19 blocks on trunk. Four came between 00:43 and 00:53 PDT, and each got "the classifier denied it". |
| The refusal lives in the conversation, and compaction carries it forward | `delegate.md:296-298` | Three compactions later (02:10 PDT) it said "my own timer was blocked earlier and I won't retry it". |
| Without a timer, reports come only when a task finishes | `delegate.md:199-201` | Reports at irregular times. The longest gap was 22:04 to 01:01 PDT (2 h 57 min). |
| Changing the interval needs every unit to re-arm | `delegate.conf:47-58` | The showrunner had to message each unit at 02:10 PDT. |

## The notifier

```
notifier.sh new <instance> --to <target> --every <min> (--command <text> | --prompt-file <path>)
                [--from <sender>] [--check <cmd>] [--hold]
notifier.sh start|stop|status|fire|restart|remove <instance>
notifier.sh interval <instance> <min>
notifier.sh tick                 # the job's entry point: checks every instance
```

- **State:** `~/.local/state/notifier/<instance>/` holds the conf, a `state` file (next_due, last_sent, last_target, last_restart) and `fire.log` in today's format. The script edits them in zsh, never with GNU `sed -i` (`showrunner_timer.sh:205-207`), which fails on the Mac.
- **Target:** `session:<claude session id>` or a name. Each tick looks the id up in `~/.claude/sessions/*.json` and sends to its live socket as `--to uds:<socket>`, so `send.py` skips ListAgents (`send.py:302-305`). Renames (trunk was `tool-based-ui-trunk` until Oct 3) and resumes need no step.
- **Check:** an optional gate command. Exit 0 sends, 1 skips the tick (logged), 2 removes the instance.
- **Tick rule:** for each instance with now ≥ next_due: check, look up the target, `send.py --from <sender> --key notifier-<instance>`, in parallel across instances. Then next_due = start of this minute + N. `restart` sets the same and records last_restart; `interval` sets N and restarts, as today.
- **`--hold`:** no new tick while the last has no `restart` after it, unless the target socket changed or two intervals passed (logged). A stuck unit gets one waiting tick, not a pile, and a lost message cannot stop the ticks.

## Showrunner migration

| | Today | New |
| --- | --- | --- |
| Timer | `systemd-run` per production (`showrunner_timer.sh:152-157`) | instance `showrunner-<slug>`, made by StartUpdates step 3 |
| Gate | doc `Status:` running / wrapped (`:223-234`) | `--check production_check.sh <doc>`: the `read_doc` part (`:103-128`). Running exits 0, wrapped exits 2, anything else exits 1. |
| Target | doc's `**Showrunner session:**` line | `session:<id>`, written by StartUpdates at start and on every resume, at the same time as the doc line |
| Sender, prompt | `showrunner-timer-<slug>`, `prompt.txt` | same name, `--prompt-file` points at the same `prompt.txt` |
| Typed dailies | `TIMER stop` + `TIMER start` (`dailies.md:38-41`) | `notifier.sh restart` |
| Next fire | `systemctl list-timers` (`produce.md:96-97`) | `notifier.sh status` |
| Reboot | StartUpdates starts it again (`produce.md:214-215`) | nothing to do |
| `--hold` | none | off: tick-driven dailies never restart the clock, as today |

The only change in behaviour: a tick lands within 15 s after the minute the report names. Today it lands on the second.

## Units

| Moment | Who | Action |
| --- | --- | --- |
| Run start | `prepare_session.sh`, beside the active marker (`:31-34`) | `new delegate-<run id> --to session:$CLAUDE_CODE_SESSION_ID --every <delegate.conf key / 60> --command '/unit:delegate_report' --check 'delegate_check <SESSION_DIR>' --hold`. Skipped for a Codex unit, which has no session id. |
| Dispatch, verify, smoke | nobody | nothing |
| Any report (tick, completion or typed) | `progress_history.py progress` | runs `notifier.sh restart`. The header's "next report" reads next_due instead of the marker (`progress_history.py:1914-1939`). |
| Gate, wait on another unit, between phases | the check | skips: no work is running |
| Compaction, resume, reboot | nobody | the conf is on disk and the target is looked up again |
| Session exits | the target lookup | skips, logged as `session not running` |
| You stop updates | the agent | `notifier.sh stop` (replaces `PROGRESS_UPDATES_ENABLED`) |
| You change one unit's timing | new `/unit:interval <min>`, or the showrunner | `notifier.sh interval` |
| Run end, or a killed run | `end_session.sh`, or the check exiting 2 on a stale run (`delegate_run.py:59-68`) | instance removed |

**Gate:** `delegate_check` lives in `delegate_run.py`. It exits 0 only when `active_run()` names this run and `running_work()` finds work. `running_work()` moves there from `stop-delegate-progress-timer.py:62-86`. The short names (trunk, GM, widget…) play no part, because the target is the session id.

## Delivery while the unit is busy

- **Code:** a Claude unit runs every launcher with `run_in_background` (`delegate.md:179-190`) and ends its turn (`:199-206`). The 2 h timeout is that background task's limit, not a foreground call, so during a typical wait the unit is idle.
- **Measured** on trunk's transcript, Sep 24 to Oct 4: 79 messages that arrived while it was idle started a turn after a median 0.0 s (max 56 s). 36 that arrived mid-turn were taken at the next tool round: median 9 s, 90th percentile 37 s, max 292 s. The showrunner's 02:10:25 PDT message got its answer at 02:10:26 while trunk waited on its fix seat.
- **Worst case:** a foreground Bash call holds a tick up to its 10-minute cap, as it holds today's timer notification. **No wait needs to change.**
- **Codex units** cannot receive SendMessage; their poll timeout stays their tick (`delegate.md:255-279`).
- **Overlap:** `--hold` allows one waiting tick, and `--key` keeps only the latest undelivered one (`send.py:194-201`). New rule in `delegate_report.md`: ticks that arrive during a report, or several at once, get one report.

## What goes, what stays

| Goes | Stays |
| --- | --- |
| `progress_timer.sh` | `PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS`: the Codex poll timeout and the default for new instances |
| `stop-delegate-progress-timer.py` and its Stop entry in `settings.json` | `<CodexDispatchWait/>` |
| `delegate.md`: `PROGRESS_TIMER_HANDLE` (`:39-40`), `PROGRESS_UPDATES_ENABLED` (`:35-36`), arm and re-arm text (`:199-206`, `:287-290`, `:298`, `:485-499`, `:520-535`, `:864`, `:1197`) | `<ProgressReport/>` content, and the pass and activity bookkeeping (`:501-512`), which the gate reads |
| `delegate_report.md:25` timer clause | `stop-delegate-continue.py`, `session-start-delegate-resume.py` |
| `showrunner_timer.sh` (its doc reader becomes `production_check.sh`) | `send.py`, unchanged |

## Platform

One `nate.jobs.session-notifier` entry in the nixos repo's `modules/common` becomes a systemd user timer on Linux and a launchd agent on the Mac (`modules/linux/nate-backend.nix:76-78`, `modules/darwin/nate-backend.nix:80-97`). The Linux backend needs an `AccuracySec=1s` option; systemd's default is 1 min. Units do run on the Mac (`prepare_session.sh:21-23` handles its python), so the Mac gets the job too. Today the showrunner timer is Linux only.

## Migration

1. **Notifier and showrunner.** `notifier.sh`, `production_check.sh`, the nix job on both machines, and `Bash(zsh ~/.claude/scripts/message/notifier.sh *)` in place of `settings.json:54`. Stop the old timer, start the instance, compare two ticks in the log. Edit `produce.md` (StartUpdates, Wrap), `dailies.md` (clock step) and `interval.md`. Delete `showrunner_timer.sh`.
2. **Units.** `delegate_check`, the `prepare_session.sh` / `end_session.sh` lines, `restart` and next_due in `progress_history.py`, `/unit:interval`, the deletions above, tests. For live runs, create one instance per `/tmp/claude/delegate/active/*` marker and tell each unit director to re-read `delegate.md`.
3. **Watch.** `unit_status.sh` flags `TICKS FAILING` when a unit has running work and no instance or two failed sends in a row, so relay failures (the weekly-limit `QUEUED` lines in fire.log on Oct 3) reach the dailies.

**Decided:** one 15 s job, not a timer per instance (risk: a nix change on each machine). Ticks only while work runs, as today. Every report restarts the clock. Each tick costs one Sonnet relay (about 9 s, `send.py:63-64`), 24 an hour for six units, accepted under the weekly-usage rule.

## Open question

1. Should the implementing session make the `settings.json` edits (the allow rule, and removing the Stop hook) and the nixos-repo edit on both machines, or will you apply those yourself?

## Showrunner decisions (2026-10-04, before planning)

The user approved this lane ("relatively short lived"). These settle the proposal's open points:

1. **No nix job, no rebuild.** The tick job is created by `notifier.sh ensure`, which does nothing when it already runs. Linux: a `systemd-run --user` transient timer every 15 s with `AccuracySec=1s`. Mac: a user launchd agent with `StartInterval` 15 that `ensure` installs. StartUpdates, `prepare_session.sh` and the session-start resume hook call `ensure`, so a reboot heals at the next start or resume. Reason: a nix job needs a sudo rebuild on both machines while the user travels.
2. **settings.json stays the user's.** This lane edits no `settings.json`. Its last checkpoint notice gives the showrunner the exact edits (the notifier allow rule, removal of the progress-timer Stop hook entry) to relay. Until the user applies them, the Stop hook script exits quietly when the session has a notifier instance, which is a script change, not a settings change.
3. **Cutover order.** The showrunner's own timer moves first; the showrunner checks two ticks in the log. Live units then move one at a time, each at its own checkpoint, through the showrunner. No unit changes mid-phase.
4. **Size.** Two or three phases. Keep it short.
5. **Repo.** Work directly in `~/.claude` on `main`. Commit only this lane's own files (other sessions leave unrelated dirty files there) and push after each checkpoint. There is no merge branch; the checkpoint notice still goes to the showrunner.
