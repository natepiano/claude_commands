# sdlc-fix-rounds

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A true count of repair rounds per phase, a study of the phases with the most rounds, and process changes that cut rounds and still deliver the work.


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
- **Test:** `python3 -m unittest discover -s scripts/history -p 'test_*.py'` from the worktree root.
  Then run the targeted test from the worktree root: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/targeted_test.py --repo .`.
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

### Phase 7 — Did the changes cut rounds and keep the work  · status: done

#### As-built

- `docs/plans/sdlc-fix-rounds-study.md` opens with `Changes took effect: 56b8f93be… on main, 2026-10-28T21:26:05-04:00`, the first `~/.claude` main update that contains the Phase 5 checkpoint (C1, from the main reflog).
- `## Effect` holds the boundary, the window selection, the exposure table, the P4 launch audit, the review rows, the routed-work closure and the questions. Every number names C1 or C2. C2 is a read-only Python command in `### Reproduce C2`; it reads run records through a fixed upper bound (2026-11-09T03:32:17+00:00) and prints a digest of its input.
- Verdicts: P1, P2, P4 and P5 inconclusive; P3 not ready. Both review windows are under 12 usable rows (9 before, 6 after), the before exposure cohorts and the hand root counts are not rebuilt, and the acceptance audit of routed closures is not complete. No change failed, so no question went to the user.
- P3 exposure counts distinct started ledger batches before each exposure; the recorded round number overstates it. 18 of 57 completed declarations qualify.
- By showrunner order (2026-11-08), a proposal under 30 exposed phases is recorded "not ready" and is not judged; the phase does not stop for it.
- The second count came from a separate read-only Codex thread inside the study worker, not a `test` slot: the Codex launcher could not start a further worker that night.

**Files:**
- `docs/plans/sdlc-fix-rounds-study.md` — the head line and `## Effect`.

**Gotchas:** Review rows can be written after their `merged` time, so a C2 rerun can add rows to a window; the run records do not change under the fixed upper bound.

### Phase 8 — Ad hoc review commands review in the invoking session  · status: done

The user, 2026-11-02 19:4x EST, relayed by the sdlc showrunner, on `/module_review`, `/team_review` and `/api_review`: "yes they should just run inside their own session"; "they are adhoc and not part of the standard process - i invoke them at will at this time". Since d3421f80d (2026-07-13), each command launches fresh one-shot reviewers through `agent_exec.sh`. This phase runs before Phase 7, which waits for its sample.

#### As-built

- `commands/module_review.md`, `commands/team_review.md` and `commands/api_review.md` launch nothing through `agent_exec.sh`. The invoking session runs each lens itself, one after the other, and writes each lens's findings under `${SESSION_DIR}` before the next lens. The lens prompts, findings formats, merge, dedupe, adversary and validation steps stay.
- Each command loads its preamble once and reads each file once per pass or cycle. `/team_review` points to `<DecisionEconomy/>` for the settled categories, and `/api_review` uses the charter's finding schema.
- The registry tasks `module_review.reviewer`, `module_review.validation`, `team_review.expert`, `api_review.reviewer` and `api_review.adversary` and their assignments are gone from `config/agents.conf` and `scripts/lib/test_agents_registry.conf`. The `teamreview` alias in `scripts/agents/agent_admin.sh` is gone. Registry and quota-alert tests use `design_roles` and `cli` as sample keys.
- `scripts/direct/test_phase_process.py` fails when any of the three commands names `agent_exec`, `claude --print` or `claude -p`.
- `scripts/agents/agent_exec.sh` and the phase review path did not change.

**Files:** the three commands; `commands/agent.md`; `config/agents.conf`; `docs/as-built/agent-registry.md`; `docs/direct_footprint_review.md`; `scripts/agents/agent_admin.sh`; `scripts/direct/test_phase_process.py`; `scripts/lib/test_agents_registry.conf`, `scripts/lib/test_agents_registry_fixture.py`; `scripts/whoami/quota_alert_test_support.py` and ten `scripts/whoami/test_*` files.

### Phase 9 — `/showrunner:units` shows only units, with a status column and anomaly lines  · status: done

