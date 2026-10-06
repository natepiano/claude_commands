# Showrunner enhancements: fewer steps to remember

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Scripts for the showrunner's mechanical steps, so a showrunner starts a unit and merges a checkpoint with one command each, and every other step it must remember gets a script-or-not verdict.

> **Production: build-followups** — unit `enh-showrunner-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-06:
- 14:0x PDT, on the steps a showrunner runs to start a unit: "okay - that's too many steps for a producer to have to remember - how can we automate this - and allow for both a situation where a doc is written and where one is NOT written but instead should be written by the unit director - and even a unit that just needs to sit there and be ready to go".
- 14:1x PDT: "right - everything that can be scripted should be scripted so the showrunner has the least amount of things to remember".
- 14:2x PDT: "move hook phase 7 to its own new unit called enh-showrunner - setting it up as you see fit".

Both phases were Phases 7 and 8 of stalls-unit's plan (`docs/plans/build-followups-fn-length-hook.md`). The showrunner moved Phase 8 here with Phase 7: both script the showrunner's own steps and share the production doc reader.

## Decisions (showrunner)

- **Gate G1:** Phase 4 starts after stalls-unit Phase 6 merges (ETA 16:15 PDT). That phase changes `produce.md`, `promote_unit.md`, `showrunners.py` and `stall_watch.py`, which Phase 4 builds on.
- **Split (unit director, 2026-10-06 14:5x PDT):** the command's own files touch none of those four, so Phase 1 runs now and ships `--plan` and `--brief`; standby and the hub-file edits are Phase 4, after G1; the checkpoint merge is Phase 5. Production rule "parallel by default" (`produce.md` → Rules, `ed166b6`).
- **Hub files:** `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, `scripts/production/showrunners.py` and `scripts/production/stall_watch.py` are stalls-unit's; this unit edits them after G1. Merge `build-followups` into the branch before each phase.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. This plan builds `/showrunner:add_unit` (Phases 1 and 4) and `merge_checkpoint.py` with the script-or-not audit (Phase 5). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner` on branch `build-followups-enh-showrunner` (unit `enh-showrunner-unit` of production `build-followups`).
- **Project started:** 2026-10-06T21:25:00+00:00
- **Stack:** Python 3.13, standard library only; zsh for command lines.
- **Layout:**
  - `scripts/production/` — production scripts and their `test_*.py`
  - `commands/showrunner/` — the showrunner commands
- **Key files:** `commands/showrunner/produce.md` (<LaunchUnits/>, <MergeCheckpoint/>), `commands/showrunner/promote_unit.md`, `docs/production_format.md`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py`, `scripts/production/unit_status.sh`, `scripts/production/review_regime.py`, `docs/plans/build-followups-production.md` (a real production doc to read, never write).
- **Port:** none.
- **Test lane:** `scripts/production/`.
- **Test:** `python3 -m unittest discover -s scripts/production -p 'test_*.py'`, from the worktree root.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`. It exits 3 in every checkout, so the exit status says nothing.
- **Invariants:**
  - Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - Times state PDT.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 4 | stalls-unit Phase 6 | the showrunner merges it and says so |

## Phases

### Phase 1 — One command starts a unit, with a plan or with a brief for its unit director to plan · status: done

#### As-built

`/showrunner:add_unit` runs `add_unit.py --production <doc> <name> (--plan <path> | --brief <words>) [--port N] [--owns <paths>] [--resume <sessionId> --cwd <dir>] [--timeout S]`. One run, restartable from any step: preflight; for a brief, a stub plan holding the user's words under `## Source`; the Units row; commit `production(<slug>): add unit <unit> (<plan|brief>)` and push of the merge branch; the worktree and branch, pushed with upstream; a tmux session under `systemd-run --user --scope` running `claude --remote-control <name>` with the unit's prompt; a wait for `/remote-control is active`; `showrunners.py add`; the unit added to an old-form `prompt.txt`; one LOG line. A re-run skips every step already done. `--resume` reattaches an existing session in its own cwd and tells it, with the plan's absolute path, that it is now the unit.

- The request is typed: `Production` (doc, slug, merge_branch, checkout, showrunner_session, log, zone), `PlanGiven | BriefGiven`, `NewSession | ResumedSession`, `UnitLaunch`.
- `read_production(path) -> Production` and `production_field(lines, field) -> str` are the production-doc reader.

**Files:**
- `scripts/production/add_unit.py` — the command.
- `scripts/production/test_add_unit.py` — subprocess tests with fake `claude`, `tmux`, `systemd-run` and `nix` on `PATH` and a temporary bare origin.
- `commands/showrunner/add_unit.md` — the skill.

