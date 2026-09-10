#!/usr/bin/env bash
# Scheduler wrapper for fix.sh. The schedule is declared once in
# /etc/nixos/modules/common/style-fix.nix and rendered per platform: a systemd
# user timer on Linux (style-fix.timer), a launchd user agent on macOS
# (org.nixos.style-fix). Both exec this script; nothing here is platform-aware.
#
# The two timers count differently on purpose. launchd's StartInterval fires
# every 600s regardless of whether a run is still going; systemd's
# OnUnitInactiveSec counts 600s from when the previous run FINISHED. The pgrep
# guard below is what absorbs launchd's extra firings, and on both platforms it
# still catches a fix.sh started by hand. There is no idle gate either way, so
# the eval/review/fix queue stays full around the clock.
#
# This is the only automated caller of fix.sh, so it is the only caller that
# sets FIX_SCHEDULED=1. That marker is what makes a run honor the `enabled=`
# switches in agent-assignments.conf; every hand-invoked run ignores them and
# runs all three stages.
#
# Concurrency guard: pgrep against the orchestrator's path.
# fix.sh runs synchronously start-to-finish (style-fix-worktrees waits
# on its backgrounded agents before returning), so its presence in the
# process table accurately reflects "a run is still in progress."

set -euo pipefail

FIX_ORCHESTRATOR_PATH="$HOME/.claude/scripts/fix/fix.sh"

if pgrep -f "$FIX_ORCHESTRATOR_PATH" >/dev/null 2>&1; then
    exit 0
fi

export FIX_SCHEDULED=1
exec "$FIX_ORCHESTRATOR_PATH"
