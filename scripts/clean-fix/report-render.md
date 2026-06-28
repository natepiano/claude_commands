# Clean-fix Report

Render a status view of one clean-fix run from a parsed log. All log discovery, regex matching, phase slicing, and bookkeeping suppression lives in `~/.claude/scripts/clean-fix/clean_fix_report_parse.py`. This document only routes arguments and renders the parsed output. It is consumed two ways: by `/clean_fix report` (interactive) and by `clean-fix.sh`, which pipes it into a headless claude after each run.

## Arguments

`$ARGUMENTS` may be:

1. **Empty** — call `clean_fix_report_parse.py` with no arguments. The parser renders the current clean-fix state for every keyed `[projects]` target, independent of whichever log was written last.
2. **The literal word `list`** — call `clean_fix_report_parse.py --list`. Print the numbered list (path, date, time, duration, status, phases). Ask the user to pick by index, then call `clean_fix_report_parse.py <chosen-path>` and render.
3. **The literal word `latest` or `newest`** — call `clean_fix_report_parse.py --latest-log`. This is the explicit old behavior: parse the newest log in `~/.local/logs/clean-fix/`.
4. **A path** — call `clean_fix_report_parse.py <path>`. If the parser exits with `ERROR: log not found`, surface that and stop.
5. **Any other token** (e.g. `rebuild`, which `clean-fix.sh` substitutes in its headless invocation) — treat as Empty: current keyed-project state.

## Parser output format

```
PATH: <path>
MTIME_AGO: <age>
RUN_START: <ts>
RUN_END: <ts>
ELAPSED: <duration | "-">
STATUS: complete | crashed | partial | in-progress | current

PHASE <name> present=<bool> ok=N fail=N skip=N [footer_ok=N footer_fail=N footer_total=N]
ROW <project>  clean=<cell> warmup=<cell> eval=<cell> review=<cell> fix=<cell> verify=<cell> reason="<short reason | ->" [phase_now="<live phase>"]
ALWAYS_EXCLUDED "<reason>" count=N projects=<a,b,c>   ← directories under ~/rust not opted into the relevant allowlist ([build] / [projects]) in clean-fix.conf
FILTERED_OUT "<reason>" count=N projects=<a,b,c>      ← would be eligible, but framework state / project layout filtered them out
WARNING <phase> <project> "<message>"                 ← real project failures
TOOL_WARNING <phase> <project> "<message>"            ← sub-tool failed but project itself is healthy
SKIP_REASON <phase> "<reason>" count=N projects=<a,b,c>  ← only meaningful skips survive
NOTE <free-text>
```

`ALWAYS_EXCLUDED` and `FILTERED_OUT` are different audiences. The first is settled (the directory was never opted into the allowlist, nothing to act on). The second is project state the user might want to clean up (stale worktree, missing Cargo.toml, etc.). Render them separately — never combine into one list.

Only real participants appear in `ROW`.

Cell values are `OK`, `FAIL`, `SKIP`, `RUNNING`, `-` (rendered as `—`). They may carry a `:slug` suffix such as `OK:no-findings` — strip that for the phase columns. Render the quoted `reason` field as the final table column; render `-` as `—`.

`Fix: OK` means the style fix was applied and a `_style_fix` worktree is ready for the user to `/style_fix_review` → `/merge_branch` — it is **not** auto-merged. The row reason says `fix applied — worktree ready for /style_fix_review` and the eval stop reason (quota/exhausted) is suppressed as noise in that case.

An eval skip reason of `evaluation ran; worktree intentionally unchanged because proposed fixes need your approval` means the evaluation and fix pass produced a Fix Summary, but the worktree is deliberately clean because every proposed change is approval-gated (for example a public API or reflected-schema rename). Do not render it as an applied fix or merge-ready worktree; tell the user there is a proposal to review/approve before code should change.

