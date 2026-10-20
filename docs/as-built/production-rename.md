# Production rename

## What it is

`scripts/production/rename.py` changes a registered production's slug, then
moves each unit between its runs. `/showrunner:rename` documents the order:
`plan <old> <new>`, `apply <old> <new>`, `unit <new> <unit>` for each
non-retired row, then `finish <new>`. Old names keep resolving through a
`**Formerly:**` line in the production doc. Apply keeps the old merge branch
until finish; each unit keeps its old branch until its replacement has been
pushed.

## Files and readers

| File | Role |
| --- | --- |
| `scripts/production/rename.py` | Plans changes, applies them, moves units and retires old branches. |
| `scripts/production/rename_state.py` | Supplies `RenameRefused`, `_read_json` and the atomic `_write_json` / `_write_lines` writers. |
| `scripts/production/tidy_wrapped.py` | Supplies `SLUG`, the reserved `SHARED` names and `owned_paths(slug)`, the four owned state paths. |
| `scripts/production/add_unit.py` | Reads the production doc, Units table and cell values; `plan_cell_is_retired`. |
| `scripts/production/showrunners.py` | Reads timer CHECK paths and resolves Formerly names. |
| `scripts/production/unit_lookup.py` | Groups marked sessions by current slug and reports Claude state. |
| `scripts/production/stall_watch.py` | Supplies hashed state paths and matches inherited process marks by current slug. |
| `scripts/direct/review_pool.py` | `_production` resolves the production argument to its current pool. |
| `commands/showrunner/rename.md` | Showrunner workflow and recovery instructions. |
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
    base: str                      # merge-branch tip saved at plan time
    units: tuple[str, ...]         # every Units row, retired included
    plans: tuple[Path, ...]        # unit plans of non-retired rows
    timer: PathMove                # showrunner-<old> -> showrunner-<new>
    state: tuple[PathMove, ...]    # owned_paths(old) -> owned_paths(new)
    stall: tuple[PathMove, ...]    # one per unit
    review_pool: PathMove
    # properties: journal = state[-1].destination / "rename.json"; message = commit subject
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
detached work with old marks still counts as running. Review-pool arguments use
the same resolver.

## Production changes

`plan` writes nothing and prints numbered items in apply order; once a journal
exists it prints the recorded plan. New slugs match `[A-Za-z0-9][A-Za-z0-9_-]*`,
exclude `footers-off`, `review-paused` and `outstanding`, must be valid git
branch names, and cannot already be current or former timer names or have a
`showrunner-<new>` directory. The source needs a readable update timer whose
CHECK names `<old>-production.md`; the doc needs Production Context and a
Merge branch line; the doc must be tracked in git. Apply also
requires a clean showrunner checkout on the declared merge branch, free
destinations, no existing `<new>` branch locally or on origin, and no
unfinished prior rename journal for the old slug.

| Completed step | Change |
| --- | --- |
| 0 | Journal created; changes have not started. |
| 1 | Add or extend Formerly as the first bullet of Production Context, write the new doc, point CHECK at it, and `git mv` the doc. |
| 2 | Rewrite unit-plan Production headers (both header forms) and their production-doc references; add a header where a plan has none. |
| 3 | Rename `showrunner-<old>` to `showrunner-<new>` and keep the updated CHECK. |
| 4 | Move `footers-off/<slug>`, `review-paused/<slug>.json`, `outstanding/<slug>.json`, then each entry of `<slug>/` into the new directory; rewrite matching `*/parent` contents. |
| 5 | Move each Units row's stall-watch file from `sha256(old\0unit).json` to `sha256(new\0unit).json`. |
| 6 | Move the production's review-pool directory, including leases. |
| 7 | Create and switch to the new merge branch at the saved tip, update Merge branch, commit documents and push with upstream. |

The journal sits in `<new>/` from step 0, so step 4 merges the old directory's
entries into it: a prior journal goes to `rename-history/<old>.json`, the old
`rename.lock` is dropped, and the empty old directory is removed. Saved logs
and backups move without content edits. Retired Units rows (Plan cell starting
`(retired`) still contribute stall-watch moves, but never unit-plan paths;
other Plan cells select existing, tracked `docs/plans/*.md` paths quoted in
backticks or given as a single bare path, ignoring as-built paths and prose.
A plan path that is missing or not tracked in git is skipped, not refused.

