# Style showrunner follow-ups

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Follow-up phases for the style showrunner after its as-built (`docs/as-built/style-showrunner.md`).

> **Production: build-followups** — unit `style-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-21 02:05 EDT

From the build-followups showrunner (natedev), on items from the cargo-liner-style showrunner: "start it now as a follow-up phase. Each repeat spends a Codex ticket for no work, and tokens are short. Key each rule-scope cell on the digest of the files the rule reads, not on the tip. Add a test: a style/main move that does not touch the rule's files offers the cell no second time. Send a checkpoint when the gate is green." Then: "add two items to the same follow-up phase … (a) A footprint check for the rewrite `_name: T` -> `_: T` when the body never reads `_name`. (b) A filer lock, or a temp name per run, so two concurrent `style_proposals.py file` runs do not clash on `.decision-*` (Errno 17)."

Item (a) shipped in phase 38 (merged into build-followups as 4c9ae45a); this plan carries the refill key and item (b).

Then (02:3x EDT), item (e): `held add` keys on rule, path and text, so after a waive a corrected add returns the waived id and stores nothing; add `held replace --id <waived id> --after <text>`, which reopens the waived row with the new text and keeps the id. Later: "Each waive records who made it: a unit by its ticket, a person with --person. Replace accepts only a unit's waive. An old waive with no record needs --person, the same as unwaive."

## Delegation Context

- **Project:** `~/.claude` (claude_commands), worktree `/home/natepiano/worktrees/claude-build-followups-style`, branch `build-followups-style`. Every Work Order runs in that worktree.
- **Project started:** 2026-10-16T19:43:43.983+00:00
- **Stack:** Python ≥3.12 through `~/.claude/scripts/lib/py`; basedpyright 1.39; stdlib `sqlite3`; markdown docs.
- **Layout:** `scripts/style/` — the style ledger, refill and ticket text, tests beside them; `scripts/production/` — `proposal_issue.py` and its tests; `docs/style_production.md` — the style production procedure workers read.
- **Key files:**
  - `scripts/style/style_ledger.py` — `DeclaredInputScope` :46, `CellInputScope` :60, `_comparison_scope` :67, `_cell_scope` :71, `Judgment` :1158, `_fingerprint` :1287, `MIGRATIONS` (last is 17, :1435), `_judgment_inputs_match` :1520, `Ledger.record` :4072 (input scope checks :4085-4107), `_judgments` :4154, `_judgment_state` :4162, `_repeated_judgments` :4205, `record` CLI parser :4516.
  - `scripts/style/style_plan.py` — `_refill_cells` :1072; the ticket text's input-scope lines :1432-1435.
  - `scripts/production/proposal_issue.py` — `_issue_lock` :156 (lock folder from `XDG_RUNTIME_DIR` or `tempfile.gettempdir()`), `_folder_lock` :167, `_decision_path_state` :513, `create` :523, `_create_numbered` :543 (`os.link(temporary, path)` :568).
  - `docs/style_production.md` — fingerprints and input scope :216-226.
  - Live ledger (read only, copy first): `~/.local/state/style/cargo-liner/ledger.sqlite`, repo `~/rust/cargo-liner`.
- **Test lanes:** `scripts/style/` and `scripts/production/` — `test_*.py` beside each module.
- **Build:** none (Python and markdown).
- **Test:** from the worktree root, `env -C /home/natepiano/worktrees/claude-build-followups-style PYTHONPATH=/home/natepiano/worktrees/claude-build-followups-style/scripts/style:/home/natepiano/worktrees/claude-build-followups-style/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest <modules>`; then the merge gate `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the root.
- **Lint:** `basedpyright -p /home/natepiano/worktrees/claude-build-followups-style <files>`; pass is `0 errors, 0 warnings`.
- **Invariants:**
  - basedpyright reports zero errors and zero warnings; no file-level ignores; no `Any`; line-level ignores only as a last resort (user, CLAUDE.md).
  - Every module has `from __future__ import annotations`; discarded results are written `_ = …`; test classes use `@final`/`@override` (repo convention).
  - Never write the live ledger; read it only from a copy (style-unit rule).
  - The forbidden-words list (`~/rust/nate_style/rust/forbidden-words.md`) applies to code, comments, docs and commits (user).

