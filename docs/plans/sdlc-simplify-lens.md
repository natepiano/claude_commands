# sdlc simplify-lens plan

> **Production:** sdlc (`docs/plans/sdlc-production.md`), unit phases-unit, worktree /home/natepiano/worktrees/claude-build-followups-sdlc-simplify, branch sdlc-simplify-lens (from origin/sdlc 6ed9311b3).

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions. This plan measures the phase process and the doc pipeline before it, and changes them one approved step at a time.
- **Project started:** 2026-10-20T20:53:27.319+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Layout:**
  - `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py` — the measure script and its tests (new)
  - `docs/plans/sdlc-phases-design.md` — the design doc (new)
  - read only, outside the repository: `~/.local/state/plan-delegate/runs/*.jsonl` (phase, pass and activity events), `~/.local/state/*/runs/*.jsonl` (other skill stores `history.py` reads), the usage records `scripts/usage/collect.py` reads, `/tmp/claude/unit/<run-id>/` session dirs (findings ledgers, prompts) where still present, `~/rust/hanadocs/issues` (proposals), git history of every worktree's `docs/plans/` and `docs/as-built/`
- **Key files:**
  - `commands/unit/direct.md` and `docs/direct/*.md` — the phase contract: every step a unit director runs
  - `commands/plan/to_phased_plan.md`, `phase_review.md`, `shrink.md`, `to_as_built.md` — plan compile, fold-in, shrink, as-built
  - `commands/unit/checkpoint.md`, `brief.md`, `phase_report.md`, `ci.md` — phase helpers
  - `docs/direct_plan_format.md` — the plan format; `docs/production_format.md` — <ProductionUnit/>
  - `commands/team_review.md`, `adversarial.md`, `api_review.md`, `module_review.md`, `astra_review.md`, `commands/producer/greenlight.md`, `to_production.md`, `commands/showrunner/proposal.md` — the doc pipeline before a plan

## Phases

### Phase simplify-lens — A four-agent simplify review lens in every phase  · status: done

User-approved 2026-11-02 via the sdlc showrunner. User requirements (these override the design below where they differ):

1. Use the four-agent form. Keep the Agent tool. Remove Edit, Write and NotebookEdit, and use plan mode.
2. Never drop a finding. A finding that changes behavior or reaches outside the phase is held. The director lists it in the checkpoint notice. It reaches the user only when it touches money, safety, product behavior the user sees, or an act that cannot be undone; the director settles every other held item.
3. The live check proves that the four agents start and that no file changes.
4. No settings.json change is planned. If the live check shows one is needed, ask the sdlc showrunner before changing it.
5. An on/off key in config, read at use.

