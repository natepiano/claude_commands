#!/usr/bin/env bash

# Authorize Claude Code file-writing tools against cargo-berth's exact-file check.
# Bash writes are deliberately outside this hook and are observed after the fact.
#
# This wrapper decides one thing: whether the engine can be reached. Everything a
# user reads is the engine's own text, delivered by `exec` so stdout, stderr, and
# the exit status pass through byte for byte. A front end that rebuilt any of that
# from typed facts is what let a newer engine and an older front end disagree
# about what happened.

set -u

# CARGO_BERTH_BYPASS=1 is the escape hatch for an engine that hangs or crashes, so
# it is honored here, before the engine is reached: after the exec below there is
# no shell left to time out. The bypass is still audited. A pending-bypass marker
# is left in the repository's common git directory, in the same shape the trunk
# gate writes, and the next journal write imports it as a bypass record.
if [ "${CARGO_BERTH_BYPASS:-}" = "1" ]; then
    if common_git_directory=$(git rev-parse --git-common-dir 2>/dev/null) \
        && [ -d "$common_git_directory" ]; then
        if occurred_at=$(date -u '+%Y-%m-%dT%H:%M:%S.000Z' 2>/dev/null); then
            case "$occurred_at" in
                [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z)
                    occurrence_time='{"status":"known","at":"'"$occurred_at"'"}' ;;
                *) occurrence_time='{"status":"unavailable"}' ;;
            esac
        else
            occurrence_time='{"status":"unavailable"}'
        fi
        marker_contents='{"action":"editing","cause":{"kind":"environment_override","bypassed_merge":"edit-hook-'"$$"'"},"occurrence_time":'"$occurrence_time"'}'
        marker_base="$common_git_directory/cargo-berth-pending-bypass-edit-$$"
        marker="$marker_base.json"
        sequence=0
        while [ -e "$marker" ]; do
            sequence=$((sequence + 1))
            marker="$marker_base-$sequence.json"
        done
        (umask 077; set -C; printf '%s\n' "$marker_contents" > "$marker") 2>/dev/null || :
    fi
    printf '%s\n' '{"systemMessage":"cargo-berth was bypassed for this edit by CARGO_BERTH_BYPASS=1.","hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"CARGO_BERTH_BYPASS=1 is set, so this write was allowed without asking cargo-berth; a pending-bypass marker in the repository records it for the next journal write."}}'
    exit 0
fi

# Refusing is the failure mode this hook has always taken when it could not reach
# a working installation, and it is the safe direction: an unchecked edit can
# collide with a foreign reservation, where a refused one only costs a retry.
if ! command -v cargo-berth >/dev/null 2>&1; then
    printf 'cargo-berth refused this edit hook request: %s\n' \
        'the cargo-berth binary is not on PATH; install it before editing reserved paths' >&2
    exit 2
fi

exec cargo-berth hook pre-tool-use