## Phases

<!-- PHASE NUMBERING — binds every command that edits this doc.
     N is a bare integer, numbered from 1, contiguous, in execution order.
     Never a letter suffix. Phases 1-39 are in docs/as-built/style-showrunner.md;
     this doc continues the same numbers. -->

### Phase 40 — a rule-scope cell comes back only when a file its verdict rests on changes, and two filer runs share one lock  · status: done

**Depends on:** none

#### Work Order

**Goal:** Refill offers a rule-scope cell again only when its own file or a file its verdict names changes, not after every change in its crate; two `style_proposals.py file` runs never fail with Errno 17 on the same decision; and a unit can correct the text of a held finding it waived.

**Spec:**

Part 1 — the refill key (style ledger).

- Today a `clean`, `exception` or `held` verdict recorded with `--input-scope rule` on a crate cell takes the crate scope fingerprint: `_fingerprint(..., CellInputScope.CRATE, ...)` is the Git tree hash of the crate directory. Any change anywhere in the crate turns the cell stale, and refill offers it again. Measured on the cargo-liner ledger (2026-10-21): 10,963 judgments are `input_scope crate, declared_scope rule`; `local:docs/cargo-port/style/vim-mode-reserves-hjkl.md` at `crates/cargo-port/src/tui/keymap_ui/controller.rs` and `mod.rs` was judged `clean` 21 times from 2026-10-19 to 2026-10-21 while the keymap files did not change.
- `record` takes a new option `--reads <path> [<path> ...]`: the other files the verdict rests on. It is allowed only with `--input-scope rule`, a verdict in `SCOPED_VERDICTS`, and a cell where `--input-scope file` is allowed (a crate or module cell of an included Rust file, the `file_scope_allowed` condition in `Ledger.record`). Each path is normalized like `--path` and must exist at `--commit`; refuse otherwise with a `LedgerError` that names the path or the reason. Duplicate paths and the cell's own path are allowed and change nothing.
- A judgment recorded with `--reads` stores its read files (a new column; add migration 18) and its fingerprint is the digest of the sorted `(path, blob)` pairs of the cell's own path plus the read files at the commit. Name this a new `CellInputScope` value or a separate field — follow the Type Design Contract: no empty list that means "no reads"; a judgment without reads and one with reads are two shapes.
- `_judgment_inputs_match` compares a judgment with reads by the same digest at the checked tip; a read file missing at the tip is a mismatch. Judgments without reads keep today's comparison byte for byte. Every caller (`_judgment_state` current and awaiting merge, `_repeated_judgments`, `judged_at_base`) gets the new key through that one function.
- The newest declared scope filter (`row.declared_scope != rows[0].declared_scope`) stays as it is. A judgment with reads counts as declared scope `rule`.
- Ticket text (`style_plan.py` :1432-1435): replace the `--input-scope rule` line with: "Use `--input-scope rule --reads <files>` when the verdict rests on other files; name each file you read for it. Without `--reads`, the cell comes back after any change in its crate or module." Keep the other two lines.
- `docs/style_production.md` :216-226: add that `--reads` keys the cell on its file plus the named files, and that a verdict without it keys on the whole crate or module.
- Speed: a `cells()` read must not load a full tree for each judgment. Read blobs for the read files at the tip from the tip already loaded; compute the stored digest once at record time. Name any cost you add.

Part 2 — the filer lock (proposal issues).

