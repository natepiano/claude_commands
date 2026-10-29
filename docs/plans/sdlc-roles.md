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

### Phase 5 — Each session records its role apart from its name  · status: done
**Depends on:** Phase 4

#### As-built

- `scripts/message/roster.py` keeps three role meanings apart: `Role` (the organizational role, the `RosterEntry.role` value), `WorkerAssignment` (its `WorkerRole`: `impl`, `test`, `fix`, `review`) and `Job` (the shown job). The guild responsibility in guild records is separate. A session with no job holds `JobUnassigned`, as `AssignmentUnrecorded` (old record) or `AssignmentUnreadable`; no role is a bare string or optional. The Role record section of `docs/plans/sdlc-roles-design.md` defines each meaning and its map to the job.
- `ShownName` (built by `shown_name()`) renders R5b, `production · unit · job`, for example `sdlc · roles · code reviewer 6`; `--json` adds `shown_name`. Codex thread titles in `codex_mesh.py` carry it.
- `production_tree.py` reads the role record, not a name suffix. `broadcast.py` selects by organizational role and prints `<shown name> [<address>] - <outcome>[: <detail>]`, with the bracket only when the address differs.
- `send.py` resolves the shown sender once, on the sending machine, and carries it as a `Shown sender:` line (about 1 s per uds or named-machine send).
- Addresses and the stored fields `seat` and `codex_seats` do not change.

**Files:** the five scripts above; `docs/as-built/session-roster.md`; fixture tests in `test_roster.py`, `test_codex_mesh.py`, `test_production_tree.py`, `test_broadcast.py`, `test_send.py`, `test_codex_winddown.py` (old records, freestanding session, one job on many sessions, reassignment, rename, transfer, restart, account move).

**Binds later work:** the names config phase reads `Job` display names from config for `ShownName.render`, the Codex thread title, the broadcast line and the tab label. It also owns two gaps: human roster output hides why a job is missing, and `--json` has no structured job field for the column owners.

**Gotchas:**
- Each `Job` value equals its first display name, so key and display text look the same. Code uses the key.
- herdr finds panes by label (`herdr.Herdr.lookup`), so a tab label is an identity.
- The broadcast line is a contract with its readers: `zone.py` counts ` - sent\b`, and the Codex winddown tests parse it. Change them together.

**Ruled out:** a second lexical baseline run — the file set is not the same after the role and names changes.

### Phase 6 — Names that can change live in one config file  · status: done

#### As-built

- `config/names.toml` has `[jobs]` and `[audiences]` tables. Each stable key holds an ordered list of display names: the current name first, retired names after it. A rename is one edit: put the new name first and keep the rest.
- `scripts/lib/names.py` has key types `JobKey` and `AudienceKey`, and `load_names(path) -> Names | NamesError`. `NamesError` is `UnknownKey | MissingKey | MissingDisplayName | DuplicateDisplayName | UnreadableNames | InvalidNames`. `Names` gives `job(key)`, `audience(key)`, `current()` and `retired()`. `error_text(error)` names the file, table and key; `report_error` prints it once per process; `runtime_names()` loads once per process and caches the result.
- Roster shown names, broadcast lines, Codex thread titles, `add_unit` and restart show display names. Record keys, addresses, session names and pane labels keep the key. A shown sender that `send.py` captured keeps its text on replay and remote delivery.
- `roster.py` human output shows a missing job's reason. `roster.py --json` adds a `job` field: `{"state": "assigned", "key", "display", "qualifier"}` or `{"state": "unassigned", "reason"}`; all earlier fields stay.
- herdr: `session_title(name, production, unit)` renders the tab title, and `Herdr.retitle(pane, title, *, label)` sets the title apart from the pane label. The Stop hook `scripts/hooks/herdr_tab_title.py` keeps both.
- `roles_audit.py check` fails on a printed display label: a whole literal, the job part of a shown name, each branch of a printed conditional, and `cat >&2 <<EOF` heredoc bodies at their physical source lines. It counts role words in sentences in one line and lists them only with `--prose`. Each retired name joins the retired-name check by itself.

**Files:**
- `config/names.toml` — keys and ordered display-name lists
- `scripts/lib/names.py` — the one loader, key types, error states
- `scripts/message/roster.py` — display-name render, missing-job reason, `--json` `job` field
- `scripts/herdr/herdr.py`, `scripts/hooks/herdr_tab_title.py` — tab title apart from pane label
- `scripts/production/add_unit.py`, `scripts/shutdown/restart.py` — tab title writers
- `scripts/production/roles_audit.py` — fixed-display label check, sentence count, heredoc and `conditional_fragments` extraction
- `docs/plans/sdlc-roles-design.md` — "Name config" section and the Role record `job` field

