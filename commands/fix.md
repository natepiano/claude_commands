---
description: Unified /fix command — run the pipeline (one project or all), add or rename projects, monitor a live log, render a report, configure style agents, or manage the skip list
---

# Fix

`$ARGUMENTS` — the first token selects a subcommand; the remaining tokens are that subcommand's arguments.

When no subcommand is provided, run this script and relay its stdout exactly:

```bash
~/.claude/scripts/fix/fix-usage.sh
```

The script owns the user-facing data, section order, column widths, wrapping, and formatting. Do not parse, summarize, truncate, filter, sort, merge, rename, rewrite, add rows, or convert its output to Markdown pipe tables. Do not read or reinterpret fix config files for this screen; the script output is the only source. If the script exits non-zero, show its stdout/stderr exactly and stop.

Dispatch: `run` → <Run/>, `add` → <Add/>, `rename` → <Rename/>, `monitor` → <Monitor/>, `report`/`list` → <Report/>, `eval`/`review`/`fix`/`agent`/`on`/`off` → <StyleAgentConfig/>, `skip` → <Skip/>. Empty or unrecognized first token → run the usage script above, relay stdout exactly, and stop.

<Run>

## run [project]

### `run` — style pipeline execution

Launch `~/.claude/scripts/fix/fix.sh` interactively, off the timer's schedule.

An interactive run **always runs all three stages**. The `enabled=` switches in `agent-assignments.conf` are scheduled-run policy: only the scheduled job consults them (`style-fix.timer` on Linux, the `org.nixos.style-fix` launchd agent on macOS), because only it sets `FIX_SCHEDULED=1`. Nothing is written to that file, so the schedule's settings survive untouched.

- `/fix run` — style eval/review/fix for all targets
- `/fix run <project>` — style eval/review/fix for one target

Normal per-project safety and eligibility skips still apply. A named project additionally overrides a temporary `/fix skip` — naming it is the intent — and a name matching no `[projects]` entry exits with an error rather than an empty run.

`<project>` may be either the active checkout name shown in the usage table's `Project` column or the preserved identity shown in `Project Key`. The scripts normalize both through `[active_checkout]`; do not create duplicate style entries for active worktrees.

**Always invoke this with `dangerouslyDisableSandbox: true` from the start.** The script launches `codex` and `claude`, which write to `~/.codex/sessions` and to many paths outside this tree.

**Step 1: Refuse to launch if the fix pipeline is already running.** A second concurrent run will collide with the first one's worktrees and history files. Before launching, check:

```bash
pgrep -fl fix.sh || true
```

If anything matches, tell the user `Fix pipeline already running (PID …). Use /fix monitor to attach to the live log, or wait for it to finish.` Stop. Do not launch a second copy.

**Step 2: Show the execution summary.** Source `~/.claude/scripts/fix/agent_assignments.sh`, resolve `style_eval`, `style_eval_review`, and `style_fix` with `cf_load_stage_assignment`, and show:

`One eval → eval_review → fix pass across <all configured style projects | project <name>>. Stage enablement applies to scheduled runs only.`

Then render the current assignments as a Markdown table with exactly these columns and rows:

| Stage | Agent:effort |
|---|---|
| eval | `<resolved agent>:<resolved effort>` |
| eval_review | `<resolved agent>:<resolved effort>` |
| fix | `<resolved agent>:<resolved effort>` |

Use `<default>` when agent or effort is empty. This summary is informational; do not edit either assignment file. `fix.sh` logs the same summary into the run log.

**Step 3: Launch.** The orchestrator writes its own timestamped log under `~/.local/logs/fix/fix-YYYYMMDD-HHMMSS.log` and updates the `~/.local/logs/fix.log` symlink to point at it. Don't pre-create or redirect — just launch:

```bash
~/.claude/scripts/fix/fix.sh [project]
```

Do **not** set `FIX_SCHEDULED` — that marker belongs to `fix-trigger.sh` and would make the run honor the stage switches.

