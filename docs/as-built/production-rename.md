# Production rename

## What it is

`scripts/production/rename.py` changes a registered production's slug. It is
operation 5 of `docs/plans/staff-lifecycle.md`: write `name`, move the folder
(the folder move), run `git branch -m`, set the displays. `/showrunner:rename`
gives the order: `plan <old> <new>`, `apply <old> <new>`, then
`unit <new> <unit>` for each non-retired row. `checkout <new> [<path>]` moves
the showrunner checkout. Old names resolve through a `**Formerly:**` line in
the production doc. A merge branch named for the production (`<old>`) takes
the name `<new>`. A merge branch with another name (`main`, `init/catalyst`)
keeps its name.

## Files and readers

| File | Role |
| --- | --- |
| `scripts/production/rename.py` | Plans the changes, applies them and renames each unit. |
| `scripts/production/rename_state.py` | Supplies `RenameRefused` and the atomic `write_text` writer. |
| `scripts/production/unit_move.py` | Supplies `PathMove`, `move`, `git`, the branch tips, the production lock and `move_folder` (the folder move). |
| `scripts/production/tidy_wrapped.py` | Supplies `SLUG`, the reserved `SHARED` names and `owned_paths(slug)`, the six owned state paths (the state folder last). |
| `scripts/production/rename_outside.py` | The apply state outside the showrunner state folder: /next pins, change-review Production cells, children's Parent production line, the bevy upgrade watch, the derived unit board, a slug-named log. |
| `scripts/production/production_units.py` | Reads the production doc, the Units table and its cells; `plan_cell_is_retired`. |
| `scripts/production/showrunners.py` | Reads timer CHECK paths and resolves Formerly names. |
| `scripts/production/unit_lookup.py` | Reads a unit's local worktree from its staff row. |
| `scripts/production/stall_watch.py` | Supplies the hashed stall-watch paths. |
| `commands/showrunner/rename.md` | The showrunner workflow. |
| `docs/production_format.md` | Production Context's Formerly field. |

```python
class PathMove(NamedTuple):
    source: Path
    destination: Path

@dataclass(frozen=True)
class RenamePlan:
    old: str
    new: str
    old_doc: Path
    new_doc: Path
    checkout: Path
    merge_branch: str              # from the doc's Merge branch line; may differ from old
    units: tuple[str, ...]         # every Units row, retired included
    plans: tuple[Path, ...]        # unit plans of non-retired rows
    timer: PathMove                # showrunner-<old> -> showrunner-<new>
    state: tuple[PathMove, ...]    # owned_paths(old) -> owned_paths(new)
    stall: tuple[PathMove, ...]    # one per unit
    staff_id: staff.StaffId        # the showrunner row whose doc is old_doc or new_doc
    # properties: message = commit subject; renames_branch = merge_branch == old;
    # branch = new if renames_branch else merge_branch
```

## Names

The production doc's filename remains the current slug:
`<slug>-production.md`. Inside `## Production Context`, a single line records
previous names, newest first:

```markdown
- **Formerly:** `old`, `older`
```

`canonical_slug(slug: str, state_dir: Path | None = None) -> str` first checks
all registered timer slugs, then reads their checked docs for this line inside
Production Context. A current slug wins over another production's former name.
An unknown name passes through unchanged; the caller keeps its own existence
checks. Unreadable timers or docs do not claim aliases. `production_slug(doc)`
still reads only the filename.

Showrunner name, address, named-runner and tree operations accept former names.
Unit lookup normalizes both queries and discovered session marks, keeping
duplicate detection. A slug with an existing `showrunner-<slug>` notifier
directory is current and needs no timer-doc read; any other slug resolves
through `canonical_slug` once per distinct discovered slug. Stall-watch
likewise resolves each distinct inherited process slug once per call, so
detached work with old marks still counts as running.

## Apply

`plan` writes nothing and prints numbered items in apply order. New slugs
match `[A-Za-z0-9][A-Za-z0-9_-]*`, exclude `footers-off`, `review-paused`,
`outstanding`, `counts` and `reports`, must be valid git branch names, and
cannot be a current timer name. The source needs a readable update timer
whose CHECK names its production doc. The doc needs Production Context and a
Merge branch line. After the timer moves, the old name resolves to the new
one, and the newest Formerly name is the old name. So the source argument
accepts the old, the new or a former name.

`apply` holds the staff, design-role and production locks. When nothing is
left to change, it prints `unchanged`. Otherwise it writes the staff `name`
(`staff.rename`), then runs these steps in order:

| Step | Change |
| --- | --- |
| `_rename_doc` | Add or extend Formerly as the first bullet of Production Context, set the Showrunner session line, write the new doc, point CHECK at it, `git mv` the doc and write the staff `doc` path. |
| `_rewrite_plans` | Rewrite unit-plan Production headers (both header forms) and their production-doc references; add a header where a plan has none. |
| `_rename_timer` | Rename `showrunner-<old>` to `showrunner-<new>` and keep the updated CHECK. |
| `_rename_showrunner_state` | Move `footers-off/<slug>`, `review-paused/<slug>.json`, `outstanding/<slug>.json`, `counts/<slug>`, `reports/<slug>.json`, then each entry of `<slug>/` into the new directory; rewrite matching `*/parent` contents; each held design the showrunner holds takes `ShowrunnerHolder(<new>)` (`design_roles.move_holder`). Then `rename_outside.apply`: move the `next-`, `proposal-` and `question-<slug>` pins (and their `-all-below` forms) and rename their `<old>: ` item keys; rewrite Production cells naming `<old>` on the change-review list; point each child doc's Parent production line at the new doc; move the bevy upgrade record and notifier job (`RUN` names the new doc, `FROM` the new instance) and drop its lock; remove the derived `unit-board/<old>.json`; a Log line naming `<old>-log.md` takes `<new>-log.md`, the log and its `-recovered` copy move, and an info/exclude line naming an old one gains one for the new. |
| `_rename_stall_watch` | Move each Units row's stall-watch file from `sha256(old\0unit).json` to `sha256(new\0unit).json`. |
| `_rename_unit_workers` | Move the units' worker ledgers from the old production to the new one (`workers.move_unit`). |
| `_retarget_phase_notes` | Retarget phase notes from the old production to the new doc (`phase_table.retarget_phase_notes`). |
| `_rename_branch` | A merge branch named for the production: `git branch -m <old> <new>` and update the Merge branch line. Then commit the changed documents and push the merge branch with upstream. |
| `_set_displays` | Set the title of the herdr pane that runs the showrunner row's `current_session` to the row's `name`. |

A step that fails prints `anomaly: <step>: <reason>` on stderr, and the next
step runs. Apply then prints `<old> is now <new>`. Each step skips work that
is already done, so a rerun finishes the rest. Apply is done when no copy of
the old name is left: the staff name and doc path, the old doc, timer, state
and stall-watch paths, a plan header, the old merge branch, uncommitted doc
or plan changes, and a merge branch that origin does not have at its local
tip. Then a rerun prints `unchanged`.

Retired Units rows (Plan cell starting `(retired`) still contribute
stall-watch moves, but never unit-plan paths. Other Plan cells select
existing, tracked `docs/plans/*.md` paths quoted in backticks or given as a
single bare path, ignoring as-built paths and prose. A plan path that is
missing or not tracked in git is skipped. Saved logs and backups move without
content edits.

The document and plan changes form one commit on the merge branch after the
rename (`RenamePlan.branch`): `chore(<new>): rename production from <old>`.
Apply leaves unit branches, worktrees and their Units-row cells to `unit`.

## Unit changes

`unit <new> <unit>` holds the same locks as apply. It resolves the production
through `canonical_slug` and reads the unit's staff row and its local
worktree. The unit needs one non-retired Units row with a Branch column. The
old branch is `<old>-<stem>` and the new branch is `<new>-<stem>`. The tip is
read from the checkout's old branch, or from the new branch on a rerun.

1. The destination is the worktree's sibling with the `-<old>-<stem>` suffix
   replaced by `-<new>-<stem>`. A folder without that suffix keeps its path.
2. When the folder changes, `unit_move.move_folder` stops the unit session
   and its workers, runs `git worktree move`, writes the staff `worktree` and
   starts them in the new folder. Each held design the unit holds takes
   `UnitHolder(<new>, <unit>)` and the new worktree paths
   (`design_roles.move_holder`).
3. When the worktree is on the old branch, `git branch -m` renames it.
4. The Units row's Worktree cell becomes `—` and its Branch cell names the
   new branch. The commit is `chore(<new>): rename unit <unit> from <old>`.
   Then the merge branch is pushed. A Units row at HEAD that already names the
   new branch makes no second commit.

`unit` prints `<unit>: moved to <destination>`, or `<unit>: unchanged` when a
rerun has nothing to change. It does not push the unit branch.

## Showrunner checkout

`rename.py checkout <slug> [<path>]` (`rename_checkout.checkout`) moves the
showrunner's own checkout, a linked worktree, to a new path. The default path
is the checkout's basename with a Formerly name replaced by the slug
(`claude-build-followups-trunk` becomes `claude-natedev-trunk`); a basename
with no Formerly name in it needs the path argument. The showrunner cannot
move the folder it runs in. `rename_checkout.checkout` calls `stop_resume.stop`,
moves the checkout with `rename_checkout.move`, then calls `stop_resume.resume`.
The move runs `git worktree move` and `git worktree repair`. It updates the
Claude project folder, folder trust, timer commands, child doc parent lines,
herdr pane cwd keys, and the Showrunner checkout line. Processes still inside
the folder block the git move. A failed step reports its error; a rerun finishes
idempotent updates. A later run pushes the doc commit. Only the doc line states
where the checkout is; logs and design docs keep historical paths.

