# sdlc-fix-rounds

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A true count of repair rounds per phase, a study of the phases with the most rounds, and process changes that cut rounds and still deliver the work.

> **Production: sdlc** — unit `fix-rounds-unit`; production doc `docs/plans/sdlc-production.md`

> **Issues:** ~/rust/hanadocs/issues/run command only repairs without a worker.md, ~/rust/hanadocs/issues/route missing criteria after landing.md, ~/rust/hanadocs/issues/route repeated repairs outside kept categories.md, ~/rust/hanadocs/issues/verify phase and activity boundaries.md, ~/rust/hanadocs/issues/preserve accepted behavior in repairs.md

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
  - `scripts/production/merge_gate.py` :47 — `TEST_SUITES`; `history` is in it.
- **Key files:**
  - `~/.local/state/plan-delegate/runs/*.jsonl` — run records, one file per `/unit:direct` run (206 files, 2026-09-04 to 2026-10-20, 92 MB). One JSON event per line. Events this unit reads:
    - `run_started` — `run_id`, `plan_doc`, `project_plan_doc`, `worktree`, `branch`, `working_dir`, `main_agent`.
    - `phase_started` — `phase_id`, `phase_instance_id`, `phase_title`, `phase_started_at`, `work_order_words`, `work_order_lines`, `work_order_file_targets`.
    - `pass_started` / `pass_finished` — `pass_kind` (`impl|test|fix|review`), `pass_activity` (free text, e.g. "repairing Phase 7 review findings"), `fix_pass`, `team_slot`, `called_agent`, `called_task`, `pass_elapsed_seconds`, `phase_id`, `phase_instance_id`, `run_id`.
    - `activity_started` / `activity_finished` — `activity_label`, `activity_text`, `activity_result`, `activity_elapsed_seconds`.
    - `finding_opened` (`finding_id`, `severity`, `lens`, `caught_by`, `file`, `title`), `finding_verdict` (`fix_attempts`, `reopen_count`), `finding_gate` (`round`, `advisory`, `open_counts`), `finding_batch_dispatched` (`round`, `covered`, `gating_open_before`, `gating_severities`, `advisory`), `finding_batch_landed` (`round`, `landed`). Ledger events carry `phase_instance_id` but not `run_id`: join them to their run through the `phase_instance_id` of that run's `phase_started`.
  - `scripts/direct/findings.py` — the ledger: round 1 gates blockers and minors, later rounds blockers only; convergence advisories never stop a round. Limits in `config/direct.conf` (`MAX_FIX_ATTEMPTS`, `STALLED_ROUNDS`, `REPAIR_ROUNDS_PER_FINDING=0.5`, `MIN_REPAIR_BUDGET`, `RUNAWAY_ROUNDS=5`).
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

- `history.py rounds` lists `/unit:direct` phases by repair rounds, most first. Columns: `run  phase  title  plan  reviewed  rounds  review-ux  review-code  gate  merge  live  other  findings  fix-time  span  check`; `--format json|csv`; flags `--since`, `--until` (on `rounds` only), `--status`, `--top N` (default 25), `--plan <substring>`. Summary lines: phases counted and reviewed, with the median rounds over reviewed phases; the count of phases with no review recorded; the reviewed phases with 3 or more rounds, their share and the share of reviewed fix time they hold; rounds by cause over all phases; count of `mixed` records. A phase is reviewed when its run and phase instance have a `pass_started` review pass. The JSON row also carries `reviewed` and `past_landing`, the number of rounds from round 4 on; `_meta` carries `reviewed_phases`, `unreviewed_phases` and `reviewed_fix_seconds`.
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

#### As-built

- `docs/plans/sdlc-fix-rounds-study.md` holds a `## Proposals` section with five process-change questions, P1 to P5. Each has a question, change, rounds saved, work kept, risk, measure and answers. One shared evaluation section applies to all five.
- Each proposal names one root cause and its certain rounds saved:
  - P1 run command-only follow-through without a repair worker — mechanical, 8.
  - P2 route missing acceptance criteria after landing — spec-gap, 5.
  - P3 route repeated repairs outside the kept categories — fix-failed, 3.
  - P4 verify phase and activity boundaries — record, 0.
  - P5 preserve accepted behavior touched by a repair — fix-regressed, 0.
- The totals are 16 rounds removed, 25,417 recorded agent-seconds moved (P1 holds 24,033 of them) and 0 s saved. The counted round sets of the proposals do not overlap.
- Issues P699 to P703 in `~/rust/hanadocs/issues/` track P1 to P5.
- The questions went to the user through the showrunner. The study adopts none of them.
- All five proposals are process changes. No tooling-only item exists.

