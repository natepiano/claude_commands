#!/usr/bin/env bash

set -uo pipefail

# Every python3 here goes through the repo shim, which picks an interpreter
# by VERSION rather than by path. These scripts used to pin "$PY" --
# Apple 3.9 on the Mac, nonexistent on NixOS. The pin was never load-bearing:
# the tests in tests/ import typing.override and cannot run under 3.9 at all.
PY="$HOME/.claude/scripts/lib/py"
SNAPSHOT_TOOL="$HOME/.claude/scripts/prioritize/snapshot.py"
RENUMBER_TOOL="$HOME/.claude/scripts/prioritize/renumber.py"
WRITER_LOCK_TOOL="$HOME/.claude/scripts/prioritize/writer_lock.py"
RUNNER_LOCK_TOOL="$HOME/.claude/scripts/prioritize/runner_lock.py"
SIGNATURE_TOOL="$HOME/.claude/scripts/prioritize/watch_signature.py"
ISSUES_DIR="$HOME/rust/hanadocs/issues"
GOALS_FILE="$HOME/rust/hanadocs/prioritization goals.md"
# The vault as a git checkout, for the index refresh after a re-rank. See
# refresh_vault_git_index below for why that is this script's business.
VAULT_DIR="$HOME/rust/hanadocs"
VAULT_FILTER_NAME="hanadocs-strip-generated"
# Vault-relative, because git resolves a pathspec against its own working
# directory and every git call below runs with -C "$VAULT_DIR".
VAULT_ISSUES_PATHSPEC="issues"
# The cache root is chosen per platform, not by XDG alone. XDG_CACHE_HOME is
# normally UNSET -- the spec says fall back to ~/.cache -- so a plain
# "${XDG_CACHE_HOME:-$HOME/Library/Caches}" reproduces the macOS layout on
# Linux rather than only on the Mac. It did: the first run on NixOS created
# ~/Library/Caches, a directory nothing else on the box has any reason to own.
# Darwin still gets Library/Caches so the Mac's existing snapshot keeps being
# found; orphaning it would force a needless full re-score.
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/$([ "$(uname -s)" = Darwin ] && echo Library/Caches || echo .cache)}/hanadocs-prioritize"
SUCCESS_SNAPSHOT="$CACHE_DIR/semantic-inputs.json"
STATE_DIR="/tmp/hanadocs-prioritize"
RUNNER_LOCK_FILE="$STATE_DIR/runner.lock"
PENDING_FILE="$STATE_DIR/pending"
EVENT_LOG="$STATE_DIR/events.log"
LAST_STATUS_FILE="$STATE_DIR/last-status"
DEBOUNCE_SECONDS="0.25"
RUNNER_BUSY_EXIT=75
POLL_SECONDS="0.5"
ERROR_RETRY_SECONDS="5"
CONCURRENT_RETRY_SECONDS="0.25"
CONCURRENT_CHANGE_EXIT=3

CANDIDATE_FILE=""
POST_RUN_FILE=""
# Set once a renumber pass has actually written issue notes, and never reset:
# the locked loop can run several passes, and a later no-op must not cancel the
# index refresh an earlier pass earned.
RANKING_WRITE_OCCURRED=0

umask 077
if ! mkdir -p "$STATE_DIR" "$CACHE_DIR"; then
    printf 'hanadocs prioritize watcher: could not create runtime directories\n' >&2
    exit 2
fi

timestamp() {
    date '+%Y-%m-%dT%H:%M:%S%z'
}

log() {
    printf '[%s] pid=%s %s\n' "$(timestamp)" "$$" "$*" >> "$EVENT_LOG"
}

write_status() {
    local state="$1"
    local result="$2"
    local detail="$3"
    local temporary

    temporary="$(mktemp "$STATE_DIR/.last-status.XXXXXX")" || return 1
    printf '%s %s result=%s detail=%s\n' \
        "$state" "$(timestamp)" "$result" "$detail" > "$temporary"
    mv -f "$temporary" "$LAST_STATUS_FILE"
}

mark_pending() {
    touch "$PENDING_FILE"
}

discard_temporary_snapshots() {
    [[ -n "$CANDIDATE_FILE" ]] && rm -f "$CANDIDATE_FILE"
    [[ -n "$POST_RUN_FILE" ]] && rm -f "$POST_RUN_FILE"
    CANDIDATE_FILE=""
    POST_RUN_FILE=""
}

