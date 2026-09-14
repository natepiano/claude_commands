#!/usr/bin/env bash
# scan.sh — /clippy's scan stages (mend, mend --fix, clippy, doc) in one shell
# process. /clippy backgrounds this once and reads summary.txt when the task
# notification lands. Nothing watches it while it runs, so nothing can strand it
# half-finished: an agent that yields mid-scan is not resumed by a backgrounded
# command's completion, which is what put the stages in a script instead.
#
# Usage: scan.sh <output-dir> [extra clippy args...]
#
# summary.txt is the contract. One line per stage:
#     <stage> exit=<status> log=<path>
#     <stage> off|clean|fixed
#     STOP <reason>
# and, always, `scan <ok|stop> done` as the final line. A summary without that
# line means the script was killed — treat the run as unfinished, never as
# clean.
set -uo pipefail

OUT=${1:?usage: scan.sh <output-dir> [clippy args...]}
shift
mkdir -p "$OUT" || exit 2

# Overridable so the stop paths can be exercised against stubs; /clippy never
# sets either.
LINT=${LINT_BIN:-$HOME/.claude/scripts/lint/lint}
CONF=${LINT_CONF:-$HOME/.claude/scripts/lint/lint_config.sh}
SUMMARY=$OUT/summary.txt
: > "$SUMMARY"

say() { printf '%s\n' "$*" >>"$SUMMARY"; }

# A missing or broken config reader runs the check, matching invoke.sh: a caller
# must never be silently under-verified because a config script moved.
on() { [[ -x $CONF ]] || return 0; bash "$CONF" enabled "$1"; local s=$?; [[ $s -le 1 ]] || return 0; return $s; }

stage() {
    local name=$1 log=$OUT/$2
    shift 2
    "$@" >"$log" 2>&1
    local st=$?
    say "$name exit=$st log=$log"
    return $st
}

status=ok

if on mend; then
    if ! stage mend mend.txt "$LINT" mend; then
        say 'STOP mend failed — see the log; environment setup may be required'
        status=stop
    elif grep -q 'No findings\.' "$OUT/mend.txt"; then
        say 'mend clean'
    else
        # A fix that fails to compile is reverted by mend, and that is a bug in
        # mend rather than a lint finding. Either way the scan stops: the tree is
        # back at the state that reproduces it, and a later stage would bury it.
        if stage mend-fix mend_fix.txt "$LINT" mend --fix &&
            ! grep -qiE 'revert|rolled back' "$OUT/mend_fix.txt"; then
            say 'mend fixed'
        else
            say 'STOP mend --fix failed or was reverted — capture the reproduction'
            status=stop
        fi
    fi
else
    say 'mend off'
fi

if [[ $status == ok ]]; then
    if on clippy; then
        stage clippy clippy.txt "$LINT" clippy "$@" || true
    else
        say 'clippy off'
    fi

    if on doc; then
        stage doc doc.txt "$LINT" doc || true
    else
        say 'doc off'
    fi
fi

say "scan $status done"
