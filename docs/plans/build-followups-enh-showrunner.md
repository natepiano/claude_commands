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

`scripts/production/stall_watch.py` sends no bump and no showrunner notice while the last turn-end line on a unit's pane is `— blocked: …`; it treats that line as it treats `done:`, a fresh stretch, so the idle clock restarts at the next status that is not a block. A `— holding: …` line with nothing running is still bumped. It skips a unit whose Units row Plan cell reads `run done`, as it skips a standby unit: no bump, no notice, no stretch kept. The Plan cell is read from the production doc through the notifier conf's `CHECK=` path.

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
- A flag the showrunner sets for a finished run: `stall_watch.py` reads the Plan cell's `run done`.

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

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** the user, 2026-10-06 ~16:5x PDT, through the showrunner (natedev): "the director's model and effort come from agents.conf". Today no unit director launch passes `--model` or `--effort`, so every one runs at the Claude Code default.

**Goal:** every unit director launch passes the `--model` and `--effort` that `agents_resolve production.director` gives; `[production.claude]` `director=opus:xhigh` keeps today's behavior; `/agent production.director sonnet:xhigh` changes it; a Codex assignment is refused with a clear line before anything is stopped or started.

**Spec:**
- **Registry.** `config/agents.conf` gains `production=claude` under `[assignments]` and a section, with a comment saying unit directors launch only on Claude and the showrunner is whatever session runs `/showrunner:produce` (nothing launches it):
  ```
  # ── production (unit directors, launched by add_unit.py) ──
  [production.claude]
  director=opus:xhigh
  ```
  There is no `[production.codex]`. The header's **Consumers** list gains `production unit directors (add_unit.py)`.
- **One-family functions in `scripts/agents/agents_config.sh`.** A function whose row sets name exactly one family (here `production`, set only for claude) is pinned to that family:
  - `agents_set_all_assignments <family>` and `agents_set_model <agent>` (no function named) leave a pinned function's assignment and rows as they are, like a `caller` function, instead of rejecting the whole switch over its missing set. `agent_admin.sh` prints `# kept production on claude: its only set` after its `# switched …` line, one line per kept function.
  - A switch aimed at the pinned function itself is refused, with the file untouched: `agents_set_assignment production codex`, `agents_set_model <codex agent> production`, and `agents_set_row production.director <codex agent>[:<effort>]` each print `ERROR: 'production' runs only on claude: [production.claude] is its only set.` and return 1.
  - Every function configured today has both sets, so none of them changes behavior.
