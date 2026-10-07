# Showrunner enhancements: fewer steps to remember

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Scripts for the showrunner's mechanical steps, so a showrunner starts a unit and merges a checkpoint with one command each, and every other step it must remember gets a script-or-not verdict.

> **Production: build-followups** — unit `enh-showrunner-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-06:
- 14:0x PDT, on the steps a showrunner runs to start a unit: "okay - that's too many steps for a producer to have to remember - how can we automate this - and allow for both a situation where a doc is written and where one is NOT written but instead should be written by the unit director - and even a unit that just needs to sit there and be ready to go".
- 14:1x PDT: "right - everything that can be scripted should be scripted so the showrunner has the least amount of things to remember".
- 14:2x PDT: "move hook phase 7 to its own new unit called enh-showrunner - setting it up as you see fit".

Both phases were Phases 7 and 8 of stalls-unit's plan (`docs/as-built/build-followups-fn-length-hook.md`). The showrunner moved Phase 8 here with Phase 7: both script the showrunner's own steps and share the production doc reader.

## Decisions (showrunner)

- **Gate G1:** Phase 4 starts after stalls-unit Phase 6 merges (ETA 16:15 PDT). That phase changes `produce.md`, `promote_unit.md`, `showrunners.py` and `stall_watch.py`, which Phase 4 builds on.
- **Split (unit director, 2026-10-06 14:5x PDT):** the command's own files touch none of those four, so Phase 1 runs now and ships `--plan` and `--brief`; standby and the hub-file edits are Phase 4, after G1; the checkpoint merge is Phase 5. Production rule "parallel by default" (`produce.md` → Rules, `ed166b6`).
- **Hub files:** `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, `scripts/production/showrunners.py` and `scripts/production/stall_watch.py` are stalls-unit's; this unit edits them after G1. Merge `build-followups` into the branch before each phase. The production doc's Owns row lists all four for this unit, so none is an `--also` path. `commands/showrunner/dailies.md` and `scripts/production/dailies_render.py` are stalls-unit's files that this unit's row does not list: a phase that edits one names it as `also touches <path> (owner stalls-unit), tested against <owner tip>`.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. This plan builds `/showrunner:add_unit` (Phases 1 and 4) and `merge_checkpoint.py` with the script-or-not audit (Phase 5). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner` on branch `build-followups-enh-showrunner` (unit `enh-showrunner-unit` of production `build-followups`).
- **Project started:** 2026-10-06T21:25:00+00:00
- **Stack:** Python 3.13, standard library only; zsh for command lines.
- **Layout:**
  - `scripts/production/` — production scripts and their `test_*.py`
  - `commands/showrunner/` — the showrunner commands
- **Key files:** `commands/showrunner/produce.md` (<LaunchUnits/>, <MergeCheckpoint/>), `commands/showrunner/promote_unit.md`, `docs/production_format.md`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py`, `scripts/production/unit_status.sh`, `scripts/production/review_regime.py`, `docs/plans/build-followups-production.md` (a real production doc to read, never write).
- **Port:** none.
- **Test lane:** `scripts/production/`; Phase 7's tester also runs the shell suite `scripts/agents/test_agents_config.sh`.
- **Test:** `python3 -m unittest discover -s scripts/production -p 'test_*.py'`, from the worktree root.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`. It exits 3 in every checkout, so the exit status says nothing.
- **Invariants:**
  - Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - Times state PDT.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 4 | stalls-unit Phase 6 | the showrunner merges it and says so — cleared: `966b814` is on `build-followups` |

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

#### As-built

`docs/delegate/phase_end.md` defines <PhaseEnd/>, the one order a loop or verbose phase ends in: the code commit (<CheckpointCommit/>, with the phase `status: done` and its Work Order still in place), the code-ready notice, the plan review, the add-on check, worker cleanup and `finish-phase`, the next phase's launch, then the shrink beside the new seats, its own commit and a second notice. The other phase-end docs point to it in one line and keep only their own steps.

- <ShrinkCommit/> (`commands/unit/checkpoint.md`) stages only the plan doc and an approved next-items file, refuses any other staged path, commits `shrink(<plan-slug>): phase N — <title>`, runs <PushCheckpoint/>, and owns no reservation.
- The shrink notice is `From <unit>: phase <N> shrink <hash> — plan doc only.`, or `— plan doc and <next-items path>.`; the showrunner merges it with no review-ledger row and no CI count.
- Variants: no next phase runs the shrink, then <FinalGate/>; a due <PeriodicCI/> point outside a production runs the shrink before the launch, so CI sees a clean tree; verbose outside a window shrinks, then reports and gates.
- A defect the plan review finds in the committed phase becomes a follow-up phase run next; the review's source-comment edits go into the next phase's checkpoint, or the final-gate commit when no phase is left.
- A failed shrink blocks only <ShrinkCommit/>; the next checkpoint refuses while an earlier `done` phase still has a Work Order.

**Files:**
- `docs/delegate/phase_end.md` — <PhaseEnd/>.
- `commands/unit/delegate.md` — the <PhaseEnd/> stub and row; loop and verbose <ExecutionSteps/> call it.
- `commands/unit/checkpoint.md` — <ShrinkCommit/>; <CheckpointCommit/> step 2's accepted paths and refusal.
- `docs/production_format.md` — item 3's two notices.
- `docs/delegate/run_phase_review.md`, `commands/unit/add_ons.md`, `commands/plan/shrink.md`, `commands/plan/phase_review.md`, `docs/delegate_plan_format.md`, `commands/unit/eta_breakdown.md`, `docs/delegate/final_gate_commit.md` — the pointer line and their own steps in the new order.

**Binds later work:** the showrunner's merge script takes a shrink notice with `--shrink`: plan doc and next-items file only, after the same phase's code checkpoint, no package tests and no review-ledger row.

**Gotchas:** in an enrolled repository the shrink's first plan-doc write goes through the Edit tool; a write from a script is never claimed by the first-touch hook.

**Ruled out:** teaching `produce.md` the shrink notice here — it is the showrunner's file, and the merge-checkpoint phase owns it.

### Phase 4 — A unit can wait on standby, and promote and produce use the command · status: done

#### As-built

- `add_unit.py --standby` is a third named launch state, `Standby` beside `PlanGiven` and `BriefGiven` in `UnitLaunch.plan`; exactly one of the three, else exit 2 with no change. It writes no plan, the Units row's Plan cell reads `standby`, the commit subject ends `(standby)` (`recorded_mode` accepts it), the prompt tells the unit to do nothing until the showrunner sends work, and `showrunners.py add --standby` records it.
- `showrunners.py` keeps a per-showrunner `standby` list; the config reader turns each unit into `WorkingUnit | StandbyUnit` (`UnitState`) at the boundary. `showrunners.py ready <session> --unit <name>` takes a unit off standby (one not on standby: prints `<unit> is not on standby`, exit 0, no change); `list` marks `<unit>:standby`. The showrunner runs `ready` when it hands the unit work and changes the Plan cell from `standby` to the plan.
- `stall_watch.py` skips a standby unit before any bump or notice, and a last turn line starting `done:` resets the stretch and skips the unit.
- Units rows are read cell by cell: `cell_value(value: str) -> str` (first backticked span, else text before ` — `, shared with `production_field`) and `unit_rows(lines: list[str]) -> tuple[int, list[str]]` (insert index, data rows). A unit's existing row is adopted (`NoUnitRow | ExistingUnitRow`) when Plan, Worktree, Branch and Session agree; its Port and Owns stand, and `--port`/`--owns` (`OmittedCell | SuppliedCell`) must match them.
- `add_unit.py --check` runs `launch_request` and `preflight` only: exit 0 or 2, nothing written or started.
- `promote_unit.md` order: find, check fit (normalized unit name), plan, `add_unit.py --check`, stop, launch and record (`add_unit.py --resume <sessionId> --cwd <cwd> --plan <plan>`), tell the user (both names when the name changed). `produce.md` <LaunchUnits/> runs `add_unit.py --production PRODUCTION_DOC <name> --plan <unit plan>` per unit (`--standby` for one waiting on work), with `<name>` the Unit value without `-unit`; typing `/compact` into a blocked unit director and **Resume** stay the showrunner's own.

**Files:**
- `scripts/production/add_unit.py` — `Standby` state, row adoption, `cell_value`/`unit_rows`, `--check`; `test_add_unit.py`.
- `scripts/production/showrunners.py` — standby registry, `WorkingUnit | StandbyUnit`, `add --standby`, `ready`; `test_showrunners.py`.
- `scripts/production/stall_watch.py` — skips standby, resets on `done:`; `test_stall_watch.py`.
- `commands/showrunner/add_unit.md` — `--standby`; `promote_unit.md` — the order above; `produce.md` — <LaunchUnits/> as the command.

**Binds later work:** the checkpoint-merge command reads a unit's Units row (Branch, Owns) only through `cell_value` and `unit_rows`; stall-watch changes keep the standby skip and the `done:` reset.

**Gotchas:**
- Every row of a real production doc is backticked and carries commentary; empty-table tests do not show it.
- produce.md has no <StartRun/>; launch lives in <LaunchUnits/>.
- `--check` does no tmux lookup: a tmux name collision can still refuse after the stop.

**Ruled out:** rewriting a pre-filled Units row to the script's own form.

### Phase 5 — One command merges a checkpoint, and every other showrunner step gets a script-or-not verdict · status: done

#### As-built

`scripts/production/merge_checkpoint.py --production <doc> <unit> <phase> <hash>` runs a checkpoint merge's mechanical steps in one run. Each step prints `<step>: ok|held|failed — <one line>`; the first `held` or `failed` stops the run, and on red the merge branch is left as it was. The last line printed is `send <unit>: <message>`, the merged line with the next step, or the hold or failure and what the unit does. The script reads the production doc through `add_unit.py`'s `read_production`, `production_field`, `cell_value` and `unit_rows`.

**Arguments.** `--also <path>` (repeatable) adds a scope path; `--shrink` takes a plan-doc-only notice; `--delivers "<line>"` is the merge message's body (required for a code checkpoint, refused for a shrink); `--cancel-prior` (`ValidateAndPush` only) passes through as the push's conditional; `--trailer <line>` (repeatable) adds attribution lines to the merge message; `--scratch <dir>` holds the message file and test log. For the review ledger: `--review-trial "<the notice's line>"` (required for a code checkpoint) yields `--ux-findings`, `--code-findings`, `--review-minutes`, `--ux-check-minutes` and `--ux-repair-minutes`; `--started <ISO>` is required; `--regime after|trial` (default `after`); `--holds` and `--merge-defects` (default 0); `--excluded "<why>"` passes through. The merge time is the merge commit's.

**Reads from the production doc:** `MERGE_BRANCH`, `CHECKOUT`, `LOG`, `ZONE`, the unit's Units row (Branch, Owns), the hub-file rows, **Merge tests**, **Known flakes** (packages whose red-then-green-alone run counts green), and two optional fields, **Push:** `validate_and_push` (default) or `git`, and **Promote:** `<checkout>[, mac <path>]` (this production carries `**Push:** git` and `**Promote:** ~/.claude, mac ~/.claude`). `LAST_MERGED[unit]` comes from the merge subjects on `MERGE_BRANCH` (`Merge <unit> phase <N> (<short>) into <merge branch>`), never from an argument.

**Named states at the reader boundary:** an optional field is `FieldPresent | FieldAbsent`; the push is `ValidateAndPush | GitPush`; the merge branch's tip on origin is `PushedTip | NotYetPushed`; the promotion is `NoPromotion | PromoteTo(<checkouts>, MacCheckout | NoMac)`; the unit's history is `FirstMerge | LastMerged(<hash>)`; the kind is `CodeCheckpoint | ShrinkCommit`; the review line is `ReviewTrial | NoReviewTrial`. `production_field` raises on a missing field, so no `str | None` crosses the boundary.

**Owns reading rule:** a bare backticked name in an Owns cell owns the root file and the sibling of the previous path (the production's rows use both). **Merge tests** are the backticked spans before the first plain `(` in the field; a missing field is refused, an empty one means no commands. The real production doc's Owns cell, Merge tests line and Units rows are pinned as verbatim test fixtures.

**The nine steps:**
1. ancestry and on-origin: `cat-file -e`; `merge-base --is-ancestor LAST_MERGED <hash>` (skipped on `FirstMerge`); `fetch origin <branch>` and `--is-ancestor <hash> origin/<branch>`, whose failure prints the message asking the unit to push. A hash already on `MERGE_BRANCH` (a re-run after a failed promote) skips steps 2–7 and resumes at the first step-8 destination not yet reached.
2. scope: every path of `diff --name-only <merge branch>...<hash>` is in Owns, a hub row of this unit, or an `--also` path; otherwise `held` with the paths and the message asking the unit why.
3. conflicts: `merge-tree --write-tree --name-only` exit 1 is `held`, with the message asking the unit to merge the merge branch and send a new hash.
4. other units (data only, never a stop): the overlap of this change's paths with each other unit's branch diff and its worktree's `status --short`.
5. merge: the message file `<scratch>/merge_<short>.msg`, then `merge --no-ff -q -F`.
6. test: each package owning a changed file (nearest `Cargo.toml`) through `verify.sh test`, each changed example through `verify.sh example`, then each **Merge tests** command, into `<scratch>/merge_<short>_test.log` with a `<name>_EXIT=<rc>` line each. A test command that cannot start is a red step with exit 127, never an exception after the merge.
7. red: each red package reruns once alone; green alone and listed in **Known flakes** continues with a LOG note; otherwise `reset --keep HEAD~1` after checking `HEAD` is this unpushed merge, and the message names the failing tests and the log path.
8. push and promote. `GitPush`: merge `origin/main` with `--no-ff` when it has diverged, push `MERGE_BRANCH`, then for each **Promote** run `git -C <checkout> merge --ff-only <the tip pushed to MERGE_BRANCH>` (the tip after any `origin/main` merge, never the earlier checkpoint merge); other sessions' uncommitted files are left alone, and a refusal is `failed` naming the paths with nothing undone. Push `main` only when `origin/main..main` held no other session's commits, then the Mac's `git pull --ff-only` over `ssh mac`, read by the `rc=` it prints. `ValidateAndPush`: `validate_and_push.sh` and its undo.
9. record: `review_regime.py add --unit --phase --regime --started --merged --holds --merge-defects` plus the five review numbers and any `--excluded`; then the LOG line `- HH:MM <zone>: <unit> phase <N> (<hash>) merged as <merge hash>; <tests> green; pushed[; promoted]`.

**Shrink notice** (`From <unit>: phase <N> shrink <hash> — plan doc only.`, or `— plan doc and <next-items path>.`): `--shrink` runs ancestry, scope (the plan doc and, when present, the unit's `<plan stem>-next.md`; `held` when another path is staged), `held` until the same phase's code checkpoint is on `MERGE_BRANCH` (its `Merge <unit> phase <N> (` subject), conflicts, merge and push. It runs no package tests and no `review_regime.py add`, and clears no gate.

**The merge report.** As step 8 reaches each place the merge went, the script prints `into: <path> (<branch>) — <why>; unblocks: <what>`, so a run that fails after the push still names every place reached:
- the merge branch's checkout: `the merge branch collects every unit's checkpoints`; unblocks each gate in the production doc's **Gates** table whose **Waits on** is this unit's phase (`<gate id>: <unit> Phase <N>`), else `no gate`. Only a code checkpoint's first merge clears a gate; a shrink and a re-run print `no gate`.
- each **Promote** checkout, the Mac included: `sessions on <machine> run the installed commands from it`; unblocks `the live gate of <unit> phase <N>`.

`commands/showrunner/produce.md` <MergeCheckpoint/> calls the script (the call runs detached) and keeps the judgment steps: other units' overlap notice, new public items, the design check, <ClearGate/>, <CrossUnitChange/>, <CIPoint/> and `review_regime.py watch`, all read from the script's lines. Its report to the user carries the `into:` lines plus one line in the same form for each unit worktree the showrunner then merges the merge branch into. Both footer rules in `produce.md` say that footers off drops the Waiting on block too.

`scripts/production/stall_watch.py` sends no bump and no showrunner notice while the last turn-end line on a unit's pane is `— blocked: …`; it treats that line as it treats `done:`, a fresh stretch, so the idle clock restarts at the next status that is not a block. A `— holding: …` line with nothing running is still bumped. It skips a unit whose Units row Plan cell reads `run done`, as it skips a standby unit: no bump, no notice, no stretch kept. The Plan cell is read from the production doc through the notifier conf's `CHECK=` path, and the row's Unit cell and its Session cell both name the finished unit, because the notifier conf names the Session cell.

`commands/unit/delegate.md` <ProgressContract/> names `/unit:report off` to stop a Claude unit's updates and `/unit:report on` to resume them (it runs `unit_notifier.sh "$CLAUDE_CODE_SESSION_ID" on|off`); Codex keeps its sentence.

**Script-or-judgment audit.** Every other step the showrunner must remember in `produce.md` and `commands/showrunner/dailies.md`. `script` is a clear automation candidate, with the title of the phase that owns it; `script (built)` exists today; `judgment` stays with the showrunner.

| Step | Where | Verdict | Why |
| --- | --- | --- | --- |
| Zone time and conversion in every update | produce Throughout | `script` | read the doc zone, format stamps → Update registration |
| Event LOG lines and periodic STATE snapshots | produce Throughout | `script` | append and ten-event trigger are mechanical → Update registration |
| Relay the user's exact words to a unit | produce Throughout | `judgment` | provenance and authorization need a person |
| Verify a unit claim about a hash, tests or shots | produce Throughout | `judgment` | evidence needs review; hash check is in the merge script |
| Request an unmeasured ETA once per phase | produce Throughout | `script` | detect missing ETA, dedupe by phase → Waiting, cross-unit search and alerts |
| Footer and outstanding-item rendering, off switch | produce Throughout | `script` | renderer exists; match "off drops Waiting on" → Update registration |
| Check checkout branch and load production state | produce LoadProduction | `script` | branch preflight, last STATE, session liveness → Production open and wrap |
| Recover each unit's last merged checkpoint | produce LoadProduction | `script (built)` | merge script reads it from merge subjects |
| Create merge branch, set running doc, commit plans, push | produce OpenMergeBranch | `script` | fixed git and doc transaction → Production open and wrap |
| Exclude and initialize LOG | produce OpenMergeBranch | `script` | fixed git-info and log operations → Production open and wrap |
| Launch or resume a unit director | produce LaunchUnits | `script (built)` | `add_unit.py` covers launch; no resume wrapper |
| Decide whether a blocked pane is safe to compact | produce LaunchUnits, StartUpdates | `judgment` | pane prompts and in-flight forms need careful reading |
| Register session, prompt, notifier, stall-watch, tmux-names jobs | produce StartUpdates | `script` | inputs in doc and registry; idempotent → Update registration |
| Build the scheduled prompt and report next_due | produce StartUpdates | `script` | template expansion, notifier status → Update registration |
| Route arrivals and serialize the checkpoint queue | produce Direct | `script` | classify notices, order queue; priority stays judgment → CI points and review watch |
| Compare other-unit branch and worktree paths | produce MergeCheckpoint 1 | `script (built)` | merge script prints overlap data |
| Decide overlap notice, owner tip or restructure hold | produce MergeCheckpoint 1, CrossUnitChange | `judgment` | impact and fix owner need interpretation |
| Decide whether each new public item has a consumer | produce MergeCheckpoint 2 | `judgment` | whether a use is meaningful is semantic |
| Judge design-check freshness and visible defects | produce MergeCheckpoint 3, DesignCheck | `judgment` | shots, intent, defect quality |
| Merge, test, push, promote, Mac pull, record, `into:` lines | produce MergeCheckpoint 4–5 | `script (built)` | the merge script |
| Send a gate-clear or gate-lift notice | produce ClearGate | `script` | format and delivery mechanical once result known → CI points and review watch |
| Count the fifth code merge and schedule a CI point | produce MergeCheckpoint 6, CIPoint | `script` | counter skips shrinks, derives from merge subjects → CI points and review watch |
| Run review watch, report, notify, log first exit 3 | produce MergeCheckpoint 6 | `script` | one-time escalation, report routing → CI points and review watch |
| Start a CI point and collect the validation and CI result | produce CIPoint | `script` | commands and polling as one transaction → CI points and review watch |
| Choose repair owner; decide if red needs a fix checkpoint | produce CIPoint, Dependencies 2 | `judgment` | cause and owner need code review |
| Smoke launch, verify CI, promote main | produce PromoteMain | `script` | preflight and git fixed; live smoke verdict may need a person → Production open and wrap |
| Git-mode checkout promotion and Mac pull | produce MergeCheckpoint 4, production rules | `script (built)` | merge script checks the Mac's printed rc |
| Classify a wait as code, file or preference | produce Dependencies 1 | `judgment` | needs the missing behavior understood |
| Run a scratch test of a claimed code block | produce Dependencies 1 | `script` | worktree setup and run mechanical once test chosen → Waiting, cross-unit search and alerts |
| Inspect berth overlaps and wait age | produce Dependencies 3–4 | `script` | board state and deadlines sampled each tick → Waiting, cross-unit search and alerts |
| Order landing, release or file porting | produce Dependencies 2–3, LandingCall | `judgment` | cost, rework and regressions decide order |
| Investigate a user permission block | produce Dependencies 6 | `judgment` | read the exact denial, do only authorized work |
| Send and log a phone alert with fallback | produce Notify | `script (built)` | `pushover.py` transports; urgency and text stay judgment |
| Raise an eight-hour agenda item | produce Agenda | `script` | threshold and first-open dedupe; options stay judgment → Waiting, cross-unit search and alerts |
| Search other units for renamed public items | produce CrossUnitChange 1 | `script` | git and worktree search gives exact sites → Waiting, cross-unit search and alerts |
| Decide restructure holds and migration instructions | produce CrossUnitChange 2 | `judgment` | how concurrent edits move |
| Relay quota state to units | produce QuotaAlert | `script` | fixed fanout and held-item lifecycle; account action is the user's → Waiting, cross-unit search and alerts |
| Final CI point and close-out cleanup | produce Wrap 1, 3–5 | `script` | clean and merged checks, worktree retire, notifier removal, doc transition → Production open and wrap |
| Decide close-out approvals and user-data migration | produce Wrap 2 | `judgment` | irreversible work needs the owner's decision |
| Render the final wrap report | produce Wrap 6 | `script` | git, CI and close-out records supply rows → Production open and wrap |
| Build dailies input from `unit_status.sh` | dailies Status check, Gather, Input | `script` | largest: every unit, pane, status, ETA, phase, warning into renderer JSON → Dailies input builder |
| Restart the update clock after a user-run dailies | dailies Status check | `script` | notifier restart and next_due log are fixed → Dailies input builder |
| Gather time, LOG, merge state and review watch | dailies Gather 1–2, 4, 6 | `script` | read-only sources fill the state → Dailies input builder |
| Detect stale or past ETA and request a new one | dailies Gather 3 | `script` | threshold and dedupe → Dailies input builder |
| Resolve open topics, needed action, user priority | dailies Gather 5, Subjects | `judgment` | is a topic closed, who owns the next act |
| Supply project goal, measurable target, upcoming `then` items | dailies Input | `judgment` | semantic plan summaries; renderer validates form |
| Render the fixed report and chart mode | dailies Output | `script (built)` | `dailies_render.py` formats, checks JSON, persists chart mode |
| Build-hold markers and holder files | dailies Input | `script` | renderer checks consistency; builder fills markers from holder files → Dailies input builder |

**Files:**
- `scripts/production/merge_checkpoint.py` — the merge command.
- `scripts/production/test_merge_checkpoint.py` — real `git` in temporary repositories with a temporary bare `origin`; stubs on `PATH` for `verify.sh`, `validate_and_push.sh`, `review_regime.py` and `ssh` record argv.
- `scripts/production/stall_watch.py`, `scripts/production/test_stall_watch.py` — the blocked rule and the finished-run skip.
- `commands/showrunner/produce.md` — <MergeCheckpoint/> calls the script; footers-off wording.
- `commands/showrunner/dailies.md` — one step reference.
- `commands/unit/delegate.md` — <ProgressContract/> names `/unit:report off|on` (a file another unit also edits).

**Binds later work:** each `script` row's owner is the phase title at its end; the owning phase builds it as a script the showrunner calls and removes the step from the command text. A general resume view (`Production open and wrap`) reads `LAST_MERGED` from the same merge-subject rule `merge_checkpoint.py` uses. The footer renderer (`Update registration`) must match `produce.md`'s rule that footers off drops the Waiting on block too.

**Gotchas:**
- `ssh`'s own status is always 0; the Mac pull is read by the `rc=` line it prints.
- The `main` push needs `origin/main..main` to hold only this production's commits; promote to the pushed tip, never the earlier checkpoint merge.
- `reset --keep HEAD~1` runs only after confirming `HEAD` is this unpushed merge.
- `MergeRequest.delivers` and `excluded` are `str` with `""` for absent, not a `CodeCheckpoint(delivers)` state.
- Tests never reach a live Mac, real `ssh`, `validate_and_push.sh` or CI, never push anywhere but a temporary bare repository, and never write the real `~/.claude` or `~/.local/state/`; the stubs check argv and returned statuses only.

**Ruled out:**
- `LAST_MERGED` as an argument: it is read from the merge subjects.
- A flag the showrunner sets for a finished run: `stall_watch.py` reads the Plan cell's `run done` and takes the unit's name from both the Unit and the Session cell.

### Phase 6 — Dailies `then` is a list of single items, never a string or a chain · status: done

#### As-built

- `parse_upcoming_work` accepts `then` only as a non-empty list of one-line items, one per upcoming phase or follow-up. It still returns `UpcomingWork(items)` or `NoUpcomingWork()`, each item still passes `check_words` and `check_then_order`, and `simple` prints the first item only.
- Three inputs are each refused with an `InputError` that names the list form and the item's index: a string `then`; an item that chains phases by `, then `, `; then` or ` then Phase` (any case); and an item holding two `Phase <N>:` heads (`Phase 3: x and Phase 4: y`). The string refusal fires at `simple`, `page` and `elaborate`; a later item is refused with its own index.
- A mention of another phase without its own head (`Phase 4: panels match once Phase 3 merges`), a range with one shared purpose (`76–79: …`), and the word `then` elsewhere in an item stay allowed.
- The `then` row and example in `commands/showrunner/dailies.md` show the list form, give one phase or follow-up per item, and say a chained item is refused; no wording allows a string.

**Files:**
- `scripts/production/dailies_render.py` — `parse_upcoming_work` and its refusal text.
- `scripts/production/test_dailies_render.py` — the string refusal at every length, the chained and two-head refusals, a later item refused with its index, and the range and ordinary `then` word allowed.
- `commands/showrunner/dailies.md` — the `then` row and example.

**Binds later work:** the dailies input builder is the next producer of `then`; it passes `then` through unchanged, so a string or chained item reaches the renderer's refusal, and its acceptance test runs the builder's output through the renderer. All three files belong to another unit, so a later edit to one is named as an `also touches <path> (owner stalls-unit)` file, tested against that owner's tip.

**Gotchas:**
- The two-heads refusal is its own alternative: `Phase 3: x and Phase 4: y` holds no `then`, so the chain-word patterns alone let it through.

**Ruled out:** a renderer-side rewrite of a chained string into a list; the refusal makes the showrunner fix the input, and a silent rewrite would hide the defect.

### Phase 7 — A unit director's model and effort come from agents.conf · status: done

#### As-built

- **Registry.** `config/agents.conf` carries `production=claude` under `[assignments]` and a `[production.claude]` section with `director=sonnet:xhigh` (the user's default, 2026-10-07). There is no `[production.codex]`; a comment states that unit directors launch only on Claude and the showrunner is whatever session runs `/showrunner:produce` (nothing launches it). The header Consumers list names `production unit directors (add_unit.py)` beside the existing consumers, `/agent_exec` included.
- **One-family pinning in `scripts/agents/agents_config.sh`.** A function whose rows name exactly one family (here `production`, set only for claude) is pinned to it (`_agents_pinned_family`). `agents_set_all_assignments <family>` and `agents_set_model <agent>` with no function named leave a pinned function's assignment and rows as they are, like a `caller` function, and record it in `AGENT_KEPT_FUNCTIONS`; `agent_admin.sh` prints `# kept <fn> on <family>: its only set` (for example `# kept production on claude: its only set`) after its `# switched …` lines, one per kept function. A switch aimed at the pinned function itself (`agents_set_assignment production codex`, `agents_set_model <codex agent> production`, `agents_set_row production.director <codex agent>[:<effort>]`) prints `ERROR: 'production' runs only on claude: [production.claude] is its only set.`, returns 1 and leaves the file untouched. `/agent production.director sonnet:xhigh` writes the row. Every other function has both sets and is unaffected; the pin lasts until a second family set is added.
- **Resolution in `scripts/production/add_unit.py`.** `director_agent(request) -> DirectorAgent` (`NamedTuple`: `model: str`, `effort: DefaultEffort | Effort(value)`) runs `bash -c 'source "$1" && agents_resolve production.director && printf "%s\n%s\n%s\n" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT"' _ <agents_config.sh>`, taking `agents_config.sh` from the script's own checkout (`Path(__file__).resolve().parents[1] / "agents" / "agents_config.sh"`) and leaving `AGENTS_CONFIG_FILE` to the caller's environment (default `~/.claude/config/agents.conf`). `preflight` calls it (`ReadyToLaunch`, `DirectorAgent`), so `--check` refuses too and promotion never stops a session it then cannot launch. A nonzero resolver exit is exit 2 with the resolver's first `ERROR:` line; a family other than `claude` is exit 2 with `unit directors launch only on claude; production.director resolves to <family> (<model>)`. Neither refusal writes, stops or starts anything in the production doc, the repository or tmux.
- **Launch flags.** `launch_session` builds `claude --model <model> [--effort <effort>] [--resume <id>] --remote-control …`, with `--effort` only for `Effort(value)` (an empty effort uses the CLI default, as `agents_claude_args` does). This covers plan, brief, standby and resume launches from `add_unit.py` (`/showrunner:add_unit`, the `produce.md` <LaunchUnits/> launch step) and `promote_unit.md` step 6. The commit, the Units row and the log line are unchanged.
- **The showrunner's own Resume** (`produce.md` <LaunchUnits/>) resolves flags first: `bash -c` sourcing `agents_config.sh`, `agents_resolve production.director`, a refusal of any family other than `claude` with the same line as `add_unit.py`, then `agents_claude_args`. Only then does it run `claude --resume <session-id> <flags> --remote-control <session> -n <session>`; a nonzero exit stops and tells the user its line, and `claude --resume` never runs without the flags. `add_unit.md` and `promote_unit.md` each state in one line that the launch takes its model and effort from `/agent production.director`; `commands/agent.md` documents a one-family function as kept by every-function switches and refused when switched alone.

**Files:**
- `config/agents.conf` — `production=claude`, `[production.claude]`, the comments.
- `scripts/agents/agents_config.sh` — pinned one-family functions, `AGENT_KEPT_FUNCTIONS`; `scripts/agents/agent_admin.sh` — the `# kept …` lines; `scripts/agents/test_agents_config.sh` — pinned-switch cases (every-function switches keep `production` and print the `# kept` line; direct switches are refused with the file byte-identical; the `sonnet:xhigh` row resolves to `sonnet` and `xhigh`).
- `scripts/production/add_unit.py` — `DirectorAgent`, `director_agent`, `ReadyToLaunch`, `preflight`, `launch_session`; `scripts/production/test_add_unit.py` — flags on all four launch paths, the codex and missing-section refusals with and without `--check`. Each test writes its own `agents.conf` in a temporary root and sets `AGENTS_CONFIG_FILE`, `CODEX_CONFIG_FILE`, `CODEX_MODELS_CACHE_FILE` and `CODEX_CATALOG_SYNC_STATE_FILE` to temporary paths (state file newest, so no catalog sync runs), with `systemd-run` stubbed.
- `commands/showrunner/produce.md`, `commands/showrunner/add_unit.md`, `commands/showrunner/promote_unit.md`, `commands/agent.md`, `docs/as-built/agent-registry.md` — the launch, Resume and `/agent` wording above.

**Binds later work:** `--check` runs `launch_request` and `preflight` only (exit 0 or 2, nothing written or started); the launch states stay `PlanGiven | BriefGiven | Standby` with the session `ResumedSession` or new, and the director's model and effort sit beside them. The readers `read_production`, `production_field`, `cell_value` and `unit_rows` are unchanged. `agents_config.sh` is bash only and refuses to be sourced into zsh (call it through `bash -c`), and the resolver must keep working under `/bin/bash` 3.2 (no associative arrays, no `${var,,}`): the style-fix pipeline runs it unattended every ten minutes on both machines, so `agents_config.sh` and `agent_assignments.sh` are never left broken. `config/agents.conf`, `scripts/agents/agents_config.sh`, `scripts/agents/agent_admin.sh`, `scripts/agents/test_agents_config.sh`, `commands/agent.md` and `docs/as-built/agent-registry.md` lie outside this unit's Owns row and no Units row owns them: a checkpoint notice names each as `also touches <path>` with no owner, and the trial merge runs against the merge branch tip.

**Gotchas:** `config/agents.conf` passes a git clean filter that pins staged content to the index, so `git diff` is empty for it and a plain `git add` commits nothing; stage it with `AGENTS_CONF_COMMIT=1 git add --renormalize config/agents.conf`, then check `git diff --cached --stat` lists it. Merging `config/agents.conf` into a machine's checkout rewrites that machine's live registry file with the committed rows, so live `/agent` retunes there are lost. Sourcing `agents_config.sh` runs the registry's routine Codex catalog refresh when its state is stale; it writes only the registry's own state, so `--check` does not promise zero writes to it. Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh` and never write `~/.claude/config/`, `~/.local/state/` or a real production doc.

**Ruled out:** a one-line substitution for the Resume flags, since a failed substitution runs `claude --resume` with no flags and skips the family check; a `[production.codex]` set or a Codex director, since unit directors launch only on Claude.

### Phase 8 — Stall watch treats a unit waiting on someone else as waiting · status: done

#### As-built

- The stall watch reads the kind of a unit pane's last turn-end line, with or without the leading `— `. `done`, `blocked`, `gate` and `decision` are waits: the unit has finished or handed the next move to someone else, so the watch sends no bump, sends no notice to the showrunner, and writes a fresh stretch.
- `holding` is the one kind that stays a stall candidate: it says the unit is waiting on work it has running, so with nothing running it is the stall the watch exists to catch. A pane with no turn-end line on screen takes the stall path as before.
- `WAITING_KINDS` and `HOLDING_KIND` name the kinds. The turn-end pattern is built from them with a named `kind` group, and the last match's kind picks the branch, so the pattern and the wait test cannot drift apart.
- The stretch restarts at the next status that is not a wait. A unit that answers a gate and then shows `— holding:` is judged stalled only after a full interval from the first screen that shows that line.
- The standby skip and the finished-run skip (a Units row whose Plan cell says `run done`) still run before any bump or notice.
- The verbatim `— gate: the user runs the Mac Claude go-live and controls at a Mac terminal` line, a bare `gate:` line, and prefixed and bare `decision:` lines are each silent at the start and after the stall interval, then bumped and reported once after a later `— holding:` line. The changed-reported-status test stays, with a `holding:` last line.

**Files:**
- `scripts/production/stall_watch.py` — the waiting kinds, the turn-end pattern and the wait branch in the unit loop.
- `scripts/production/test_stall_watch.py` — the stall-watch tests, 29 in all.
- `docs/as-built/build-followups-fn-length-hook.md` — its stall-watch sentence names the four waiting kinds.

**Binds later work:** `scripts/production/unit_status.sh` is unchanged: it treats a `gate:` line as activity text, and anything that reads a unit's status takes it the same way.

**Gotchas:**
- `unit_status.sh` prints `STILL WAITING on you` only for `decision:` and `blocked:` last lines. A `gate:` wait shows only as the unit's activity line, so a unit stopped at a gate is silent in the stall watch and not flagged to the user by the status script.
- Sandboxed seats have no writable temporary directory and cannot run this suite; run it from the main tree.

**Ruled out:** teaching `unit_status.sh` to flag `gate:` in this change, since that is a script outside the phase's files and changes what the dailies show.

### Phase 9 — Dailies input builder · status: done

#### As-built

- `dailies_input.py --production <doc> --status <file> --judgment <file> --state-dir <dir> --out <file> [--length simple|page|elaborate] [--holders <dir>] [--notifier <path>] [--user-run] [--at <time>]` writes the mechanical half of `dailies_input.json`, merges the showrunner's judgment file over it and validates the whole through the renderer's own `parse_report`. It prints `<step>: ok|failed — <line>` per step and exits 0, or 2 on a refusal, which writes nothing to `--out`. `--at` fixes the clock for tests.
- `--status` is the saved `unit_status.sh` output. A `== <unit>` line opens a block; a name outside the Units table is refused, naming it. Each unit is `SessionGone | ClaudeNotRunning | Running` plus flags `FormWaiting | Decision | StillWaiting | Block | TicksFailing`; every other block line is kept as `Activity(text)` and never refused. `flags first:` lines name what dailies puts first (session gone, Claude not running, form, decision, a usage limit in the activity text); `needs_user` is set for a form, decision or still-waiting flag the judgment file left unset.
- The builder fills `length`, `zone`, `unit`, `next_run`, each unit's `build_hold`, the `Review watch` topic and the merge-branch topic. `label` is left to the renderer's own default or refusal. `build_hold` is true for a unit under a holder file in `--holders` (default `~/.local/state/build-hold/`), a holder's own unit never marked; a marker with no holder file, or holder files with no marked unit, is refused as the renderer refuses it. `review_regime.py watch` supplies the review-watch line until it prints `acknowledged`; exit 3 sets `needs_user`. The merge-branch topic shows the last merge from the reader and whether `MERGE_BRANCH` is pushed; held or testing comes from the judgment file.
- ETA state per unit is `EtaNone | EtaStale | EtaPassed | EtaFresh`, converted to the renderer's text only at that boundary. An ETA text first seen over an hour ago keeps the ETA with `detail` set to `set HH:MM` and prints `request /unit:eta: <session>`; a passed ETA (judged with the renderer's `parse_time`, tomorrow rule included) reads `none measured - requested` and prints the same line. `<state-dir>/eta_seen.json` holds `{"text", "first_seen", "requested"}` per `<session>|<phase>`, `first_seen` to the minute, saved in a `finally`; `requested` survives a changed ETA text, so the line prints once per unit and phase. The showrunner sends the request; the builder only prints it.
- A scheduled tick restarts nothing. Under `--user-run` the builder first parses the finished input with the renderer's parser (imported, `next_run` left out), then runs `<notifier> restart <UPDATES>`, takes `next_due` for `next_run` and appends that line to `LOG` (opened before the restart). A failed restart is `failed` and leaves `--out` untouched. `next_run` is read in the production zone.
- The builder names five judgment fields itself (project, phase, started, held, update); the renderer names the rest as `units[N].<field>: required`. `then` passes through unchanged: a string or a chained item is refused with the renderer's message and the item's index, naming the judgment file. `dailies.md` names `label` among the judgment fields.
- `merge_branch_history(checkout, merge_branch) -> MergeBranchHistory` in `merge_checkpoint.py` offers `last_merge()`, `last_for_unit(unit)`, `last_code_for_unit(unit)`, `code_merge_count()` and `has_code_merge(unit, phase)`; absence is the named `NoMerge`. It reads the subject prefix `Merge <unit> phase <N>[ shrink] (<short>)`; a shrink merge never counts as a code merge. `merge_history` (taking the unit's last code merge) and `code_merged` call it with unchanged behavior.
- `dailies.md` saves the status output, runs the builder (`--user-run` for a user-run dailies), then the renderer on `--out`, and asks the showrunner only for the judgment file. `produce.md`'s scheduled-update prompt saves the full `unit_status.sh` output to `<SCRATCH>/unit_status.txt`, then runs `/showrunner:dailies simple`.

**Files:**
- `scripts/production/dailies_input.py` — the builder; `scripts/production/test_dailies_input.py` — 40 tests on real status line kinds and a fixed `--at` clock.
- `scripts/production/merge_checkpoint.py` — the merge-branch reader; `scripts/production/test_merge_checkpoint.py` — its query cases on a temporary repository.
- `commands/showrunner/dailies.md`, `commands/showrunner/produce.md` — the call sites.

**Binds later work:** the production open-and-wrap work and the CI points and review-watch work import the merge-branch reader by the names above and the `NoMerge` absence, never copying the subject rule. The ETA record's form is shared: the waiting, cross-unit search and alerts work owns `eta-request` and the no-ETA path reading it. The CI points and review-watch work owns one first review-watch alert, whichever of a dailies or a merge sees exit 3 first.

**Gotchas:**
- The renderer exposes no list of required fields, which is why the builder names five itself.
- The renderer's ETA-change check reads its own state file after a user-run dailies has restarted the update clock, so a refusal leaves the clock moved; the fix is to correct the judgment and run again. The update-registration work adds a read-only `--render-state` preflight so the refusal comes first.
- `merge_branch_history` reads every ancestor where `produce.md` specifies first-parent history; the production open-and-wrap work repairs it first.
- `DAILIES_REVIEW_REGIME` keeps tests off the real review regime.
- `unit_status.sh` never flags a `gate:` wait (it knows only `decision:` and `blocked:`); queued as an add-on for the user, placed in no work order.

**Ruled out:** renaming `MergeEntry` / `NoMerge` to checkpoint-merge names (churn across shipped code and tests for no behavior change); a gate-wait surface in a later work order (a new item is the user's call through the add-on review); reporting the builder's `<step>: ok` lines to the user (an implementation transcript).

### Phase 10 — Production open and wrap · status: done

#### As-built

`production_lifecycle.py` runs a production's fixed git and document steps. Each subcommand takes `--production <doc>` and `--no-ci`, prints `<step>: ok|held|failed — <one line>`, stops at the first `held` or `failed`, and exits 0 or 2.
- `load [--resume]` — `CHECKOUT` must be on `MERGE_BRANCH`; reads the last `### STATE` block of `LOG`, `LAST_MERGED[unit]` from `last_code_for_unit`, and each session through `tmux has-session`.
- `open [--session <name>]` — status `planned` only: creates `MERGE_BRANCH`, sets the doc `running`, commits, pushes with upstream, excludes and creates `LOG`; a re-run resumes at the first step not done.
- `promote-main` — the git steps up to the person's live smoke verdict; a **Promote:** destination at the tip prints `already at <tip>` and is not pushed, one behind is pushed, and the declared Mac destination is pulled even under `--no-ci`.
- `wrap [--close-out-done "<exact item>"]` — clean and merged checks, worktree retire, notifier removal, status change, final wrap report; a unit whose close-out needs approval is `held`, naming it. A doc whose final push failed resumes from status `wrapped`.
- `--no-ci` makes each CI, smoke-launch and Mac-run step print `<step>: ok — skipped: no CI` and runs the rest.
- `merge_branch_history` reads `git log --first-parent`: a merge subject carried in by a merged side branch counts toward nothing.
- `produce.md` names each subcommand and flag in <LoadProduction/>, <OpenMergeBranch/>, <PromoteMain/> and <Wrap/> and keeps the judgment: close-out approval, the live smoke verdict, the CI, smoke and mirror text.

**Files:**
- `scripts/production/production_lifecycle.py`, `test_production_lifecycle.py` — the command; tests on real git with a bare origin and stub `py`, `tmux`, `ssh`, `cargo-berth`.
- `scripts/production/merge_checkpoint.py`, `test_merge_checkpoint.py` — shared promotion code, first-parent history.
- `scripts/production/stall_watch.py`, `test_stall_watch.py` — finished-run match.
- `commands/showrunner/produce.md` — the four steps above.

**Binds later work:**
- `production_lifecycle.py` takes `--ci-green <sha>` and `--smoke-passed <sha>`, each of which must equal the merge tip or the step is held; the CI points command (CI points and review watch) must print its sha in a form passed verbatim as `--ci-green`.
- `open --session <name>` writes the showrunner session once and `load --resume` never changes it; a resumed session registers its own name through update registration's explicit session argument.
- `merge_checkpoint.py` exposes `promote_local_checkout` and `pull_mac_checkout`, shared by the merge command and `promote-main`.
- `merge_branch_history` counts first-parent merge subjects.
- The stall watch's finished-run match reads the Units row's Plan and Session cells.

**Gotchas:**
- `showrunners.py` is mode 0644: call it through `scripts/lib/py`.
- `already at` in a promote line means main was at the tip before the run; no other line uses it.

**Ruled out:**
- Distinct CI and smoke verdict types: the flags are separate, each checked at its own step against the same tip.
- A user topic in the lifecycle command for a mismatched verdict: the hold line names it.

### Phase 11 — Update registration · status: done

#### As-built

`scripts/production/update_registration.py` is the production's update command. It has four subcommands, each taking `--production <doc>`, printing `<step>: ok|failed — <one line>` and exiting 0 or 2. The reader, the named states and the step-line form come from `merge_checkpoint.py` by import. Named states cross the reader boundary, never a string or `None`: `InstancePresent | InstanceAbsent`, and `SessionLineSame | SessionLineChanged(<old>)` for the doc's session line.

- `register --session <name>` — `--session` is required on every start and every resume (the command cannot read the session's current name; the doc line holds only the first). The notifier target is `session:$CLAUDE_CODE_SESSION_ID`, refused when unset. Steps in order:
  1. Validate and build everything, changing nothing: the environment, the Updates interval, the prompt expanded from the `produce.md` template (a missing marker is a failed step, never an exception), and a production doc with no uncommitted edits.
  2. Registry: when the doc's line names another session, `showrunners.py rename <old> <name>` retires the old entry; `showrunners.py add <name> --zone <zone> --unit <each unit's tmux session>` runs on every call.
  3. The doc's `**Showrunner session:**` line is set to the name and committed as `production(<name>): showrunner session <session name>`, only when the line changed. This follows the registry so a retry of a failed rename still finds the old name in that line.
  4. The prompt is written to `PROMPT_FILE`.
  5. The `UPDATES` instance gets `new` on every call; a repeated `new` retargets it at this session without moving its clock.
  6. The stall-watch and tmux-names jobs are created only when `notifier.sh status` reports no instance.

  It prints the instance's `next_due`. The scheduled prompt is expanded from its template with the interval, the user's zone and this session's name; the template alone carries the `unit_status.txt` save and the `/showrunner:dailies simple` call, and passes `--render-state`.
- `time <ISO or HH:MM>` — converts any stamp to the doc's `ZONE` and prints `HH:MM <zone>`, never UTC; `H:MM` and `HH:MM` are parsed explicitly.
- `log "<event>"` — appends `- HH:MM <zone>: <event>` to `LOG`; on every tenth event and with `--before-compaction` it also appends a `### STATE <time>` block: one line per Units row (last merged checkpoint from the merge history by unit name; phase and wait from `--state <file>` by session name, `not stated` when absent), the merges accepted but held, and the items open for the user (from `OUTSTANDING`).
- `footer` — reads `FooterState` from `scripts/hooks/showrunner_footer.py` by import. `FooterState.OFF` prints `footer: ok — footers off` and nothing else, which drops the Waiting on block too. `ON` runs `dailies_render.py --footer` with the doc's zone and the next run (none when the notifier instance is stopped) and the `OUTSTANDING` file. `--nothing-needed` says the last bullet is empty.
- `--state <file>` is one showrunner file (`SHOWRUNNER_STATE`, documented in the State list of `produce.md`), parsed strictly: `units`, `merges_held` and `open_for_user`, every key optional; an unknown key or a wrong type fails the step. `OUTSTANDING` is rewritten only by `footer`, only when `--state` has an `open_for_user` key, and every item is validated before anything is written.

`dailies_render.py` exposes `StateClear | StateRefused(field, why)` and `check_render_state(value, state_path, at=None)`, which wraps its `Previous`, `load_state` and `check_changes`; those stay private to the renderer. `dailies_input.py` takes the required `--render-state <path>` (the file the renderer's `--state` gets) and calls only `check_render_state`, after `validate_report` and before the clock restart. A refusal is a `judgment` failure naming the field and leaves everything as found: the notifier is not called, `--out` is untouched, `eta_seen.json` is byte-identical and no `request /unit:eta:` line is printed. `--user-run` creates `LOG` before the restart. `commands/showrunner/dailies.md` and the scheduled prompt pass the same path to the builder and the renderer, and give a builder `<step>: failed` line to the user as the failure message. `produce.md` calls `time`, `log`, `footer` and `register` where it names the Time, Log, Footer and StartUpdates steps.

**Files:**
- `scripts/production/update_registration.py` — the command; `scripts/production/test_update_registration.py` — its tests (stub `notifier.sh` on `PATH`, temporary doc, `LOG` and `FooterState` directory).
- `scripts/production/dailies_input.py` — the builder, `--render-state` required, all-or-nothing writes; `scripts/production/test_dailies_input.py` — its cases.
- `scripts/production/dailies_render.py` — `StateClear | StateRefused`, `check_render_state`; `scripts/production/test_dailies_render.py` — its cases.
- `commands/showrunner/produce.md` — Time, Log, Footer, StartUpdates and the showrunner state file.
- `commands/showrunner/dailies.md` — `--render-state` pass-through and the builder failure line.

**Binds later work:** the dailies builder's order is judgment, build hold, review watch, merge branch, `validate_report`, `check_render_state`, the clock, then the record and output writes (`eta_seen.json`, the `request /unit:eta:` lines, `--out`); a step added later goes before the writes. Every builder call needs `--render-state`; the test helper `run_builder` passes a default unless a case supplies one. `stall_watch.py` still reads the notifier conf's `CHECK=` path and the Units rows' Plan cell.

**Gotchas:**
- `register` is idempotent across a partial failure only because the registry rename precedes the doc-line commit; reordering them breaks retry.
- A missing prompt marker in the `produce.md` template is a failed step, not an exception.
- `footer` never erases `OUTSTANDING` unless `--state` carries `open_for_user`.

**Ruled out:** a footer scan for an invented `needed` key (`--nothing-needed` replaced it).

### Phase 12 — CI points and review watch · status: done

#### As-built

`scripts/production/ci_points.py` is the production's CI-point and review-watch command. Its subcommands are `due`, `ci start [--cancel-prior]`, `ci collect`, `watch`, `notice clear <gate id>` and `notice lift <gate id> --log <path>`. Every one takes `--production <doc>`, prints `<step>: ok|failed — <one line>` and exits 0 or 2; the stateful ones take `--state-dir <dir>`. `due`, `ci start` and `ci collect` take `--no-ci`, which the showrunner passes when the production doc's **Production rules** say CI does not apply: each then prints `<step>: ok — skipped: no CI` and does nothing else. States cross the reader boundary as named types, never strings or `None`: `CiDue | CiNotDue(<count>) | CiNotUsed`, `WatchFirstAlert | WatchRepeat | WatchBelowThreshold`, `CiRecord(<count>, <tip>) | NoCiRecord`.

- `due` counts code merges with `merge_checkpoint.merge_branch_history(checkout, merge_branch).code_merge_count()` (imported; a shrink merge never counts) and is due at five or more since the record in `<state-dir>/ci_points.json`, `{"count", "tip"}`; no record counts as zero. It stays due until `ci collect` records a point, and a fresh process reads the same file.
- `ci start` runs the validate-and-push script and leaves `ci_run.json` (tip, run id) in the state directory, the only handoff to `ci collect`. `--cancel-prior` is forwarded to the script after `--fix-commit`.
- `ci collect` waits on the run and prints the validation and CI result as one line each. Its last line is `--ci-green <tip>`, printed only when the run's head commit equals the validated tip, job `Test Suite` succeeded and every other job succeeded or was skipped; `promote-main` and `wrap` take that line as it is. Any other outcome prints `ci collect: failed — rerun <check> on <tip>` and no flag; a missing, non-string or empty `headSha` is `rerun CI on <tip>: CI result names no head commit`, never a pass. The merge count is taken at the validated tip before the wait and recorded with that tip on a pass, so a merge landing during the wait stays due. The showrunner turns a failed line into a dailies topic with `needs_user: true`. The smoke verdict is the showrunner's own launch (`--smoke-passed <tip>`), not this command's.
- `watch` runs `review_regime.py watch` and prints its report line. The first exit 3 is claimed under `fcntl.flock` on `<state-dir>/review_watch.lock`: it prints the `report --since` table, pushes one notification (`~/.claude/scripts/notify/pushover.py --priority 1 "Hana: review watch" "<one line>"`), appends one `LOG` line directly (as `merge_checkpoint.py` does), then writes `review_watch.json`. A later exit 3, or the loser of two simultaneous calls, is `WatchRepeat`; any other exit is `WatchBelowThreshold`.
- `notice clear` and `notice lift` draft the `send <unit>:` line for a cleared or lifted gate, in the form the Gates table and <ClearGate/> give. `notice lift` refuses without `--log`, since the gate-lift message names the test's log path.

`commands/showrunner/produce.md` names each subcommand where it names the step. Its State list has `DAILIES_STATE_DIR` (`<SCRATCH>/dailies_input_state`), passed to every stateful `ci_points.py` call. <CIPoint/> keeps the `--cancel-prior` and `--no-ci` judgment sentences and finishes <PromoteMain/> with the `--ci-green` line; <Wrap/> names `--ci-green` and `--smoke-passed` side by side. Routing arrivals and ordering the checkpoint queue stay showrunner judgment.

`scripts/production/dailies_input.py`'s review-watch step calls `ci_points.review_watch` by import, so a first exit 3 is logged and notified once, whether a dailies or a merge reaches it first. The builder prints the `report --since` table on a first alert and treats a repeat as `needs_user` too.

**Files:**
- `scripts/production/ci_points.py` — the command.
- `scripts/production/test_ci_points.py` — 14 cases: older commit, red job, unfinished run, missing head commit, fourth/fifth/sixth merge, a cross-command case feeding the real `promote-main`, and dailies-first, merge-first and simultaneous first alerts.
- `scripts/production/dailies_input.py`, `scripts/production/test_dailies_input.py` — review-watch step by import; 44 cases, including a doc-contract case that pins `produce.md` and `dailies.md` to the same state directory.
- `commands/showrunner/produce.md` — <CIPoint/>, <ClearGate/>, <Direct/>, <MergeCheckpoint/> step 6, <Wrap/>, State list.

**Binds later work:** the first-alert record and `eta_seen.json` share the one directory `DAILIES_STATE_DIR`; two directories fork the record, so callers of the waiting and cross-unit search command that touch `eta_seen.json` pass the same directory as the builder, with the same doc-contract pin. `notice lift --log <path>` takes the log path a scratch test produces. The builder's review-watch step stays routed through `ci_points.review_watch`.

**Gotchas:**
- `review_watch.json` is written after the push and the `LOG` line, so a crash between them repeats the alert once: at-least-once by choice.
- Live GitHub CI and the real push notification are never exercised (stubs only); this production has no CI.

**Ruled out:**
- A `queue` command: arrivals are messages in the showrunner's own session, and ordering is judgment.
- Routing the review-alert `LOG` line through `update_registration.py log`: it adds a stray `log: ok` line and couples two commands.
- Writing `review_watch.json` before the alert (at-most-once): a lost alert is the worse failure.

### Phase 13 — Waiting, cross-unit search and alerts · status: done

#### As-built

`scripts/production/waiting.py` holds the showrunner's waiting, search and alert mechanics. Each subcommand takes `--production <doc>`, prints `<step>: ok|failed — <one line>` and exits 0 or 2, reusing `merge_checkpoint.py`'s reader, named states and step-line form by import; `main` catches `merge_checkpoint.Stop`, so a bad production doc fails as one line. Every stateful call takes `--state-dir DAILIES_STATE_DIR`, the builder's directory, since a second one forks the once-per-phase record. `commands/showrunner/produce.md` names each subcommand at its step; classifying a wait and ordering a landing stay showrunner judgment.

- `scratch-test <waiting unit> --tests <command> --state-dir <dir>` — merges `MERGE_BRANCH` with `--no-commit --no-ff` into a detached temporary worktree at the unit's branch tip and runs the tests, keeping `<state-dir>/scratch-test-<unit>-<time>.log`: `ScratchGreen(<log path>) | ScratchRed(<last lines>, <log path>) | ScratchConflict`. It reports its verdict first, always removes the temporary worktree and merge state, and never commits or pushes. A red never says `blocked`; a conflict is a `failed` line, not a red. The log path feeds `ci_points.py notice lift <gate id> --log <path>`.
- `waits` — berth overlaps and wait ages from `cargo-berth board --json` and `LOG`: `WaitOpen(<since>) | WaitCleared`. An `unconfigured` board reads as "berth not configured", never a failure.
- `search <old name> <new name>` — each path and line in the other units' branches and worktrees naming a renamed public item.
- `quota` — the fixed fanout of a quota notice to every unit director, and per-account held alerts in `quota_seen.json` until acknowledged or restored: `HoldQuotaAlert | ClearQuotaAlerts | RestoreQuotaAlerts`. A send exit of 0 or 1 counts as delivered (1 is queued); it prints `held: <alert>` per held account. The account action stays the user's.
- `agenda` — `AgendaOpen(<unit>, <phase>) | AgendaNotDue`: an item when the current phase began more than eight hours ago (its start is the unit's previous merge in `LOG`, or its launch line) or its ETA falls more than eight hours after the start, deduplicated by `<unit>|<phase>` in `agenda_seen.json`.
- `eta-request <unit> --phase <phase>` — `EtaRequested | EtaNotYetRequested`: once per unit and phase, prints `request /unit:eta: <unit>` and writes `{"requested": true}` alone under `<unit>|<phase>` in `eta_seen.json`.

One locked state helper, `waiting.update_state(path, change)` (`fcntl.flock` on `<path>.lock`, re-read under the lock, no write when unchanged, unique temp file then `os.replace`), serves all three state files. `dailies_input.py` imports it with `eta_requested`: the no-ETA path reports a requested record as `none measured - requested`, and computed ETA records merge into fresh state under the lock, ORing `requested`.

**Files:**
- `scripts/production/waiting.py` — the command; `scripts/production/test_waiting.py` — 27 tests on producer-built fixtures and real git in temporary repositories.
- `scripts/production/dailies_input.py`, `scripts/production/test_dailies_input.py` — requested-ETA reading and the locked merge.
- `commands/showrunner/produce.md` — the subcommand call sites.

**Binds later work:** `waiting.phase_eta` reads a unit's ETA from `dailies_render.log_line`'s `dailies ETAs:` line, so any change to how that line resolves an ETA reaches the agenda. The held-quota and agenda user surfaces (held alerts as the Waiting on block's first item; the dailies `For discussion with you` topic; the `agenda closed:` form) are pinned by the status and dailies fixes phase.

**Gotchas:**
- `log_events` dates a date-less `LOG` line by walking backward across midnights; a gap of more than 24 hours between consecutive timestamped lines is invisible to it.
- The producers' shapes are not the obvious ones (the unconfigured board is a JSON envelope with exit 4; the ETA line is `dailies ETAs: <unit> <phase> <eta>; ...`; an account stem can hold a space, `codex 1`), so fixtures come from calling the producers, never from hand.

**Ruled out:**
- A separate follow-up phase for the waiting-surface contract cases: they are test-only, and the status and dailies fixes phase already edits `test_waiting.py`.

### Phase 14 — Status and dailies fixes · status: done

#### As-built

- **Gate waits.** `waiting_on_user` in `unit_status.sh` treats a last turn-end line of `gate:` exactly as `decision:` and `blocked:`. First sight prints the `=== DECISION for you from <unit> ===` … `=== end <unit> ===` block and records its key in the seen file. A repeat prints `STILL WAITING on you, <unit>: …`, and a later `holding:` or other non-wait turn-end clears it. A wait line naming `showrunner` or a peer unit from `$units` is the showrunner's: it prints no wait line and stays an activity line in the dailies status, and only a `blocked:` one prints `BLOCK in <unit>, open <age>: …`. The turn-end word is read from the start of the line only, and the showrunner and peer names match as whole words.
- **Held unchanged ETA.** In `dailies_input.py`, a unit whose judgment carries a `held` reason and whose ETA line equals the recorded `text` in `eta_seen.json` is never `EtaPassed` and never requested; its time passes through. An unheld unit keeps `none measured - requested` and one request per phase. `commands/showrunner/dailies.md` says a held unit's unchanged ETA is never requested and stays as stated.
- **One ETA moment.** `dailies_render.py` holds `Previous = LastUnitReport(phase, eta, held, first) | NoLastUnitReport`, with `eta: LastReportedEta(text, moment) | NoLastReportedEta`, and saves `eta_text` beside the moment. `resolve_eta_moment` returns `ResolvedEtaMoment | NoResolvedEtaMoment`: an ETA text exactly equal to the last report's for the same phase (`same_phase`) keeps that moment at any later clock time and across midnight, and a changed text resolves by `parse_time`'s two-hour rule. `resolve_eta_moments` runs once per report, and the change check and note, display, ordering, chart row, saved state, `log_line` and first-ETA drift all read its result, so an overdue unchanged ETA renders `unchanged, overdue` and the `dailies ETAs:` line carries the moment the report shows. A saved entry with no `eta_text` reads as `NoLastReportedEta` and is never refused as a move.
- **Waiting.** `waiting.eta_moment` resolves a weekday ETA to its nearest occurrence within three days of the log line, so a held ETA from before midnight never lands a week ahead and a held unit's kept ETA raises no agenda item. Contract cases in `test_waiting.py` pin the `held: <alert>` quota line with its percent left and reset time text, the agenda `LOG` and close forms, and the `For discussion with you` topic.
- **Director default.** Unit directors default to `opus:xhigh` in `config/agents.conf` `[production.claude]` again, moved back from `sonnet:xhigh` by the user on 2026-10-07; the comment above the row says so. The fixture rows in `test_add_unit.py` and `test_agents_config.sh` test that a row changes the launch, not the live default.

**Files:**
- `scripts/production/unit_status.sh` — per-unit status; gate, decision and `BLOCK` routing.
- `scripts/production/dailies_input.py` — held unchanged ETA stays fresh and unrequested.
- `scripts/production/dailies_render.py` — named previous-report states, `eta_text` in state, one resolved moment per ETA.
- `scripts/production/waiting.py` — nearest-weekday ETA moment.
- `commands/showrunner/dailies.md` — the held-ETA rule.
- `config/agents.conf` — the director default and its comment.
- `scripts/production/test_unit_status.py`, `test_dailies_input.py`, `test_dailies_render.py`, `test_waiting.py` — gate routing, held-ETA builder-to-renderer and past-midnight, legacy-state and waiting-surface cases.

**Gotchas:** `config/agents.conf` passes a clean filter: `git diff` shows nothing for it, and a change stages only with `AGENTS_CONF_COMMIT=1 git add --renormalize config/agents.conf`.

**Ruled out:**
- Named states for `LastUnitReport`'s `held` and `first`: they stay bare optionals, older than the ETA states.