Design (copied from the showrunner's proposal, 2026-11-02):

#### 2. Recommended design

/simplify becomes a fourth read-only lens of each phase's broad review. A script starts it.
Nobody types into a pane. Codex workers apply its findings in the normal repair round.

Trigger: <DualReview/> pass 1, in the same message that launches `adversary`, `contract` and `rerun`.
It runs once per phase, on the completed phase diff. Closure reviews and early reviews do not run it.

Order in a phase:

1. Codex workers implement. Lint and tests pass.
2. The director launches the broad review. The `simplify` lens runs beside the other lenses, all read-only.
3. <Synthesize/> sorts each simplify item into one of two groups. It drops no item.
   - In the phase: the item keeps behavior and stays inside the phase's own change.
     It opens as a `minor` finding with `--lens simplify`.
   - For a decision: the item changes behavior or reaches outside the phase's own change.
     It opens as a `simplify` finding held for a decision. No worker applies it.
4. <FixDispatch/> sends the in-phase batch to Codex workers. Lint runs once, then the affected tests.
5. A closure review reads the repair. Smoke, UX review and <PhaseEnd/> follow as now.

So the step comes before the review ends, before the checkpoint commit and before the merge gate.
No code changes after review without a closure review and a gate.

Items held for a decision (user, 2026-11-02, via Morty: "these changes may be desirable"):

1. The director sends each held item to the showrunner. It goes in the phase's checkpoint notice, with its finding id, the files and the change it proposes.
2. A held item goes to the user only when it touches money, safety, product behavior the user sees, or an act that cannot be undone. The showrunner files only those as user questions with `/showrunner:question`, led by the unit.
   The director settles every other held item under DecisionEconomy and states the call in one line
   (user, 2026-11-02, via builds: "you're asking me a bunch of questions that seem obvious - and design oriented - which ones really need me?").
3. The status line count and `/next question` show the item. The user approves or declines it.
4. Approved: the showrunner sends it to the owning unit as a `next` item, or as a phase when it is large.
   Declined: the finding closes with the user's words. In both cases nothing is applied before the answer.
5. The ledger keeps each held item until its answer, so a later phase or a compaction does not lose it.

Who does what:

- The unit director launches the lens with the same `assign.sh --role review --lens simplify` call as other lenses.
- `assign.py` sends this one lens to `agent_exec.sh simplify readonly`, not to the Codex review worker.
  That runs `claude --print --permission-mode plan` on the workers Claude account.
  It adds `--disallowedTools Edit,Write,NotebookEdit`. The Agent tool stays, so /simplify starts its four review agents
  (reuse, simplification, efficiency, altitude), as in the user's trial. Plan mode and the removed tools hold for those agents too, so no agent can edit.
- The prompt file is `${PHASE_DIR}/review_prompt_1_simplify.md`. Its first line is `/simplify <saved phase diff path>`.
  Then it says: report only, edit no file. It gives the output form and the user's words (below).
- Codex fix workers make every edit. The director writes no code.

Coordination with no person: the lens is part of the review that every phase already runs.
The showrunner does nothing new. A merge notice stays a report and starts nothing.

The user's extra words come from two places:

- Standing words: a new `~/.claude/config/review/simplify.md`, read at each run. The user or a showrunner edits it.
- Words for one unit: the showrunner relays them as now, `From the user (via the showrunner): ...`.
  The director passes them to its next simplify launch, and the saved prompt file records them.

Every simplify launch starts a fresh review, including a retry after failure.
The phase launch lock protects the active report and status from a second launch.
A failed or busy Claude run reports `error`. <DualReview/> step 6 names the missing lens and the phase goes on.

No worktree writes: plan mode and the removed tools stop edits. review.sh already fails a review when a file changes.

#### 3. Claude and Codex

- Claude unit director: the lens call above. The Claude tokens come from the workers account, not the units account.
- Codex unit director: the same call. The script starts the Claude run, so no Codex-only path and no new hook exist.
- Codex workers: apply the findings, as with every repair.
- Claude subagents: four per phase, started by /simplify inside one headless Claude process,
  the same kind `agent_exec.sh` already starts. The user chose this form on 2026-11-02 as a team review and accepts the token cost.
  It is a stated exception to the no-subagent rule, for this lens only. /simplify exists only in Claude, so the step needs that one run.

#### 4. Cost per phase

| Item | This design | Typing /simplify at the checkpoint |
| --- | --- | --- |
| Extra builds and tests | 0 when other lenses also find items; else 1 repair round: 1 lint, the affected tests | a second review, a second checkpoint and a second merge gate |
| Extra wall time | 0 unless simplify is the slowest lens; 1 repair round when it alone finds items | the full re-review and re-gate |
| Claude tokens | one four-agent run; the user's trial read about 4.4M input tokens, most from cache, in about 3 minutes | the same, in the director's own context |
| Director context | none; it reads only the summary | the whole /simplify run |

The trial is the only measure so far. The review-trial line records minutes and tokens from the first phase.

#### 5. Files to change

1. `~/.claude/docs/direct/dual_review.md`: <TeamReview/> gets a `simplify` row (what it reads, Claude run, report only).
   <DualReview/> step 3 launches it with the pass-1 lenses. <ClosureReview/> does not change.
2. `~/.claude/commands/unit/direct.md`: <Synthesize/> step 1 names `simplify` among the merged lenses.
   It sets severity `minor` for in-phase items. It holds the other items for a decision and drops none.
   <PhaseEnd/>'s checkpoint notice lists the held items.
2a. `~/.claude/scripts/direct/findings.py`: a held-for-decision state that no fix round takes and no phase end closes.
2b. `~/.claude/commands/showrunner/produce.md`: the showrunner files each held simplify item with `/showrunner:question`, and never settles one itself.
3. `~/.claude/scripts/usage/agents_log.py`: add `simplify` to the `Lens` type, so `assign.py` and `findings.py` accept it.
4. `~/.claude/scripts/direct/assign.py`: route lens `simplify` through `agent_exec.sh` with the removed tools.
   Write the prompt file before each fresh review.
5. `~/.claude/config/agents.conf`: one row with the Claude model and effort for the simplify run.
6. `~/.claude/config/direct.conf`: `SIMPLIFY_LENS=on|off`, read by `assign.py` at launch.
7. `~/.claude/config/review/simplify.md` (new): the report-only output form and the user's standing words.
8. `~/.claude/scripts/direct/progress_history.py` and `merge_checkpoint.py`: the review-trial line
   counts simplify findings and minutes on their own field, and `review_numbers` reads it.
9. Tests beside `assign.py`: the argv in `AGENT_EXEC_DRY_RUN` mode, a fresh review on the same diff, the `off` switch.

No change: `settings.json`, `checkpoint.md`, `session_pane.py`.
`production_format.md` gets one line only if the checkpoint notice format lives there and not in `phase_end.md`.

#### 6. Risks and open choices

1. Settled by the user, 2026-11-02: the four-agent form, as a team review, on the workers account. The higher token cost is accepted.
2. Larger repair rounds. Trial item 1 removed a database connection, a cache and retry state.
   Recommended: open them in round 1. The landing rules already move unfixed items to a follow-up phase.
3. The `craft` lens, a Codex quality lens, ended its trial on 2026-10-04 (fc3c4aea6).
   Recommended: run simplify as a trial. After 10 phases, the review-trial counts show its value.
4. Print-mode parsing comes from the 2.1.316 binary, not from a run.
   Recommended: the build phase's first acceptance test runs one live check on a scratch diff.
   The check also confirms that the four agents start in print mode and that no agent edits a file.
5. A Claude Code update can change /simplify's text. Recommended: Synthesize reads only the output form.
   A run that does not follow it counts as `error`, which never blocks a phase.
6. The user said "at phase checkpoints". This puts the step inside the phase, before the checkpoint.
   The result is the same simplified code, with no second review and no second merge gate.
7. Settings: keep `user-invocable-only`. Recommended: no flip. No session needs the model to call it.

#### Held simplify items

The phase's own live simplify run (2026-11-02, pass 1) raised 22 items. The phase took 21 of them; items 15-18 are covered by the item 14 repair. One item was held for the user's decision:

1. `scripts/agents/agent_exec.sh:136-138`: make every Claude `readonly` launch remove `Edit`, `Write` and `NotebookEdit`, and delete the simplify branch. Reason: tool removal follows the mode, not one task name. Effect: other readonly Claude runs (`review.sh` when `direct.review=claude`) become stricter. Status: approved by the user 2026-11-02; filed in `docs/plans/sdlc-phases-next.md` for a later phase.

The phase took items 14 and 22 although they reach `review.sh` and `test_agent_exec.sh`: the first launcher was a copy of `review.sh`'s fresh-reviewer path, and the user's no-copy rule removes copies.

The second live simplify run (2026-11-02, pass 1 through `review.sh`) raised 25 items. Item 1 was the same as closure finding F018, which the phase fixed. The phase took items 2-17 and item 24. Item 24 makes a failed usage-log write not stop the review; that write is new in this phase, and `prepare_session.sh` and `agent_bg.sh` already treat it this way. Seven items were held for the user's decision. The report text is in the phase's `review_findings_1_simplify.txt`.

2. Item 18, `review.sh:521-533`, `assign.py:671-674`: a failed review runs again on the same diff. Status: approved by the user 2026-11-02. The user later approved a fresh run for every simplify review.
3. Item 19, `assign.py:663-708`, `review.sh:521-539`: one process owns each simplify review. Status: replaced by the exec design in phase simplify-empty. `review.sh` owns the phase launch lock and result files.
4. Item 20, `assign.py:684-700`: install `interrupt` for three signals and remove the restore loop and the `try/finally`. Effect: a forwarded SIGINT or SIGHUP no longer sends a second SIGTERM. Status: removed by the exec design in phase simplify-empty; no signal handlers remain.
5. Item 21, `progress_history.py:6135-6223`: take simplify minutes from `pass_elapsed_seconds` on `pass_finished` events with `called_task` `direct.simplify`. Effect: one key and one clock; minute values change by a small amount. Status: settled by standing rule (one clock; two sources for one number is a copy; sdlc 2026-11-02); listed in `docs/plans/sdlc-phases-next.md` for the next fixes.
6. Item 22, `findings.py:724-729`: let `--held-for-decision` take any lens and refuse `--severity blocker`. Effect: the held state is open to all lenses; four doc lines change. Status: declined by the user 2026-11-02: "i haven't needed it so far so we're only making the exception for simplify for the time being - we may make it auto in the future".
7. Item 23, `review.sh:735`: do not give the simplify run the pending fast-track diffs. Effect: changes the documented rule that every broad review gets them (`docs/plans/sdlc-devtools-tests.md:303`). Status: declined by the user 2026-11-02: "i don't agree - simplify is now a standard review process".
8. Item 25, `review_regime.py:85-125`: add `optional_float` beside `optional_int` and use it for the four minute fields. Effect: edits three lines from before this phase. Status: approved by the user 2026-11-02 ("you never have to ask me to remove duplication"); filed in `docs/plans/sdlc-phases-next.md` for a later phase.

### Phase 2 — findings.py records the user's answer over a withdrawn ruling  · status: done

#### Work Order

**Goal:** `findings.py decision` records the user's own answer on a finding that already holds a withdrawn or non-user ruling, keeps both rulings in the history, and `verdict` can close a finding after its decision.

**Spec:**
1. Today `_decision` (`scripts/direct/findings.py:1219`) refuses any finding whose state is not `held_for_decision`. A finding that holds a showrunner ruling (state `decided`) cannot take the user's answer. Seen on simplify-empty F022, and on builds aws Phase 8 F038-F040.
2. Add `--withdraw "<why the current ruling no longer stands>"` to `decision`. With it, a `decided` finding takes the new answer:
   - move the current `decision` object into a `decision_history` list on the finding, adding `withdrawn_epoch` and `withdraw_reason`;
   - store the new answer as `decision`;
   - append one `finding_decision` event that carries `withdrew` (the old decision object).
3. Without `--withdraw`, a `decided` finding still refuses, and the message names `--withdraw`. `--withdraw` on a `held_for_decision` finding is refused. Every other state refuses as now.
4. Today `_verdict` (`findings.py:787-788`) refuses `decided` findings. Change it:
   - `held_for_decision` still refuses;
   - a `decided` finding with answer `approved` accepts `verdict` like an open finding (the repair was applied; this was simplify-follow F033);
   - a `decided` finding with answer `declined` refuses `verdict` with a message that it is closed by the decision.
5. `status` shows `decision_history` when present. The `gate` result and batches do not change.
6. Update the `decision` row in `~/.claude/commands/unit/direct.md` <FindingsLedger/> (in this repo: `commands/unit/direct.md`) to name `--withdraw`.
7. Tests in `scripts/direct/test_findings.py`: decision over a decided finding with and without `--withdraw`; the history keeps both; `--withdraw` on a held finding is refused; verdict after an approved decision is accepted; verdict after a declined decision is refused; verdict on a held finding is refused.

**Files:**
- `scripts/direct/findings.py` — `_decision`, `_verdict`, the `decision` parser, `status`
- `scripts/direct/test_findings.py` — the tests in Spec item 7
- `commands/unit/direct.md` — the `decision` row

**Workers:** 1 writer; the change is one module and its tests.
- `impl` — all three files

**State:** the findings ledger state file and its event log (`decision_history`, `finding_decision` events).

**Constraints from prior phases:** `decision` never applies the proposed change; it records words only. Only the user's own words go into `--user-words` (memory: record only the user's answer).

