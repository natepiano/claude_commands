---
description: Show a per-project nightly status table across clean / warmup / evaluation / review / style-fix phases, with commentary
---

## Arguments

`$ARGUMENTS` may be:

1. **Empty** — pick the newest log in `~/.local/logs/nightly/` by **filename timestamp** (the `YYYYMMDD-HHMMSS` embedded in the filename) and report on it. Filename timestamp is preferred over mtime because copying or rewriting a file changes mtime but not the encoded run-start.
2. **The literal word `list`** — enumerate available logs in `~/.local/logs/nightly/` (newest first by filename timestamp, with run-age delta and a one-word phase summary), ask the user to pick one, then report on the chosen log. Do not produce a matrix on this turn.
3. **A path** — report on that exact file. If the file does not exist, tell the user `No log at <path>` and stop.

If `~/.local/logs/nightly/` is empty or missing, fall back to:
- `~/.local/logs/nightly-rust-clean-build.log` (legacy single-file path; may be a symlink to the latest run)
- `/tmp/nightly-rust-clean-build-stdout.log` (launchd plist sink)

If none exist, tell the user `No nightly logs available` and stop.

## Log accumulation

Every run that produces a log lives under `~/.local/logs/nightly/`:

- `nightly-YYYYMMDD-HHMMSS.log` — full orchestrator runs (clean + warmup + eval + fix), produced by `nightly-rust-clean-build.sh`
- `style-fix-manual-YYYYMMDD-HHMMSS.log` — manual style-fix runs launched via `~/.claude/scripts/nightly/style-fix-manual.sh`

Manual runs that cover only one phase produce a partial log — the matrix should still render, but with `—` in every column the log does not have data for, and a Notes line saying which phases are absent.

## Goal

Summarize one run as a single matrix the user can scan in one glance:

- **Rows** = every project that appeared in any phase (union of the clean-phase iteration list, the warmup targets, the style-evaluation list, the eval-review list, and the style-fix eligible list).
- **Columns** = **Clean**, **Warmup**, **Eval**, **Review**, **Fix**.
- **Cells** = `OK`, `FAIL`, `SKIP`, or `—` when the phase did not apply (or the log did not cover that phase).

Below the table, write a Commentary section that names every failure, every script-level crash, and aggregate skip counts. The user should be able to tell from this report alone *what ran, what did not, what failed, and why*.

## Steps

### 0. Resolve the log path

Apply the rules in **Arguments** to pick `${LOG_PATH}`. State it in one line at the top of the report:
```
Report for: <path> (mtime: Ns ago)
```

For the `list` argument:
- Enumerate `~/.local/logs/nightly/*.log`, sort by the `YYYYMMDD-HHMMSS` substring in the filename (descending — newest first).
- For each file, compute the run-age delta from the filename timestamp (parse with `date -j -f '%Y%m%d-%H%M%S' <ts>`) against `$(date +%s)` and format as `Nm Ns ago` / `Nh Nm ago` / `Nd Nh ago`.
- For each file, peek `head -1` for the run start timestamp and `tail -3` for the run-complete marker. Tag the entry with one of: `complete`, `crashed`, `partial (style-fix only)`, or `in progress`.
- Print a numbered list. Ask the user to pick by index. Wait for the answer; on this turn, do not produce a matrix.

### 1. Read and bound the run

- **Start**: first line matching `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}` — extract the timestamp.
- **End**: line matching `=== Nightly Rust clean \+ rebuild complete \((\d+m \d+s)\) ===`. If absent, the run is still in progress or it is a partial (single-phase) log — note that in commentary and proceed with whatever data exists.

### 2. Slice the log into phases

Phase boundaries (in order):

1. **Clean+rebuild** — from start until the first `WARMUP` line or `=== Style evaluation` header, whichever comes first. (Absent in style-fix-manual logs.)
2. **Warmup** — all `WARMUP*` lines (these are scattered between clean and eval). (Absent in style-fix-manual logs.)
3. **Style evaluation** — between `=== Style evaluation: N projects ===` and the matching `=== Done: A succeeded, B failed out of N ===`. (Absent in style-fix-manual logs.)
4. **Style eval review** — between `=== Style eval review: N projects ===` and the matching `=== Done: A reviewed, B failed out of N ===`. (Absent in style-fix-manual logs and in old logs that predate the review stage.)
5. **Style fix** — between `=== Style-fix worktrees: N eligible projects ===` and end-of-run. (This is the only phase a style-fix-manual log contains.)

If a phase header is missing entirely, mark every project's cell for that phase as `—` and add a Notes line saying which phases the log does not cover.

### 3. Parse each phase per project

**Clean+rebuild phase**
- `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} CLEAN: <project>` and the matching `BUILD: <project>` line mean the project was processed. Without an explicit per-project DONE marker, treat it as `OK` unless an `ERROR:` or `WARNING:` line for that project appears in this phase slice.
- `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} SKIP: <project> \((reason)\)` → `SKIP` (capture reason for commentary).

