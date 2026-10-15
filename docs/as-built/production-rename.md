# Production rename

## What it is

`scripts/production/rename.py` changes a registered production's slug, then
moves each unit between its runs. `/showrunner:rename` documents the order:
`plan <old> <new>`, `apply <old> <new>`, `unit <new> <unit>` for each active row,
then `finish <new>`. Apply preserves the old merge branch until finish; each
unit preserves its old branch until its replacement has been pushed.

## Files and readers

| File | Role |
| --- | --- |
| `scripts/production/rename.py` | Plans changes, applies them, moves units and retires old branches. |
| `scripts/production/rename_state.py` | Supplies `RenameRefused` and atomic JSON and text writers. |
| `scripts/production/tidy_wrapped.py` | Supplies slug validation, reserved names and the four owned state paths. |
| `scripts/production/showrunners.py` | Reads timer CHECK paths and resolves Formerly names. |
| `scripts/production/unit_lookup.py` | Groups marked sessions by current slug and reports Claude state. |
| `scripts/production/stall_watch.py` | Supplies hashed state paths and matches inherited process marks by current slug. |
| `scripts/delegate/review_pool.py` | Resolves the production argument to its current pool. |
| `commands/showrunner/rename.md` | Showrunner workflow and recovery instructions. |
| `docs/production_format.md` | Production Context's Formerly field. |

`RenamePlan` records the source and destination slugs, document and checkout
paths, declared merge branch, saved merge tip, unit names, unit-plan paths and
state moves. `PathMove` holds a source and destination path.

## Names

The production doc's filename remains the current slug:
`<slug>-production.md`. Inside `## Production Context`, a single line records
previous names:

```markdown
- **Formerly:** `old`, `older`
```

`canonical_slug(slug, state_dir=None)` first checks all registered timer slugs,
then reads their checked docs for this line inside Production Context. A current
slug wins over another production's former name. An unknown name passes through;
the caller retains its own existence checks. Unreadable timers or docs do not
claim aliases. `production_slug(doc)` still reads only the filename.

Showrunner name, address, named-runner and tree operations accept former names.
Unit lookup normalizes both queries and discovered session marks, retaining
duplicate detection. A slug with an existing `showrunner-<slug>` notifier
directory is current and needs no timer-doc read; any other slug resolves
through `canonical_slug` once per distinct discovered slug. This keeps the
shutdown inventory's membership and missing-target checks on one shared
timer read.
Stall-watch similarly resolves each distinct inherited process slug once per
call, so detached work with old marks still counts as running. Review-pool
arguments use the same resolver.

## Production changes

`plan` reads without writing and prints numbered items in apply order. New
slugs match `[A-Za-z0-9][A-Za-z0-9_-]*`, exclude `footers-off`, `review-paused`
and `outstanding`, and cannot already be current or former timer names.
Apply also requires a clean showrunner checkout on the declared merge branch,
free destinations and no existing replacement branch locally or on origin.

| Completed step | Change |
| --- | --- |
| 0 | Journal created; changes have not started. |
| 1 | Add or extend Formerly, prepare the new doc and CHECK path, and `git mv` the doc. |
| 2 | Rewrite unit-plan Production headers and their production-doc references. |
| 3 | Rename `showrunner-<old>` to `showrunner-<new>` and retain the updated CHECK. |
| 4 | Move `footers-off/<slug>`, `review-paused/<slug>.json`, `outstanding/<slug>.json` and `<slug>/`; rewrite matching `*/parent` contents. |
| 5 | Move each Units row's stall-watch file from `sha256(old\0unit).json` to `sha256(new\0unit).json`. |
| 6 | Move the production's review-pool directory, including leases. |
| 7 | Create and switch to the new merge branch at the saved tip, update Merge branch, commit documents and push. |

CHECK always names a readable doc during the doc and timer moves; Formerly is
present before the timer changes name. Retired Units rows still contribute
stall-watch moves, but never unit-plan paths. Saved logs and backups move with
the production state directory without content edits.

The document and plan changes form one commit on the new merge branch:
`chore(<new>): rename production from <old>`. The old merge branch stays at its
saved tip locally, and its existing origin branch is retained. Apply leaves
unit branches, worktrees and their Units-row cells for the separate unit operation.

## Unit changes

A unit must have one non-retired Units row with Worktree and Branch columns,
a linked worktree on its old-prefixed branch, free destinations, and a clean
showrunner checkout on the new merge branch. An unfinished move must be
completed before another unit starts.

