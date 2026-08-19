#!/usr/bin/env bash
# progress_timer.sh — One-shot progress timer that leaves a marker while it runs.
#
# Usage: progress_timer.sh <session_dir> [seconds]
#
# Arms a single progress tick and records that it is armed. The marker is what
# the Stop hook reads to tell "a timer is pending" from "work is running and
# nobody is reporting on it": without it a bare `sleep` is indistinguishable
# from no timer at all, and the turn ends silently.
#
# Writes <session_dir>/progress_timer holding the deadline epoch and this
# process id, then removes it on exit however the timer ends -- tick, kill, or
# shell teardown -- so a stale marker never suppresses the hook.

set -euo pipefail

SESSION_DIR="${1:?Usage: progress_timer.sh <session_dir> [seconds]}"
SECONDS_TO_WAIT="${2:-}"

DELEGATE_CONF="${HOME}/.claude/config/delegate.conf"
INTERVAL_ORIGIN="argument"
if [[ -z "${SECONDS_TO_WAIT}" ]]; then
  INTERVAL_ORIGIN="PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS in ${DELEGATE_CONF}"
  SECONDS_TO_WAIT="$(
    sed -n 's/^PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=\([0-9][0-9]*\).*/\1/p' \
      "${DELEGATE_CONF}" 2>/dev/null | head -1
  )"
fi
# No default interval: an unconfigured timer would run at a length nobody chose,
# so fail loudly and let the caller fix the config.
if [[ ! "${SECONDS_TO_WAIT}" =~ ^[0-9]+$ ]] || (( SECONDS_TO_WAIT <= 0 )); then
  printf 'progress_timer.sh: interval from %s is not a positive integer: %s\n' \
    "${INTERVAL_ORIGIN}" "${SECONDS_TO_WAIT:-<empty>}" >&2
  exit 1
fi

MARKER="${SESSION_DIR}/progress_timer"
mkdir -p "${SESSION_DIR}"

cleanup() { rm -f "${MARKER}"; }
trap cleanup EXIT INT TERM

DEADLINE=$(( $(date +%s) + SECONDS_TO_WAIT ))
printf 'deadline_epoch=%s\npid=%s\ninterval_seconds=%s\n' \
  "${DEADLINE}" "$$" "${SECONDS_TO_WAIT}" > "${MARKER}"

sleep "${SECONDS_TO_WAIT}"
printf 'PLAN_DELEGATE_PROGRESS_TICK\n'
