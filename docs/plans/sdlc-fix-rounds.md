# sdlc-fix-rounds

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A true count of repair rounds per phase, a study of the phases with the most rounds, and process changes that cut rounds and still deliver the work.

> **Production: sdlc** — unit `fix-rounds-unit`; production doc `docs/plans/sdlc-production.md`

## Source

2026-10-20 16:47 EDT

One of the unit directors in the SDLC should be dedicated to the prospect of analyzing the phases that have the most number of fixed rounds. And when that happens, to be able to like propose improvements that will limit the fixed rounds and yet still achieve the goals of the coding. So we know how to stop ourselves and still get the work done that we need to get done. So it could prioritize the ones with the most fixed rounds as a starting point for measuring impact.

For active process redesigns, as we are currently undergoing with promotions and unit director renames and responsibility changes and process changes, those should come back to me as questions.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, docs and Python scripts that run every production. This unit measures repair rounds per `/unit:direct` phase, studies the phases with the most, and proposes process changes.
- **Project started:** 2026-10-20T20:56:57.500+00:00
- **Stack:** Python 3.13, standard library only; basedpyright strict (`pyrightconfig.json`); unittest. Markdown for commands and docs.
- **Layout:**
  - `scripts/history/history.py` — the `/history` CLI (1,160 lines). It reads every `~/.local/state/<skill>/runs/*.jsonl` store and writes nothing. `phases` (`_phase_rows` :873, `_cmd_phases` :907) counts fix *passes* per `(run_id, phase_id)`; `compare` (:986) compares a window before a date with the window after; `summary` (:1041) names the fix tail.
  - `scripts/history/rounds.py` (new, Phase 1) — the repair-round measure; `history.py rounds` calls it.
  - `scripts/history/test_rounds.py` (new, Phase 1) — its tests.
  - `docs/plans/sdlc-fix-rounds-study.md` (new, Phase 2) — the study and, from Phase 3, the proposals; tracked, and committed with each phase that changes it.
  - `commands/history.md` — the `/history` command doc; lists the subcommands.
  - `scripts/production/merge_gate.py` :47 — `TEST_SUITES`; `history` is not in it yet.
- **Key files:**
  - `~/.local/state/plan-delegate/runs/*.jsonl` — run records, one file per `/unit:direct` run (206 files, 2026-09-04 to 2026-10-20, 92 MB). One JSON event per line. Events this unit reads:
    - `run_started` — `run_id`, `plan_doc`, `project_plan_doc`, `worktree`, `branch`, `working_dir`, `main_agent`.
    - `phase_started` — `phase_id`, `phase_instance_id`, `phase_title`, `phase_started_at`, `work_order_words`, `work_order_lines`, `work_order_file_targets`.
    - `pass_started` / `pass_finished` — `pass_kind` (`impl|test|fix|review`), `pass_activity` (free text, e.g. "repairing Phase 7 review findings"), `fix_pass`, `team_slot`, `called_agent`, `called_task`, `pass_elapsed_seconds`, `phase_id`, `phase_instance_id`, `run_id`.
    - `activity_started` / `activity_finished` — `activity_label`, `activity_text`, `activity_result`, `activity_elapsed_seconds`.
    - `finding_opened` (`finding_id`, `severity`, `lens`, `caught_by`, `file`, `title`), `finding_verdict` (`fix_attempts`, `reopen_count`), `finding_gate` (`round`, `advisory`, `open_counts`), `finding_batch_dispatched` (`round`, `covered`, `gating_open_before`, `gating_severities`, `advisory`), `finding_batch_landed` (`round`, `landed`). Ledger events carry `phase_instance_id` but not `run_id`: join them to their run through the `phase_instance_id` of that run's `phase_started`.
  - `scripts/direct/findings.py` — the ledger: round 1 gates blockers and minors, later rounds blockers only; convergence advisories never stop a round. Limits in `config/delegate.conf` (`MAX_FIX_ATTEMPTS`, `STALLED_ROUNDS`, `REPAIR_ROUNDS_PER_FINDING=0.5`, `MIN_REPAIR_BUDGET`, `RUNAWAY_ROUNDS=5`).
  - `scripts/direct/progress_history.py` :4636 `_review_trial` — joins UX findings to ledger rounds through `covered` and to fix passes through `fix_pass`; the model for the joins Phase 1 uses.
  - `docs/production_format.md` → <ProductionUnit/> item 7 (:343–381) — the current repair rules: own change only, landing rule after round 2, hard landing from round 3 (user, 2026-10-01: "eleven repair rounds on one phase is way too many"), one batch per check, a fix that fails twice.
  - `commands/unit/direct.md` → <FindingsLedger/> (:520–560), <FixDispatch/> (:1056–1160) — how a round is gated and dispatched.
  - `~/.claude/data/review_regime.jsonl` — one row per merged phase: holds, merge design-check defects, review and repair minutes. The goal-kept measure (Phase 5) reads it with `scripts/production/review_regime.py report`.
  - Production logs `docs/plans/*-log.md` in each showrunner checkout, attempts logs `~/.claude/projects/*/memory/attempts-*.md`, and plan docs in git history (`git log -p -- <plan>`) — the Phase 2 evidence for why a round ran.