**Acceptance gate:** `python3 -m unittest discover -s scripts/direct -p test_findings.py` and `test_findings_simplify.py` pass; `basedpyright scripts/direct/findings.py scripts/direct/test_findings.py` reports 0 errors, 0 warnings, 0 notes.

### Phase 4 — test_verify_doc.py leaves no lint sweep after it ends  · status: done

**Depends on:** Phase 3

#### Work Order

**Goal:** `scripts/direct/test_verify_doc.py` passes every run. No detached lint sweep writes into the test's temporary home after the test ends.

**Spec:**
1. The failure: the temporary home cleanup fails with `Directory not empty: case/home/.local/state/lint-sweep`. A lint sweep child writes after the test ends.
2. `sweep_after_step` in `scripts/lint/invoke.sh` starts a detached sweep after each step. `LINT_SKIP_SWEEP=1` turns it off. The doc fixture (`scripts/direct/verify_doc_test_support.py`) removes every `LINT_` variable and does not set `LINT_SKIP_SWEEP`. Other verify fixtures set it (`verify_step_routes_test_support.py:69`, `test_verify_checks_fail_closed.py:97`).
3. Find the process that writes `home/.local/state/lint-sweep`, and confirm that `LINT_SKIP_SWEEP=1` stops it. If a different child writes there, stop that child at its cause.
4. Set `LINT_SKIP_SWEEP=1` in the doc fixture environment. Add no wait, retry or cleanup loop.
5. The phase adds no lines to staff or lifecycle code.

