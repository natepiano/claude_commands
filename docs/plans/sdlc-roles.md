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
- **Roles with no defining file:** the small-fix helper (memory `small-fixes-by-subagent-in-main`), the nightly review, the postmaster and the notifiers.
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

#### As-built
- **Candidates** holds P-A/P-B (where duties are written), O20/O22/O23/O32 (owners of conflicting or ownerless duties), T-A/T-B (where definitions live), N-A/N-B (scoped names or documented aliases) and G-A/G-B (merge-gate check for retired uses or a periodic review). **Comparison** scores each one on each measure and user rule in common words.
- **Recommendation** picks N-A with G-A to stop drift. P-A and T-A win by a small margin; on reading they are near-ties. It keeps the any-time, stop-at-once rename and corrects PF.
- **Steps** holds three phase-ready steps: 1 retires singular and plural worker-seat prose with its check (19 measured files plus 4 test files, expected reduction n=66, n=18 kept); 2 names the coordinator by its role; 3 corrects current command references and keeps compatibility. **Later work** holds the other candidates.
- The duty matrix adds D53–D55 and corrects D17 and D26. The term table corrects the lease and gate meanings. Q9 asks about the phase-boundary rename alternative.

**Files:**
- `docs/plans/sdlc-roles-design.md` — duty matrix (D01–D55), term table, baseline, friction, Q1–Q9, Candidates, Comparison, Recommendation, Steps, Later work and Next steps.

**Binds later work:**
- Step 1 is the first candidate for an approved step. Only a numbered phase-ready step becomes a build Work Order; a Later work item needs exact files, dependencies and before/target counts first.
- Step 1's build phase first replays the plural-aware check over the commits after the seat retirement, before any rename. If the replay reverses the case for G-A, the phase stops and asks again.
- Plural matching and per-file kept contexts are new code. Test files change but stay outside the count. The `direct.md` edit goes to phases-unit.

**Gotchas:**
- `roles_audit.py` `name_pattern` matches the singular form only. The audit skips `test_` files, but tests pin user-visible text.
- `banned_words_lib.py --diff` waits on stdin unless run with `</dev/null`.

**Ruled out:**
- O23-B (rename only at a phase boundary): it reverses the user's any-time rename rule unless Q9 changes it.
- Claims of fewer wrong-party asks: message records have no sender or recipient roles.
- D16 as a duplicate duty: it has one owner.

### Phase 3 — Build the first approved step  · status: done
**Depends on:** Phase 2, and the user's approval of Step 1

#### Work Order

**Goal:** the step the user approved is live, and its measure is recorded before and after.

**Spec:**
- Before dispatch, the unit director copies the approved step from the design doc's Steps into this Spec verbatim (files, the change, the measure, how to undo it) and fills **Files** and **Workers** from it. A phase with no approved step does not dispatch: it waits on the showrunner's answer.
- Record the step's measure with `roles_audit.py baseline` before the change, into the design doc under **Step log**.
- An edit to `commands/unit/direct.md` goes to phases-unit as a change request through the showrunner; this phase does not edit it. Edits to `docs/production_format.md` and `commands/showrunner/produce.md` merge with any pending edits from guilds-unit or fix-rounds-unit that touch the same tags; the Work Order names each.
- A rename changes every live file the measure names in one phase, and adds the old name to `role_terms.toml` as `retired`.

- **Approved step** (the user, 2026-10-28 13:18 EDT, "yes to all"; Step 1 copied verbatim from the design doc at 5ebb4dc2, `### 1. Retire singular and plural worker-seat prose with its check`):

