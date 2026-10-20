# sdlc-roles

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; long-running.** Own the responsibilities of showrunners, unit directors and workers, and the names of things: map who does what and what each thing is called, measure where duties overlap, conflict or have no owner and where one thing has several names, propose changes, then build and measure one approved change at a time.

> **Production: sdlc** — unit `roles-unit`; production doc `docs/plans/sdlc-production.md`

## Source

2026-10-20 16:47 EDT

it will probably need to get promoted to showrunner because it will cover things like this, you know, how we prepare our documentation and design and how the design and documentation process runs and how the responsibilities work across showrunners and directors in order to actually implement and what are the names of things and all of these areas. There's so many moving pieces to our SDLC ... Our SDLC should be a top-level showrunner. It should be a peer of Nate Dev, so we can split our responsibilities and not go as deep ... I want a flat organization.

For active process redesigns, as we are currently undergoing with promotions and unit director renames and responsibility changes and process changes, those should come back to me as questions.

## What exists today (measured 2026-10-20 16:53 EDT by the plan author)

- **Role contracts:** `docs/production_format.md` (468 lines; its **Words** section defines production, executive producer, producer, showrunner, unit, unit director, worker, review pool, reviewer, template, lease, merge branch, gate, and parent, child and root showrunner; <ProductionUnit/> holds the unit director's duties toward the showrunner), `commands/showrunner/produce.md` (1192 lines, 22 tags), `commands/unit/direct.md` (1579 lines), `docs/delegate/*.md`, `commands/showrunner/*.md` (24 files), `commands/unit/*.md` (13 files), `commands/producer/*.md` (2 files), `~/.claude/docs/decision_criteria.md` (what reaches the user).
- **Names in live files** (`commands/`, `docs/` without `docs/plans/`, `skills/`; case-insensitive word matches, paths included): `worker` 729 hits in 59 files, `seat` 52 in 7, `unit director` 228 in 49, `delegate` 320 in 55 (`/unit:delegate` is the old name of `/unit:direct`; `docs/delegate/` and `plan-delegate` state keep it), `merge branch` 130 in 22, `trunk` 77 in 11, `orchestrator` 20 in 6, `helper` 57 in 23, `subagent` 56 in 9, `executive producer` 4 in 2.
- **Roles with no defining file:** the small-fix helper (memory `small-fixes-by-subagent-in-main`), the chief designer and the chief reviewer (planned; sdlc production, Guild heads), the nightly review, the postmaster and the notifiers.
- **One conflict already seen:** **Words** says a showrunner "reports on a schedule"; every production's Updates line and the user's 2026-10-13 rule say updates come on demand.
- **hierarchies-unit (build-followups)** has a design doc in progress, `docs/plans/build-followups-hierarchies-design.md` on branch `build-followups-hierarchies`. Its **Map** lists each role with its defining file, its work and who it reports to, and each channel. It owns the structure: which productions and showrunners exist, who decides priority, how work is admitted, how capacity is shared and how the user is reached. Its plan says a step that changes a responsibility or a name goes to roles-unit.

## Decisions

- **Every change to a responsibility or a name is a question for the user** (user, 2026-10-20: "those should come back to me as questions"). This unit maps, measures and proposes; the showrunner brings each proposal to the user; no live command, doc or script changes a duty or a name before the user approves it. Measuring and writing the design doc need no approval (plan author).
- **Scope beside the other units** (production doc, Scope of each unit; hierarchies-unit plan, Scope beside sdlc): this unit owns who does each duty among the user, producers, showrunners, unit directors, workers and helpers, and the names of roles, documents, steps and records. hierarchies-unit owns the structure and this unit reads its **Map** and does not copy it: the duty matrix here refers to its role rows. guilds-unit owns the designer and reviewer roles and their duties; this unit lists them as guilds-unit defines them. cargo-liner picks the guild head names with the user; this unit uses `astra-designer` and `astra-reviewer` until then. phases-unit owns the steps of a phase and the doc pipeline; this unit owns only who runs each step and what it is called. fix-rounds-unit owns the repair round rules.
- **Hub files** (production doc, Hub files): this unit owns `docs/production_format.md` and `commands/showrunner/produce.md`, and merges the edits guilds-unit and fix-rounds-unit send for them. An edit to `commands/unit/direct.md` goes to phases-unit, its owner. All three are also build-followups hub files: an edit goes through cargo-berth's normal flow and the checkpoint notice names the build-followups owner.
- **One design doc** holds the duty matrix, the term table, the baseline, the friction, the candidates and the steps: `docs/plans/sdlc-roles-design.md` (plan author). It becomes the as-built at run end.
- **One measure script, run again after each step** (plan author): `scripts/production/roles_audit.py`. It sits in `scripts/production`, which the merge gate and `basedpyright scripts/direct scripts/production` already cover, so it needs no new suite or pyright entry. Its term list is data, `scripts/production/role_terms.toml`, so a name the user approves is one line. A step counts as done only when its measure moved.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions. This plan maps the duties of each role and the names of things, measures their overlaps and other names, and changes them one approved step at a time.
- **Project started:** 2026-10-20T20:55:49.927+00:00
- **Stack:** Python 3.13, standard library only (`tomllib` for the term list); Markdown.
- **Layout:**
  - `scripts/production/roles_audit.py`, `scripts/production/test_roles_audit.py` — the measure script and its tests (new)
  - `scripts/production/role_terms.toml` — the term list (new)
  - `docs/plans/sdlc-roles-design.md` — the design doc (new)
  - read only, outside the worktree: every production doc that `scripts/production/showrunners.py list` prints, `~/.local/state/message/log.jsonl`, the memory folders under `~/.claude/projects/*/memory/`, hierarchies-unit's design doc (`git -C /home/natepiano/worktrees/claude-build-followups-hierarchies show HEAD:docs/plans/build-followups-hierarchies-design.md`)
- **Key files:**
  - `docs/production_format.md` — **Words** and <ProductionUnit/>: the role words and the unit director's duties to the showrunner
  - `commands/showrunner/produce.md` — the showrunner's duties (<DesignAuthority/>, <Notify/>, <CrossUnitChange/>, **Rules**)
  - `commands/unit/direct.md`, `docs/delegate/*.md` — the unit director's and the workers' duties (<LongLivedWorkers/>, <WorkerRuns/>)
  - `commands/producer/greenlight.md`, `to_production.md` — the producer's duties
  - `commands/showrunner/*.md`, `commands/unit/*.md` — each helper command's actor
  - `~/.claude/docs/decision_criteria.md` — what reaches the user
  - `scripts/production/showrunners.py` (`list`, `tree`), `scripts/production/units_table.py` — live productions and units
- **Test lanes:** `scripts/production/` — `test_*.py` beside the script.
- **Test:** `python3 -m unittest discover -s scripts/production -p 'test_roles_audit.py'`, from the worktree root. Gate: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` from the worktree root (memory `shared-module-full-gate`).
- **Lint:** `basedpyright scripts/direct scripts/production` passes when its output has the `0 errors, 0 warnings` line; the exit status says nothing.
- **Invariants:**
  - Measuring reads every source read-only. It sends no message, push or alert, and starts no session (plan author: a measure must not add traffic).
  - Tests use fixtures; they never read the real `~/.local/state`, `~/rust/hanadocs`, memory folders or transcripts.
  - Each number states its window or its file set, and its count.
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - User-facing times use `scripts/lib/user_zone.py` (America/New_York, user 2026-10-12); log stamps stay UTC.
  - All text uses Simplified Technical English (user, 2026-10-18).
  - No step changes a live duty or name before the user approves it (user, 2026-10-20).

## Phases

### Phase 1 — One command counts each name and its other names, and the design doc maps who does each duty  · status: done

#### As-built

- `roles_audit.py baseline [--root DIR] [--since DATE] [--until DATE]` reads natedev's records. The default root is the repository. The default window is the last 14 days. It prints one aligned table per group, and each row shows its file set or window and n.
- The tables are: names and other names in live files (`commands/`, `docs/` without `docs/plans/`, `skills/`, `CLAUDE.md`, and the string literals of `scripts/`), with a total row for the other-name share; definitions (terms with no definition, terms defined in more than one file); production unit and session names; and sent contacts per recipient in the window, with request intent `not recorded`.
- `role_terms.toml` holds 36 terms. Each term has `name`, `kind`, `defined_in`, `other_names`, `kept` (paths and command names that do not count) and `other_meaning_files` (files where the word has an unrelated meaning).
- The design doc has these sections: Sources, Duty matrix (D01–D52; the designer and reviewer rows are marked planned), Term table, Baseline, Friction (18 lines), Questions for the user (Q1–Q8) and Next steps.
- **Words** in `docs/production_format.md` and the `description` of `commands/showrunner/produce.md` say the showrunner reports when asked.

**Files:**
- `scripts/production/roles_audit.py` — the measure
- `scripts/production/test_roles_audit.py` — 18 fixture tests
- `scripts/production/role_terms.toml` — the term list
- `docs/plans/sdlc-roles-design.md` — the design doc

**Binds later work:** The measure command is `~/.claude/scripts/lib/py scripts/production/roles_audit.py baseline [--root DIR] [--since DATE] [--until DATE]`. Wrong-party asks are not measurable. The message log has no role or intent fields, so the tool counts sent contacts per recipient only. A real wrong-party measure needs role fields in message records. Python and shell counts are lower bounds. Work that depends on an answer to Q1–Q8 marks that dependency.

**Gotchas:** The tool follows Python output references in lexical scope only. It does not follow helper call arguments into parameters; the report header says so. It does not count shell `cat >&2 <<EOF` stderr heredocs. The term `change` also means menu actions in some files; `other_meaning_files` excludes those files.

### Phase 2 — The design doc recommends who owns each duty and what each thing is called, split into steps  · status: done
**Depends on:** Phase 1

#### Work Order

**Goal:** the design doc proposes candidate changes for each `conflict`, `duplicate` and `no owner` duty and each term with other names, recommends the best, and splits them into small steps, each with the measure it moves.

**Spec:**
- Section **Candidates.** At least two candidates for each of: where duties are written down, which role owns each conflicting or ownerless duty, and how names stay one per thing. Seeds, not limits:
  - **One duty per place:** each duty is stated once, in its owner's command file, and other files link to it; **Words** in `docs/production_format.md` defines each role by its duties in one line.
  - **One term list:** one file holds the only definition of each term, and every other file links to it; `role_terms.toml` is made from that file, or is that file.
  - **A name check in the merge gate:** `roles_audit.py check` fails when a live file uses an other name the term list marks `retired`.
  - **Renames by kind:** retire `seat` for worker, `trunk` for merge branch and the `/unit:delegate` alias, each with a list of the files it changes.
- Every candidate keeps the rules the user set: duty and name changes are questions (Decisions); a flat organization with sdlc, the chief designer and the chief reviewer as top-level peers (production doc, Production rules); the three gods and the decision economy (`decision_criteria.md`); the scope lines beside hierarchies-unit, guilds-unit, phases-unit and fix-rounds-unit. A candidate that changes the structure names hierarchies-unit as its owner and goes there.
- Section **Comparison.** One table: each candidate scored on each Phase 1 measure (expected direction and size, with the reason) and each rule (kept or broken). A candidate that breaks a user rule is out.
- Section **Recommendation.** The best set of candidates and why, in at most ten lines.
- Section **Steps.** The change from today to the recommendation as numbered steps. Each step: what changes (files, duties and names), the measure it should move and by how much, how to undo it, and that it needs the user. Step 1 is the one with the largest expected effect per unit of work.
- Before the gate, the unit director runs `/adversarial` on the recommendation against the runner-up and `/astra_review` on the doc, and folds their findings into the doc.

**Files:**
- `docs/plans/sdlc-roles-design.md` — new sections above

**Workers:** `1 writer + 1 tester` — nothing splits: one doc. `impl` takes it.
- `impl` — `docs/plans/sdlc-roles-design.md`
- `test` — opens as impl — re-runs `roles_audit.py baseline` and checks each number the doc cites against it, and each Steps file list against the files the measure names; it writes no doc text

**Constraints from prior phases:**
- The duty matrix (D01–D52), the term table, the baseline, the friction (18 items) and the questions Q1–Q8 are in the design doc. The questions went to the showrunner on 2026-10-20 17:50 EDT. Do not wait for the answers: write each answer that has arrived into the doc, and mark each candidate that depends on an open question with its Q number.
- The message log has no sender role, recipient role or request intent. `roles_audit.py baseline` prints sent contacts per recipient with intent `not recorded`. No candidate is scored on wrong-party asks: score it on sent contacts, or say that it needs role fields in message records first.
- `roles_audit.py` counts Python names in their lexical scope only and does not follow helper call arguments; it does not count shell `cat >&2 <<EOF` stderr heredocs. Shell and Python counts are lower bounds; say so beside any number taken from them.
- Run the measure with `~/.claude/scripts/lib/py scripts/production/roles_audit.py baseline` from the worktree root.
- Phase 1 read hierarchies-unit's **Map** from its uncommitted draft. The committed Map is `git -C /home/natepiano/worktrees/claude-build-followups-hierarchies show d0b07aec:docs/plans/build-followups-hierarchies-design.md`, section **Map** (also on main as merge 83a0ad28). Check every duty matrix row that cites the Map against it and correct the row first. A later Map change arrives as a new pointer from the showrunner.
- fix-rounds-unit checked the repair-round rows against `docs/production_format.md` <ProductionUnit/> item 7 (2026-10-20). Fold in before the new sections: D26's primary is "Top-level showrunner for the early landing call; Unit director when another unit waits on the phase". Add three rows, owner Unit director, source PF item 7: "A fix that fails twice" (find the cause, write it in the attempts log, then one pass with a new approach); "One batch per check" (each live check or shot set after a fix covers every changed view, and all its findings go to the next round together; also direct.md <UXReview/>, <FixDispatch/>); "Own change only" (the unit director sends a defect from before the phase to the showrunner, and the showrunner routes it to the owning unit). Rule changes stay with fix-rounds-unit.

**Acceptance gate:** the doc has the four sections; each candidate has a score on every measure and rule; each step names its measure, its size and how to undo it; the adversarial and astra findings are folded in. At phase end the unit director sends the showrunner the Recommendation and Step 1 as one question for the user, with the answers it can build and the one it recommends.

### Phase 3 — Build the first approved step  · status: todo
**Depends on:** Phase 2, and the user's approval of Step 1

#### Work Order

**Goal:** the step the user approved is live, and its measure is recorded before and after.

**Spec:**
- Before dispatch, the unit director copies the approved step from the design doc's Steps into this Spec verbatim (files, the change, the measure, how to undo it) and fills **Files** and **Workers** from it. A phase with no approved step does not dispatch: it waits on the showrunner's answer.
- Record the step's measure with `roles_audit.py baseline` before the change, into the design doc under **Step log**.
- An edit to `commands/unit/direct.md` goes to phases-unit as a change request through the showrunner; this phase does not edit it. Edits to `docs/production_format.md` and `commands/showrunner/produce.md` merge with any pending edits from guilds-unit or fix-rounds-unit that touch the same tags; the Work Order names each.
- A rename changes every live file the measure names in one phase, and adds the old name to `role_terms.toml` as `retired`.

**Files:**
- `docs/plans/sdlc-roles-design.md` — Step log
- `scripts/production/role_terms.toml` — when the step changes a name

**Workers:** filled from the approved step.

**Constraints from prior phases:** the Steps section and the user's answer.

**Acceptance gate:** the gates in Delegation Context green; the change is live on main per the production's rule for `~/.claude`; the Step log has the before measure, the command and its file set or window.

### Phase 4 — Measure the first step and propose the next  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** the design doc says whether Step 1 moved its measure, and the next step goes to the user.

**Spec:**
- Run `roles_audit.py baseline` after the change, on the same file set; for a measure with a window (sent contacts per recipient), the after window is at least 3 days from the merge. Wrong-party asks are not measurable until message records carry roles; do not report them. Add the after numbers to the Step log with n for each.
- If the measure did not move by at least half the expected size, the Step log says so, names the likely reason, and proposes undoing it or changing it; that proposal is the question.
- Otherwise update Steps with what the measure taught, and pick the next step.
- At phase end, the unit director sends the showrunner the result and the next step as one question for the user. The next approved step becomes a new phase after this one, with the Work Order of Phase 3 and the step's own Spec.

**Files:**
- `docs/plans/sdlc-roles-design.md` — Step log, Steps

**Workers:** `1 writer + 1 tester` — one doc. `impl` runs the measure and writes the Step log.
- `impl` — `docs/plans/sdlc-roles-design.md`
- `test` — opens as impl — re-runs the measure and checks each number in the Step log; it writes no doc text

**Constraints from prior phases:** the Step log before measure and the change Phase 3 shipped.

**Acceptance gate:** the Step log has before and after numbers with n and file set or window; the question is sent.