Use `Bash` with `dangerouslyDisableSandbox: true` and `run_in_background: true`. Capture the resulting bash shell id so the user can kill it later with `KillShell` if needed. After launch, resolve the active log path:

```bash
ls -t ~/.local/logs/fix/fix-*.log 2>/dev/null | head -1
```

Tell the user: `Fix pipeline launched (shell <id>). Log: <path>.`

**Step 4: Arm the monitor. Automatically, without asking.** Execute <Monitor/> with no arguments as part of this same turn. Do not offer it, do not ask permission, do not wait for the user to request it — an interactive run is watched by definition, because the only reason to launch one from here rather than let the timer do it is to see it happen.

This step is the interactive path only. The scheduled job never reaches it: `style-fix.timer` and the `org.nixos.style-fix` launchd agent invoke `fix.sh` directly through `fix-trigger.sh`, with no agent and no conversation to report into. `FIX_SCHEDULED=1` marks that path. Nothing here should arm a monitor when that marker is set.

### Notes

- The full run can take an hour or more. The user does not need to keep this conversation open — the script runs detached and writes to disk.
- `run` is the on-demand counterpart to the scheduled job; the schedule is unaffected. If a timer-triggered run is already in flight, Step 1 will catch it.
- For testing only the review stage in isolation, prefer `~/.claude/scripts/fix/style-eval-review-all.sh [project]` — much faster than a full fix run. Called directly like this it also ignores stage enablement, for the same reason `/fix run` does.
- Use `/fix report` after the run for a per-project matrix. Live updates need no command — Step 4 already armed the monitor.

</Run>

<Add>

## add <path-or-project>

Add a Rust project to `~/.claude/scripts/fix/fix.conf`.

`<path-or-project>` may be a project directory name under `~/rust`, a path
relative to `~/rust`, an absolute path under `~/rust`, or a `Cargo.toml` path.
The target must exist and contain `Cargo.toml`.

Run the helper and relay its output verbatim:

```bash
python3 ~/.claude/scripts/fix/project_add.py <path-or-project>
```

The helper adds the normalized entry to `[projects]`. For a
workspace member, it writes the workspace-relative entry
`<workspace-dir>/<member-subpath>`, so the project/history key remains the
member directory name, matching existing fix workspace entries.

The helper refuses duplicate `[projects]` identity keys and refuses to reactivate
temporarily skipped entries; use `/fix skip enable <target>` for
that case.

</Add>

<Rename>

## rename <old> <new>

Rename a fix project identity and migrate its existing fix state.

`<old>` may be the current project key or `[projects]` entry. `<new>` may be a
project directory name under `~/rust`, a path relative to `~/rust`, an absolute
path under `~/rust`, or a `Cargo.toml` path. The new target must exist and
contain `Cargo.toml`.

Run the helper and relay its output verbatim:

```bash
python3 ~/.claude/scripts/fix/project_rename.py <old> <new>
```

The helper updates `[projects]`, `[active_checkout]`, and keyed
fix config entries, then migrates:

- `~/rust/nate_style/.history/<old-key>.jsonl`
- `~/rust/nate_style/.history/.pending/<old-key>.json`
- `~/rust/nate_style/.history/.pending/<old-key>.json.lock`
- matching `~/rust/nate_style/.history/.failures/*_<old-key>.md`
- `.fix-project` markers in existing `_style_fix` worktrees

It refuses collisions with an existing new key; it does not merge histories.

</Rename>

<Monitor>

## monitor

Attach a persistent Monitor to whichever fix-related script the user just kicked off (style evaluation, style-fix worktrees, or the full orchestrator) and surface meaningful state transitions in real time. Skip the high-volume noise (SKIP lines, raw cargo output) — only emit lines the user would act on.

