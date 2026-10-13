# review-pool follow-ups

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Four follow-ups to the review pool: the trial report counts a merge hold only for its own plan and names the machine whose design checks it counted, the showrunner gets a unit's review history location from one command, and a pooled design check stops re-reading the design rules on every turn.

> **As-built disposition: amend** — `docs/as-built/review-pool.md`, `docs/as-built/showrunner-automation.md`

> **Production: build-followups** — unit `review-pool-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The four items of `docs/plans/build-followups-review-pool-next.md`, each placed by the showrunner on 2026-10-12, in its order. The showrunner, 2026-10-12, on the third: "editing <UXReview/> step 3 in commands/unit/direct.md is yours"; on the fourth: "Add one line to --shadow's output naming the machine whose pairs it counted… Reopen when a Mac unit runs design checks."

## Delegation Context

- **Project:** `~/.claude` — the Claude Code configuration repository: commands, the `/unit:direct` launchers, the review pool and the production scripts.
- **Project started:** 2026-10-13T02:21:38.189+00:00
- **Stack:** Python 3.10+ run through `scripts/lib/py`, `unittest`, basedpyright; bash launchers; markdown command files.
- **Layout:** `scripts/delegate/` (review pool, trial report), `scripts/production/` (showrunner scripts), `commands/unit/`, `commands/review/`, `commands/showrunner/`, `config/review_pool/`, `docs/as-built/`.
- **Worktree:** `/home/natepiano/worktrees/claude-build-followups-review-pool`, branch `build-followups-review-pool`. Every seat works only there.
- **Key files:**
  - `docs/as-built/review-pool.md` — the pool as built: state, verbs, the pooled review turn, the turn record, the trial report, the UI pool. Read it first; every name below is defined there.
  - `scripts/delegate/review_pool_report.py` — the trial report: `ReviewRecord` (l. 59), `ReportSources` (l. 179), `read_events` (l. 342), `local_sources` (l. 353, reads the ledger at l. 363), `remote_command` and `mac_sources` (l. 369–440, the Mac's ledger and history over ssh), `quality_lines` (l. 452), `render_shadow` (l. 589), `main` (l. 634)
  - `scripts/delegate/test_review_pool_report.py` — its tests
  - `scripts/production/review_regime.py` — the merge-hold ledger (`LEDGER`, `Row`, `read_rows`); read only
  - `scripts/production/units_table.py` — reads a production doc's Units table; read only
  - `scripts/usage/collect.py` — `MAC_HOST`; read only
  - `commands/showrunner/produce.md` — <MergeCheckpoint/>, the `review merge:` line and the hand lookup after it (l. 587–597)
  - `commands/unit/direct.md` — <UXReview/> (l. 1178–1268), step 3
  - `commands/review/direct.md` — the review unit director: how it builds and primes each standing prompt (l. 80–135)
  - `config/review_pool/ui.md` — the UI reviewer's standing prompt
  - `scripts/delegate/test_review_pool.py` — the pool's tests
- **Test lanes:** Python tests sit beside the code as `test_*.py`: `scripts/delegate/`, `scripts/production/`.
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest discover -s <dir> -p '<pattern>'` from the worktree root, with the directory and pattern each phase names.
- **Lint:** `basedpyright <the phase's Python files>`; pass is its `0 errors, 0 warnings` line (it exits 3 because pyrightconfig names a `.venv` no checkout has).
- **Invariants:**
  - The report holds no rollout parsing and no token weights; cost comes from the shared usage store (`docs/as-built/review-pool.md`, Trial report).
  - With no `**Review pool:**` line in the production doc, and outside a production, every command behaves as today (`docs/as-built/review-pool.md`, Invariants).
  - The Claude design-check helper's prompt and verdict path do not change while the UI pool is in shadow: the helper is the baseline the pool is compared with (the showrunner, 2026-10-12).
  - Python: every signature annotated, no `Any`, no file-level ignores; basedpyright reports zero errors and zero warnings (repository CLAUDE.md).
  - Files outside this unit's **Owns** (`scripts/delegate/review_pool.py`, `commands/review/`) and outside the files its first plan created (`scripts/delegate/review_pool_report.py` with its test, `config/review_pool/`) are named in each checkpoint notice as `also touches` (production_format.md, <ProductionUnit/> item 9).

## Phases

### Phase 1 — The trial report matches merge holds by plan and names its machine  · status: done

#### As-built

