# natedev shutdown-unit phases

> **Unit:** agents (was shutdown-unit), under sdlc · branch `sdlc-agents` · as-built `docs/as-built/shutdown-and-restart.md`

Earlier phases of this unit are recorded in the as-built and in the branch history. This plan holds Phases 4 to 16.

## Phase 4: herdr's shutdown items and the pane-shell stop — done

Landed in checkpoint `6cfdcbb1` (branch `f0f21af6`, main `fe789e33`, 2026-10-30). Showrunner decision B (02:34 EDT): land the stop fixes and herdr's items, with no pane-close step.

- herdr's items: F032 (record locks inside the 66 s stop limit), F045, F046 and F049 (restart timer restore). Their routes are closed.
- The herdr-unit transfer failure: a stop no longer targets the interactive pane shell, and it reports "M of N stopped". A stop retry never signals a session that resumed elsewhere. Cancel withdraws a failed stop whose session runs again.
- Transfer, rename and retire of a unit whose Claude runs under an interactive pane shell refuse before any stop, with one line that names the shell.
- Review: five closure passes, four repair rounds. Merge gate: full suite green.

## Phase 5: pane close for unit operations — done

Landed in checkpoint `3c24f9de` (branch `dcca53de`, 2026-10-30); the showrunner holds the merge until the user's morning restart test.

- Transfer, rename and retire now complete for a unit whose Claude runs under an interactive pane shell. One guarded close (`unit_move.close_unit_pane`) closes only the unit's exact pane, after a clean stop and worker cleanup, under launch admission and the shutdown-record lock. A foreign, unknown or nested occupant of any UID refuses it; herdr's one ancestor-aware lookup (`herdr.pane_scopes`) finds the pane scope.
- A process in the unit's checkout blocks retirement and is never stop authority (F003). `fake_unit_host` is the one pane-host fixture (F007).
- A stop that takes over a restoring timer keeps the restore intent (F012).
- `restart --all` keeps a single-session, `--only` or self stay-down stop down, never a `down --all` or reboot entry; a `down --all` takeover records account scope, so the morning path restores every session.
- Add-on: the Mac offload test wrapper publishes its state files atomically.
- Review: two passes, two repair rounds. Gate: every suite passed across two runs; each run had one red outside this phase (memory cap, `test_codex_mesh` hang), held by the showrunner for devtools and sdlc fixes. The Phase 4 routes stay open until sdlc's `close-route` fix lands.
- Live check, started by the showrunner: transfer of a throwaway pane-shell unit.

## Three-step stop and resume (user go 2026-10-30, via natedev and sdlc)

The user's rule: "1 yes 2 yes 3 yes - plus a resume message". There is one way for each step. Each phase deletes what it replaces as it lands. herdr-unit owns move (staff.json parent and the live tab move) and the one tab reader (the session id from a tab). This unit uses that reader and writes no second one.

- **Stop:** end the session's Claude process at once. There is no stopping record, no ready mark, no deadline and no conductor. Codex workers keep their threads. A stop does not touch them.
- **Resume:** read the session id from its own tab, type the session's resume command into that tab, then send the session one message. The message says that the session was stopped and resumed, why, and how to resume its workers.
- **Account move:** stop, then resume on the target account.

### Phase 6: stop, resume and account move

- One resume command builder, made from `restart._resume_parts` and `_resume_shell`: the flags, the account environment and `accounts.py exec <target>` for a move. `pane_restore.claude_arguments` and `herdr_sessions.py resume` call it, so the copies go.
- `agents stop <session>`: TERM to the session's Claude pid, then KILL if it is still alive after a short grace. Nothing is written.
- `agents resume <session> [--to claudeN] --why <why>`: herdr's tab reader gives the session id. The command is typed into the tab with herdr's typing function. One message follows through send.py; its text comes from `restart_note` / `account_move_note` and the worker resume text, joined into one builder.
- Account moves switch to stop + resume: `accounts.move`, `move_group`, `move_sessions`, `showrunner_move.py` and the automatic refusal moves in `refusal_watch.py`.
- Deleted: the move path through `down --restart --to` (`_move_down_argv`, the moves journal, `follow_move`, `_prove_move`, `tell_showrunners_outcome` for moves).
- Needs from herdr-unit: the tab reader (today the only true reader is shutdown's private `pane_restore._restored_panes`, from herdr's `pane list` `agent_session`; herdr lifts it into `herdr.py` as the one reader). It must give the id of a tab whose Claude has exited.

### Phase 7: callers off the old stop, and the conductor deleted

- Callers switch to stop / resume: `retire_unit.py` (down + forget), `rename_checkout.py`, `add_unit.py` attach, `nix_env/heal.py`, `unit_watchdog.py` restart, the `shutdown.holdout` pushes in `push_catalog.py`, and the guard hook rule for `now`.
- The docs switch: `<PauseForMove>` in `commands/unit/direct.md` goes; `commands/showrunner/produce.md`, `move.md`, `commands/shutdown.md` and `docs/accounts.md` describe the three steps.
- Deleted: `settle.py` (the conductor, `down`, `now`, `ready`, `cancel` and the pause texts), `stop.py` (the bounded stop and its claim), and the matching `shutdown.py` verbs.

