# Staff and lifecycle: the simplest system

Owner: the sdlc showrunner. Written 2026-11-03 at the user's word: "all i want is the simplest possible system", "the staff is the one place", "everything is too complex".

Every unit cuts its staff and lifecycle code to this page. A phase in this work deletes more lines than it adds. At the merge I count the lines; a phase that grows the code goes back.

## 1. The one record

`~/.local/state/staff/staff.json` holds one row for each showrunner, unit, worker, and producer:

| Field | Meaning | Changes when |
|---|---|---|
| `id` | the first native session id | never |
| `name` | the shown name | rename |
| `kind` | showrunner, unit, worker, or producer | explicit role conversion |
| `unit_type` | `direct`, `merge`, or `style`, for unit rows only. | Explicit type selection at admission. |
| `parent` | the `id` of the row it reports to | transfer |
| `current_session` | the native id that runs now | `/clear`, family switch |
| `worktree` | Machine and folder for a unit or producer. | Folder move, rename. |
| `doc` | Machine and document for a showrunner or producer. | Document move. |
| `home` | Account configuration folder. | Account selection or proved process evidence. |
| `run` | The open run folder, or no run. | Run start, reopen, or finish. |
| `paused` | The session pause state. | Pause or resume. |
| `exited` | The session exit state. | Exit or resume. |