**Binds later work:** a names-config error prints `names.error_text` once per process, and renderers fall back to the address. `runtime_names()` caches once per process; read shown jobs (the code reviewer too) through it. The tab title (shown display title) is not the pane label (session and record identity); restart and layout restore find panes by the label key. `roles_audit.py` keeps label failures apart from the sentence count (211 sentence uses on the live tree).

**Gotchas:** a config edit does not retitle a live tab; launch, restart or a session rename sets the next title. Fixtures pass a config path or patch `runtime_names`; they never read the live config. A pane-restore stub must take the `label` keyword.

**Ruled out:** live retitle of open tabs after a config edit — the old title stays until restart or session rename; guild head names in the config — guild heads are retired and `config/guilds.conf` keeps guild ownership.

### Phase 7 — Each unit director's reviewers can move to it  · status: done
**Depends on:** Phase 6. It needs no `assign.sh` (showrunner, 2026-10-29: start now; the launcher switch and the pool removal wait for `assign.sh` on main).

#### Work Order

**Goal:** everything that moves code reviewers from the review pool to their unit director is built and tested, while the pool still runs every review.

**Spec:**
- Decided by the user (2026-10-28, through the showrunner): "unit directors own their own reviews just as they have their own implementers and testers and fixers and they should restart sessions rather than start new ones so context builds up"; the pool "feels unnecessarily complex"; the roles unit director owns the retirement.
- The transfer: one explicit, recoverable step that moves each unit's leased pool code reviewer (`~/.local/state/review-pool/<slug>/pool.json`) to that unit's `review` worker on the same thread. Its result for each reviewer is one of three typed states, migrated, deferred (the reviewer is busy; a rerun moves it) and conflict (the unit already has a different `review` worker), with no nullable fields (showrunner, 2026-10-29). A rerun after a move changes nothing. A unit with no leased reviewer gets no entry. Build it with a dry-run mode that prints each result and writes nothing; the live run happens when launchers switch, so this phase never runs it live.
- Reviewers are ordinary unit workers in the unit worker ledger, role `review`: addresses `<worktree>-review` and `<worktree>-review2`, job key `code_reviewer`, qualifiers `JobSlot("review")` and `JobSlot("review2")`. Reuse `workers.resolve` and `open_unit_worker` (it makes `review` read-only); do not write a second allocator. Take addresses from the worker-seats address contract.
- Standing instructions: `config/review_pool/code.md` moves to `config/review/code.md`; every reader of the old path (`review_pool.py` and its tests, `docs/as-built/review-pool.md`) reads the new one, so the pool keeps working.
- `role_terms.toml` gains `review pool` as a retired term. The retired-name check stays green: until the pool is removed, each current use is a kept context or listed exception, which the pool removal deletes.
- `role_terms.toml`: every `defined_in` and plan path names a file that exists; fix each entry that points at a deleted plan (the showrunner names `sdlc-guilds.md`).
- Docs: `docs/as-built/long-lived-workers.md` gains the reviewer rows (role `review`, `review2`, read-only, persist, pause, move and end as writers do) and the transfer command; `docs/as-built/review-pool.md` names the new standing-instructions path.
- Review turn records (showrunner add-on, 2026-10-29): `review.sh` names each turn record `<started>-<pid>.json`, so two attempts of one launcher in the same second collide (`FileExistsError`, then a failed tree check; seen on main d18712b3). The record write uses exclusive create with a unique name, and a test writes two records in the same second.

**Files:**
- `scripts/direct/workers.py`, `scripts/direct/test_workers.py` — reviewer roles and the transfer
- `config/review/code.md` — moved from `config/review_pool/code.md`
- `scripts/direct/review_pool.py`, `scripts/direct/test_review_pool.py` — read the new standing-instructions path only
- `scripts/production/role_terms.toml`, `scripts/production/test_roles_audit.py` — `review pool` retired; stale `defined_in` paths fixed
- `docs/as-built/long-lived-workers.md`, `docs/as-built/review-pool.md`
- `scripts/direct/review.sh`, `scripts/direct/test_review_launcher.py` — unique review turn record names

