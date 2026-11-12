# Showrunner automation: unit start, checkpoint merge, lifecycle, questions and waits

Codex code reviews use the unit's persistent `review` and `review2` workers through `assign.sh --role review`.
The [daily simplify run](simplify-lens.md) uses a fresh Claude review.
Pool maintenance paths do not dispatch new reviews.

## What it is

The Python scripts in `scripts/production/` run production commands with the standard library.
They start units, merge checkpoints, promote results, record questions, and prepare reports.
Staff owns production and unit identity.
The commands read unit settings from `.unit.toml`.
Command documents define decisions that require judgment.

Each command performs repeatable steps and reports a named result.
The showrunner uses those results to decide the next action.

### Shared conventions

- **Production-doc reader** (`scripts/production/production_doc.py`): `read_production(name, record=None)` finds the showrunner staff row.
  The row's `name` is the production name. Its `doc.path` selects the document.
  `Production` holds `doc`, `name`, `merge_branch`, `checkout`, `log` and `zone`.
  It also retains the resolved staff row as `owner`.
  `ProductionDocument` holds an unbound document; `for_owner` produces a `Production`.
  The reader takes `Merge branch`, `Showrunner checkout` and `Log` from that document.
  `Log` must remain relative to the showrunner checkout.

Staff supplies unit membership, order, names, parents, and locations.
Git supplies the current branch.
`.unit.toml` supplies the plan, port, and responsibilities.
Responsibilities name duties, such as nightly review; they do not restrict changed paths.
Production documents contain no Units table.
A plan has no production-name header; its unit's staff parent selects the production.
`unit_lookup.doc_owner` resolves a document owner, and `StaffSnapshot.parent_of` resolves a parent.
Callers share one staff snapshot within an operation.

- **Retired units**: `agents retire NAME` records a completed retirement as a `RemovedWorktree`.
  The row stays in `staff.json` with its fixed identity and parent.
  Status, the board, status reports, waits, and the stall watch exclude retired units.
  `unit_lookup.doc_units(doc, record)` returns every recorded unit in staff order.
  `merge_checkpoint.py`, `ci_points.py`, and `production_lifecycle.py` retain access to retired rows.
  `agents resume NAME` refuses a retired unit and names `--retired`.
  `agents resume NAME --retired` restores the worktree, director, and retained workers.
  The showrunner asks the user yes or no before it runs `--retired`.

`agents retire NAME` finds the unit and its workers in one staff snapshot.
It stops each row's `current_session` through `stop_resume.stop`.
Uncommitted work and commits outside the merge branch prevent removal.
The recorder's only added `Project started` line does not count as uncommitted work.
Retire releases build holds, berth reservations, and Mac claims.
It reports other cleanup failures as anomalies.
It removes the worktree and branch.
The staff row stays with a `RemovedWorktree` location.
Unit settings remain parked under the staff id.
Retire writes no retired note or saved branch ref.
Resume restores the folder from the merge branch and resumes `current_session`.

- **Merged work.** Git ancestry proves whether a checkpoint reached the merge branch. Git merge bases supply the last shared unit commit. Unit checkpoint records supply CI counts. Unit `phase_started` records supply phase starts. Commit subjects supply no state.

- **Named states**: every optional or alternative value crossing a reader boundary is a `NamedTuple` union (`FieldPresent | FieldAbsent`, `PushedTip | NotYetPushed`, `EtaFresh | EtaStale | EtaPassed | EtaNone`, and so on), never a string or `None`.

- **Session state.** `showrunners.session_state_dir(<staff id>)` is `~/.local/state/showrunner/<staff id>/session` (`STATE_DIR` in `produce.md`).
  It holds `showrunner_state.json`, `unit_status/` and `waiting_state/`.
  A session change, restart, reboot or production rename preserves the staff id folder.
  Wrapping a production removes that folder.
  `showrunners.copy_session_state` copies missing scratchpad files through atomic writes and preserves existing files.
  `update_registration.adopt_moved_state` selects the timer owner's current session through staff.
  It copies missing state from `/tmp/claude-*/*/<session id>/scratchpad` and writes no prompt file.
  Registration and the log and question commands call it before their work.
  Scheduled prompt delivery reads the current owner and document schedule without adopting state.

### Unit start: `add_unit.py` and `/showrunner:add_unit`

`add_unit.py` runs through `scripts/lib/py`.
It takes `--production <showrunner name>` and the unit name.
Its mode is `--plan <path>`, `--brief <words>`, or `--standby`.
Other options include `--permanent`, `--account`, `--model`, `--port`, `--resume`, `--cwd`, `--timeout`, and `--check`.

- **Request.** `launch_request` keeps the requested staff name. It creates no suffix or alias. `UnitLaunchConfiguration` carries the production, branch, worktree, plan, port, session, timeout, and responsibilities. A resumed session requires `--cwd` and `--plan`.
  Shutdown restart resumes staff sessions directly. It does not call `add_unit.py`.

- New work and restores launch without an account hold or admission claim.

- **Preflight.** `preflight(request: RequestedUnitLaunch, selected: UnitLaunchAccount) -> ReadyToLaunch` checks the checkout, staff worktree, branch, plan, and mode.
  The checkout must use the merge branch. Another unit cannot hold the requested worktree.
  An existing unit uses its staff worktree and `.unit.toml`.
  A new launch requires the requested worktree and branch to match the existing unit.
  A rerun must use the recorded mode. `recorded_mode` reads the commit subject, plan, then stub source.
  A missing stub means brief mode. The worktree path must be free or already use the requested branch.
  Existing local or origin branches require the corresponding row and worktree.
  A `--plan` file must exist on the merge branch. A plain resume accepts the existing row's recorded plan and transcript.
  A resume with a different plan refuses. An existing brief stub must contain the same words.
  Preflight returns `UnitLaunch(identity=UnitIdentity(unit, session), ...)` with the row and director.
  `session` is the requested launch name and is not recorded.
  A herdr session marked for the unit counts as launched.
  Another live session with the destination name refuses before any write.
  `--check` runs only `launch_request` and `preflight`.
  A resume refuses on `ClaudeUnknown`. It closes a stale herdr session on `ClaudeNotRunning` before launch.
  Preflight does not check Claude trust.
  The shared launch writes trust in the selected account before starting Claude.
  A failed trust write prints one anomaly and prevents the start.

- **Steps**, each skipped when already done: the brief stub beside the production doc at `<its directory>/<branch>.md` (`## Source`, a zone stamp and the user's words); the unit staff row and its `.unit.toml`; commit `production(<showrunner name>): add unit <unit> (<plan|brief|standby|permanent>)` on the merge branch for a brief stub or a brief-to-plan switch, which is not pushed here and goes out with the next merge push (user, 2026-10-08); `git worktree add` from the local merge branch, with no push of the unit branch (the unit's first checkpoint push sets its upstream), plus `branch.<branch>.cargoBerthTarget` when the repo has `.claude/config/berth.toml`; the herdr session (Launch below); the wait for `/remote-control is active` (default 90 s, last 15 pane lines on timeout); one LOG line `added <unit> (<mode>), worktree <path>`. It prints `<unit> started: herdr session attach <name>`. Shutdown restart resumes the staff session directly.
 New units select the configured units account through `accounts.account_for_group_launch("unit", ...)`. Resumes use the pane launch’s account when available, else the configured units account. `--source-fallback` must name the pane launch’s account. Resume model selection uses explicit `--model`, then the pane launch, then the last assistant model from the unit session (`transcript_model`); otherwise it uses the director model.

- **Launch.** `launch_session` starts the shared herdr server and opens the unit's tab in its production workspace.
  The tab title uses the unit name. The agent name uses the production and unit names.
  The environment uses the selected account and the session labels.
  New work uses the plan, brief, or standby prompt. A normal unit resume keeps its existing admission path.
  Shutdown restart bypasses this path and sends no work prompt.

- **Unit run status.** `unit_lookup.read_run_states(record=...)` joins each run owner to its current staff folder.
  `RunStates.state(worktree)` returns `standing-by`, `running`, or `run-finished` from the newest matching run.
  A missing record gives `standing-by`.
  The reader uses the first run event and last valid JSON event.
  It skips a partial first line and ignores a partial final line.
  Unreadable records retain their error for the caller.
  The stall watch uses the current director's held runs, as described under Calibration and gotchas.

- **Callers.** `produce.md` calls `add_unit.py --production <showrunner name> <staff name> --plan <unit plan>`. A waiting unit uses `--standby`. Promotion uses the same staff name with `--resume`, `--cwd`, and `--plan`. The showrunner first uses `--check`, then closes the old session and runs the launch.

### Shared runtime configuration

`scripts/lib/conf.py` owns Python reads of `config/*.conf`.
`conf.read(file) -> ConfigRead` reads and parses one file for a pass.
Callers pass that read to helpers instead of reading each setting again.
`ConfigRead` provides `text`, `number`, `whole`, `words`, `values`, and `origin(key, convert)`.
`UseConfiguredValue.CURRENT` selects a fresh read when a helper receives no operation read.
It lives beside `ConfigRead` in `conf.py`.
Helpers use `ConfigRead | UseConfiguredValue` with that named default.
Each new `conf.read` observes the file again.
Fault recovery occurs when a setting is used.

