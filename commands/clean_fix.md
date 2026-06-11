---
description: Unified clean-fix command — run the pipeline (one project or all), monitor a live log, render a report, set the style mode, or manage skip lists
---

# Clean-fix

`$ARGUMENTS` — the first token selects a subcommand; the remaining tokens are that subcommand's arguments.

| Subcommand | Purpose |
|---|---|
| `run [clean \| style \| all \| project]` | Launch a pipeline scope (default all), or eval + fix for one project |
| `monitor [log-path] [project]` | Attach a live Monitor to a running clean-fix log |
| `report [list \| <path>]` | Render a per-project status table from a run log |
| `mode [eval \| fix] [off \| claude \| codex \| on \| inherit]` | Show or set the eval/fix style agent modes |
| `skip clean\|style [...]` | Skip or re-enable targets for the clean or style pass |

Dispatch: `run` → <Run/>, `monitor` → <Monitor/>, `report` → <Report/>, `mode` → <Mode/>, `skip` → <Skip/>. Empty or unrecognized first token → print the table above and stop.

<Run>

## run [clean | style | all | project]

### `run`, `run all`, `run clean`, `run style` — pipeline scopes

Launch `~/.claude/scripts/clean-fix/clean-fix.sh <scope>` interactively, off the launchd schedule. Scopes:

- `all` (or no token) — full pipeline: clean + build + warmup + eval + review + fix
- `clean` — clean + build + mend + warmup only (the nightly 4 AM job's scope)
- `style` — eval + review + fix worktrees only (the every-10-min job's scope)

**Hard requirement: must run unsandboxed.** The script invokes `codex` and `claude`, which need write access to `~/.codex/sessions` and to many paths outside the sandbox allowlist. Per `~/.claude/CLAUDE.md` ("codex and clean-fix style scripts must run unsandboxed"), **always** invoke this with `dangerouslyDisableSandbox: true` from the start. Do not try the sandboxed run first — it will fail.

**Step 1: Refuse to launch if a clean-fix is already running.** A second concurrent run will collide with the first one's worktrees and history files. Before launching, check:

```bash
pgrep -fl clean-fix.sh || true
```

If anything matches, tell the user `Clean-fix already running (PID …). Use /clean_fix monitor to attach to the live log, or wait for it to finish.` Stop. Do not launch a second copy.

**Step 2: Launch.** The orchestrator writes its own timestamped log under `~/.local/logs/clean-fix/clean-fix-YYYYMMDD-HHMMSS.log` and updates the `~/.local/logs/clean-fix.log` symlink to point at it. Don't pre-create or redirect — just launch:

```bash
~/.claude/scripts/clean-fix/clean-fix.sh <scope>
```

Use `Bash` with `dangerouslyDisableSandbox: true` and `run_in_background: true`. Capture the resulting bash shell id so the user can kill it later with `KillShell` if needed. After launch, resolve the active log path:

```bash
ls -t ~/.local/logs/clean-fix/clean-fix-*.log 2>/dev/null | head -1
```

Tell the user: `Clean-fix launched (shell <id>). Log: <path>.`

**Step 3: Offer to arm the monitor.** In the same response, offer one short follow-up: `Want me to /clean_fix monitor to stream phase transitions?` If the user says yes, execute <Monitor/> with the resolved log path. If they decline, stop.

### `run <project>` — single project

The orchestrator has no per-project filter, so a single-project run covers the style pipeline only (eval + review + fix) — not clean + build.

1. Resolve the project root: `~/rust/<project>` must contain a `Cargo.toml`. If it doesn't, report that and stop.
2. Invoke the `style_eval` skill with arguments `~/rust/<project> --fix`. It owns the evaluation, the fix worktree launch, the monitoring (via <Monitor/>), and the final summary.

### Notes

- The full run can take an hour or more. The user does not need to keep this conversation open — the script runs detached and writes to disk.
- `run` is the on-demand counterpart to the launchd job; the schedule is unaffected. If a launchd-triggered run is already in flight, Step 1 will catch it.
- For testing only the review stage in isolation, prefer `~/.claude/scripts/clean-fix/style-eval-review-all.sh [project]` — much faster than a full clean-fix.
- Use `/clean_fix report` after the run for a per-project matrix, or `/clean_fix monitor` during the run for live updates.

</Run>

<Monitor>

## monitor [log-path] [project]

Attach a persistent Monitor to whichever clean-fix-related script the user just kicked off (clean+rebuild, style evaluation, style-fix worktrees, or the full orchestrator) and surface meaningful state transitions in real time. Skip the high-volume noise (SKIP lines, raw cargo output) — only emit lines the user would act on.

### Arguments

Remaining tokens: optional `<log-path> [<project>]`. The project name is only used for style-fix manual logs (see <ArmMonitor/>).

- If the first token is a path that exists, set `${LOG_PATH}` to it (and `${PROJECT}` to the second token if present) and proceed to <ArmMonitor/>.
- If no tokens, execute <DetectLog/>.
- If the first token is non-empty but the file does not exist, inform the user: `Log not found: <token>. Run /clean_fix monitor with no argument to autodetect, or pass a valid path.` Then stop.

### <DetectLog/>

Inspect the well-known log locations and pick the one most recently modified within the last 2 hours. Older candidates are stale — do not pick them.

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
- **Exactly one fresh candidate** — set `${LOG_PATH}` to it and tell the user `Watching ${LOG_PATH} (last write Ns ago).` Proceed to <ArmMonitor/>.
- **Multiple fresh candidates** — list each with its mtime delta and ask the user to pick one by index. Wait for the answer; then set `${LOG_PATH}` and proceed.
- **No fresh candidates** — inform the user: `No clean-fix logs modified in the last 2 hours. Pass an explicit path: /clean_fix monitor <path>.` Then stop.

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

`${PROJECT}` comes from the arguments; if absent, derive it from the log (first `Launched:`/worktree line names the project) or ask the user. The helper tails both the manual log and the agent's own log (`/private/tmp/claude/style_fix_<project>.log`), translates the agent's phase sentinels to `phase=agent-step name=<...>`, and exits 0 on the `phase=launcher-exit` sentinel — no TaskStop needed. (A tail+grep pipeline cannot terminate itself at launcher-exit inside the sandbox: `pkill` is denied access to macOS's process-list service and shell job-control is unavailable.) Then report events per <StyleFixManualEvents/> and skip the rest of this section.

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
- `phase=already-applied …` → `already-applied (retry detected finished worktree)`
- `phase=launcher-exit code=N` → `launcher exited code=N` (also the Monitor's last event; the helper exits here)
- cargo/clippy/test/error/warning lines → echo verbatim; they're already short
- Skip `worktree-create` if immediately followed by `worktree-ready` to keep chatter low.

### Monitor notes

- The orchestrator script `clean-fix.sh` writes both to `~/.local/logs/clean-fix.log` (via `tee`) and via the launchd plists to `/tmp/style-fix-stdout.log` (`com.natemccoy.style-fix`, every 10 min) or `/tmp/cargo-clean-stdout.log` (`com.natemccoy.cargo-clean`, nightly 4 AM). Any is valid; <DetectLog/> will prefer whichever is freshest.
- Standalone runs of `style-eval-all.sh` or `style-fix-worktrees.sh` invoked interactively typically log to `/tmp/claude/<name>-<suffix>.log`. The detector pattern globs match those.
- The Monitor uses `tail -F -n 0` so we start at the current end of the file — backlog is not re-emitted. If the user wants a recap of what already ran, point them at <ArmMonitor/>'s `tail -30` output instead.
- `grep --line-buffered` is required — without it, pipe buffering delays events by minutes and the monitor looks broken.

</Monitor>

<Report>

## report [list | <path>]

Read `~/.claude/scripts/clean-fix/report-render.md` and follow it, substituting the remaining tokens (after `report`) for its `$ARGUMENTS`. That document owns the parser invocation, the output format, and every rendering rule; it is shared with `clean-fix.sh`, which pipes it into a headless claude after each scheduled run. Do not duplicate rendering logic here — if something is missing, fix `report-render.md` (or the parser).

</Report>

<Mode>

## mode [eval | fix] [off | claude | codex | on | inherit]

Show or set the clean-fix style agent modes.

The config file is `~/.claude/scripts/clean-fix/clean-fix.conf`. Two agents are configurable:

- **eval** — the `mode=` line under `[style_eval]`. Drives the eval and review stages. `mode=off` is the master off switch for the whole style pipeline (including fix).
- **fix** — the `mode=` line under `[style_fix]`. Drives the fix-worktree agents. When absent or commented out, the fix stage inherits the eval mode.

Argument handling:

1. Read `~/.claude/scripts/clean-fix/clean-fix.conf`.
2. **No tokens** — show both: the eval mode, the fix mode (or "inherits eval → <mode>"), and each section's `model=` if set. Stop.
3. **First token is `eval` or `fix`** — the scope; the next token is the value:
   - **`claude`** / **`codex`**: set that scope's `mode=` line.
   - **`off`**: eval scope only — set `mode=off` (master switch). For the fix scope, explain that off is controlled by the eval mode and stop.
   - **`on`**: set `mode=claude`.
   - **`inherit`**: fix scope only — comment out the `[style_fix]` `mode=` line so fix inherits eval.
   - **empty**: show that scope's current mode.
4. **First token is a bare value** (`off`/`claude`/`codex`/`on`) — one agent everywhere: apply it to the **eval** scope as in step 3 AND reset the fix scope to inherit (comment out the `[style_fix]` `mode=` line).
5. When changing a value, use the Edit tool on that `mode=` line in-place (uncomment `#mode=` under `[style_fix]` when setting the fix scope). Only touch mode lines — the `[build]` and `[targets]` skip lists are managed by `phase_skip.py` (see <Skip/>), never by direct edits.

</Mode>

<Skip>

## skip clean|style [...]

Skip or re-enable targets in `~/.claude/scripts/clean-fix/clean-fix.conf` by commenting/uncommenting allowlist lines.

The second token is the scope — required:

- **`clean`** — the `[build]` allowlist (clean + build pass). A directory skipped from clean has no effect on style.
- **`style`** — the `[targets]` allowlist (style eval, review, and fix passes). A target is either a whole directory or a workspace member (`<dir>/<subpath>`); a member is named by its last path segment (e.g. `bevy_diegetic`). A style skip leaves clean untouched.

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

The helper is the single source of truth for the skip lists — do not edit those entries with Edit/Write. (The `[style_eval]` mode line is the one conf setting edited directly — see <Mode/>.)

A commented allowlist line is invisible to the conf parser, so the entry drops out of its pass. The helper tags its edits with `#CLEAN_FIX_SKIP#` so `enable-all` only reverses temp skips and never touches plain doc comments. It exits non-zero on a name with no matching allowlist entry.

</Skip>