A producer has no parent and can start a staff path.
An old unit row without `unit_type` reads as `direct`.
An invalid unit type makes the row unreadable.
Readers discard the field on other staff kinds; the next write removes it.
Code uses `UnitDetails` for a unit and `NotUnit` for other members.
Promotion removes the type; demotion selects an explicit type, default `direct`.
New and changed unit rows write the type explicitly.
The type selects the unit command for launch, resume, account move, rename, and compaction.
Morty, brain, and dwight use kind `producer`.
Each keeps its own command under `commands/producer/` and its own job.
The [staff contract](../as-built/staff-accessors.md#producer-rows-and-commands) defines the current producer rows.

No other file, table, journal, ledger, roster, record or cache stores any of these facts.
One exception: a pending producer admission record holds the reserved pane until staff admission and pane publication succeed.
Pending unit admission and director role records keep the selected type until the operation completes.

## 2. Found, never stored

1. Claude transcript or Codex thread: found by `current_session`.
2. Backend family: the native session that `current_session` identifies.
3. Branch: `git -C <worktree> branch --show-current`.
4. Pane: the herdr pane whose process runs `current_session`.
5. Production: the `parent` chain.
6. Workers of a unit: the rows whose `parent` is the unit.

Run state keeps fixed owner and dispatch participant IDs.
Worker records keep fixed staff IDs and dispatch telemetry.
Mesh roster entries use fixed staff IDs as keys.
Run-owner lookup requires a fixed staff ID.
Pass lookup requires a fixed worker ID and a matching current parent.
Historical reads preserve the recorded folder, branch, session, and family.
Current readers validate each worker against its staff parent.
Historical identities, roles, folders, and branches describe past events only.

Displays (the herdr tab label, the Claude session title) show `name`. Rename sets them. No code reads them back.

Producer launches select their family from `[producers]` in `config/models.conf`.
The native session supplies evidence of the running backend.

## 3. The operations

Each operation is a short list of plain steps. A rerun with nothing to change prints `unchanged`.

1. **Stop:** stop each session found by `current_session`. Write nothing.
2. **Start:** resume `current_session` in the staff folder through the shared resume path.
   Claude uses `--resume <id>`.
   Codex uses `resume <id>`.
   Producers use their configured family, model, and effort from `[producers]` in `config/models.conf`.
   Codex producers use the workers account; Claude producers use the showrunners account.
   Each producer uses its own tab in `PRODUCER_WORKSPACE`, normally `producers`.
   A configured producer without a row uses the shared first-admission path.
   [Producer launch and admission](../as-built/shutdown-and-restart.md#producer-launch-and-admission) defines its reservation and identity checks.
3. **Stop all, restart all** (user, 2026-10-08): one loop. Stop each live row, then start each.
4. **Folder move:** stop the unit and its workers. Run `git worktree move`. Write `worktree`. Start each.
5. **Rename** (user, 2026-11-04): write `name`. A same-name rename only sets the displays. A new name replaces the old name at the end of the folder name and the branch name; the prefix and the parent folder stay. Move the folder (step 4). Run `git branch -m`. Set the displays. A folder or branch that does not end with the old name prints one anomaly line and keeps its name.
   - A new unit folder is `<checkout>-<unit>` next to its production checkout: `~/rust/cargo-liner-port`, `~/worktrees/claude-natedev-builds-aws`.
   - The sdlc units and organon sit at `~/worktrees/<unit>` with no prefix. They keep those folders.
6. **Transfer:** write `parent`. Move its herdr tab, and its workers' tabs, into the new production's group (a display; step 3 restores the same order).
7. **`/clear`:** write `current_session`.
8. **Family switch:** start a session of the other family in the same slot, with the transcript extract. Write `current_session`. `id` and `name` stay.
9. **Retire:** stop the unit and its workers (step 1). Remove the worktree and the branch. Write `worktree` state `removed`; the row stays, so a resume by `id` still works. Refuse only on refusal 4; an uncommitted `Project started` plan line alone is not work.

## 4. The only refusals

A check stays only if it prevents a catastrophe:

1. The target folder exists (git's own error).
2. A second operation on the same row runs at the same time (one lock for each row).
3. A second copy of a running session would start.
4. Work would be deleted: uncommitted changes, or commits that the merge branch does not hold.
5. A signal, keystroke or pane close would reach a process that is not the target (a reused pid, another conversation, a pane with other live work).

Bad input (an unknown or unclear name, a session on the other machine) is an error message, not a refusal.

Anything else unexpected prints one anomaly line and the operation continues.

## 5. What goes

| Unit | Deletes |
|---|---|
| devtools | every other refusal in `unit_move.py`, `rename_unit.py`, `transfer_unit.py`, `retire_unit.py`; their journals; the retired note and saved branch ref; the rename sweep that classifies files; every stored folder-path copy (4v, 4w); the Units tables: every reader reads staff (4d, on main 3ffe2193f), and 4u deletes the writers and the tables (until 4u lands, a retire deletes the unit's row) |
| herdr | `production_tree.py` (4b); the Production header and Gates table (4o); the plan's `> **Production:**` header, a copy of `parent` (4c; the plan path is in `.unit.toml`); the seed importer (4d); legacy pane marks (4e); stop, resume and restart refusals |
| models | the worker ledger's adoption journal; repair-roster; the ledger-to-roster check; legacy identity readers (one migration run); per-thread records in `design_roles`; any seat-to-thread map other than staff |
| shutdown | refusals in `restart.py` and `shutdown_record.py`; the restore record keeps only the list of row ids that were live. Done: the `launch_permission.py` and `folder_trust.py` gates (deleted in b9fd6ada4) |
| accounts | `cutover-home` (after the Mac cutover); the Mac session counts fetch; the resume retry loop |
| phases | wait recovery; unknown-owner states; queue_place cleanup redesign; verify staging recovery; `process_start.py` unless it deletes the `/proc` copies (net negative) |

## 6. Every accessor

`docs/as-built/staff-accessors.md` lists every file that reads or writes the staff file, and each copy the user approved. Use it for a staff migration.

## 7. One way to start a worker

User rule, 2026-11-04: "it should be impossible to start a worker in more than one way". The sdlc showrunner owns this rule.

- The director submits assignments through `assign.sh`, for either native family. It returns after prompt delivery.
- Native worker admission and pane operations implement worker start and reuse. The mesh command and its subcommands are removed.
- `assign.sh` refuses when no live Claude or Codex process is in its parent chain (a `nohup`, `setsid` or `&` start).
- The completion hook keeps the pass active while detached work remains. A result returns to the same native worker conversation.
- `scripts/direct/test_one_launch_path_sources.py` fails when a file outside the launcher starts or follows a worker.
- Worker panes (herdr-worker-panes decision 11) change what is behind `assign.sh`; they add no second way.