- `PlanKnown(path: str)` and `PlanUnavailable(reason: str)` are frozen dataclasses returned by `review_plan(record: ReviewRecord, sources: ReportSources) -> PlanKnown | PlanUnavailable`, which reads `plan_doc` and `working_dir` from the first `run_started` event of the review's `history_file`. Neither the turn record nor `ReviewRecord` carries a plan.
- `normalize_plan(path: str, working_dir: str = "") -> str` makes an absolute path under `working_dir` relative to it, then applies `os.path.normpath`; the ledger's `plan` is normalized with no `working_dir`, so `docs/plans/alpha.md`, `./docs/plans/alpha.md` and `<working_dir>/docs/plans/alpha.md` are one plan.
- `quality_lines` reads the ledger's raw JSON rows through `read_events` (not `review_regime.read_rows()`) and matches each row: no string `plan` → unit and phase; a plan with `PlanKnown` → unit, phase and normalized plan; a plan with `PlanUnavailable` → the row's holds are left out and one `source warning: merge holds unmatched: <unit> phase <phase>: review plan unavailable (<reason>)` line prints per affected phase. Mac rows and histories from `mac_sources` take the same path. No table column changed.
- `render_shadow` prints `Machine: <short host name> (pairs saved on another machine are not counted)` directly after its `Comparison:` line, the name being `socket.gethostname().split(".")[0]`; the rest of the report still calls this machine `local`. Only local verdict pairs are read.

**Files:**
- `scripts/delegate/review_pool_report.py` — plan lookup, normalization, the three-way match and warning in `quality_lines`, the `Machine:` line in `render_shadow`
- `scripts/delegate/test_review_pool_report.py` — tests for each match case, the warning, Mac sources and the `Machine:` line

**Binds later work:** the `Machine:` line in `--shadow` output keeps its text and its place after `Comparison:`.

**Gotchas:** `PlanUnavailable` reasons are exactly `history file unavailable`, `no run_started event` and `run_started has no plan_doc`. `read_events` returns `[]` for a missing file, so `local_sources` stores a history only when `is_file()` holds; an absent entry in `sources.histories` is what tells a missing history apart from an empty one. Symlinks in plan paths are not resolved.

**Ruled out:** reading a second machine's verdict pairs in `render_shadow` — reopen when a unit on the Mac runs design checks.

### Phase 2 — One command gives the showrunner a unit's review history location  · status: todo

#### Work Order

**Goal:** At each merged code checkpoint the showrunner fills the `review merge:` line's session directory from one command's output, with no run records read by hand.

**Spec:**

- New script `scripts/production/unit_run.py`, run through `scripts/lib/py`:
  `unit_run.py --production <production doc> <unit> [--root <history root>] [--no-mac]`
- It reads the unit's Worktree cell from the production doc's Units table through `units_table.py`. A unit that has no row exits 1 with `unit_run: no Units row for <unit>`.
- It reads every `runs/*.jsonl` under the history root (`--root`, else `PLAN_DELEGATE_HISTORY_DIR`, else `~/.local/state/plan-delegate`), keeps the files whose `run_started` event has `working_dir` equal to the Worktree cell after `os.path.normpath`, and chooses the greatest `run_started_at`. A file that is unreadable or holds no `run_started` is skipped.
- With no local match and without `--no-mac`, it does the same on the Mac over ssh at `collect.MAC_HOST` (`scripts/usage/collect.py`), in one remote command. A failed ssh is reported on stderr as `unit_run: Mac unavailable: <reason>` and counts as no match.
- On a match it prints exactly one line, tab-separated: `<run id>`, `<machine>` (`natedev` or `mac`), `<run_started_at as ISO-8601 UTC>`. The run id is the file's stem; it is the session directory's name, which is what the merge line carries. Exit 0.
- With no match anywhere: `unit_run: no run found for <unit> in <worktree>` on stderr, exit 1.
- In `commands/showrunner/produce.md` <MergeCheckpoint/>, replace the hand lookup (the sentences from "Find the unit's newest run" through "as the session dir's name in the merge line") with the command and one sentence: its first field is the session directory's name for the merge line; on exit 1, send the merge line with `session dir unavailable` and tell `review-unit` why.
- `commands/review/direct.md`: where the review unit director reads a merge line, a `session dir unavailable` line keeps that merge pending, as an unreadable history file does today.

**Files:**
- `scripts/production/unit_run.py` — new
- `scripts/production/test_unit_run.py` — new
- `commands/showrunner/produce.md` — <MergeCheckpoint/> names the command
- `commands/review/direct.md` — the `session dir unavailable` case

**Seats:** `1 writer + 1 tester`.
- `impl` — `scripts/production/unit_run.py`, `commands/showrunner/produce.md`, `commands/review/direct.md`
- `test` — `scripts/production/test_unit_run.py`, from the Spec alone, with a temporary history root and a temporary production doc: the newest of several runs of one worktree; a run of another worktree ignored; a worktree path with a trailing slash; a file with no `run_started` skipped; no Units row; no run found with `--no-mac`; the Mac path through a fake `ssh` on `PATH` (match, and failure).