**This section is normally reached from <Run/> Step 4, not typed.** An interactive `/fix run` arms it automatically; the user should never have to ask for it and should never be offered it. Typing `/fix monitor` remains valid for the one case Step 4 cannot cover: attaching to a run this conversation did not start — a scheduled run already in flight, or an interactive one from an earlier session.

`monitor` takes no arguments. If the user passes any token after `monitor`, ignore the token and run the normal detection path.

### <DetectLog/>

Inspect the well-known log locations and pick the single most recently modified log within the last 2 hours. Older candidates are stale — do not pick them.

```bash
bash -c '
now=$(date +%s)
for f in \
  ~/.local/logs/fix.log \
  ~/Library/Logs/nate-jobs/style-fix.log \
  ~/.local/logs/fix/style-fix-manual-*.log \
  /tmp/claude/style-fix-*.log \
  /tmp/claude/style-eval-*.log \
  /tmp/claude/clean-fix-*.log \
  /tmp/claude/fix-*.log
do
  [ -f "$f" ] || continue
  m=$(stat -Lc %Y "$f" 2>/dev/null || stat -Lf %m "$f" 2>/dev/null) || continue
  echo "$((now - m))	$f"
done | sort -n | head -5
'
```

Each line is `<age-in-seconds><TAB><path>`, freshest first. `stat -Lc %Y` is the GNU spelling and `stat -Lf %m` the BSD one, so this runs unchanged on both machines; `-L` matters because `~/.local/logs/fix.log` is a symlink to the newest run log and the link's own mtime stops moving once the run starts. A candidate is fresh when its age is under 7200 seconds.

The `bash -c` wrapper is required, not stylistic. Both machines default to zsh, and the Bash tool runs the command under that shell. When a glob in the `for` list matches nothing — `style-fix-manual-*.log` whenever no manual run has happened — zsh treats it as an error, prints `no matches found: …` and never enters the loop, so detection returns nothing and the monitor looks like it found no logs. Bash leaves an unmatched pattern as a literal word, which the `[ -f "$f" ]` guard then skips, which is the behavior this loop is written for. Measured 2026-09-10: the unwrapped form failed here on the first glob and reported no candidates while a run was actively writing `~/.local/logs/fix.log`. Do not "simplify" this by removing the wrapper, and do not fix it with `setopt null_glob` — that is zsh-only and would break the Mac.

Decision:
- **Fresh candidate found** — set `${LOG_PATH}` to the newest fresh candidate and tell the user `Watching ${LOG_PATH} (last write Ns ago).` Proceed to <ArmMonitor/>.
- **No fresh candidates** — inform the user: `No fix logs modified in the last 2 hours. Start a run first, then use /fix monitor.` Then stop.

### <ArmMonitor/>

Identify the current phase by calling the parser:

```bash
python3 ~/.claude/scripts/fix/fix_report_parse.py --phase-detect ${LOG_PATH}
```

Output is two lines:

```
PHASE <name>          # one of: style-eval, style-eval-review, style-fix, done, unknown
LATEST_EVENT "<line>"
```

If `PHASE done`, tell the user the run is finished and stop — do not arm a monitor.

Otherwise tell the user: `Detected phase: <name>. Arming monitor on ${LOG_PATH}.`

**Style-fix manual logs** — if `${LOG_PATH}` matches `style-fix-manual-*.log`, do NOT use the tail+grep pipeline below. Arm the Monitor with the sandbox-safe Python helper instead:

- `description`: `fix: style-fix (manual)`
- `persistent`: `true`
- `timeout_ms`: `3600000`
- `command`: `python3 ~/.claude/scripts/fix/style-fix-monitor.py ${PROJECT}`