**Binds later work:** the standby state joins `UnitLaunch.plan` beside `PlanGiven | BriefGiven`, and `recorded_mode` accepts its commit-subject mode; the merge-checkpoint script imports `read_production` and `production_field`.

**Gotchas:**
- tmux targets are `=<name>`; a plain `-t <name>` prefix-matches another session.
- A re-run must name the mode it was started with; the mode is read from the commit subject, then from the stub's `## Source`.
- Every refusal happens in `preflight`, before any write: another unit's branch or worktree in the Units table, an existing local or origin branch, an occupied worktree path, a live tmux session, a plan missing on the merge branch or lacking the Production header, a stub with different words.

**Ruled out:** a mode column in the Units table — the commit subject already records it.

### Phase 2 — Simple dailies name only the next item; page dailies list each upcoming item as a sub-bullet · status: done

#### As-built

A unit's `then` in the dailies input is a JSON list of one-line items, one per upcoming phase or follow-up, each led by its phase number when it has one (`76–79: …` for a range with one purpose); a plain string still reads as one item. `parse_upcoming_work` turns it into `UpcomingWork(items)` or `NoUpcomingWork()` on `Unit.upcoming_work`. `simple` prints `- then: <first item>`; `page` and `elaborate` print `- then:` with each item as `  - <item>`; a single item stays on the `- then:` line at every length.

- Each item passes `check_words` and `check_then_order`. A leading `<N>:` or `<N>–<M>:` label is this plan's phase and is checked even when the item names another plan's document.
- A follow-up needs one item naming its return (`the plan at Phase <N>` or `plan done`); a last phase needs the list.

**Files:**
- `scripts/production/dailies_render.py` — the list reader, `UpcomingWork` / `NoUpcomingWork`, the per-length render.
- `scripts/production/test_dailies_render.py` — the list, legacy-string, refusal, order and length cases.
- `commands/showrunner/dailies.md` — the list schema, its example and the per-item order rule.

**Gotchas:** `UpcomingWork.items` is non-empty through `parse_upcoming_work`, not by construction.

### Phase 3 — A unit commits its code before the shrink, and shrinks beside the next phase · status: done

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** the user, 2026-10-06 ~15:25 PDT, typed in this unit's session: "when you're done i want you to make it so that in the phase end process for a unit, that when they reach the phase of finished and the only thing they're doing is shrinking i want it to now do a commit first, then tell its showrunner the code is ready, then shrink and commit, then tell the showrunner that the phase is done. this way a showrunner can use the committed work and unblock other sessions more rapidly and the shrink can happen in parallel with work that starts on the next phase (shrink and commit just the docs while other work is happening)". The showrunner (natedev, 15:3x PDT) placed it here as Phase 3 and gave this unit `commands/unit/delegate.md`, `commands/unit/checkpoint.md`, `docs/delegate/run_phase_review.md` and `docs/production_format.md`. The showrunner's side (`commands/showrunner/produce.md` <MergeCheckpoint/>) is a hub file under G1 and goes to Phase 5.

**Goal:** in loop mode, a phase's code is committed, pushed and announced to the showrunner as soon as its code gates pass; the plan review then folds what was learned into the remaining Work Orders, the next phase's seats start, and the shrink runs beside them as a commit of the plan doc alone, announced with a short second notice.

**Spec:**
- **The new order** (`commands/unit/delegate.md` <ExecutionSteps/>), loop mode and inside an auto window: after <Synthesize/> converges and smoke and <UXReview/> pass —
  1. <CheckpointCommit/>: the phase's code, its tests and the plan doc as it stands, with the phase marked `status: done`; the reservation release, <PushCheckpoint/> and the `review-trial` line as today.
  2. The checkpoint notice (production_format item 3), unchanged; under a production its hash is mergeable at once. Outside a production, the one-line `Checkpoint <short hash> — phase N: <title>.` report.
  3. <RunPhaseReview/>: the retrospective, the state audit, the architect review when a trigger fires, and the forward edits into the remaining Work Orders. The next phase waits for it, because its Work Order may change.
  4. <ConsiderNextItems/>, then `finish-phase` for this phase.
  5. <NextPhase/>: the next phase's <ComposeWorkOrder/>, reservation and launch, as today.
  6. While the next phase's seats run: <RunPhaseShrink/> on this phase, under a `progress_history.py` activity labelled `shrink`; then the shrink commit; then the shrink notice; then this phase's `clear_phase_review.sh`. The turn then ends holding on the seats.
  When no phase remains, steps 5 and 6 swap: the shrink and its commit come first, then <FinalGate/>. Verbose mode outside a window runs steps 1–4, then 6 without a next phase, then the post-phase report and gate; nothing starts before `continue`. `single` never commits and is unchanged.
