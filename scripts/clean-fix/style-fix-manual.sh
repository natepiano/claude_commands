#!/bin/bash
# Manual launcher for style-fix-worktrees.sh that accumulates log files
# into ~/.local/logs/clean-fix/ alongside the clean-fix orchestrator runs, so
# /clean_fix report can pick them up.
#
# Usage: style-fix-manual.sh [--foreground] [project_name]
#   --foreground  — run style-fix-worktrees.sh in the current process so the
#                   caller (e.g. an agent invoking via Bash with
#                   run_in_background:true) gets a real completion event when
#                   the fix actually finishes. Without this flag the launcher
#                   detaches via nohup + disown (the original clean-fix pattern).
#   project_name (optional) — pass through to style-fix-worktrees.sh to
#                             restrict the run to a single project.
#
# In both modes the log path is printed up front so it can be tailed.

set -euo pipefail

FOREGROUND=0
if [[ "${1:-}" == "--foreground" ]]; then
    FOREGROUND=1
    shift
fi

LOG_DIR="$HOME/.local/logs/clean-fix"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/style-fix-manual-$(date '+%Y%m%d-%H%M%S').log"

SCRIPT="$HOME/.claude/scripts/clean-fix/style-fix-worktrees.sh"

echo "Log: $LOG"

if (( FOREGROUND )); then
    # Run in the current process so the caller's "command completed" signal
    # fires when the fix actually finishes. Output still lands in the same
    # log file so a Monitor on $LOG sees every progress line.
    exec "$SCRIPT" "$@" > "$LOG" 2>&1
else
    nohup "$SCRIPT" "$@" > "$LOG" 2>&1 &
    PID=$!
    disown
    echo "PID: $PID"
    echo "Tail: tail -f \"$LOG\""
    echo "Monitor: /clean_fix monitor"
fi