Derive `${PROJECT}` from the log before arming the helper. Use the first `Launched:` or worktree line that names the project. If the project cannot be derived, tell the user `Could not identify the project from ${LOG_PATH}; use tail -f ${LOG_PATH}.` Then stop. The helper tails both the manual log and the agent's own log (`/tmp/claude/style_fix_<project>.log`), translates the agent's phase sentinels to `phase=agent-step name=<...>`, and exits 0 on the `phase=launcher-exit` sentinel — no TaskStop needed. (The tail+grep pipeline it replaced could not stop itself: terminating it meant `pkill`-ing the right pid, and shell job-control is unavailable here. Use the helper on both machines — one code path, no signalling.) Then report events per <StyleFixManualEvents/> and skip the rest of this section.

For all other logs, fetch the live-monitor filter regex from the parser (single source of truth — keeps phase classification and live filtering in lockstep):

```bash
FILTER_REGEX=$(python3 ~/.claude/scripts/fix/fix_report_parse.py --filter-regex)
```

Call the Monitor tool with these exact parameters:

- `description`: `fix: <phase>` (e.g. `fix: style-fix`)
- `persistent`: `true`
- `timeout_ms`: `3600000`
- `command`:
  ```
  tail -F -n 0 ${LOG_PATH} 2>/dev/null | grep -E --line-buffered "${FILTER_REGEX}"
  ```

Always use the parser's regex regardless of detected phase — the regex is cheap and phases transition mid-run (eval → review → fix all in one orchestrator run).

After Monitor returns its task id, tell the user: `Monitor armed (task <id>). I will report state transitions as they arrive.` Then yield — do not poll, do not sleep, do not re-read the log.

### <EventReporting/>

Every Monitor event arriving in chat is a single line from the log. For each:

- Strip the leading `^` matched class and report a one-line update naming the project and outcome. Examples:
  - `OK: bevy_catenary (worktree created, fixes applied)` → `bevy_catenary done (style-fix).`
  - `ERROR: hana (codex exited immediately with no output)` → `hana failed style-fix: codex exited immediately with no output.`
  - `=== Done: 5 created, 2 failed, 0 skipped out of 7 ===` → `Style-fix run complete: 5 ok, 2 failed.`
- Maintain a running tally of OK/FAILED counts across notifications when the user benefits from it (e.g. style-fix has a known 7-project denominator).
- Treat `ERROR:`, `FAILED:`, `TIMEOUT:`, and `=== Done:` with non-zero failures as user-actionable — call PushNotification for those. Routine `OK:` and `Launched:` lines do not need a push.
- When `=== Fix complete` lands, or `=== Done:` for the standalone phase the user kicked off, tell the user the run is finished and stop the monitor with TaskStop using the task id you stored.

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

- The orchestrator script `fix.sh` tees to `~/.local/logs/fix.log` on both machines, so that symlink is the portable candidate and the one <DetectLog/> normally lands on. The scheduled job's own sink is per-platform — the journal on Linux (`journalctl --user -u style-fix`), `~/Library/Logs/nate-jobs/style-fix.log` on macOS — and only the macOS one is a file <DetectLog/> can stat.
- Standalone runs of `style-eval-all.sh` or `style-fix-worktrees.sh` invoked interactively typically log to `/tmp/claude/<name>-<suffix>.log`. The detector pattern globs match those.
- The Monitor uses `tail -F -n 0` so we start at the current end of the file — backlog is not re-emitted.
- `grep --line-buffered` is required — without it, pipe buffering delays events by minutes and the monitor looks broken.

</Monitor>

<Report>

## report [list | <path>]
## list

Read `~/.claude/scripts/fix/report-render.md` and follow it, substituting the remaining tokens (after `report`) for its `$ARGUMENTS`. If the top-level token was `list`, execute this section with `$ARGUMENTS` set to `list`. That document owns the parser invocation, the output format, and every rendering rule; it is shared with `fix.sh`, which sends it to the configured report agent after each scheduled run. Do not duplicate rendering logic here — if something is missing, fix `report-render.md` (or the parser).

</Report>

<StyleAgentConfig>

## eval|review|fix [on|off]
## agent
## on|off

Show fix pipeline agent assignments or set stage enablement. These commands do not run the eval, review, or fix phase for a project. Project-scoped execution is `/fix run <project>`.