- **The shrink commit** (`commands/unit/checkpoint.md`, a new <ShrinkCommit/> beside <CheckpointCommit/>): stages only the plan doc and an approved `${NEXT_ITEMS_PATH}` change; refuses when any other path is staged; commits `shrink(<plan-slug>): phase N — <title>` with the session trailer; runs <PushCheckpoint/>. It owns no reservation and calls no release: in an enrolled repository the plan-doc edit is claimed on first touch by the next phase's reservation, like any other edit.
- **The commit kinds** (<CoreContract/>): one checkpoint and one shrink commit per completed phase, plus the final-gate and as-built commits; the shrink commit is the only one made while seats run, and it touches only the plan doc.
- **The two notices** (`docs/production_format.md` item 3): the checkpoint notice as today, sent before the plan review; then `From <unit>: phase <N> shrink <hash> — plan doc only.`, merged like any checkpoint whose only path is the unit's own plan doc, with no `review trial` or `design check` line. The checkpoint notice's `Phase <next> ETA` counts from the code commit.
- **Shrink placement** (`docs/delegate/run_phase_review.md`): <RunPhaseReview/> runs after the checkpoint notice and before the next phase's <ComposeWorkOrder/>; <RunPhaseShrink/> runs after the next phase's launch and before the shrink commit. Its rule that remaining `todo` phases keep the plan review's forward edits holds; the phase the seats are running is one of them.
- **One doc holds the order** (the user, ~15:50 PDT, via the showrunner: "if 3 files share the same insructions, shouldn't those instructions be in their own file, reerenced by the skill? we have precedent for this"; the showrunner: write it once in a new doc). `docs/delegate/phase_end.md` defines <PhaseEnd/>: the code commit and its "code ready" notice, the plan review, the add-on check, worker cleanup and `finish-phase`, the next launch, the shrink beside the seats with its plan-doc-only commit and notice, the no-next-phase and verbose variants, and the routes below. `commands/unit/delegate.md`, `commands/unit/checkpoint.md`, `commands/unit/add_ons.md`, `commands/plan/shrink.md`, `commands/plan/phase_review.md`, `docs/delegate/run_phase_review.md` and `docs/production_format.md` each point to it in one line and keep only their own steps. Precedents: `docs/decision_criteria.md` and `docs/production_format.md`.
- **Routes after the code commit** (the showrunner approved, ~16:10 PDT): a defect the plan review finds in the committed phase becomes a follow-up phase inserted next and run next; source-comment edits the review makes go into the next phase's checkpoint commit.
- **Compaction and failure** (<CompactionContract/>, <RetainDelegatedPhaseReservation/>): a handoff written while a shrink is pending names that phase. A shrink that fails its structural check blocks only the shrink commit; the seats keep running, and the unit director repairs the shrink before the next checkpoint, which refuses while an earlier phase still has a Work Order.

**Files:**
- `commands/unit/delegate.md` — <ExecutionSteps/>, <CoreContract/>, <NextPhase/>, <PhaseCleanup/>, <RecordPhaseCompletion/>, <CompactionContract/>.
- `commands/unit/checkpoint.md` — <ShrinkCommit/>; <CheckpointCommit/> step 2 and step 8's report.
- `docs/delegate/run_phase_review.md` — where the review and the shrink run.
- `docs/production_format.md` — item 3's two notices.
- `docs/delegate/phase_end.md` — new; <PhaseEnd/>.
- `commands/unit/add_ons.md` — <ConsiderNextItems/> runs after the plan review and before the shrink.
- `commands/plan/shrink.md` — the shrink follows the code commit; plus the four wording swaps from the live checkout's uncommitted edit.
- `commands/plan/phase_review.md` — the review follows the code commit; the two routes above.

**Seats:** 2 writers.
- `impl` — `commands/unit/delegate.md`; post `done` without waiting for the other seat.
- `test` — opens as a writer: `commands/unit/checkpoint.md`, `docs/delegate/run_phase_review.md`, `docs/production_format.md`; agree each tag name with `impl` by message before writing it, and post it on the board.

