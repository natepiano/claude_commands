#!/usr/bin/env bash
# board.sh — Shared coordination board and mutual-exclusion tokens for a
# multi-agent delegate phase.
#
# Why a file and not messages: the agents of a phase may be codex
# processes, which have no ListAgents/SendMessage tool at all, and the
# unit director is asleep between progress ticks so it cannot relay. A file in
# the shared session directory is the one channel every family can both write
# and read, and every post is a broadcast -- reaching all peers and the wrapper
# at once -- rather than N-1 point-to-point sends that can each fail.
#
# Usage:
#   board.sh post    <session_dir> <agent> <kind> <message...>
#   board.sh read    <session_dir> [--since N] [--from AGENT] [--kind KIND]
#   board.sh acquire <session_dir> <agent> <resource> [--pid PID] [--hold SECONDS] [--wait SECONDS]
#   board.sh release <session_dir> <agent> <resource> [--pid PID]
#   board.sh renew   <session_dir> <agent> <resource> [--hold SECONDS]
#   board.sh role    <session_dir> <slot> <role> [note...]
#   board.sh roles   <session_dir>
#   board.sh locks   <session_dir>
#
# Post kinds (a closed set, so peers can scan for what concerns them):
#   register  — "I am <slot>, opening in <role>". The launcher stamps the same
#               machine-readable `role=<name>` field that `role` writes, so a
#               slot has a reported role before it posts anything itself
#   claim     — "I am taking these files / this work"
#   release   — "I am done with these files / this work"
#   status    — progress narration
#   blocked   — cannot proceed, and why
#   handoff   — this slot changed role; always written by `board.sh role`, which
#               stamps a machine-readable `role=<name>` field so the progress
#               table can say what each agent is doing without parsing prose.
#               `post` refuses a handoff that does not lead with that field
#   done      — this agent's assignment is complete. The launcher's own exit
#               posts (`done`, `blocked`) carry a `launcher:` prefix so the
#               progress report can tell them from the seat's words
#
# There is no `ask` and no `answer`: a question to a peer is a message.
#
# Produces:
#   <session_dir>/board.log       — append-only broadcast log, one line per post
#   <session_dir>/locks/<res>.d/  — token directory; existence IS the lock
#   <session_dir>/locks/<res>.d/holder_pid — optional pid of the token holder
#
# Concurrency: each post is a single write() to an O_APPEND file descriptor,
# which is the same guarantee heartbeat.sh relies on for its concurrent wrapper
# and agent writers. Messages are flattened to one line and capped so a post
# never interleaves with another. Tokens use mkdir, which is atomic on POSIX:
# exactly one of N racing acquirers creates the directory and the rest fail.

set -euo pipefail

MAX_MESSAGE_CHARS=900
DEFAULT_HOLD_SECONDS=900

die() { printf 'board.sh: %s\n' "$1" >&2; exit 2; }

# One clock for the whole run. The progress recorder reads roles back off this
# log and places them on a timeline built from its own events, so the two have
# to agree on what time it is -- a board stamped from the wall clock against a
# ledger stamped from an override puts every role change outside every round.
# `PLAN_DELEGATE_NOW_EPOCH` is that override, and `-r` versus `-d @` is the only
# thing BSD and GNU date disagree about here.
now_iso() {
    if [[ -n "${PLAN_DELEGATE_NOW_EPOCH:-}" ]]; then
        { date -r "${PLAN_DELEGATE_NOW_EPOCH%%.*}" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null || date -d "@${PLAN_DELEGATE_NOW_EPOCH%%.*}" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null; } \
            || date -d "@${PLAN_DELEGATE_NOW_EPOCH%%.*}" +%Y-%m-%dT%H:%M:%S%z
        return
    fi
    date +%Y-%m-%dT%H:%M:%S%z
}
now_epoch() {
    if [[ -n "${PLAN_DELEGATE_NOW_EPOCH:-}" ]]; then
        printf '%s\n' "${PLAN_DELEGATE_NOW_EPOCH%%.*}"
        return
    fi
    date +%s
}

# One line, no control characters, bounded length: the three properties that
# keep a concurrent append atomic and the log parseable.
flatten() {
  printf '%s' "$*" | tr '\n\r\t' '   ' | tr -cd '[:print:]' | cut -c "1-${MAX_MESSAGE_CHARS}"
}

