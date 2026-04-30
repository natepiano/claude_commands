#!/bin/bash
# Manual launcher for style-fix-worktrees.sh that accumulates log files
# into ~/.local/logs/nightly/ alongside the nightly orchestrator runs, so
# /nightly_report can pick them up.
#
# Usage: style-fix-manual.sh [project_name]
#   project_name (optional) — pass through to style-fix-worktrees.sh to
#   restrict the run to a single project.
#
# Runs in the background via nohup + disown. Prints the log path and PID
# so you can tail or monitor.

set -euo pipefail

LOG_DIR="$HOME/.local/logs/nightly"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/style-fix-manual-$(date '+%Y%m%d-%H%M%S').log"

SCRIPT="$HOME/.claude/scripts/nightly/style-fix-worktrees.sh"

echo "Log: $LOG"
nohup "$SCRIPT" "$@" > "$LOG" 2>&1 &
PID=$!
disown
echo "PID: $PID"
echo "Tail: tail -f \"$LOG\""
echo "Monitor: /monitor_nightly \"$LOG\""