**Constraints from prior phases:**
- Phase 5 (todo) teaches `merge_checkpoint.py` and <MergeCheckpoint/> the shrink notice; until it lands, the showrunner merges a shrink commit through the ordinary steps, which already pass a change whose only path is the unit's plan doc.
- Every tag a call site names keeps a definition; a contract moved between files keeps its stub row in <TagReferenceContract/>.

**Acceptance gate:**
- `grep -n "ShrinkCommit\|shrink(" commands/unit/delegate.md commands/unit/checkpoint.md docs/delegate/run_phase_review.md docs/production_format.md` shows the contract, its call sites and the commit kind; every `<Tag/>` the four files name has one definition (a script listing names against `<Tag>` openings, in the summary).
- Live: this unit's next completed phase runs the new order, and its two notices reach natedev.

### Phase 4 — A unit can wait on standby, and promote and produce use the command · status: todo

**Blocked by:** G1 — stalls-unit phase 6 merged into `build-followups`

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** the user, 2026-10-06 14:0x PDT, through the showrunner (natedev): "... and even a unit that just needs to sit there and be ready to go". The rest of the original Phase 1, split out by the unit director 2026-10-06 14:5x PDT because it edits the four hub files stalls-unit Phase 6 changes.

**Goal:** `/showrunner:add_unit <name> --standby` starts a unit that does nothing until the showrunner sends it work, and the stall watcher never bumps it while it waits. `promote_unit.md` uses `add_unit.py --resume` for its launch and record steps, and `produce.md` <LaunchUnits/> shrinks to the command.

**Spec:**
- **Standby mode:** `add_unit.py` and `add_unit.md` gain `--standby`, a third named state `Standby` beside `PlanGiven` and `BriefGiven`; exactly one of the three. It writes no plan; the row's Plan cell reads `standby`; the commit subject ends `(standby)`; the prompt is `You are <name>-unit in production <slug> (doc <path>), under the showrunner <session>, on standby. Work only in your worktree <worktree>, branch <branch>. Do nothing until the showrunner sends you work.`; step 7 passes `--standby` to `showrunners.py add`; the LOG line says `standby`.
- **The registry:** `showrunners.py add` gains `--standby`, which records the unit in that showrunner's `standby` list; `showrunners.py ready <session> --unit <name>` takes it out, and the showrunner runs it when it hands the unit work, changing the Units row's Plan cell from `standby` to the plan. The config reader converts each unit into a named state (working or standby) at the boundary. `stall_watch.py` never bumps or reports a standby unit.
- **Promote:** `promote_unit.md` keeps steps 1, 2 and 5 (find it, check fit, stop it) and replaces steps 3, 4, 6 and 7 with one call of `add_unit.py --resume <sessionId> --cwd <cwd> --plan <plan>`.
- **Produce:** <LaunchUnits/> becomes the command, one line per unit to start, plus what stays the showrunner's own: typing `/compact` into a unit director blocked on a full context, and **Resume** (step 4). <StartRun/>'s worktree and launch steps point to it.

**Files:**
- `commands/showrunner/add_unit.md` — `--standby`.
- `scripts/production/add_unit.py` — the `Standby` state; `scripts/production/test_add_unit.py`.
- `scripts/production/showrunners.py` — `--standby` and `ready`; `scripts/production/test_showrunners.py`.
- `scripts/production/stall_watch.py` — skips a standby unit; `scripts/production/test_stall_watch.py`.
- `commands/showrunner/produce.md` — <LaunchUnits/> shrinks to the command.
- `commands/showrunner/promote_unit.md` — steps 3, 4, 6 and 7 call the script.