The reader ignores section headers and text after `#`.
The first key wins, and `key.<host>` overrides `key`.
The comment immediately above a key defines its whole-number range, numeric range, or allowed words.
An invalid setting warns on stderr and uses the newest usable committed value under that revision's limits.
No usable value raises `conf.Unusable` with the source, key, and reason.
`ConfigRead.origin` returns live, recovered, or unavailable evidence without printing a warning.
The [accounts configuration section](accounts.md#runtime-configuration-and-constraints) defines repository defaults and explicit-folder behavior.
`conf.sh` supports bash 3.2 and zsh.
Its `conf_value` uses shell builtins for valid integer and text settings and calls Python when validation needs it.

`/tune` publishes one user-approved setting through `scripts/lib/tune.py`.
It validates the requested value and refuses an uncommitted edit to that setting's file.
It preserves the line's comments and spacing.
The commit uses `tune(<file>): <key> <old> -> <new>` and a `User-Approved:` trailer.
A same-value retry finishes publication without another commit.
`scripts/lib/main_publish.py` supplies the shared publication path for `/tune` and `/zone`.
Its preparation and push each take a short [main push lock](repair-rounds.md#shared-main-push-lock).
It accepts unpublished subjects beginning `tune(`, `zone(`, or `user zone: `.
It refuses another unpublished commit before changing the setting.
It fetches main and rebases pending publisher commits when a fast-forward fails.
The rebase uses `--no-autostash --no-update-refs --empty=drop`.
A conflict names the commit and files, aborts the rebase, and preserves main.
After publication, the Mac pulls or receives a queued pull.

`showrunners.timer_scratch` uses the showrunner's fixed staff id to find its timer.
It returns `TimerConfigurationUnreadable`, `TimerTargetUnresolved`, `TimerScratchAbsent`, or `TimerScratchFound`.
`adopt_session_state` reports an unreadable timer and continues.

### Unit work queue

`/unit:queue add <text>`, `done`, `clear`, and `show` call `scripts/production/unit_queue.py`.
The list uses `UNIT_QUEUE_STATE_DIR/<staff id>.json`, default `~/.local/state/unit-queue/`.
`add` appends one normalized text item.
`done` removes the first item.
`clear` writes an empty list without reading a damaged old file.
Writes use the list lock and atomic publication.
The queue survives a unit rename because its key is the fixed staff id.

### Director model and effort: `config/models.conf`

- `[assignments]` has `production=claude`; `[production.claude]` has `director=opus:xhigh`. There is no `[production.codex]`: unit directors launch only on Claude, and the showrunner is whichever session runs `/showrunner:produce`. The header Consumers list names `production unit directors (add_unit.py)`.
- `director_agent(request) -> DirectorAgent(model, effort: DefaultEffort | Effort(value))` runs `bash -c 'source "$1" && agents_resolve production.director && printf …'` with the `models_config.sh` from the script's own checkout (`Path(__file__).resolve().parents[1] / "agents/models_config.sh"`); `MODELS_CONFIG_FILE` comes from the caller (default `~/.claude/config/models.conf`). A resolver failure is exit 2 with its first `ERROR:` line; a family other than `claude` is exit 2 with `unit directors launch only on claude; production.director resolves to <family> (<model>)`. Both happen in `preflight`, so `--check` refuses too. `--effort` is passed only for `Effort(value)`.
- **One-family pinning** (`scripts/agents/models_config.sh`): a function with exactly one family set is pinned to it (`_agents_pinned_family`). Every-function switches (`agents_set_all_assignments <family>`, `agents_set_model <agent>` with no function) leave it as it is and add it to `AGENT_KEPT_FUNCTIONS`; `default_models.sh` prints `# kept production on claude: its only set`. A switch aimed at it (`agents_set_assignment production codex`, `agents_set_model <codex agent> production`, `agents_set_row production.director <codex agent>`) prints `ERROR: 'production' runs only on claude: [production.claude] is its only set.`, returns 1 and leaves the file untouched. `/default_models production.director <model>:<effort>` changes the row.
- **Resume** in `<LaunchUnits/>` uses add_unit’s live session and pane launch checks. Its Claude arguments come from `models_config.sh`, `agents_resolve production.director` and `agents_claude_args`, then `--resume <session-id> --remote-control <session> -n <session>`. No shutdown hold is read.

### Phase end on the unit side

`docs/direct/phase_end.md` defines `<PhaseEnd/>`, the one order a loop or verbose phase ends in: (1) code commit (`<CheckpointCommit/>`, with the phase `status: done` and its Work Order still in place); (2) the code-ready notice, whose hash is mergeable now; (3) the plan review, where a defect in the committed phase becomes a follow-up phase run next; (4) the add-on check; (5) worker cleanup and `finish-phase`; (6) the next phase's launch; (7) the shrink beside the next phase's workers, its commit and the shrink notice. With no phase left, step 7 runs, then `<FinalGate/>`. A due `<PeriodicCI/>` point outside a production runs step 7 before step 6, so CI sees a clean tree. The other phase-end docs point to it in one line.

- `<ShrinkCommit/>` (`commands/unit/checkpoint.md`) stages only the plan doc and an approved next-items file, refuses any other staged path, commits `shrink(<plan-slug>): phase N — <title>`, runs `<PushCheckpoint/>` and owns no reservation.
- The shrink notice is `From <unit>: phase <N> shrink <hash> — plan doc only.` or `… — plan doc and <next-items path>.`
- A failed shrink blocks only `<ShrinkCommit/>`; the next `<CheckpointCommit/>` refuses while an earlier `done` phase still has a Work Order.
- **Closing work in the recorder** (`scripts/direct/progress_history.py`). `start-activity` succeeds whenever the run is active and a phase has been started in it, still active or already closed by `finish-phase`, so work after step 5 with no next phase started (the last phase's `shrink` activity) keeps its row. On a closed phase `activity_started` and `activity_finished` carry that phase's `phase_instance_id`, and the phase's status, `finished_at` and elapsed time stay as `finish-phase` left them; `finish-activity` records status, result and elapsed seconds as it does inside a phase. `start-activity` exits 1 with `Start a phase before starting an activity` when the run has no phase, and with `Cannot start an activity: the run is finished` after `finish-run`.
- **An activity left open.** `start-phase` and `finish-run` close an activity still open, inside a phase or on a closed one, as `interrupted`. A repeated `finish-phase` does nothing: `_finish_phase` returns before any cleanup when the phase in state is not active, so an activity opened after the phase closed keeps running.

### Checkpoint merge: `merge_checkpoint.py`

`merge_checkpoint.py --production <showrunner name> <unit> <phase> <hash> --started <ISO>` accepts these options:

- `--review-trial "<line>"` and `--delivers "<line>"`.
- `--shrink` or `--as-built`.
- `--trailer <line>` (repeatable), `--regime after|trial`, `--holds K`, and `--merge-defects D`.
- `--excluded "<why>"`, `--cancel-prior`, `--bump`, and `--scratch <dir>`.

`produce.md` `<MergeCheckpoint/>` runs the command in the background after the showrunner's judgments.
`--as-built` merges a unit's closing documentation commit without tests or a review-ledger row.
It requires an earlier code checkpoint merge for that phase.
It accepts only `.md` paths and requires a change to or deletion of the unit's plan.
Its subject is `Merge <unit> phase <N> as-built (<short>) into <branch>`.
`Promote: none` and an absent field both mean `NoPromotion`.
Each local Promote checkout must exist before the command pushes.

- **Origin ancestry:** The checkpoint must occur on an origin branch. The command fetches and checks the unit's Branch first. If necessary, it fetches all origin heads. Machine-readable fetch output identifies current origin refs. Stale tracking refs cannot certify a checkpoint. Neither ancestry checks nor worktree cleanup prune shared refs. This also applies when the checkout normally fetches one branch. The ancestry line names the containing branch. Without a containing branch, the result stays `commit is not on origin`. Scope, conflict checks, and merging use the submitted checkpoint hash. The overlap report checks other units' branches and dirty worktrees. It excludes the submitting unit. Push preparation, publication, batch preparation, and promotion fetch explicit refspecs before they read full origin ref names.
- **Inputs:** The showrunner staff row supplies the production name and document path. Unit staff rows supply the worktrees. `.unit.toml` supplies each plan. The document supplies `Merge branch`, `Showrunner checkout`, `Log`, and `Merge tests`. It also supplies `Known flakes` and `Known flaky tests`. Flakes name packages. Flaky tests use the form `` `<path::test>`, `<path::test>` ``. `Push` selects `validate_and_push` by default, or `git`. `Promote` accepts `<checkout>[, mac <path>]`. Its type is `NoPromotion | PromoteTo(checkouts, MacCheckout | NoMac)`.

- **Input rules.** A code checkpoint needs a `--review-trial` line and a one-line `--delivers`; a shrink refuses `--delivers`.
  [Review regime records](review-regime-trial.md) define the line.
  `--cancel-prior` is refused with `Push: git`.
  `MergeRequest.delivers` and `excluded` use `str`, with `""` for an absent value.
- **User bump.** `--bump` accepts only code checkpoints.
  It refuses `--shrink`, `--as-built`, and `--from-child` before merging.
  The normal path checks still apply.
  Tests wait until after the push, including on a retry between merge and push.
  `start_gate` starts `bump_gate.py --record <step record>` before promotion.
  The checkpoint does not wait for its result.
  `copies.standing` supplies a checkout of the recorded pushed tip.
  `checkpoint_gate.run_checkpoint_test_steps` supplies the shared test, flake, and retry rules.
  The production checkout retains flake notes, and `bump-gate.log` retains output.
  The record holds the test plan, tip, runner pid and start, result, detail, and successful recipients.
  A live runner prevents another launch; unknown liveness holds the gate.
  A dead runner without a result permits another launch.
  Completion and delivery compare the current tip, pid, and process start under `checkpoint_gate.record_lock`.
  A replaced runner cannot publish a result or deliver a notice.
  Red results reach the unit and showrunner; green results reach the showrunner.
  Delivery resolves current sessions from the recorded fixed owner and unit ids.
  Each successful recipient is saved, and a failed send gets one retry.
  `MERGE_CHECKPOINT_SEND` selects the sender for tests.

- **Publication locks.** `scripts/lib/main_push_lock.py` supplies the shared helper.
  The gate-record lock precedes `main_push_lock`.
  Nested calls in one process share the hold for the same repository.
  Only a child with an explicit `held_environment` shares that hold through `MAIN_PUSH_LOCK_HELD`.
  `push_validate` passes this environment to the validation shell.
  Detached gate children receive the plain environment and take their own locks.
  The checkpoint releases the gate-record lock before local promotion.
  It retains `main-push.lock` through the last local push to main.
  Mac pulls and result delivery occur after that lock is released.
  Publication records survive promotion failure.
  A style notice becomes complete only after successful delivery.
  A completed child handoff retry changes nothing and sends no second notice.
  Local and Mac promotion use the [shared edited-path check](merge-gate-and-test-speed.md#the-gate-scriptsproductionmerge_gatepy).
- **Review numbers.** The unit obtains `--review-trial` from `progress_history.py review-trial`.
  The checkpoint command passes `--phase-id <phase id>` for the phase it checkpoints.
  [Review regime records](review-regime-trial.md) define the counts, times, types, and checkpoint parser.
- **Held simplify items.** The checkpoint notice carries each held item and its ledger reference.
  The [simplify lens](simplify-lens.md#findings-and-decisions) defines which items require user questions and which items the director settles.
- **Merge tests** are the backticked spans before the first unquoted `(`. A missing field is refused. An empty field means no commands.
- **Steps:**
  1. *ancestry*: the checkpoint exists on an origin branch. Git ancestry identifies a checkpoint that already reached the merge branch. An already merged hash skips steps 2–7.
  2. *scope*: nothing staged in the merge checkout. Code paths have no responsibility filter.
     `_unit_paths` excludes unchanged paths that an origin merge base contains.
     A path absent from that base remains in scope, including a deleted plan.
     The report gives both path counts.
     A shrink allows only the plan doc and `<plan stem>-next.md`.
     An as-built commit allows only Markdown paths and must change or delete the plan.
  3. *conflicts*: `merge-tree --write-tree --name-only` exit 1 is held, asking the unit to merge the merge branch and send a new hash.
  4. *other units*: overlap of this change's paths with each other unit's branch diff and its worktree's `status --short`, printed as data, never a stop.
  5. *merge*: `<scratch>/merge_<short>.msg` (subject `Merge <unit> phase <N>[ shrink] (<short>) into <merge branch>`, body `--delivers` or `Phase plan shrink.`, then trailers), `merge --no-ff -q -F`.
  6. *test*: `prepare_checkpoint` merges `origin/main` before test selection.
     It selects every changed path from the tip before the checkpoint merge through the prepared tip.
     Packages run through `verify.sh test`, and changed examples run through `verify.sh example`.
     Then each configured Merge tests command runs without a shell.
     `parse_merge_tests` reads backticked entries before the first unquoted parenthesis and splits them with `shlex`.
     Each word expands home and environment variables.
     A remaining `$VAR` or an unavailable executable refuses the command by name.
     Output enters `<scratch>/merge_<short>_test.log` with `<name>_EXIT=<rc>` lines.
     A command that cannot start has exit 127.
     A shrink runs no tests.
  7. *red*: run each failed command once alone.
     If every nextest `FAIL` row names a Known flaky test, run only those tests again.
     Use `verify.sh test [<package>] --filter <name>` per test, or `-E test(=<name>)` for other nextest commands.
     A pass alone continues with a LOG note in this case, or when Known flakes lists the package.
     Otherwise, restore the tip before the checkpoint merge while origin still has its old tip.
     This removes the checkpoint merge and the main merges from this run, while preserving earlier unpublished work.
  8. *push and promote*: `tested_main_push` acquires `main_push_lock` and fetches main after tests.
     It publishes only when the tested tip contains main.
     Otherwise, it keeps the lock, merges main, retests the new paths, and tries again.
     The first test run is outside this lock. Retests run inside it, so main cannot move under a tested tip.
     `push_git` pushes without another merge.
     A main conflict aborts the merge and reports `push: held` with the conflicting paths.
     Preparation failure restores the tip before this run when origin still has its old tip.
     An interrupted promotion and a child handoff use the same test loop.
     Each caller retains its own rollback rule for a failed push.
     `ValidateAndPush` uses `validate_and_push.sh --quick --to <merge branch>` with the configured fix subject and optional `--cancel-prior`.
     Local promotion fast-forwards each main checkout to the pushed tip.
     It pushes main only when main holds no foreign commits outside origin main and the production merge branch.
     Mac promotion calls `pull_mac_checkout(mac, source)` with the first Promote checkout as source.
     The Mac pulls through [`main_publish.mac_pull_command`](repair-rounds.md#mac-pulls-and-visible-change-ownership).
     It reads the printed `rc=` line and uses `MAC_PULL_TRIES` and `MAC_PULL_RETRY_SECONDS` from `config/merge_checkpoint.conf`.
     An unreachable Mac queues its target and source through `main_publish` and permits the merge to finish.
     An answering Mac whose pull fails still fails promotion.
  9. Record the code checkpoint's review ledger row and production log line.
     `review_regime.py add` uses the configured plan, unit, phase, merge time, review numbers, and optional exclusion.
     The ledger key includes unit, plan, and phase.
     The LOG line identifies the checkpoint, merge, tests, push, and promotion.
     An existing matching log line skips the write.
- **Merge report.** Step 8 prints `into: <path> (<branch>) — <why>; unblocks: <what>` as it reaches each place: the merge-branch checkout (`the merge branch collects every unit's checkpoints`; unblocks `<gate id>: <Waiting cell>` for each Gates row whose **Waits on** is `<unit> phase <N>`, else `no gate`), then each Promote checkout and the Mac (`sessions on <machine> run the installed commands from it`; unblocks `the live gate of <unit> phase <N>`). Only a code checkpoint's first merge names a gate.
- **Re-run.** A hash already merged reads `PushedTip | NotYetPushed` from origin, skips the push when pushed, and re-runs every promotion, each a no-op at the tip. A merge not yet pushed is one a killed run left untested, so the re-run runs its merge tests first, and red undoes the merge as on a first run. A failed promote is re-run with the same command.
- **Change review** (`scripts/production/change_review.py`).
  A visible checkpoint line has three fields: `<what changed> | <how to try> | <what you see>`.
  An optional fourth field, `after <step>`, names a required rebuild, restart or deploy.
  An invisible change uses `Change-Review: none — <why>`.
  `problem` reads both visible forms through the shared parser.
  A merge message can carry only invisible lines.
  The hook and merge gates reject visible lines and `Change-Review-Covers` lines on merge commits.
  A checkpoint that changes more than Markdown needs a valid trailer unless a later plain commit covers it.
  `Change-Review-Covers: <hash>` names each covered commit.
  The merge gates retain their staff reads before publication.

  Merge callers use `list_ready` after promotion succeeds or the Mac pull enters the shared pending queue.
  A failed required step adds no rows.
  Git publication receipts and style notices retain their existing behavior, including on failed promotions.
  There is no `change-review` receipt.
  `list_ready` fetches `origin main` in the source checkout before it checks ancestry.
  A failed fetch prints one error line, adds nothing, and does not fail the merge.
  It scans first-parent merges on the production's merge branch from 2026-11-06.
  Older merges add no rows.
  It excludes foreign history through origin merge refs and `_publication_exclusions`.
  A local gate branch remains eligible when its name differs from the production merge branch.
  `review_commits` keeps every checkpoint and other commits without a descendant checkpoint in that merge range.
  The count and row writer use this same selection.
  `checkpoint_producers` reads recorded owners through `progress_history.load_events`, using the held staff snapshot.
  Legacy records use `StaffSnapshot.unit_by_worktree`.
  Merge writers record the known producer when they make the merge.
  An inner merge record supplies ownership for handoff commits without a checkpoint owner.
  Commit subjects supply no Producer identity.
  The Production cell uses the producing unit's showrunner staff id.
  Missing ownership leaves Producer empty.
  Promotion-only scans use the promoted checkout.
  The [repair overview](repair-rounds.md#mac-pulls-and-visible-change-ownership) defines these records.
  A row requires its merge to be an ancestor of `origin/main`.
  The row records that merge's short hash.

  A line with `after <step>` waits outside the list.
  The scan prints the merge, the step and the exact `live` command only for uncovered lines.
  The session that completes the step runs `change_review.py live --merged <hash> --step <step> --repo <checkout> --production <showrunner>`.
  `live` requires both arguments and releases only the named step through `list_ready`.
  A step with no matching line gives an error.
  `add --merged` and `live` share the ancestor check and refusal text.
  `append` and `drift` are removed.
  Git trailers hold waiting changes.

  A row in either section covers the same change when its merge contains the checkpoint.
  A row without a known merge also covers the change.
  A later fix can add a new row with the same words.
  `_merge_covered` reserves text matches before pairing unused rows by this merge's hash.
  It pairs ready changes first, in commit order.
  It pairs held changes only when every remaining held change names one step.
  `list_ready` supplies all changes routed to each list, including for a selected `live` step.
  Each unused row covers at most one remaining change.
  Several uncovered held steps prevent hash pairing because a row stores no step.
  That case can add a duplicate row; it must not hide another change.
  The directory lock covers the missing-row check and the row write.
  A merge with one existing line adds only its missing lines.
  A repeated call adds nothing.
  Concurrent calls retain each row once.
  A missing list starts from the existing skeleton.
  The required **Change review** field selects the production's list.
  Each top-level showrunner owns one list; child rows reach the root's list.
  A child without the field uses the merging production's list and keeps its own production credit.
  A different path in the field prints a warning.

  `publication_changes` joins each checkpoint branch to its unit staff row.
  The row supplies the production document and the fixed Producer id.
  An unresolved unit uses the merging production and an empty Producer cell.
  A style production credits its parent.
  Each open Production cell holds the producer's fixed staff id, or the showrunner's id when the producer is unknown.
  Readers follow current staff names and parents.
  Reviewed rows retain the production name recorded at the verdict.
  `/next` shows the selected production and its descendants.

  Each row receives the next C number, the write date, the merge hash and its How to check cell.
  The How to check cell contains `<how to try it> You see: <what you see>`.
  The writer inserts a period when the first part lacks closing punctuation.
  The On the Mac build cell starts as `not yet` when that column exists.
  Writes update `date_modified` and the frontmatter's `productions` list.
  The list skeleton includes date fields, the showrunner, a title, To review and Reviewed tables.
  A directory lock protects every write without a separate lock file.
  A write commits only the list when the list is in a Git worktree.
  It never pushes the commit or changes another staged file.
  A failed commit prints `written but not committed`; the next write commits the complete list.
  The vault's Templater must exclude the change-review folder to prevent concurrent changes to new lists.

  `add` records ready changes that have no checkpoint line.
  `edit` retains the row id, merge, other cells and position.
  `show` displays the next row only when the user asks.
  `verdict` moves a row to Reviewed; `works` or `fine` accepts it.
  Other verdicts return the change to its unit.
  `list-path` and `showrunner` resolve the list and its root owner.
  `backfill-production [--list L] [--write]` retains its older owner-cell repair.
  It traces an empty cell through the merge's checkpoint, production ownership, then first-parent history.
  It prints unresolved rows and writes only with `--write`.
- **Interrupted checkpoint recovery.** A successful retry scans the same Git history after promotion completes.
  It adds each missing ready line once, including when an earlier call stopped after the list write.
  A failed main push or Mac pull adds no rows.
  The next successful call adds the rows from earlier promoted merges.
- **Overrides for tests:** `MERGE_CHECKPOINT_VERIFY`, `MERGE_CHECKPOINT_VALIDATE_AND_PUSH`, `MERGE_CHECKPOINT_REVIEW_REGIME`, `MERGE_CHECKPOINT_SCRATCH`.
- **Unit targeted tests.** A unit runs `scripts/production/targeted_test.py --checkout TREE`.
  It uses the shared runner with affected-module selection.
- **The `~/.claude` repo's own gate.** `scripts/production/merge_gate.py` checks ancestry and checkout changes before it merges a checkpoint with `--no-ff`.
  The gate runs all test modules under a memory governor.
  Basedpyright runs only with `--nightly` or an explicit `--type-dirs`.
  Recursive discovery sorts module paths and uses the first folder under `scripts/` as the suite.
  Test modules use `builds.slice` scopes when available.
  After the parallel pass, up to eight failed test modules run once more, alone.
  Runner-killed jobs do not run again.
  The final results decide rollback or push.
  A signal also causes rollback.
  `--promote` sends the green merge to `~/.claude` main and the Mac.
  See [merge-gate-and-test-speed.md](merge-gate-and-test-speed.md).

The showrunner still judges, before the call: the other units' overlap notice, new public items without a consumer, the design check. After it: `<ChangeReviewFeatureLog/>` for a merge in `~/rust/hana` or `~/rust/hana_catalyst` (one feature-log proposal per change-review row; `docs/as-built/feature-log.md`), then `<ClearGate/>`, `<CrossUnitChange/>`, `<CIPoint/>` and the review watch. It sends the script's `send <unit>:` line and puts every `into:` line in the user update, adding one in the same form for each unit worktree it then merges the merge branch into. A change for the user to check is never an open question: it goes on the change review list, and `<CloseChangeReview/>` takes the user's verdict for every production, with feature-log calls only where the project closes out by change review (Hana and `~/.claude`).

### Smoke launch

`smoke_launch.sh` resolves its checkout to an absolute path before any folder change.
`checkout_cargo` runs `cargo build` and `cargo metadata` through `direnv exec` from that checkout.
The checkout's `.cargo/config.toml` applies even when another folder calls the script.
The caller's working directory stays unchanged.

### Production open, promote and wrap: `production_lifecycle.py`

`production_lifecycle.py load|open|promote-main|wrap --production <showrunner name> [--no-ci] …`. `--no-ci` makes the CI, smoke-launch and Mac-run steps print `<step>: ok — skipped: no CI` and is refused together with a verdict flag.

- `load [--resume] [--scratch <dir>]`: checks the checkout. With `--scratch`, it prints `state-dir: ok — <dir>` after the copy above. On resume or for a running doc, it reads the current state file through `update_registration.judgment_state`. It prints the `next` items, or `no next list`, and each unit's last code merge and session. A refused state file prints one `anomaly:` line; load continues.

- `open --session <name>` (status `planned` only): creates the merge branch, sets the doc to `running`, commits `production(<name>): plans for <N> units` with the unit plans, pushes with upstream, adds `LOG` to `.git/info/exclude` and creates it with `# Production log — <name>`. A re-run resumes at the first step not done.

- `promote-main --ci-green <sha> --smoke-passed <sha>`: each verdict must resolve to the merge tip, else held; `origin/main` must be an ancestor of the tip; each Promote destination prints `already at <tip>`, `pushed <tip>` or `advanced to <tip>`; with no destinations it pushes `<tip>:refs/heads/main`; the declared Mac is pulled even under `--no-ci`.

- `wrap [--close-out-done "<exact item>"]…`: a running doc is held first (`children: held`) while a child showrunner staff row has a live showrunner (**Nested showrunners**, phase 6), except a `**Kind:** style` child, which stays attached (`refuse_running_children -> RunningStyleChildren`); each `## Close-out` item must be passed back verbatim, else held; then the verdicts, clean and merged unit worktrees, push, `promote-main`, cargo-berth reservation release (`retire_reservations` reads `berth_board.read_board` and releases each reservation the unit worktree holds; `BoardUnavailable` prints `retire: skipped — board unavailable (<reason>)`), worktree and branch removal (local and origin), `notifier.sh remove showrunner-<staff id>`, the `children` step (`detach_style_children` detaches each running style child and tells it `<showrunner name> wrapped, so you are a root now: …`), the `tree` step (phase 6), the `tidy` step, the `phase-notes` step (`phase_table.py archive`, reporting `archived N phase note(s)` or `no phase notes to archive`; a failure exits 2 with the doc still `running`, so a rerun repeats it), status `wrapped`, a doc kept in a `productions/` folder moved to `archive/<repository>/<showrunner name>/` beside it, commit `production(<name>): wrapped` and push, and a final `| Area | Result |` table. A doc already `wrapped` (a failed final push) resumes at the doc commit and push. The staff row supplies the archived document path for a repeated command.

- `produce.md` names each subcommand in `<LoadProduction/>`, `<OpenMergeBranch/>`, `<PromoteMain/>` and `<Wrap/>` and keeps the close-out approvals, the live smoke verdict and the CI, smoke and mirror text.

### Updates, time, log and questions: `update_registration.py`

- `time <ISO or HH:MM>` prints `time: ok — HH:MM <zone>` in the user's zone, never UTC.

- `log "<event>" [--state <file>] [--before-compaction]` appends the event with the user-zone time.
  Every tenth event and each explicit compaction log also writes a STATE block.
  The block lists staff units with their phase, last merge, and wait.
  Missing judgment fields show `not stated`.
  It also records held merges, open user items, and the state's current `next` list.
- **Compaction hooks**: `scripts/hooks/showrunner-compaction.py` acts only for a session named by a production update timer (`showrunners.production_of`). On SessionStart `compact` or `resume`, it names `/showrunner:produce <doc> resume`, unless the resuming prompt already runs it. The command prints the current `next` list from the state file. The showrunner acts on that list first. PreCompact writes no state block. A showrunner can still run `log --before-compaction` by hand. Compaction runs by itself during work. The per-call context message is off in `config/context_usage.conf`. With `message = on`, the hook uses its existing thresholds and showrunner text.

- `questions [--state <file>]` applies the state's `open_for_user` items.
  Questions use `QuestionRecord` files; reminder strings use `OUTSTANDING`.
  `OUTSTANDING` is `<SHOWRUNNER_STATE_DIR>/outstanding/<showrunner staff id>.json`.
  A missing `open_for_user` key preserves the existing items.
  A new question requires its full text and a review result before storage.

- `ask --text "<one line>" --question "<full>" [--unit <unit>]` files a reviewed question without a reply to the user.
  The unit must belong to the production in staff.
  The writer stores the asker's fixed staff id in `asked_by`.
  A held review refuses before any write.
  `QuestionRecord(path, owner, asker, item, status)` stores the canonical question.
  `question_folder` uses the owner row's document to resolve its Proposals folder.
  Without that field, it uses `<SHOWRUNNER_STATE_DIR>/<showrunner staff id>/questions`.
  A missing showrunner owner gives `QuestionOwnerUnavailable`.
  A unit rename rewrites no question record.
  `questions-table` lists the open questions for `/showrunner:questions`.
  `migrate-questions` converts legacy outstanding questions once; a repeated conversion changes nothing.
  Wrapping a production closes its questions before state cleanup.
- `answer --item <words>` removes one matching item from this production or a production below it.
  The item's text must equal or contain `<words>`.
  More or fewer than one match exits 2.
  It prints `relay: to <unit>`, `relay: to the showrunner of <showrunner name>, for <unit>`, or `relay: none; …`.
  It then prints `answer: ok — <showrunner name>: <text>`.
- `withdraw --item <text>` removes one open item with exactly that text from this production or a production below it.
  Use `update_registration.py withdraw --item <text>` when the cause of a filed item is gone.
  It writes a `withdrawn` time to the history and writes no answered record.
  It sends no relay line.
  It prints `withdraw: ok — <showrunner name>: <text>`.
- **Filed items stay.** Registration retains an omitted item that has `asked_by`.
  Only `answer` or `withdraw` removes that filed item.
  The item lock uses the showrunner's fixed staff id beside `OUTSTANDING`.
  Each operation reads one staff snapshot and resolves current names from fixed ids.
- `--state` selects `showrunner_state.json` in the production session state directory.
  It accepts unit judgments, held merges, and `open_for_user` items.
  Reminder items contain one line.
  Question objects can contain text, a due time, and the full question.
  Unknown keys or wrong types refuse before writes.
  New questions require review.
  The showrunner carries the reviewed text in its reply.
  The [question review design](message-clarity.md#question-review-in-showrunner-replies) defines the review.

### Production status

`/showrunner:units` shows production status.
The report renderer, report schedule, child report rows, and report interval command are retired.
`unit_status.py` owns the shared unit-status parser.
`update_registration.py` owns question types, reading, and due filtering.
Registration retains a stopped timer for discovery and session ownership.

Timer folders and targets retain the showrunner's fixed staff ID.
Discovery uses the staff row's current session and current name.
Registry names do not affect registration or discovery.
The herdr session list classifies showrunner panes through native session identity and the owning staff ID.
Quota notices retain staff IDs for named recipients.
Current staff names resolve at notice creation; former names do not resolve.
Explicit `session:` and `uds:` recipients retain their native addresses.
Wind-down projection keys and count timer commands use staff IDs.
Count timers resolve the current session through staff when they fire.
Wind-down starts clear projection state; this change adds no conversion.
Worker completion notices resolve the director's staff ID after the worker lock releases.
Friend waits use the native process and the last transcript prompt's reply address.
Consultations need no staff row.
Unrelated panes with another agent kind do not stop the hook.
Pane labels do not identify a friend.

- **User zone.** `scripts.lib.user_zone` supplies `user_zone`, `user_zone_name`, and `zone_label`.
  `config/user_zone.conf` supplies the configured zone.
  `USER_ZONE` overrides that value, and `USER_ZONE_CONF` selects another file.
  Production documents do not select a zone.
  Scripts use package imports; shell displays call the helper through `scripts/lib/py`.
  A stored time retains its original zone when that zone identifies the recorded instant.
  Tests select a private zone when their expected output requires it.
- **`/zone`:** `commands/zone.md` runs `scripts/lib/zone.py`.
  Bare, it prints the configured zone, label, UTC offset, and current time.
  `set NAME` resolves an IANA name, city, or current seasonal label.
  Ambiguous or unknown names exit 2 with the candidate offsets.
  Publication runs on natedev through `main_publish`, shared with `/tune`.
  It updates `zone=` while preserving comments, commits only that file, and pushes main.
  A failed commit keeps the new text when the file already matches HEAD.
  Otherwise, it restores the previous text and refuses publication.
  The Mac pulls through the shared helper, and the broadcast reaches both machines.
  A failed pull queues its notice in `<XDG_STATE_HOME>/zone/mac.json` and broadcasts locally.
  Pending pulls retain each Mac path and source.
  A bare call drains the queue before it returns.
  Promotion runs `zone.py drain --pulled <path>` after a successful pull.
  The drain skips only that path and pulls every other pending target before sending the notice.
  Clearing one target preserves other targets and an undelivered notice.
  A legacy queue without `pulls` represents one default `~/.claude` pull.
  `ZONE_SSH`, `ZONE_BROADCAST`, and `ZONE_STATE` select private test dependencies.
  A repeated request commits an uncommitted zone even when its stored value already matches.
- **Status line assignment.** `statusline.sh` sends the native session ID and staff data to `scripts/statusline/assignment.py`.
  `StaffSnapshot.by_current_session` selects the row.
  The row supplies the current name, kind, and parent name.
  A showrunner displays `<name> showrunner`; a worker displays `worker`.
  A unit displays `<name> unit`, with `for <parent>` when the parent exists.
  Its first line displays `<assignment> | <folder> | <branch>` when the branch is available.
  Branch lookup starts at `workspace.current_dir`, else `cwd`, else the process folder from `--arg pwd`.
  It resolves symbolic links in that folder before the parent search.
  It searches parent folders for the first `.git` entry and reads `HEAD` without a `git` process.
  A `.git` folder contains `HEAD`.
  A `.git` file supplies an absolute `gitdir` path or a path relative to its containing folder.
  A `ref: refs/heads/<name>` value supplies the branch name.
  A detached `HEAD` displays `detached <first 7 characters>`.
  A missing repository, an unreadable file, or a folder resolution failure omits the branch field.
  Other roles keep their first line unchanged.
  Missing or damaged staff data displays `unassigned`.
  Copied `STAFF_ID`, timer targets, folders, and names do not select a row.
  Quota and notifier refresh paths keep their own behavior.

- **For you.** A unit director sees waiting questions, changes, proposals, and efficiency items from that unit.
  The payload's current session selects the unit staff row.
  The unit's parent supplies the production document.
  Counts match the unit's fixed staff id against `asked_by`, `Producer`, `proposed_by`, and `[by: <staff id>]`.
  Items without a recorded source count for no unit.
  Unit counts omit unrouted changes.
  Showrunner sessions retain the full production counts.
  `counts_row.py --unit <staff id>` writes `<showrunner state>/counts/<unit staff id>`.
  Each cache retains the counts line, deadline, and watched paths.
  A unit cache ends with `source=unit:<staff id>`.
  A source marker mismatch forces a refresh, even when the cache is newer than staff.
  The status line refreshes only that unit's cache when a watched path changes or the deadline passes.
  Failed reads use the existing retry deadline.

- **Configured zones.** The status line reads `USER_ZONE`, else `zone=` from `USER_ZONE_CONF` or `config/user_zone.conf`.
  `send_later.py --at` reads the user's zone and sends UTC to systemd.
  `verify.sh` uses the user's zone for recorded results.
- **Display times.** User displays use `scripts.lib.user_zone`.
  Stored instants keep their offsets.
  Tests select a private zone when their assertions need one.

### CI points and review watch: `ci_points.py`

`ci_points.py due | ci start [--cancel-prior] | ci collect | watch | notice clear <gate> | notice lift <gate> --log <path>`, each with `--production`; `due`, `ci`, `watch` take `--state-dir`; `due` and `ci` take `--no-ci`.

- `due` counts distinct recorded unit checkpoints that reached the merge branch. Five checkpoints after the last CI point produce `CiDue`. A collected point saves the count and tip in `ci_points.json`. Shrink commits and child hand-offs have no unit checkpoint record.
- `ci start`: `bash validate_and_push.sh --to <merge branch> --fix-commit "ci(<showrunner name>): validation fixes after <unit> phase <N>|production wrap" [--cancel-prior]`; reads `repo`, `sha` and `run_id` from the `=== CI HANDOFF TO AGENT ===` block (`run_id: none` fails); the sha must be the merge-branch tip; writes `ci_run.json` (`repo`, `run_id`, `tip`), the only handoff to `collect`.
- `ci collect`: takes the tip and its code-merge count before `gh run watch --exit-status`, then `gh run view --json headSha,status,conclusion,jobs`. It passes only when `headSha` equals the validated tip and the current tip, the run is `completed` and concluded `success`, job `Test Suite` succeeded, and every other job succeeded or was skipped. Then it records `{count, tip}` and prints `--ci-green <tip>` as the last line, which `promote-main` and `wrap` take as is. Anything else is `ci collect: failed — rerun <check> on <tip>: …`, with no flag; a missing or empty `headSha` is never a pass.
- `watch` / `review_watch(production, state_dir) -> WatchFirstAlert(line, table) | WatchRepeat(line) | WatchBelowThreshold(line)`: runs `review_regime.py watch` under `fcntl.flock` on `review_watch.lock`. On the first exit 3 with no `review_watch.json`, it takes `review_regime.py report --since 2026-09-28`, appends `- HH:MM <zone>: review watch: <line>` and the table to `LOG`, then writes `review_watch.json`. It sends no phone push: a status note that asks nothing goes to the log (`docs/as-built/message-clarity.md`). The shared state prevents repeated first alerts.
- `notice clear <gate>` drafts `send <waiting unit>: From the showrunner: <gate> clear — <unit> phase <N> is on <merge branch> as <merge hash>. Merge <merge branch> and continue.` (refused until that code merge exists); `notice lift <gate> --log <path>` drafts `… <gate> lifted — your tests pass without <unit> phase <N> (<log>). Continue.` Gates rows read `| <id> | <waiting unit> phase <N> | <unit> phase <N> | … |`.
- Overrides for tests: `CI_POINTS_VALIDATE_SCRIPT`, `CI_POINTS_REVIEW_REGIME`, `CI_POINTS_REVIEW_REGIME`.

### Unit configuration formatting

`unit_config.py` owns the unit's `.unit.toml` and its parked configuration.
`UnitConfig` carries the plan, port, responsibilities, retirement state, and formatting choice.
Formatting is `RepositoryTaploConfig(text)` or `DefaultTaploConfig`.
It does not change configuration equality.

The writer reads `.taplo.toml`, then `taplo.toml`, from the repository when present.
It formats through `taplo fmt --no-auto-config` with an explicit input path and the selected configuration.
It validates the decoded values before publication.
It never publishes an unformatted fallback after a formatter failure.
`Published` and `AlreadyCurrent` distinguish changed bytes from matching bytes.
Matching values in differently formatted text still produce `Published`.

A parked configuration saves the repository formatting text as `taplo_config`.
A later write uses the present worktree's formatting when available, then the saved formatting, then the caller's formatting.
`repair_port` compares the recorded old port under the writer lock before it publishes the settled port.
A different concurrent port produces a conflict instead of an overwrite.

### Unit lookup: `unit_lookup.py`

Staff holds each member's current name and native session ID.
The lookup resolves live process evidence through that native ID.
The [staff identity contract](staff-accessors.md#staff-identity-and-backends) defines the stored facts.
Saved progress state uses the fixed staff reference instead of a copied session name.

- **Readers.** `live_units.py <showrunner>` joins the production's staff units with their current sessions. It prints the unit, its pane, `live|stopped|unknown|gone` and its session name, separated by tabs. `unit_status.sh`, `broadcast.py`, `codex_winddown.py`, `stall_watch.py`, `waiting.py` and `production_lifecycle.py` take a unit's pane or name from the lookup at the moment of use. Saved state (judgments, status blocks, decisions seen, stall stretches) is keyed by unit id.

- **Unit phases (`unit_phases.py [--production <showrunner name>]`, `/showrunner:units`, user 2026-10-28).**
  The command prints one row per production staff unit.
  Without `--production`, `showrunners.own_name()` selects the production name.
  `read_production(name)` reads the document from that showrunner's staff row.
  A missing owner or document exits 1.
  Unit shows the unit's staff name (`row.name`), including stopped units and units without a session.
  All cells escape pipes.
  Both tables use `display_cells.fitted(headers, rows, *, cuttable, columns)` before escaping cells.
  `cuttable` contains original column indexes in cut order, so equal header text does not change the selected columns.
  The helper omits columns whose cells are all `—`; a table without rows keeps its columns.
  Both callers read `units_table_columns` from `config/production.conf` at use; the default is 170.
  The helper measures display columns with `terminal_cells.terminal_width` and allows four columns for the terminal margin.
  Row width is `1 + sum(width + 3)`, with each width at least three columns.
  The helper cuts text in the specified order until the rows fit or each cuttable column reaches its header width.
  A cut adds `…` and never splits a wide character.
  Fixed columns and minimum text widths can exceed a smaller configured width.
  The command reads terminal metadata, never pane contents.
  - **Status and anomalies:** Both tables derive Status through the shared session classifier and the staff hold fields.
    `live_units.read_units(name)` returns `UnitListing(units, anomalies)`.
    `units_of(name)` uses that reader and prints its anomalies on stderr.
    `units_in(name, every)` joins supplied observations and adds each unit's staff status.
    `session_staff.anomalies` collects diagnostics and preserves other callers' stderr output.
    The command prints read failures below the table on stdout when they make the table wrong or incomplete.
    Staff name conflicts also produce lines. Each process has at most one line.
    Detached run checks share one process snapshot when needed.
    A process with an identity but no staff row produces no line and cannot use a unit's pane placement.
    A process without an identity reports an unreadable thread when its pane holds staff.
    One staff candidate retains its pane placement with unknown status.
    `CodexProcessThreads.thread` reads resume arguments, the native pane session, and the thread environment in that order.
    A child Codex inherits its parent's `CODEX_THREAD_ID`, so the environment value never comes before the other two.
    It then reads the terminal title with daemon names.
    These pane sources require this process to be the sole foreground process.
    A client that shares the foreground with another process has no thread. It prints one line and refuses nothing.
    The worker scanner uses this reader before it joins the current thread to its staff row.
    A missing pane launch record does not make a resolved staff worker unknown.
    `scripts/message/morty_identity.py` supplies the shared match against `MORTY_NATIVE_ID` from `config/send.conf`.
    A match produces no anomaly.
    `unit_phases.send_anomalies` sends all lines once to `UNIT_ANOMALIES_TO` from `config/production.conf` (`sdlc`).
    A failed send adds one anomaly line and preserves the table and exit code.
  - **Pause and exit:** `agents pause <unit> --why "<why>"` records `paused: {since, by, why}` and stops the workers.
    The director finishes its current turn and holds at the prompt.
    `agents exit <unit> --why "<why>"` records `exited: {since, by, why}` and stops the director and workers.
    Both commands retain the staff rows and tabs.
    Add `--finish-step` to wait for the workers' current tasks before stopping them.
    `agents pause --production <showrunner name> [--except <unit>...] --why "<why>"` uses the production membership reader.
    `agents resume <unit>` removes a pause and sends one continuation. Failed delivery restores the pause and returns failure.
    For an exited unit, resume starts its retained conversation on the role account. Success removes the exit record.
    `agents resume --production <showrunner name>` resumes the production's units.
    `since` uses the user zone. `by` uses the caller's staff name.
    Staff row copies retain the hold fields.
  - **Phase productions:** `| Unit | Status | Progress | Phase | Notes | ETA Next | ETA Plan | Workers | Non-Phase Work | Queued |`.
    ETA Plan appears only when at least one row has unfinished plan phases.
    Status uses the [shared session classifier](accounts.md#status-counts-and-notes).
    A live Claude or Codex pane supplies live evidence.
    Both tables add a **Machine** column after **Status** only when a row runs on another machine.
    The home machine is the `kind.<machine>=local` entry in `config/build_targets.conf`.
    `worker.py list` places the optional machine field after the account field, and takes the machine from the parent's worktree.
    `worker.py list --json` keeps its existing fields.
    Both tables sort unit names without case distinctions before they select their rows.
    Progress shows only `🟩` done, `🟨` open, and `⬜` not started.
    Plans above 20 phases use 20 equal groups, with open before not started before done.
    The bar has no text cut, and all three characters use the same display width.
    When the table keeps Progress, it adds a blank line and `Progress: 🟩 done · 🟨 open · ⬜ not started`.
    `progress_cell` and `progress_legend` use the same `ProgressState` constants for the squares and labels.
    A table with no Progress bar in any row has no legend; the style table also has no legend.
    Phase shows `<done>/<total>` from the same phase rows, or `—` with no phases.
    Notes shows `phase <id> — <step>: <task>`, with the phase title when no step is open.
    Other notes are `between phases`, `not started`, `plan done`, `no phases`, and `no plan`.
    Repair rounds append `fix rounds <n>` and `+ 1 running` in Notes when applicable.
    Notes first limits text to 80 characters and ends longer text with `…`.
    The table then cuts Queued, Non-Phase Work, Workers, and Notes, in that order, to fit the width.
    Notes keeps its text while another cuttable column can shrink.
    Unit, Status, Machine, Progress, Phase, and both ETA cells keep their text.
    When the plan has no phases, an open run supplies Progress, Phase, and both ETA cells from its full event history.
    Notes uses this fallback when it has phase rows; otherwise, it keeps the plan state.
    The fallback does not change Non-Phase Work or Queued.
    A finished newest run supplies no fallback.
    ETA Next uses the open plan phase's latest promise from `eta_trail`, never `schedule_end`.
    Otherwise, it uses the newest open phase outside the plan, then the next not-started plan phase with a promise.
    Passed promises add `late <elapsed>`; open work without a promise shows `no ETA`.
    With no open work or promised phase, ETA Next shows `—`.
    ETA Plan shows the predicted plan finish without `ends`; a row without unfinished plan phases shows `—`.
    The finish never precedes any open or not-started plan phase's latest promise, including checkpoint promises.
    Times use the production zone and a short date outside the current day.
    Workers uses `worker_runs` to show waiting runs, queued results, missing runs, and working workers.
    Run workers appear first; idle workers append a count, and closed workers add nothing.
    Remote units show no worker facts.
    The command reads panes once for the displayed units and their workers.
    It shares one lazy process snapshot and reads no CPU totals or log tails.
    Non-Phase Work uses the unit's newest unfinished run and shows open phases absent from the configured plan.
    Queued combines the first queue item with the first unfinished plan phase, excluding done and open phases.
    Each item or title uses an 80-character limit; counts remain after the cut.
    A configured plan with phases takes priority over the newest run's plan.
    The fallback plan path resolves against the staff worktree.
    An unreadable queue retains `queue unreadable` and reports its path and cause.
  - **Style productions:** `Kind: style` selects `style_units.table` for all staff units.
    Other productions use it for units named in their `Style units` table.
    `style_config.has_style_units` detects that table; `style_config.read_config` reads its assignments.
    It prints `| Unit | Status | Ticket | Step | ETA | Findings built | Last 24 h | Findings left |`.
    Ticket shows `#<number without unit prefix> · <rule stem> · <crate> · <ticket work>`.
    An empty rule uses the first rules-list stem plus `+<extra count>`.
    `ticket_work` gives `<n> findings` for a proposal ticket, `<n> sites` for a listed ticket and `<n> files` for read or structure.
    A count of 1 uses the singular.
    The table shows the oldest assigned or checkpointed ticket.
    Otherwise, it shows `idle (<HH:MM>)` with the last refill time, else `idle`.
    `NoTicket` represents a unit with no such ticket.
    The table cuts Step before Ticket to fit the width.
  - **Style step:** `format_step(step, ticket, idle)` says in words what the unit does now. It takes `open_step` on the newest run. When the step's activity text is not empty and is not a generic word, the cell shows that text, for example `building 5 frontend-boundaries moves`. Otherwise it maps the label: `impl` → `writing <ticket work>`, `fix` → `fixing review findings`, `review` → `reviewing`, `test` → `testing`, `checkpoint sent` → `waiting for the showrunner to merge`. Another label shows as it is; an empty label shows `—`. With no open step, an assigned ticket shows `starting <ticket work>`, a checkpointed ticket shows `waiting for the showrunner to merge`, and a unit with no ticket shows `idle — <last refill outcome>` or `idle`. Step includes the refill outcome and any sync wait reason, subject to the table width. The cell never shows a bare `impl` or `checkpoint sent`. ETA (`eta_cell`) is `current.eta.time` as `HH:MM`, else `—`.
  - **Style counts:** The production's `Style units` table assigns families to each unit.
    `style_units.table` sums `FamilyCounts` across those families for each unit row.
    Findings built counts all built findings; Last 24 h counts findings first built in the last 24 hours.
    These columns and Findings left align right; text columns align left.
    The recent count uses `built_at`, with `style_ledger.legacy_built_at_sql` for ledgers before schema 25 (`style-showrunner.md`).
    Findings left (`left_cell(approved, filed, split, now)`) counts filed findings with approved decisions.
    `FamilyCounts.filed` counts every filed finding in one family.
    Equal counts show `<approved>`; unequal counts show `<approved> approved of <filed> filed`.
    A recorded split appends `: ready <a> · changed <b> · structure <c> · other <d> (<age>)`.
    Age counts whole minutes below 60 (`<n> min`), whole hours below 24 (`<n> h`), then whole days (`<n> d`), since `split_at`.
    A split row with no refill outcome leaves an idle unit as `idle`.
    The reader reads each distinct proposal file once.
    `proposal_issue.decision_answer` determines approval; the pinned rule blob determines the finding's family.
    A built finding with no decision file uses its build ticket's family.
    The total line is:
    `All families: <built> findings built · <approved> approved findings left[ of <filed> filed] · <r> built in the last 24 h · ETA at that rate <YYYY-MM-DD>`.
    `of <filed> filed` appears only when the counts differ.
    `<filed>` sums `LedgerSnapshot.total_filed` across distinct ledgers, including findings with no resolvable family.
    The date is now plus `approved / r` days.
    Zero recent builds gives `no ETA: nothing built in 24 h`.
    Numbers use thousands commas; times and the date use the production zone.
  - **Style reads and tests:** `unit_phases.main` passes each unit's staff worktree to `style_ledger.ledger_location`. A missing plan folder does not affect this lookup. A worktree that cannot be resolved supplies no ledger data for that row. It passes the explicit unit-to-database map to the renderer even when phase records are unavailable. One read-only SQLite connection serves each distinct database; missing databases or tables supply no data. No ledger lock or migration runs. The command overlaps independent reads. `display_cells.py` owns the shared step and escaping helpers used by both tables. `style_units.table` requires the keyword argument `ledger_paths` (unit id to database path); it never reads ledger paths from phase records. It also accepts `now`, `zone`, and `decision_folder` for fixed test inputs. Relative proposal paths use that folder; absolute paths stay absolute.

- **A unit's newest run.** `unit_run.py --production <showrunner name> <unit>` prints the run id, machine, and UTC start.
  Staff supplies the current unit folder.
  Each candidate `run_started` event resolves its owner through staff before the reader compares that folder.
  The newest numeric start wins; sorted file order settles equal starts.
  Invalid or unreadable files are skipped.
  The scan stops after the first valid run-start event in each file.
  `--root` selects the history root, then `UNIT_HISTORY_DIR`, then the configured history default.
  With no local match, the reader queries the Mac unless `--no-mac` is present.
  An unavailable Mac prints its cause.
  No matching run exits 1 with the unit and folder in the diagnostic.

- **Open proposals table (`proposals_table.py [--production <showrunner name>]`, `/showrunner:proposals`, user 2026-10-12).** Prints one Markdown table, `| Proposed by | Proposal | Date | State |`, of every proposal still open for the user. It reads only, and finds the doc as the units table does. Three sources, in this order:
  - **Issues** (2026-10-15, replacing the `proposals/index.md` table and the `docs/plans/proposal-*.md` scan): each `*.md` directly in the issues folder that the optional `- **Proposals:**` line names, of the production doc only (`proposals_folder.scope_docs`; the productions below list their own, user 2026-10-27), a file read once (`issues_folder`; a relative folder is taken from the showrunner checkout; without the line there are no issue rows). Subfolders hold research notes and are not read. `issues.py` reads the frontmatter between the leading `---` lines, a list inline (`[issue, proposal]`) or as `- item` lines, and strips quotes and a wiki link's brackets, keeping its alias over its target (`"[[2026-10-09]]"` reads `2026-10-09`). A row shows while `status` is `open` and `tags` holds `proposal`; rows sort by `date_created`. Proposed by is `proposed_by`, else `—`; the Proposal is the file's name; the Date is `date_created`.
  - **The plan backlog**: each line ending `proposed` or `deferred` of the ledger the `- **Improvement queue:**` line names (`proposals_folder.ledger`), for the doc only (user 2026-10-27); a production with no such line has no backlog rows. A file a line names is beside its ledger. The proposer is the `Proposed <date> by <who>` words of the file the line names, else `plan backlog`. Proposed lines join the issues waiting for review in `/next proposal` and the For-you proposals count; accepted and deferred lines do not count.
  - **Add-ons**: each top-level entry of `next_items_pending.md` in `/tmp/claude/unit/<run id>/` or `/tmp/claude/delegate/<run id>/`, for every run of the unit's plan (`plan_history.plan_runs`), with the list's last change as its date. A plan that has moved to `docs/as-built` is also looked up under `docs/plans`, the path its runs recorded. An entry's text is its `Title:` line, else its bold lead, else the words after its action word, else the first sentence of its `Proposed`, `Need`, `Why needed` or `Why` line. An action word alone (`add`, `change`, `remove`) is never the text (user 2026-10-12, after two rows read `add`).

  **State** says what the user can do, never a bare `next` (user 2026-10-12: "next after what?"). An issue that `proposal_issue.proposal_progress` reads as `InProgress` (its approval is recorded, or it has no answer and its `stage` is `active`) reads `in progress by <unit>` (`in progress` with no `unit`); else `waiting for your review`, as does a backlog line marked proposed. The waiting rows (issues and proposed backlog lines) come first in `scripts/lib/urgency.py` order (level from `review_urgency`, value the score of the proposed stars, prerequisite the first `depends_on`), then the in-progress ones, oldest first. A deferred backlog line reads `deferred until you ask for it`. An add-on is the showrunner's to settle (user 2026-10-12, below), so it reads `the showrunner decides: build, save for later, or drop`: the unit sends its add-ons to the showrunner, which answers `current`, `next` or `drop` for each.

  Staff supplies the unit's worktree.
  `proposals_table.table` reads `title_width` and `state_width` through one `conf.read("proposals_table")`.
  The configured values are 110 and 140 characters.
  With no rows, it prints `no open proposals`.

- **Nightly list (`scripts/nightly_review/candidates.py`, `/showrunner:nightly`, user 2026-10-12).** Prints one Markdown table, `| Pick | Night | Area | Candidate | Gain per week | State |`, of every nightly-review candidate still open. The ledger (`nightly-review/ledger.md` under `XDG_STATE_HOME`, else `~/.local/state`) is the one record. A night's proposal and each of its Also found and Noted items is a line of its own, `- <night> <scope>[, also found|, noted]: <title> [gain/week: <gain>] [by: <staff id>] — <state>`, open while it ends `proposed` or `deferred`. `record()` writes the ` [by: <staff id>]` tag only when the caller is a unit director. Printing changes no file but the vault note below.
  Recording prints and flushes new entries before it publishes the complete ledger atomically. An interrupted print leaves them available on retry. A partial final candidate from an interrupted append is replaced on rerun; proposed, deferred, accepted, declined, done and relayed states count as recorded, including the dated outcomes written by `/next`.
  - **Order.** `rank` reads the tag: `N min` or `N h` is user-visible time, `N%` is a share of tokens, and a line with no tag or other words is unmeasured. Time comes before tokens, each largest first; unmeasured lines come last, in the ledger's order. `/next efficiency` takes its candidates and their order from this table.
  - **Pick.** Row one reads `recommended` only when its gain is measured; with nothing measured no row is marked.
  - **Settings.** `config/nightly_review.conf` holds the nightly review's tunables: the state folders, the session and unit names, the review modes, the message sample size and history window, the table cell width, the note path and the command path. Each value is read when it is used. `XDG_STATE_HOME` and `NIGHTLY_REVIEW_NOTE` still override the state base and the note path.
  - **Vault note.** `write_note(body)` rewrites `~/rust/hanadocs/nightly improvement list.md` after the table prints and after every `record` that exits 0; `NIGHTLY_REVIEW_NOTE`, read at each write, replaces the path. The note is the line `Read <YYYY-MM-DD HH:MM> <zone label> from the nightly review's ledger on <short host>. The nightly review rewrites this note; edits made here are lost.`, a blank line, then the table as printed or `no open candidates`. The read time is in the user's zone, and the host is `socket.gethostname()` up to its first dot. The write is a dot-named temp file in the note's folder, mode `0o644`, then `os.replace`. The ledger stays the one record and the note is a copy: nothing is written when the note's folder is missing or this machine holds no `ledger.md`, so a machine without the ledger never replaces natedev's note. Every step after the path is chosen sits in one `OSError` handler, the two existence checks included, because `Path.is_dir` and `Path.is_file` can raise: a failure prints `candidates: cannot write <path>: <error>` to stderr and leaves stdout and the exit status as they were. The note sits at the vault root, outside the excluded `showrunners/` folder, so the vault's Git sees every rewrite. A test keeps `NIGHTLY_REVIEW_NOTE` on a temp path, because the default is the live vault.
  - **`record <report>`.** The watcher runs it on `Nightly proposal ready: <path>`; the night is the report's folder name. It appends the proposal (`# <title>`, `**Target:**`, `**Gain per week:**`) and each item under `**Also found:**` or `**Noted:**`: a `- ` or numbered line, whose bold lead is its title and whose closing tag is its gain. A paragraph closes the item list, so a list of what was denied that night is no candidate. A line whose start (`- <night> <area>: <title>`) the ledger already holds is not written again: a second run adds nothing, and a decided line keeps its outcome. A report with no title or target exits 1 and writes nothing.
  - **Earlier lines.** Before 2026-10-12 a smaller item's outcome was written on its night's line as `; from Also found: <title> — <outcome>`. Those stay as written; every item still open then got a line of its own.

- **Queued messages.** `sessions.addressed` resolves a staff name through its fixed staff ID and `current_session`. Explicit `session:` and `uds:` addresses select native records. Registry names and former names do not select recipients. A named recipient uses `STATE/queue/staff_<staff id>.jsonl`. An explicit session address uses `session-<session id>.jsonl`. A session drains its native keys, its socket key, and its fixed staff key. Queue entries retain their stable owner across session changes. A delivered keyed message removes its queued copy. `send.py retire` removes the selected queued messages. The one-off `convert_queue_keys.py` program moves current staff-name queue files into their staff ID files. Its default mode only prints the moves.
  Unit names also accept the form from `copies.unit_owner`.
  Morty uses its staff row when present.
  Without that row, `send.py` matches Morty's configured `MORTY_NATIVE_ID` against native transport addresses.
  Postmaster replies use explicit session addresses.
  `postmaster`, `sdlc-merge-gate`, and `natedev` have staff rows.

- **Not moved.** Native session queues and natedev holds retain their session ID keys. Staff-name queues need the one-off conversion described above. Old stall-watch files remain under their original names. Each unit starts a new stretch.

### Waits, renames and alerts: `waiting.py`

Each subcommand takes `--production`.
`main` catches `checkpoint_gate.Stop` and reports a bad doc on one line.
A unit uses its Unit value or its current session name.
`units_from_doc` reads `unit_lookup.doc_units` and the lookup.
`search` and `agenda` skip retired units.
`named_unit` (`scratch-test`, `eta-request`) refuses a retired unit as `unknown unit <name>`.

### Status and stall watch

Permission prompts notify their owning showrunner after 60 seconds, retrying until delivered; the saved `told` mark stops repeats. A still-blocked prompt alerts the user at its start plus 60 seconds plus `prompt_grace_seconds` from `config/stall_watch.conf`, independently of owner delivery. Missing or invalid grace prints an error and uses zero grace. A prompt cleared during the grace sends no push. Units whose showrunner is not running remain unwatched; notices grant no permission to answer another pane. Prompt state stores the owning session id, an immutable wall-clock observation-start time (taken before the pane and blocked-status reads), and its alert key; `since` only measures the wait. Stop and move call `stall_watch.retire_session_prompt`, which atomically publishes `{session_id, at}` without any lookup, watcher lock or send. At tick entry, the watcher scans saved prompts for that session and retires their stored keys only when `created <= at`. A prompt observed after a completed stop is untouched; a pane read that spans stop/resume can be retired, and the next tick observes the resumed prompt afresh. Launch deletion or parent changes cannot lose the old alert. The local write is synchronous; a failure is printed on its own line and never fails stop or move. Requests are removed after confirmed retirement, or when no matching prompt and no unresolved ownership remain. One reader classifies current, legacy and invalid prompt files for all consumers, reporting each bad file once per tick. Recoverable legacy ownership is migrated; orphan alerts with known keys retire independently of live unit discovery, and unusable keyless state is removed with a warning. Unreadable content and its cancellation obligation remain for retry without blocking identified retirements; each malformed request is reported and retained independently, including timestamp overflow, while other requests and alerts continue. Relays share the `sender_batch_seconds` limit (90 seconds in config), capped before the pass's 110-second deadline to leave `sender_cleanup_seconds` (two seconds in config) for cleanup. Showrunner discovery, owner socket lookup and RCA case locks share that deadline; failed work remains for the next pass. RCA first notices and reminders send concurrently within the remaining budget, below the notifier's 120-second watchdog.

The pass shares one absolute monotonic deadline across discovery, pane text and agent-status reads, sends and RCA work. Each discovery lookup receives a share of the remaining time, reserving time for later productions and inspection; a failed lookup prints once and leaves healthy results available in the same pass. The discovery result carries each failed production and its reason alongside the rows; one formatter renders log diagnostics, while single-value callers receive reason text without the command prefix; the showrunner listing, name, address and tree commands, and stall-watch, finish their healthy work before returning non-zero when a lookup failed. Single-name and address APIs still raise on unknown liveness rather than treating it as a stopped showrunner; incomplete registrations keep their existing warning-and-skip behavior. Herdr caps each pane command at the smaller of its usual 10 seconds and the remaining pass time. Once inspection time expires, each skipped unit prints one line and retains its saved prompt and idle state for the next pass. A status read that exhausts the budget also leaves that unit unchanged. Inspection ends `delivery_reserve_seconds` before the pass deadline (10 seconds in `config/stall_watch.conf`, read at use). This leaves time to deliver alerts found in that pass. The process snapshot shares the inspection deadline and reserves the configured cleanup time within it. A failed, timed-out or unavailable `ps` produces one process-snapshot diagnostic and unknown liveness, never an empty process table. That pass retains pane prompt and idle state, sends already pending deliveries and retires, then exits non-zero. Snapshot and sender children start in separate process groups. On timeout the watcher signals the owned group through the shared checked process-group helper, bounds pipe draining and child reaping by the cleanup deadline, and closes pipes that still remain open. A timed-out retire keeps its state for retry; neither inherited output pipes nor sender cleanup can extend the pass deadline. The pass reads and validates both sender limits once from `config/stall_watch.conf`, then shares them between the snapshot and delivery batch. An invalid override prints one sender-config diagnostic and uses the packaged limits, so pending relays and retires still run within the pass deadline; the pass returns non-zero after that work. Scheduling uses one atomically written `inspection.cursor`, separate from alert state. Work is sorted by production and unit, then starts after the previous pass's first attempted unit, wrapping at the end. The first eligible pane about to be read advances the cursor once per pass, even if its text or status read exhausts the inspection budget. A pass that starts no inspection leaves the cursor unchanged. With a stable eligible set, every unit gets first access to the budget within at most one pass per unit. Exhausted and skipped units retain their alert state byte for byte. A partial discovery never clears watchdog recovery evidence for unobserved productions, permits a safety decision about unknown child liveness, or reports a successful wind-down. Inventory callers that require completeness reject the partial result with its plain reasons; observational reports keep the healthy rows with their visible failure status.

- **Paused units and external waits.** `stall_watch.status_defers_bump` checks the latest assistant turn-end status in the transcript. The pane supplies a fallback when the transcript has no usable status. A pause or an explicit external-owner wait resets the stretch without a bump or report, including near compaction. The watcher reads `paused_status_pattern`, `external_wait_pattern` and `external_owner_pattern` from `config/stall_watch.conf` at use. The `owner` group identifies the dependency. Live peer names and the showrunner's staff name also match. Matching ignores case. Other `holding:` lines prevent a bump only when they name an outside party anywhere in the line. The watcher reads those party words from `holding_party_pattern` at use and also checks the same peer names. The unit's own name, pane label, Claude session name and full session address do not count as outside parties. A party occurrence inside a complete own name does not count. A separate occurrence or a longer peer name still counts. The watcher reuses the pane labels from discovery through the shared pane snapshot. It makes no extra herdr read for labels. A failed label read produces one diagnostic per session per pass. Prompt checks continue without the label. Both checks keep the status text unchanged. Hyphens join words, so `user-interface` does not name the user. A sentence period ends a name; `.v2` continues it. Such a hold gets only the per-unit log line `inspected <showrunner name>/<name>: holding`. It sends no delivery to the unit or showrunner. The hold leaves `tell_sent` false, so a later idle unit can receive its bump and tell. A hold naming only its own worker, work order, tests or build receives the normal idle bump when nothing runs. Running work resets the stretch, and the unit reads `running`. Running work is one of these: a process that `work_running` finds; a busy pane worker whose staff parent is the director (`worker_panes.worker_state(observed) == "busy"`); or a live detached run of such a worker (`detached_runs.live_runs(<held run folder>/detached_runs, <worker name>)`). The watcher checks pane workers only for a unit with no process work, and detached runs only when no pane worker is busy. `read_busy_pane_worker_parents` makes one `worker_panes.observe_parents` read for all directors of the pass, on first need, and makes none when no watched director has staff workers. The held run folder is `unit_lookup.HeldRun.folder`, which can be a verified marker target outside `unit_runs.run_roots()`. A worker that waits on its own detached run reads idle in its pane, so only the detached-run check finds that work. A failed pane read or detached-run read counts as no work. It prints one `stall-watch: pane workers:` or `stall-watch: detached runs: <unit>:` line, and the pass continues. A resumed unit gets a fresh idle stretch. An override may omit status patterns; the watcher reads those patterns from the packaged config. Prompt grace still requires a valid value in the selected file. Invalid status rules print a diagnostic and suppress the unit's bump for that pass. Other units and pending deliveries continue. Missing, unmatched or empty `owner` captures are invalid. Run records still control eligibility. Herdr's `blocked` status follows the permission-prompt path above. Shutdown adds no unit-pause flag; stopped directors fail the live-session check.

- **Expired login.** The watcher reads `login_limit_pattern` from `config/stall_watch.conf` at use. It checks the pane text after the last turn end, before the idle stretch. While the limit shows, the unit gets no bump. The showrunner gets one tell: `<unit>: the Claude login reached its time limit and needs /login.` The saved stretch keeps a `login_since` mark and `tell_sent`, so later passes send nothing more. The idle clock stays reset, and the log line reads `login limit`. The login is back when the usage store holds a successful read for the session's Claude login, newer than the session's last transcript activity. `login_restored` makes that read only for a pane at the limit. The login belongs to the account, so other sessions keep the old limit text after one `/login`. With the login back, the unit follows the normal idle path and gets its bump after the stall time. A value that cannot be read keeps the limit. A newer turn end also ends the limit. An old stretch file without the mark still reads.

- **`unit_status.sh` units.** `unit_status.sh <state-dir> <user-zone> --production <showrunner name>` takes its units from `live_units.py --production <showrunner name>`. Each block is headed by the unit id, and its text is read with `session_pane.py read unit <showrunner name> <unit>`.

- **`unit_status.sh`** `waiting_on_user`: nothing when a spinner shows in the last 12 lines; `FORM WAITING on you in <unit>: …` for `Enter to select`. Otherwise it reads the last turn-end line (`^\s*(— )?(holding|gate|decision|blocked|done):`). `decision`, `gate` and `blocked` are waits on the user: first sight prints `=== DECISION for you from <unit> ===` … `=== end <unit> ===` and records the key in `decisions_seen`; a repeat prints `STILL WAITING on you, <unit>: …`. A wait naming `showrunner` or a peer unit (whole word) is the showrunner's: no wait line, and only `blocked:` prints `BLOCK in <unit>, open <age>: …`, aged from `blocks_open`. Any other turn-end clears the block entry.

- **A hold with a stated end (2026-10-17).** A `holding:` turn-end with a future time stays silent until that time, including near compaction. The idle clock starts after the hold ends. The outside-party rule then decides whether the unit receives a bump. `stated_hold_end(line, now, zone)` takes the latest `HH:MM` in the line. An optional `YYYY-MM-DD` or `MM-DD` can precede it. The time uses the production's zone unless `UTC` or `GMT` follows it. A time without a date, or a date without a year, uses the occurrence nearest now (`hold_days`). Thus, `until 01:00` read at 23:00 means tomorrow. `since 23:50` read at 00:10 means yesterday. `Thu 10-22 11:47 EDT` means the nearest 22 October. A hold has no upper limit because real holds can last days. The watcher bumped mul_add at 05:32 during a measurement window whose stated end was 16:09.

### Staff relationships

The staff record stores showrunners, unit directors, and workers as rows.
Each row has a fixed staff id, a name, a kind, and its current native runtime id.
The native runtime id is a Claude session id or a Codex thread id.
A row's parent identifies its manager by staff id.
Readers join runtime observations to rows through the current native runtime id.
They read names and manager relationships from the selected rows.
Copied pane marks do not select rows.

## Invariants

- `production_doc.py` owns production field reads.
  Staff supplies membership and order; `.unit.toml` supplies unit settings.
  Reports retain `UnitMetadata.problems` when only some unit facts are available.


- Git and the unit records supply merged state. Unit renames do not require a commit subject reader. The checkpoint command still writes merge subjects for people.

- Every refusal comes before any write: `add_unit.py` refuses in `launch_request`, the shutdown-state check (before `preflight`), `preflight` or the live herdr session name check before the stub, row or commit, and a shutdown refusal ends no session; `--check` writes and starts nothing. The director resolves in `preflight`, so promotion never stops a session it cannot relaunch.

- `merge_checkpoint.py` restores the tip before the checkpoint merge on red when origin still has its old tip.
  Rollback includes main merges from this run and preserves earlier unpublished work.
  Promotion targets the pushed merge-branch tip.
  Main is pushed only when it holds no commits outside `origin/main` and `origin/<merge branch>`.
  Publication never uses force.


- One state directory: `eta_seen.json` and `review_watch.json` live in `WAITING_STATE_DIR` for every caller; a second directory forks the once-per-phase and first-alert records. The command document names this shared path.

- `then` reaches the renderer unchanged; the renderer refuses, it never rewrites.

- An activity on a closed phase leaves that phase's record as `finish-phase` wrote it: status, `finished_at` and elapsed time. The printed `review trial:` line keeps its form, which `merge_checkpoint.py` parses.

- The stall watch skips standing-by units, finished runs and retired rows before any bump or idle notice. A pause or external-owner wait always resets the stretch. Waiting kinds and future stated holds also reset the stretch. Only `COMPACTION_WAKE_KIND` (`blocked`) can enter the idle path at or above `context_usage.handoff_threshold(window)`. That path sends one bump and one showrunner tell per stretch. The watcher reads `context_near_compaction` only for a blocked turn. It uses the context hook's bounded transcript-tail reading and model-specific handoff threshold from `scripts/hooks/context_usage.py`. Missing or unreadable usage keeps the wait quiet. The turn-end pattern uses `WAITING_KINDS` and `HOLDING_KIND`, so the pattern and wait test use the same kinds. A one-line reply can have a `●` or `⏺` bullet before the turn end.

- **Outside waits.** The stall watch reads every active showrunner name from the pass's staff snapshot.
  It also reads the unit's sibling names, excluding the unit itself, once per parent.
  `PaneInspection.peers` carries those names with the configured session and production name.
  A holding or waiting status that names one of those outside parties prevents a bump.
  Complete own names still take precedence.
  Unrelated unit names are excluded because common names can hide the unit's own work.
  A common-word sibling name can still defer a bump.

- Unit directors launch only on Claude. `production` has only `[production.claude]`; `claude --resume` never runs without resolved flags.

- `models_config.sh` is bash only (call it through `bash -c`) and must keep working under `/bin/bash` 3.2 (no associative arrays, no `${var,,}`).

## Calibration and gotchas

- `config/models.conf` is shared by both machines.
  The [model registry](agent-registry.md#registry-publication) owns publication, concurrent edits, and catalog additions.

- A fresh launch or editor read can synchronize the Codex catalog before its registry snapshot.
  Sourcing `models_config.sh` only defines functions.
  The [configuration lifetime](agent-registry.md#configuration-lifetimes) keeps settings consistent across helper processes.
  Tests use private `MODELS_CONFIG_FILE`, `CODEX_CONFIG_FILE`, `CODEX_MODELS_CACHE_FILE`, and `CODEX_CATALOG_SYNC_STATE_FILE` paths.

- A re-run of `add_unit.py` must name its original mode; the mode comes from the commit subject, then the configured plan, then the stub.

- The remote `rc=` line identifies a Mac pull result.
  SSH exit 255 without that line identifies an unreachable Mac after the configured retries.
  Promotion queues that target and source, reports the pending pull, and continues.
  Other pull failures still fail promotion.
  The Mac pulls from `natedev:<first Promote checkout>`.

- `merge_checkpoint.py` accepts a Merge tests command only with status zero.

- CI point: due at 5 code merges; `ci collect` requires job `Test Suite` by name. The review table is `report --since 2026-09-28`.

- `review_watch.json` is written after the LOG line: a crash between them repeats the log entry once (at least once).


- The renderer exposes no list of required fields, which is why the builder names five itself.

- Agenda threshold is 8 hours; waits act at 60 minutes open or 30 minutes past the clear time; the weekday ETA window is ±3 days.

- `log_events` dates a date-less LOG line by walking backward across midnights; a gap over 24 hours between timestamped lines is invisible.

- `unit_status.sh` routes `gate:` exactly as `decision:` and `blocked:`. The stall watch keeps `done`, `gate` and `decision` silent at every context level. Only a blocked turn can receive a compaction wake. The status script still flags a gate to the user.

- `unit_status.sh` is zsh.

- `test_unit_status.py` copies `add_unit.py`, `live_units.py` and `showrunners.py` into its test tree, so a new import in `live_units.py` needs a new entry in that list.

- `waiting.py waits` rebuilds waits from the LOG alone, so filtering the doc's rows never reaches it; it filters on the waiting unit's name through `retired_units`.

- The stall watch reads units from the staff record. A unit's run is the run its staff `current_session` directs. `unit_runs.held()` lists that director's runs, with its marker first. Recorded director changes take priority over the original director. The watcher selects the first held run whose history is unfinished. A missing or finished run does not make the unit eligible. The watcher scans run folders once per pass and reads each folder's director once. It verifies the marker's director, including markers outside the scanned roots. It reads only held histories, once per history per pass. An unreadable held history makes that unit's state unknown. The watcher preserves its saved state and prints one stdout line with the unit and history path. Unheld histories cannot stop inspections. The watcher checks the inspection deadline before each run lookup and before changing saved state afterward. Units beyond that deadline keep their saved state. Staff and document worktree paths do not select the run. Pane, status and work checks use the current director's live session and pane, without a worktree lookup. The document still supplies production registration data. Showrunner discovery and unit lookup share one staff snapshot. Each attempted inspection prints one stdout line: `inspected <showrunner name>/<unit>: <decision>`. RUN mode writes this line to `~/.local/state/notifier/stall-watch/run.log`. The line never enters a message path.

- Idle: `IDLE_LIMIT` is 80 characters; `idle.until` is a naive local time in the report's zone, compared with the renderer's naive local now.

- Recorder: `_event` stamps the phase in state on every event, active or closed; nothing else carries an activity's phase. When the last phase is closed but an activity is open, `progress` prints the short report from that phase's last recorded values, clocks table included, and shows the activity as the stage line. The round table needs `--tables`. `No active phase to report: …` remains for a missing or closed phase with no open activity.

- The recorder other units run is the main checkout's copy under `~/.claude/scripts/direct/`; a change in a worktree reaches them, and their `review-trial` line, only when promoted.

- `showrunners.py` is mode 0644: call it through `scripts/lib/py`.

- `already at` in a promote line means main was at the tip before the run; no other line uses it.

- In an enrolled repository the shrink's first plan-doc write goes through the Edit tool; a write from a script is never claimed by the first-touch hook.

- Sandboxed workers have no writable temporary directory and cannot run the stall-watch suite; run it from the main tree.

## Why

## Session hosting

Unit lookup reads only herdr. `Place` is `HerdrPlace(session, pane)`;
`LabeledSession(unit, claude, place, process, identity)` exposes the server name as `place.session`.
`every_labeled_session()` raises `OSError` when herdr cannot be read, and
`labeled_sessions(name)` selects one production. A live Claude joins native identity evidence from
its exact PID. Other processes retain their panes as `ClaudeNotRunning` for recovery, resume, and status. Live or unknown
Claude holders take precedence over stopped panes; only those holders count
toward duplicate faults. With only stopped holders, the first pane is retained.
Duplicate live or unknown processes produce a fault with each PID, command, and pane.
Registry records join by PID through `herdr_processes` and `herdr_place_of`.
An unreadable directory preserves placement.
An unreadable registry preserves `ClaudeUnknown(reason)`.
A registered Claude without its messaging socket also remains unknown.

Units launch in herdr and restore through `add_unit.py --host-session`.
Resume models come from transcripts.
Native runtime ids select staff rows.
New launches carry no copied staff identity.
Promotion waits for Claude to exit before it starts the replacement.
Registration creates the showrunner update and the run-only stall-watch job.

### Rerun recovery (2026-10-29)

`showrunners.py attach` accepts the same parent again and repeats the routing notices. A different parent still requires detach. `ci_tick.sh` prints an error cell for a failed run read, reads the other runs, and returns nonzero after the table. `ensure_git_filters.sh` skips a driver only when clean, smudge and `required=true` all match.