- **Measured facts (2026-10-20, `history.py phases --since 60d --sort fix_passes --status all`):** 2,896 fix passes over 839 phases, but only 1,621 ledger rounds (`finding_batch_dispatched`). Fix time is 41% of all agent time; 188 phases (11%) with ≥4 fix passes hold 75% of fix time. The top record, run `189487ea` phase `6` ("Palette rows highlight the matched letters", rich-text-plan), shows 70 fix passes and 2 ledger rounds over 8 days. Its pass activities name Phases 7 and 14 and merge work, so the record holds work from several phases. A fix-pass count is therefore not a repair-round count, and a `(run, phase)` record is not always one phase.
- **Test lanes:** `scripts/history/` — `test_*.py` beside the module (none yet; Phase 1 adds `test_rounds.py`); markdown files — none.
- **Build:** none (Python and markdown).
- **Test:** `python3 -m unittest discover -s scripts/history -p 'test_*.py'` from the worktree root. Then the merge gate from the worktree root: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only`.
- **Lint:** `basedpyright scripts/history` from the worktree root; pass is its `0 errors, 0 warnings` line (exit status says nothing).
- **Invariants:**
  - `/history` reads only; it never writes a run record (`commands/history.md`, repository rule).
  - Process changes are the user's: every proposal reaches the user as a question through the showrunner, with the buildable answers and the one recommended (production rule, the user 2026-10-20). Tooling and analysis are this unit's.
  - Deferred work is never dropped: a finding moved out of a phase goes to a follow-up phase with its own Work Order (<ProductionUnit/> item 7, user 2026-09-28 and 2026-10-01).
  - Hub files go through cargo-berth's normal flow: `commands/unit/direct.md` (owner phases-unit), `docs/production_format.md` (owner roles-unit). Both are also build-followups hub files (production doc).
  - All text uses Simplified Technical English (user, 2026-10-18).

## Phases

### Phase 1 — A true count of repair rounds per phase  · status: done

#### As-built

- `history.py rounds` lists `/unit:direct` phases by repair rounds, most first. Columns: `run  phase  title  plan  rounds  review-ux  review-code  gate  merge  live  other  findings  fix-time  span  check`; `--format json|csv`; flags `--since`, `--until` (on `rounds` only), `--status`, `--top N` (default 25), `--plan <substring>`. Summary lines: phases counted, median rounds, share of phases with 3 or more rounds and the share of fix time they hold, rounds by cause, count of `mixed` records. The JSON row also carries `past_landing`, the number of rounds from round 4 on.
- A repair round is the set of concurrent `fix` passes of one `phase_instance_id`; a ledger round (`finding_batch_dispatched`) matched by `round == fix_pass` is the same round. Ledger events join their run through `phase_instance_id`.
- Cause, first match wins: `review-ux` / `review-code` (a ledger round; `ux` when any covered finding's lens holds `ux`), `gate`, `merge`, `live` (a live, shot or smoke activity finished within 30 minutes before), `other`.
- A record is `mixed` when a pass activity names another phase number, its passes span more than 72 hours, or its merge rounds are more than 24 hours apart. A `mixed` record is listed and marked, never dropped.
- The merge gate runs the `history` suite.

**Files:**
- `scripts/history/rounds.py` — the round builder, cause classifier, record check and the `rounds` command.
- `scripts/history/history.py` — the `rounds` subparser; the `summary` hint names `history.py rounds`.
- `scripts/history/test_rounds.py` — tests on synthetic event lists.
- `commands/history.md` — one line for `rounds`.
- `scripts/production/merge_gate.py`, `scripts/production/test_merge_gate.py` — `history` in `TEST_SUITES` and `EXPECTED_SUITES`.

**Binds later work:** `rounds.py` is an importable API with no printing outside `run(args)`: `load_phases(events: list[dict[str, object]]) -> list[PhaseRounds]`, `phase_rounds(events) -> list[Round]`, and the `PhaseRounds` / `Round` TypedDicts. phases-unit's `phase_process.py baseline` imports it. The word lists are the module constants `GATE_WORDS`, `MERGE_WORDS`, `LIVE_WORDS`; `MERGE_SEPARATION_SECONDS = 24 * 3600`.

**Gotchas:** passes sort by start with unfinished passes last, and a pass with no finish never extends a round, so counts do not depend on event order.

**Ruled out:** `--until` on `runs` (it ignored the flag); marking a record `mixed` because two merge rounds name different merges (merge texts vary).

### Phase 2 — Why the phases with the most rounds needed them  · status: done

#### As-built

- `docs/plans/sdlc-fix-rounds-study.md` gives, for the 15 phases with the most repair rounds, why each round ran. Its head notes the `history.py rounds --since 60d --status all --format json` command, its date and its summary lines.
- Each phase section gives plan, phase title, rounds, findings and fix time, then one line per round: its cause from the round classifier, its root cause, and its evidence.
- Root causes are `spec-gap`, `target-moved`, `out-of-scope`, `fix-failed`, `fix-regressed`, `serial-check`, `mechanical` and `record`.
- The 15 ranked records hold 255 rounds and 975,069 agent-seconds of fix time, as counted by the batch join of this phase. The study also splits 3 `mixed` records by hand into their phases.
- A rule check gives, per phase, the rounds past the hard landing and whether their findings were the kind item 7 lets stay. Totals tables give rounds and fix time by root cause over the 15 phases, and by classifier cause over the full 60 days.
- A separate section covers the cargo-liner `/astra_review` design-doc rounds (8, 6, 5 findings; `fix-regressed` / `target-moved`). It stays out of the run-record totals.
- `LIVE_WORDS` in `scripts/history/rounds.py` includes "ux review". No other classifier change.

**Files:**
- `docs/plans/sdlc-fix-rounds-study.md` — method, 15 phase sections, 3 `mixed` splits, rule check, totals, Astra reference.
- `scripts/history/rounds.py`, `scripts/history/test_rounds.py` — `LIVE_WORDS` gains "ux review", with a test.

**Binds later work:** `rounds.py` joins ledger batches by `fix_pass` number alone. When numbers restart inside one instance (8f4c2e88/38, 7b115092/137), later rounds get the findings of an earlier batch. The batch join fix corrects this, and the study's counts change with it. The proposals cite the study's root totals and wait on that fix.

**Gotchas:** Root labels are hand assessments. A row that only verifies is `record`. `serial-check` needs a trace of one defect per check; no normal record shows that pattern.

**Ruled out:** a wider `mechanical` root, and `serial-check` for batched checks — both blur the defined roots.

### Phase 3 — Each repair round joins only its own ledger batch  · status: done

#### As-built

- `_rounds` in `scripts/history/rounds.py` joins each `finding_batch_dispatched` event to at most one fix-worker group: the first group, in start order, that holds a fix pass with the dispatch's round number and a start at or after the dispatch time. A pass inside a group that is already open counts.
- Each dispatch joins once. A dispatch that no pass takes is its own zero-time ledger-only round, also when ledger round numbers repeat. A group with no batch keeps the word-list causes.
- `load_phases`, `phase_rounds` and every `Round` / `PhaseRounds` field are unchanged.
- The study is recounted: the 15 normal records hold 260 rounds and 975,069 agent-seconds (fix-failed 94, spec-gap 43, mechanical 39, record 35, fix-regressed 30, out-of-scope 12, target-moved 7, serial-check 0); the 3 mixed records hold 139 rounds; the full 60-day window holds 2,287 rounds, 24 more ledger-only rounds than before.

**Files:**
- `scripts/history/rounds.py` — round classifier and the time-ordered batch join.
- `scripts/history/test_rounds.py` — synthetic join tests, `load_phases` checks and `test_json_cli_reports_each_batch_once_after_fix_numbers_restart`.
- `docs/plans/sdlc-fix-rounds-study.md` — corrected round rows, root totals, the dispatch-join correction section and the full-window cause table.

**Binds later work:** Any work that copies the study cohort uses 260 rounds and the root counts above; the set of roots at or above 10% of rounds is unchanged. Recounts use the corrected join.

**Gotchas:**
- A ledger-only round has zero worker time by design; never invent time for it.
- A dispatch can come after the last pass with its number (run 7b115092 phase 137 batch 3); it stays a ledger-only round.
- The fix moves many rounds from review causes to gate and other, because restarted numbers gave review batches to unrelated passes.

**Ruled out:** Joining by fix-pass number alone — restarted numbers give one batch to several rounds.

### Phase 4 — Proposals to the user that cut rounds and keep the work  · status: done
**Depends on:** Phase 2, Phase 3

#### Work Order

**Goal:** the study ends with ranked proposals, each sent to the user as a question through the showrunner, each with the rounds it would have saved and how the deferred work still gets done.

**Spec:**
- Add `## Proposals` to `docs/plans/sdlc-fix-rounds-study.md`. One proposal per root cause that holds at least 10% of the studied rounds, most rounds saved first; at most six.
- Each proposal has:
  - **Change:** the rule or tool change, in the words it would have in its target file (`commands/unit/direct.md`, `docs/production_format.md` item 7, `scripts/direct/findings.py`, `config/delegate.conf`, a Work Order rule in `docs/delegate_plan_format.md`, or a `/plan:to_phased_plan` check). Name the file and section.
  - **Rounds saved:** applied by hand to the 15 studied phases: rounds that would not have run, and fix time that would not have been spent. Count only rounds the change removes for certain; a round it might remove counts as zero.
  - **Work kept:** where each finding the change stops repairing goes (follow-up phase, owning unit, routed to the showrunner), and what tells us it got done.
  - **Risk:** what a defect could reach the user or a merge because of it.
  - **Measure:** the `history.py rounds` and `review_regime.py report` numbers Phase 6 compares, with the target for each.
  - **Answers:** the buildable answers (adopt, adopt with a named change, do not adopt) and the one recommended.
