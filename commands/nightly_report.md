---
description: Show a per-project nightly status table across clean / warmup / evaluation / style-fix phases, with commentary
---

## Arguments
- `$ARGUMENTS` — optional path to a log file. If empty, default to `~/.local/logs/nightly-rust-clean-build.log` (the orchestrator log written by `scripts/nightly/nightly-rust-clean-build.sh`).

If the file does not exist or is empty, tell the user `No nightly log at <path>` and stop.

## Goal

Summarize the most recent nightly run as a single matrix the user can scan in one glance:

- **Rows** = every project that appeared in any phase (union of the clean-phase iteration list, the warmup targets, the style-evaluation list, and the style-fix eligible list).
- **Columns** = **Clean**, **Warmup**, **Eval**, **Fix**.
- **Cells** = `OK`, `FAIL`, `SKIP`, or `—` when the phase did not apply.

Below the table, write a Commentary section that names every failure, every script-level crash, and aggregate skip counts. The user should be able to tell from this report alone *what ran, what did not, what failed, and why*.

## Steps

### 1. Read and bound the run

Read the log. Identify:

- **Start**: first line matching `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} === Starting nightly`.
- **End**: line matching `=== Nightly Rust clean \+ rebuild complete \((\d+m \d+s)\) ===`. If absent, the run is still in progress — say so in commentary and proceed with whatever data exists.

### 2. Slice the log into phases

Phase boundaries (in order):

1. **Clean+rebuild** — from start until the first `WARMUP` line or `=== Style evaluation` header, whichever comes first.
2. **Warmup** — all `WARMUP*` lines (these are scattered between clean and eval).
3. **Style evaluation** — between `=== Style evaluation: N projects ===` and `=== Done: A succeeded, B failed out of N ===`.
4. **Style fix** — between `=== Style-fix worktrees: N eligible projects ===` and end-of-run.

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

**Style fix phase**
- `ELIGIBLE: <project>` defines the eligible row set for this phase.
- Result lines: `OK: <project>` → `OK`; `ERROR:` / `FAILED:` / `TIMEOUT:` / `WARN:` → `FAIL` (capture reason).
- `SKIP: <project> \((reason)\)` → `SKIP`.
- **If no per-project result lines appear after the header** (e.g. the phase emitted only the section banner before the run ended), the script crashed or was killed. Mark every eligible project as `FAIL (script crashed)` and capture the crash detail for commentary. Crash signals:
  - A line like `WARNING: style-fix worktree script failed`
  - A bash error such as `unexpected EOF while looking for matching` shortly after the section header
- Project not in the eligible list → `—`

### 4. Build the table

Render a markdown table sorted alphabetically by project. Keep cells short — only `OK`, `FAIL`, `SKIP`, or `—`. Put reasons in commentary, not cells.

```
| Project | Clean | Warmup | Eval | Fix |
|---|---|---|---|---|
| bevy_brp | OK | — | SKIP | SKIP |
| bevy_catenary | OK | — | SKIP | FAIL |
| ... | ... | ... | ... | ... |
```

### 5. Commentary

After the table, write these sub-sections (omit any that have nothing to report):

**Run window** — `start → end (elapsed)`. If still running, say `start → in progress`.

**Phase summary** — one line per phase:
- `Clean: P processed, S skipped`
- `Warmup: O ok, F fail, S skip` (with the names of any FAIL)
- `Eval: A/N evaluated` (matching the `=== Done:` footer)
- `Fix: ran on E/E eligible` or `Fix: did not run — <reason>` if the script crashed

**Failures** — bullet every FAIL cell with project, phase, and reason from the parenthetical.

**Skips by reason** — aggregate counts grouped by reason text, e.g.
- excluded: 8
- already at cap of 2 findings: 10
- another worktree checkout already exists: 2
- no Cargo.toml: 2

**Notes** — anything that does not fit above:
- Script-level crashes with the exact error line.
- Phases that were skipped entirely.
- Mismatches between the footer counts and per-project markers.
- Anything the user should investigate.

## Notes
- The orchestrator log mixes timestamped lines (`YYYY-MM-DD HH:MM:SS PREFIX:`) and bare echoes from sub-scripts (`PREFIX:`). Match both — anchor regexes loosely on the prefix, not the start of line.
- This report is regenerated every call. Do not cache to disk; print directly.
- Do not invoke Monitor or stream the log — this is a snapshot, not a live view. For live updates use `/monitor_nightly`.
- Do not use markdown links — they do not render in the terminal.