### Phase 8: the record store deleted

- Deleted: `shutdown_record.py` (session records, account holds and their locks), the record-driven restart in `restart.py`, recovery snapshots, `records`, `holds`, `held-sessions`, `launch-blocked` and `forget`.
- `pane_restore.py` (boot restore) stays and uses the Phase 6 builder.

### Phase 9: the transfer journal and the long rename steps deleted

- `rename_unit.py` stops with Phase 6 stop, moves with herdr's move, and resumes with Phase 6 resume; the record rewrite and the `shutdown.py restart --only` relaunch go.
- `rename.py` uses the same stop, move and resume. herdr-unit deletes the `transfer_unit.py` journal at its own checkpoint (sdlc, 2026-10-30); this phase touches `transfer_unit.py` only if a stop or resume call is left in it.
- The as-built docs that describe the old machinery are corrected: `shutdown-and-restart.md`, `unit-rename.md`, `unit-transfer.md`, `production-rename.md` and `long-lived-workers.md`.

## One agents command (user decision 2026-11-07, via sdlc)

The user's rule: one name, `agents`. One skill, `/agents`, and one program, `agents`. Everyone uses only these. The commands are status, pause, exit, resume, retire, restart, rename and move. `agents` with no arguments prints the same short usage as `agents help`.

Each phase deletes the skill and the code it replaces when it lands. `staff.py` stays as the code that stores the staff record; no person or skill runs it. herdr-unit is the partner for worker rename and pane files. The accounts unit is the partner for move. Phases 10 and 15 run side by side. Phase 11 follows Phase 10, because it adds commands to the program Phase 10 makes. Phases 13 and 14 wait on Phase 12, because the three write the same staff rows. Phase 16 runs beside Phase 11. Phase 12 starts when a writer is free.

### Phase 10: the agents program and the /agents skill — done

- `agents` and `agents help` print one short usage.
- `agents status`, `agents restart` and `agents resume NAME`. Resume of a paused unit sends it a message to continue. Resume of an exited unit starts it again on the current role account, the same as `resume <unit>` at the shell, which stays as the short form.
- Deletes `/shutdown` and the shutdown.py command surface that `agents` replaces.

### Phase 11: pause and exit

- `agents pause NAME`: stop now by default. Workers stop at once, and the unit holds at its prompt. The pause record holds the time, the caller and why. The board shows `paused`.
- `agents exit NAME`: the same stop rule. The director and its workers quit, the herdr tab stays at the shell prompt, and the staff row stays. The board shows `exited`.
- "finish the current step" on either command lets workers finish their current task first, then hold or exit.
- Deletes the `staff.py` pause and resume commands and the pause and resume text in `/showrunner:units`.
- No draft keep: a move does not save or type back unsent composer text (user 2026-11-07, via sdlc: "this is overkill").

### Phase 12: retire, and resume of a retired unit

- `agents retire NAME` removes the unit from active staff. Its row stays only as a retired record. The board does not show it.
- `agents resume NAME` of a retired unit refuses and names `--retired`. The showrunner asks the user yes or no before it runs `--retired`.
- Replaces `retire_unit.py` and `resume_unit.py`. Deletes `/showrunner:retire_unit` and `/showrunner:resume_unit`.

### Phase 13: rename

- `agents rename OLD NEW` for units, showrunners and workers. Workers are in herdr panes, so herdr-unit gives the pane rename.
- Replaces `rename.py`. Deletes `/showrunner:rename` and `/unit:rename`.

### Phase 14: move

- `agents move SESSION_OR_GROUP ACCOUNT`, with natedev's accounts unit. `accounts.py` keeps the account board and the queued moves (`accounts.py next`).
- Replaces `showrunner_move.py`. Deletes `/showrunner:move`.

### Phase 15: the launch record takes the role account — done

- A staff member takes its account from the current role account at launch. A session with no staff row opens on its family default: the workers account for Codex, the units account for Claude.
- Copies of staff data leave `pane_launch.PaneLaunch`. Pane data stays there.
- Also touches `scripts/herdr/pane_launch.py`; sdlc confirmed 2026-11-07 that it is free.

### Phase 15b: worker list gives work, account and home — done

- `worker.py list --json` gives `work`, `account` and `home` for each pane worker, computed at read time. A worker with no backend entry reports `AccountUnavailable`.
- `member_account.resume_account` is the one account rule for resume, restart and the worker list.

### Phase 16: a launch trusts its folder; the draft stash goes

- A launch by our tools writes Claude trust for the target folder in the target account before it starts. It reuses the `copy_claude_trust` write, in one place. No user step (user rule 2026-11-07: a move or start needs no user work).
- When trust cannot be written, the launch prints one anomaly line and stops cleanly.
- Deletes the herdr 23 draft stash in `herdr.py` and its three tests (change row C1211). The user does not want the feature.