**Stage enablement governs the scheduled run only.** `enabled=false` means the timer's run skips that stage; it never blocks `/fix run`, `/style_eval --fix`, or a stage script called directly. Say so whenever you report a stage as disabled, so `DISABLED` is not read as "the pipeline is off".

The fix stage assignment file is `~/.claude/scripts/fix/agent-assignments.conf`; it owns only stage `enabled=` values. The global agent registry is `~/.claude/config/agents.conf`; `[assignments] fix=<family>` selects the family and `[fix.<family>]` provides each stage's `agent[:effort]` row.

Three fix sections are configurable:

- **eval** — `[style_eval] enabled=`; registry row `fix.style_eval`.
- **review** — `[style_eval_review] enabled=`; registry row `fix.style_eval_review`.
- **fix** — `[style_fix] enabled=`; registry row `fix.style_fix`.

`fix.conf` owns pipeline targets and tunables only; agent settings placed there are rejected as stale.

Argument handling:

1. **`/fix agent`** — run `bash ~/.claude/scripts/fix/agent_assignments.sh` and relay its status view: assignment path, registry path, and each stage's `enabled`, family, resolved agent, and effort. Then point to `/agent fix <family>` for family switching and `/agent fix.<stage> <agent>[:<effort>]` for row edits. Stop. Extra tokens after `agent` are invalid; show those two `/agent` forms and stop.
2. **First token is `eval`, `review`, or `fix`** — map it to `[style_eval]`, `[style_eval_review]`, or `[style_fix]`.
3. **Scoped status:** `/fix eval`, `/fix review`, or `/fix fix` sources `agent_assignments.sh` and calls `cf_print_stage_assignment` for that section. Show its `enabled`, family, resolved agent, and effort.
4. **Project names are invalid here:** `/fix eval <project>`, `/fix review <project>`, and `/fix fix <project>` are not supported. Tell the user to use `/fix run <project>` for a single project.
5. **Scoped enable/disable:** `/fix eval on`, `/fix review off`, `/fix fix on`, etc. set only that section's `enabled=` to `true` or `false` in `agent-assignments.conf`.
6. **Global enable/disable:** `/fix on` and `/fix off` set all three `enabled=` values to `true` or `false` in `agent-assignments.conf`.
7. Any former `/fix agent ...`, `<scope> agent ...`, `<scope> model ...`, or `<scope> effort ...` setter form is invalid. Point to `/agent fix <family>` or `/agent fix.<stage> <agent>[:<effort>]` and stop without editing.
8. The `[projects]` skip list remains in `fix.conf` and is managed by `phase_skip.py` (see <Skip/>), never by direct edits.

</StyleAgentConfig>

<Skip>

## skip [...]

Skip or re-enable projects in the `[projects]` allowlist in `~/.claude/scripts/fix/fix.conf` by commenting/uncommenting allowlist lines. A target may be a whole directory or a workspace member (`<dir>/<subpath>`); a member is named by its last path segment (e.g. `bevy_diegetic`).

For redirected projects, the helper accepts either the active checkout name or the project key and applies the skip to the underlying allowlist entry.

Map the tokens to an action:

- empty → `status` (show which entries are currently skipped)
- first token is `enable-all` or `reset` → `enable-all`
- first token is `enable` → `enable` with the remaining tokens
- anything else → `skip` with all tokens

Run the helper and relay its output verbatim:

```bash
python3 ~/.claude/scripts/fix/phase_skip.py <action> [target ...]
```

The helper is the single source of truth for the skip list — do not edit those entries with Edit/Write. Agent settings are edited directly — see <StyleAgentConfig/>.

A commented allowlist line is invisible to the conf parser, so the entry drops out of its pass. The helper tags its edits with `#FIX_SKIP#` so `enable-all` only reverses temp skips and never touches plain doc comments. It exits non-zero on a name with no matching allowlist entry.

</Skip>