settled_state_is_current() {
    local check_status
    local settle_snapshot
    local snapshot_status

    if [[ ! -f "$SUCCESS_SNAPSHOT" ]]; then
        return 1
    fi
    settle_snapshot="$(mktemp "$CACHE_DIR/.semantic-inputs.settle.XXXXXX")" || {
        log "error: could not create settle snapshot"
        return 2
    }

    "$PY" "$SNAPSHOT_TOOL" --output "$settle_snapshot" >> "$EVENT_LOG" 2>&1
    snapshot_status=$?
    if (( snapshot_status != 0 )); then
        rm -f "$settle_snapshot"
        log "error: settle semantic snapshot failed exit=$snapshot_status"
        return 2
    fi
    if ! cmp -s "$settle_snapshot" "$SUCCESS_SNAPSHOT"; then
        rm -f "$settle_snapshot"
        return 1
    fi
    rm -f "$settle_snapshot"

    "$PY" "$RENUMBER_TOOL" --check >> "$EVENT_LOG" 2>&1
    check_status=$?
    if (( check_status == 0 )); then
        return 0
    fi
    if (( check_status == 1 )); then
        return 1
    fi
    log "error: settle rank check failed exit=$check_status"
    return 2
}

# Make `git status` in the vault agree with `git diff` after a re-rank.
#
# THE PROBLEM. The vault's .gitattributes runs issues/*.md through a
# `hanadocs-strip-generated` clean filter that drops the backlog_score and
# backlog_rank lines this watcher writes, so a global re-rank is invisible to
# git and produces no commit churn. `git diff` honours that and correctly
# reports nothing. `git status` does NOT: an index entry records the size of the
# FILTERED blob, so a note whose unfiltered size no longer matches reads as
# modified on stat data alone, and status will not run a filter to find out
# otherwise. After a re-rank that is every open issue at once -- 336 of them on
# this vault -- which buries any real edit and makes the vault look unsafe to
# commit.
#
# WHY HERE. This script is what makes those writes happen, so it is the one
# place that knows a re-rank just finished. The alternative -- a filesystem
# watcher on the vault, the way ~/.claude drives refresh_filtered_index.sh from
# a launchd WatchPaths job and a systemd .path unit -- would need new machine
# state on two platforms to rediscover a fact already known right here.
#
# WHY `git add --renormalize` AND NOT `git update-index --refresh`, which is the
# obvious candidate and does not work. `--refresh` re-reads stat data but does
# not put the file through the clean filter, so it cannot discover that a note
# is unchanged in git's terms; it reports "needs update", exits 1, and leaves
# the entry exactly as dirty as it found it. `--renormalize` re-runs the filter,
# writes the resulting blob -- byte-identical to the one already stored, so the
# index content does not move -- and records the WORKING TREE stat alongside it,
# which is the part that makes status quiet and keeps it quiet.
#
# WHY IT CANNOT STAGE WORK YOU DID NOT ASK IT TO STAGE, which matters because
# nothing reviews what a background daemon does to your index. `--renormalize`
# on its own would stage a genuine edit, so it is never pointed at one: the
# pathspec first EXCLUDES every note `git diff` reports as really changed. That
# diff is the filter-aware comparison of working tree against index, so a note
# whose change survives the filter is excluded by construction and stays visible
# in status, unstaged. Everything else is provably identical to its stored blob,
# and re-adding it is a no-op for content.
#
# `:(exclude,literal)` rather than a plain exclude: issue filenames are free
# text and several contain glob characters, which pathspec would otherwise
# interpret rather than match.
#
# This touches .git/index alone, never a watched file, so it cannot perturb the
# signature this daemon polls and cannot wake itself in a loop.
refresh_vault_git_index() {
    local configured
    local path
    local -a pathspec=("$VAULT_ISSUES_PATHSPEC")

    [[ -d "$VAULT_DIR/.git" ]] || return 0
    command -v git >/dev/null 2>&1 || return 0

    # No filter configured means no filtered paths to reconcile, and every issue
    # note is then legitimately modified. Renormalizing would rewrite the index
    # for all of them on every pass and buy nothing.
    configured="$(git -C "$VAULT_DIR" config --get "filter.$VAULT_FILTER_NAME.clean" 2>/dev/null)"
    if [[ -z "$configured" ]]; then
        log "vault clean filter not configured; skipping index refresh"
        return 0
    fi

    while IFS= read -r -d '' path; do
        pathspec+=(":(exclude,literal)$path")
    done < <(
        git -C "$VAULT_DIR" diff --name-only -z -- "$VAULT_ISSUES_PATHSPEC" 2>/dev/null
    )

    if git -C "$VAULT_DIR" add --renormalize -- "${pathspec[@]}" >> "$EVENT_LOG" 2>&1; then
        log "refreshed vault git index after ranking writes; real edits left alone=$(( ${#pathspec[@]} - 1 ))"
    else
        log "warning: vault git index refresh failed; status may report ranking churn"
    fi
    return 0
}

