# Change page in-process calls

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The change page server calls the `change_review` and `feature_log` library functions in its own process, with no child Python process for a verdict.

> **As-built disposition: amend** — `docs/as-built/review-links.md` (change page verdicts)

## Source

sdlc [cb350813] 2026-11-09, approved: natest 105 proposal (`~/.local/state/showrunner/20b0427d-945b-4c40-bb24-a6bec83dd648/natest105_change_page_proposal.md`). The change page server answers each verdict on an HTTP worker thread and starts child Python processes for `feature_log.py project-of`, the feature-log land or drop command, `feature_log.py supersede`, and `change_review.py verdict`. Each child pays a full Python start and a module import. Phase 105 measured `scripts/review_links/test_change_page.py` at a 22.3 CPU s pair median on base `befc1a04a`, and at 7.2 CPU s with these calls in process.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-09T11:31:09+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Layout:** `scripts/review_links/` (the change page server), `scripts/production/` (change review lists), `scripts/feature_log/` (the feature log).
- **Key files:**
  - `scripts/review_links/change_page.py` — the change page: `close_feature_log` (lines 598-623) and `submit` (lines 642-711) run on the HTTP worker threads of `serve.ReviewLinksServer` (a `ThreadingHTTPServer`)
  - `scripts/production/change_review.py` — review lists: `_run` case `"verdict"` (lines 1799-1816), `record_verdict`, `end_row_scratch`, `production_doc`, `lands`; `main` maps `ChangeReviewError` and `OSError` to stderr and exit 1
  - `scripts/feature_log/feature_log.py` — the feature log: `main` (line 2424) forwards a command by ssh to natedev when the host is not natedev and `FEATURE_LOG_VAULT` is unset (`_ssh_command`, `_run_ssh`); `_run_review_land` (line 1658), `_run_drop`, `_run_project_of` (line 2291), `_project_of` (line 2275), `locked` (line 985, a `flock` on a new open file for each call)
  - `scripts/review_links/test_change_page.py`, `scripts/production/test_change_review.py`, `scripts/feature_log/test_feature_log.py` — their tests
  - `scripts/production/cli_fixture.py` — `invoke_cli`, a test fixture that patches the environment, cwd, argv and streams of the whole process; it is not safe beside other threads
- **Test lanes:** none (Python unittest modules beside the code).
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest scripts.review_links.test_change_page scripts.production.test_change_review scripts.feature_log.test_feature_log`, from the worktree root.
- **Lint:** `basedpyright <changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line; the exit status says nothing.
- **Invariants:**
  - Tests use fixtures; they never read live state (`~/.claude` main, `/tmp/claude`, the live vault, live staff).
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - All text uses Simplified Technical English.
  - One place for each thing: each action has one library path that the CLI and the change page both call. No copy of the verdict, land, drop, project or off-host rules.

## Phases

### Phase 1 — The change page calls change_review and feature_log in process  · status: done

#### As-built

- `change_review.save_verdict(path, row, verdict, production) -> VerdictSaved | VerdictRefused` writes one verdict and prints nothing. `record_verdict`, `_edit` and `_commit` send warnings through `report_warning`. The `verdict` CLI prints the returned lines and exits 1 on a refusal.
- `feature_log.project_of(path) -> str | None`, `land_review(row, if_proposed, *, verb)` and `drop_review(row, verdict, if_proposed)` return output lines, print nothing and raise `FeatureLogError` on a refusal. Land uses the verb that the proposal records. The `land` CLI command lands a review with that verb.
- `_off_host` is the one off-host rule. `main` forwards with inherited output. `land_review` and `drop_review` forward by ssh with captured output and raise `FeatureLogError` with the remote stderr.
- `FeatureLogError.diagnostic` gives one `feature_log: ` prefix for local and remote refusals.
- `change_page.close_feature_log` and `submit` call these functions on the server's HTTP worker threads. A verdict starts no child Python process for `change_review` or `feature_log`. Saved results and refusal texts are unchanged. The CPU median of `test_change_page.py` went from 24.42 s to 11.37 s.
- `atomic_file` reads the default new-file mode from a probe file that it creates with mode `0o666` and removes at once. It never sets the process umask.

**Files:**
- `scripts/production/change_review.py` — `save_verdict`; the `verdict` CLI calls it
- `scripts/feature_log/feature_log.py` — `project_of`, `land_review`, `drop_review`, `_off_host`, the `land` CLI command
- `scripts/review_links/change_page.py` — in-process calls in `close_feature_log` and `submit`
- `scripts/lib/atomic_file.py` — default new-file mode without a umask change
- `scripts/review_links/test_change_page.py`, `scripts/production/test_change_review.py`, `scripts/feature_log/test_feature_log.py`, `scripts/lib/test_atomic_file.py` — rows for the library calls, the off-host forward and the umask-free mode

**Gotchas:**
- `os.umask` is process-wide. Code that runs on a server thread never sets it.
- An off-host land forwards the `land` command to natedev. A Mac change page on this code needs natedev `~/.claude` main to carry that command.

**Ruled out:**
- The `folder_audit.py` child of `scratch.clean_cargo_outputs`: it is older than this phase and outside its files. `docs/plans/change-page-in-process-next.md` holds it.
- A default ACL check on the new-file mode: the mode equals the mode of a plain `open()` in that folder.