Liveness comes from `marked_units(slug, every_host=True)`. Both session sources
must be readable; failure to discover sessions refuses. `LiveClaude` and
`ClaudeUnknown` refuse; `ClaudeNotRunning` proceeds. Other callers keep tolerant
discovery by default. The old local branch, while it exists, must remain at
the recorded tip before subsequent changes.

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
default `~/.claude` account, and `<config-dir>/.claude.json` for other accounts.
The old path's `projects` entry is copied to the new key, retaining the old
key and unrelated settings. Atomic writes are read back after two seconds,
with at most three attempts if the new key disappears. Persistent loss refuses
and names the file; the next launch needs a trust prompt.

Session names and tmux marks stay as they are during the move. `add_unit`
writes the new production mark at the next launch. The showrunner tells each
unit director its new worktree path before it resumes.

## Journal and recovery

`SHOWRUNNER_STATE_DIR/<new>/rename.json` stores version 1, the recorded plan,
completed apply `step` and `done`. Apply reaches step 7 with `done: false`.
`unit_moves[<unit>]` stores each unit's paths, branches, tips and completed
step. All modifying verbs coordinate through the same `rename.lock` using
`flock`; JSON and text replacements use the shared atomic writers. Individual
moves and writes are atomic; the whole rename resumes rather than rolling back.

Rerun the same command after a refusal or interruption, once its cause is
resolved. Apply's destination stays the selected new slug; its source accepts
the recorded old name, the new name when the journal exists, or a former name.
After timer registration, unit and finish also resolve former production names.
Leave `rename.lock` to the script. A later rename archives a completed prior
journal under `rename-history/<source-slug>.json`; these files are archival
state and do not replace the current journal.

Finish requires apply step 7, no unfinished unit moves, no non-retired row
with an `<old>-` branch, and the showrunner checkout on the new merge branch.
It deletes the journal's recorded `merge_branch`, which may differ from the
old slug. `finish_step: 1` records remote deletion; `finish_step: 2` and
`done: true` record local deletion and completion. Missing already-deleted
branches permit recovery; repeating a completed finish is a no-op. Changed
branch tips or remote history outside the preserved tip refuse deletion.

The CLI exits 0 on success, 1 with `rename: <reason>` on a handled refusal,
and 2 for invalid arguments. Existing commit subjects, log lines and transcript
contents retain their historical names. No rename force-pushes or rewrites
history.

## State roots and tests

Tests set `HOME`, `CLAUDE_CONFIG_DIR`, `NOTIFIER_STATE_DIR`,
`SHOWRUNNER_STATE_DIR`, `STALL_WATCH_STATE_DIR` and `REVIEW_POOL_STATE` to
temporary directories, with a local bare origin and disposable linked worktrees.
The default state roots are `~/.local/state/{notifier,showrunner,stall-watch,review-pool}`.

`test_rename.py` covers moves, retained history, interrupted steps, branch
changes, account data, trust retries, refusals and repeated completion.
`test_showrunners.py`, `test_unit_lookup.py` and `test_stall_watch.py` cover
Formerly resolution, old session marks, strict discovery and detached work.

```sh
python3 -m unittest scripts.production.test_rename scripts.production.test_unit_lookup scripts.production.test_showrunners scripts.production.test_stall_watch
```

`unit_lookup.py` is shared: the shutdown inventory and message roster suites
also exercise it, so a change there runs the full merge-gate suites:

```sh
python3 scripts/production/merge_gate.py --repo <checkout> --tests-only
```

## Live check

A scratch production completed `plan`, `apply`, `unit` and `finish` with a
local bare origin, isolated account/state roots and fake tmux/herdr discovery.
A socket-backed showrunner fixture and an old-marked stopped unit remained
discoverable under both `old` and `new`: `address`, `list` and `pane` returned
identical non-empty answers after apply, before and after the unit move, and
after finish. Canonicalized stall-watch paths and review-pool lease paths
resolved to the same retained files under both names at every checkpoint.

The worktree path contained `.` and `_`; transcripts moved and trust entries
were copied in both default and separate account layouts. Plan wrote nothing;
finish retired the old local/origin branches and marked the journal done.
Historical commit subjects, saved logs and transcript contents were preserved.
The scratch script, repositories, sockets and all state were removed afterwards.