**Constraints from prior phases:** none.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/production -p 'test_unit_run.py'` green; `basedpyright scripts/production/unit_run.py scripts/production/test_unit_run.py` prints `0 errors, 0 warnings`; `scripts/lib/py scripts/production/unit_run.py --production docs/plans/build-followups-production.md review-pool-unit --no-mac` prints this unit's newest run id.

### Phase 3 — A pooled design check reads no file on its turn  · status: todo

#### Work Order

**Goal:** A pooled UI reviewer judges its shots from its standing instructions alone: its turn reads neither `ux_eval.md` nor a rule file, and it reports the same defect rows as the Claude helper.

**Spec:**

Today every UI turn starts from a reset thread whose standing instructions hold the checklist, yet three things make the reviewer read files again on each turn: the shared prompt opens with "Read `~/.claude/commands/ux_eval.md` and follow it"; `config/review_pool/ui.md` says "Follow `~/.claude/commands/ux_eval.md`" and "Read a rule's full text before reporting its defect"; and the rule text itself is only in the guide's files. Each read is one more request that re-sends the whole thread.

- **The standing prompt holds everything.** `config/review_pool/ui.md` keeps the role line, the Three Gods sentence and the checklist, and gains the report format and the judging steps of `commands/ux_eval.md` written out in full (the `Verdict:` line, the `| Shot | Rule | Defect | Fix |` table, the `Canonical candidates` section, how to measure size and contrast with `~/.claude/scripts/ux_style/measure_text.py`). It no longer tells the reviewer to follow `ux_eval.md` or to read a rule's text. Its line about rule paths becomes: the full text of each rule follows under `## Rules`.
- **The review director adds the rules' text.** In `commands/review/direct.md`, building `<pool dir>/standing/ui.md` becomes: `config/review_pool/ui.md`, then `## Rules` with each rule file of the production doc's **UX guide** in name order as `### <file name>` and its text, then the guide's `docs/design-decisions.md` when present, then the `ui` section of `memory/repository.md`. With no UX guide in the production doc, the `ui` type is not primed and the director says so in one line. The director rebuilds and primes at start and on resume as today; a changed rule file changes the hash, which makes a new template.
- **The pooled prompt.** In `commands/unit/direct.md` <UXReview/> step 3, the unit director writes two prompt files. The Claude helper's is unchanged. The pool's is:

  > Judge these shots by your standing instructions, which hold the rules and the report format: <paths>. Scale: <shot pixels per logical pixel>. Context: <unit> phase <N> — <what changed, every state the shots must show, and each shot's window size in logical pixels>. Production rules: <the production doc's rules that concern looks>. You only review: edit nothing. Return only the verdict and the table.

  It names no file to read: no `ux_eval.md`, no guide path, no rule path. The shots reach the reviewer as images through `ux_shots_<pass>.txt`, as today.
- **The stale sentence.** `commands/review/direct.md` l. 130 says the launcher sends a unit's memory file only at lease ordinal 1. Correct it: at ordinal 1 for `code`, and on every `ui` turn.
- **Live check, by the unit director.** In a scratch pool (`REVIEW_POOL_STATE` and `PLAN_DELEGATE_HISTORY_DIR` set to scratch directories), prime `ui` from a standing prompt built as above from `~/rust/hanadocs/ux/`, run one shadow check on the shots the first plan's live check used, and read the pool turn's log: no read of `ux_eval.md` or of a rule file, the same defect rows as the helper by `review_pool_report.py --shadow`, and the turn's weighted tokens beside the earlier check's.

**Files:**
- `config/review_pool/ui.md` — the report format and judging steps in full; no instruction to read a file
- `commands/review/direct.md` — the UI standing prompt's build with the rules' text; the unit-memory sentence
- `commands/unit/direct.md` — <UXReview/> step 3: the pool's own prompt
- `scripts/delegate/test_review_pool.py` — the test below
- `docs/as-built/review-pool.md` — Prompts and UI pool sections, at the as-built pass

**Seats:** `1 writer + 1 tester` — the three markdown files are one writer's; the test reads only `config/review_pool/ui.md`.
- `impl` — `config/review_pool/ui.md`, `commands/review/direct.md`, `commands/unit/direct.md`
- `test` — in `scripts/delegate/test_review_pool.py`: `config/review_pool/ui.md` holds the verdict line format and the four-column table header that `read_ux_verdict` requires, names `measure_text.py`, and contains neither `Follow \`~/.claude/commands/ux_eval.md\`` nor `Read a rule's full text`.

**Constraints from prior phases:** `review_pool_report.py --shadow` prints the `Machine:` line (the trial-report phase); the live check's output carries it.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/delegate -p 'test_review_pool.py'` green; the live check above: no file read on the pool's turn, and `pool missed defects: 0`.