- File each proposal as a hanadocs issue through `/issue` with tag `proposal`, `proposed_by: fix-rounds-unit`, and its five ranking values on a `Proposed ranking:` line (<ProductionUnit/> item 17).
- Send the proposals to the showrunner as one message: per proposal, the question, the answers and the recommendation, its issue path, and the study path. Name it as a process change for the user (production rule). Then write the user's answers into Phase 5's Spec when they arrive.
- A proposal that only changes this unit's own tooling (`scripts/history/`) is not a process change: build it in Phase 5 without a question, and state it in one line.

- A change that only corrects the record (a `record` row, a cause label) gets no work-saving credit unless the work itself goes away; both windows of Phase 6 use the same corrected measurement.
- Each proposal states, beside its round target, the work-kept margins Phase 6 judges it by, the sample it needs, and that missing follow-up or review evidence makes the result inconclusive, not a pass.
**Files:**
- `docs/plans/sdlc-fix-rounds-study.md` — `## Proposals`.

**Workers:** `2 writers`
- `impl` — `docs/plans/sdlc-fix-rounds-study.md`: the proposals.
- `test` — opens as `impl`, writes nothing: recounts each proposal's **Rounds saved** from the study's round lines and sends `impl` each count that differs, with the round lines it read.

**Constraints from prior phases:**
- From Phase 2: `docs/plans/sdlc-fix-rounds-study.md` holds the evidence: 15 non-mixed records, 260 rounds, 975,069 agent-seconds (270.85 h). Roots by rounds (share of fix time): fix-failed 94 (48.5%), spec-gap 43 (19.1%), mechanical 39 (11.0%), record 35 (5.5%), fix-regressed 30 (8.1%), out-of-scope 12 (6.8%), target-moved 7 (1.0%), serial-check 0. These are the counts after the batch-join fix. Its `## Totals` and `## Rule check` sections are the base each proposal cites.
- From Phase 2: no normal record shows the one-defect-per-check pattern; the study treats that as a limit of the evidence, not proof. `fix-failed` counts interrupted continuations and failed gate batches as well as rejected patches. Spec-gap rows name a missing target or test case in the start Work Order; they are inferences, not proof that the test would have stopped the repair.
- From Phase 2: the three largest mixed records (189487ea/6, 03f9b80e/20 and 8f4c2e88/38) are outside the totals; the Astra reference case is outside them too.
- From Phase 3: proposal measures use the `history.py rounds` numbers after Phase 3's batch-join fix, not the earlier Phase 1 cause shares.

