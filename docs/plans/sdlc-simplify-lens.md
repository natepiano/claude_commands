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
2. Never drop a finding. A finding that changes behavior or reaches outside the phase is held for the user. The director lists it in the checkpoint notice. The showrunner files it as a user question and never settles it.
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
2. The showrunner files each item as a question for the user with `/showrunner:question`, led by the unit.
   The showrunner does not settle these items itself. This is a stated exception to its usual design authority.
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

Rerun: the summary is keyed by phase, pass and diff hash. A second launch on the same diff exits 0 with no new run.
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
   Write the prompt file. Reuse an existing summary for the same diff hash and exit 0.
5. `~/.claude/config/agents.conf`: one row with the Claude model and effort for the simplify run.
6. `~/.claude/config/direct.conf`: `SIMPLIFY_LENS=on|off`, read by `assign.py` at launch.
7. `~/.claude/config/review/simplify.md` (new): the report-only output form and the user's standing words.
8. `~/.claude/scripts/direct/progress_history.py` and `merge_checkpoint.py`: the review-trial line
   counts simplify findings and minutes on their own field, and `review_numbers` reads it.
9. Tests beside `assign.py`: the argv in `AGENT_EXEC_DRY_RUN` mode, the rerun with no new run, the `off` switch.

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

2. Item 18, `review.sh:521-533`, `assign.py:671-674`: publish the simplify cache only when the status reads `reviewed`, as a plain atomic copy of the summary. Effect: a failed run runs again on the same diff, and does not replay `error`. Status: approved by the user 2026-11-02; applied in phase simplify-empty.
3. Item 19, `assign.py:663-708`, `review.sh:521-539`: give `review.sh` one `--cache <file>` option that owns the lock, the replay and the publish. Effect: changes how a group SIGKILL stops a run. Status: replaced 2026-11-02 by the showrunner's design: `assign.py` hands control to `review.sh` with exec, and `review.sh` owns the lock, the reuse and the save. Applied in phase simplify-empty.
4. Item 20, `assign.py:684-700`: install `interrupt` for three signals and remove the restore loop and the `try/finally`. Effect: a forwarded SIGINT or SIGHUP no longer sends a second SIGTERM. Status: removed by the exec design in phase simplify-empty; no signal handlers remain.
5. Item 21, `progress_history.py:6135-6223`: take simplify minutes from `pass_elapsed_seconds` on `pass_finished` events with `called_task` `direct.simplify`. Effect: one key and one clock; minute values change by a small amount. Status: approved by the user 2026-11-02; filed in `docs/plans/sdlc-phases-next.md` for a later phase.
6. Item 22, `findings.py:724-729`: let `--held-for-decision` take any lens and refuse `--severity blocker`. Effect: the held state is open to all lenses; four doc lines change. Status: declined by the user 2026-11-02: "i haven't needed it so far so we're only making the exception for simplify for the time being - we may make it auto in the future".
7. Item 23, `review.sh:735`: do not give the simplify run the pending fast-track diffs. Effect: changes the documented rule that every broad review gets them (`docs/plans/sdlc-devtools-tests.md:303`). Status: declined by the user 2026-11-02: "i don't agree - simplify is now a standard review process".
8. Item 25, `review_regime.py:85-125`: add `optional_float` beside `optional_int` and use it for the four minute fields. Effect: edits three lines from before this phase. Status: approved by the user 2026-11-02 ("you never have to ask me to remove duplication"); filed in `docs/plans/sdlc-phases-next.md` for a later phase.