During apply, a merge branch that moved from the saved tip, a checkout switched
to another branch, a CHECK that no longer names this production, or changes
outside the doc and plans refuse. The document and plan changes form one
commit on the new merge branch: `chore(<new>): rename production from <old>`.
The old merge branch stays at its saved tip locally and on origin. Apply leaves
unit branches, worktrees and their Units-row cells for the separate unit
operation.

## Unit changes

A unit must have one non-retired Units row with Worktree and Branch columns,
a branch starting `<old>-`, a linked worktree on that branch, no existing
`<new>-<stem>` branch locally or on origin, a free destination, and a clean
showrunner checkout on the new merge branch. An unfinished move must be
completed before another unit starts, and a finished rename refuses new moves.

Herdr observation failures refuse the move. The old local branch, while it
exists, must stay at the recorded tip before each later change.

| Unit step | Completed work |
| --- | --- |
| 0 | Saved old and new branch names, worktree paths and tips. |
| 1 | Created `<new>-<stem>` at the old tip and pushed it. |
| 2 | Ran `git worktree move`, `git worktree repair` and switched the moved worktree to its new branch. |
| 3 | Moved account project directories and copied trust entries. |
| 4 | Committed the Units row's Worktree and Branch cells and pushed the merge branch. |
| 5 | Verified the new remote tip and deleted the old remote and local unit branch. |

The destination is a sibling named `<repository>-<new>-<stem>`. The repository
prefix comes from the conventional old basename, or the showrunner checkout's
name when that suffix is absent. The unit commit is
`chore(<new>): rename unit <unit> from <old>`. Resume checks the Units row at
HEAD for the new values, so later merge-branch commits do not cause duplicate
unit commits. Unrelated checkout changes still refuse.

Every `~/.claude*/` directory and `CLAUDE_CONFIG_DIR` is checked for project
data. Project directory names replace each character outside `[A-Za-z0-9]`
in the full worktree path with `-`. Trust lives at `~/.claude.json` for the
shared seed and MCP baseline, and `<config-dir>/.claude.json` for numbered accounts.
The old path's `projects` entry is copied to the new key, keeping the old
key and unrelated settings. Each atomic write is read back after two seconds,
with at most three attempts if the new key disappears. Persistent loss refuses
and names the file; the next launch needs a trust prompt.

## Journal and recovery

`SHOWRUNNER_STATE_DIR/<new>/rename.json` stores `version: 1`, the recorded
plan, the completed apply `step` and `done`. Apply reaches step 7 with
`done: false`. `unit_moves[<unit>]` stores each unit's paths, branches, tips
and completed `step`. All modifying verbs hold the same `<new>/rename.lock`
through `flock`; `plan` takes no lock. Individual moves and writes are atomic;
the whole rename resumes rather than rolling back.

Rerun the same command after a refusal or interruption, once its cause is
resolved. Apply's destination stays the selected new slug; its source accepts
the recorded old name, the new name when the journal exists, or a former name.
`unit` and `finish` pass their production argument through `canonical_slug`,
so once the timer has moved they also accept the old or a former name. Leave
`rename.lock` to the script. `rename-history/<source-slug>.json` files are
archived completed journals and do not replace the current journal.

Finish requires apply step 7, no unfinished unit moves, no non-retired row
with an `<old>-` branch, and the showrunner checkout on the new merge branch.
It deletes the journal's recorded `merge_branch`, which may differ from the
old slug. `finish_step: 1` records remote deletion; `finish_step: 2` and
`done: true` record local deletion and completion. Missing already-deleted
branches permit recovery; repeating a completed finish is a no-op. A changed
local tip, or an origin tip that is not an ancestor of the saved tip, refuses
deletion.

The CLI exits 0 on success, 1 with `rename: <reason>` on a handled refusal,
and 2 for invalid arguments.

## Invariants

- **History is never rewritten.** Existing commit subjects, log lines and
  transcript contents keep the old slug. No step force-pushes or resets;
  branch deletion happens only after the saved tip is confirmed.
