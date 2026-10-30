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

#### As-built

- Code reviewers are ordinary unit workers in the unit worker ledger, role `review`: addresses `<worktree>-review` and `<worktree>-review2`, job key `code_reviewer`, qualifiers `JobSlot("review")` and `JobSlot("review2")`. They are read-only. They persist, pause, move and end as writers do. Allocation reuses `workers.resolve` and `open_unit_worker`.
- `python3 scripts/direct/workers.py transfer-reviewers --production <slug> --session-dir <run>... [--apply]` moves each unit's leased pool code reviewer (`~/.local/state/review-pool/<slug>/pool.json`) to that unit's `review` worker on the same thread. Dry run is the default and writes nothing. It prints one line per reviewer: `ReviewerMigrated`, `ReviewerDeferred` (reviewer busy; a rerun moves it) or `ReviewerConflict(reviewer, unit, cause: ReviewerConflictCause)`, with a closed cause enum and no nullable fields. A unit with no leased reviewer gets no line. A rerun after a move changes nothing.
- `--apply` writes a journal under `worker-transfers/<generation>/`, fences the source and registers under the destination ledger lock, and merges the inbox once with a durable `reviewer_inbox_sources` receipt. An interrupted apply resumes from the journal.
- `codex_mesh.open_unit_worker` gives a new review thread its configured shown name (`code_reviewer` display + slot) at open.
- `review.sh` names each turn record `<started>-<pid>-<sequence>.json` and writes it with exclusive create.

**Files:**
- `scripts/direct/workers.py`, `scripts/direct/test_workers.py` — reviewer roles and the transfer
- `scripts/agents/codex_mesh.py` — reviewer shown name at open
- `config/review/code.md` — code reviewer standing instructions
- `scripts/direct/review_pool.py`, `scripts/direct/test_review_pool.py` — standing-instructions paths
- `scripts/direct/review.sh`, `scripts/direct/test_review_launcher.py` — turn record names
- `scripts/production/role_terms.toml`, `scripts/production/test_roles_audit.py` — `review pool` retired
- `docs/as-built/long-lived-workers.md` — reviewer rows and the transfer command; `docs/as-built/review-pool.md` — new standing-instructions path

**Binds later work:** The reviewer cutover runs the transfer live (dry run first) and then deletes the transfer code, its journal and its receipt. The dry run checks busy turns and detached work as apply does. A destination inbox that holds valid JSON of the wrong shape is still replaced; the cutover adds a refusal that names the inbox before the live run. `review_pool.standing_prompt` keeps a per-type default table: code reads `config/review/code.md`, ui still reads `config/review_pool/ui.md`, and `REVIEW_POOL_CONFIG` overrides both. `role_terms.toml` has `retired = ["review pool"]` with 39 kept contexts; the pool removal deletes them. Turn record readers glob `*.json`.

**Gotchas:** The CLI takes each unit from `_ledger_for_run` (plan-header fallback); live runs have no `state["unit"]`. Reviewer addresses come from `worker_name.sh` (git root, sanitize, truncate). An apply interrupted after the inbox move but before registration merges into an existing replacement inbox; it never skips it.

**Ruled out:** a second allocator for reviewers; a basename rule for reviewer addresses.

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
- Add the merge-gate cost of the retired-name check (Phase 4 budget) as measured in real gates over the window. Take it only from recorded gate logs tied to a checkpoint and a time in the window; the gate's rolling module-duration history names no revision or time, so it is not evidence. Report the audit suite's duration apart from the check's own run time. Where no recorded log exists, say the cost is unavailable; never substitute a fresh timing.
- Do not run the lexical baseline again: the recorded 104-to-22 reduction (n=82) is the lexical result, and a new run on the current tree has no same file set, because Phases 5–8 change sources, config and output. The lexical half-size decision is closed by that result.
- Measure contacts alone, without the combined `baseline` command (it reads lexical sources first): call `parse_messages` over one fixed `AuditWindow` and `measure_sent_contacts` in `roles_audit.py`, which returns a `SentContactCoverage`. Keep unknown coverage and historical intent as unknown. Report gate timings for each implementation checkpoint, because Phase 6 changed extraction and classification after the Phase 4 timings.
- For the windowed measure, report what `measure_sent_contacts` returns: `SentContactCoverage.senders`, each sender's sent-contact count and recipient categories, with unknown attribution kept unknown; do not build a new per-recipient grouping. The after window is at least 3 days from the merge. Wrong-party asks are not measurable until message records carry roles; do not report them. Add each number to the Step log with its n or window and its source.
- Update Steps with what the measure taught, and propose the next step; that proposal is the question.
- At phase end, the unit director sends the showrunner the result and the next step as one question for the user. The next approved step becomes a new phase after this one, with its own self-contained Work Order: the step's files, owners, compatibility constraints, measures and gates. Phase 3 is now an As-built and holds no Work Order to copy.

