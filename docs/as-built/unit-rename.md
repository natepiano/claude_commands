# Unit rename (as built)

`/showrunner:rename_unit <old> <new>` (`commands/showrunner/rename_unit.md`) renames one natedev unit
of a production. `/unit:rename` forwards to it. The script is `scripts/production/rename_unit.py`,
with three actions: `plan`, `apply` and `verify`.

## Names

- The unit id (`<stem>-unit`) is the key. An `<old>` that is exactly a Units cell is kept as given, so a unit without the suffix is found; any other `<old>`, and every `<new>`, take `-unit` (`rename_unit._ids`). A stem already starting with `<slug>-` keeps that prefix
  once in branch, worktree and host session names. When the new stem equals the old branch or
  worktree folder name, that branch or worktree stays: `names-unit` → `cargo-liner-style-names-unit`
  keeps branch and folder `cargo-liner-style-names`.
- Branches and worktree folders made by the shared naming rule take the new stem through that
  rule again. `cargo-liner-style-names-unit` → `cargo-liner-style-words-names-unit` gives branch
  and folder `cargo-liner-style-words-names`. Other names keep the existing suffix replacement.
- The Claude session always takes the new stem, and verify fails while the live session or a herdr pane label still has the old session name (user, 2026-10-19). `<old>` may be the session name; when the id already matches, only stop, record, relaunch and verify run.
- Rule: store only what cannot be looked up, and never rewrite history.
  - Merge subjects, logs, transcripts and plan prose keep the old id.
  - `merge_checkpoint.merge_branch_history` follows the commit `chore(<slug>): rename unit <old> to
    <new>`, so merges made before the rename still count under the new id.
- Work Orders read the worktree and branch from git, so the rename does not edit plan prose.

## apply

apply works under the production lock in `unit_move`, which `rename.py` also takes. It saves the
journal `rename_unit.json` before changing anything, and checks the showrunner checkout before
each step. The journal omits the production doc and checkout paths: resume and verify resolve
them through the current update timer, ignoring those fields in older journals. The unit's old
and new worktree paths remain recorded because they describe the rename itself.
The preflight, each step's checks, and every step except stop, relaunch and verify
(`Step.locked`) run under the merge lock, `merge_checkpoint.merge_lock(checkout, slug)`: an flock
on `<git common dir>/showrunner-merge-<slug>.lock`, which `merge_checkpoint.py` holds for a
whole gate run, unit checkpoints and `--from-child` hand-offs alike. A rename that finds a gate
running prints `merge lock: waiting — …` and waits, and a gate waits for the step. The steps that
wait do not hold the lock, so no gate waits behind a stop. Every step except the first and the last also checks that the unit is stopped;
relaunch alone accepts the journal's session already live under the new id. The
steps:

1. **Stop.** A running unit is not a refusal: plan lists this step, and apply stops the unit
   through `shutdown.py down --only <session>`. The unit's id is `SHOWRUNNER_UNIT_ID` in the
   environment of its Claude process in its herdr pane, set at launch, and a live process cannot
   change it, so every rename is a relaunch. apply then waits for the stop (`_await_stopped`):
   it reads the unit's shutdown record every `POLL_SECONDS` (1 s) and prints `stop: waiting —
   <unit> is <progress>` on each change. It goes on when the record reads stopped, and refuses when
   the record asks for a restart, reads something else, or was never written (`RECORD_WAIT_SECONDS`,
   120 s). It exits 2 only past `STOP_WAIT_SECONDS` (`RENAME_UNIT_STOP_WAIT`, default 3600), and a
   rerun resumes there, so a rename never waits for a person (user: "it is unacceptable that it
   require my intervention"). Once the unit is stopped (or was not running),
   `_stop_runs` ends what its runs leave in the worktree. First each run's live app-server (found
   by the run's `mesh_server.json`) stops with its workers through `codex_mesh.py stop`, as
   retire_unit does; a failed stop is printed and SIGTERM ends the server. Then SIGTERM goes to a
   detached run's supervisor (`mesh_runs.py supervise`, found by its run record) with every process
   of its session, an app-server's idle-stop watcher (found by the run folder in its arguments), and
   an app-server still alive with its children, such as `codex-code-mode-host`. Each leads its own
   session, so the shutdown path does not end it (2026-10-20: a lint run and a day-old watcher
   refused the move; 2026-10-21: modules-unit's 8-hour app-server refused it).
   `retire_unit.run_processes` finds them and `retire_unit.terminate` stops them.
   A dead server's idle-stop shell can still be asleep with its cwd in the worktree.
   The shared classifier checks the shell's `sh -c` arguments, the idle-stop script and the
   resolved `codex_mesh.py` entry point. When `kill -0` reports its server pid gone, it admits
   the watcher and its session children even if the run folder is missing or its history names
   another worktree. SIGTERM goes only to the classified pids. A live server's watcher still
   requires a run folder from `runs_of(worktree)`; unrelated processes still refuse the move.
   Preflight uses the same classification so apply can stop these leftovers. One alive after
   `TERM_WAIT_SECONDS` refuses, and a rerun resumes. Refused up front: the new id
   already running, a Claude that cannot be read, and other processes inside the worktree of a unit
   with no Claude to stop.
2. **Merge.** `origin/<merge branch>` is merged into the unit in the old worktree. While the unit's
   work is in progress (`_in_progress`: an open phase in its run record, or `git status --porcelain`
   output), nothing is merged: a failed merge's `--abort` can lose uncommitted work. The journal's
   `kept` holds the reason, `merged` is the old tip, and step 6 takes that tip as `unit_tip`. The
   unit takes both merges at its own next merge of the merge branch (user, 2026-10-18: a rename
   works at any time, mid-phase too; the plan's refusals of an open phase and of a checkpoint not
   merged are gone).
3. **Git.** Changed branches are pushed and changed worktrees moved, repaired and switched to the
   destination branch. Kept paths take no move.
4. **Claude.** For a changed worktree, transcript folders move and the trust entry is copied in
   every config dir.
5. **Doc commit.** The commit edits:
   - the Unit, Worktree and Branch cells;
   - the Hub Owner and Other cells;
   - the Waiting and Waits-on cells of open Gates;
   - the `> **Production:**` header of the unit's live plans.

   A `Kind: style` production reads the Plan cell under `docs/style-production/units/`, keeps the
   filename, and updates the header in both the showrunner checkout and unit worktree. Its doc
   and plans stay git-excluded; `--allow-empty` records the rename subject even without a tracked
   change. If a stop follows a doc or plan write, the pending step accepts only
   files exactly equal to its transformations of HEAD; unrelated contents, paths
   or mode changes still refuse. It finishes the remaining writes and commit.
   The journal records the commit's hash, so a retry only pushes it.
6. **Merge rename.** The unit branch takes the rename commit.
7. **State.** These move to the new id:
   - the six showrunner state files (`rename_state.SCRATCH_FILES`) in `showrunners.session_state_dir(<slug>)`, or in the `--scratch` directory when one is given;
   - the free text of the live lists in `showrunner_state.json`, `next` and `open_for_user` (text and question),
     and the same items in OUTSTANDING (`outstanding/<slug>.json`): the old id, branch and worktree each take
     the new one as a whole token, so `per-unit` stays. OUTSTANDING then holds the same texts, so the next
     `questions` run finds each item stored and asks no new review (sdlc, 2026-10-21);
   - the same live-list text and OUTSTANDING items of every other production's showrunner, such as
     the parent (`showrunners.SHOWRUNNER_STATE_DIR/<slug>/session/showrunner_state.json`); their unit
     rows stay. A production whose Units table has its own unit of the old id, or whose doc cannot
     be read, keeps its text, and verify counts its file as history (cargo-liner-style, 2026-10-21);
   - the stall-watch stretch;
   - the reviewer leases and memory;
   - the newest run record and the recorder state;
   - this production's open proposal issues and phase notes (`phase_table.owned_notes`).
   - the herdr pane record drops the old unit's entries (`pane_record.forget_unit`), in each session's file. The relaunched unit records its new pane at SessionStart; an old entry would resume the old name.
   - each held design role of the unit (`design_roles.rename_holder`, `docs/as-built/long-lived-workers.md#lasting-roles`): with the role idle, the ownership authority moves the checkout and advances the generation, and the holder, checkout, document and binding paths, `design_context.json` and the roster `cwd` take the new names. A failure raises `RenameRefused`, and the journal retries the step.
   Style productions also call `Ledger.rename_unit` with their Repository: queued, assigned and
   checkpointed tickets and sync waits take the new unit id; ticket ids and closed rows stay. It
   refuses a target that owns current rows or maps to another family, and a repeat moves nothing.
   `plan` runs the same move on a `preview_ledger` copy, so a refusal comes before any change.
   Their open proposal issues move when the issue's `unit` is the old id and its `proposed_by`
   field belongs to this production's owners; no plan field match is needed. This accepts the production slug or
   `unit (production)` form and leaves another production's issues in a shared folder untouched.