`verify=<cell>` is the second style-fix pass: after the fix agent applies the findings, the same configured agent re-checks the diff against the Fix Summary, corrects mistakes, and updates the summary. Cell meaning:
- `OK` — the verify pass ran and wrote its `## Fix Verification` section (the fix was confirmed and/or corrected). The detailed per-finding verdict and any corrections live in that section inside the worktree; the user sees it during `/style_fix_review`.
- `FAIL:<reason>` — the verify pass started but did not finish (e.g. `timeout`, `agent-exit`). The applied fix is still in the worktree and reviewable; only the independent verification is missing. This does **not** mean the fix failed — read it as "fix applied, not yet verified."
- `RUNNING` — the verify pass is in progress right now.
- `—` — verify did not run for this row (the fix produced no Fix Summary, the fix was skipped, or this is an old log from before the verify pass existed).

`RUNNING` means the phase agent was launched and the run is still live — it has not reported an outcome yet. The parser only emits `RUNNING` while the run is in progress and that phase has no `=== Done:` footer; a finished run never shows `RUNNING` (an unresolved launch there becomes `FAIL:no-result`).

`phase_now="<live phase>"` appears on a ROW only while that row is still running. It is the precise current sub-phase of the style-fix pipeline for that project — e.g. `applying: write fix summary`, `verifying: clippy`, `build check (after verify)`. This is the single source for "what phase is this row in right now"; it already rides in the row `reason` for running rows, and the `RUNNING` records below carry the same information.

## Rendering

Use plain language. No "matrix", "cell", "flag", "footer", or "reconcile" jargon.

### 1. Header

```
Report for: <PATH>
Run: <RUN_START> → <RUN_END> (<ELAPSED>, <STATUS>)
```

If `STATUS` is `current`, render `Current state as of: <RUN_START>` instead of a run window.

If `STATUS` is `partial` or `in-progress`, replace the parenthetical with `partial log` or `still running`.

### 2. Not-run sections (two distinct groups)

Render these as **two separate sections**, never one combined list. They have different audiences.