> **Needs the user and dependencies:** Q7, R-seat and G-A approval. Keep worker duties and any-time rename behavior. The question carries the measured source-only scan time (Comparison). The build phase first replays a plural-aware prototype over the commits after the retirement (`4ddd7b52..HEAD`, n=21 commits that add the word) and counts real flags against false failures and run time; if that reverses the case for G-A, the phase stops before any rename and asks again. The implementation estimate covers matcher, context data, fixtures and gate test.
>
> **Change files — live prose:** `commands/showrunner/rename_unit.md`; `commands/showrunner/retire_unit.md`; `commands/unit/direct.md`; `docs/production_format.md`; `docs/as-built/agent-registry.md`; `docs/as-built/long-lived-workers.md`; `docs/as-built/shutdown-and-restart.md`; `docs/as-built/unit-rename.md`; `docs/delegate/dual_review.md`. Phases-unit supplies the director edit; hub edits use the owning unit's normal flow. Change worker-role labels, not runtime behavior. Treat `Codex seats` and `Codex seat` before the shorter words.
>
> **Change files — scripts:** `scripts/agents/codex_mesh.py`; `scripts/agents/mesh_runs.py`; `scripts/direct/implement.sh`; `scripts/direct/phase_reservation.py`; `scripts/production/rename_unit.py`; `scripts/production/retire_unit.py`; `scripts/shutdown/restart.py`; `scripts/direct/board.sh`; `scripts/direct/review.sh`; `scripts/hooks/pre-tool-use-berth.py`. Change worker-role comments, docstrings, help, printed labels and messages, including `restart.py:1975`, `mesh_runs.py:272–275` and the rename/retire descriptions. The last three files contain role prose missed by string extraction; their edits earn no extra count credit. Keep identifiers, API fields, flags, transport addresses, filenames, placeholders documenting those contracts and historical identities.
>
> **Change files — measure and gate:** `scripts/production/role_terms.toml`; `scripts/production/roles_audit.py`; `scripts/production/test_roles_audit.py`. Add explicit inflected forms for the worker aliases to the data and use those forms in both Names and `check`; do not apply an English plural rule to every command or term. Add per-file kept contexts with reasons, instead of extending today's global masking. In `test_roles_audit.py`, add fixture tests for singular/plural matches, overlap, protocol/history contexts, a context copied into another file, changed kept text and a returning retired use. Add a source-only repository check test in this same discovered test file, so existing `merge_gate --tests-only` runs it in the unit and at merge. No new merge-gate dispatch code is needed. It reads repository sources, not home-state records. Tests that prove behavior must still fail on an eligible re-add.
>
> **Change files — assertions of changed output:** `scripts/shutdown/test_restart.py:435`, `scripts/production/test_rename_unit.py:553`, `scripts/agents/test_mesh_runs.py:183`, and `scripts/production/test_retire_unit.py:214`. Update their role-label expectations with the source output. Keep checks for thread identity, launcher arguments, worker names, refusal behavior and dry-run side effects. The expanded search found the retire dry-run assertion in addition to the first review's pinned tests. Checked `scripts/agents/test_codex_mesh.py` reopen assertions and `scripts/direct/test_implement_launcher.py` claim refusal assertions retain their existing expectations: those strings do not change. Test files stay outside the Names denominator because `read_live_files` skips `test_` scripts.
>
> **Reviewed and kept live files:** `docs/as-built/message-clarity.md:269` is a retirement record preserved at commit `291e1da55`, dated 2026-10-16 by blame; keep its old word as history, not an allowed role label. `docs/as-built/usage-ledger.md:46` and `scripts/usage/collect.py:532–534` document or implement the old `seats` data value migration; keep those exact values. These files belong to the measured source set and are reviewed in this phase, not deferred to a later rename. Raw source matches in `scripts/accounts/worker_names.py`, `scripts/agents/agent_bg.sh`, `scripts/buildlog/index.py`, `scripts/direct/worker_state.py`, `scripts/feature_log/inventory.py`, `scripts/shutdown/inventory.py`, `scripts/shutdown/record.py` and `scripts/shutdown/stop.py` are identifiers, stored keys or migration values, not worker-role prose. Keep them. This classification covers every worker-role singular/plural use found by the read-only search over the audit's scanned folders, plus uncounted source prose above; it makes no claim about files the scanner excludes.
>
> **Director-owned memory cleanup:** review agent-read prose under `~/.claude/projects/*/memory/` with the approved names. Correct current role explanations as director-owned cleanup, outside this worker's repository Work Order. Keep dated history, identities and quoted command output as recorded. Memory folders are not in the audit's Names set and earn no count credit. Do not send a question to the user for routine memory maintenance under an already approved naming policy.
>
> **Read-only classification, 2026-10-20:** the following table uses the current audit file filters, extraction and global kept-path masking, then applies `(?<![\w-])seats?(?![\w-])` case-insensitively. It adds only the explicit plural form to today's singular matcher. Each cell is eligible / kept. Every occurrence not listed in the kept contexts below is eligible role prose. Script and combined counts remain lower bounds. Source-only comments/docstrings excluded by the extractor receive no numeric credit.
>
> | Measured live file | Singular eligible / kept (n) | Plural eligible / kept (n) |
> | --- | --- | --- |
> | `commands/showrunner/rename_unit.md` | 5 / 0 | 2 / 0 |
> | `commands/showrunner/retire_unit.md` | 0 / 0 | 2 / 0 |
> | `commands/unit/direct.md` | 8 / 0 | 0 / 0 |
> | `docs/as-built/agent-registry.md` | 1 / 0 | 1 / 0 |
> | `docs/as-built/long-lived-workers.md` | 5 / 0 | 0 / 0 |
> | `docs/as-built/message-clarity.md` | 0 / 0 | 0 / 1 |
> | `docs/as-built/shutdown-and-restart.md` | 20 / 11 | 7 / 3 |
> | `docs/as-built/unit-rename.md` | 1 / 0 | 1 / 0 |
> | `docs/as-built/usage-ledger.md` | 0 / 0 | 0 / 1 |
> | `docs/delegate/dual_review.md` | 1 / 0 | 0 / 0 |
> | `docs/production_format.md` | 0 / 0 | 1 / 0 |
> | Prose subtotal, n=11 files | 41 / 11 | 14 / 5 |
> | `scripts/agents/codex_mesh.py` | 2 / 0 | 0 / 0 |
> | `scripts/agents/mesh_runs.py` | 1 / 0 | 0 / 0 |
> | `scripts/direct/implement.sh` | 1 / 0 | 0 / 0 |
> | `scripts/direct/phase_reservation.py` | 0 / 0 | 1 / 0 |
> | `scripts/production/rename_unit.py` | 1 / 0 | 2 / 0 |
> | `scripts/production/retire_unit.py` | 0 / 0 | 2 / 0 |
> | `scripts/shutdown/restart.py` | 1 / 0 | 0 / 0 |
> | `scripts/usage/collect.py` | 0 / 0 | 0 / 2 |
> | Script subtotal, n=8 files | 6 / 0 | 5 / 2 |
> | Total, n=19 measured live files | 47 / 11 | 19 / 7 |
>
> **Kept contexts:**
>
> - Stored field: `docs/as-built/shutdown-and-restart.md:116`, literal `seat` (n=1 singular).
> - Command/output placeholders: that file `:120`, `--to <seat>`, `reopened <seat>`, printed `--to <seat>` and `codex_mesh: <seat>: <reason>` (n=4 singular); `:143`, `<seat> (<role>, was working|idle): <command>` (n=1 singular). These document protocol arguments; ordinary role prose and the newly changed live message quotation change together.
> - Filename placeholders: that file `:116`, `<run_dir>/<seat>.resume-prompt.md`, and `:122`, `<seat>.account-move.json` (n=2 singular). Today's masking leaves those placeholders partly visible; keep that fact in the lexical report.
> - Dated probe history: that file `:290`, the 2026-10-16 run narrative (n=3 singular, n=3 plural). Keep its recorded wording and worker identity. Unlike the prior classification, this classification retains the full dated record, not just its identity.
> - Retirement history: `docs/as-built/message-clarity.md:269` at `291e1da55`, dated 2026-10-16 (n=1 plural).
> - Legacy data value: `docs/as-built/usage-ledger.md:46` (n=1 plural) and `scripts/usage/collect.py:532–534` SQL migration values (n=2 plural). They remain inputs the migration must recognize, not names for current workers.
>
> **Before and target:** singular n=58 reconciles with the checker's n=58 exactly; plural adds n=26, giving n=84 in n=19 live files. Eligible n=66 comprises prose n=55 and script n=11; kept n=18 comprises prose n=16 and script n=2. Singular eligible is n=47 and plural eligible n=19. With the same extraction the lexical result should become singular n=11 plus plural n=7, total n=18, while scoped eligible uses become zero. Phase 4's half-size test uses n=66 as the expected reduction. Re-run this classification before the build Work Order; source drift requires a new frozen classification. Do not claim adding plural detection or retaining more history as a rename saving. Keep the singular-only before/after report as well as the expanded report. `Codex seat` and `Codex seats` overlap these matches and earn no extra credit. Definitions, production names and sent contacts have no target here.
>
> **Why first:** this narrow role meaning has a classified plural-aware pool and a documented drift event after retirement. G-A adds matcher, context data, fixture and integration work, but later approved retirements reuse it. The judge supports drift prevention, not a measured work rate. The ranking remains conditional on the false-failure replay at the start of the build phase; a prose count alone does not rank implementation effort. This keeps the first phase measurable without pretending the other broad aliases are classified defects.
>
> **Undo:** revert role prose and pinned expectations with the inflection/retirement entries and check test. Preserve identities and migration inputs. Keep the before records. No live session, transport key, file path or thread moves during this rename or its undo.