**Warmup phase**
- `WARMUP OK: <project>` → `OK`
- `WARMUP FAIL: <project> \((reason)\)` → `FAIL` (capture reason)
- `WARMUP SKIP: <project> \((reason)\)` → `SKIP` (capture reason)
- Project not listed in warmup at all → `—`

**Style evaluation phase**
- `OK: <project> \(\s*\d+ lines\)` → `OK`
- `FAILED: <project>` / `ERROR: <project>` / `TIMEOUT: <project>` → `FAIL` (capture trailing parenthetical as reason)
- `SKIP: <project> \((reason)\)` → `SKIP`
- `Launched: <project> via codex (PID …)` with no later result → `FAIL (no result; run ended mid-phase)`
- Project not listed → `—`
- Cross-check the phase footer: `=== Done: A succeeded, B failed out of N ===` — A and B should equal the cells you marked OK/FAIL.

**Style eval review phase**
- `OK: <project>` → `OK`
- `FAILED: <project> \((reason)\)` → `FAIL` (capture reason)
- `SKIP: <project> \((reason)\)` → `SKIP` (capture reason — most commonly `already reviewed`)
- `Launched: <project>` with no later result → `FAIL (no result; run ended mid-phase)`
- Project not listed in this phase → `—`
- If the phase header is absent (old log predating the review stage), every cell in the Review column is `—`. Add a Notes line saying so and do not treat it as failure.
- Cross-check the phase footer: `=== Done: A reviewed, B failed out of N ===` — A and B should equal the cells you marked OK/FAIL.

**Style fix phase** — for each project listed as `ELIGIBLE: <project>`, resolve the cell by walking these sources in order and stopping at the first hit:

1. **Log result line** in the fix-phase slice:
   - `OK: <project>` → `OK`
   - `ERROR: <project>` / `FAILED: <project>` / `TIMEOUT: <project>` / `WARN: <project>` → `FAIL` (capture reason)
2. **Worktree on disk** at `~/rust/<project>_style_fix/` (or `~/rust/<project>_style_fix/<subpath>/` for workspace members):
   - `EVALUATION.md` contains `^## Fix Summary` → `OK` (annotate commentary: "OK from disk; supervisor did not log result")
   - Worktree exists but no Fix Summary → `FAIL (work did not complete)`
3. **Nothing on disk, nothing in log**:
   - If the phase header appeared with no per-project work → `FAIL (script crashed before run)`
   - Otherwise → `FAIL (no result; cause unknown)`

`SKIP: <project> \((reason)\)` lines override everything → `SKIP`. Project not in the eligible list → `—`.

Crash detection (for commentary, not cell resolution): if the phase header appeared and no project produced any work, look for `WARNING: style-fix worktree script failed` or a bash syntax error like `unexpected EOF while looking for matching` and surface the exact line.

### 4. Build the table

Render a markdown table sorted alphabetically by project. Keep cells short — only `OK`, `FAIL`, `SKIP`, or `—`. Put reasons in commentary, not cells.

```
| Project | Clean | Warmup | Eval | Review | Fix |
|---|---|---|---|---|---|
| bevy_brp | OK | — | SKIP | — | SKIP |
| bevy_catenary | OK | — | OK | OK | FAIL |
| ... | ... | ... | ... | ... | ... |
```

### 5. Commentary

After the table, write these sub-sections (omit any that have nothing to report):

**Run window** — `start → end (elapsed)`. If still running or partial, say `start → in progress` or `start → ended (partial log; no run-complete marker)`.

**Phase summary** — one line per phase that ran:
- `Clean: P processed, S skipped`
- `Warmup: O ok, F fail, S skip` (with the names of any FAIL)
- `Eval: A/N evaluated` (matching the `=== Done:` footer)
- `Review: A reviewed, B failed of N` (matching the review-stage `=== Done:` footer; omit if the phase header is absent)
- `Fix: ran on E/E eligible` or `Fix: did not run — <reason>` if the script crashed
- For phases the log does not cover (e.g. clean/warmup/eval in a style-fix-manual log), write `<phase>: not in this log`.

**Failures** — bullet every FAIL cell with project, phase, and reason from the parenthetical.

**Skips by reason** — aggregate counts grouped by reason text, e.g.
- excluded: 8
- already at cap of 2 findings: 10
- another worktree checkout already exists: 2
- no Cargo.toml: 2

**Notes** — anything that does not fit above:
- Partial logs (which phases are absent).
- Script-level crashes with the exact error line.
- Mismatches between the footer counts and per-project markers.
- Anything the user should investigate.

## Notes

- The orchestrator log mixes timestamped lines (`YYYY-MM-DD HH:MM:SS PREFIX:`) and bare echoes from sub-scripts (`PREFIX:`). Match both — anchor regexes loosely on the prefix, not the start of line.
- This report is regenerated every call. Do not cache to disk; print directly.
- Do not invoke Monitor or stream the log — this is a snapshot, not a live view. For live updates use `/monitor_nightly`.
- Do not use markdown links — they do not render in the terminal.
- Manual style-fix runs should be launched via `~/.claude/scripts/nightly/style-fix-manual.sh` so their logs land in the accumulation directory.
