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

### Phase 6 — Names that can change live in one config file  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** a name the user may change later (a shown job, a broadcast audience label) is one line in one config file, so a rename never spreads through scripts and docs again, as `seat` and `delegate` did.

**Spec:**
- Decided by the user (2026-10-28, via the showrunner): names go through config indirection. One config file holds each changeable name as a stable key and a display name. Scripts use the key; printed text reads the display name. A rename is a one-line config edit.
- The file: `config/names.toml` (unit director's call: one file for every changeable name, beside the other `config/` files; `config/guilds.conf` keeps the guild ownership settings).
- Guild heads are retired (the user, through the showrunner, 2026-10-28): no head names in the config, the loader or the check. Role labels stay.
- Shown jobs (phase 5's `Job` values: showrunner, unit director, worker, code reviewer, UI reviewer, designer, design reviewer): each gets a key and a display name; `roster.ShownName.render()` reads the display name. The key stays the value in records and code.
- The herdr session name keeps the key (it works as an address). The herdr tab label shows the display name, set at `add_unit` and at restart; there is no live retitle (agreed with the guilds showrunner).
- Tab title and identity are two things. Today `add_unit.launch_session` and `restart._herdr_launch` pass the session name as the tab title, and `herdr.Herdr.lookup(label)` (`scripts/herdr/herdr.py`) finds panes by label; restart also restores the tab layout and order from labels. Only the shown tab title takes the display name. Every lookup, restored layout, tab order and attach hint keeps using the key. Files at phase start name each callee that sets or reads a tab title or pane label. Tests: after a display rename in a fixture config, restart finds each pane, restores the old layout and tab order, and prints attach hints that work; a label assertion at `add_unit` and restart alone does not count.
- Inventory first: before dispatch, the unit director lists each printed literal that is a changeable display name, with its file. Phase 5's `Job` values equal their first display names, so a stored key and its display text look the same today. Organizational roles, worker assignments, legacy JSON values and old snapshots stay compatibility contracts and keep their keys. Fixtures prove that retiring a display name flags printed text and leaves stored keys, comparisons and old snapshots alone.
- The history format: each key holds one ordered list of display names, current name first, earlier names after it. A rename is one edit: put the new name first and keep the rest. Test two successive renames in a fixture: both earlier names reach the retired-name list, and the current one does not.
- Shown senders captured by phase 5 stay as captured. `send.py` resolves the shown sender on the sending machine, stores it in the queued message (`Shown sender:` line) and forwards it to remote delivery; replay keeps that text. Test in `test_send.py`: after a config rename, a new message carries the new display name, a message queued before it keeps its original text, and a receiver never puts in its own config or roster value.
- What the fixed-display check fails on (the showrunner, 2026-10-28, option 1): only labels — a printed literal whose whole text is a display name, or the job part of a shown name or a status column. Role words inside sentences (help, errors, report prose) stay plain text; the check prints one count line for them and lists them only with `--prose`. Stored keys (for example the account group key in `accounts.py`) are never hits. Prose is covered by the retired-name check: a changed display name joins the retired list by itself, so a rename is two steps — change the config, then fix the lines the retired-name check lists; the design doc says so.
- The stderr heredoc gap closes in this phase (next item amended, phase 5 review): `roles_audit.py`'s shell output extraction skips `cat >&2 <<EOF` bodies, so a script could print a fixed display name there and pass the check. Fixtures prove stderr `cat` heredocs count in Names and report retired or fixed display names at their physical source lines, while non-output heredocs stay excluded. Record the extraction change apart from the frozen Phase 3 lexical measure.
- Phase 5 follow-up, done first in this phase (phase 5 review found two gaps in what shipped):
  - The roster's human output shows why a job is missing. `ShownName.render()` drops `JobUnassigned.reason`, and the human table shows the legacy `slot_role` instead. Show the reason (assignment not recorded, or record not readable, with the record named), so the responsible director can fix the board or mesh record. Never derive a job from the name. Test in `test_roster.py`.
  - `roster.py --json` carries the job as its own field. Today it adds only the composite `shown_name`, and `slot_role` is neither the shown job nor a reliable assignment state. Add one structured field that holds the job key, its display name, or the explicit unassigned state with its reason; keep every existing field. The design doc's Role record section names this field for the unit board's owner and cargo-liner, with each one's landing evidence and acceptance test. Sending them the definition does not count as their change landing.
- The retired-name check (`roles_audit.py check`) fails when a script holds a display name from `config/names.toml` as fixed printed text outside that file. Docs prose is out of scope: a rename still updates docs through the normal retired-name flow.
- A rename stays one edit (the showrunner, 2026-10-28): each name in `config/names.toml` keeps a history of its earlier display names, and the loader adds each one to the retired-name list on its own. The fast retired-name check then finds every doc and script that still uses the old display name. Test: change a display name in a fixture config, and the check reports a fixture doc that still uses the old one.
- Types: a key type per name group and one loader that refuses an unknown key, a missing display name and a duplicate display name, each with its own explicit error; no bare string lookups with a default.
- Tests: rename one display name in a fixture config and every renderer (shown name, Codex thread title, broadcast line, tab label) shows the new name while addresses, session names and records keep the key; the check fails on a planted fixed display name in a fixture script and passes on the live tree.
- Files at phase start name every place a job or audience display text is printed or set as a tab label.

**Files:**
- `config/names.toml` — keys and ordered display-name lists
- `scripts/lib/names.py` — the one loader: key types per name group and its explicit errors
- `scripts/message/roster.py` — shown job reads the display name; human output shows a missing job's reason; `--json` job field
- `scripts/message/send.py` — keeps the captured shown sender (read only unless a test shows a gap)
- `scripts/production/roles_audit.py`, `scripts/production/role_terms.toml` — fixed display-name check; stderr heredoc extraction
- `scripts/production/add_unit.py`, `scripts/shutdown/restart.py`, `scripts/herdr/herdr.py` — tab title writers and label readers; the unit director adds each further callee found at phase start
- `docs/plans/sdlc-roles-design.md` — the name config section; the Role record job field
- `scripts/lib/test_names.py`, `scripts/message/test_roster.py`, `scripts/message/test_send.py`, `scripts/production/test_roles_audit.py`, `scripts/production/test_add_unit.py`, `scripts/shutdown/test_restart.py`, `scripts/herdr/test_herdr.py` — tests

**Workers:** `1 writer + 1 tester`. `impl` holds the loader, the config, the scripts and the design doc section; `test` holds the test files listed above. Use semantic key types and distinct config-error states, never optional values or fallback strings.

**Constraints from prior phases:** R5b shown names; the stored fields `seat` and `codex_seats` stay as they are. Phase 5's role record: `Job`, `OrganizationalRole` and `WorkerAssignment` stay distinct; `JobUnassigned` carries `AssignmentUnrecorded` or `AssignmentUnreadable`. `send.py` carries the shown sender, resolved once on the sending machine, as a `Shown sender:` line. The broadcast report line reads `<shown name> [<address>] - <outcome>[: <detail>]`, with the bracket only when the address differs; `zone.py` counts ` - sent\b` on it.

**Acceptance gate:** the gates in Delegation Context green, with `python3 -m unittest discover -s scripts/message -p 'test_*.py'` and the tests of each tab title writer; a one-line display-name edit in a fixture config changes every printed place and nothing else; two successive renames retire both earlier names; the check fails on a planted fixed display name, in a stderr heredoc too; after a rename, restart finds each pane by its key and keeps the tab order; `roster.py` on the live tree shows a missing job's reason, and `roster.py --json` carries the job field (smoke: `python3 ~/.claude/scripts/message/roster.py` and `--json` on natedev).

### Phase 7 — The retired-name baseline reads at a glance  · status: todo
**Depends on:** Phase 6

#### Work Order

**Goal:** a person reads `roles_audit.py baseline` in a few lines (user verdict on change review row C1, 2026-10-28: "pretty much something you can read but not me really").

**Spec:**
- The output starts with a short summary: the share of other names, the worst names with their counts, and each finding in plain words.
- The full tables print only behind a flag.
- Drop wrong-party rows that are all zero or "not recorded".
- Keep what the numbers mean: the short output keeps each file-set count (n), each message window, each warning for a source it could not read, and the difference between unknown coverage and zero findings. Hiding a row with no information never hides missing evidence.
- Rename the report-facing `WrongPartyCounts` to `SentContactCoverage`: phase 5 records no historical request intent, so the counts show sent contacts, not wrong-party asks.
- The change comes back to the user as a new change review row.
- Tests pin the summary lines on a fixture and that the flag prints the full tables; also an unreadable source (its warning shows), an unknown coverage (shown as unknown, not 0) and a window (named in the summary).

**Files:**
- `scripts/production/roles_audit.py`, `scripts/production/test_roles_audit.py`

**Workers:** `1 writer + 1 tester`. `impl` holds `scripts/production/roles_audit.py`; `test` holds `scripts/production/test_roles_audit.py`. Phase 6 also edits `roles_audit.py`, so this phase starts after Phase 6's checkpoint.

**Acceptance gate:** the gates in Delegation Context green; the baseline on the live tree prints the summary first, in plain words.

### Phase 8 — Measure the first step and propose the next  · status: todo
**Depends on:** Phase 5 (it needs only the Phase 3 merge window, so it may run beside Phases 6 and 7)

#### Work Order

**Goal:** the design doc says whether Step 1 moved its measure, and the next step goes to the user.

**Spec:**
- The lexical after measure is done: the Step log's Phase 3 after measure (n=596 live files) reads 15 singular and 7 plural kept uses, 0 eligible. On the comparable merged set the reduction is 104 to 22 (n=82); this supersedes the earlier expected reduction of n=66. Do not mix the n=563 and n=596 snapshots.
- The windowed measure names the merge timestamp of f8f5b962 into sdlc and a complete observation interval of at least 3 days after it. Sent-contact counts are descriptive and have no Step 1 reduction target. Keep unknown roles unknown: a current role record gives no historical roles or request intent, so never infer a role from a shown name.
- Add the merge-gate cost of the retired-name check (Phase 4 budget) as measured in real gates over the window.
- Do not run the lexical baseline again: the recorded 104-to-22 reduction (n=82) is the lexical result, and a new run on the current tree has no same file set, because Phases 5–7 change sources, config and output. The lexical half-size decision is closed by that result.
- For the windowed measure (sent contacts per recipient), the after window is at least 3 days from the merge. Wrong-party asks are not measurable until message records carry roles; do not report them. Add each number to the Step log with its n or window and its source.
- Update Steps with what the measure taught, and propose the next step; that proposal is the question.
- At phase end, the unit director sends the showrunner the result and the next step as one question for the user. The next approved step becomes a new phase after this one, with the Work Order of Phase 3 and the step's own Spec.

**Files:**
- `docs/plans/sdlc-roles-design.md` — Step log, Steps

**Workers:** `1 writer + 1 tester` — one doc. `impl` runs the windowed measure and writes the Step log.
- `impl` — `docs/plans/sdlc-roles-design.md`
- `test` — checks each number in the Step log against its recorded evidence (window, contact counts, gate cost); it edits nothing

**Constraints from prior phases:** the Step log before measure and the change Phase 3 shipped.

**Acceptance gate:** the Step log has before and after numbers with n and file set or window; the question is sent.