**Workers:** `1 writer + 1 tester`. `impl` holds `workers.py`, `review_pool.py`, `role_terms.toml`, `config/review/code.md` and the docs; `test` holds `test_workers.py`, `test_review_pool.py` and `test_roles_audit.py`. `workers.py` is a build-followups file: name it on the checkpoint notice's also-touches line, trial-merged against its owner's tip.

**Constraints from prior phases:** phase 5 role record: `Job` keeps `code reviewer`; a reviewer's shown name is `<production> · <unit> · code reviewer[ N]`. Phase 6 names config: the reviewer's shown job reads `config/names.toml`. The retired-name check is a merge gate (`roles_audit.py check`, exit 0 on the live tree).

**Acceptance gate:** the gates in Delegation Context green, with the direct and production suites. The transfer's dry run on this machine prints one typed result per leased pool reviewer and writes nothing (compare `pool.json` and the worker ledger before and after). `roles_audit.py --root . check` exits 0 with `review pool` retired.

### Phase 8 — The retired-name baseline reads at a glance  · status: done

#### As-built

- `roles_audit.py baseline` prints a short summary by default: the other-name share, the top other names with counts, definition and unit-name findings, the file set (n), the message window, and sent-contact and director-contact coverage. `--tables` adds the full tables.
- The summary shows fixed-display label failures (a person must fix them) apart from the count of role words in sentences (information only). `check` keeps its exit codes and `--prose` keeps its listing.
- `SentContactCoverage` and `measure_sent_contacts` replace `WrongPartyCounts` and `measure_wrong_party`. The counts show sent contacts, not wrong-party asks. Unknown coverage prints as unknown, never 0. The full table drops rows that are all zero or all unknown.
- `report(..., *, tables, source_result, unread_production_docs)` takes the source result as a required argument, so a missing source check cannot print as zero labels.
- Typed `UnreadSource(path, reason)` and `LiveSources(files, unread_sources)`: `baseline()` reads the tree once and passes it to `check_live_sources`. Each unreadable file prints as a warning with its path and reason.
- A names-config error fails the baseline with `names.error_text` (file, table and key). It never reads as zero findings.

**Files:**
- `scripts/production/roles_audit.py` — baseline summary, `--tables` flag, single tree read, unread-source warnings
- `scripts/production/test_roles_audit.py` — summary lines, `--tables`, dropped rows, unread-source warning, unknown coverage, window, sentence-only fixture, required source result, single-read regression

**Binds later work:** contact measures use `measure_sent_contacts` and `SentContactCoverage`. The baseline prints the summary by default and the tables only with `--tables`. Unknown coverage stays unknown, never 0. Unread sources show as warnings.

**Gotchas:** `check` and `check_sources` still call `source_check(root)`, which reads the tree itself. Only `baseline` shares one read.

**Ruled out:** a second tree read in `baseline` (about 3.6 s of a 25.6 s run, and its result can disagree with the first read).

### Phase 9 — Measure the first step and propose the next  · status: todo
**Depends on:** Phase 5 (it needs only the Phase 3 merge window, so it may run beside Phases 6, 7 and 8)

#### Work Order

**Goal:** the design doc says whether Step 1 moved its measure, and the next step goes to the user.

**Spec:**
- The lexical after measure is done: the Step log's Phase 3 after measure (n=596 live files) reads 15 singular and 7 plural kept uses, 0 eligible. On the comparable merged set the reduction is 104 to 22 (n=82); this supersedes the earlier expected reduction of n=66. Do not mix the n=563 and n=596 snapshots.
- The windowed measure names the merge timestamp of f8f5b962 into sdlc and a complete observation interval of at least 3 days after it. Sent-contact counts are descriptive and have no Step 1 reduction target. Keep unknown roles unknown: a current role record gives no historical roles or request intent, so never infer a role from a shown name.
- Add the merge-gate cost of the retired-name check (Phase 4 budget) as measured in real gates over the window.
- Do not run the lexical baseline again: the recorded 104-to-22 reduction (n=82) is the lexical result, and a new run on the current tree has no same file set, because Phases 5–8 change sources, config and output. The lexical half-size decision is closed by that result.
- Measure contacts alone, without the combined `baseline` command (it reads lexical sources first): call `parse_messages` over one fixed `AuditWindow` and `measure_sent_contacts` in `roles_audit.py`, which returns a `SentContactCoverage`. Keep unknown coverage and historical intent as unknown. Report gate timings for each implementation checkpoint, because Phase 6 changed extraction and classification after the Phase 4 timings.
- For the windowed measure (sent contacts per recipient), the after window is at least 3 days from the merge. Wrong-party asks are not measurable until message records carry roles; do not report them. Add each number to the Step log with its n or window and its source.
- Update Steps with what the measure taught, and propose the next step; that proposal is the question.
- At phase end, the unit director sends the showrunner the result and the next step as one question for the user. The next approved step becomes a new phase after this one, with its own self-contained Work Order: the step's files, owners, compatibility constraints, measures and gates. Phase 3 is now an As-built and holds no Work Order to copy.