- **Amendments since the step was written** (unit director, 2026-10-28; showrunner points of 2026-10-28):
  - **Re-classify first.** The merge of `origin/sdlc` (307 commits) moved the source: a raw count over the audit's 563 live files now finds singular n=69 and plural n=29 (was 58 and 26). New files with uses: `commands/showrunner/transfer_unit.md`, `scripts/production/transfer_unit.py`, `docs/as-built/build-memory-admission.md`, `docs/as-built/guilds.md`; `docs/as-built/shutdown-and-restart.md` grew. The first task, before any rename, freezes a new classification with the step's method (eligible / kept per file, kept contexts with reasons) and writes it with the `roles_audit.py baseline` before measure into the design doc's **Step log**. The new files join the change set by the same rules. The 2026-10-20 replay stands (sent to the showrunner); it is not run again.
  - **`commands/unit/direct.md` is not edited here.** phases-unit owns it. `impl` writes the exact replacement text for each eligible use in it into the Step log; the unit director sends that to phases-unit as a change request through the showrunner. Its uses stay counted as eligible until phases-unit lands them; the check's kept list names them with that reason until then.
  - **Hub files.** Read the `<Guild>` sections guild-process added to `commands/showrunner/produce.md` and `docs/production_format.md` before editing either file; change only worker-role words there.
  - **Fix in this phase:** `role_terms.toml` lines 185 and 191 name a plan file in another worktree (`sdlc-guilds.md`) and `docs/plans/sdlc-production.md`; point each `defined_in` at the live defining file (`docs/as-built/guilds.md` where it defines the role). Its kept path `docs/delegate/` is stale: those files moved to `docs/direct/`; keep both until no live file is under `docs/delegate/`.
  - **Approved names that touch this step** (the user, sdlc review): "worker" replaces "seat" for a Codex thread a unit director sends work to; "small-fix helper" is a showrunner's helper that does no code work. Q2 and Q3 answers (a "wait" is one unit waiting on another; a "gate" is a check that must pass; reviewers are named design reviewer, code reviewer, UI reviewer and designer) go into the design doc's Questions and term table as answered, with no source rename in this phase.
  - **Read before relying on the duty map:** hierarchies' Map changed in `d14a7368`.