cleanup() {
    discard_temporary_snapshots
}

trap cleanup EXIT

debounce_events() {
    while true; do
        rm -f "$PENDING_FILE"
        sleep "$DEBOUNCE_SECONDS"
        if [[ ! -e "$PENDING_FILE" ]]; then
            return
        fi
        log "coalescing another filesystem event during debounce"
    done
}

require_runtime() {
    if [[ ! -d "$ISSUES_DIR" ]]; then
        log "error: issues directory missing: $ISSUES_DIR"
        write_status "error" "preflight" "issues-directory-missing" || true
        return 1
    fi
    if [[ ! -f "$GOALS_FILE" ]]; then
        log "error: goals file missing: $GOALS_FILE"
        write_status "error" "preflight" "goals-file-missing" || true
        return 1
    fi
    if [[ ! -f "$SNAPSHOT_TOOL" ]]; then
        log "error: snapshot tool missing: $SNAPSHOT_TOOL"
        write_status "error" "preflight" "snapshot-tool-missing" || true
        return 1
    fi
    if [[ ! -f "$RENUMBER_TOOL" ]]; then
        log "error: renumber tool missing: $RENUMBER_TOOL"
        write_status "error" "preflight" "renumber-tool-missing" || true
        return 1
    fi
    if [[ ! -f "$WRITER_LOCK_TOOL" ]]; then
        log "error: writer lock tool missing: $WRITER_LOCK_TOOL"
        write_status "error" "preflight" "writer-lock-tool-missing" || true
        return 1
    fi
    if [[ ! -f "$RUNNER_LOCK_TOOL" ]]; then
        log "error: runner lock tool missing: $RUNNER_LOCK_TOOL"
        write_status "error" "preflight" "runner-lock-tool-missing" || true
        return 1
    fi
    if [[ ! -f "$SIGNATURE_TOOL" ]]; then
        log "error: watch signature tool missing: $SIGNATURE_TOOL"
        write_status "error" "preflight" "signature-tool-missing" || true
        return 1
    fi
    return 0
}