**Files:**
- `docs/plans/sdlc-roles-design.md` — Step log, Steps

**Workers:** `1 writer + 1 tester` — one doc. `impl` runs the windowed measure and writes the Step log.
- `impl` — `docs/plans/sdlc-roles-design.md`
- `test` — checks each number in the Step log against its recorded evidence (the exact window, the message records read, the per-sender counts, the gate logs); it edits nothing

**Constraints from prior phases:** the Step log before measure and the change Phase 3 shipped. Phase 11 deleted the review pool and Phase 12 the guilds name: before choosing the next step, close or re-source the later-work candidates in `sdlc-roles-design.md` that still name the deleted `review_pool.py` or the deleted pool and guild documents (the pool lease definition and its source lines); keep dated measurements as history, and do not rerun the lexical baseline for this. Phase 8: `baseline` prints a short summary by default and the full tables only with `--tables`; a sent-contact count it cannot attribute prints as unknown, never zero, and an unreadable source prints as a named warning.

**Acceptance gate:** the Step log has before and after numbers with n and file set or window; the question is sent.

### Phase 10 — Each unit director owns its reviewers  · status: done

#### As-built

- `review.sh` with no worker flag and no pool flag keeps the fresh read-only reviewer. Lens slots: adversary→`test`, contract→`impl`, craft/ux→`review`.
- `--pool code` and `--lease` print `review.sh: --pool code and --lease are ignored; using the unit reviewer.` on stderr. Then `workers.py resolve --role review` gives the unit's own reviewer: `<worktree>-review`, or `<worktree>-review2` when `review` is busy. Reviewers are read-only unit-ledger workers (`docs/as-built/long-lived-workers.md`).
- The first resolve adopts the unit's leased pool code reviewer on the same thread and sets the unit ledger field `reviewers_adopted`. The flag survives `rename_unit` and `move_unit`. A busy leased reviewer counts as a busy `review`.
- `--pool ui` refuses with `ERROR: --pool ui is retired; use the fresh Claude helper in <UXReview/> for the design check.` and exit 2. A resolver refusal writes status `error`, prints `ERROR: no unit reviewer: <cause>` and exits 1. New review records write `arm: own`; old `arm: pool` records still count.
- `progress_history` reports review evidence as `NoRecordedCodeReview | RecordedCodeFindings`, so no record is apart from a recorded zero.
- `workers.py transfer-reviewers --production <slug> --session-dir <run>... [--apply]` moves leased code reviewers to unit workers. A wrong-shape destination inbox makes `_read_inbox` refuse and name the inbox; both files stay byte-identical. The pool code, the review-pool as-builts and `config/review_pool/ui.md` stay; production docs and `docs/production_format.md` carry no pool lines (old rule 16 gone, rules renumbered).

**Files:**
- `scripts/direct/review.sh` — fresh reviewer by default; ignored-flag and refused-flag paths
- `scripts/direct/workers.py` — `review`/`review2` resolve, adoption at first use, `transfer-reviewers`
- `scripts/agents/mesh_runs.py` — `caller_from_environment` skips adopted and ended entries
- `scripts/agents/codex_mesh.py` — reviewer follow title, `detach_reviewer_transport`
- `scripts/direct/progress_history.py` — named review evidence states

