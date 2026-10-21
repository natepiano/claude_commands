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

#### As-built

- `style_ledger.py record --reads <path> [<path> ...]` names the other files a verdict rests on. The ledger accepts it only with `--input-scope rule`, a verdict in `SCOPED_VERDICTS`, and a crate or module cell of an included Rust file. It normalizes each path like `--path`, then sorts the paths and removes duplicates. The cell's own path changes nothing. A path with no file at the commit is refused with `LedgerError` "<path>: no file at <commit>".
- Migration 18 adds `judgments.read_files` (JSON list, default `'null'`). A judgment has one of two shapes: `ReadFiles(paths)`, which refuses an empty tuple, or `NoReadFiles` (`NO_READ_FILES`). `Judgment` and `TicketJudgment` carry the shape in `reads`. `_read_fingerprint(tip, path, reads)` is the SHA-256 digest of the sorted `(path, blob)` pairs of the cell file plus the read files, read from `tip.blobs`. `record` stores this digest once. The checkpoint carry uses it too, and runs its `rev-list` over the cell file plus the read files.
- `_judgment_inputs_match` compares a judgment with reads by `_read_fingerprint` at the checked tip. A read file that is missing at the tip is a mismatch. Judgments without reads keep the earlier comparison. Current state, awaiting merge, `_repeated_judgments` and `judged_at_base` all go through this one function. The ticket text in `style_plan.py` tells a unit to use `--input-scope rule --reads <files>`, and says that without `--reads` the cell comes back after any change in its crate or module. On the cargo-liner ledger copy, `cells()` ran in 20.9 s against 27.5 s before the change, with the same states.
- `proposal_issue._issue_lock` takes its lock file from the fixed folder `~/.local/state/proposal-issue/locks` (`Path.home()`) and creates the folder on use. It does not read `XDG_RUNTIME_DIR` or `TMPDIR`. In `_create_numbered`, a `FileExistsError` from `os.link` reads the path again through `_decision_path_state`. The same key returns `False` and leaves the other run's file as it is. A different key raises the earlier `Refusal`. The temp file is removed in every case.
- `held waive --id F` requires exactly one of `--ticket T` or `--person NAME` (an argparse group of exclusive options). `waive_held(finding_id, actor)` takes a `WaiveActor` = `WaiveByUnit(ticket)` | `WaiveByPerson(by)`. It refuses a blank name and a ticket that did not judge the finding. Migration 19 adds `held_findings.waive_provenance` (`WaiveProvenance`: `unrecorded`, `unit`, `person`; default `unrecorded`). Migration 20 adds `waived_by`. `HeldFinding` carries `waive_provenance` and `waive_actor` (`WaiveActor | NoRecordedWaiveActor`). A unit's waive does not change a finding that is already waived. A person's waive writes over a unit's waive.
- `held replace --id F --after TEXT [--person NAME]` (`Ledger.replace_held`) reopens a waived finding with the new after text and keeps its id and judging ticket. It prints the id. It sets the finding to unfiled and clears its proposal, build ticket and person reading. It also sets a newest `waived` cell judgment back to `held` (`_restore_held_findings`, which `unwaive_held` also uses). It refuses a finding that is not waived, and a person's waive. An unrecorded waive needs `--person`, and the ledger records that name in `unwaived_by`, as `unwaive` does. Plain `held add` keeps an existing waive.

**Files:**
- `scripts/style/style_ledger.py` — `ReadFiles`/`NoReadFiles`, `_read_files`, `_read_fingerprint`, migrations 18-20, `record --reads`, the reads branch of `_judgment_inputs_match` and of the checkpoint carry, `WaiveProvenance`/`WaiveActor`, `waive_held(actor)`, `replace_held`, `_restore_held_findings`, the `held replace` and `held waive` options.
- `scripts/style/style_plan.py` — the ticket text's `--input-scope rule --reads <files>` line.
- `scripts/production/proposal_issue.py` — the fixed lock folder and the `FileExistsError` path at the decision link.
- `docs/style_production.md` — the `--reads` paragraph, `held waive` with `--ticket` or `--person`, and the `held replace` paragraph.
- `docs/as-built/style-showrunner.md` — the `held waive` and `held replace` command lines.
- `commands/unit/checkpoint.md` — the person's `held waive` command names `--person <name>`.
- `scripts/style/test_style_ledger.py` — tests for `--reads` (state, awaiting merge, base, checkpoint, refusals, normalization, migration from schema 17) and for waive actors and `held replace`.
- `scripts/style/test_style_plan.py` — the refill test with and without `--reads`, and the ticket text test.
- `scripts/production/test_proposal_issue.py` — the fixed lock folder test and the same-key and different-key link tests.
- `scripts/production/test_footer_counts.py` — patches `Path.home` in place of `XDG_RUNTIME_DIR`.
- `scripts/style/test_style_proposals.py` — patches `Path.home` and `HOME`, and passes `WaiveByPerson` to `waive_held`.
- `scripts/style/test_coverage_report.py` — passes `WaiveByPerson` to `waive_held`.