run_once() {
    local apply_status
    local check_status

    discard_temporary_snapshots
    CANDIDATE_FILE="$(mktemp "$CACHE_DIR/.semantic-inputs.candidate.XXXXXX")" || {
        log "error: could not create candidate snapshot"
        write_status "error" "snapshot" "candidate-create-failed" || true
        return 2
    }

    "$PY" "$SNAPSHOT_TOOL" --output "$CANDIDATE_FILE" >> "$EVENT_LOG" 2>&1
    apply_status=$?
    if (( apply_status != 0 )); then
        log "error: semantic snapshot failed exit=$apply_status"
        write_status "error" "snapshot" "exit-$apply_status" || true
        return "$apply_status"
    fi

    if [[ -f "$SUCCESS_SNAPSHOT" ]] && cmp -s "$CANDIDATE_FILE" "$SUCCESS_SNAPSHOT"; then
        rm -f "$CANDIDATE_FILE"
        CANDIDATE_FILE=""
        "$PY" "$RENUMBER_TOOL" --check >> "$EVENT_LOG" 2>&1
        check_status=$?
        if (( check_status == 0 )); then
            log "semantic inputs and generated ranking state unchanged"
            write_status "ok" "no-op" "semantic-inputs-and-ranking-unchanged" || true
            return 0
        fi
        if (( check_status != 1 )); then
            log "error: unchanged-input rank check failed exit=$check_status"
            write_status "error" "rank-check" "exit-$check_status" || true
            return "$check_status"
        fi

        log "semantic inputs unchanged but generated ranking drifted; repairing"
        "$PY" "$RENUMBER_TOOL" --apply >> "$EVENT_LOG" 2>&1
        apply_status=$?
        if (( apply_status != 0 )); then
            if (( apply_status == CONCURRENT_CHANGE_EXIT )); then
                log "ranking files changed during generated-state repair; retry required"
                write_status "pending" "rerun" "concurrent-change-during-repair" || true
                return "$apply_status"
            fi
            log "error: generated ranking repair failed exit=$apply_status"
            write_status "error" "rank-repair" "exit-$apply_status" || true
            return "$apply_status"
        fi
        "$PY" "$RENUMBER_TOOL" --check >> "$EVENT_LOG" 2>&1
        check_status=$?
        if (( check_status != 0 )); then
            log "error: generated ranking repair validation failed exit=$check_status"
            write_status "error" "rank-repair-check" "exit-$check_status" || true
            return "$check_status"
        fi
        log "repaired generated ranking state without semantic input changes"
        RANKING_WRITE_OCCURRED=1
        write_status "ok" "repaired" "score-and-rank-canonical" || true
        return 0
    fi

    log "semantic ranking inputs changed; applying score and rank update"
    "$PY" "$RENUMBER_TOOL" --apply >> "$EVENT_LOG" 2>&1
    apply_status=$?
    if (( apply_status != 0 )); then
        if (( apply_status == CONCURRENT_CHANGE_EXIT )); then
            log "ranking files changed during apply; successful snapshot unchanged"
            write_status "pending" "rerun" "concurrent-change-during-apply" || true
            return "$apply_status"
        fi
        log "error: renumber apply failed exit=$apply_status; successful snapshot unchanged"
        write_status "error" "renumber-apply" "exit-$apply_status" || true
        return "$apply_status"
    fi

    "$PY" "$RENUMBER_TOOL" --check >> "$EVENT_LOG" 2>&1
    check_status=$?
    if (( check_status != 0 )); then
        log "error: post-apply validation failed exit=$check_status; successful snapshot unchanged"
        write_status "error" "post-apply-check" "exit-$check_status" || true
        return "$check_status"
    fi

    # Set here rather than at either return below: the apply has written notes
    # by this point, and it stays true whether this pass goes on to commit its
    # snapshot or bails out to a fresh pass because the inputs moved underneath.
    RANKING_WRITE_OCCURRED=1

    POST_RUN_FILE="$(mktemp "$CACHE_DIR/.semantic-inputs.post-run.XXXXXX")" || {
        log "error: could not create post-run snapshot"
        write_status "error" "snapshot" "post-run-create-failed" || true
        return 2
    }
    "$PY" "$SNAPSHOT_TOOL" --output "$POST_RUN_FILE" >> "$EVENT_LOG" 2>&1
    apply_status=$?
    if (( apply_status != 0 )); then
        log "error: post-run semantic snapshot failed exit=$apply_status"
        write_status "error" "snapshot" "post-run-exit-$apply_status" || true
        return "$apply_status"
    fi

    if ! cmp -s "$CANDIDATE_FILE" "$POST_RUN_FILE"; then
        log "ranking inputs changed during renumber; scheduling one fresh pass"
        write_status "pending" "rerun" "inputs-changed-during-pass" || true
        mark_pending
        return 0
    fi

    rm -f "$POST_RUN_FILE"
    POST_RUN_FILE=""
    if ! mv -f "$CANDIDATE_FILE" "$SUCCESS_SNAPSHOT"; then
        log "error: could not commit successful semantic snapshot"
        write_status "error" "snapshot-commit" "atomic-move-failed" || true
        return 2
    fi
    CANDIDATE_FILE=""

    log "renumber completed, validated, and committed semantic snapshot"
    write_status "ok" "updated" "score-and-rank-canonical" || true
    return 0
}

