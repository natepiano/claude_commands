---
description: Kick off a full nightly run on demand (clean + build + warmup + eval + review + fix), unsandboxed, then arm the live monitor
---

# Run Nightly

Launch `~/.claude/scripts/nightly/nightly-rust-clean-build.sh` interactively, off the launchd schedule, so the user can test the full pipeline end-to-end.

## Hard requirement: must run unsandboxed

The script invokes `codex` and `claude`, which need write access to `~/.codex/sessions` and to many paths outside the sandbox allowlist. Per `~/.claude/CLAUDE.md` ("codex and nightly style scripts must run unsandboxed"), **always** invoke this with `dangerouslyDisableSandbox: true` from the start. Do not try the sandboxed run first — it will fail.

## Arguments

`$ARGUMENTS` — none. (No per-project filter is supported by the orchestrator.)

## Step 1: Refuse to launch if a nightly is already running

A second concurrent run will collide with the first one's worktrees and history files. Before launching, check:

```bash
pgrep -fl nightly-rust-clean-build.sh || true
```

If anything matches, tell the user `Nightly already running (PID …). Use /monitor_nightly to attach to the live log, or wait for it to finish.` Stop. Do not launch a second copy.

## Step 2: Pick a log path and launch

The orchestrator writes its own timestamped log under `~/.local/logs/nightly/nightly-YYYYMMDD-HHMMSS.log` and updates the `~/.local/logs/nightly-rust-clean-build.log` symlink to point at it. Don't pre-create or redirect — just launch.

Run the script in the background, unsandboxed:

```bash
~/.claude/scripts/nightly/nightly-rust-clean-build.sh
```

Use `Bash` with `dangerouslyDisableSandbox: true` and `run_in_background: true`. Capture the resulting bash shell id so the user can kill it later with `KillShell` if needed.

After launch, resolve the active log path:

```bash
ls -t ~/.local/logs/nightly/nightly-*.log 2>/dev/null | head -1
```

Tell the user: `Nightly launched (shell <id>). Log: <path>.`

## Step 3: Offer to arm the monitor

In the same response, offer one short follow-up: `Want me to /monitor_nightly to stream phase transitions?`

If the user says yes, invoke `/monitor_nightly` with the resolved log path. If they decline, stop.

## Notes

- The full run can take an hour or more. The user does not need to keep this conversation open — the script runs detached and writes to disk.
- This skill is the on-demand counterpart to the launchd job; the schedule is unaffected. If a launchd-triggered run is already in flight at midnight, Step 1 will catch it.
- For testing only the new review stage in isolation, prefer `~/.claude/scripts/nightly/style-eval-review-all.sh [project]` — much faster than a full nightly.
- Use `/nightly_report` after the run for a per-project matrix, or `/monitor_nightly` during the run for live updates.