The user, 2026-11-05, relayed by hana and the sdlc showrunner: "we should show the units for a producer - we shouldn't show anything else but the anomalies sould show because they are not accounted for either in the script (like morty - which should be accounted for and not shown) or if there ar orphan units they are indicators of a bug or an actual orphan and need to be addressed" (allow-banned: user quote). And: "you need to show a status column for a unit as to whether it is paused or active - that should be added to the command also".

#### Work Order

Design from the sdlc showrunner:

1. The table shows only the production's staff units. Every process that `unit_phases.py` cannot place is an anomaly line under the table, on stdout. Today these lines go to stderr from `scripts/production/session_staff.py:665`, above the table. When a run has anomaly lines, the script sends one message per run with those lines through `send.py --to <target>`. The target is `sdlc`, read from a key in `config/production.conf`.
2. Morty is placed and is not an anomaly. Match its Codex thread with `MORTY_NATIVE_ID` in `config/send.conf`, the value that `send.py` uses for `Authority: morty`. Read the value from that config. Do not copy it. A Codex process that is not Morty stays an anomaly.
3. The tables get a Status column, `paused` or `active`, in the plain table and the style table. The pause record is one field on the staff row: `paused = {since, by, why}`. No field means active. There is no new store.
4. One command sets and clears the field, in the existing staff command:
   - `staff.py pause <unit> --why "<why>"`
   - `staff.py pause --production <slug> [--except <unit>...] --why "<why>"`
   - `staff.py resume <unit>`
   - `staff.py resume --production <slug>`
5. A pause is a record only. It stops nothing. A rerun of pause or resume succeeds.
6. Add no other check.

**Files:** `scripts/production/unit_phases.py`, `live_units.py`, `session_staff.py`, `staff_sources.py`, `unit_lookup.py`, `display_cells.py`, `style_units.py`, `staff.py`; `config/production.conf`; `commands/showrunner/units.md`; `docs/as-built/staff-accessors.md`, `docs/as-built/showrunner-automation.md`; and their test files.

**Workers:** `2 writers`
- `impl` — the source, config and doc files.
- `test` — the test files, written at the same time, with the callers' existing tests in each edit round.

**Acceptance gate:** on the live sdlc production, `unit_phases.py --production docs/plans/sdlc-production.md` prints a table with a Status column; no Morty line; each other unplaced process as an `anomaly:` line under the table; and one message reaches sdlc with those lines. `staff.py pause` then `resume` on a live unit changes its Status cell and back. Named tests pass for: Morty placed, another Codex process unplaced, one send per run, no send with no lines, a send failure leaves the table and exit code unchanged, and each pause and resume form.

### Phase 9r — A retire fault leaves no server, and a source release does not refuse  · status: done

From the sdlc showrunner, 2026-11-05. Separate hash, on branch `sdlc-fix-rounds-retire`. Add no new check.

- `codex_mesh end` stops the run's servers before `staff.retire`, so a retire fault can never leave a server running.
- `_release_source_servers` and `stop_worker_for_switch` lose their refusals. A release prints one `anomaly:` line for an invalid record or a server that fails to answer, and goes on.
- One case still refuses, by sdlc ruling: a thread loaded on a server that answers with an unknown stop state. A changed source identity prints one anomaly line, signals nothing and goes on.
- `staff.retire` is out of scope: hierarchies `9772f7357` deletes its refusals.
- After this hash and `9772f7357` are on main, retire every astra-reviewer row with no live process and report the count.

Landed on main in 9aa3b5526 (2fc784207), folded into the fix-rounds branch. devtools 8l keeps the staff row at `codex_mesh end`, so end stops the last worker's server and does not call `staff.retire`. The sweep on 2026-11-07 retired all 24 astra-reviewer rows through `staff.py retire`; none had a live process.

### Phase 9s — A fix records its repair round in the Codex launch path  · status: done

From the sdlc showrunner, 2026-11-05. Dropped on 2026-11-06: hash `50bfbdcf6` does not merge.

Devtools 3y `finish_repair` in `scripts/direct/assign.py` resolves the round: `landed` on a clean exit, `abandon --edits-landed` on an error. The fix call in `commands/unit/direct.md` sets `UNIT_RESOLVES_ROUND=1`, so a director passes that prefix on each repair round.

### Phase 9w — The units table prints an anomaly only for a fault someone must act on  · status: done