**Files:**
- `docs/plans/sdlc-roles-design.md` — Step log (before measure, frozen classification, the direct.md change request text), Questions and term table answers
- `commands/showrunner/rename_unit.md`, `commands/showrunner/retire_unit.md`, `commands/showrunner/transfer_unit.md`, `docs/production_format.md`, `docs/as-built/agent-registry.md`, `docs/as-built/build-memory-admission.md`, `docs/as-built/guilds.md`, `docs/as-built/long-lived-workers.md`, `docs/as-built/shutdown-and-restart.md`, `docs/as-built/unit-rename.md`, `docs/direct/dual_review.md` — live prose
- `scripts/agents/codex_mesh.py`, `scripts/agents/mesh_runs.py`, `scripts/direct/implement.sh`, `scripts/direct/phase_reservation.py`, `scripts/production/rename_unit.py`, `scripts/production/retire_unit.py`, `scripts/production/transfer_unit.py`, `scripts/shutdown/restart.py`, `scripts/direct/board.sh`, `scripts/direct/review.sh`, `scripts/hooks/pre-tool-use-berth.py` — scripts
- `scripts/production/role_terms.toml`, `scripts/production/roles_audit.py`, `scripts/production/test_roles_audit.py` — measure and gate
- `scripts/shutdown/test_restart.py`, `scripts/production/test_rename_unit.py`, `scripts/agents/test_mesh_runs.py`, `scripts/production/test_retire_unit.py`, `scripts/production/test_transfer_unit.py` — assertions of changed output
- `docs/as-built/rustdoc-once-and-final-targets.md`, `scripts/berth/install/README.md`, `scripts/shutdown/inventory.py` — worker-role prose the `origin/sdlc` merge brought; the unit director's doc-only fix (F006)
- `commands/unit/direct.md` — the 9 worker-role word changes from the Step log, applied by the unit director (showrunner's call, 2026-10-28: phases-unit is paused); phases-unit owns the file and gets an also-touches line
- `docs/as-built/message-clarity.md`, `docs/as-built/usage-ledger.md`, `scripts/usage/collect.py` — read only; kept history and migration values
- `commands/showrunner/produce.md` — read only unless the re-classification finds an eligible use


**Workers:** `1 writer + 1 tester`, split by file set.
- `impl` — the design doc, every live prose file and every script above; it freezes the classification first and writes the Step log
- `test` — `role_terms.toml`, `roles_audit.py`, `test_roles_audit.py` and the assertion test files; it builds the plural-aware forms, per-file kept contexts and the source-only check test, and updates pinned expectations after `impl` posts each changed label on the board

**Constraints from prior phases:** the Steps section and the user's answer, plus these facts from Phase 2:
- Step 1 (as committed in e02e6370) covers 19 measured files plus 4 test files that assert the changed text (`scripts/shutdown/test_restart.py`, `scripts/production/test_rename_unit.py`, `scripts/agents/test_mesh_runs.py`, `scripts/production/test_retire_unit.py`). The expected reduction is n=66 (singular and plural), with n=18 kept uses; re-run the classification before dispatch.
- The match becomes plural-aware through explicit inflected forms in `role_terms.toml`; `roles_audit.py` today matches the singular only (`name_pattern`) and masks kept strings globally (`without_paths`), so per-file kept contexts are new code.
- The phase's first task, before any rename, replays a plural-aware check prototype over `4ddd7b52..HEAD` (n=21 commits add the word after its retirement) and counts real flags against false failures and run time. If that reverses the case for the check, the phase stops and asks again through the showrunner.
- The check runs as a test that `merge_gate --tests-only` discovers. A source-only scan of all n=546 files and n=36 terms took 10.5–10.8 s at load 50–54 on 32 cores (2026-10-20).
- Memory prose that uses the retired word is director-owned cleanup with no count credit; quoted command output stays as recorded.

**Acceptance gate:** the gates in Delegation Context green; the change is live on main per the production's rule for `~/.claude`; the Step log has the before measure, the command and its file set or window.

### Phase 4 — Each session records its role apart from its name  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** every session records its role apart from its name, so many sessions can hold one role and no session needs a role in its name.

**Spec:**
- Decided by the user (R1b, 2026-10-21; production doc Decisions): a session's name and its role are separate. Every session records its role apart from its name. roles-unit defines the role record; the unit board's owner and cargo-liner add the name and role columns (the showrunner asks them).
- Packaging: the showrunner's call, 2026-10-28 — this phase follows Step 1. Bring the user only a choice R1b leaves open.
- The unit director writes the Work Order from the current session records at phase start (roster, `showrunners.py`, `units_table.py`), with the role values from the approved names (Q3: design reviewer, code reviewer, UI reviewer, designer; Q6: showrunner, unit director; Q7: worker, small-fix helper).

**Files:** set at phase start from the records the role field joins.

**Workers:** set at phase start.

**Constraints from prior phases:** the approved names; Step 1's worker words.

**Acceptance gate:** the gates in Delegation Context green; each role-holding record type has a role field apart from its name; the record definition is in the design doc and sent to the showrunner for the column owners.

### Phase 5 — Measure the first step and propose the next  · status: todo
**Depends on:** Phase 4

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