The CLI exits 0 on success, 1 with `rename: <reason>` on an error, and 2 for
invalid arguments or a staff error.

## Invariants

- **History is never rewritten.** Existing commit subjects, log lines and
  transcript contents keep the old slug. No step force-pushes or resets.
- **Every write is atomic.** Files go through `rename_state.write_text`;
  directories move with one `rename`, so source and destination share a
  filesystem; worktrees move with `git worktree move`.
- **Old slugs resolve forever** through the Formerly line, which is added
  inside Production Context (`_rename_doc`) before the timer directory moves
  (`_rename_timer`).
- **`canonical_slug` never raises** and returns an unknown slug unchanged.
- **CHECK names a readable doc at every point**: the new doc is written and
  CHECK points at it before `git mv` removes the old path.

## Calibration and gotchas

- Claude's projects-dir name replaces every non-alphanumeric character of the
  worktree path with `-`, not only `/`.
- CHECK is rewritten by shell-parsed token (the argument after
  `production_check.sh`) and re-quoted with `shlex.quote`, since it can hold a
  shell-escaped path.
- A rerun after a `git mv` that moved the file but not the index removes the
  old doc from the index explicitly.
- The old unit branch tip is checked before the unit commit and before its
  push.
- `_lookup_slug`'s current-name fast path keeps the shutdown inventory within
  its timer-read budget: membership and missing-target checks share one read.
- A `**Formerly:**` line outside `## Production Context` is ignored.
- `test_unit_lookup` uses productions with no update
  timer; a strict lookup at those callers breaks them.
- Every path honors the state-dir environment variables; never run the verbs
  against live state when testing.
- Rename moves only the per-slug state listed above. These stay under the old
  slug: `outstanding/<slug>.history.jsonl` and the `review-health-<slug>`
  notifier instance. `_rename_showrunner_state` moves the `bevy-upgrade-<slug>`
  instance and the `BEVY_UPGRADE_STATE_DIR/<slug>.json` record, and each held
  design role through `design_roles.move_holder`
  (`docs/as-built/long-lived-workers.md#lasting-roles`).

## Why

- **Unknown slugs pass through.** A resolver that raised on an unknown slug
  would break legacy reviewer-state lookup and unit lookup for timer-less productions.
- **A failed step does not stop apply.** It prints one anomaly line, and the
  next step runs (`docs/plans/staff-lifecycle.md`, section 4). A rerun
  finishes the work.
- **Units move one at a time.** Each `unit` call moves one folder through the
  folder move, which stops and starts that unit.
- **Only a merge branch named for the production is renamed.** A merge branch
  such as `main` is shared history, not the production's name; renaming it
  would move every checkout that tracks it.
- **Merge history survives the rename.** `merge_checkpoint.py` derives the
  last merge per unit from subjects `Merge <unit> phase <N> (<hash>)`, which
  hold no branch name, so old subjects need no rewrite.
- **Commit detection reads HEAD.** Merges onto the merge branch between unit
  reruns must not cause a second unit commit.

## State roots and tests

`test_rename.py` covers planning, state moves and apply reruns.
`test_rename_rerun.py` compares a rerun after a failed step with one
uninterrupted run. `test_rename_units.py` covers unit reruns after a later
staff folder change and after a failed staff write. `test_rename_workers.py`
covers worker ledgers, held roles and the design-role warning.
`test_rename_finish.py` covers a merge branch not named for the production.
`test_rename_phase_notes.py` covers phase-note retargeting. These suites share
`rename_test_support.py`: each class seeds Git once and restores independent
copies at the same paths, while commands call the real handlers in-process.
One apply case keeps the CLI subprocess boundary.
`test_rename_checkout.py` moves a real linked worktree and repeats the move,
and runs the `checkout` verb's stop and push.
`test_showrunners.py`, `test_unit_lookup.py` and `test_stall_watch.py` cover
Formerly resolution, old session marks and detached work.

```sh
python3 -m unittest scripts.production.test_rename scripts.production.test_rename_rerun scripts.production.test_rename_units scripts.production.test_rename_workers scripts.production.test_rename_finish scripts.production.test_rename_phase_notes scripts.production.test_rename_checkout scripts.production.test_unit_lookup scripts.production.test_showrunners scripts.production.test_stall_watch
```

`unit_lookup.py` is shared: the shutdown inventory
(`scripts/shutdown/test_inventory.py`) and message roster
(`scripts/message/test_roster.py`) suites also exercise it, so a change there
runs the full merge-gate suites:

```sh
python3 scripts/production/merge_gate.py --repo <checkout> --tests-only
```