**Binds later work:** `judgments.read_files` is `'null'` or a sorted JSON list of normalized paths. A judgment with reads compares only by `_read_fingerprint`, and every state reader goes through `_judgment_inputs_match`. `waive_held` needs a `WaiveActor`. A new kind of waive actor extends `WaiveActor` and `WaiveProvenance`, and `_held` must map it back from `waive_provenance` and `waived_by`. `replace_held` refuses every waive except a unit's waive and an unrecorded waive that has `--person`. Tests that file proposals patch `Path.home`, because the lock folder no longer reads the environment.

**Gotchas:**
- `decline_findings` writes `waive_provenance='person'` with an empty `waived_by`. It does this for a person's decline and also for the file-gone waive from `style_plan.py`, and it erases any earlier actor name. `_held` reads such a row as `NoRecordedWaiveActor`, and `replace` refuses it as a person's waive.
- A `--reads` judgment keeps declared scope `rule`. The newest declared scope filter still treats it as a rule-scope verdict.

### Phase 41 — every waive names who made it, declines and file-gone waives included  · status: done

**Depends on:** Phase 40

#### As-built

- `WaiveProvenance` has the value `LEDGER = "ledger"`. The new frozen dataclass `WaiveByLedger` has no fields. `WaiveActor` is now `WaiveByUnit | WaiveByPerson | WaiveByLedger`. `_held` reads a `ledger` row back as `WaiveByLedger()`.
- `_waive_actor_record(actor)` maps each actor to its stored `(provenance, waived_by)` pair. A `WaiveByLedger` stores `waived_by='ledger'`. It refuses an empty ticket or an empty person name. `held waive` and `decline_findings` both use it.
- `DeclineActor = WaiveByPerson | WaiveByLedger`. `decline_findings(finding_ids, words, actor)` refuses any other actor with `LedgerError("decline requires a person or the ledger")` before a write. It stores the actor's provenance and name, and no longer writes an empty name. A decline over a unit's waive replaces that actor with the decline actor.
- `decline` and `apply_answer` in `style_proposals.py` store `WaiveByPerson("user")`. The waive in `style_plan.py` for a finding whose file is gone, or whose site moved to another file, stores `WaiveByLedger()`.
- `held replace` refuses a ledger waive (`cannot replace a ledger waive`) and a person's waive. It replaces only a unit's waive, and an unrecorded waive still needs `--person`. There is no new CLI flag and no migration. Old rows do not change.

**Files:**
- `scripts/style/style_ledger.py` — `WaiveProvenance.LEDGER`, `WaiveByLedger`, `DeclineActor`, `_waive_actor_record`, the actor argument of `decline_findings`, and the refusal of a ledger waive in `held replace`.
- `scripts/style/style_proposals.py` — `decline` and `apply_answer` pass `WaiveByPerson("user")`.
- `scripts/style/style_plan.py` — the gone-file and moved-site waive passes `WaiveByLedger()`.
- `scripts/style/test_style_ledger.py` — tests for the decline actors, the refusal of a unit actor with no write, and the refusal of a ledger waive in `held replace`.
- `scripts/style/test_style_proposals.py` — tests that a decline and a later finding under the stored answer store the actor `user`.
- `scripts/style/test_style_plan.py` — tests that a gone-file waive stores the actor `ledger`.
- `docs/style_production.md` — the decline, refill waive and `held replace` lines name the actor.

**Binds later work:** Every waive row has a provenance and a non-empty `waived_by`, except old `unrecorded` rows. A new waive path picks a `WaiveActor` variant and passes it through `_waive_actor_record`. `decline_findings` takes only a `DeclineActor`. Only a unit's waive can be replaced.