**Binds later work:** "The review pool and the transfer code are gone" keeps the no-flag fresh reviewer, the ignored-flag stderr line, the `--pool ui` refusal and the `ERROR: no unit reviewer: <cause>` path with status `error` and exit 1. It keeps the named evidence states. It runs `transfer-reviewers` live, then deletes it. Preview lines start `preview: would migrate: …`; applied lines read `migrated:`, `deferred:` or `conflict:` with owner and thread checked. Skipped UI and unleased reviewers print `skipped: …`, then one `Archive after each completed move: …` line. `--apply` exits 1 on any deferred or conflict result. With no `pool.json` it prints `No reviewer pool for <production>; nothing to move.` and exits 0. A refusal, such as the wrong-shape inbox, prints `workers: <cause>` and exits 2. `_finish_reviewer_transfer_locked` calls `codex_mesh.detach_reviewer_transport`. The `reviewers_adopted` reader goes with the transfer code; adoption at first use stays while `--pool code` is accepted.

**Gotchas:** `mesh_runs.caller_from_environment` skips entries with `status` `ended` or `adopted` or with `adopted_to`, and returns at once for a unit-ledger entry; else it matches an adopted roster entry. `codex_mesh` renames a reviewer thread (`thread/name/set`) only after the follow is accepted; a refused follow must not leave a renamed thread. `interrupt_review` does not kill the worker child.

**Ruled out:** a UI review pool — the fresh Claude helper stays the one design check; a second reviewer allocator — `workers.resolve` and `open_unit_worker` are enough.

### Phase 10b — A unit with several old pool reviewers adopts one  · status: done

#### As-built

- `workers.transfer_reviewers` picks one reviewer per unit. A unit's own reviewer in its destination slot, adopted from the pool or opened by the unit, wins over every lease. With none, the lease with the greatest `Reviewer.last_allocated` is adopted; the reviewer name breaks ties. A saved `worker-transfers/` journal keeps its choice after a crash.
- Other idle code leases to that unit become `review_pool.Unleased` and return `ReviewerReleased(reviewer, unit)`, printed `released: <unit>: <reviewer>: returned to pool` (`preview: would release` in a dry run, which writes nothing). Busy ones return `ReviewerDeferred` and are released on a later call. `reviewers_adopted` is set once no lease to the unit remains, also when the unit had no ledger file.
- `transfer-reviewers --apply` exits non-zero only on a deferred or conflict result. `review.sh` with no worker flag no longer refuses a unit with old leases; it runs on the unit's reviewer.

**Files:**
- `scripts/direct/workers.py` — reviewer selection and lease release in `_transfer_reviewers`
- `scripts/direct/test_workers.py`, `scripts/direct/test_review_launcher_pool.py` — 2-, 3- and 6-lease cases, a unit with its own reviewer and six leases, dry run, crash recovery, busy lease

**Gotchas:** `UnitLease.ordinal` counts reviews, not recency.

### Phase 10c — Small fixes from the phase 10 vet  · status: done

#### As-built

- `roles_audit.py check` prints `role words in sentences: N (list with --prose)` without `--prose` and `role words in sentences: N` with it, followed by the list.
- `roster.py` keeps a worker's production and unit after its director stops: `UnitWithoutRunningDirector(unit)` (a `DirectorRelationship`) comes from the run's recorded identity (`resolve_production_unit`). A worker with a shown job prints without an extra `worker ` prefix, with or without a running director. A stopped director is still no live messaging link.
- `docs/as-built/herdr-adoption.md`: `open_tab`'s `title` names the tab and `label` names the pane (else `title`); launchers and the Stop hook give a short tab title and the full session name as the pane label.

**Files:**
- `scripts/production/roles_audit.py`, `scripts/message/roster.py` and their tests

### Phase 10d — The tab labels are back as they were  · status: done

**Why:** Phase 6 changed herdr tab titles to `production · unit · job` (e.g. `sdlc · roles · unit director`). The user never approved a tab-name change. The first tab is the showrunner and every other tab is a unit director, so a tab needs no role word and no production (user, 2026-10-29).

**Spec:**
- Each herdr tab title is the session name again, exactly as at `~/.claude` main before 5adb23e8 (`5adb23e8^1`): the launchers (`add_unit.py`, `restart.py`) and the Stop hook (`herdr_tab_title.py`) give the tab the session name. `herdr.session_title` and its production/job join are gone.
- A test pins the tab title of a showrunner, a unit director and a plain session to its session name.
- `docs/as-built/herdr-adoption.md` says what is true after the revert.