- **Every write is atomic.** Files go through `rename_state`'s temp file plus
  `os.replace`; directories move with one `rename`, so source and destination
  share a filesystem; worktrees move with `git worktree move`.
- **A unit move refuses while Claude is live or its state is unknown** in that
  worktree, and while any session host cannot be read.
- **Old slugs resolve forever** through the Formerly line, which is added
  inside Production Context (step 1) before the timer directory moves (step 3).
- **`canonical_slug` never raises** and returns an unknown slug unchanged.
- **CHECK names a readable doc at every point**: the new doc is written and
  CHECK points at it before `git mv` removes the old path.

## Calibration and gotchas

- Claude's projects-dir name replaces every non-alphanumeric character of the
  worktree path with `-`, not only `/`.
- The shared seed and trust baseline is `~/.claude.json` in `HOME`; each
  numbered account has its own `.claude.json`. Relaunches use the configured
  group account with an explicit numbered `CLAUDE_CONFIG_DIR`.
- CHECK is rewritten by shell-parsed token (the argument after
  `production_check.sh`) and re-quoted with `shlex.quote`, since it can hold a
  shell-escaped path.
- A retry after a `git mv` that moved the file but not the index removes the
  old doc from the index explicitly.
- The old unit branch tip is re-checked before each destructive step.
- `_lookup_slug`'s current-name fast path keeps the shutdown inventory within
  its timer-read budget: membership and missing-target checks share one read.
- A `**Formerly:**` line outside `## Production Context` is ignored.
- `test_unit_lookup` and `test_review_pool` use productions with no update
  timer; a strict lookup at those callers breaks them.
- Every path honors the state-dir environment variables; never run the verbs
  against live state when testing.
- Rename moves only the per-slug state listed above. These stay under the old
  slug: `outstanding/<slug>.history.jsonl`, the `review-health-<slug>` and
  `bevy-upgrade-<slug>` notifier instances, and the
  `BEVY_UPGRADE_STATE_DIR/<slug>.json` record.

## Why

- **Unknown slugs pass through.** A resolver that raised on an unknown slug
  would break the review pool and unit lookup for timer-less productions.

- **Units move separately, between runs.** A moved cwd breaks a live session,
  and the rename must not modify another session's claims. That is also why
  discovery is strict: one failing host could hide a live unit.

- **The old merge branch stays until finish.** Apply leaves units on their
  old branches; finish deletes the old merge branch only once every
  non-retired row has moved, and only at the saved tip, so no commit is lost.

- **Merge history survives the rename.** `merge_checkpoint.py` derives the
  last merge per unit from subjects `Merge <unit> phase <N> (<hash>)`, which
  hold no branch name, so old subjects need no rewrite.

- **The trust copy is read back.** A running Claude can rewrite its account
  file from memory and drop the new key.

- **Commit detection reads HEAD.** Merges onto the merge branch between unit
  steps must not block a resume or cause a second unit commit.

## State roots and tests

`test_rename.py` covers moves, kept history, interrupted steps, branch
changes, account data, trust retries, refusals and repeated completion.
`test_showrunners.py`, `test_unit_lookup.py` and `test_stall_watch.py` cover
Formerly resolution, old session marks, strict discovery and detached work.

```sh
python3 -m unittest scripts.production.test_rename scripts.production.test_unit_lookup scripts.production.test_showrunners scripts.production.test_stall_watch
```

`unit_lookup.py` is shared: the shutdown inventory
(`scripts/shutdown/test_inventory.py`) and message roster
(`scripts/message/test_roster.py`) suites also exercise it, so a change there
runs the full merge-gate suites:

```sh
python3 scripts/production/merge_gate.py --repo <checkout> --tests-only
```

## Live check

A worktree path containing `.` and `_` moves; transcripts move and trust
entries are copied in both the default and the separate account layouts. Plan
writes nothing; finish retires the old local and origin branches and marks the
journal done. Historical commit subjects, saved logs and transcript contents
are kept. The scratch root holds every path the check creates and is removed
afterwards.

## Herdr liveness

Unit liveness uses `marked_units(new)` before each move. Observation failures,
duplicate marks and live or unknown Claudes refuse. Existing herdr marks keep
their slug until relaunch and resolve through aliases. Tests use herdr and
process fixtures under isolated account and state roots.