**Seats:** 1 writer + 1 tester.
- `impl` — `commands/showrunner/add_unit.md`, `scripts/production/add_unit.py`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py`, `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`; post `done` without waiting for the test seat.
- `test` — the standby cases in `scripts/production/test_add_unit.py`, `scripts/production/test_showrunners.py` and `scripts/production/test_stall_watch.py`, from the Spec alone; owns the final suite run:
  - `--standby`: the row's Plan cell reads `standby`, the config lists the unit as standby, no plan is written, the prompt is the standby one, and the stall watcher skips the unit while it stays idle; after `ready` it is bumped as any unit;
  - `--standby` with `--plan` or `--brief` exits 2 with no change;
  - `showrunners.py ready` on a unit that is not standby changes nothing and says so.

**Constraints from prior phases:**
- Phase 1 (as built): `add_unit.py` and its typed launch request; add the state, never a parallel path. `UnitLaunch.plan` is `PlanGiven | BriefGiven` — `Standby` joins that union; `recorded_mode` reads the mode from the commit subject `production(<slug>): add unit <unit> (<mode>)` and must accept `standby`; `prompt_for` gains the standby prompt; every refusal stays in `preflight`, before any write; tmux targets are `=<name>`.
- stalls-unit Phase 5 (as built): every edit of `config/showrunners.json` goes through `showrunners.py`'s locked write (`change()`), and `stall_watch.py` reads units from it.
- stalls-unit Phase 6 (as built at G1; it also makes `stall_watch.py` find a unit whose pane process is `claude` itself): `showrunners.py rename` and `import` of both prompt forms, `unit_status.sh … --showrunner <session>`, the `tmux-names` instance; build on them, never a parallel copy.
- Tests never start a real `claude`, `tmux` or `systemd-run`, never write the real `~/.claude/config/showrunners.json`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_add_unit.py'`, `-p 'test_showrunners.py'` and `-p 'test_stall_watch.py'` green; basedpyright 0 errors and 0 warnings on the changed `.py` files.
- Live (natedev, once the merge reaches `~/.claude` main): `/showrunner:add_unit add-scratch --standby` prints the attach line, the pane shows remote control active, `showrunners.py list` shows `add-scratch` standby, and the stall watcher does not bump it over 6 idle minutes. Then remove it as in Phase 1's live gate.

### Phase 5 — One command merges a checkpoint, and every other showrunner step gets a script-or-not verdict · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** the user, 2026-10-06 14:1x PDT, through the showrunner (natedev): "right - everything that can be scripted should be scripted so the showrunner has the least amount of things to remember". The script, its steps and the audit are the showrunner's packaging of that ask, placed in this plan as its merge-checkpoint phase.

**Goal:** `merge_checkpoint.py --production <doc> <unit> <phase> <hash> [--also <path>…]` runs `produce.md` <MergeCheckpoint/>'s mechanical steps in one run, prints one result line per step and the message to send the unit, and on red leaves the merge branch as it was; <MergeCheckpoint/> keeps only the judgment steps and the call. The As-built lists every other step in `produce.md` and `commands/showrunner/dailies.md` the showrunner must remember, each with a script-or-not verdict, and each clear one becomes a follow-up phase.