From the user's C1156 verdict and the sdlc showrunner, 2026-11-06. All 18 anomaly lines in the user's screenshot were false: a test's fake codex with an inherited pane id, three staff workers read as not staff, and one session with no staff row. Remove the process scan from the table, remove the lines for a process with no staff row, stop placing a process under a unit by its pane alone when its id has no staff row, print each process once, and run the test's fake without the pane variables. If another caller uses the lookup that calls a staff worker not staff, fix it; otherwise delete the dead path. The user approved one new check: an --except value that matches no unit of the production refuses the pause, changes nothing, and names the bad value.

### Phase 9v — A production pause keeps each unit named in --except  · status: done

From the sdlc showrunner, 2026-11-06. `staff.py pause --production hana --except fastest-unit` paused every unit, because the exception compares only the row name and id. Resolve each value through the one unit lookup: the name, the -unit form and the id all match. Add a test for each form. One matcher, unit_match, limited to the production's own members. The user approved a refusal for an unmatched value on 2026-11-06; Phase 9w carries it.

### Phase 9u — The units hash merges with the staff-name change on sdlc  · status: done

From the sdlc showrunner, 2026-11-06. Devtools 7u replaced the production slug with the showrunner's staff name. Merge sdlc into this branch. Keep that name, and keep the Status column, pause records and anomaly lines from Phase 9. Add no new check.

### Phase 9t — A commit that exists is not reported as failed  · status: done

From the sdlc showrunner, 2026-11-06, rca 261106-1. Separate hash, on branch `sdlc-fix-rounds-publish`, from sdlc. Add no new check.

`scripts/lib/main_publish.commit` runs `git commit --only`. A stale `.git/MERGE_RR.lock` made git exit nonzero after it wrote the commit, because rerere runs after the commit. `commit` took that as no commit, restored the old file text and said the value was unchanged. HEAD held `SIMPLIFY_LENS=off`, the live file said on, the commit was not pushed, and every main promotion refused. The fix compares HEAD before and after: when HEAD moved, the commit exists, so the new file stays and the push goes on.

### Phase 9x — A push that cannot merge origin/main leaves the checkout as it was  · status: done

From the sdlc showrunner, 2026-11-06: "merge_checkpoint.py at push merged origin/main into sdlc, hit a conflict in 3 files, and left the showrunner checkout mid-merge. It must abort the merge on a conflict, leave the checkout as it was, and print a held line that names the files."

Cause: `push_git` aborts its origin/main merge only when `may_undo` is true. `refresh_pushed_tip` calls it with false, so a conflict leaves the merge in place. The merge is `push_git`'s own step, so `push_git` aborts it on every failure, then holds with the conflicted paths.

The `--from-child` handler in `main` reports a held Stop as it reports a cancelled one: the held line, the files and the recovery message. Held returns 1; cancelled keeps 77.

### Phase 9y — The review trial line counts the findings in the ledger  · status: done

From the sdlc showrunner, 2026-11-06: "unit-board's review trial recorder printed code 4 findings, but its ledger holds 1 (F001)... The count must come from the findings ledger." In this unit the recorder printed code 4 for 9x, whose ledger holds 2, and code 5 for 9w, whose ledger holds 6. Count the code findings from the phase's findings ledger. Add no new check.

`review-trial` reads the selected instance's ledger file through `findings.read_phase_ledger`. The UX and code counts and the UX repair rounds come from that ledger, so a routed or closed finding counts, and a closure reply adds none.

### Phase 9z — The launcher sees the end of a worker that read a passed run  · status: done

From the sdlc showrunner, 2026-11-06. Codex worker merge16 (verify-unit) stopped with no event after it read a passed run, twice (14:59 and 16:35 EST). Its launcher stayed attached for 62 and 19 minutes. An interrupt ended the wait in seconds. Find why the launcher does not see the worker's end, and fix the cause where it lives. Add no new check.

Cause: the worker's turn never ended. Each stall began at an `rm -f` of a run log, which Codex holds for approval under `on-request`. merge16's turns all ran `on-request`: `_start_turn` named `sandboxPolicy` on each turn but no `approvalPolicy`, and the server keeps a thread's last turn policy. The launcher does not answer server requests, so the approval waited until an interrupt. `_start_turn` now names `approvalPolicy: never` on every turn. sdlc kept the fix to `_start_turn`.