run_daemon() {
    local baseline=""
    local observed
    local after
    local confirmed
    local signature_status
    local runner_status
    local settle_status

    if ! require_runtime; then
        return 2
    fi
    log "persistent signature watcher started"
    while true; do
        observed="$("$PY" "$SIGNATURE_TOOL" 2>> "$EVENT_LOG")"
        signature_status=$?
        if (( signature_status != 0 )); then
            log "error: watch signature failed exit=$signature_status; retrying"
            write_status "error" "watch-signature" "exit-$signature_status" || true
            baseline=""
            sleep "$ERROR_RETRY_SECONDS"
            continue
        fi
        if [[ -n "$baseline" ]] && [[ "$observed" == "$baseline" ]]; then
            sleep "$POLL_SECONDS"
            continue
        fi

        bash "$0"
        runner_status=$?
        if (( runner_status != 0 )); then
            if (( runner_status == CONCURRENT_CHANGE_EXIT )); then
                log "files changed during ranking; coalescing and retrying"
                baseline=""
                sleep "$CONCURRENT_RETRY_SECONDS"
                continue
            fi
            log "error: detected change was not ranked exit=$runner_status; retrying"
            baseline=""
            sleep "$ERROR_RETRY_SECONDS"
            continue
        fi

        after="$("$PY" "$SIGNATURE_TOOL" 2>> "$EVENT_LOG")"
        signature_status=$?
        if (( signature_status != 0 )); then
            log "error: post-run watch signature failed exit=$signature_status; retrying"
            baseline=""
            sleep "$ERROR_RETRY_SECONDS"
            continue
        fi
        if [[ "$after" != "$observed" ]]; then
            settled_state_is_current
            settle_status=$?
            confirmed="$("$PY" "$SIGNATURE_TOOL" 2>> "$EVENT_LOG")"
            signature_status=$?
            if (( signature_status != 0 )); then
                log "error: settle watch signature failed exit=$signature_status; retrying"
                write_status "error" "watch-signature" "settle-exit-$signature_status" || true
                baseline=""
                sleep "$ERROR_RETRY_SECONDS"
                continue
            fi
            if (( settle_status == 0 )) && [[ "$confirmed" == "$after" ]]; then
                log "ranking writes changed file signatures; semantic inputs and ranks remain canonical"
                baseline="$confirmed"
                sleep "$POLL_SECONDS"
                continue
            fi
            log "watched files changed during ranking or settle verification; starting one fresh pass"
            baseline=""
            continue
        fi
        baseline="$after"
        sleep "$POLL_SECONDS"
    done
}

if [[ "${1:-}" == "--daemon" ]]; then
    if (( $# != 1 )); then
        log "error: invalid daemon watcher invocation"
        exit 2
    fi
    run_daemon
    exit $?
fi

if [[ "${1:-}" != "--locked" ]]; then
    if (( $# != 0 )); then
        log "error: unsupported watcher arguments: $*"
        exit 2
    fi
    if [[ ! -f "$RUNNER_LOCK_TOOL" ]]; then
        log "error: runner lock tool missing: $RUNNER_LOCK_TOOL"
        write_status "error" "preflight" "runner-lock-tool-missing" || true
        exit 2
    fi

    while true; do
        mark_pending
        "$PY" "$RUNNER_LOCK_TOOL" run "$RUNNER_LOCK_FILE" \
            bash "$0" --locked
        runner_status=$?
        if (( runner_status == RUNNER_BUSY_EXIT )); then
            log "watcher already running; marked one pending rerun"
            exit "$RUNNER_BUSY_EXIT"
        fi
        if [[ -e "$PENDING_FILE" ]]; then
            log "filesystem event arrived during runner lock handoff"
            continue
        fi
        exit "$runner_status"
    done
fi

if (( $# != 1 )); then
    log "error: invalid locked watcher invocation"
    exit 2
fi

if ! require_runtime; then
    exit 2
fi

last_status=0
while true; do
    debounce_events
    run_once
    last_status=$?

    if [[ -e "$PENDING_FILE" ]]; then
        log "pending filesystem event detected; starting one coalesced rerun"
        continue
    fi

    # Once per locked session, after the last pass, rather than inside run_once:
    # a coalesced rerun would otherwise pay for a full index refresh on every
    # pass, and only the final state of the notes is worth reconciling.
    if (( RANKING_WRITE_OCCURRED == 1 )); then
        refresh_vault_git_index
    fi

    exit "$last_status"
done