# `ask` and `answer` are deliberately absent. A question to a peer is a message
# now -- SendMessage for a claude member, codex_mesh.py for a codex one -- and
# leaving the kinds here would give a delegate two ways to ask one question,
# with the board's way being the one nobody is listening on. Every kind that
# remains is a record something later reads back: the progress table, a peer
# resuming hours on, or the unit director at its next tick.
valid_kind() {
  case "$1" in
    register|claim|release|status|blocked|handoff|done) return 0 ;;
    *) return 1 ;;
  esac
}

# Agent and resource names index into paths and log fields, so keep them to a
# character set that cannot escape either.
valid_token_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]
}

cmd_post() {
  local session_dir="${1:?post needs <session_dir>}"
  local agent="${2:?post needs <agent>}"
  local kind="${3:?post needs <kind>}"
  shift 3
  valid_token_name "$agent" || die "agent name must be alphanumeric/._- and 1-64 chars: '$agent'"
  valid_kind "$kind" || die "unknown kind '$kind' (register claim release status blocked handoff done); ask a peer by message, not on the board"
  [[ $# -gt 0 ]] || die "post needs a message"
  local body
  body="$(flatten "$@")"
  # A handoff is a role change and nothing else. The progress table reads the
  # role= field off it, so a handoff written as prose is a movement the table
  # never shows -- and the row above it then claims the old role held all
  # along. Refusing it here sends narration to `status` and role changes
  # through `role`, which writes the field.
  local role_lead='^role=(impl|test|fix|review)( |$)'
  if [[ "$kind" == "handoff" && ! "$body" =~ $role_lead ]]; then
    die "handoff records a role change: use board.sh role <session_dir> <slot> <role> [note]; narrate with status"
  fi
  mkdir -p "$session_dir"
  printf '%s [%s] %s: %s\n' "$(now_iso)" "$agent" "$kind" "$body" \
    >> "${session_dir}/board.log"
}

cmd_read() {
  local session_dir="${1:?read needs <session_dir>}"; shift
  local since=0 from="" kind=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --since) since="${2:?--since needs a number}"; shift 2 ;;
      --from)  from="${2:?--from needs an agent}";   shift 2 ;;
      --kind)  kind="${2:?--kind needs a kind}";     shift 2 ;;
      *) die "read: unknown option '$1'" ;;
    esac
  done
  [[ "$since" =~ ^[0-9]+$ ]] || die "--since must be a non-negative integer"
  local log="${session_dir}/board.log"
  [[ -f "$log" ]] || return 0
  # Number every line first so the caller's cursor counts board positions, not
  # positions within a filtered view -- a cursor taken from filtered output
  # would silently skip every post the filter dropped.
  awk -v since="$since" -v from="$from" -v kind="$kind" '
    NR <= since { next }
    from != "" && index($0, "[" from "]") == 0 { next }
    kind != "" && index($0, "] " kind ": ") == 0 { next }
    { printf "%d\t%s\n", NR, $0 }
  ' "$log"
}

lock_dir() { printf '%s/locks/%s.d' "$1" "$2"; }

# Keep this file and its inode after each use. An open descriptor holds the
# lock until this shell closes it or exits.
lock_guard() {
  if ! exec 9>>"$1"; then
    return 2
  fi
  local status
  if python3 -c 'import fcntl, sys
try:
    fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(75)' 9 2>/dev/null; then
    return 0
  else
    status=$?
  fi
  exec 9>&-
  [[ "$status" -eq 75 ]] && return 1
  return 2
}

# Read a lock's metadata. Absent metadata means the directory was created a
# moment ago and its holder has not written itself in yet; treat that as a live
# lock held by an unknown agent rather than as a free one, so a race never
# resolves into two holders.
lock_holder() { cat "$1/holder" 2>/dev/null || printf 'unknown'; }
lock_expiry() { cat "$1/expires" 2>/dev/null || printf ''; }

lock_is_expired() {
  local expires; expires="$(lock_expiry "$1")"
  [[ "$expires" =~ ^[0-9]+$ ]] || return 1
  (( $(now_epoch) > expires ))
}

pid_is_gone() {
  local state status
  if state="$(ps -p "$1" -o stat= 2>/dev/null)"; then
    [[ "$state" == Z* ]]
  else
    status=$?
    [[ "$status" -eq 1 && -z "$state" ]]
  fi
}