**Acceptance gate:** every proposal has all six parts; each **Rounds saved** matches the recount; the message to the showrunner is sent, and the turn ends `— decision:` on the user's answers.

### Phase 5 — Apply the changes the user approved  · status: todo
**Depends on:** Phase 4

#### Work Order

**Pending decision: which proposals the user adopts, and in what form**

Actual problem:
Phase 4 sends the proposals to the user through the showrunner. This phase builds only what the user adopts, and the files it edits follow from those answers.

What exists now:
- The repair rules in `docs/production_format.md` item 7, `commands/unit/direct.md` <FindingsLedger/> and <FixDispatch/>, and the limits in `config/delegate.conf`.

What should change:
- The adopted proposals' **Change** text goes into those files, through cargo-berth for the two hub files.

Recommendation:
Write the user's answers into this Spec, Files and Acceptance gate when they arrive, then dispatch.

**Goal:** the adopted changes are in the files `/unit:direct` runs from, merged to main, and the date they took effect is recorded for Phase 6.

**Spec:**
- One edit per adopted proposal, in the words the user approved.
- Edits to `commands/unit/direct.md` (owner phases-unit) and `docs/production_format.md` (owner roles-unit) go through cargo-berth's normal flow; the checkpoint notice names each as `also touches <path> (owner <unit>), tested against <owner tip>`, and names the build-followups owner too.
- Tooling proposals in `scripts/history/` need no question.
- Add a line to the head of `docs/plans/sdlc-fix-rounds-study.md`: `Changes took effect: <checkpoint hash> on main, <date>`. Phase 6 compares from that date.

