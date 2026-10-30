# natedev shutdown-unit phases

> **Production:** `docs/plans/natedev-production.md`
> **Unit:** shutdown-unit · branch `natedev-shutdown` · as-built `docs/as-built/shutdown-and-restart.md`

Earlier phases of this unit are recorded in the as-built and in the branch history. This plan holds Phase 4 and Phase 5, the follow-up Phase 4 routed.

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

- Deleted: `shutdown_record.py` (session records, account holds and their locks), the record-driven restart in `restart.py`, recovery snapshots, `records`, `holds`, `held-sessions`, `launch-blocked` and `forget`, and the launch barrier in `launch_permission.py` that only account holds fed.
- `pane_restore.py` (boot restore) stays and uses the Phase 6 builder.

### Phase 9: the transfer journal and the long rename steps deleted

- `rename_unit.py` stops with Phase 6 stop, moves with herdr's move, and resumes with Phase 6 resume; the record rewrite and the `shutdown.py restart --only` relaunch go.
- `rename.py` uses the same stop, move and resume. herdr-unit deletes the `transfer_unit.py` journal at its own checkpoint (sdlc, 2026-10-30); this phase touches `transfer_unit.py` only if a stop or resume call is left in it.
- The as-built docs that describe the old machinery are corrected: `shutdown-and-restart.md`, `unit-rename.md`, `unit-transfer.md`, `production-rename.md` and `long-lived-workers.md`.
