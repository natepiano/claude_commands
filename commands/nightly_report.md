---
description: Format and display the nightly Rust clean+rebuild job report with statistics
---

## Arguments
- `$ARGUMENTS` may contain `rebuild`
- If `$ARGUMENTS` contains `rebuild`, generate a fresh report from the log and save it to `/tmp/nightly-rust-report.txt`
- If `$ARGUMENTS` is empty or does not contain `rebuild`:
  1. Try to read `/tmp/nightly-rust-report.txt` and display its contents verbatim
  2. If the file does not exist, generate a fresh report (same as `rebuild`), save it to `/tmp/nightly-rust-report.txt`, display it, and tell the user: "Note: cached report was missing, so it was rebuilt from the log."

## Generating the report

Read the nightly build log at `~/.local/logs/nightly-rust-clean-build.log` and produce the following sections. If the log file is empty or missing, inform the user that no nightly run data is available and stop.

### 1. Header
- Show the date of the run (from the first log timestamp).
- Show total elapsed time (from the final summary line).

### 2. Project table
Display a markdown table with one row per project that appeared in the log. Columns:

| Project | Status | Clean | Build | Clippy |
|---------|--------|-------|-------|--------|

- **Status**: one of `OK`, `SKIPPED (reason)`, or `ERROR (stage)`
- **Clean / Build / Clippy**: show duration for each stage by computing the difference between consecutive log timestamps. Show `—` if the stage was skipped or not reached.

### 3. Summary statistics
- Repeat the run date and total elapsed time so the user doesn't have to scroll back up
- Total projects processed (non-skipped)
- Total projects skipped (with breakdown by reason: excluded, no Cargo.toml, not modified)
- Projects with errors (list them)
- Projects with clippy warnings (list them)
- Long builds: list any project whose build stage exceeded 5 minutes (project name + duration). If none, say "None".
- Longest clippy (project name + duration)

## Saving the report

After generating the report, write the full output to `/tmp/nightly-rust-report.txt`.

## Notes
- Timestamps in the log are formatted as `YYYY-MM-DD HH:MM:SS`.
- Do not use markdown links — they don't render in the terminal.
