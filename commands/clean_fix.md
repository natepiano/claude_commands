---
description: Unified clean-fix command — run the pipeline (one project or all), monitor a live log, render a report, configure style agents, or manage skip lists
---

# Clean-fix

`$ARGUMENTS` — the first token selects a subcommand; the remaining tokens are that subcommand's arguments.

When no subcommand is provided, run this script and relay its stdout exactly:

```bash
~/.claude/scripts/clean-fix/clean-fix-usage.sh
```

The script owns the user-facing data, section order, column widths, wrapping, and formatting. Do not parse, summarize, truncate, filter, sort, merge, rename, rewrite, add rows, or convert its output to Markdown pipe tables. Do not read or reinterpret clean-fix config files for this screen; the script output is the only source. If the script exits non-zero, show its stdout/stderr exactly and stop.

Dispatch: `run` → <Run/>, `monitor` → <Monitor/>, `report`/`list` → <Report/>, `eval`/`review`/`fix`/`agent`/`on`/`off` → <StyleAgentConfig/>, `skip` → <Skip/>. Empty or unrecognized first token → run the usage script above, relay stdout exactly, and stop.

<Run>

## run [clean | style] [project]
## run [project]

### `run`, `run clean`, `run style` — pipeline scopes

Launch `~/.claude/scripts/clean-fix/clean-fix.sh` interactively, off the launchd schedule. Build the script arguments from the user command:

- no scope — full pipeline: clean + build + warmup + eval + review + fix
- `clean` — clean + build + mend + warmup only (the nightly 4 AM job's scope)
- `style` — eval + review + fix worktrees only (the every-10-min job's scope)

Any scope can take an optional project name:

- `clean_fix run` — full pipeline for all targets
- `clean_fix run <project>` — full pipeline for one target
- `clean_fix run clean` — clean/build/warmup for all clean targets
- `clean_fix run clean <project>` — clean/build/warmup for one clean target
- `clean_fix run style` — style eval/review/fix for all style targets
- `clean_fix run style <project>` — style eval/review/fix for one style target

`<project>` may be either the active checkout name shown in the usage table's `Project` column or the preserved identity shown in `Project Key`. The scripts normalize both through `[active_checkout]`; do not create duplicate style entries for active worktrees.

**Hard requirement: must run unsandboxed.** The script invokes `codex` and `claude`, which need write access to `~/.codex/sessions` and to many paths outside the sandbox allowlist. Per `~/.claude/CLAUDE.md` ("codex and clean-fix style scripts must run unsandboxed"), **always** invoke this with `dangerouslyDisableSandbox: true` from the start. Do not try the sandboxed run first — it will fail.

**Step 1: Refuse to launch if a clean-fix is already running.** A second concurrent run will collide with the first one's worktrees and history files. Before launching, check:

```bash
pgrep -fl clean-fix.sh || true
```

If anything matches, tell the user `Clean-fix already running (PID …). Use /clean_fix monitor to attach to the live log, or wait for it to finish.` Stop. Do not launch a second copy.

**Step 2: Launch.** The orchestrator writes its own timestamped log under `~/.local/logs/clean-fix/clean-fix-YYYYMMDD-HHMMSS.log` and updates the `~/.local/logs/clean-fix.log` symlink to point at it. Don't pre-create or redirect — just launch:

```bash
~/.claude/scripts/clean-fix/clean-fix.sh [clean|style] [project]
```

Use `Bash` with `dangerouslyDisableSandbox: true` and `run_in_background: true`. Capture the resulting bash shell id so the user can kill it later with `KillShell` if needed. After launch, resolve the active log path:

```bash
ls -t ~/.local/logs/clean-fix/clean-fix-*.log 2>/dev/null | head -1
```

Tell the user: `Clean-fix launched (shell <id>). Log: <path>.`

**Step 3: Offer to arm the monitor.** In the same response, offer one short follow-up: `Want me to /clean_fix monitor to stream phase transitions?` If the user says yes, execute <Monitor/> with no arguments. If they decline, stop.

### Notes

- The full run can take an hour or more. The user does not need to keep this conversation open — the script runs detached and writes to disk.
- `run` is the on-demand counterpart to the launchd job; the schedule is unaffected. If a launchd-triggered run is already in flight, Step 1 will catch it.
- For testing only the review stage in isolation, prefer `~/.claude/scripts/clean-fix/style-eval-review-all.sh [project]` — much faster than a full clean-fix.
- Use `/clean_fix report` after the run for a per-project matrix, or `/clean_fix monitor` during the run for live updates.

</Run>

<Monitor>

## monitor

Attach a persistent Monitor to whichever clean-fix-related script the user just kicked off (clean+rebuild, style evaluation, style-fix worktrees, or the full orchestrator) and surface meaningful state transitions in real time. Skip the high-volume noise (SKIP lines, raw cargo output) — only emit lines the user would act on.

`monitor` takes no arguments. If the user passes any token after `monitor`, ignore the token and run the normal detection path.

### <DetectLog/>

Inspect the well-known log locations and pick the single most recently modified log within the last 2 hours. Older candidates are stale — do not pick them.

```bash
ls -lt --time=mtime \
  /tmp/style-fix-stdout.log \
  /tmp/cargo-clean-stdout.log \
  ~/.local/logs/clean-fix.log \
  ~/.local/logs/clean-fix/style-fix-manual-*.log \
  /tmp/claude/style-fix-*.log \
  /tmp/claude/style-eval-*.log \
  /tmp/claude/clean-fix-*.log \
  2>/dev/null | head -5
```

`mtime` from `stat -f '%m' <file>` gives a unix timestamp; compare against `$(date +%s)` and require the difference under 7200 seconds.

Decision:
- **Fresh candidate found** — set `${LOG_PATH}` to the newest fresh candidate and tell the user `Watching ${LOG_PATH} (last write Ns ago).` Proceed to <ArmMonitor/>.
- **No fresh candidates** — inform the user: `No clean-fix logs modified in the last 2 hours. Start a run first, then use /clean_fix monitor.` Then stop.

### <ArmMonitor/>

Identify the current phase by calling the parser:

```bash
python3 ~/.claude/scripts/clean-fix/clean_fix_report_parse.py --phase-detect ${LOG_PATH}
```

Output is two lines:

```
PHASE <name>          # one of: clean+rebuild, warmup, style-eval, style-eval-review, style-fix, done, unknown
LATEST_EVENT "<line>"
```

If `PHASE done`, tell the user the run is finished and stop — do not arm a monitor.

Otherwise tell the user: `Detected phase: <name>. Arming monitor on ${LOG_PATH}.`

**Style-fix manual logs** — if `${LOG_PATH}` matches `style-fix-manual-*.log`, do NOT use the tail+grep pipeline below. Arm the Monitor with the sandbox-safe Python helper instead:

- `description`: `clean-fix: style-fix (manual)`
- `persistent`: `true`
- `timeout_ms`: `3600000`
- `command`: `python3 ~/.claude/scripts/clean-fix/style-fix-monitor.py ${PROJECT}`

Derive `${PROJECT}` from the log before arming the helper. Use the first `Launched:` or worktree line that names the project. If the project cannot be derived, tell the user `Could not identify the project from ${LOG_PATH}; use tail -f ${LOG_PATH}.` Then stop. The helper tails both the manual log and the agent's own log (`/private/tmp/claude/style_fix_<project>.log`), translates the agent's phase sentinels to `phase=agent-step name=<...>`, and exits 0 on the `phase=launcher-exit` sentinel — no TaskStop needed. (A tail+grep pipeline cannot terminate itself at launcher-exit inside the sandbox: `pkill` is denied access to macOS's process-list service and shell job-control is unavailable.) Then report events per <StyleFixManualEvents/> and skip the rest of this section.

For all other logs, fetch the live-monitor filter regex from the parser (single source of truth — keeps phase classification and live filtering in lockstep):

```bash
FILTER_REGEX=$(python3 ~/.claude/scripts/clean-fix/clean_fix_report_parse.py --filter-regex)
```

Call the Monitor tool with these exact parameters:

- `description`: `clean-fix: <phase>` (e.g. `clean-fix: style-fix`)
- `persistent`: `true`
- `timeout_ms`: `3600000`
- `command`:
  ```
  tail -F -n 0 ${LOG_PATH} 2>/dev/null | grep -E --line-buffered "${FILTER_REGEX}"
  ```

Always use the parser's regex regardless of detected phase — the regex is cheap and phases transition mid-run (clean → eval → fix all in one orchestrator run).

After Monitor returns its task id, tell the user: `Monitor armed (task <id>). I will report state transitions as they arrive.` Then yield — do not poll, do not sleep, do not re-read the log.

### <EventReporting/>

Every Monitor event arriving in chat is a single line from the log. For each:

- Strip the leading `^` matched class and report a one-line update naming the project and outcome. Examples:
  - `OK: bevy_catenary (worktree created, fixes applied)` → `bevy_catenary done (style-fix).`
  - `ERROR: hana (codex exited immediately with no output)` → `hana failed style-fix: codex exited immediately with no output.`
  - `=== Done: 5 created, 2 failed, 0 skipped out of 7 ===` → `Style-fix run complete: 5 ok, 2 failed.`
- Maintain a running tally of OK/FAILED counts across notifications when the user benefits from it (e.g. style-fix has a known 7-project denominator).
- Treat `ERROR:`, `FAILED:`, `TIMEOUT:`, and `=== Done:` with non-zero failures as user-actionable — call PushNotification for those. Routine `OK:` and `Launched:` lines do not need a push.
- When `=== Clean-fix Rust clean + rebuild complete` lands, or `=== Done:` for the standalone phase the user kicked off, tell the user the run is finished and stop the monitor with TaskStop using the task id you stored.

### <StyleFixManualEvents/>

For Monitors armed with `style-fix-monitor.py`, emit one short line per event:

- `[progress …] phase=worktree-ready` → `worktree ready`
- `phase=agent-launch …` → `agent launched (codex pid <pid>)`
- `phase=agent-running elapsed=Ns` → `agent running Ns` (skip if Fix Summary is already detected)
- `phase=agent-step name=<name>` → `agent step: <name>` (the phase sentinels the agent prints — `read-evaluation`, `apply-finding`, `cargo-mend-preview`, `clippy-preview`, `tests`, `fmt`, `write-fix-summary`, etc.)
- `phase=agent-fix-summary-detected …` → `Fix Summary detected`
- `phase=agent-exit code=N` → `agent exited code=N`
- `phase=verify-start` → `verify pass starting (same agent re-checks the fix)`
- `phase=verify-launch …` → `verify agent launched (pid <pid>)`
- `phase=verify-running elapsed=Ns` → `verify running Ns`
- `phase=agent-step name=verify-…` → `verify step: <name>` (verify's own sentinels — `verify-read-evaluation`, `verify-inspect-diff`, `verify-finding`, `verify-clippy`, `verify-tests`, `verify-fmt`, `write-verification`)
- `phase=verify-summary-detected …` → `Fix Verification detected`
- `phase=verify-exit code=N` → `verify agent exited code=N`
- `phase=verify-done …` → `verify pass complete`
- `phase=verify-incomplete reason=<r>` → `verify pass incomplete (<r>) — applied fix kept for review`
- `phase=already-applied …` → `already-applied (retry detected finished worktree)`
- `phase=launcher-exit code=N` → `launcher exited code=N` (also the Monitor's last event; the helper exits here)
- cargo/clippy/test/error/warning lines → echo verbatim; they're already short
- Skip `worktree-create` if immediately followed by `worktree-ready` to keep chatter low.

### Monitor notes

- The orchestrator script `clean-fix.sh` writes both to `~/.local/logs/clean-fix.log` (via `tee`) and via the launchd plists to `/tmp/style-fix-stdout.log` (`com.natemccoy.style-fix`, every 10 min) or `/tmp/cargo-clean-stdout.log` (`com.natemccoy.cargo-clean`, nightly 4 AM). Any is valid; <DetectLog/> will prefer whichever is freshest.
- Standalone runs of `style-eval-all.sh` or `style-fix-worktrees.sh` invoked interactively typically log to `/tmp/claude/<name>-<suffix>.log`. The detector pattern globs match those.
- The Monitor uses `tail -F -n 0` so we start at the current end of the file — backlog is not re-emitted.
- `grep --line-buffered` is required — without it, pipe buffering delays events by minutes and the monitor looks broken.

</Monitor>

<Report>

## report [list | <path>]
## list

Read `~/.claude/scripts/clean-fix/report-render.md` and follow it, substituting the remaining tokens (after `report`) for its `$ARGUMENTS`. If the top-level token was `list`, execute this section with `$ARGUMENTS` set to `list`. That document owns the parser invocation, the output format, and every rendering rule; it is shared with `clean-fix.sh`, which pipes it into a headless claude after each scheduled run. Do not duplicate rendering logic here — if something is missing, fix `report-render.md` (or the parser).

</Report>

<StyleAgentConfig>

## eval|review|fix [model <id>|default | effort <id>|default | on|off]
## agent claude|codex
## agent eval|review|fix claude|codex
## on|off

Show or set the clean-fix style agent configuration. These commands do not run the eval, review, or fix phase for a project. Project-scoped style execution is `clean_fix run <project>`.

The clean-fix stage assignment file is `~/.claude/scripts/clean-fix/agent-assignments.conf`. The global agent registry is `~/.claude/config/agents.conf`; it contains each agent's default `model=`/`effort=` plus `[<agent>.models]` and `[<agent>.efforts]` allowlists. When a clean-fix stage leaves `model=`/`effort=` empty, the values resolve from the global `[<agent>]` section. An explicit `model=` or `effort=` in `agent-assignments.conf` overrides for that clean-fix stage only and must pass the global allowlist.

Three clean-fix sections are configurable:

- **eval** — `[style_eval] enabled=`, `agent=`, optional `model=`, and optional `effort=`.
- **review** — `[style_eval_review] enabled=`, `agent=`, optional `model=`, and optional `effort=`.
- **fix** — `[style_fix] enabled=`, `agent=`, optional `model=`, and optional `effort=`.

`mode=` is not valid anymore. The scripts reject any `mode=` key in the clean-fix stage assignment sections. `clean-fix.conf` owns pipeline targets and tunables only; agent settings placed there are rejected as stale.

Argument handling:

1. Read `~/.claude/scripts/clean-fix/agent-assignments.conf`.
2. Read `~/.claude/config/agents.conf`.
3. **No tokens** — show all three sections: `enabled`, `agent`, resolved `model`, resolved `effort`, assignment path, and registry path. Stop.
4. **First token is `eval`, `review`, or `fix`** — the scope. Map `eval` to `[style_eval]`, `review` to `[style_eval_review]`, and `fix` to `[style_fix]`.
5. **Scoped status:** `/clean_fix eval`, `/clean_fix review`, or `/clean_fix fix` shows only that scope's current `enabled`, `agent`, resolved `model`, and resolved `effort`.
6. **Project names are invalid here:** `/clean_fix eval <project>`, `/clean_fix review <project>`, and `/clean_fix fix <project>` are not supported. Tell the user to use `/clean_fix run <project>` for a single project's style eval/review/fix flow.
7. **Scoped enable/disable:** `/clean_fix eval on`, `/clean_fix review off`, `/clean_fix fix on`, etc. set that scope's `enabled=` to `true` or `false`.
8. **Global enable/disable:** `/clean_fix on` and `/clean_fix off` set all three `enabled=` values to `true` or `false`.
9. **Scoped agent:** `/clean_fix agent eval claude` or `/clean_fix agent fix codex` sets only that scope's `agent=`. The old `/clean_fix <scope> agent claude|codex` form is also accepted, but do not show it in usage. If that scope has a non-empty `model=` or `effort=` not allowed for the new agent in `config/agents.conf`, also set the invalid value to empty (`<default>`) and report that reset.
10. **Global agent:** `/clean_fix agent claude` or `/clean_fix agent codex` sets all three scope `agent=` values and resets any now-invalid non-empty `model=` or `effort=` values to empty.
11. **Scoped model:** `/clean_fix eval model opus`, `/clean_fix review model sonnet`, or `/clean_fix fix model gpt-5.4-mini` first reads the selected scope's `agent=`. The model must exactly match a non-comment line under `[<agent>.models]` in `config/agents.conf`; otherwise stop and show the allowed values.
12. **Scoped effort:** `/clean_fix eval effort max` or `/clean_fix fix effort xhigh` first reads the selected scope's `agent=`. The effort must exactly match a non-comment line under `[<agent>.efforts]` in `config/agents.conf`; otherwise stop and show the allowed values.
13. **Default model/effort:** `/clean_fix <scope> model default` sets that scope's `model=` to empty. `/clean_fix <scope> effort default` sets that scope's `effort=` to empty. Empty values resolve through the global `[<agent>]` defaults; if those are empty too, the agent CLI default applies.
14. When changing a value, edit only the relevant `enabled=`, `agent=`, `model=`, or `effort=` line in `agent-assignments.conf`. The `[build]` and `[projects]` skip lists remain in `clean-fix.conf` and are managed by `phase_skip.py` (see <Skip/>), never by direct edits.

</StyleAgentConfig>

<Skip>

## skip clean|style [...]

Skip or re-enable targets in `~/.claude/scripts/clean-fix/clean-fix.conf` by commenting/uncommenting allowlist lines.

The second token is the scope — required:

- **`clean`** — the `[build]` allowlist (clean + build pass). A clean target may be a whole directory or a workspace member (`<dir>/<subpath>`); a member is named by its last path segment (e.g. `bevy_diegetic`). A clean skip leaves style untouched.
- **`style`** — the `[projects]` allowlist (style eval, review, and fix passes). A style target follows the same naming rule. A style skip leaves clean untouched.

For redirected projects, the helper accepts either the active checkout name or the project key and applies the skip to the underlying allowlist entry.

If the scope is missing or anything else, explain the two scopes and stop.

Map the remaining tokens to an action:

- empty → `status` (show which entries are currently skipped)
- first token is `enable-all` or `reset` → `enable-all`
- first token is `enable` → `enable` with the remaining tokens
- anything else → `skip` with all tokens

Run the helper and relay its output verbatim:

```bash
python3 ~/.claude/scripts/clean-fix/phase_skip.py <scope> <action> [target ...]
```

The helper is the single source of truth for the skip lists — do not edit those entries with Edit/Write. Agent settings are edited directly — see <StyleAgentConfig/>.

A commented allowlist line is invisible to the conf parser, so the entry drops out of its pass. The helper tags its edits with `#CLEAN_FIX_SKIP#` so `enable-all` only reverses temp skips and never touches plain doc comments. It exits non-zero on a name with no matching allowlist entry.

</Skip>