lock_stale_reason() {
  local holder_pid
  holder_pid="$(cat "$1/holder_pid" 2>/dev/null || true)"
  if [[ "$holder_pid" =~ ^[1-9][0-9]*$ ]] && pid_is_gone "$holder_pid"; then
    printf 'pid %s' "$holder_pid"
    return 0
  fi
  if lock_is_expired "$1"; then
    printf 'expired'
    return 0
  fi
  return 1
}

write_lock_meta() {
  local dir="$1" agent="$2" hold="$3" holder_pid="$4"
  printf '%s' "$agent" > "${dir}/holder"
  printf '%s' "$(( $(now_epoch) + hold ))" > "${dir}/expires"
  if [[ -n "$holder_pid" ]]; then
    printf '%s' "$holder_pid" > "${dir}/holder_pid"
  fi
}

cleanup_owned_group() {
  local dir="$1"
  [[ -f "$dir/owned" ]] || return 0
  python3 - "$dir/owned" <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

record = Path(sys.argv[1])

def identity(pid: int) -> str | None:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        # A zombie leader still owns its PID, so its start time is safe to
        # check while live children remain in the group.
        return fields[19]
    except (OSError, IndexError):
        try:
            return subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        except subprocess.CalledProcessError:
            return None

def live_group(group: int) -> bool:
    try:
        lines = subprocess.check_output(['ps', '-A', '-o', 'pgid=', '-o', 'stat='],
                                        text=True).splitlines()
        return any(int(parts[0]) == group and not parts[1].startswith('Z')
                   for line in lines if (parts := line.split()) and len(parts) > 1)
    except (OSError, ValueError, subprocess.CalledProcessError):
        # Reclaim and release must fail closed if the group cannot be inspected.
        raise RuntimeError('cannot inspect step process group')

try:
    saved = json.loads(record.read_text())
    group = int(saved['group'])
    start = str(saved['start'])
except (OSError, ValueError, KeyError, TypeError):
    print('board.sh: invalid step ownership record', file=sys.stderr)
    sys.exit(1)

if identity(group) != start:
    if live_group(group):
        print('board.sh: step group leader identity changed', file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

try:
    os.killpg(group, signal.SIGKILL)
except ProcessLookupError:
    pass

deadline = time.monotonic() + 2
while live_group(group) and time.monotonic() < deadline:
    time.sleep(0.05)
if live_group(group):
    print('board.sh: step group still has running processes', file=sys.stderr)
    sys.exit(1)
PY
}

cmd_acquire() {
  local session_dir="${1:?acquire needs <session_dir>}"
  local agent="${2:?acquire needs <agent>}"
  local resource="${3:?acquire needs <resource>}"
  shift 3
  local hold="$DEFAULT_HOLD_SECONDS" wait_secs=0 holder_pid=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --hold) hold="${2:?--hold needs seconds}"; shift 2 ;;
      --wait) wait_secs="${2:?--wait needs seconds}"; shift 2 ;;
      --pid) [[ $# -ge 2 ]] || die "--pid must be a positive integer"
             [[ "$2" =~ ^[1-9][0-9]*$ ]] || die "--pid must be a positive integer"
             holder_pid="$2"; shift 2 ;;
      *) die "acquire: unknown option '$1'" ;;
    esac
  done
  valid_token_name "$agent" || die "bad agent name '$agent'"
  valid_token_name "$resource" || die "bad resource name '$resource'"
  [[ "$hold" =~ ^[0-9]+$ ]] && (( hold > 0 )) || die "--hold must be a positive integer"
  [[ "$wait_secs" =~ ^[0-9]+$ ]] || die "--wait must be a non-negative integer"
  local dir guard; dir="$(lock_dir "$session_dir" "$resource")"
  guard="${session_dir}/locks/${resource}.guard"
  mkdir -p "${session_dir}/locks"
  local deadline=$(( $(now_epoch) + wait_secs ))

  while :; do
    if mkdir "$dir" 2>/dev/null; then
      write_lock_meta "$dir" "$agent" "$hold" "$holder_pid"
      cmd_post "$session_dir" "$agent" claim "token ${resource} acquired for up to ${hold}s"
      printf 'acquired %s\n' "$resource"
      return 0
    fi

    # Only one waiter can remove a stale lock. Check it again under the guard,
    # since another waiter may have replaced it after the first check.
    if lock_stale_reason "$dir" >/dev/null; then
      if lock_guard "$guard"; then
        local reason previous previous_pid reclaimed_holders
        if reason="$(lock_stale_reason "$dir")"; then
          previous="$(lock_holder "$dir")"
          previous_pid="$(cat "$dir/holder_pid" 2>/dev/null || true)"
          reclaimed_holders="$(cat "$dir/reclaimed_holders" 2>/dev/null || true)"
          if [[ "$reason" == pid\ * ]] && ! cleanup_owned_group "$dir"; then
            exec 9>&-
            return 1
          fi
          rm -rf "$dir"
          if mkdir "$dir" 2>/dev/null; then
            write_lock_meta "$dir" "$agent" "$hold" "$holder_pid"
            if [[ "$reason" == expired && "$previous" != unknown
                  && "$previous_pid" =~ ^[1-9][0-9]*$ ]]; then
              printf '%s\n%s\t%s\n' "$reclaimed_holders" "$previous" "$previous_pid" \
                > "$dir/reclaimed_holders"
            elif [[ -n "$reclaimed_holders" ]]; then
              printf '%s\n' "$reclaimed_holders" > "$dir/reclaimed_holders"
            fi
            exec 9>&-
            if [[ "$reason" == pid\ * ]]; then
              cmd_post "$session_dir" "$agent" claim \
                "token ${resource} reclaimed from ${previous}: holder pid ${reason#pid } is gone"
            else
              cmd_post "$session_dir" "$agent" claim \
                "token ${resource} reclaimed from ${previous} after its hold expired"
            fi
            printf 'acquired %s (reclaimed from %s)\n' "$resource" "$previous"
            return 0
          fi
        fi
        exec 9>&-
      fi
    fi

    (( $(now_epoch) < deadline )) || break
    sleep 3
  done

  printf 'busy %s held by %s\n' "$resource" "$(lock_holder "$dir")" >&2
  return 1
}