**Files:**
- `docs/plans/sdlc-fix-rounds-study.md` — the study; `## Proposals` holds P1 to P5 and the shared evaluation section.

**Binds later work:**
- The proposals keep their titles and numbers P1 to P5 in the study and in the issues.
- Rounds are counted as `history.py rounds` counts repair intervals. Any later comparison uses the same count.
- The user's answers to P1 to P5 decide which files change. The hub files are `commands/unit/direct.md` and `docs/production_format.md`.
- No proposal is tooling-only, so no tooling item gets built without a question.

**Gotchas:**
- The routing proposals (P2, P3) count repair rounds as `history.py rounds` numbers its repair intervals, not as ledger batches. `findings.py` numbers ledger dispatches differently.
- Moved time is work that leaves the phase. It is not saved time.

### Phase 5 — Apply the changes the user approved  · status: done

#### As-built

- P1 to P5 and an Astra step are in force (the user, 2026-10-28). P2 to P5 are 30-phase trials. Issues P699 to P703 in `~/rust/hanadocs/issues/` track P1 to P5.
- `findings.py` sets `repair_round` to `len(phase_rounds(events)) + 1` over the run-record events of this phase instance, or to the ledger number when no run record exists. The ledger `round` and every limit keep their meaning.
- New finding state `routed`: not resolved, out of the gating batch and the open counts. `findings.py route --id --reason <spec-gap|outside-kept> --owner --work-order --due [--evidence]` refuses an empty field, a finding in flight, pending review or accepted, and a finding with an Astra review due but not recorded. `findings.py close-route --from <run id>:<phase instance id>:<finding id> --evidence` closes it at the receiving checkpoint.
- From `ROUTING_FROM_ROUND`, `dispatch` needs one `--keep <id>=<regression|broken|gate|ruling>:<evidence>` per covered finding. Every gating open finding that is not routed stays in `--covers`. Every `dispatch` needs `--accepted-checks "<checks>"` or `"none: <why>"`.
- A gating open finding is due for Astra when `repair_round >= ROUTING_FROM_ROUND` or `fix_attempts >= ASTRA_AFTER_FAILED_FIXES`, with no record at its current `fix_attempts`. `findings.py astra --id --note <path> (--approach "<line>" | --unavailable "<reason>")` stores the record; `route` copies the newest approach. The director runs `/astra_review --run-directory --phase` on the note.
- `findings.py exposure --proposal <P1|P2> --basis [--decision]` records P1 and P2 by hand. `gate` records P3, `dispatch` records P5 through `accepted_checks`, and the launch check records P4.
- `progress_history.py check-launch` runs the launch check alone, for workers that start before `start-pass`; `start-pass` runs it on each launch. It refuses on a recorded run with no `--phase-id`, a phase that is not open, a `--prompt-file` whose first `Phase <id> — ` line names another phase, and a fix with no round in flight or landed with findings pending review, unless `--fix-cause` is given. `start-pass --worker-name` stores `pass.worker_name`. Both launchers pass `--phase-id` and `--prompt-file` and stop on a refusal; `implement.sh` takes `--fix-cause`.
- `history.py routed [--open] [--since] [--format text|json]` lists routed findings with run, plan, phase, owner, work order, due, age and closure, joined by key.

**Files:**
- `scripts/direct/findings.py`, `config/direct.conf` — the counter, the `routed` state, the new commands and `dispatch` checks; `ROUTING_FROM_ROUND=4`, `ASTRA_AFTER_FAILED_FIXES=2`.
- `scripts/direct/progress_history.py`, `implement.sh`, `review.sh` — the launch check and its options.
- `scripts/history/routed.py`, `history.py`, `commands/history.md` — the `routed` command.
- `scripts/shutdown/restart.py` — fallback resumes find the phase by `pass.worker_name`.
- `commands/unit/direct.md` (`<FixDispatch/>`, `<FindingsLedger/>`, `<Synthesize/>` step 5, `<PassOwnership/>`), `docs/production_format.md` item 7 — the rule text.
- Tests: `scripts/direct/test_findings.py`, `test_progress_history.py`, `test_implement_launcher.py`, `test_review_launcher.py`, `scripts/history/test_routed.py`, `scripts/shutdown/test_restart.py`.