- When the user adopts no process change, record `No process change adopted` at the head of the study with the date, mark this phase and Phase 6 done with that reason, and build only the tooling-only items Phase 4 named.
**Files:** set by the decision.

**Workers:** set by the decision.

**Constraints from prior phases:** set at Phase 4's checkpoint.

**Acceptance gate:** set by the decision; at least the merge gate green and `basedpyright scripts/direct scripts/production scripts/history` `0 errors, 0 warnings`.

### Phase 6 — Did the changes cut rounds and keep the work  · status: todo
**Depends on:** Phase 5
**Blocked by:** 30 phases with at least one repair round recorded after the date Phase 5 records, counted by `history.py rounds --since <date> --status all`.

#### Work Order

**Goal:** `docs/plans/sdlc-fix-rounds-study.md` ends with a before-and-after table per adopted change, a verdict on each, and any revert sent to the user as a question.

**Spec:**
- Before window: the 30 days before the Phase 5 date. After window: from that date to the run.
- **Fewer rounds:** from `history.py rounds`: median rounds per phase, share of phases with ≥3 rounds, share of fix time those hold, rounds past the hard landing, and rounds by root cause class for the causes each change targets.
- **Work kept:** follow-up phases written after a landing and how many reached `done` (plan docs under each production's units); merge design-check defects per phase and holds per phase from `review_regime.py report` on rows in each window; findings routed to other units and whether their phases ran.
- A change passes when its round measure meets the target Phase 4 set and no work-kept measure is worse by more than its stated margin. A change that fails goes to the user as a question through the showrunner: keep, change, or revert, with the numbers and the one recommended.
- State the sample sizes beside every number; a window under 20 phases is reported, not judged.

- Root counts use the Phase 2 study method by hand, one evidence line per round, with the second writer recounting each window on its own; `history.py rounds` gives only the Phase 1 causes.
- Review rows come from `review_regime.py`'s shared row reader with both window bounds applied in the study's extraction, not from `report`'s fixed regimes; state the boundary field, the excluded rows and each window's sample size.
- The readiness count takes phases from `history.py rounds --since <date> --status all --format json --top 100000` with at least one round, a `check` of `ok`, and a start on or after the date; open and mixed phases are listed apart and not judged.
**Files:**
- `docs/plans/sdlc-fix-rounds-study.md` — `## Effect`.

**Workers:** `2 writers`
- `impl` — `docs/plans/sdlc-fix-rounds-study.md`: the effect tables and verdicts.
- `test` — opens as `impl`, writes nothing: reruns each command the tables cite and sends `impl` each number that differs.

**Constraints from prior phases:** set at Phase 5's checkpoint.

**Acceptance gate:** every number in `## Effect` names the command that produced it and matches a rerun; each adopted change has a verdict; each failed change's question is sent to the showrunner.