### Phase 10 — A relaunch of the same work adds no repair round  · status: done

From the sdlc showrunner, 2026-11-06. In cargo-liner, a worker relaunch after a launcher fault counted as repair rounds: the ledger showed 4 rounds for 1 repair and asked for two extra Astra reviews. direct.md says: "Count one repair round per ledger batch with a started fix pass. Relaunches of that batch add no round." This unit shows the same defect: phase 9x had 2 ledger batches, and its Astra record says repair round 4. Add no new check.

Cause: `findings._repair_round` counted every group from `history/rounds.phase_rounds`. A fix pass with no ledger batch (a gate run sent as a fix, a relaunch with no ledger number, a follow-up) makes its own group, so it added a round. It now counts the distinct ledger batch numbers that the groups hold, less the refunded batches that never land. One batch split across two groups counts once. `phase_rounds` and `history.py rounds` keep the other groups.

### Phase 10a — Branch tests stay green on the merge with sdlc  · status: done

From the sdlc merge gate, 2026-11-07. Hash 2b1b216e7 merged into sdlc a8123acd4 turned two tests red, in the batch and alone. Both pass on a8123acd4 without this branch.

1. `shutdown/test_restart.py` `test_live_rows_scans_codex_once_for_members_and_unassigned_candidates` reads a live Codex id into its anomaly list.
2. `ask_a_friend/test_end_friend.py` `test_a_staff_retirement_refusal_still_stops_the_consultation_server`: the consultation server survives the retirement refusal.

The branch changes neither folder. The cause is a shared path the branch changes: the Codex process scan in `staff_sources.py` or the retire path. Tests must not read live units.

Cause: neither test failure came from this branch. `test_restart` read the live Astra root, because its fixture patched `unit_runs.run_roots` but not `unit_runs.ASTRA_RUN_ROOT`. Under load, `codex_mesh._reap` returned success while a zombie child app-server was not yet waitable, so end-friend left the consultation server alive. The fixture now patches the Astra root, and only `waitpid` confirms a child exit.

The phase also folds retire-servers `7c0e6596f` (Phase 9r) into this branch. devtools 8l keeps the staff row at `codex_mesh end`, so the merge keeps the last-worker server reap and drops the retire call. The reap now also runs when an end of the last worker fails after it publishes `ended`. `scripts/message/morty_identity.py` holds the `MORTY_NATIVE_ID` rule for `send` and `session_staff`, which breaks the send → roster → session_staff → send import cycle.

### Phase 10b — Git proves that a shrink's code checkpoint merged  · status: done

From the sdlc showrunner, 2026-11-07. `merge_checkpoint.ancestry` holds a shrink unless `merged_checkpoints` finds a `progress_history` checkpoint event for the phase. Today it held two shrinks whose code checkpoint was on sdlc:

1. shutdown resume-codex-by-name: the phase ran on another branch, so its event names another worktree.
2. herdr 21: the code checkpoint is the top hash, and it has no event.

Git proves the merge. A shrink passes when its parent commit is an ancestor of the merge branch. This path no longer reads events. Add no new check. Add one test for each case.

As built: `ancestry` holds a shrink or as-built commit only when `git rev-list --no-merges <merge_branch>..<commit>^` prints a commit. The literal rule (the parent is an ancestor) holds a shrink for good after a unit-side merge of the merge branch, because that merge commit never reaches the merge branch. `test_shrink_after_criss_cross_merge_scopes_only_its_doc` covers that case. `merged_checkpoints` stays for its other callers.

### Phase 10c — `/unit:direct` loads a core and reads other sections when they apply  · status: done

From the user, through accounts and the sdlc showrunner, 2026-11-07 15:10 EST: "tell sdlc to cut direct.md down to the core so that it loads other instructions as it needs them". A director reads `commands/unit/direct.md` (116 KB, about 29k tokens) again after each compaction. A core of 30-40 KB saves about 20k tokens per cycle.

