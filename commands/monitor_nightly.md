---
description: Attach a live Monitor to a running nightly clean/build/style-eval/style-fix log and report state transitions as they happen
---

# Monitor Nightly

Attach a persistent <MonitorTool/> to whichever nightly-related script the user just kicked off (clean+rebuild, style evaluation, style-fix worktrees, or the full nightly orchestrator) and surface meaningful state transitions in real time. Skip the high-volume noise (SKIP lines, raw cargo output) — only emit lines the user would act on.

## Arguments

`$ARGUMENTS` — optional path to a log file.

- If `$ARGUMENTS` is a path that exists, set `${LOG_PATH} = $ARGUMENTS` and proceed to <ArmMonitor/>.
- If `$ARGUMENTS` is empty, execute <DetectLog/>.
- If `$ARGUMENTS` is non-empty but the file does not exist, inform the user: `Log not found: $ARGUMENTS. Run /monitor_nightly with no argument to autodetect, or pass a valid path.` Then stop.

## <DetectLog/>

Inspect the four well-known log locations and pick the one most recently modified within the last 2 hours. Older candidates are stale — do not pick them.

```bash
ls -lt --time=mtime \
  /tmp/nightly-rust-clean-build-stdout.log \
  ~/.local/logs/nightly-rust-clean-build.log \
  /tmp/claude/style-fix-*.log \
  /tmp/claude/style-eval-*.log \
  /tmp/claude/nightly-*.log \
  2>/dev/null | head -5
```

`mtime` from `stat -f '%m' <file>` gives a unix timestamp; compare against `$(date +%s)` and require the difference under 7200 seconds.

Decision:
- **Exactly one fresh candidate** — set `${LOG_PATH}` to it and tell the user `Watching ${LOG_PATH} (last write Ns ago).` Proceed to <ArmMonitor/>.
- **Multiple fresh candidates** — list each with its mtime delta and ask the user to pick one by index. Wait for the answer; then set `${LOG_PATH}` and proceed.
- **No fresh candidates** — inform the user: `No nightly logs modified in the last 2 hours. Pass an explicit path: /monitor_nightly <path>.` Then stop.

## <ArmMonitor/>

Inspect the last ~30 lines of `${LOG_PATH}` to identify which phase is currently running. This shapes what the user will see next, so report it before arming:

```bash
tail -30 ${LOG_PATH}
```

Classify the current phase from the most recent matching keyword:

- `CLEAN:` / `BUILD:` / `MEND:` / `DONE:` → **clean+rebuild phase**
- `WARMUP` → **warmup phase**
- `=== Style evaluation` or `Launched: <project> via codex` → **style eval phase**
- `=== Style-fix worktrees` or `Launched: <project> (PID` → **style-fix phase**
- `=== Nightly Rust clean + rebuild complete` → **already finished** — tell the user the run is done, do not arm a monitor, stop.

Tell the user: `Detected phase: <phase>. Arming monitor on ${LOG_PATH}.`

Then call the Monitor tool with these exact parameters:

- `description`: `nightly: <phase>` (e.g. `nightly: style-fix worktrees`)
- `persistent`: `true`
- `timeout_ms`: `3600000`
- `command`:
  ```
  tail -F -n 0 ${LOG_PATH} 2>/dev/null | grep -E --line-buffered "<FILTER_REGEX>"
  ```

Where `<FILTER_REGEX>` is the single combined alternation defined in <FilterRegex/>. Always use the combined regex regardless of detected phase — the regex is cheap and the phase can transition mid-run (e.g. clean→build→eval→fix all in one orchestrator run).

After Monitor returns its task id, tell the user: `Monitor armed (task <id>). I will report state transitions as they arrive.` Then yield — do not poll, do not sleep, do not re-read the log.

## <FilterRegex/>

Single alternation covering every meaningful state-transition line across all phases. Lines matching this regex are the events the user wants to know about; everything else is noise.

```
^(CLEAN|BUILD|MEND|DONE|ERROR|WARNING|TIMEOUT|RETRY|RETRY OK|RETRY FAILED|FAILED|WARN|OK):|^WARMUP (OK|FAIL|SKIP):|^Launched: |^=== |^commit-style-results: |^Wrote /Users/natemccoy/rust/nate_style/style_report\.md$
```

Coverage rationale (do not narrow this without restoring all categories):

- Clean+rebuild: `CLEAN:`, `BUILD:`, `MEND:`, `DONE:`, `ERROR:`, `WARNING:`
- Warmup: `WARMUP OK`, `WARMUP FAIL`, `WARMUP SKIP`
- Style eval / fix: `OK:`, `WARN:`, `ERROR:`, `FAILED:`, `RETRY:`, `RETRY OK:`, `RETRY FAILED:`, `TIMEOUT:`
- Per-project launches across both eval and fix: `Launched:`
- Section markers and final summary: any `=== ` line (`=== Style evaluation`, `=== Style-fix worktrees`, `=== Done:`, `=== Nightly Rust clean + rebuild complete`)
- Commit-step status: `commit-style-results: ` (matches both `commit-style-results: skipped — unexpected changes` and any future success/error variants from `commit-style-results.sh`)
- Style report write: `Wrote /Users/natemccoy/rust/nate_style/style_report.md` is the marker that `style_report.py --generate` finished writing the Obsidian summary; precise anchoring keeps unrelated `Wrote ` lines out

`SKIP:` (project-skipped lines from the clean phase) is intentionally **not** matched — those are high-volume and low-signal. If the user later asks for full visibility, add `|^SKIP: ` to the alternation.

## <EventReporting/>

Every Monitor event arriving in chat is a single line from the log. For each:

- Strip the leading `^` matched class and report a one-line update naming the project and outcome. Examples:
  - `OK: bevy_catenary (worktree created, fixes applied)` → `bevy_catenary done (style-fix).`
  - `ERROR: hana (codex exited immediately with no output)` → `hana failed style-fix: codex exited immediately with no output.`
  - `=== Done: 5 created, 2 failed, 0 skipped out of 7 ===` → `Style-fix run complete: 5 ok, 2 failed.`
- Maintain a running tally of OK/FAILED counts across notifications when the user benefits from it (e.g. style-fix has a known 7-project denominator).
- Treat `ERROR:`, `FAILED:`, `TIMEOUT:`, and `=== Done:` with non-zero failures as user-actionable — call PushNotification for those. Routine `OK:` and `Launched:` lines do not need a push.
- When `=== Nightly Rust clean + rebuild complete` lands, or `=== Done:` for the standalone phase the user kicked off, tell the user the run is finished and stop the monitor with TaskStop using the task id you stored.

## Notes

- The orchestrator script `nightly-rust-clean-build.sh` writes both to `~/.local/logs/nightly-rust-clean-build.log` (via `tee`) and via the launchd plist to `/tmp/nightly-rust-clean-build-stdout.log`. Either is valid; <DetectLog/> will prefer whichever is freshest.
- Standalone runs of `style-eval-all.sh` or `style-fix-worktrees.sh` invoked interactively typically log to `/tmp/claude/<name>-<suffix>.log`. The detector pattern globs match those.
- The Monitor uses `tail -F -n 0` so we start at the current end of the file — backlog is not re-emitted. If the user wants a recap of what already ran, point them at <ArmMonitor/>'s `tail -30` output instead.
- `grep --line-buffered` is required — without it, pipe buffering delays events by minutes and the monitor looks broken.
