# Staff and lifecycle: the simplest system

Owner: the sdlc showrunner. Written 2026-11-03 at the user's word: "all i want is the simplest possible system", "the staff is the one place", "everything is too complex".

Every unit cuts its staff and lifecycle code to this page. A phase in this work deletes more lines than it adds. At the merge I count the lines; a phase that grows the code goes back.

## 1. The one record

`~/.local/state/staff/staff.json` holds one row for each showrunner, unit and worker:

| Field | Meaning | Changes when |
|---|---|---|
| `id` | the first native session id | never |
| `name` | the shown name | rename |
| `kind` | showrunner, unit or worker | never |
| `parent` | the `id` of the row it reports to | transfer |
| `current_session` | the native id that runs now | `/clear`, family switch |
| `worktree` | machine and folder (a unit) | folder move, rename |
| `doc` | machine and production doc (a showrunner) | doc move |

No other file, table, journal, ledger, roster, record or cache stores any of these facts.

## 2. Found, never stored

1. Claude transcript or Codex thread: found by `current_session`.
2. Family (Claude or Codex): the kind of session that `current_session` finds.
3. Branch: `git -C <worktree> branch --show-current`.
4. Pane: the herdr pane whose process runs `current_session`.
5. Production: the `parent` chain.
6. Workers of a unit: the rows whose `parent` is the unit.

Displays (the herdr tab label, the Claude session title) show `name`. Rename sets them. No code reads them back.

## 3. The operations

Each operation is a short list of plain steps. A rerun with nothing to change prints `unchanged`.

1. **Stop:** stop each session found by `current_session`. Write nothing.
2. **Start:** resume `current_session` in `worktree`. Claude: `claude --resume <id>`. Codex: thread resume by id, with cwd = `worktree`.
3. **Stop all, restart all** (user, 2026-10-08): one loop. Stop each live row, then start each.
4. **Folder move:** stop the unit and its workers. Run `git worktree move`. Write `worktree`. Start each.
5. **Rename:** write `name`. Move the folder to `~/worktrees/<name>` (step 4). Run `git branch -m`. Set the displays.
6. **Transfer:** write `parent`. Move the unit's Units row to the new production doc.
7. **`/clear`:** write `current_session`.
8. **Family switch:** start a session of the other family in the same slot, with the transcript extract. Write `current_session`. `id` and `name` stay.
9. **Retire:** stop the unit and its workers (step 1). Remove the worktree and the branch. Write `worktree` state `removed`; the row stays, so a resume by `id` still works. Delete the Units row. Refuse only on refusal 4; an uncommitted `Project started` plan line alone is not uncommitted work.

## 4. The only refusals

A check stays only if it prevents a catastrophe:

1. The target folder exists (git's own error).
2. A second operation on the same row runs at the same time (one lock for each row).
3. A second copy of a running session would start.
4. Uncommitted work would be deleted.
5. A signal, keystroke or pane close would reach a process that is not the target (a reused pid, another conversation, a pane with other live work).

Bad input (an unknown or unclear name, a session on the other machine) is an error message, not a refusal.

Anything else unexpected prints one anomaly line and the operation continues.

## 5. What goes

| Unit | Deletes |
|---|---|
| devtools | every other refusal in `unit_move.py`, `rename_unit.py`, `transfer_unit.py`, `retire_unit.py`; their journals; the retired note and saved branch ref; the rename sweep that classifies files; every stored folder-path copy (4v, 4w) |
| herdr | `production_tree.py` (4b); the Production header and Gates table (4o); the Units table's staff facts (worktree, branch, session) (4c); the seed importer (4d); legacy pane marks (4e); stop, resume and restart refusals |
| models | the worker ledger's adoption journal; repair-roster; the ledger-to-roster check; legacy identity readers (one migration run); per-thread records in `design_roles`; any seat-to-thread map other than staff |
| shutdown | refusals in `restart.py` and `shutdown_record.py`; the restore record keeps only the list of row ids that were live. Done: the `launch_permission.py` and `folder_trust.py` gates (deleted in b9fd6ada4) |
| accounts | `cutover-home` (after the Mac cutover); the Mac session counts fetch; the resume retry loop |
| phases | wait recovery; unknown-owner states; queue_place cleanup redesign; verify staging recovery; `process_start.py` unless it deletes the `/proc` copies (net negative) |
