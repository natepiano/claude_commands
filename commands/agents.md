---
description: Show, pause, exit, retire, rename, move, resume, stop, or restart sessions.
argument-hint: 'move SESSION_OR_GROUP ACCOUNT [--dry-run] [--why TEXT] | rename OLD NEW [--to-doc PATH] [--to-branch NAME] | retire NAME | pause NAME --why TEXT | exit NAME --why TEXT | help | status [ACCOUNT] [--json] [--here] | resume NAME [--retired] [--why TEXT] [--to ACCOUNT] | restart [ACCOUNT] [--all] [--dry-run] [--only ID[,ID...]] [--here] | stop SESSION [--dry-run] | stop --all [--dry-run] | remove-stray NAME SESSION'
---

Use the `agents` program.

- `agents help`: show the usage. `agents` without arguments prints the same text.
- `agents status [ACCOUNT] [--json] [--here]`: show live sessions. `--here` selects this machine.
- `agents pause NAME --why TEXT`: stop the workers now. Let the director finish its turn and hold at its prompt.
- `agents exit NAME --why TEXT`: stop the director and its workers. Keep their staff rows, tabs, and shells.
- Add `--finish-step` to pause or exit after the workers finish their current tasks.
- Use `agents pause --production SHOWRUNNER [--except UNIT ...] --why TEXT` to select a production.
- Use `agents resume --production SHOWRUNNER` to resume its units.
- `agents resume NAME [--retired] [--why TEXT] [--to ACCOUNT]`: resume a name, staff id, session id, or exact Codex thread name.
- `agents rename OLD NEW [--to-doc PATH] [--to-branch NAME]`: rename a unit, showrunner, or worker.
- `agents move SESSION_OR_GROUP ACCOUNT [--dry-run] [--why TEXT]`: move Claude sessions to a numbered account.
- `agents retire NAME`: remove the unit worktree and branch. Keep its retired staff row and conversations.
- `agents resume NAME --retired`: restore a retired unit and its retained workers.
- Before running `--retired`, ask the user yes or no. Run it only after yes.
- `agents restart [ACCOUNT] [--all] [--dry-run] [--only ID[,ID...]] [--here]`: restore saved rows or restart local live rows with `--all`.
- `agents stop SESSION [--dry-run]`: stop the session and its workers. Keep their rows, conversations, and panes at the shell.
- `agents stop --all [--dry-run]`: save the live local staff rows, then stop them without a restart.
- `agents remove-stray NAME SESSION`: remove a stray producer conversation on this machine.

First admission adopts one proved live Claude session or Codex thread with the producer name.
It also checks live agents in matching tabs and panes in `PRODUCER_WORKSPACE`.
Adoption writes the staff row with that native session. It launches nothing.
Two live sessions cause refusal. An unproved live session also causes refusal.
The refusal names each conflicting session and pane. It writes no staff row.
An empty labeled shell does not count as a live session.
Recovery checks live sessions before it resumes a retained conversation.
Recovery adopts a different proved live session and removes the pending admission record.
Stop selects the producer row's `current_session` when several tabs have the same label.
The Claude session registry proves a plain start without a session ID in its arguments.

Remove a stray producer with these steps:

1. Run `agents remove-stray NAME SESSION` with the stray session ID.
2. Read the command result.

The command refuses any staff row's `current_session`.
It proves the selected process before it sends a signal.
It closes only the selected pane. Other panes in the tab stay open.
It deletes the selected transcript files across Claude account projects folders with Python.
It removes a producer admission record that matches the session or the proved pane and terminal.
It removes the matching admission record before pane closure and transcript deletion.
It saves the proved cleanup targets before the first change.
A repeated command uses that record to complete failed steps.
It deletes the cleanup record after every step completes.
A repeated command prints `unchanged` when no cleanup record or selected resource remains.

Pause and exit record the time, caller, and reason. The board shows `paused` or `exited`.
Resume removes the record. A paused director receives one continuation message.
Failed continuation delivery restores the pause and returns failure.
A stop dry run prints the selected session and workers without stopping them.
An exited director starts through the shared resume path.
Stop does not save or restore composer drafts.
Resume restores the saved folder and environment.
Codex uses the configured `workers` account; Claude units and showrunners use their configured role accounts.
Other Claude sessions use the units account; `--to ACCOUNT` overrides the account.
The default reason is `resumed by name`.
Retired units do not appear in active status or the board.
Resume without `--retired` refuses a retired unit.
A running session prints `unchanged` and returns status 0.
A closed tab uses the existing restore path.
An empty current pane can resume its own saved session.
Resume restores retained workers with their native conversations.
The short form `resume NAME [--retired] [--why TEXT] [--to ACCOUNT]` stays available from any directory.

Use a staff id, a staff path, or a unique name for rename.
A bare name first selects the calling showrunner or unit and its direct children.
A unit rename changes matching folder and branch suffixes.
A unit or showrunner rename updates its worker agent names and pane labels to `<new>-<worker>`.
Use `--to-branch NAME` for an explicit unit branch.
Keep OLD and NEW equal to change only that branch.
A showrunner document move requires both `--to-doc PATH` and `--to-branch NAME`.
A worker rename changes proved pane titles and keeps shared tab and workspace names.
Repeat an interrupted rename to complete its saved move.
Show each anomaly and the final output.
See `docs/as-built/production-rename.md` and `docs/as-built/unit-rename.md`.

Move selects a session id, an exact session name, `units`, or `showrunners`.
It stops each selected session and resumes the same conversation in its local herdr pane on ACCOUNT.
One resume message explains the move and the retained Codex workers. Workers keep their threads.
A group move selects the group's account and moves every live member on another account.
`--dry-run` previews the move. `--why` supplies the resume reason.
Each member prints one result line. Show the output unchanged.
Run the command on the machine that holds the session.
For stop or resume alone, use `agents stop SESSION` or `agents resume SESSION --why TEXT`.
See `docs/accounts.md` for account checks and refusals.

Before a power-off, run `agents stop --all`.
After login, run `agents restart`.
Stop and restart select the same live local staff rows, including producers.
Stop saves each row ID before its stop. It starts nothing.
A live session without a staff row prints one anomaly line. Stop does not save it.
Restart keeps only row IDs whose starts fail.
`--only` selects row IDs and leaves other saved rows unchanged.
`stop --all --dry-run` prints each selected row and its position with `would stop and save`.
`restart --dry-run` prints saved rows and identifies live rows outside the saved list.
Both commands change nothing.

Restart with ACCOUNT and `--all` selects live rows on that Claude or Codex account.
Selected rows restart on ACCOUNT.
Each worker matches its own account.
Filtered restarts leave unselected owners and workers unchanged.
A live worker without account evidence prints `NAME: left alone; account unknown` once.
Without `--all`, ACCOUNT filters saved rows by their last session's account.
An unknown ACCOUNT prints one error and returns status 2 before any stop.
Without ACCOUNT, selection and role accounts stay unchanged.
`--only ID[,ID...]` also limits `--all`; ACCOUNT and `--only` must both match.
If a stop includes the caller, restart continues in a detached process before the first stop.
The caller prints the restart log path and returns.
Other restarts run in the caller's process.
A row without a saved tab says so on the same line.
One anomaly does not prevent unrelated work.
Stop uses the limits in `config/shutdown.conf`.
See `docs/as-built/shutdown-and-restart.md` and `docs/accounts.md`.
