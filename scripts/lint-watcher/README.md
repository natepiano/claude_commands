# Lint Watcher

Background lint runner for all Rust projects under `~/rust/`. Watches for file changes and automatically runs `cargo mend` + `cargo clippy`, writing results to a shared protocol file that other tools can read.

Starts on login via launchd, stays alive indefinitely.

## Files

### Scripts

| File | Purpose |
|------|---------|
| `lint-watcher.sh` | Orchestrator. Scans `~/rust/` for eligible projects, spawns a `cargo-watch` per project. Restarts dead watchers. |
| `run-lint.sh` | Per-change runner. Acquires a global `flock`, runs `cargo mend` then `cargo clippy`, writes status to `target/port-report.log` and raw output to `target/port-report/`. |
| `status.sh` | Management CLI. Subcommands: `status` (default), `start`, `stop`, `restart`. |
| `setup.sh` | Idempotent setup. Installs `flock` via brew, symlinks the plist, bootstraps the LaunchAgent. |

### Configuration

| File | Purpose |
|------|---------|
| `com.natemccoy.lint-watcher.plist` | launchd plist — `RunAtLoad`, `KeepAlive`. |

Uses the exclude list from `~/.claude/scripts/nightly/nightly-rust.conf` `[exclude]` section.

### Flowchart

| File | Purpose |
|------|---------|
| `lint-watcher-flow.dot` | Graphviz source for the system flowchart. |
| `render-flow.py` | Renders the dot file to SVG (same approach as the nightly pipeline flowchart). |
| `lint-watcher-flow.svg` | Generated output — do not edit by hand. |

## Setup

```bash
bash ~/.claude/scripts/lint-watcher/setup.sh
```

## Usage

```bash
# Show status dashboard
bash ~/.claude/scripts/lint-watcher/status.sh

# Stop / start / restart
bash ~/.claude/scripts/lint-watcher/status.sh stop
bash ~/.claude/scripts/lint-watcher/status.sh start
bash ~/.claude/scripts/lint-watcher/status.sh restart

# Watch a project's lint log live
tail -f ~/rust/my-project/target/port-report.log

# Check launchd stdout/stderr
tail -f /tmp/lint-watcher-stdout.log
tail -f /tmp/lint-watcher-stderr.log
```

## port-report.log protocol

The protocol file is `{project}/target/port-report.log`. Any tool can produce or consume it.

### Format

Append-only, tab-delimited, one event per line:

```
{timestamp}\t{status}
```

Where `timestamp` is local time (`YYYY-MM-DD HH:MM:SS`) and `status` is one of:
- `started` — a lint run began
- `passed` — lint run completed with no issues
- `failed` — lint run completed with issues

### Reading it

Read the last line:
- `started` → lint is running (if timestamp > 30min old → stale/crashed)
- `passed` → all clean
- `failed` → issues found

### Raw output

Detailed tool output is stored in `target/port-report/`:
- `mend-latest.log` — stdout+stderr from the last `cargo mend` run
- `clippy-latest.log` — stdout+stderr from the last `cargo clippy` run

Written atomically (`.tmp` then `mv`).

### Lifecycle

The log grows by ~2 lines per lint run. It's wiped by `cargo clean` (lives in `target/`), which is fine — it's inherently transient.

## Architecture

```
Login → launchd → lint-watcher.sh (orchestrator)
                      │
                      ├─ cargo-watch (project A) ──→ run-lint.sh → flock → mend → clippy → port-report.log
                      ├─ cargo-watch (project B) ──→ run-lint.sh → flock (waits) → ...
                      └─ cargo-watch (project C) ──→ ...
```

- **One cargo-watch per project**: watches `.rs` and `Cargo.toml` via FSEvents. Idle until a file changes.
- **No serialization**: all projects lint in parallel. In practice, rapid edits keep restarting lints (cargo-watch kills and restarts), so only active projects run.
- **Kill+restart**: if you save again while a lint is running in the same project, cargo-watch kills the running lint and starts a new one.
- **nice 10**: lint runs yield to interactive builds.
- **--postpone**: cargo-watch does not run on startup — only on actual file changes.

## Consumers

| Consumer | How it uses port-report.log |
|----------|---------------------------|
| `/clippy` command | Checks for fresh cached results before running mend + clippy. If fresh, reads saved output instead of re-running. |
| cargo-port TUI | Displays green/red/spinner dot per project (optional `[lint]` config). |
| `status.sh` | Shows per-project lint status table. |

## Generating the flowchart

Prerequisites: `graphviz` (`brew install graphviz`), Python 3.

```bash
cd ~/.claude/scripts/lint-watcher
python3 render-flow.py
```
