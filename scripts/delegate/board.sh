#!/usr/bin/env bash
# board.sh — Shared coordination board and mutual-exclusion tokens for a
# multi-agent delegate phase.
#
# Why a file and not messages: the three agents of a phase may be codex
# processes, which have no ListAgents/SendMessage tool at all, and the
# orchestrator is asleep between progress ticks so it cannot relay. A file in
# the shared session directory is the one channel every family can both write
# and read, and every post is a broadcast -- reaching all peers and the wrapper
# at once -- rather than N-1 point-to-point sends that can each fail.
#
# Usage:
#   board.sh post    <session_dir> <agent> <kind> <message...>
#   board.sh read    <session_dir> [--since N] [--from AGENT] [--kind KIND]
#   board.sh acquire <session_dir> <agent> <resource> [--hold SECONDS] [--wait SECONDS]
#   board.sh release <session_dir> <agent> <resource>
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
        date -r "${PLAN_DELEGATE_NOW_EPOCH%%.*}" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null \
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
# resuming hours on, or the orchestrator at its next tick.
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

write_lock_meta() {
  local dir="$1" agent="$2" hold="$3"
  printf '%s' "$agent" > "${dir}/holder"
  printf '%s' "$(( $(now_epoch) + hold ))" > "${dir}/expires"
  printf '%s' "$$" > "${dir}/pid"
}

cmd_acquire() {
  local session_dir="${1:?acquire needs <session_dir>}"
  local agent="${2:?acquire needs <agent>}"
  local resource="${3:?acquire needs <resource>}"
  shift 3
  local hold="$DEFAULT_HOLD_SECONDS" wait_secs=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --hold) hold="${2:?--hold needs seconds}"; shift 2 ;;
      --wait) wait_secs="${2:?--wait needs seconds}"; shift 2 ;;
      *) die "acquire: unknown option '$1'" ;;
    esac
  done
  valid_token_name "$agent" || die "bad agent name '$agent'"
  valid_token_name "$resource" || die "bad resource name '$resource'"
  [[ "$hold" =~ ^[0-9]+$ ]] && (( hold > 0 )) || die "--hold must be a positive integer"
  [[ "$wait_secs" =~ ^[0-9]+$ ]] || die "--wait must be a non-negative integer"

  local dir; dir="$(lock_dir "$session_dir" "$resource")"
  mkdir -p "${session_dir}/locks"
  local deadline=$(( $(now_epoch) + wait_secs ))

  while :; do
    if mkdir "$dir" 2>/dev/null; then
      write_lock_meta "$dir" "$agent" "$hold"
      cmd_post "$session_dir" "$agent" claim "token ${resource} acquired for up to ${hold}s"
      printf 'acquired %s\n' "$resource"
      return 0
    fi

    # Held. Reclaim it only once it is provably past its own deadline: a
    # delegate can be killed mid-hold, and a token no dying process ever
    # releases would strand the phase behind a lock nobody owns.
    if lock_is_expired "$dir"; then
      local previous; previous="$(lock_holder "$dir")"
      rm -rf "$dir"
      if mkdir "$dir" 2>/dev/null; then
        write_lock_meta "$dir" "$agent" "$hold"
        cmd_post "$session_dir" "$agent" claim \
          "token ${resource} reclaimed from ${previous} after its hold expired"
        printf 'acquired %s (reclaimed from %s)\n' "$resource" "$previous"
        return 0
      fi
      continue
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
  local dir; dir="$(lock_dir "$session_dir" "$resource")"
  [[ -d "$dir" ]] || { printf 'not held %s\n' "$resource"; return 0; }
  local holder; holder="$(lock_holder "$dir")"
  # Releasing a token another agent now holds would hand a third agent a lock
  # while the real holder is still working behind it.
  if [[ "$holder" != "$agent" && "$holder" != "unknown" ]]; then
    printf 'board.sh: %s does not hold %s (holder is %s)\n' "$agent" "$resource" "$holder" >&2
    return 1
  fi
  rm -rf "$dir"
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
# progress table read a role back exactly instead of guessing from a sentence.
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
