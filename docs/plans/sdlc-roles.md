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

#### As-built

- Live prose, commands and scripts call a Codex thread that a unit director sends work to a "worker". Role labels changed; runtime behavior, identifiers, API fields, flags, transport addresses and filenames did not.
- `commands/unit/direct.md` carries its 9 worker-role word changes, and its kept contexts are gone.
- `roles_audit.py` counts singular and plural forms. The forms are explicit inflected forms in the term data, not a general English plural rule.
- `roles_audit.py check` exits non-zero and names the file and line of each retired name outside a kept context.
- `test_roles_audit.py` holds fixture tests and the source-only repository test `test_repository_sources_have_no_unkept_retired_names`. The fixtures cover singular and plural, overlap, protocol and history contexts, copied context, changed kept text and a returning retired use. `merge_gate --tests-only` discovers the repository test, so every merge gate runs it. The test reads repository sources only, not home-state records.
- `role_terms.toml` points `defined_in` at live defining files, such as `docs/as-built/guilds.md`. It keeps both `docs/delegate/` and `docs/direct/` as kept paths until no live file is under `docs/delegate/`.

**Files:**
- `scripts/production/role_terms.toml` — terms, retired forms, inflected forms, per-file kept contexts with reasons
- `scripts/production/roles_audit.py` — the measure and the `check` action
- `scripts/production/test_roles_audit.py` — fixtures and the repository check test
- `docs/plans/sdlc-roles-design.md` — Step log (frozen classification, before and after measures), answered Questions and term table rows
- Renamed prose and scripts: `commands/showrunner/{rename,retire,transfer}_unit.md`, `commands/unit/direct.md`, `docs/production_format.md`, `docs/direct/dual_review.md`, `docs/as-built/` (agent-registry, build-memory-admission, guilds, long-lived-workers, shutdown-and-restart, unit-rename, rustdoc-once-and-final-targets), `scripts/berth/install/README.md`, `scripts/agents/{codex_mesh,mesh_runs}.py`, `scripts/direct/{implement,board,review}.sh`, `scripts/direct/phase_reservation.py`, `scripts/production/{rename,retire,transfer}_unit.py`, `scripts/shutdown/{restart,inventory}.py`, `scripts/hooks/pre-tool-use-berth.py`; pinned label tests in `test_restart.py`, `test_rename_unit.py`, `test_mesh_runs.py`, `test_retire_unit.py`, `test_transfer_unit.py`

**Binds later work:**
- Each kept context in `scripts/production/role_terms.toml` applies once, in its named file only. Kept text that is changed or copied to another file is flagged.
- Names keeps independent counts for each term. Only `check` uses the longest matching phrase.
- The stored `seat` field and `codex_seats` stay as kept contracts. A change to them must add a kept context in the same change.
- The lexical after measure is in the design doc Step log: merged tree, n=596 live files; `seat` 15, `seats` 7, `Codex seat` 1, `Codex seats` 0; 22 kept, 0 eligible.
- Kept history and migration values: `docs/as-built/message-clarity.md`, `docs/as-built/usage-ledger.md`, `scripts/usage/collect.py`.

**Gotchas:**
- `check` took about 38 s wall (28 s CPU) on the full tree. The retired-name speed work makes it fast.
- If a later edit rewords a file that has a kept context, the repository test fails until that file's kept text in `role_terms.toml` is refreshed. Example: a reword of `build-memory-admission.md`.

**Ruled out:** extending the global kept-path masking (per-file kept contexts are used instead); a general English plural rule for every term (explicit inflected forms are used instead).

### Phase 4 — The retired-name check runs in a few seconds  · status: done

#### As-built

- `roles_audit.py check` selects candidate files before extraction. A file is a candidate when its raw text holds a retired form. A Python file is also a candidate when any string Constant in its AST holds a form after escape decoding and literal joins. Shell files select on raw text only.
- `read_live_file(root, retired, select_candidates, path)` returns `(files, issues)` for one path. `read_live_files` runs it in `ProcessPoolExecutor(max_workers=8)` with `chunksize=4` when retired forms are present and 64 or more paths are selected; otherwise it runs serially. `map` keeps sorted order, so output is byte-equal to full extraction.
- `check_sources(root, *, full_extraction=False)`: `full_extraction=True` extracts every file and runs serially. The parity test uses it.
- Findings, exit status, kept contexts, physical source lines and source-read errors are the same as before. Untracked sources, fixture roots and non-Git roots stay covered.
- On natedev at 1-minute load 67-69, `check` takes 1.1-1.3 s wall and the `test_roles_audit.py` suite takes 1.5-1.7 s wall (before: `check` 39.8 s at load 78).