**Files:**
- `scripts/direct/verify_doc_test_support.py` — the fixture environment

**Workers:** 1 writer.
- `impl` — the fixture change and the proof

**Acceptance gate:** `python3 -m unittest scripts.direct.test_verify_doc` passes 5 runs in a row in one mesh run, and no `lint-sweep` folder is left under the case root; `basedpyright scripts/direct/verify_doc_test_support.py` reports 0 errors, 0 warnings, 0 notes.

### Phase 5 — assertion_guard.py accepts one ruling for a deleted mechanism  · status: done

**Depends on:** Phase 4

#### Work Order

**Goal:** one `Assertion-Change` line rules every removed test of a deleted mechanism.

**User ruling (to devtools, 2026-11-03 12:1x):** "yes the guard should accept one line for a whole deleted mechanism".

**Spec:**
1. The new form: `Assertion-Change: <path>::<test-name-prefix>* — <mechanism> deleted`. The trailing `*` marks a prefix ruling.
2. A prefix ruling rules each `test removed` finding in `<path>` whose test name starts with the prefix. It does not rule a `removed` or `weaker` assertion in a test that stays.
3. A prefix ruling that matches no removed test is blocking. The guard prints the ruling and says it matched no removed test.
4. The output for each ruled finding stays as now: `— ruled: <why>`.
5. Delete the per-test code that the prefix form replaces where you can. The tool must not grow by much. Net lines in `assertion_guard.py` must be 20 or fewer.
6. Docs: name the prefix form beside the per-test form in `commands/unit/direct.md` and `docs/as-built/phase-checks.md`. Add text; do not reword pinned phrases (`scripts/hooks/test_direct_command.py` pins them).
7. Tests, one per behavior, in `scripts/direct/test_assertion_guard.py`: a prefix rules two removed tests; a prefix does not rule a weaker assertion in a kept test; a prefix that matches no removed test blocks.

**Files:**
- `scripts/direct/assertion_guard.py` — `read_rulings`, `run`
- `scripts/direct/test_assertion_guard.py` — the tests in Spec item 7
- `commands/unit/direct.md`, `docs/as-built/phase-checks.md` — the prefix form

**Workers:** 1 writer.
- `impl` — all files

**Acceptance gate:** `test_assertion_guard.py`, `test_verify_assertion_guard.py` and `scripts/hooks/test_direct_command.py` pass; `basedpyright scripts/direct/assertion_guard.py scripts/direct/test_assertion_guard.py` reports 0 errors, 0 warnings, 0 notes.
