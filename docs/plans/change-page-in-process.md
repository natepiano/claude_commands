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

**Depends on:** none

#### Work Order

**Goal:** A verdict on the change page starts none of the four child Python processes for `change_review` and `feature_log`; it calls the `change_review` and `feature_log` library functions in the server process, with the same saved results, messages and refusals as before.

**Spec:**

1. `change_review.py`: move the body of the `_run` case `"verdict"` into one public library function. It takes the list path, the row id, the verdict text and the production doc path, and returns its result as data: the lines the CLI prints on stdout, and a conflict refusal with the text the CLI prints on stderr. It prints nothing. The `verdict` CLI case calls it, prints the same lines to the same streams, and returns the same exit status (1 on a conflict). `ChangeReviewError` and `OSError` keep their current CLI handling.
2. `feature_log.py`:
   - Make `_project_of` public as `project_of(path: str) -> str | None`. `_run_project_of` calls it. It keeps its `git` child process; that is not a Python start.
   - Add one public library function for each review closeout action. Land lands the review proposal with the verb that the proposal records (`add` or `supersede`). Drop drops it with a one-line verdict. Both take `if_proposed`, return the output lines, print nothing and raise `FeatureLogError` on a refusal. `_run_review_land` and `_run_drop` call them and print the lines. The CLI `add --review` and `supersede --review` keep their check that the named verb equals the proposal's verb, and keep the text `proposal <row> uses <verb>; run <verb> --review <row>`.
   - The off-host rule is one function, used by `main` and by the closeout library functions. Off host, a library function forwards the matching CLI command by ssh with `_ssh_command`, captures its output, returns its stdout lines, and raises `FeatureLogError` with its stderr text on a nonzero exit. `main` keeps its inherited-output forward.
3. `change_page.py`:
   - `close_feature_log` calls `feature_log.project_of(str(repo))`; `None` is not in the closing projects. It calls the land function when `change_review.lands(verdict)` is true, else the drop function, both with the review id and `if_proposed=True`. The retry that reads `uses supersede` from stderr goes away, because land uses the proposal's verb. A `FeatureLogError` becomes the `RuntimeError` that `closeout_before_save` catches today, with the same error text.
   - `submit` calls the `change_review` verdict function with `page.review_list`, `row_id`, `verdict` and the doc path that `change_review.production_doc(<owner name>)` gives for the owner name it passes today. A conflict refusal or a `ChangeReviewError` or `OSError` clears the status and raises `AnswerRefused` with the text the child wrote before. The message carries the returned stdout lines joined by newlines, where it carried `result.stdout.strip()`.
   - Remove `import subprocess` when no use remains.
4. Thread safety: the calls run on HTTP worker threads beside other requests. The library functions change no cwd, environment, `sys.argv`, `sys.path`, stream or signal handler, and mutate no module global. Locks stay per call (`feature_log.locked`, `change_review._list_lock`, both `flock` on a new open file).
5. Tests: the `test_change_page.py` tests that patch `subprocess.run` to fail or interrupt the verdict or the closeout (near lines 761, 2106, 2451-2527) patch the new library functions instead, with the same cases. Do not use `invoke_cli` from server threads. Add no new test file. A new case is a row in an existing test (test growth rule 1). Add a row for the off-host closeout forward to the existing off-host test in `test_feature_log.py`, if one exists.

**Files:**
- `scripts/production/change_review.py` — the verdict library function; the CLI case calls it
- `scripts/feature_log/feature_log.py` — `project_of`, the land and drop library functions, the one off-host rule
- `scripts/review_links/change_page.py` — in-process calls in `close_feature_log` and `submit`
- `scripts/review_links/test_change_page.py` — patches move from `subprocess.run` to the library functions
- `scripts/production/test_change_review.py` — rows for the verdict function, only where an existing test needs one
- `scripts/feature_log/test_feature_log.py` — rows for land, drop and the off-host forward, only where an existing test needs one

**Workers:** 1 writer — the three modules share one change; nothing splits.
- `impl` — all Files above.

**State:** none — the phase changes how existing writes start, not what they write.

**Constraints from prior phases:** none.

**Acceptance gate:**
- The test modules in **Test** pass, and `basedpyright` on each changed file gives `0 errors, 0 warnings, 0 notes`.
- `grep -n "subprocess" scripts/review_links/change_page.py` finds no child process for `change_review` or `feature_log`.
- CPU time (user plus system) of `scripts/lib/py -m unittest scripts.review_links.test_change_page`: the median of two runs after the change is about 10 CPU s less than the median of two runs on the phase base `9dbb68407`, measured on the same load.