1. Keep in the core only what every run needs. Move each section that only one mode or one moment needs to its own file under `docs/direct/`. Use the existing `<TagReferenceContract/>` stub pattern: a table row and a stub that names the file and the moment to read it.
2. Move text; do not copy it. Each rule stays in one place.
3. The compaction hooks and `<CompactionContract/>` name the core and the stub files that a live moment needs.
4. herdr edits `direct.md` after this phase lands. herdr moves its later edits into the new layout.

The moved groups:

| New file | Sections | Read when |
| --- | --- | --- |
| `repair_rounds.md` | FindingsLedger, PassOwnership, Synthesize, FixDispatch | a review, gate or verification result returns; before each repair dispatch |
| `early_review.md` | EarlyReviewArm | at each implementation or fix dispatch |
| `phase_reservation.md` | PhaseReservationContract, RetainPhaseReservation, CoordinatePhaseReservation | at phase start, at checkpoint release, and after compaction with a live reservation |
| `verification.md` | VerificationContract, VerificationNarration | before a verification run |
| `ux_review.md` | UXReview, RunApplicationSmokeTest | when a phase has a UX review or an application smoke test |
| `launch_failure.md` | WorkerLaunchFailure | when a dispatch fails |
| `authorization.md` | AuthorizationContract | when an action needs user authorization |
| `held_astra_roles.md` | HeldAstraRoles | when the run holds an Astra role |
| `standing_plan_refill.md` | StandingPlanRefill | when the plan header carries `**Standing:**` |
| `berth_decisions.md` | BerthDecisions | when a cargo-berth decision applies |
| `codex_dispatch_wait.md` | CodexDispatchWait | when the director is Codex |
| `final_gate_commit.md` (exists) | FinalGate, RunSummary | at the final gate |

As built: the core is 45 KB, not 30-40 KB. The five largest core sections are needed by every run, so they stay. Each moved group has one stub and one `<TagReferenceContract/>` row, so `_contract_revision` and `tag_inventory.py` cover every new file. After compaction, the hooks say to read the core in full, then each stub file whose moment is live.

### Phase 10d — A merge adds change rows only for its own production, with the producing unit  · status: done
**Depends on:** Phase 10c

From the sdlc showrunner, 2026-11-07, a defect from builds. When builds merges main, `merge_checkpoint` adds a row for every Change-Review line in the merged commits, with builds as Producer. So rows C1179-C1183 (merge 429d1528) show the builds session, and the user asks builds about sdlc work.

Fix where the rows are made:
1. A merge adds rows only for its own production's checkpoints.
2. Each row's Producer is the unit that made the checkpoint.
3. Each row's Production cell is the fixed staff id of the showrunner whose production made the checkpoint.

The user, through builds and sdlc, 2026-11-07: "shutdown was part of sdlc not natedev - it got renamed but whatever - the fix needs to keep changes in the production that made them based on the id of the showruner for that production". So a merged row's Production cell is the fixed staff id of the showrunner whose production made the checkpoint, not the production that merges it. Example: shutdown's 9e8e10895 (subject natedev-shutdown), rows C1195-C1197, landed in builds; they belong to sdlc.

Add no new check.

As built: `change_review.list_ready` reads only the merges whose subject ends ` into <merge branch>`, so a foreign merge that a fast-forward brings in adds no row. It skips each commit already on the origin merge branch of a production outside this one's scope (`reviewed_merge_refs`); for such a branch that later took in the merge, `_publication_exclusions` skips only the history it had before, so a rescan or a held-step release gives the same rows. The Producer is the unit the checkpoint branch resolves to; the requesting unit's id applies when the branch resolves to it or to no unit. The Production cell is the fixed staff id of the producing unit's showrunner. `foreign_units` is gone. `merge_gate.py --promote-only` locks and scans the promoted main repository, not the working directory it runs from.

### Phase 10e — Two test rules in the direct contract  · status: done
**Depends on:** Phase 10c (it moves FixDispatch)

