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

### Phase 2 — One command gives the showrunner a unit's review history location  · status: done

#### As-built

- `scripts/lib/py scripts/production/unit_run.py --production <production doc> <unit> [--root <history root>] [--no-mac]` prints `<run id>\t<natedev|mac>\t<run_started_at as ISO-8601 UTC>`, exit 0. The run id is the run file's stem: the session directory's name for the `review merge:` line.
- The Worktree cell comes from the Units table through `add_unit.unit_table` / `add_unit.cell_value`; no row exits 1 with `unit_run: no Units row for <unit>`.
- History root: `--root`, else `PLAN_DELEGATE_HISTORY_DIR`, else `~/.local/state/plan-delegate`. Among `runs/*.jsonl` whose `run_started.working_dir` equals the Worktree cell after `os.path.normpath`, the greatest numeric `run_started_at` wins (ties: first sorted name); unreadable files and files with no matching `run_started` are skipped.
- No local match and no `--no-mac`: one batch-mode `ssh` to `collect.MAC_HOST` (30 s limit) runs the same scan on the Mac; failure prints `unit_run: Mac unavailable: <reason>` and counts as no match. No match anywhere: `unit_run: no run found for <unit> in <worktree>`, exit 1.
- `commands/showrunner/produce.md` <MergeCheckpoint/> takes the merge line's session directory from the first field; on exit 1 it sends `session dir unavailable` and tells `review-unit` why, and `commands/review/direct.md` keeps that merge pending.

**Files:**
- `scripts/production/unit_run.py` — the lookup
- `scripts/production/test_unit_run.py` — 16 tests; a fake `ssh` on `PATH` covers the Mac path
- `commands/showrunner/produce.md`, `commands/review/direct.md` — the merge-line steps above

**Gotchas:** a run file counts once its `run_started` line reads; undecodable bytes later in it do not demote it.

**Ruled out:** reading the Units table through `units_table.py` — it has no Units-table reader; skipping a newest run with undecodable later bytes — that names an older run's session directory.

### Phase 3 — A pooled design check reads no file on its turn  · status: done

#### As-built

- `config/review_pool/ui.md` holds the pooled UI reviewer's whole procedure: the checklist, the judging steps (shrink each shot, check context coverage, canonical-candidate slots, contrast and text size measured with `measure_text.py`, window-size normalisation, confirming each defect against the included rule text) and the report format (`Verdict: pass | defects`, the `| Shot | Rule | Defect | Fix |` table, canonical candidates). It tells the reviewer not to load command or rule files.
- `commands/review/direct.md` <PrimeReviewMemory/> builds `standing/ui.md` from `ui.md`, then `## Rules` (`UX guide: <absolute path>`, then each guide-root `.md` with a non-empty frontmatter `test:`, in file-name order, as `### <file name>` plus its text), then `docs/design-decisions.md` of the repository that holds the production doc under `## Design decisions`, then the `ui` memory section. With no UX guide, `ui` is not primed and the director tells the showrunner `UI pool not primed: production has no UX guide`. An unreadable guide or rule fails the prime.
- <ReviewMemory/> now says the launcher sends unit memory at lease ordinal 1 for `code` and on every `ui` turn, as `review.sh` already did.
- `commands/unit/direct.md` <UXReview/> step 3 writes `${SESSION_DIR}/ux_prompt_<PASS_INDEX>_helper.md` (the unchanged `ux_eval.md` prompt) and `ux_prompt_<PASS_INDEX>_pool.md` ("Judge these shots by your standing instructions, which hold the rules and the report format: …"), and launches the pool with the pool file.
- Live check (scratch pool, hana guide, 30 rules, one stored shot): the pooled turn read neither `ux_eval.md` nor a rule file, `review_pool_report.py --shadow` gave `pool missed defects: 0`, and the turn cost 79,903 weighted tokens in 6 requests against 102,264 in 8 for the earlier check.

**Files:**
- `config/review_pool/ui.md` — the self-contained standing instructions
- `commands/review/direct.md` — building and priming `standing/ui.md`
- `commands/unit/direct.md` — the helper and pool prompt files
- `scripts/delegate/test_review_pool.py` — `UIStandingPromptTests` pins the verdict line, the table header, `measure_text.py`, and the absence of the old read instructions

**Gotchas:** rule selection matches `scripts/ux_style/load-ux-style.sh`, so README and notes are left out. Design decisions come from the production's repository, the file `ux_eval.md` reads from a unit's worktree, never from the guide. The pooled turn may still read `measure_text.py`'s source for its flags; that is a script, not a rule.

**Ruled out:** taking design decisions from the guide — the helper and the pool would then judge against different exceptions.