cmd_release() {
  local session_dir="${1:?release needs <session_dir>}"
  local agent="${2:?release needs <agent>}"
  local resource="${3:?release needs <resource>}"
  shift 3
  local release_pid=""
  if (( $# > 0 )); then
    [[ $# -eq 2 && "$1" == --pid && "$2" =~ ^[1-9][0-9]*$ ]] \
      || die "release accepts only --pid followed by a positive integer"
    release_pid="$2"
  fi
  local dir; dir="$(lock_dir "$session_dir" "$resource")"
  local guard="${session_dir}/locks/${resource}.guard"
  local attempt lock_status guarded=0
  if [[ -d "$dir" ]]; then
    for ((attempt=0; attempt<200; attempt++)); do
      if lock_guard "$guard"; then
        guarded=1
        break
      else
        lock_status=$?
      fi
      (( lock_status == 1 )) || break
      sleep 0.05
    done
  fi
  if [[ ! -d "$dir" ]]; then
    (( guarded == 0 )) || exec 9>&-
    printf 'not held %s\n' "$resource"
    return 0
  fi
  if (( guarded == 0 )); then
    printf 'board.sh: cannot guard %s release\n' "$resource" >&2
    return 1
  fi
  local holder holder_pid
  holder="$(lock_holder "$dir")"
  holder_pid="$(cat "$dir/holder_pid" 2>/dev/null || true)"
  # Releasing a token another agent now holds would hand a third agent a lock
  # while the real holder is still working behind it.
  if [[ "$holder" != "$agent" && "$holder" != "unknown" ]] \
      || [[ -n "$release_pid" && "$release_pid" != "$holder_pid" ]]; then
    local reclaimed=0
    if [[ -n "$release_pid" && -f "$dir/reclaimed_holders" ]] \
        && grep -Fxq -- "$(printf '%s\t%s' "$agent" "$release_pid")" "$dir/reclaimed_holders"; then
      reclaimed=1
    fi
    (( guarded == 0 )) || exec 9>&-
    if [[ "$holder" == "$agent" && -n "$release_pid" && "$release_pid" != "$holder_pid" ]]; then
      printf 'board.sh: %s pid %s does not hold %s (holder pid is %s)\n' \
        "$agent" "$release_pid" "$resource" "$holder_pid" >&2
    else
      printf 'board.sh: %s does not hold %s (holder is %s)\n' "$agent" "$resource" "$holder" >&2
    fi
    if (( reclaimed == 1 )); then
      return 3
    fi
    return 1
  fi
  if ! cleanup_owned_group "$dir"; then
    exec 9>&-
    return 1
  fi
  rm -rf "$dir"
  (( guarded == 0 )) || exec 9>&-
  cmd_post "$session_dir" "$agent" release "token ${resource} released"
  printf 'released %s\n' "$resource"
}

cmd_renew() {
  local session_dir="${1:?renew needs <session_dir>}"
  local agent="${2:?renew needs <agent>}"
  local resource="${3:?renew needs <resource>}"
  shift 3
  local hold="$DEFAULT_HOLD_SECONDS"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --hold) hold="${2:?--hold needs seconds}"; shift 2 ;;
      *) die "renew: unknown option '$1'" ;;
    esac
  done
  [[ "$hold" =~ ^[0-9]+$ ]] && (( hold > 0 )) || die "--hold must be a positive integer"
  local dir; dir="$(lock_dir "$session_dir" "$resource")"
  [[ -d "$dir" ]] || { printf 'board.sh: %s is not held\n' "$resource" >&2; return 1; }
  local holder; holder="$(lock_holder "$dir")"
  [[ "$holder" == "$agent" ]] || {
    printf 'board.sh: %s does not hold %s (holder is %s)\n' "$agent" "$resource" "$holder" >&2
    return 1
  }
  printf '%s' "$(( $(now_epoch) + hold ))" > "${dir}/expires"
  printf 'renewed %s\n' "$resource"
}

# A role is what a slot is doing now, as opposed to the slot itself, which never
# changes. Recording it through a command rather than free text is what lets the
# progress table read a role back exactly instead of inferring it from a sentence.
cmd_role() {
  local session_dir="${1:?role needs <session_dir>}"
  local slot="${2:?role needs <slot>}"
  local role="${3:?role needs <role>}"
  shift 3
  valid_token_name "$slot" || die "bad slot '$slot'"
  case "$role" in
    impl|test|fix|review) ;;
    *) die "role must be impl, test, fix, or review; got '$role'" ;;
  esac
  cmd_post "$session_dir" "$slot" handoff "role=${role} $(flatten "${@:-taking this role}")"
}