**Files:**
- `docs/plans/sdlc-roles-design.md` — Step log, Steps

**Workers:** `1 writer + 1 tester` — one doc. `impl` runs the windowed measure and writes the Step log.
- `impl` — `docs/plans/sdlc-roles-design.md`
- `test` — checks each number in the Step log against its recorded evidence (window, contact counts, gate cost); it edits nothing

**Constraints from prior phases:** the Step log before measure and the change Phase 3 shipped. Phase 8: `baseline` prints a short summary by default and the full tables only with `--tables`; a sent-contact count it cannot attribute prints as unknown, never zero, and an unreadable source prints as a named warning.

**Acceptance gate:** the Step log has before and after numbers with n and file set or window; the question is sent.

### Phase 10 — Each unit director owns its reviewers  · status: todo
**Depends on:** Phase 7 (the transfer, the moved standing instructions and the retired term), and `assign.sh` from models-unit's worker-seats plan on main; the showrunner says when. Start no code before that message.

#### Work Order

**Goal:** a unit director owns its code reviewers as it owns its writers, testers and fixers. It resumes the same reviewers across phases and plans, so their context builds up. The review pool is gone.

**Spec:**
- Move each pool reviewer to its unit with one explicit, recoverable transfer whose states are migrated, deferred and conflict, with no nullable fields (showrunner, 2026-10-29). A busy reviewer stays reachable until its move ends. The pool readers go, and unused pool servers stop, only after every move ends. Delete the transfer code in this same phase once every pool reviewer is migrated or ended, so no migration layer stays.
- Remove `scripts/direct/review_pool.py`, `scripts/direct/test_review_pool.py`, their callers and the review-pool as-built (moved here from worker-seats Phase 15 by models' plan change d8873d42).
- Decided by the user (2026-10-28, through the showrunner): "unit directors own their own reviews just as they have their own implementers and testers and fixers and they should restart sessions rather than start new ones so context builds up"; the pool "feels unnecessarily complex"; the roles unit director owns the retirement.
- Reviewers are ordinary unit workers in the unit worker ledger (worker-seats Phase 4), role `review`: addresses `<worktree>-review` and `<worktree>-review2`. `assign.sh --role review` resumes `review`; when `review` is busy (the two lenses of one review run at once), `workers.py resolve` gives `review2`. They are read-only and persist, pause, move and end exactly as writers do (`docs/as-built/long-lived-workers.md`). Never a fresh reviewer while its thread exists.
- Context carries over (unit director's call): a one-time step makes each unit's leased pool code reviewer (`~/.local/state/review-pool/<slug>/pool.json`) its `review` worker on the same thread. A unit with no leased reviewer opens `review` at its next review. A reviewer still reviewing is not moved; a rerun moves it. A rerun after the move changes nothing.
- Standing instructions live in `config/review/code.md` (moved by the transfer phase). Every review prompt carries one line that names that file; the reviewer reads it at its first review and again when the file changes (`<ReviewPromptContract/>` keeps its standing-instructions line).
- Remove: `scripts/direct/review_pool.py`, `test_review_pool.py`, `review_pool_report.py` and its test; the pool paths in `review.sh` (`--pool` and `--lease` are accepted and ignored for one release: one stderr line, then the unit's own `review` worker takes the review); the pool code in `rename.py`, `rename_unit.py`, `retire_unit.py`, `transfer_unit.py`, `restart.py`; the pool reviewer rows and `worker:review-pool/...` addresses in `roster.py` and `send.py` (phase 5); the `**Review pool:**` lines in the production docs; `docs/production_format.md` rule 16 (later rules renumbered, with every reference); the pool text in `commands/unit/direct.md`, `docs/direct/dual_review.md`, `commands/showrunner/produce.md`. `scripts/feature_log/inventory.py --pool` is a different thing and stays.
- UI design checks (unit director's call): no `ui_adopted` marker exists, so the UI pool only ran in shadow. Its shadow trial ends; the fresh Claude helper, which already governs, stays the one design check. `config/review_pool/ui.md` is removed.
- `progress_history.py` keeps reading old review records that carry `arm: pool`; new records write `own`.
- Pool state on disk: after the one-time step, `~/.local/state/review-pool/` stays as an archive for one release and nothing reads it; the step prints its path. The production reviewer app-servers end when no reviewer uses them.
- Remove the `review pool` kept contexts and exceptions from `role_terms.toml` as the pool text goes.
- Run the transfer live (first a dry run), then delete the transfer code once every pool reviewer is migrated or ended.
- Docs match: `docs/as-built/review-pool.md` and `review-pool-probes.md` are removed; `long-lived-workers.md` gains the reviewer rows; every other as-built that names the pool (Files at phase start list each) says what is true now.

**Files:**
- `scripts/production/production_tree.py`, `scripts/production/test_production_tree.py`, `scripts/agents/mesh_runs.py`, `scripts/agents/test_mesh_runs.py`, `scripts/production/test_rename.py`, `scripts/production/test_rename_unit.py`
- `scripts/direct/review.sh` — pool paths removed; `--pool`/`--lease` accepted and ignored
- `scripts/direct/assign.sh`, `scripts/direct/workers.py` — the `review`/`review2` resolve and the one-time reviewer step (models-unit dropped worker-seats Phase 8, its Phase 12 import-pool call and its Phase 15 `review_pool.py` removal, 2026-10-29: all reviewer work is in this phase)
- `scripts/direct/review_pool.py`, `scripts/direct/test_review_pool.py`, `scripts/direct/review_pool_report.py`, `scripts/direct/test_review_pool_report.py` — removed
- `scripts/production/rename.py`, `scripts/production/rename_unit.py`, `scripts/production/retire_unit.py`, `scripts/production/transfer_unit.py`, `scripts/shutdown/restart.py` — pool code removed
- `scripts/message/roster.py`, `scripts/message/send.py` — pool reviewer rows and addresses removed
- `scripts/production/role_terms.toml` — `review pool` kept contexts removed
- `config/review_pool/ui.md` — removed
- `docs/production_format.md`, `commands/unit/direct.md`, `docs/direct/dual_review.md`, `commands/showrunner/produce.md`, `docs/plans/sdlc-production.md`, `docs/plans/build-followups-production.md` — pool text and Review pool lines
- `docs/as-built/review-pool.md`, `docs/as-built/review-pool-probes.md` — removed; `docs/as-built/long-lived-workers.md` and each other as-built that names the pool
- `scripts/direct/test_review_launcher.py`, `scripts/direct/test_workers.py`, `scripts/production/test_retire_unit.py`, `scripts/production/test_transfer_unit.py`, `scripts/production/test_stall_watch.py`, `scripts/production/test_add_unit.py`, `scripts/message/test_roster.py`, `scripts/message/test_send.py` — pool cases removed, reviewer cases added

**Workers:** `1 writer + 1 tester`. `impl` holds the scripts, config and docs above; `test` holds the test files. Files owned by build-followups units (`review.sh`, `assign.sh`, `workers.py`, `codex_mesh.py`, `docs/production_format.md`, the commands) go on the checkpoint notice's also-touches lines, trial-merged against each owner's tip.

**Constraints from prior phases:** phase 5 role record: `Job` keeps `code reviewer`; a reviewer's shown name is `<production> · <unit> · code reviewer[ N]`. Phase 6 names config: the reviewer's shown job reads `config/names.toml`. Worker-seats Phase 7: `assign.sh` options, exit codes, the address and shown name kept apart. Reuse `workers.resolve` (it reuses idle workers, adopts prior-run workers, reopens threads and numbers new ones) and `open_unit_worker` (it makes `review` read-only); do not write a second allocator. The job key is `code_reviewer`; ordinary reviewer qualifiers are `JobSlot("review")` and `JobSlot("review2")`, not the pool's numbers. Take reviewer addresses from the worker-seats address contract; do not invent a second form. Two pool readers still read the archive: `production_tree.py` builds reviewer edges from `pool.json` (line 231) and `mesh_runs.py` `_roster_folders` scans pool meshes; the pool part of the design doc's Role record section changes with them.

**Acceptance gate:** the gates in Delegation Context green, with the direct, production and message suites; a fixture-backed check finds no operational pool reader or caller, with named exceptions for the migration decoder, compatibility flags and kept history (the retired-name check stays the separate prose gate). Live check: this unit's next review runs on its own `review` and `review2` workers, and the review after it resumes the same threads (same thread ids in the roster).