- `_issue_lock` takes its lock folder from `XDG_RUNTIME_DIR` or `tempfile.gettempdir()`. Two runs whose environments differ (a sandboxed run with another `TMPDIR`, or a run without `XDG_RUNTIME_DIR`) take different lock files, so `_folder_lock` does not exclude them. Both then pass the check inside the lock, and the second `os.link(temporary, path)` fails with `FileExistsError` (Errno 17), which names the `.decision-*` temp file.
- Put the lock files in one fixed folder that does not read the environment: `Path.home() / ".local/state/proposal-issue/locks"`, created on use. Lock file names stay `proposal-issue-<digest>.lock` from the resolved path.
- In `_create_numbered`, a `FileExistsError` from `os.link` reads the path again through `_decision_path_state`: the same key returns `False` (already filed, nothing written, the other run's file byte-identical); a different key raises the existing `Refusal(f"{path}: different decision key")`. The temp file is removed in every case, as now.
- Keep both `_decision_path_state` checks in `create`, before and inside the lock (phase 36 binds this).

Part 3 — held replace (style ledger).

- `held replace --id <waived id> --after <text>` reopens a waived held finding with the new after text and keeps its id. Plain `held add` is unchanged.
- Each waive records who made it (migration 19, `WaiveProvenance`): a unit's waive by its `--ticket`, a person's by `--person <name>`; `held waive` requires exactly one. A decline records the person. Waives from before the migration read as unrecorded.
- Replace accepts a unit's waive. A person's waive is refused. An unrecorded waive needs `--person <name>`, the same rule `unwaive` uses.

**Files:**
- `scripts/style/style_ledger.py` — `--reads` on `record`, migration 18, the read-files judgment shape, its fingerprint and `_judgment_inputs_match`; Part 3's `held replace`, waive provenance and migration 19.
- `scripts/style/style_plan.py` — the ticket text's input-scope line.
- `docs/style_production.md` — the `--reads` paragraph and the `held waive` / `held replace` lines.
- `scripts/style/test_style_ledger.py` — tests for Parts 1 and 3.
- `scripts/style/test_style_plan.py` — the refill test and the ticket text.
- `scripts/production/proposal_issue.py` — the fixed lock folder and the `FileExistsError` path in `_create_numbered`.
- `scripts/production/test_proposal_issue.py` — tests for Part 2.

**Workers:**
2 writers + 1 tester; the parts touch disjoint files.
- `impl2` — Part 1: `scripts/style/style_ledger.py`, `scripts/style/style_plan.py`, `docs/style_production.md`. hub: `style_ledger.py` (migration).
- `impl` — Part 2: `scripts/production/proposal_issue.py`, `scripts/production/test_proposal_issue.py`.
- `test` — Part 1 tests from the Spec: `scripts/style/test_style_ledger.py`, `scripts/style/test_style_plan.py`.

**Constraints from prior phases:**
- Phase 25: `judged_at_base` and `_repeated_judgments` decide which stale cells a ticket at the unit tip cannot change; they read inputs only through `_judgment_inputs_match`.
- Phase 36: `create` checks the decision path before and inside `_folder_lock`; a change keeps both checks.
- Phase 39: `record` refuses `held` with no live finding on the cell; `--reads` does not change that check.

**Acceptance gate:**
- Refill test (`test_style_plan.py`): a crate cell recorded `clean --input-scope rule --reads <b.rs>` at commit 1; commit 2 changes another file in the same crate and the synced tip moves to it; refill offers the cell no second time. Commit 3 changes `b.rs`; refill offers it again. The same cell recorded without `--reads` is offered again at commit 2 (today's behavior).
- Ledger tests (`test_style_ledger.py`): the cell reads `current` after an unrelated crate change and `stale` after a change to its own file or a read file, or when a read file is deleted; `awaiting merge` and `judged_at_base` follow the same key; `--reads` is refused with `--input-scope file`, without `--input-scope`, with `fixed` or `waived`, on a file-scope or manifest cell, and with a path missing at the commit; a ledger at schema 17 migrates and its old crate judgments keep their state.
- Ticket text test: the new `--reads` line is in the ticket text.
- Held replace tests (`test_style_ledger.py`): a unit's waive is replaced and keeps its id; a person's waive is refused; an unrecorded waive is refused without `--person` and replaced with it; `held waive` refuses neither and both actor flags.
- Filer tests (`test_proposal_issue.py`): the lock folder is the same with `XDG_RUNTIME_DIR` unset and `TMPDIR` changed; a `FileExistsError` at the link with the same key returns `False` and leaves the other file byte-identical; with a different key it raises `Refusal`; the temp file is gone in both cases.
- Live check, read only: on a copy of `~/.local/state/style/cargo-liner/ledger.sqlite`, `cells()` at the copy's synced tip runs in no more than 10% more time than before the change (time both).
- `env -C /home/natepiano/worktrees/claude-build-followups-style PYTHONPATH=/home/natepiano/worktrees/claude-build-followups-style/scripts/style:/home/natepiano/worktrees/claude-build-followups-style/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest scripts.style.test_style_ledger scripts.style.test_style_plan scripts.production.test_proposal_issue`
- `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the worktree root.
- `basedpyright -p /home/natepiano/worktrees/claude-build-followups-style` on every changed Python file: `0 errors, 0 warnings`.

### Phase 41 — every waive names who made it, declines and file-gone waives included  · status: todo

**Depends on:** Phase 40

Decided by the showrunner (natedev, 2026-10-21): "A decline is always the user's verdict, so it stores the actor `user`. A file-gone waive stores the actor `ledger`. Replace refuses both, as it refuses a person's waive." No new flag on `decline`.

#### Work Order

**Goal:** No waive is stored without its actor: a decline names the person who declined, and a waive the ledger makes itself says so.

**Spec:**

- Today `decline_findings` (`scripts/style/style_ledger.py` :2365, the update near :2418) sets `waive_provenance='person', waived_by=''`, and erases an earlier actor name. Its callers are `style_proposals.py` `apply_answer` :436 and `decline` :641 (a person's answer) and `style_plan.py` :1666 (findings whose file or site is gone at the synced tip; no person).
- `decline_findings` takes the actor as a `WaiveActor` value (phase 40's sum type). A decline (`apply_answer`, `decline`) records the actor `user` as a person's waive; the file-gone waive in `style_plan.py` records the actor `ledger` (a new variant, not a person). No new CLI flag.
- `held replace` refuses a `ledger` waive as it refuses a person's.
- `held replace` keeps its phase 40 rule: only a unit's waive is replaced; an unrecorded one needs `--person`.

**Files:**
- `scripts/style/style_ledger.py` — `decline_findings`, the actor type, `held replace`'s refusal of the new actor.
- `scripts/style/style_proposals.py` — the decline actor through `apply_answer` and `decline`.
- `scripts/style/style_plan.py` — the file-gone waive's actor.
- `scripts/style/test_style_ledger.py`, `scripts/style/test_style_proposals.py`, `scripts/style/test_style_plan.py` — tests.
- `docs/style_production.md` — the decline and file-gone waive lines.

**Workers:**
1 writer + 1 tester; the tests sit in their own files.
- `impl` — `scripts/style/style_ledger.py`, `scripts/style/style_proposals.py`, `scripts/style/style_plan.py`, `docs/style_production.md`. hub: `style_ledger.py`.
- `test` — `scripts/style/test_style_ledger.py`, `scripts/style/test_style_proposals.py`, `scripts/style/test_style_plan.py`.

**Constraints from prior phases:**
- Phase 40: `WaiveByUnit(ticket)` / `WaiveByPerson(name)` and `WaiveProvenance` (migration 19, `waived_by` migration 20); `held waive` requires exactly one of `--ticket` and `--person`; `held replace` refuses a person's waive and needs `--person` for an unrecorded one.

**Acceptance gate:**
- A decline stores the actor `user` and never an empty name; a file-gone waive stores the actor `ledger`; `held replace` refuses both.
- `env -C /home/natepiano/worktrees/claude-build-followups-style PYTHONPATH=/home/natepiano/worktrees/claude-build-followups-style/scripts/style:/home/natepiano/worktrees/claude-build-followups-style/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest scripts.style.test_style_ledger scripts.style.test_style_proposals scripts.style.test_style_plan`
- `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the worktree root.
- `basedpyright -p /home/natepiano/worktrees/claude-build-followups-style` on every changed Python file: `0 errors, 0 warnings`.