### Phase 10e — A run older than its state file gets its unit reviewer  · status: done

#### As-built

- `workers.py resolve --working-dir <path>` names the unit reviewer from the progress state's `working_dir` when it has one, else from the launcher's directory. `review.sh` passes its `WORKING_DIR`. With neither, the typed `invalid worker state: missing working_dir` stays, and no thread opens.
- A run with no `progress_history_state.json` and no other record (efficiency-unit's pre-reboot run) gets its reviewer, and a rerun gets the same reviewer on the same thread. The one computed name flows through adoption and transfer; ledger choice and the transfer `cwd` still read the progress state only.

### Phase 11 — The review pool and the transfer code are gone  · status: done

#### As-built

- The review pool and the reviewer transfer code do not exist. `review_pool.py`, `review_pool_report.py`, `config/review_pool/ui.md`, `docs/as-built/review-pool.md` and `review-pool-probes.md` are deleted. `workers.py` has no `transfer-reviewers`, no pool adoption (`_adopt_unit_reviewers`, `reviewers_adopted`), no `IncomingReviewer` and no reviewer journal or inbox receipts.
- `workers.py` keeps the generic worker adoption and the inbox checks (`_read_inbox`, `_giving_inbox`, `_transfer_pending`, `_merge_pending`) that transfers between unit runs use.
- `codex_mesh.py`, `mesh_runs.py` (`_roster_folders`), `worker_owners.py` (no `PooledReviewerOwner`), `roster.py`, `production_tree.py`, `rename.py`, `rename_unit.py`, `retire_unit.py` and `transfer_unit.py` do not read pool state. `production_tree.py` draws the unit's own review edge.
- `review.sh` accepts `--pool`/`--lease` and ignores them: `--pool code`/`--lease` print the ignored line and use the unit reviewer through ordinary `workers.py resolve`; `--pool ui` refuses with the design-check pointer and exit 2; no flag keeps the fresh read-only reviewer.
- `rename.py` has apply steps 1-7 and saves no `review_pool` field. It never reads or moves the pool archive.
- `role_terms.toml` keeps `retired = ["review pool"]` with `defined_in` set to `docs/as-built/long-lived-workers.md`; the pool kept contexts are gone.

**Files:**
- `scripts/direct/workers.py` — generic worker roster and adoption, no pool
- `scripts/production/rename.py` — apply steps 1-7 and the legacy pool step decoder
- `scripts/direct/test_retired_review_pool.py` — AST guard against pool readers and transfer callers

**Binds later work:**
- No script reads the archived pool state or pool meshes; `scripts/direct/test_retired_review_pool.py` refuses any new reader or transfer caller. The archive stays on disk under `~/.local/state/review-pool/`, unread.
- `rename.py` reads an old progress file's pool step as `LegacyApplyStep.REVIEW_POOL_RETIRED` (`legacy_review_pool_retired`); malformed or duplicate progress raises `RenameRefused("invalid rename progress")`; unknown named steps are kept.
- Ordinary reviewers use the slots `review` and `review2`.

**Gotchas:** Outside the tabs, ordinary reviewers show as `code reviewer` and `code reviewer 2` (from the slots), not the numbered pool form. The shown-name form is an open question with the showrunner and the user; any change needs the user's go.

### Phase 12 — The guilds name is gone  · status: done

#### As-built

- The held Astra designer and reviewer per design are "held design roles": `scripts/astra/design_roles.py` and `scripts/astra/astra_work.py` (moved with history from `scripts/guilds/`, which is gone), tests `scripts/astra/test_design_roles.py` and `test_astra_work.py`.
- Names: env `DESIGN_ROLES_STATE`, `DESIGN_OWNERSHIP_STATE`, `DESIGN_ROLES_HOST`, `HELD_DESIGN_ID`, `DESIGN_BINDING_GENERATION`, `DESIGN_ROLES_CONFIG`; state `~/.local/state/astra/design-roles` and `~/.local/state/astra/design-ownership` (apart from `astra_roles.py`'s `~/.local/state/astra/roles`); roster key `design_context`, run file `design_context.json`; `config/agents.conf` `[assignments] design_roles=codex` and `[design_roles.codex]`; `config/design_roles.conf`, `config/design_roles/designer.md`. No old-name reader exists.
- `merge_gate.py` `TEST_SUITES` has no `guilds` suite (`astra` runs the moved tests); `pyrightconfig.json` has a `scripts/astra` root.
- `role_terms.toml` term `held design role` (kind role, defined in `docs/as-built/long-lived-workers.md#Why roles outlive runs`) retires `guild`, `guilds`, `guild head`, `guild heads`. `git grep -il guild -- commands docs/as-built scripts config skills CLAUDE.md pyrightconfig.json ':!scripts/**/test_*.py'` prints only `role_terms.toml`.
- The five guilds production plans are deleted; `sdlc-production.md` "Held design roles" is 3 lines that link `long-lived-workers.md`. `~/rust/nate_style/guilds/` is deleted.

**Files:**
- `scripts/astra/design_roles.py` — held designer and reviewer per design
- `scripts/astra/astra_work.py` — saved Astra assignments
- `scripts/production/role_terms.toml` — the retired guild names

**Gotchas:** `~/.claude` main's `merge_gate.py` refuses a tree without `scripts/guilds` until this phase is on main; gate such a tree with its own `merge_gate.py`. `~/.local/state/guilds/` is removed only after this phase is on main.

### Phase 13 — Our scripts are safe to run again after a stop  · status: done
**Depends on:** Phase 11

#### Work Order

**Goal:** each of the 17 live rerun-sweep rows owned by this unit is fixed: after a stop or failure at the named place, running the same command again finishes the work or reports a clear cause, and a test proves it.

**Spec:**
- Source: `docs/plans/sdlc-rerun-sweep.md`, sections `roles-unit` (14 rows) and `guild-process-unit` (6 rows); showrunner, 2026-10-29. Each row's "On a rerun" text is the failure to remove. Line numbers moved: find each place again by its code. Path map after Phase 12: `scripts/guilds/roles.py` is `scripts/astra/design_roles.py`, `scripts/guilds/astra_work.py` is `scripts/astra/astra_work.py`.
- Closed rows: the three `scripts/direct/review_pool.py` rows (144, 358, 439). Phase 11 deleted that file. Record them as closed with that reason; no code.
- Live rows, with the place as found after Phase 11:
  - `codex_mesh.py` 1443 — `_write_roster_record` truncates the roster before it writes the new JSON.
  - `codex_mesh.py` 2006 — `_write_pending` truncates the pending inbox before it writes the JSON.
  - `codex_mesh.py` 8169 — `command_prime` (~8141) saves the held-role creation claim before it records a thread.
  - `mesh_runs.py` 258 — `reap_orphans` makes the exclusive `.reap` claim (~254) with no owner, expiry or clear.
  - `send.py` 912 — `claim` records the emergency attempt before delivery; `still_live` does not check delivery.
  - `send.py` 989 — `pending` deletes queue files before their text is printed.
  - `send.py` 1389 — the daily template-fix stamp is written before the delivery child starts.
  - `progress_history.py` 1480 — `_start_phase` saves state (~1481) before it appends `phase_started` (~1493).
  - `progress_history.py` 5634 — `record_turn_wait` records the wait (~5672) before it writes its marker (~5673).
  - `workers.py` 553 — generic `_write_roster` (~493) truncates before it writes; adoption recovery then cannot parse the roster.
  - `workers.py` 1604 — `end_unit` skips mesh end for an `ended` entry (~1201) and removes the ledger while server cleanup is unfinished.
  - `astra_roles.py` 276 — exec-role liveness checks only the saved PID.
  - `astra_work.py` 167 — exclusive brief creation comes before the brief text is written.
  - `design_roles.py` 744 — the opening claim is saved before the thread launches.
  - `design_roles.py` 918 — the new binding generation is saved before `transition.json` is cleared and context updated.
  - `design_roles.py` 1000 — `RoleEnded` is saved before transition cleanup and census release.
  - `design_roles.py` 1427 — the new holder and generation are saved before transition cleanup and route updates.
- Each fix has one test that stops at the named place (inject the stop or the failure there), runs the same command again, and checks that the work finished or that a clear cause was reported. No existing assertion is removed or weakened.
- One storage and lock rule for the roster: `codex_mesh.py`, `workers.py` and `design_roles.py` all lock and rewrite the roster file. An atomic replace while a writer holds a lock on the old inode can lose updates, so all three use one shared write-and-lock rule (write a temp file, replace it under a lock that does not live on the replaced file). Tests cover a stop mid-write and two writers at once, and keep other thread identities, accepted messages and generic adoption receipts.
- Thread creation keeps "creation not started" apart from "creation outcome unknown" (`design_roles.RoleNotOpened`, the `codex_mesh.command_prime` claim). An unknown outcome gives the holder a clear recovery result and never starts a second thread. Keep `test_unknown_thread_creation_retains_the_claim_instead_of_launching_again` and `test_lost_creation_acknowledgement_never_launches_a_second_thread`; add stops before launch and after creation succeeds.
- Name the absence states the fixes touch: the `design_roles` rebind `Holder | None` / `Binding | None` ("keep the current value") and `mesh_runs.deliver`'s `int | None` (exit status vs lost supervisor) become semantic types, converted at the CLI or subprocess boundary. The `astra_roles.ExecProcess` liveness fix names the original process lifetime (pid plus process start time), not a PID that can be reused.
- Every mark a fix adds has an owner check and an expiry, or a command that clears it (rerun lens question 5).
- One place for each mechanism (user standard, 2026-10-30): the atomic write, the lock, and the process-lifetime check are one helper each, called by every fix that needs it; the stop-then-rerun tests share one stop-injection helper, not a copy per test file. Before adding a helper, look for one that already exists in `scripts/lib` or the touched module and use it.

**Files:**
- `scripts/agents/codex_mesh.py` — roster and inbox writes, held-role creation claim
- `scripts/agents/mesh_runs.py` — reap claim owner and expiry, delivery outcome type
- `scripts/message/send.py` — emergency claim, pending queue, template-fix stamp
- `scripts/direct/progress_history.py` — phase start and turn wait ordering
- `scripts/direct/workers.py` — roster write, unit retirement cleanup
- `scripts/astra/astra_roles.py` — exec-role process lifetime
- `scripts/astra/astra_work.py` — brief creation
- `scripts/astra/design_roles.py` — open, rebind, end and holder move
- `scripts/agents/test_codex_mesh.py`, `scripts/agents/test_mesh_runs.py`, `scripts/message/test_send.py`, `scripts/direct/test_progress_history.py`, `scripts/direct/test_workers.py`, `scripts/astra/test_astra_roles.py`, `scripts/astra/test_astra_work.py`, `scripts/astra/test_design_roles.py` — stop-then-rerun tests

**Workers:** `1 writer + 1 tester`.
- `impl` — `codex_mesh.py`, `mesh_runs.py`, `send.py`, `workers.py`, `progress_history.py`, `astra_roles.py`, `astra_work.py`, `design_roles.py`; owns the shared storage and recovery rules
- `test` — the matching test files: one stop-then-rerun test per live row, the concurrent-writer tests, and the kept adoption, uncertain-creation, archive and legacy-journal assertions

**State:** roster, inbox, claim, stamp and role-state files under the run and state folders; formats stay readable by the old code where a file already exists.

**Constraints from prior phases:** Phase 11: no reader of the archived pool state or pool meshes (`test_retired_review_pool.py` refuses one); generic inbox validation, ordinary reviewer thread identity and read-only behavior stay; `rename.py` reads the old pool step as `legacy_review_pool_retired`, refuses malformed or duplicate progress, and keeps unknown named steps (`test_apply_rerun_keeps_unit_moves_and_other_journal_keys`). Phase 12 path map above. main 7e85a74b renamed `scripts/shutdown/record.py` to `scripts/shutdown/shutdown_record.py` with no alias: any new import uses `shutdown_record` (the branch gets it from origin/sdlc).

**Acceptance gate:** each live row has its stop-then-rerun test passing; the `astra`, `agents`, `message`, `direct` and `production` suites pass; basedpyright on the changed files and their caller modules reports 0 errors, 0 warnings, 0 notes; then the full merge gate.