**Excluded** — render only if `ALWAYS_EXCLUDED` or `FILTERED_OUT` records exist. The orchestrator picks up every directory under `~/rust/` automatically (no allowlist) so anything not under "Excluded" is implicitly in. Render as a header line followed by one bullet per reason, with the **category name in bold**. Use these category names (map from the parser's raw reason text):

| Parser reason text                            | Bullet category                            |
|-----------------------------------------------|--------------------------------------------|
| `excluded`                                    | `by config`                                |
| `no Cargo.toml`                               | `no Cargo.toml`                            |
| `style-fix worktree`                          | `style_fix worktree already exists`        |
| `worktree, not primary checkout`              | `not the primary checkout`                 |
| `no bevy_panorbit_camera/Cargo.toml`          | `stale config (source dir missing)`        |
| `style_fix directory exists`                  | `existing _style_fix worktree`             |
| `another worktree checkout already exists`    | `another worktree checkout already exists` |

Format:

```
**Excluded:**
- **by config** — bevy, bevy_hana, bevy_mod_outline, ...
- **no Cargo.toml** — cache-apt-pkgs-action, nate_style
- **style_fix worktree already exists** — bevy_window_manager_style_fix, hana_style_fix, ...
- **not the primary checkout** — cargo-port-api-fix
- **stale config (source dir missing)** — bevy_panorbit_camera_extras
```

Skip any bullet whose project list is empty. Sort projects alphabetically within each bullet. Order the bullets in the order the rows appear above (config → no Cargo.toml → worktree categories → stale config).

If neither `ALWAYS_EXCLUDED` nor `FILTERED_OUT` has records, omit the Excluded section entirely.

### 3. Status table

Markdown table with columns `Project | Clean | Warmup | Eval | Review | Fix | Verify | Reason`. Preserve parser row order: `Eval` status first (`RUNNING`, `FAIL`, `OK`, `SKIP`, then `—`), then project name alphabetically. Each phase cell is `OK`, `FAIL`, `SKIP`, `RUNNING`, or `—`. **Strip the `:reason` suffix from phase cells** (so a `verify=FAIL:timeout` cell renders as `FAIL`; the reason text is carried by the final column). Render the parser's quoted `reason` value in the final column, using `—` when the value is `-`. The `Verify` column comes from the `verify=<cell>` field — see the verify-cell meanings above; for a finished run with fixes applied it is normally `OK`, and `—` for any run with no applied fix or any old log. If `cargo-mend` shows `clean=OK:warning`, render the cell as `OK*` and add a footnote line under the table: `* cargo-mend built fine, but the cargo mend tool itself failed against it. The build is healthy; the linter is not.`

### 4. Still running

Only render if `RUNNING` records exist (the run is in progress). Heading: `Still running`. **One bullet per project** (not per record — a project mid-verify produces both a `RUNNING fix` and a `RUNNING verify` record; collapse them). For each running project name the project and its current phase, preferring the row's `phase_now` value (the precise sub-phase) when present; otherwise fall back to the `RUNNING` record's detail verbatim:

```
Still running
- bevy_brp_bevy_update (eval): running 22m, agent reached quota reached, finalizing; last heartbeat 12s ago
- bevy_catenary: verifying: clippy   ← phase_now: fix applied, the verify pass is re-checking it
- hana: applying: write fix summary
```

When `phase_now` reads `verifying: …` / `verification written` / `verify …`, the apply pass already finished and wrote the Fix Summary — the project is in the second (verify) pass. When it reads `applying: …` / `creating worktree` / `worktree ready`, it is still in the first (apply) pass. `build check (after verify)` means both agents are done and the external `cargo check` gate is running.

A `heartbeat stale … — finishing or wedged` detail (eval phase) means the eval agent stopped emitting heartbeats: it is either finalizing or hung. If it persists across reports the agent likely died and the run will reap it as a failure — re-run the report once the run finishes for the real outcome.

### 5. What failed

Only render this section if there are `WARNING` records. Use full sentences:

```
What failed
- bevy_catenary at the fix step: worktree creation failed because the branch `refactor/style` already exists. The retry didn't recover. Manual cleanup needed before the next run.
```

Combine multiple `WARNING` records about the same project into a single sentence — don't print the same project twice.

### 6. Sub-tool warnings

Only if `TOOL_WARNING` records exist. Heading: `Tool issues (not project failures)`. One bullet per tool warning, e.g. `cargo-mend: the cargo mend linter failed against this project. The build itself succeeded.`

### 7. Skipped on purpose

Only if `SKIP_REASON` records exist (the parser already drops bookkeeping reasons; whatever survives is a real workflow decision). Heading: `Skipped on purpose`. Render as bullets in plain English, naming the projects:

```
Skipped on purpose
- cargo-mend, cargo-port: already at the per-project cap of 2 style findings, so no new evaluation this run.
```

A reason of the form `had <X> during this run; now resolved (merged or discarded)` means eval skipped the project at run time because a fix was pending, but that pending evaluation has since been finalized, merged, or discarded — the project is no longer blocked and is eligible next run. Render it as resolved, not stuck. A reason like `an applied fix awaiting your review/merge` (no "had …") means it is still pending right now. A reason like `evaluation ran; worktree intentionally unchanged because proposed fixes need your approval` means the pending review is approval-only; the clean worktree is expected and should not be treated as missing work.

### 8. Heads up

Render any `NOTE` records as bullets under the heading `**Heads up**`. Phrase each as a one-line user-facing call to action — name the path, name what's needed.

Examples of NOTEs the parser emits:
- `~/rust/nate_style left dirty: 1 worktree run(s) failed; leaving nate_style dirty for review` → `nate_style is in a dirty state because a worktree fix failed. Review and commit (or discard) ~/rust/nate_style before the next run.`
- `style-fix script failed before per-project work` → `the style-fix script crashed before doing any work — investigate the orchestrator log.`
- `phases not in this log: clean,warmup,eval,review` → omit (this is a partial-log informational, already implied by the empty cells).

Also synthesize NOTEs for:
- **Footer mismatches**: a `PHASE` line where `ok + fail` doesn't equal the count of per-project results in the matrix for that phase. Phrase as `Eval phase: footer says 12 in scope, 10 produced results, 2 unaccounted for.`
- **Status anomalies**: if `STATUS` is `crashed`, lead with `The run crashed mid-phase.` if `partial`, lead with `Partial log — only the <phase> phase ran.`

If there's nothing to surface, omit the section entirely.

## Notes for the renderer

- Do not render any section that has no data. A clean run with only OK rows should be a header + excluded line + table, nothing else.
- Do not invent commentary. If the parser doesn't emit it, don't write it.
- Do not `grep`/`awk`/`tail` the log. If the parser is missing something you need, fix the parser.
- Markdown links don't render in the terminal — don't use them.