cmd_roles() {
  local session_dir="${1:?roles needs <session_dir>}"
  local log="${session_dir}/board.log"
  [[ -f "$log" ]] || return 0
  # Last write wins per slot: a slot that changed role twice reports the role it
  # holds now, not the one it opened in.
  awk '
    match($0, /\[[^]]+\]/) {
      slot = substr($0, RSTART + 1, RLENGTH - 2)
    }
    /\] handoff: role=/ {
      r = $0
      sub(/.*\] handoff: role=/, "", r)
      sub(/ .*/, "", r)
      role[slot] = r
      next
    }
    /\] register: / {
      # A register carrying role= is a launcher opening the slot, and the latest
      # one wins: the board spans the whole run and still holds every earlier
      # round register. One without the field is an agent introducing itself and
      # must not erase a role already taken; it only seeds a placeholder.
      if (match($0, /role=[A-Za-z0-9_]+/)) {
        r = substr($0, RSTART + 5, RLENGTH - 5)
        role[slot] = r
      } else if (!(slot in role)) {
        role[slot] = ""
      }
    }
    END { for (s in role) printf "%s\t%s\n", s, role[s] }
  ' "$log"
}

cmd_locks() {
  local session_dir="${1:?locks needs <session_dir>}"
  local locks_root="${session_dir}/locks"
  [[ -d "$locks_root" ]] || return 0
  local dir name remaining
  for dir in "$locks_root"/*.d; do
    [[ -d "$dir" ]] || continue
    name="$(basename "$dir" .d)"
    remaining="$(lock_expiry "$dir")"
    if [[ "$remaining" =~ ^[0-9]+$ ]]; then
      remaining=$(( remaining - $(now_epoch) ))
    else
      remaining=unknown
    fi
    printf '%s\tholder=%s\tremaining_seconds=%s\n' "$name" "$(lock_holder "$dir")" "$remaining"
  done
}

main() {
  local command="${1:-}"
  [[ -n "$command" ]] || die "usage: board.sh {post|read|acquire|release|renew|role|roles|locks} <session_dir> ..."
  shift
  case "$command" in
    post)    cmd_post "$@" ;;
    read)    cmd_read "$@" ;;
    acquire) cmd_acquire "$@" ;;
    release) cmd_release "$@" ;;
    renew)   cmd_renew "$@" ;;
    role)    cmd_role "$@" ;;
    roles)   cmd_roles "$@" ;;
    locks)   cmd_locks "$@" ;;
    *) die "unknown command '$command'" ;;
  esac
}

main "$@"