**Files:**
- `scripts/production/roles_audit.py` — candidate selection and the parallel per-file scan
- `scripts/production/test_roles_audit.py` — fixtures for joined literals, escapes, read errors, a non-Git root, and parity with full extraction on a bounded repository sample
- `docs/plans/sdlc-roles-design.md` — Step log section "Retired-name check timing — 2026-10-28"

**Gotchas:** After candidate selection, most of the cost is parsing every Python file, because syntax diagnostics must still report. Only parallel reading removes it. Timings above load 80 do not count against the 5 s budget; record the load with each time.

**Ruled out:** a literal `git grep` prefilter, because it misses joined literals and escapes that Python extraction decodes; candidate selection alone, because it reached 8.4 s at load 38-44.

### Phase 5 — Each session records its role apart from its name  · status: todo
**Depends on:** Phase 4

#### Work Order

**Goal:** every session records its role apart from its name, so many sessions can hold one role and no session needs a role in its name.

**Spec:**
- Decided by the user (R1b, 2026-10-21; production doc Decisions): a session's name and its role are separate. Every session records its role apart from its name. roles-unit defines the role record; the unit board's owner and cargo-liner add the name and role columns (the showrunner asks them).
- Packaging: the showrunner's call, 2026-10-28 — this phase follows Step 1. Bring the user only a choice R1b leaves open.
- Decided by the user (R5b, 2026-10-28, via the showrunner): every shown name carries production · unit · job, for example `sdlc · roles · code reviewer 6`, in reports, messages and Codex thread names. The address stays as it is. The role record supplies the job part.
- Three meanings of role already exist and stay distinct: the organizational role (`RosterEntry.role`, apart from its name), the worker assignment in mesh records (`impl`, `test`, `fix`, `review`), and the guild responsibility in guild records. Define each, and how it maps to the R5b job. Keep current routing and permission meanings; do not replace every `role` with one vocabulary.
- Types: distinct semantic types for organizational role, worker assignment and shown job, with explicit states for unassigned, legacy-unrecorded and unreadable records; no interchangeable strings or bare optional fields. If `LiveUnit.session: MarkedUnit | None` joins the role-record boundary, give its absence an explicit state.
- Fixture tests: old records, missing production or unit, a freestanding session, several sessions holding one job, reassignment, rename, transfer, restart and account move. The role survives each; shown names and Codex thread titles follow R5b; addresses and thread identities stay valid.
- Files at phase start name every record writer and reader, the renderers of reports and messages, the code that sets Codex thread titles, and their tests. The unit board's owner and cargo-liner are named dependencies with their own deliverables; sending them the definition does not count as their change landing.
- The unit director writes the Work Order from the current session records at phase start (roster, `showrunners.py`, `units_table.py`), with the role values from the approved names (Q3: design reviewer, code reviewer, UI reviewer, designer; Q6: showrunner, unit director; Q7: worker, small-fix helper).

**Files:**
- `docs/plans/sdlc-roles-design.md` — role record definition
- `scripts/message/roster.py` — organizational role, worker assignment and shown job types; the shown name
- `scripts/agents/codex_mesh.py` — Codex thread titles carry the shown name; the roster record keeps its worker assignment
- `scripts/production/production_tree.py` — reads the role record, not a name suffix
- `scripts/production/broadcast.py` — selects by organizational role
- `scripts/message/send.py` — shown sender name
- `scripts/message/test_roster.py`, `scripts/agents/test_codex_mesh.py`, `scripts/production/test_production_tree.py`, `scripts/production/test_broadcast.py`, `scripts/message/test_send.py` — fixture tests

**Workers:** `1 writer + 1 tester`. `impl` holds the five scripts; `test` holds the five test files.

**Constraints from prior phases:** the approved names; Step 1's worker words. Stored fields `seat` and `codex_seats` stay as they are: a role field never renames them, and a retirement that leaves a live key, value or flag adds it to the kept contexts in `scripts/production/role_terms.toml` in the same change.

**Acceptance gate:** the gates in Delegation Context green; each role-holding record type has a role field apart from its name; shown names follow R5b; the record definition is in the design doc and sent to the showrunner for the column owners.

### Phase 6 — Measure the first step and propose the next  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** the design doc says whether Step 1 moved its measure, and the next step goes to the user.

**Spec:**
- The lexical after measure is done: the Step log's Phase 3 after measure (n=596 live files) reads 15 singular and 7 plural kept uses, 0 eligible. On the comparable merged set the reduction is 104 to 22 (n=82); this supersedes the earlier expected reduction of n=66. Do not mix the n=563 and n=596 snapshots.
- The windowed measure names the merge timestamp of f8f5b962 into sdlc and a complete observation interval of at least 3 days after it. Sent-contact counts are descriptive and have no Step 1 reduction target. Keep unknown roles unknown: a current role record gives no historical roles or request intent, so never infer a role from a shown name.
- Add the merge-gate cost of the retired-name check (Phase 4 budget) as measured in real gates over the window.
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
