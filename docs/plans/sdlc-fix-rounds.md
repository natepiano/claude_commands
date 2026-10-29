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
**Depends on:** Phase 4

#### Work Order

**Goal:** the five approved changes and the Astra step are in the files `/unit:direct` runs from, the tools enforce them, and each one records the evidence Phase 7 reads.

**Spec:**

The user's answers (production doc, Review decisions F1–F6, 2026-10-28): P1 approved; P2 approved as a 30-exposed-phase trial from round 4; P3 approved as a 30-phase trial, plus the Astra step; P4 approved as a 30-phase accuracy trial, where a stale or missing phase refuses the launch; P5 approved as a 30-phase trial for all repairs. The approved **Change** text of each proposal is in `## Proposals` of `docs/plans/sdlc-fix-rounds-study.md` (P1 :976, P2 :1010, P3 :1043, P4 :1072, P5 :1090). Put that text into the named files with its words kept. Change a word only where a path or a command name in it is wrong now, and use the command names this Spec defines.

1. **One repair-round counter** (P2, P3, Astra). `scripts/direct/findings.py` counts repair rounds as `history.py rounds` numbers its repair intervals. Read this phase instance's events from the run record (`_history_root() / "runs" / f"{session_dir.name}.jsonl"`, the file `_phase_passes` reads), keep the events whose `phase_instance_id` is the ledger's, and call `scripts.history.rounds.phase_rounds(events)`. `repair_round = len(rounds) + 1` is the round about to be dispatched. With no run record, `repair_round` is the ledger number `len(_rounds(state)) + 1`. The gate payload and the `finding_gate` and `finding_batch_dispatched` events carry `repair_round` beside the existing `round`. The ledger number `round`, the convergence advisories and every limit in `config/direct.conf` keep their present meaning.
2. **Routed findings** (P2, P3). A new finding state `routed`. A routed finding is not resolved: it leaves the gating batch and the open counts, and `status` lists it under `routed`. New command `findings.py route --id F003 --reason <spec-gap|outside-kept> --owner <unit> --work-order "<plan path> — Phase <N> — <title>" --due "<the checkpoint that must close it>" [--evidence <why it leaves>]`. It refuses an empty owner, work order or due, a finding that is in flight, pending review or accepted, and a finding with an Astra review due (item 4) that has none recorded. It appends `finding_routed`: `finding_id`, `reason`, `owner`, `work_order`, `due`, `evidence`, `repair_round`, `round`, `fix_attempts`, `astra_approach`, timestamps. Routing is allowed in any round, so the landing rule in item 7 uses it too.
3. **Keep evidence from round 4** (P2, P3). New key `ROUTING_FROM_ROUND=4` in `config/direct.conf`, read like the other limits. When `repair_round >= ROUTING_FROM_ROUND`, `dispatch` requires one `--keep F003=<regression|broken|gate|ruling>:<evidence>` for each covered finding and refuses, naming each finding that has none: "route it with `findings.py route` or give the kept category and its evidence". The existing rule stays: every gating open finding that is not routed must be in `--covers`. So a mixed batch splits into kept findings with evidence and routed findings. The `finding_batch_dispatched` event carries `kept`: one entry per covered finding with its category and evidence.
4. **Astra review at each fix limit** (the user's addition to P3). New key `ASTRA_AFTER_FAILED_FIXES=2`. A gating open finding is due for an Astra review when `repair_round >= ROUTING_FROM_ROUND`, or when its `fix_attempts >= ASTRA_AFTER_FAILED_FIXES`, and it has no Astra record made at its current `fix_attempts`. The gate payload carries `astra_review_due: [ids]`. New command `findings.py astra --id F003 --note <path> --approach "<one line>"`, or `--unavailable "<reason>"` in place of `--approach` when `/astra_review` reports busy or unavailable. It refuses a note path that does not exist. It stores the record on the finding with the `fix_attempts` it was made at and appends `finding_astra_review` (`finding_id`, `note`, `approach`, `unavailable`, `fix_attempts`, `repair_round`). `dispatch` refuses to cover a due finding with no record, and `route` copies the newest approach into `astra_approach`. The note is short: the finding, each try so far and why each failed. The director runs `/astra_review --run-directory "${SESSION_DIR}" --phase <id> <note>` on it, and the approach goes into the next repair prompt, or into the follow-up Work Order when the finding is routed.
5. **Accepted checks on every repair** (P5). `dispatch` requires `--accepted-checks "<the accepted checks the repair's edits can break>"` or `--accepted-checks "none: <why no accepted behavior is reached>"`, and refuses an empty value. The value goes on the `finding_batch_dispatched` event as `accepted_checks`.
6. **Exposure records** (Phase 7 reads them). New command `findings.py exposure --proposal <P1|P2> --basis "<text>" [--decision "<text>"]` appends `trial_exposure` (`proposal`, `basis`, `decision`, `repair_round`). The director records P2 after `start-phase` and before the first dispatch when the Work Order changes visible layout, lifecycle or integration behavior, or rule or acceptance criteria. The director records P1 at each repair handoff whose Verification list holds lint or test commands, before it decides who runs them, with `--decision` "director ran the commands" or "worker: <the edit or judgment still pending>". The tools record the other three: `gate` appends `trial_exposure` for P3 when `repair_round >= ROUTING_FROM_ROUND` and an open gating finding has `fix_attempts >= 1`; `dispatch` records P5 through `accepted_checks` when the value does not start with `none:`; every launch records P4 through its launch check (item 7).
7. **Launch check** (P4). `progress_history.py start-pass` refuses a launch and appends `launch_refused` (`reason`, `pass_kind`, `phase_id`, `prompt_file`) to the run record when:
   - no `--phase-id` is given while a run is recorded (a missing phase). `implement.sh` and `review.sh` pass `--phase-id` whenever they have `--phase`, as now, and stop with the recorder's message when it refuses;
   - the named phase is not open (a stale phase; this refusal exists now and gains the event);
   - the new `--prompt-file <path>` holds a line that starts `Phase <id> — ` and names an id or title other than the open phase's (a stale prompt). A prompt with no such line passes. Both launchers pass their prompt file;
   - `pass_kind` is `fix` and the phase's ledger has no round in flight, unless `--fix-cause "<the failed gate or defect>"` is given. `implement.sh` takes `--fix-cause <text>` before its positional arguments and passes it on.

   `pass_started` carries `launch_check`: `phase_id`, `phase_title`, `prompt_phase_line` (`matched` or `absent`) and `fix_cause` (the ledger round and its covered ids, or the given text).
8. **Open routed work is visible.** New read-only command `history.py routed [--open] [--since <time>] [--format text|json]`. It lists every `finding_routed` event from the run records, with its run (the file name), plan, phase, finding, owner, work order, due checkpoint, age and closure. New command `findings.py close-route --from <run id>:<phase instance id>:<finding id> --evidence "<commit, test result, review>"` appends `finding_route_closed` to the calling session's run record. The director of the receiving phase runs it at that phase's checkpoint. `routed` joins the closure by that key, and `--open` lists only the unclosed. `commands/history.md` gets one line for it.
9. **The rule text.**
   - `docs/production_format.md` item 7: the P2 and P3 **Change** text. Then the Astra step: "At each fix limit, try something different. When a phase reaches repair round 4 with a finding still open, or a kept finding has failed two fixes, the director writes a short note: the finding, the tries so far and why each failed. It runs `/astra_review` on the note. Astra's approach goes into the next try, or into the follow-up Work Order when the finding moves out (the user, 2026-10-28)." The existing "A fix that fails twice" bullet stays and points to this step. Name `findings.py route`, `--keep` and `astra` as the tools that enforce it.
   - `commands/unit/direct.md` `<FixDispatch/>`: the P1 **Change** text after its Verification paragraph, the P5 **Change** text beside the regression-test paragraph, and the P2, P3 and Astra steps where the batch is formed (`route`, `--keep`, `astra`, `--accepted-checks`). `<FindingsLedger/>`: table rows for `route`, `close-route`, `astra` and `exposure`, the new `dispatch` options and the `routed` state. `<Synthesize/>` step 5: the gate's `repair_round` and `astra_review_due`, and what the director does with each. `<PassOwnership/>`: the P4 **Change** text and the launch check.
   - Every new rule names its source as "(the user, 2026-10-28)".
10. **Tests.**
   - `scripts/direct/test_findings.py`:
     - a batch that mixes kept and routed findings dispatches;
     - a round-4 dispatch without `--keep` is refused;
     - routing starts at the round that `phase_rounds` counts, not at the ledger number, with a run record built so the two differ;
     - `astra_review_due` fires on each trigger and clears after an Astra record;
     - `dispatch` refuses without `--accepted-checks`;
     - each new event carries every field this Spec names. A test fails when a field is missing.
   - `scripts/direct/test_progress_history.py`: each launch refusal and its `launch_refused` event; a matching prompt line and an absent one both pass.
   - `scripts/direct/test_implement_launcher.py` and `scripts/direct/test_review_launcher.py`: the launchers pass `--prompt-file` and stop on a refusal.
   - `scripts/history/test_routed.py`: open and closed routed work, and the join by key.
   - The fixture configs in these tests gain the two new keys.

**Files:**
- `scripts/direct/findings.py` — the counter, the `routed` state, `route`, `close-route`, `astra`, `exposure`, and the `--keep` and `--accepted-checks` checks in `dispatch`.
- `config/direct.conf` — `ROUTING_FROM_ROUND=4`, `ASTRA_AFTER_FAILED_FIXES=2`, each with a comment that names its source.
- `scripts/direct/progress_history.py` — the launch check in `start-pass`, and `--prompt-file`, `--fix-cause` and `launch_refused`.
- `scripts/direct/implement.sh` — passes `--prompt-file` and `--fix-cause`; takes `--fix-cause`.
- `scripts/direct/review.sh` — passes `--prompt-file`.
- `scripts/history/routed.py` — new: the `routed` command.
- `scripts/history/history.py` — the `routed` subparser.
- `commands/history.md` — one line for `routed`.
- `commands/unit/direct.md` — `<FixDispatch/>`, `<FindingsLedger/>`, `<Synthesize/>`, `<PassOwnership/>` (owner phases-unit; models-unit co-owns).
- `docs/production_format.md` — item 7 (owner roles-unit).
- `scripts/direct/test_findings.py`, `scripts/direct/test_progress_history.py`, `scripts/direct/test_implement_launcher.py`, `scripts/direct/test_review_launcher.py` — the tests above.
- `scripts/history/test_routed.py` — new: tests for `routed`.

**Workers:** `1 writer + 1 tester`
- `impl` — every file above except the test files; hub: `config/direct.conf` (both workers' fixtures read its keys).
- `test` — the five test files, written from this Spec alone.

**Constraints from prior phases:**
- `scripts/history/rounds.py` stays an importable API: phases-unit imports it, and `phase_rounds` and every `Round` field stay unchanged.
- Rounds are counted as `history.py rounds` counts repair intervals, with the time-ordered batch join. A ledger-only round has zero worker time.
- Issues P699–P703 in `~/rust/hanadocs/issues/` track P1–P5. The plan header names them. The run's as-built pass closes them (`docs/production_format.md` item 17).
- The study's proposals keep their titles and numbers P1–P5.
- The edits to `commands/unit/direct.md` and `docs/production_format.md` were cleared by the showrunner (2026-10-28). The checkpoint notice names each one as `also touches <path> (owner <unit>), tested against <owner tip>`, and also names the build-followups owner.

**Acceptance gate:**
- `env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/merge_gate.py --checkout <worktree> --tests-only` green, run from `~/.claude` main.
- `basedpyright scripts/direct scripts/history scripts/production` reports `0 errors, 0 warnings, 0 notes`.
- The named tests pass, and each one fails without the change it pins.
- A dry run in a scratch session dir shows each refusal: a round-4 dispatch without `--keep`, a due finding without an Astra record, a dispatch without `--accepted-checks`, a launch with no phase, and a launch with a stale prompt line.

### Phase 6 — The round summary counts reviewed phases only  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** the `history.py rounds` summary reports the median, the share of phases with 3 or more rounds and the fix-time share over reviewed phases only, and counts the unreviewed phases on their own line.

**Spec:**

Change review row C2 (`/history rounds`, merged 54774de4) came back from the user (via the sdlc showrunner, 2026-10-28): "does not work". The user asked "how can the median be 0 rounds?" With `--since 60d --status all`, 1456 of 2542 phases show 0 rounds, and 1167 of those have no review recorded (0 reviews when joined to `history.py phases` on run and phase). Among reviewed phases the median is 1.

1. **A reviewed phase** has at least one recorded review pass: a `pass_started` event with `pass_kind` `review` in the same run with the same `phase_instance_id`. Count them the way `history.py phases` counts its `reviews` column, so a phase reads as reviewed in both commands or in neither.
2. **The summary statistics** in `rounds.run` use reviewed phases only: the median rounds, the phases with 3 or more rounds and their share, and the share of fix time those phases hold. The counted line names both numbers, for example `2542 phases counted; 1375 reviewed; median 1 rounds over reviewed phases`.
3. **Unreviewed phases** get their own summary line with a count, for example `1167 phases with no review recorded (not in the median or shares)`. They stay in the table and the JSON rows, and each row carries `reviewed` (true or false).
4. **The JSON `_meta`** adds `reviewed_phases` and `unreviewed_phases`. `median_rounds`, `heavy_phases`, `heavy_phase_share` and `heavy_fix_share` use the reviewed set. `phases` stays the count of all listed phases.
5. **The API stays.** `phase_rounds`, `Round` and every existing `PhaseRounds` field are unchanged; phases-unit imports them. `PhaseRounds` may gain one field (`reviews: int`) when `load_phases` can count it from the events it already reads.
6. **Tests** in `scripts/history/test_rounds.py`: a set with unreviewed zero-round phases gives the median of the reviewed phases; the unreviewed count line; the `reviewed` row field and the `_meta` fields; a phase with a review and no repair counts as reviewed with 0 rounds.
7. **Docs.** The Phase 1 As-built summary-line text and the `rounds` line in `commands/history.md` say that the statistics cover reviewed phases.
8. **Study summary** (row C5, returned by the showrunner, 2026-10-28). `docs/plans/sdlc-fix-rounds-study.md` is 1106 lines and has no summary at the top. Add a short summary under its title: 15 phases, 260 rounds, 271 h; fix-failed 36% of rounds and 49% of time; spec-gap 17% and 19%; mechanical 15% and 11%. Take each number from `## Totals` and check it there. Remove the "median 0 rounds" figure. Every median in a footer or summary line of the study counts reviewed phases only, as item 2 does.
9. **A check that shows the live cause** (row C6). The Change-Review line gives a command that prints the live cause for phase `8013edcb`, for example a `--format json` `jq` filter that prints `[2,"live",...]`.
10. **A short check for one phase's rounds** (row C7). The old check printed 397 KB of JSON. The Change-Review line gives a filter that prints only the two round lines of run `8f4c2e88` phase 38 (round 39: F001–F006, round 40: F007–F009).

**Files:**
- `scripts/history/rounds.py` — the reviewed count and the summary.
- `scripts/history/test_rounds.py` — the tests above.
- `commands/history.md` — the `rounds` line.
- `docs/plans/sdlc-fix-rounds-study.md` — the summary at the top (item 8).

**Workers:** `1 writer + 1 tester`
- `impl` — `scripts/history/rounds.py`, `commands/history.md`, `docs/plans/sdlc-fix-rounds-study.md` (item 8), and the two check commands for items 9 and 10, each run on the live records with its output in the summary.
- `test` — `scripts/history/test_rounds.py`, written from this Spec alone.

**Constraints from prior phases:** `rounds.py` is an importable API (Phase 1 **Binds later work**); `history.py routed` (Phase 5) reads the same run records and is not changed.

**Acceptance gate:**
- `env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/merge_gate.py --checkout <worktree> --tests-only` green, run from `~/.claude` main.
- `basedpyright scripts/history` reports `0 errors, 0 warnings, 0 notes`.
- `history.py rounds --since 60d --status all` on the live records prints the reviewed count, the unreviewed line and a median over reviewed phases; the reviewed count matches `history.py phases --since 60d` rows with reviews of 1 or more, joined on run and phase.
- The item 9 and item 10 commands print what their rows ask for when run on the live records, and the study summary numbers match `## Totals`.
- Change-Review lines, one per returned row (C2, C5, C6, C7): what changed | the check command | what it prints. Each fix comes back as a new row.

### Phase 7 — Did the changes cut rounds and keep the work  · status: todo
**Depends on:** Phase 5
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