From the user, through natest and the sdlc showrunner, 2026-11-07 (the user's words: "do rules 1 and 2"). The suite grew from 263 tests (Oct 1) to 18,879 (Nov 7), with one narrow test for each review finding.

1. A fix adds its case to the existing test of the same behavior (a table row), not a new test method. Add a new test only when no test covers the behavior.
2. Write or keep a rare-case test (an edge case, a refusal, malformed input, a failure path) only when its defect would show the user a wrong result, damage saved state (a file, Git, a ledger, staff, a config), or lose work (a commit, an edit, a message, a worker result). Every main-path test stays.

Places: the FixDispatch regression-test sentence ("writes, for each testable finding, the regression test that would have caught it"), the test rules line in `write_prompt_contract.md`, and the test worker prompt. Each rule has one place; the other places point to it. This phase is doc-only.

As built: both rules are in `docs/direct/write_prompt_contract.md` item 6, which implementation, fix and test prompts carry verbatim. FixDispatch, the main-read closure check and `<ClosureReview/>` ask for coverage under item 6, not one new regression test for each finding. The test worker paragraph points to item 6.

### Phase 10f — A checkpoint named for its plan without "-plan" gets its Producer  · status: done
**Depends on:** Phase 10d

From hana's showrunner, through sdlc, 2026-11-07. `change_review.py list-ready` printed `no staff row for unit info-panels; Producer stays empty` for demo's rows. demo's plan is `docs/hana/info-panels-plan.md`, its branch is `tool-based-ui-demo-36`, and it commits `checkpoint(info-panels): …`. The checkpoint aliases hold the plan stem (`info-panels-plan`), not the stem without `-plan`. Hana's main uses both forms for one plan: `info-panels-plan` and `info-panels`, `widget-enhancements-plan` and `widget-enhancements`.

Fix: the checkpoint aliases of a unit also hold its plan stem without a trailing `-plan`. Add no new check.

As built: `_branches_from_units` adds `plan.removesuffix("-plan")` right after the plan stem, so `checkpoint(info-panels)` resolves to demo.

### Phase 10g — A merge made on a local merge-gate branch adds its change rows  · status: done

**Depends on:** Phase 10d.

From hana's showrunner, through sdlc, 2026-11-08 [954e2ae6]. After two main promotions, `change_review.py list-ready --repo ~/rust/hana_catalyst --production hana` added no row. It missed three Change-Review lines in b7c17741de (merged as 5c444e5b40) and one in f75b3adb07 (merged as afdb0012db). hana added them by hand as C294-C297.

Cause: Phase 10d keeps only a merge whose subject ends ` into <merge branch>`. hana's merge branch is `init/catalyst`, but its merge-gate unit merges on a local branch, so every hana merge subject ends ` into tool-based-ui-merge-gate`. All 33 hana merges since 2026-11-06 fail that test.

Fix: skip a merge only when its subject ends ` into <B>`, where `origin/<B>` is the merge branch of a production outside this scan (the refs `reviewed_merge_refs` already gives). Keep every other merge. The rows hana added by hand must not come again. Add no new check.

As built: `list_ready` skips a merge only when its subject ends ` into <B>` for a branch `origin/<B>` in `outside_refs`. A scan of a copy of hana's list without C294-C297 adds those four rows with demo as Producer; with them, it adds none.

### Phase 10h — A merge finishes when the Mac is offline, and the Mac pulls later  · status: done

**Depends on:** none.

From the sdlc showrunner, 2026-11-08 [f15c6428], urgent. `merge_checkpoint.pull_mac_checkout` raises `Stop` when ssh cannot reach the Mac. The builds chain (17 merges) held from 13:43: builds 84 pushed and promoted on natedev, then `promote: failed - mac ~/.claude: connection timed out`, and a rerun failed again.

Fix: when ssh cannot reach the Mac, queue one pull-only Mac pull with the existing `main_publish` queue (the record `/zone` and `/tune` already use), report `promote ok: mac unreachable, pull pending`, and let the merge finish. The next promotion that reaches the Mac pulls once (a fast-forward carries every commit) and clears that record. A Mac that answers but whose pull fails still stops, as now. `tune.py` and the promotion share one queue-and-clear helper in `main_publish`. Add no other check.

As built: `pull_mac_checkout` returns `main_publish.MacPulled` or `MacPullPending`. After its last try, ssh exit 255 with no `rc=` line queues a pull-only record through `main_publish.queue_pull_only`; any other failure still raises `Stop`. A pull clears a pull-only record with `main_publish.clear_pull_only`, and `tune.py` uses the same two helpers. Both promotion paths report `mac unreachable, pull pending: <path>` and go on; `finish_promotion_targets` then skips the zone send and the Mac notice.
