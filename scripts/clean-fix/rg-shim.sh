#!/bin/bash
# rg timeout shim — bounds every NON-INTERACTIVE ripgrep invocation so a
# stdin-blocked rg can never hang forever.
#
# Why this exists
# ---------------
# The style-eval agents (claude --print) frequently issue pipelines like
#   rg PATTERN -g '*.rs' | rg -v X | head
# where the first rg has glob filters but NO path argument. With no path, rg
# searches stdin whenever stdin is not a terminal. claude's Bash tool hands each
# command an open stdin pipe that never receives data and never closes, so that
# first rg blocks on read() forever. On 2026-06-02 two such agents wedged for
# 11h, stalling the whole nightly clean-fix run; because the dead run stayed in
# the process table, the launchd trigger's `pgrep` guard then suppressed every
# subsequent run all night. See clean-fix/README.md.
#
# What it does
# ------------
# Interactive rg (stdin is a tty) is a transparent passthrough — exec the real
# rg with zero added latency or processes. Non-interactive rg runs under a
# watchdog that SIGTERM/SIGKILLs it after RG_SHIM_TIMEOUT seconds (default 60),
# so a path-less rg reading a dead pipe dies in seconds instead of hanging the
# caller. Normal rg searches finish in milliseconds and never hit the cap.
#
# Status
# ------
# Retired. This was previously activated by a symlink at ~/.claude/scripts/rg
# and a shell PATH entry that put ~/.claude/scripts before the real rg. That
# global PATH shadowing caused unrelated command-resolution risk, so the symlink
# and .zshrc PATH export were removed. Keep this file only as incident context.

set -u

# Resolve the real rg: the first `rg` on PATH that does not live in this shim's
# own directory (avoids the shim invoking itself). Fall back to the known
# Homebrew location so a path-search miss never fork-bombs.
SHIM_DIR="$HOME/.claude/scripts"
REAL_RG=""
OLD_IFS="$IFS"
IFS=:
for d in $PATH; do
    [ "$d" = "$SHIM_DIR" ] && continue
    if [ -x "$d/rg" ]; then
        REAL_RG="$d/rg"
        break
    fi
done
IFS="$OLD_IFS"
[ -z "$REAL_RG" ] && [ -x /opt/homebrew/bin/rg ] && REAL_RG=/opt/homebrew/bin/rg
if [ -z "$REAL_RG" ]; then
    echo "rg-shim: could not locate the real rg on PATH" >&2
    exit 127
fi

# Interactive: passthrough. No watchdog, no extra processes, identical behavior.
if [ -t 0 ]; then
    exec "$REAL_RG" "$@"
fi

# Non-interactive: run rg under a watchdog so it cannot block forever.
TIMEOUT="${RG_SHIM_TIMEOUT:-60}"

# <&0 is required: bash redirects a background job's stdin to /dev/null unless an
# explicit redirection is given. Without it, rg would be severed from the real
# stdin — a piped `cmd | rg -v X` would read /dev/null and silently drop all
# input. <&0 hands rg the shim's actual stdin (the upstream pipe, or the
# blocked claude pipe that the watchdog then bounds).
"$REAL_RG" "$@" <&0 &
rg_pid=$!

(
    # Poll liveness so the watchdog stops early once rg finishes; on overrun,
    # SIGTERM then SIGKILL. kill -0 on a zombie still reports alive, but the
    # foreground wait below reaps rg the instant it exits and then kills this
    # watchdog, so zombie-blindness here is harmless.
    waited=0
    while [ "$waited" -lt "$TIMEOUT" ]; do
        kill -0 "$rg_pid" 2>/dev/null || exit 0
        sleep 1
        waited=$((waited + 1))
    done
    echo "rg-shim: rg exceeded ${TIMEOUT}s (likely a path-less search blocked on stdin) — killing" >&2
    kill -TERM "$rg_pid" 2>/dev/null
    sleep 2
    kill -KILL "$rg_pid" 2>/dev/null
) &
watchdog=$!

wait "$rg_pid"
status=$?
kill "$watchdog" 2>/dev/null
wait "$watchdog" 2>/dev/null
exit "$status"