8. **Retire.** A changed branch is deleted once origin holds the new tip.
9. **Restart record.** The fields that move:
   - `host.unit`;
   - the cwd, plan path and worker cwds under the worktree;
   - the Claude and herdr names, while they are still the defaults;
   - `renamed`, which `restart.restart_note` puts first in the resumed prompt. With `kept` set it
     also says the branch took no merge and the paused Codex workers now work in the new worktree.
     `implement.sh` passes `--cwd <working dir>` on every follow-up, so a resumed worker's turns run
     there and not in the path its thread started in.

   `restart._unit_argv` still reads the plan from the Units row.
   Old and new default herdr names use the shared prefix rule, so `names-unit` →
   `cargo-liner-style-names-unit` keeps herdr session `cargo-liner-style-names`.
10. **Relaunch.** `shutdown.py restart --only <session> --here` brings the unit back. apply then
    waits for the new id to carry a live Claude mark (`_await_live`, `LIVE_WAIT_SECONDS`,
    `RENAME_UNIT_LIVE_WAIT`, default 180), so verify does not run before the unit is up. The rename
    claim (`rename_state.rename_claim`) refuses add_unit for both ids until step 9 is done.
    If the restart succeeded before this step was saved, apply accepts only that
    session id under the new unit id and retries restart's own recovery before
    verification; it does not stop the restored workers. Another live session
    or an unreadable session mark still refuses.
11. **Verify** (below).

A unit that was not running has no record, so steps 9 and 10 do nothing for it.

## verify

- **Live places.** These must hold the new names:
  - the Units row;
  - the doc cells and plan headers (apply's own edit would change nothing);
  - the branches and worktree;
  - the transcript folders, the stall stretch and the newest run;
  - proposals and phase notes;
  - the shutdown record and the live session's mark;
  - held design roles (`design_roles.rename_failures`): no old holder, no old checkout in a live field or roster `cwd`, no stale `design_context.json`.
- **Control.** The old id must still be in the merge-branch subjects. If the matcher finds nothing
  in history, it is broken.
- **Sweep.** `unit_rename_sweep.sweep` runs `rg -F` for the old id, branch and worktree path, then
  checks each hit as a whole token. It covers:
  - the state root;
  - every `~/.claude*` config dir;
  - the showrunner checkout and the new worktree, as git sees them;
  - the six showrunner state files.

  Kept branch and worktree names are excluded from the search. The style ledger folder
  (`style/` in `unit_rename_sweep.STATE_HISTORY`) is history, because closed tickets keep the old
  id. Live places read the ledger through `preview_ledger` and fail on a queued, assigned or
  checkpointed ticket or a sync wait that still names the old id. The postmaster's replies are history.
  So are the pause hook's scheduled prompts (`conversation-pause/scheduled-prompts/`): they are copies
  of queued prompt texts, such as the relaunch prompt with its rename note, and the hook matches each
  incoming prompt to them exactly, so a rewrite would break the match. In the held role store
  (`astra/design-roles/`), `prompts/`, `replies/`, `adoption.json` and `transfer.json` are history, and role
  `name` and `aliases` in `design.json` and the entry names and `aliases` in a role mesh roster are identity, not live names. A hit outside the history classes fails and names its file. Shutdown paths print as `(masked)`.
  Three owner-maintained records also keep their original names: `herdr-panes/` contains launch
  snapshots (apply drops the old director's entries; SessionStart rewrites other panes, and
  missing panes are ignored); `lint-sweep/target-deletes.json` records deletion timestamps whose
  absent roots and entries older than a day expire; `mac-idle/calls/` contains verify registrations
  admitted only by a current waiting step and matching live pid/start, with the repository read
  from that step. The sweep leaves these files unchanged. Explicitly live files still override
  these classifications.
  On natedev a sweep takes about 80 s.

## Tests

`test_rename_unit.py` covers Git moves, planning and refusals. The neighboring
`test_rename_unit_{style,sessions,waits,state,processes}.py` modules cover style
identities, session records and verification, live coordination and locks, moved
state and journal retries, and stopped-run process ownership respectively.
`rename_unit_test_support.py` builds one Git fixture per class and restores a
private copy at the same absolute path for each test. Synchronous cases use the
real CLI parser and handlers in-process, including the fake herdr response CLI;
Git commands remain real. Entry-point errors, cross-process locks, shutdown and
relaunch coordination retain subprocesses. Fixture file waits use directory
events with a 300-second hang guard.
The process suite applies a rename with a real dead-server watcher blocked on a pipe; a readiness
event establishes that it holds the old cwd before the server exits, and its run history names
another worktree. Classification tests leave live servers' watchers alone when their run history
does not match. `test_unit_rename_sweep.py` checks the three snapshot/history classes and verifies
that live-file overrides still fail.