**Binds later work:** "Did the changes cut rounds and keep the work" reads these events. `trial_exposure`: `proposal`, `basis`, `decision`, `repair_round`. `finding_routed`: `finding_id`, `reason`, `owner`, `work_order`, `due`, `evidence`, `repair_round`, `round`, `fix_attempts`, `astra_approach`. `finding_route_closed`, joined by run, phase instance and finding id. `finding_astra_review`: `finding_id`, `note`, `approach`, `unavailable`, `fix_attempts`, `repair_round`. `launch_refused`: `reason`, `pass_kind`, `phase_id`, `prompt_file`. `launch_checked`. `pass_started.launch_check`: `phase_id`, `phase_title`, `prompt_phase_line` (`matched`|`absent`), `fix_cause`. `finding_gate` and `finding_batch_dispatched` carry `repair_round` beside `round`; the batch also carries `kept` and `accepted_checks`. The gate payload carries `astra_review_due`. `history.py routed --open` shows unclosed routes.

**Gotchas:** A recorded-run launch with no phase is refused. A fix launch with no in-flight round and no landed round pending review is refused unless `--fix-cause` is given. Only the first `Phase <id> — ` line of a prompt is the identity line; a prompt with none passes.

**Ruled out:** picking a resume phase by team slot, because a review sent to a fix seat records slot fix while the durable slot is impl.

### Phase 6 — The round summary counts reviewed phases only  · status: done

#### As-built

- A phase is reviewed when its run and phase instance record a `pass_started` event with `pass_kind` `review`. `PhaseRounds` gains `reviews: int`, counted in `load_phases`; `phase_rounds`, `Round` and the other `PhaseRounds` fields do not change.
- `rounds.run` takes the median rounds, the share of phases with 3 or more rounds and the fix-time share of those phases over reviewed phases only. An empty reviewed set gives median 0 and shares 0. `phases`, `fix_seconds`, `rounds` and the cause counts stay over all listed phases.
- The table has a `reviewed` column (`yes`/`no`). The summary prints `<n> phases counted; <r> reviewed; median <m> rounds over reviewed phases`, then `<u> phases with no review recorded (not in the median or shares)`, then `<h> reviewed phases with >=3 rounds (<s>%) hold <f>% of reviewed fix time`.
- Each JSON row carries `reviewed` (bool). `_meta` adds `reviewed_phases`, `unreviewed_phases` and `reviewed_fix_seconds`, the denominator of `heavy_fix_share`.
- `docs/plans/sdlc-fix-rounds-study.md` opens with a summary taken from `## Totals`: 15 phases, 260 repair rounds, 271 h; failed fixes 36% of rounds and 49% of time; spec gaps 17% and 19%; mechanical 15% and 11%. The study has no "median 0 rounds" figure, and its evaluation section takes medians and shares over reviewed phases.
- `scripts/direct/demo_refusals.sh` takes no arguments (exit 2 with any). It makes its own scratch folder with `mktemp -d` under `/tmp` and removes it on exit. It sets `HOME`, `XDG_CACHE_HOME`, `UNIT_HISTORY_DIR`, `PLAN_DELEGATE_HISTORY_DIR`, `AGENTS_CONFIG_FILE` and `UNIT_CONFIG`/`PLAN_DELEGATE_CONFIG` to scratch paths, writes fixed thresholds to that config, unsets `CLAUDE_CODE_SESSION_ID`, and adds PATH fallbacks for the system, per-user and Homebrew tool folders. It runs `findings.py` and `progress_history.py` beside it through `../lib/py`, prints each of the five refusals under a heading that names the rule, shows the passing call after each, then prints `all five refusals shown` and exits 0. A failed step prints `FAILED: <step>` and exits 1.
- `scripts/lib/py` needs Python 3.11 or later, because `findings.py` and `progress_history.py` import `datetime.UTC`. Its cache key is `python3-3.11`.

**Files:**
- `scripts/history/rounds.py`, `scripts/history/test_rounds.py` — the reviewed count, the summary and their tests.
- `commands/history.md` — the `rounds` line says the median and shares cover phases with a recorded review start.
- `docs/plans/sdlc-fix-rounds-study.md` — the summary at the top.
- `scripts/direct/demo_refusals.sh`, `scripts/direct/test_demo_refusals.py` — the refusal demo and its test.
- `scripts/lib/py` — the 3.11 floor; the scripts and test fixtures that name the floor say 3.11.

**Binds later work:** "Did the changes cut rounds and keep the work" takes proposal statistics from exposed rows, not from the reviewed-only `_meta`.

**Gotchas:** A Python 3.10 interpreter cannot run `findings.py` or `progress_history.py`; `lib/py` skips it. A cache file under the old `python3-3.10` key is not read.

### Phase 7 — Did the changes cut rounds and keep the work  · status: todo
**Depends on:** Phase 5, Phase 6 (it redefines the `history.py rounds` medians and shares)
**Blocked by:** for each adopted proposal, 30 completed, non-mixed exposed phases that started after the time Phase 5 records, zero-repair phases included, with exposure as that proposal's **Measure** defines it.