**Spec:**
- **Reads** from the production doc what <MergeCheckpoint/> reads: `MERGE_BRANCH`, `CHECKOUT`, `LOG`, `ZONE`, the unit's Units row (branch, Owns), the hub-file rows, **Merge tests**, and the production rules that decide the push. `LAST_MERGED[unit]` comes from the merge subjects on `MERGE_BRANCH` (`Merge <unit> phase <N> (<short>) into <merge branch>`), never from an argument. Two optional doc fields make the push explicit: **Push:** `validate_and_push` (the default, <MergeCheckpoint/> step 10) or `git`; **Promote:** `<checkout>[, mac <path>]` (after a push, fast-forward that checkout's `main` and pull on the Mac). This production's doc carries `**Push:** git` and `**Promote:** ~/.claude, mac ~/.claude` (the showrunner, 2026-10-06). **Known flakes:** names packages whose red-then-green-alone run counts green.
- **Steps,** each printing `<step>: ok|held|failed — <one line>`; the first `held` or `failed` stops the run:
  1. ancestry (`cat-file -e`; `merge-base --is-ancestor LAST_MERGED <hash>`, skipped on a unit's first merge) and on-origin (`fetch origin <branch>`, `--is-ancestor <hash> origin/<branch>`; failing prints the message asking the unit to push);
  2. scope: every path of `diff --name-only <merge branch>...<hash>` is in Owns, a hub row of this unit, or an `--also` path; anything else is `held` with the paths and the message asking the unit why;
  3. conflicts: `merge-tree --write-tree --name-only` exit 1 is `held` with the message asking the unit to merge the merge branch and send a new hash;
  4. other units (data only, never a stop): for each other unit, the overlap of this change's paths with its branch diff and its worktree's `status --short`, printed so the showrunner applies <CrossUnitChange/> or the one-line notice;
  5. merge: the message file `<scratch>/merge_<short>.msg` as <MergeCheckpoint/> step 7 writes it, with this session's attribution lines passed by `--trailer`, then `merge --no-ff -q -F`;
  6. test: each package owning a changed file (nearest `Cargo.toml`) through `verify.sh test`, each changed example through `verify.sh example`, then each **Merge tests** command, one after another into `<scratch>/merge_<short>_test.log` with a `<name>_EXIT=<rc>` line each;
  7. red: each red package rerun once alone; green alone and listed in **Known flakes** continues with a LOG note; otherwise `reset --keep HEAD~1` after checking `HEAD` is this unpushed merge, and print the message with the failing tests and the log path;
  8. green, `Push: git`: merge `origin/main` with `--no-ff` when it has diverged, push `MERGE_BRANCH`, then each **Promote**: `git -C <checkout> merge --ff-only <merge hash>` (other sessions' uncommitted files left alone; a refusal is `failed` naming the paths, nothing undone), push main only when `origin/main..main` held no other session's commits, then the Mac's `git pull --ff-only` over `ssh mac`, read by the `rc=` it prints (ssh's own status is always 0); `Push: validate_and_push`: <MergeCheckpoint/> step 10's command and its undo;
  9. record: `review_regime.py add` with the numbers from `--review-trial "<the notice's line>"`, `--holds` and `--merge-defects` (default 0), `--started`, and the merge time; then the LOG line `- HH:MM <zone>: <unit> phase <N> (<hash>) merged as <merge hash>; <tests> green; pushed[; promoted]`.
- The last line printed is `send <unit>: <message>`: the merged line with the next step, or the hold or failure and what the unit does.
- `commands/showrunner/produce.md` <MergeCheckpoint/>: steps 1–3 and 7–10 and the record step become the call, run in the background; steps 4–6 (other units, new public items, design check), <ClearGate/>, <CrossUnitChange/>, <CIPoint/> and `review_regime.py watch` stay the showrunner's, read from the script's lines.
- **The audit:** every other step in `produce.md` and `commands/showrunner/dailies.md` the showrunner must remember — building the dailies input from `unit_status.sh`, the idle check, promotion and the Mac pull included — one row each: the step, where it lives, `script` or `judgment`, and why. Written into this phase's As-built; each `script` row the unit director turns into a follow-up phase with its own Work Order.

**Files:**
- `scripts/production/merge_checkpoint.py` — new.
- `scripts/production/test_merge_checkpoint.py` — new.
- `commands/showrunner/produce.md` — <MergeCheckpoint/> calls the script.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/production/merge_checkpoint.py`, `commands/showrunner/produce.md`; post `done` without waiting for the test seat.
- `test` — `scripts/production/test_merge_checkpoint.py` from the Spec alone, with real `git` in temporary repositories and a temporary bare `origin`, stubs on `PATH` for `verify.sh`, `validate_and_push.sh`, `review_regime.py` and `ssh` recording argv: each step's `held` and `failed` cases with no merge left behind; a red package reset and a known flake continuing; the green `git` path merging diverged `origin/main`, pushing, promoting a second temporary checkout by fast-forward and reading the Mac's `rc=`; a promote refused by an uncommitted file fails without undoing the push; the record call's argv and the LOG line. Owns the final suite run. Then the audit table, in its summary.

**Constraints from prior phases:**
- A shrink notice (`From <unit>: phase <N> shrink <hash> — plan doc only.`, or `— plan doc and <next-items path>.`) merges with no review-ledger row and no CI count (the showrunner, 16:2x PDT, doing it by hand until this lands).
- Phase 1 (as built): `add_unit.py` reads the production doc with `read_production(path) -> Production` (doc, slug, merge_branch, checkout, showrunner_session, log, zone) and `production_field(lines, field) -> str`; import them, never a second parser.
- Phase 4 (as built at its merge): it edits `produce.md` <LaunchUnits/>; this phase edits <MergeCheckpoint/>. Starts beside Phase 4 once Phase 1 merges, claiming `produce.md` after it.
- Tests never push anywhere but a temporary bare repository, never run real `ssh`, and never write the real `~/.claude` checkout or `~/.local/state/`.
- Only the showrunner merges (production_format item 4): the live gate is natedev's.
- Phase 3 (as built at its merge): a unit sends a second notice, `From <unit>: phase <N> shrink <hash> — plan doc only.`, for a commit whose only path is its plan doc. `merge_checkpoint.py` takes it with `--shrink`: ancestry, scope (the plan doc alone, else `held`), conflicts, merge and push, with no package tests and no `review_regime.py add`; <MergeCheckpoint/> names the call. Runs after Phase 3.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_merge_checkpoint.py'` green; basedpyright 0 errors and 0 warnings on the changed `.py` files.
- Live (natedev, once the merge reaches `~/.claude` main): the next checkpoint any unit sends is merged with the script; its lines match what the showrunner would have done by hand, and the As-built carries the audit table.
