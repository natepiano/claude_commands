---
description: Show a per-project nightly status table across clean / warmup / evaluation / review / style-fix phases, with commentary
---

# Nightly Report

Render a status view of one nightly run from a parsed log. All log discovery, regex matching, phase slicing, and bookkeeping suppression lives in `~/.claude/scripts/nightly/nightly_report_parse.py`. This command only routes arguments and renders the parsed output.

## Arguments

`$ARGUMENTS` may be:

1. **Empty** — call `nightly_report_parse.py` with no arguments. The parser picks the newest log in `~/.local/logs/nightly/`.
2. **The literal word `list`** — call `nightly_report_parse.py --list`. Print the numbered list (path, age, status, phases). Ask the user to pick by index, then call `nightly_report_parse.py <chosen-path>` and render.
3. **A path** — call `nightly_report_parse.py <path>`. If the parser exits with `ERROR: log not found`, surface that and stop.

## Parser output format

```
PATH: <path>
MTIME_AGO: <age>
RUN_START: <ts>
RUN_END: <ts>
ELAPSED: <duration | "-">
STATUS: complete | crashed | partial | in-progress

PHASE <name> present=<bool> ok=N fail=N skip=N [footer_ok=N footer_fail=N footer_total=N]
ROW <project>  clean=<cell> warmup=<cell> eval=<cell> review=<cell> fix=<cell>
ALWAYS_EXCLUDED "<reason>" count=N projects=<a,b,c>   ← user opted these out via [exclude] in nightly-rust.conf
FILTERED_OUT "<reason>" count=N projects=<a,b,c>      ← would be eligible, but framework state / project layout filtered them out
WARNING <phase> <project> "<message>"                 ← real project failures
TOOL_WARNING <phase> <project> "<message>"            ← sub-tool failed but project itself is healthy
SKIP_REASON <phase> "<reason>" count=N projects=<a,b,c>  ← only meaningful skips survive
NOTE <free-text>
```

`ALWAYS_EXCLUDED` and `FILTERED_OUT` are different audiences. The first is settled (user chose to exclude them, nothing to act on). The second is project state the user might want to clean up (stale worktree, missing Cargo.toml, etc.). Render them separately — never combine into one list.

Only real participants appear in `ROW`.

Cell values are `OK`, `FAIL`, `SKIP`, `-` (rendered as `—`). They may carry a `:slug` suffix — strip that for the table; the full reason is in commentary.

## Rendering

Use plain language. No "matrix", "cell", "flag", "footer", or "reconcile" jargon.

### 1. Header

```
Report for: <PATH>
Run: <RUN_START> → <RUN_END> (<ELAPSED>, <STATUS>)
```

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

Markdown table with columns `Project | Clean | Warmup | Eval | Review | Fix`. Sort alphabetically (parser already does). Each cell is `OK`, `FAIL`, `SKIP`, or `—`. **Strip the `:reason` suffix.** If `cargo-mend` shows `clean=OK:warning`, render the cell as `OK*` and add a footnote line under the table: `* cargo-mend built fine, but the cargo mend tool itself failed against it. The build is healthy; the linter is not.`

### 4. What failed

Only render this section if there are `WARNING` records. Use full sentences:

```
What failed
- bevy_catenary at the fix step: worktree creation failed because the branch `refactor/style` already exists. The retry didn't recover. Manual cleanup needed before the next run.
```

Combine multiple `WARNING` records about the same project into a single sentence — don't print the same project twice.

### 5. Sub-tool warnings

Only if `TOOL_WARNING` records exist. Heading: `Tool issues (not project failures)`. One bullet per tool warning, e.g. `cargo-mend: the cargo mend linter failed against this project. The build itself succeeded.`

### 6. Skipped on purpose

Only if `SKIP_REASON` records exist (the parser already drops bookkeeping reasons; whatever survives is a real workflow decision). Heading: `Skipped on purpose`. Render as bullets in plain English, naming the projects:

```
Skipped on purpose
- cargo-mend, cargo-port: already at the per-project cap of 2 style findings, so no new evaluation this run.
```

### 7. Heads up

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