#### Work Order

**Goal:** `docs/plans/sdlc-fix-rounds-study.md` ends with a before-and-after table per adopted change, a verdict on each, and any revert sent to the user as a question.

**Spec:**
- First add a line to the head of `docs/plans/sdlc-fix-rounds-study.md`: `Changes took effect: <Phase 5 checkpoint hash> on main, <YYYY-MM-DDTHH:MM:SS±HH:MM>`, the time that checkpoint reached `~/.claude` main (the first main commit that contains it), with its offset. A bare date reads as local midnight in `rounds.run` and can take in work from before the change. Every window below counts from that time.
- Before window: the 30 days that end just before the time Phase 5 records. After window: from that time to the run. The windows do not overlap; a phase that started before the time and ended after it is listed apart and not judged.
- **Fewer rounds:** from `history.py rounds`: median rounds per phase, share of phases with ≥3 rounds, share of fix time those hold, rounds past the hard landing, and rounds by root cause class for the causes each change targets.
- **Work kept:** follow-up phases written after a landing and how many reached `done` (plan docs under each production's units); merge design-check defects per phase and holds per phase from `review_regime.py report` on rows in each window; findings routed to other units and whether their phases ran.
- A change passes when its round measure meets the target Phase 4 set and no work-kept measure is worse by more than its stated margin. A change that fails goes to the user as a question through the showrunner: keep, change, or revert, with the numbers and the one recommended.
- State the sample sizes beside every number. Each window needs at least 20 completed phases, 10 exposed phases and 12 merge design-check rows that are not excluded; a window under any minimum is reported, not judged.
- Apply `### Evaluation shared by all five questions` and each adopted proposal's **Measure** in `## Proposals` of the study as written: exposure decided before the treated step runs, zero-repair exposed phases kept, the work-kept margins, combined parent-plus-follow-up rounds and agent-seconds, moved seconds apart from saved seconds, closure of each follow-up due in the window, and defects that reach the user or merge. Missing evidence or unfinished due work gives `inconclusive`, never a pass. When adopted changes cannot be separated, each one's attribution is `inconclusive`.
- When P4 is adopted, its accuracy target is not read from `history.py rounds` alone: `load_phases` drops a launch with no recorded `phase_started`, and the `mixed` check cannot prove each launch's phase. Reconcile launch evidence with phase boundaries and recorded activities, list each missing or wrong association, and count it against the target.

- Proposal statistics come from the explicitly selected exposed rows of each proposal, unreviewed zero-repair exposures included; never from the reviewed-only `_meta` statistics of `history.py rounds`.
- P4 accuracy judges three records apart: `launch_refused`, UX `launch_checked` and `pass_started.launch_check`, including refusals with no phase instance. A correct refusal of a stale or missing-phase request counts as a success. A false refusal, or a launch associated with the wrong phase, counts against the target. The `test` slot checks one case of each.
- Routed-work closure: the recount checks an open route, a valid cross-run closure, a closure with a wrong key, and a closure event with no acceptance evidence (commit, test result or review). The last one is `inconclusive`, never closed.
- Root counts use the Phase 2 study method by hand, one evidence line per round, with the second writer recounting each window on its own; `history.py rounds` gives only the Phase 1 causes.
- Review rows come from `review_regime.py`'s shared row reader with both window bounds applied in the study's extraction, not from `report`'s fixed regimes; state the boundary field, the excluded rows and each window's sample size. Bound review rows on `merged`. Before any total, sort each `Row` as usable, excluded or insufficient evidence: an absent plan identity, review minutes or repair minutes is not a measured zero, and a required field that is absent blocks a pass instead of making the sample smaller.
- The readiness count takes phases from `history.py rounds --since <time> --status all --format json --top 100000` with a `check` of `ok`, a start at or after the time, and exposure as each proposal's **Measure** defines it, zero rounds included; open and mixed phases are listed apart and not judged.
**Files:**
- `docs/plans/sdlc-fix-rounds-study.md` — `## Effect`.

**Workers:** `2 writers`
- `impl` — `docs/plans/sdlc-fix-rounds-study.md`: the effect tables and verdicts.
- `test` — opens as `impl`, writes no repository file: recounts on its own the roots, exposure, routing, follow-up closure evidence and each number taken from a command, and sends `impl` each one that differs.

**Constraints from prior phases:** set at Phase 5's checkpoint.

**Acceptance gate:** every number in `## Effect` names the command that produced it and matches a rerun; the test recount agrees with each root, exposure and closure call; each window meets the sample minimums or is marked reported, not judged; each adopted change has a verdict of pass, fail or inconclusive; each failed change's question is sent to the showrunner.