- **Resolution in `scripts/production/add_unit.py`.** `director_agent(request) -> DirectorAgent` (`NamedTuple`: `model: str`, `effort: DefaultEffort | Effort(value)`) runs `bash -c 'source "$1" && agents_resolve production.director && printf "%s\n%s\n%s\n" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT"' _ <agents_config.sh>`, with `agents_config.sh` taken from this script's own checkout (`Path(__file__).resolve().parents[1] / "agents" / "agents_config.sh"`), and `AGENTS_CONFIG_FILE` left to the caller's environment (default `~/.claude/config/agents.conf`). It is called from `preflight`, so `--check` refuses too and promotion never stops a session it then cannot launch:
  - a nonzero resolver exit is exit 2 with the resolver's first `ERROR:` line;
  - a family other than `claude` (a hand-written `production=codex` beside a hand-written `[production.codex]`) is exit 2 with `unit directors launch only on claude; production.director resolves to <family> (<model>)`;
  - nothing in the production doc, the repository or tmux is written, stopped or started on either refusal (the resolver's routine Codex catalog refresh may run, as on every resolver call).
- **Every launch path passes it.** `launch_session` builds `claude --model <model> [--effort <effort>] [--resume <id>] --remote-control …`, `--effort` only for `Effort(value)` (an empty effort uses the CLI default, as `agents_claude_args` does). That covers a plan launch, a brief launch and a standby launch (`produce.md` <LaunchUnits/> and `/showrunner:add_unit`) and a resume launch (`promote_unit.md` step 6). The commit, the Units row and the log line do not change.
- **The showrunner's own Resume.** `commands/showrunner/produce.md` <LaunchUnits/> **Resume** gets its flags first from `bash -c` sourcing `agents_config.sh`, running `agents_resolve production.director`, refusing a family other than `claude` with the same line as `add_unit.py`, then `agents_claude_args`; only then `claude --resume <session-id> <flags> --remote-control <session> -n <session>`. On a nonzero exit, stop and tell the user its line, and never run `claude --resume` without the flags. `commands/showrunner/add_unit.md` and `promote_unit.md` each say in one line that the launch takes its model and effort from `/agent production.director`. `commands/agent.md` documents a one-family function: kept by every-function switches, refused when switched alone.

**Files:**
- `config/agents.conf` — `production=claude`, `[production.claude]`, the comments.
- `scripts/agents/agents_config.sh` — pinned one-family functions; `scripts/agents/agent_admin.sh` — the `# kept …` lines; `scripts/agents/test_agents_config.sh`.
- `scripts/production/add_unit.py` — `DirectorAgent`, `director_agent`, `preflight` and `launch_session`; `scripts/production/test_add_unit.py`.
- `commands/showrunner/produce.md` — **Resume**; `commands/showrunner/add_unit.md`, `commands/showrunner/promote_unit.md`, `commands/agent.md` — one line each.

**Seats:** 1 writer + 1 tester.
- `impl` — every file above except the two test files; post `done` without waiting for the test seat.
- `test` — from the Spec alone. `scripts/production/test_add_unit.py`: each test writes its own `agents.conf` in the temporary root and sets `AGENTS_CONFIG_FILE`, `CODEX_CONFIG_FILE`, `CODEX_MODELS_CACHE_FILE` and `CODEX_CATALOG_SYNC_STATE_FILE` to temporary paths (the state file newest, so no catalog sync runs), as `scripts/agents/test_agents_config.sh` does; then, from the recorded `systemd-run` argv, a plan launch, a brief launch, a standby launch and a resume launch each run `claude --model opus --effort xhigh`; a `sonnet:xhigh` row gives `--model sonnet --effort xhigh`; an `opus` row with no effort gives `--model opus` and no `--effort`; a hand-written codex assignment is exit 2 with its one line and nothing launched, written or committed, with and without `--check`; a missing `[production.claude]` is exit 2 with the resolver's line. `scripts/agents/test_agents_config.sh`: `/agent codex` and `/agent <codex agent>` keep `production` on claude, print the `# kept …` line and still switch every other function; `/agent production codex`, `/agent <codex agent>` for `production` and `/agent production.director <codex agent>:xhigh` are refused with the file byte-identical; `/agent production.director sonnet:xhigh` writes the row and `agents_resolve production.director` then gives `sonnet` and `xhigh`. Owns the final suite run.

**Constraints from prior phases:**
- Phase 4 (as built): `add_unit.py --check` runs `launch_request` and `preflight` only, exit 0 or 2, nothing written or started; promotion runs it before it stops the session, so resolution sits in `preflight`. The launch states are `PlanGiven | BriefGiven | Standby` and the session `ResumedSession` or a new one; keep them, and add the director's model and effort beside them, not as a new launch state.
- Phase 5 edits `produce.md` <MergeCheckpoint/> and the footer lines; this phase edits only <LaunchUnits/> **Resume**. Runs after Phase 5.
- `produce.md` and `promote_unit.md` are stalls-unit's hub files that this unit's Owns row lists (gate G1 is cleared): edit them after merging `build-followups` into the branch at the phase start; neither is an `--also` path.
- `agents_config.sh` is bash only and refuses to be sourced into zsh; call it through `bash -c`, never `zsh`. The resolver must keep working under `/bin/bash` 3.2 (no associative arrays, no `${var,,}`): the style-fix pipeline runs it unattended every ten minutes on both machines, so `agents_config.sh` and `agent_assignments.sh` are never left broken (`docs/as-built/agent-registry.md`).
- Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.
- `config/agents.conf`, `scripts/agents/agents_config.sh`, `agent_admin.sh`, `test_agents_config.sh` and `commands/agent.md` are outside this unit's **Owns**: the checkpoint notice names each as `also touches <path>`.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_add_unit.py'` and `bash scripts/agents/test_agents_config.sh` green; basedpyright 0 errors and 0 warnings on `scripts/production/add_unit.py` and `test_add_unit.py`.
- Live (natedev, once the merge reaches `~/.claude` main): `/agent production` shows `director` at `opus` `xhigh`; the next unit director launched shows `--model opus --effort xhigh` in its `ps` command line.

### Phase 8 — Stall watch treats a unit waiting on someone else as waiting · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** the showrunner (natedev), 2026-10-06 ~19:1x PDT. At 19:0x PDT the stall watch bumped mul_add, whose last line was "— gate: the user runs the Mac Claude go-live and controls at a Mac terminal". `stall_watch.py` resets a unit's stretch only for a last line that starts `done:` or `blocked:`, so a unit waiting on the user under `gate:` or `decision:` is bumped and reported as stalled every stretch.

**Goal:** the stall watch treats a unit whose last turn-end line is `— gate:` or `— decision:` as waiting, exactly as it treats `done:` and `blocked:`: no bump, no notice to the showrunner, and a fresh stretch. `— holding:` stays a stall candidate.

**Spec:**
- **Read first, in full:** `scripts/production/stall_watch.py` (`TURN_END`, the last-line test in the unit loop, `Stretch`, `read_stretch`, `work_running`, and the standby and finished-run skips) and `scripts/production/test_stall_watch.py` (`test_blocked_turn_end_waits_for_a_new_unblocked_status`, `test_holding_turn_end_still_bumps_idle_unit`, `test_done_turn_end_is_never_stalled`).
- **The rule.** A turn-end line is a wait when its kind is `done`, `blocked`, `gate` or `decision`: in each the unit has handed the next move to someone else or has finished. `holding` is the one kind that says the unit itself is waiting on work it has running, so with nothing running it is the stall the watch exists to catch. A unit waiting on the user is already reported to the user (`unit_status.sh` prints `STILL WAITING on you`); the watch adds no second report.
- **One list of kinds.** `TURN_END` already names all five kinds. Build it and the wait test from the same two names in the module, the waiting kinds and `holding`, so the pattern and the test cannot drift apart again. The test reads the kind of the last line the way `TURN_END` does, with or without the leading `— `, and replaces `last.lstrip("— ").startswith(("done:", "blocked:"))`. A pane with no turn-end line on screen (`none on screen`) takes the stall path as today.
- **The reset is the existing one.** A waiting last line writes the fresh stretch and skips the unit, as `done:` and `blocked:` do today; the stretch restarts at the next status that is not a wait, so a unit that answers the gate and then says `— holding:` is judged from that line.

**Files:**
- `scripts/production/stall_watch.py` — the waiting kinds and the last-line test.
- `scripts/production/test_stall_watch.py` — its tests.

**Seats:** 1 writer + 1 tester.
- `impl` — `stall_watch.py`; post `done` without waiting for the test seat.
- `test` — from the Spec alone, in `test_stall_watch.py`, modelled on `test_blocked_turn_end_waits_for_a_new_unblocked_status`: the mul_add line verbatim, `— gate: the user runs the Mac Claude go-live and controls at a Mac terminal`, and a `— decision: …` line each get no bump and no notice at the start and after the stall interval, and a later `— holding: …` line starts the stall from that line; a `gate:` and a `decision:` line without the leading `— ` behave the same; a `— holding: …` line with nothing running is still bumped (the existing test, left as it is); the `done:` and `blocked:` tests stay green unchanged. Each new test fails on the tree before this phase. Owns the final suite run.

**Constraints from prior phases:**
- Phases 4 and 5 (as built): the watch skips a standby unit and a finished run (a Units row whose Plan cell says `run done`) before any bump or notice, and `done:` and `blocked:` reset the stretch; keep all of them.
- `stall_watch.py` and `test_stall_watch.py` are in this unit's **Owns** row (hub files, gate G1 cleared): neither is an `--also` path. Merge `build-followups` into the branch at the phase start.
- Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_stall_watch.py'` green; basedpyright 0 errors and 0 warnings on `scripts/production/stall_watch.py` and `test_stall_watch.py`.
- Live (natedev, once the merge reaches `~/.claude` main): a unit whose last line is `— gate:` or `— decision:` gets no bump across a stall stretch.

### Phase 9 — Dailies input builder · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** Phase 5's script-or-judgment audit (its As-built table): the rows "Build dailies input from `unit_status.sh`", "Restart the update clock after a user-run dailies", "Gather time, LOG, merge state and review watch", "Detect stale or past ETA and request a new one" and "Build-hold markers and holder files". The audit's owner phase for each is this one.

**Goal:** one command, `scripts/production/dailies_input.py`, writes the mechanical half of `dailies_input.json` and merges the showrunner's judgment fields over it, so `commands/showrunner/dailies.md`'s Status check and Gather become one call: the showrunner supplies only what needs judgment (project, goal, phase, `started`, held reason, update, waiting_on_it, needed, then, ETA numbers, held or testing merges), and the command refuses the input it cannot complete, naming each missing field. It first extracts the merge-branch reader that Phases 10 and 12 also use.

**Spec:**
- **Read first, in full:** `commands/showrunner/dailies.md` (Status check and clock, Gather, Input), `scripts/production/unit_status.sh`, `scripts/production/dailies_render.py` (the unit field set, `InputError`, the input parser its command line calls) and `scripts/production/merge_checkpoint.py` (`merge_history`, `code_merged`). The command is the mechanical lines of those steps in the order the text gives them; the judgment lines stay in the text.
- **The merge-branch reader, in `merge_checkpoint.py`.** `merge_history(request)` answers for one unit's last checkpoint only. Extract the parse of the merge subjects on `MERGE_BRANCH` (`Merge <unit> phase <N> (<short>) into <merge branch>`, a shrink merge told from a code merge) into one typed reader over a checkout and a merge branch, with three queries: the last merge of any unit, a unit's last merge, and the count of code merges (a shrink merge never counts, a code checkpoint's first merge does). `merge_history` and `code_merged` call it and keep their behavior, so the existing `test_merge_checkpoint.py` cases stay green unchanged; new cases pin the three queries on a temporary repository. Phases 10 and 12 import the reader; none of the three copies the rule.
- **Command.** `dailies_input.py --production <doc> --status <file> --judgment <file> --state-dir <dir> --out <file> [--length simple|page|elaborate] [--holders <dir>] [--notifier <path>] [--user-run]`. It reads `ZONE`, `LOG`, `MERGE_BRANCH`, `CHECKOUT`, the **Updates** instance name and each unit's session through `add_unit.py`'s `read_production`, `production_field`, `cell_value` and `unit_rows`, as `merge_checkpoint.py` does. It prints `<step>: ok|failed — <one line>` per step and exits 0, or 2 when it refuses; a refusal writes nothing to `--out`.
- **Status.** `--status` is the output of `unit_status.sh <scratch>/unit_status <zone> --showrunner <name>`, saved to a file by the one line `dailies.md` names. A `== <unit>` line opens a unit's block, for a unit in the Units table (any other name is refused, naming it); before the first one only the `at <time> / <time> UTC` line may stand. The structural lines, each copied into the tests from `unit_status.sh`: `SESSION GONE`, `CLAUDE NOT RUNNING`, `TICKS FAILING (…)`, `FORM WAITING on you in <unit>: …`, `BLOCK in <unit>, open <age>: …`, `STILL WAITING on you, <unit>: …`, and the `=== DECISION for you from <unit> ===` … `=== end <unit> ===` block. Every other line in a block is the unit's activity text (the script prints up to five free lines per unit: its latest step, a spinner, a decision, blocked or gate line, and its ETA line), kept as `Activity(text)` and never refused. States, never strings: `SessionGone | ClaudeNotRunning | Running`, and the flags `FormWaiting(text) | Decision(text) | StillWaiting(text) | Block(age, text) | TicksFailing(text)`. It prints `flags first:` lines for what dailies must put first (session gone, Claude not running, form waiting, a decision, a usage limit named in the activity text) and sets `needs_user` true for a form, decision or still-waiting flag the judgment file left unset.
- **Fields it fills.** `length`, `zone`, `unit`, `label` (the renderer's default when absent), `next_run`, each unit's `build_hold` marker, the review-watch topic and the merge-branch topic. `build_hold`: `true` for a unit under a holder file in `--holders` (default `~/.local/state/build-hold/`), a holder's own unit never marked; a marker with no holder file, or holder files with no marked unit, is refused as the renderer refuses it. Review watch: it runs `review_regime.py watch`, shows its line as the `Review watch` topic until it prints `acknowledged`, and exit 3 sets `needs_user`. Merge branch: the last merge from the merge-branch reader and whether `MERGE_BRANCH` is pushed; held or testing comes from the judgment file. `next_run`: the `next_due` of `<notifier> status <UPDATES>`; under `--user-run` it is the `next_due` the clock restart prints (below).
- **ETA age.** The block's ETA line is the unit's ETA text, and its first-seen time is kept in `--state-dir`, as `unit_status.sh` keeps a block's age. Two outcomes, as `dailies.md` Gather item 3 gives them: an ETA text first seen more than an hour ago keeps the ETA, its `detail` set to `set HH:MM` (the time it was first seen), and prints `request /unit:eta: <unit>`; an ETA whose time has passed is not shown: the ETA reads `none measured - requested` and the same line prints. The request prints once per unit and phase. The command holds the ETA as `EtaFresh | EtaStale(first_seen) | EtaPassed | EtaNone` and converts to the renderer's text only at that boundary. The showrunner sends the request; the command only prints it.
- **The clock.** A scheduled tick restarts nothing. Under `--user-run` the command first runs the finished input through `dailies_render.py`'s own input parser (imported, never copied; `next_run` left out), so no renderer refusal can follow a clock move, and only an input that parses continues. It then runs `<notifier> restart <UPDATES>`, takes `next_due` from its output for `next_run` and appends that `next_due` line to `LOG`; a failed restart is `failed` and leaves `--out` untouched.
- **Judgment.** `--judgment` holds, per unit, the fields only the showrunner can write (the Goal lists them), and the other topics. It is merged under the fields above, then the whole is checked against what `dailies_render.py` requires (its unit field set and required fields are the contract; read them, never copy a list); each missing required field is named as `units[N].<field>: required`. `then` is passed through unchanged: a string, or an item that chains two phases, is refused with the renderer's own message and the item's index, naming the judgment file, `--out` untouched; the showrunner fixes the file, runs the command again, and the output then passes the renderer.
- **The doc.** `dailies.md`'s Status check and Gather say to save the status script's output, run the command, run the renderer on `--out`, and write only the judgment file, with the fields it takes from the showrunner named once.

**Files:**
- `scripts/production/dailies_input.py` — the command (new).
- `scripts/production/test_dailies_input.py` — its tests (new).
- `scripts/production/merge_checkpoint.py` — the merge-branch reader; `scripts/production/test_merge_checkpoint.py` — its new cases.
- `commands/showrunner/dailies.md` — Status check and clock, Gather and Input.

**Seats:** 1 writer + 1 tester.
- `impl` — `dailies_input.py`, `merge_checkpoint.py` and `dailies.md`; post `done` without waiting for the test seat.
- `test` — from the Spec alone, in `test_dailies_input.py` and the new cases of `test_merge_checkpoint.py`: status files written from the real line kinds `unit_status.sh` prints (copy each string from its source), one test per named state and flag, a free activity line kept and not refused, an unknown unit refused; a temporary holders directory for each `build_hold` case; a stub notifier on `PATH` that records its argv for `--user-run` and for its absence, and one that fails; a temporary state directory for the ETA age (stale and passed) and the once-per-phase request; a temporary git repository for the merge-branch reader and the merge topic; a judgment file missing a required field, and one whose `then` is a string or a chained item, each refused with the field or index named, `--out` untouched and the notifier never called under `--user-run`; the output accepted by `dailies_render.py` at each length. Owns the final suite run.

**Constraints from prior phases:**
- Phase 5 (as built): `merge_checkpoint.py` reads the production doc through `add_unit.py`'s readers, names its states at the reader boundary (no `str | None` across it) and prints `<step>: ok|held|failed — <line>`; follow all three. This phase's refusals are `failed`.
- Phases 2 and 6 (as built): `then` is a list of one-line items, one per upcoming phase or follow-up; `dailies_render.py` refuses a string, a chained item and unknown fields, so the builder passes `then` through and emits exactly the renderer's field set. Do not edit the renderer.
- `unit_status.sh` stays as it is (a `script (built)` row); the command reads its output and never runs `tmux`.
- Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.
- Both new files are outside this unit's **Owns**: the checkpoint notice names each as `also touches <path>`. `commands/showrunner/dailies.md` is stalls-unit's (its Units row lists it, this unit's does not): the notice also names it as `also touches commands/showrunner/dailies.md (owner stalls-unit), tested against <owner tip>`, after the trial merge of that tip. `merge_checkpoint.py` and its test are this unit's.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_dailies_input.py'`, `-p 'test_merge_checkpoint.py'` and `-p 'test_dailies_render*.py'` green; basedpyright 0 errors and 0 warnings on `scripts/production/dailies_input.py`, `test_dailies_input.py` and `merge_checkpoint.py`.
- Live (natedev, once the merge reaches `~/.claude` main): the next dailies runs `dailies_input.py` and `dailies_render.py` and prints the same report the written-by-hand input gave.

### Phase 10 — Production open and wrap · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** Phase 5's script-or-judgment audit: the rows "Check checkout branch and load production state", "Create merge branch, set running doc, commit plans, push", "Exclude and initialize LOG", "Final CI point and close-out cleanup", "Render the final wrap report" and "Smoke launch, verify CI, promote main".

**Goal:** one command, `scripts/production/production_lifecycle.py`, runs a production's fixed git and document steps at its start and its end, so `commands/showrunner/produce.md`'s <LoadProduction/>, <OpenMergeBranch/>, <PromoteMain/> and <Wrap/> keep only the judgment: whether a unit's close-out needs the owner's approval, and the person's live smoke verdict.

**Spec:**
- **Read `commands/showrunner/produce.md` <LoadProduction/>, <OpenMergeBranch/>, <PromoteMain/> and <Wrap/> in full first,** and `scripts/production/merge_checkpoint.py`'s merge-branch reader (built in Phase 9). Each subcommand is the mechanical lines of one of them, in the order the text gives them; the judgment lines stay in the text.
- **A production without CI.** Every subcommand takes `--no-ci`, which the showrunner passes when the production doc's **Production rules** say CI does not apply (this production's do: no CI, no smoke launch, no Mac run, no `validate_and_push.sh`). With it, the CI, smoke-launch and Mac-run steps of `promote-main` and `wrap` each print `<step>: ok — skipped: no CI` and the rest runs, the promote of main by the doc's **Promote:** field included. `produce.md` keeps that one judgment line beside each call.
- **Subcommands**, each taking `--production <doc>` and printing `<step>: ok|held|failed — <one line>`, the first `held` or `failed` stopping the run, exit 0 or 2:
  - `load` — `CHECKOUT` is on `MERGE_BRANCH`, or clean on the commit the branch will start from, else the message `Run /showrunner:produce in a checkout on <merge branch>.`; on `resume` or a running doc, the last `### STATE` block of `LOG`, `LAST_MERGED` from Phase 9's merge-branch reader (imported, not copied), and each unit's session through `tmux has-session`.
  - `open` — only for status `planned`: creates `MERGE_BRANCH`, sets the doc `running` and its `**Showrunner session:**` line, commits `production(<name>): plans for <n> units`, pushes with upstream, adds `LOG` to the checkout's `info/exclude` and creates it with its heading. A re-run resumes at the first step not done.
  - `promote-main` — the preflight and git steps of <PromoteMain/>, stopping at the live smoke verdict, which stays with the person.
  - `wrap` — the clean and merged checks, the worktree retire, the notifier removal and the doc's status change of <Wrap/>, then the final wrap report rendered from the git, CI and close-out records; a unit whose close-out needs an approval is `held`, naming it.
- **Named states**, never a string or `None` across the reader boundary: `PlanLanded | PlanPending`, `SessionLive | SessionGone`, `FirstMerge | LastMerged(<hash>)` (the last imported from `merge_checkpoint.py`).
- **The doc.** `produce.md` names each subcommand where it names the step and drops the mechanical text it replaces.

**Files:**
- `scripts/production/production_lifecycle.py` — the command (new); `scripts/production/test_production_lifecycle.py` — its tests (new).
- `commands/showrunner/produce.md` — <LoadProduction/>, <OpenMergeBranch/>, <PromoteMain/> and <Wrap/>.

**Seats:** 1 writer + 1 tester.
- `impl` — the command and `produce.md`; post `done` without waiting for the test seat.
- `test` — from the Spec alone: real `git` in temporary repositories with a temporary bare `origin`; a stub `tmux` on `PATH`; one test per subcommand's held and failed branch, for a re-run after a failed push, and for `--no-ci` (each CI, smoke-launch and Mac-run step skipped, the rest run). Owns the final suite run.

**Constraints from prior phases:**
- Phase 5 (as built): the reader, the named states and the step-line form are `merge_checkpoint.py`'s; reuse its functions by import.
- Phase 9 (as built): the merge-branch reader (last merge of any unit, a unit's last merge, the code-merge count) lives in `merge_checkpoint.py`; import it, never copy the merge-subject rule. Phase 9 also adds `dailies_input.py`; this phase does not touch it.
- Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.
- Both new files are outside this unit's **Owns**: the checkpoint notice names each as `also touches <path>`.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_production_lifecycle.py'` green; basedpyright 0 errors and 0 warnings on `scripts/production/production_lifecycle.py` and its test.

### Phase 11 — Update registration · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** Phase 5's script-or-judgment audit: the rows "Register session, prompt, notifier, stall-watch, tmux-names jobs", "Build the scheduled prompt and report next_due", "Footer and outstanding-item rendering, off switch", "Zone time and conversion in every update" and "Event LOG lines and periodic STATE snapshots".

**Goal:** one command, `scripts/production/update_registration.py`, registers the production's update jobs idempotently and writes its time and log lines, so `commands/showrunner/produce.md` <StartUpdates/> and the `Throughout` time and log rules keep only the wording the showrunner chooses.

**Spec:**
- **Read `commands/showrunner/produce.md` <StartUpdates/> and <Throughout/>, and the footer rules in `commands/showrunner/footer.md` if it exists, in full first,** with `scripts/production/dailies_render.py` (`--footer`, `footer_main`), `scripts/hooks/showrunner_footer.py` (`FooterState`, `footer_state`) and the notifier script `produce.md` names as NOTIFIER. The command is their mechanical lines in the order the text gives them.
- **Subcommands**, each with `--production <doc>`, printing `<step>: ok|failed — <one line>`, exit 0 or 2:
  - `register` — the session line in the doc, the update instance, the prompt, the stall-watch job and the tmux-names job through `notifier.sh`. The `UPDATES` instance always gets `new`, on every start and every resume: a repeated `new` retargets it at this session without moving its clock (<StartUpdates/> step 3). The stall-watch and tmux-names jobs are created only when `notifier.sh status` reports no instance. The scheduled prompt is expanded from its template with the interval, the user's zone and this session's name. It prints the instance's `next_due`.
  - `time <ISO or HH:MM>` — converts any stamp to the doc's `ZONE` and prints `HH:MM <zone>`; never UTC.
  - `log "<event>"` — appends `- HH:MM <zone>: <event>` to `LOG` and, on every tenth event and with `--before-compaction`, a `### STATE <time>` block carrying each unit's phase, last merged checkpoint and wait, merges accepted but held, and items open for the user (the last three from `--state <file>`, the showrunner's judgment).
  - `footer` — the footer already renders in `dailies_render.py --footer`, and its on/off switch is `FooterState` in `scripts/hooks/showrunner_footer.py`; this subcommand only calls them. It reads `FooterState` by import (never copied): `FooterState.OFF` prints `footer: ok — footers off` and nothing else, which drops the Waiting on block too (Phase 5 binds this: `produce.md`'s footer rules say footers off drops it); `ON` runs `dailies_render.py --footer` with the doc's zone and the next run, and the outstanding-items file it writes from the open-for-the-user items of `--state <file>`.
- **Named states**, never a string or `None` across the reader boundary: `InstancePresent | InstanceAbsent`. The footer's on and off state is the existing `FooterState`, not a second one.
- **The doc.** `produce.md` names each subcommand where it names the step and drops the text it replaces.

**Files:**
- `scripts/production/update_registration.py` — the command (new); `scripts/production/test_update_registration.py` — its tests (new).
- `commands/showrunner/produce.md` — <StartUpdates/> and <Throughout/>.

**Seats:** 1 writer + 1 tester.
- `impl` — the command and `produce.md`; post `done` without waiting for the test seat.
- `test` — from the Spec alone: a stub `notifier.sh` on `PATH` recording argv for present and absent instances, where a present `UPDATES` instance gets `new` again with this session as its target and a present stall-watch or tmux-names instance gets none; a temporary production doc and `LOG`; the tenth-event block; the zone conversion across midnight and a daylight change; the footer on and off (a temporary state directory for `FooterState`). Owns the final suite run.

**Constraints from prior phases:**
- Phase 5 (as built): the reader, the named states and the step-line form are `merge_checkpoint.py`'s; reuse them by import. `stall_watch.py` reads the notifier conf's `CHECK=` path and the Units rows' Plan cell; do not change what it reads.
- Phases 9 and 10 add their own commands; this phase does not touch them.
- Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.
- Both new files are outside this unit's **Owns**: the checkpoint notice names each as `also touches <path>`.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_update_registration.py'` green; basedpyright 0 errors and 0 warnings on `scripts/production/update_registration.py` and its test.

### Phase 12 — CI points and review watch · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** Phase 5's script-or-judgment audit: the rows "Count the fifth code merge and schedule a CI point", "Run review watch, report, notify, log first exit 3", "Start a CI point and collect the validation and CI result", "Send a gate-clear or gate-lift notice" and "Route arrivals and serialize the checkpoint queue".

**Goal:** one command, `scripts/production/ci_points.py`, counts code merges, starts and collects a CI point, runs the review watch once and drafts the gate-clear and gate-lift notices, so `commands/showrunner/produce.md` <CIPoint/>, <ClearGate/> and <Direct/> keep only the choice of repair owner, the routing of arrivals and the order of a queue.

**Spec:**
- **Read `commands/showrunner/produce.md` <CIPoint/>, <ClearGate/>, <Direct/> and <MergeCheckpoint/> step 6 in full first,** and `scripts/production/merge_checkpoint.py`'s merge-branch reader (built in Phase 9). Each subcommand is the mechanical lines of one of them, in the order the text gives them.
- **A production without CI.** `due`, `ci start` and `ci collect` take `--no-ci`, which the showrunner passes when the production doc's **Production rules** say CI does not apply (this production's do). With it each prints `<step>: ok — skipped: no CI` and does nothing else; `produce.md` keeps that one judgment line beside each call.
- **No `queue` command.** The audit row "Route arrivals and serialize the checkpoint queue" is a judgment row, not a script: an arrival is a message in the showrunner's own session, not a file a script can read, and the one fact worth a lock, one merge at a time, is already `merge_checkpoint.py`'s step lines. This phase's As-built records the row as judgment, with this reason.
- **Subcommands**, each with `--production <doc>`, printing `<step>: ok|failed — <one line>`, exit 0 or 2:
  - `due` — the count of code merges on `MERGE_BRANCH` from Phase 9's merge-branch reader (imported, not copied), and whether a CI point is due on every fifth.
  - `ci start` and `ci collect` — the commands and polling of <CIPoint/> as one transaction, the validation and CI result printed as one line each.
  - `watch` — runs `review_regime.py watch`, prints its report line, and on the first exit 3 only, logs it and prints a notify line; later exit 3 results are not repeated.
  - `notice clear|lift <gate id>` — drafts the `send <unit>:` line for a gate that cleared or lifted, in the form the Gates table and <ClearGate/> give.
- **Named states**, never a string or `None` across the reader boundary: `CiDue | CiNotDue(<count>) | CiNotUsed`, `WatchFirstAlert | WatchRepeat | WatchClear`.
- **The doc.** `produce.md` names each subcommand where it names the step and drops the text it replaces.

**Files:**
- `scripts/production/ci_points.py` — the command (new); `scripts/production/test_ci_points.py` — its tests (new).
- `commands/showrunner/produce.md` — <CIPoint/>, <ClearGate/>, <Direct/> and <MergeCheckpoint/> step 6.

**Seats:** 1 writer + 1 tester.
- `impl` — the command and `produce.md`; post `done` without waiting for the test seat.
- `test` — from the Spec alone: a temporary git repository whose merge subjects hold code and shrink merges; stubs for `gh`, `review_regime.py` and the CI polling on `PATH`; the first and repeat exit 3; each notice's text; `--no-ci` on `due`, `ci start` and `ci collect`. Owns the final suite run.

**Constraints from prior phases:**
- Phase 5 (as built): the reader, the named states and the step-line form are `merge_checkpoint.py`'s; reuse them by import. The merge command's step 9 already records `review_regime.py add`; this phase only reads.
- Phase 9 (as built): the merge-branch reader (the merge-subject rule and the code-merge count) lives in `merge_checkpoint.py`; import it, never copy the rule.
- Phases 9 to 11 add their own commands; this phase does not touch them.
- Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.
- Both new files are outside this unit's **Owns**: the checkpoint notice names each as `also touches <path>`.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_ci_points.py'` green; basedpyright 0 errors and 0 warnings on `scripts/production/ci_points.py` and its test.

### Phase 13 — Waiting, cross-unit search and alerts · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner`, branch `build-followups-enh-showrunner`. State every time in PDT.

**Source:** Phase 5's script-or-judgment audit: the rows "Run a scratch test of a claimed code block", "Inspect berth overlaps and wait age", "Search other units for renamed public items", "Relay quota state to units", "Raise an eight-hour agenda item" and "Request an unmeasured ETA once per phase".

**Goal:** one command, `scripts/production/waiting.py`, does the sampling and searching the showrunner repeats while units wait on each other, so `commands/showrunner/produce.md` <Dependencies/>, <CrossUnitChange/>, <QuotaAlert/>, <Agenda/> and the unmeasured-ETA rule keep only the classification of a wait and the order of a landing.

**Spec:**
- **Read `commands/showrunner/produce.md` <Dependencies/>, <CrossUnitChange/>, <QuotaAlert/>, <Agenda/> and the unmeasured-ETA rule in <Throughout/> in full first,** with the berth board (`cargo-berth board --json`) and the quota protocol <QuotaAlert/> names. Each subcommand is the mechanical lines of one of them, in the order the text gives them.
- **Subcommands**, each with `--production <doc>`, printing `<step>: ok|failed — <one line>`, exit 0 or 2:
  - `scratch-test <waiting unit> --tests <command>` — the gate-lift test of <Dependencies/> rule 1: in a temporary worktree of the waiting unit's branch it merges `MERGE_BRANCH` without the other unit's unmerged work, runs the tests, and prints `scratch-test: ok — green: not blocked` or `scratch-test: failed — red: <last lines>`. The worktree is removed either way; nothing is committed or pushed.
  - `waits` — the berth overlaps and each wait's age from the board and `LOG`, the deadlines each tick samples.
  - `search <old name> <new name>` — every site in the other units' branches and worktrees that names a renamed public item, with the path and line.
  - `quota` — the fixed fanout of a quota alert to each unit and the held-item lifecycle; the account action stays the user's.
  - `agenda` — raises an item once its wait passes eight hours, deduplicated by first open; the options stay the showrunner's.
  - `eta-request <unit>` — once per phase, prints `request /unit:eta: <unit>` for a unit with no measured ETA and records it in `--state-dir`; Phase 9's builder prints the same line and shares this record.
- **Named states**, never a string or `None` across the reader boundary: `WaitOpen(<since>) | WaitCleared`, `EtaRequested | EtaNotYetRequested`, `NotBlocked | StillBlocked(<last lines>)`.
- **The doc.** `produce.md` names each subcommand where it names the step and drops the text it replaces.

**Files:**
- `scripts/production/waiting.py` — the command (new); `scripts/production/test_waiting.py` — its tests (new).
- `commands/showrunner/produce.md` — <Dependencies/>, <CrossUnitChange/>, <QuotaAlert/>, <Agenda/> and the unmeasured-ETA rule.

**Seats:** 1 writer + 1 tester.
- `impl` — the command and `produce.md`; post `done` without waiting for the test seat.
- `test` — from the Spec alone: real `git` in temporary repositories for `scratch-test` (a green and a red test command, the worktree gone after each, nothing committed) and `search`; a temporary `LOG` and board file for `waits` and `agenda`; a temporary state directory for the once-per-phase request; the quota fanout through a stub on `PATH`. Owns the final suite run.

**Constraints from prior phases:**
- Phase 5 (as built): the reader, the named states and the step-line form are `merge_checkpoint.py`'s; reuse them by import.
- Phase 9 (as built): `dailies_input.py` keeps the once-per-phase ETA request record in `--state-dir`; this phase reads and writes the same record in the same form, and never forks it.
- Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere but a temporary bare repository.
- Both new files are outside this unit's **Owns**: the checkpoint notice names each as `also touches <path>`.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest discover -s scripts/production -p 'test_waiting.py'` green; basedpyright 0 errors and 0 warnings on `scripts/production/waiting.py` and its test.
