#!/usr/bin/env zsh
# Unit status for /showrunner:produce's update schedule.
# Usage: unit_status.sh <state-dir> <user-zone> <session>...
# Each call checks every unit director: that its session and Claude are running, tick
# health for every unit with an active run, any form or decision waiting on the user,
# and its latest step, gate and ETA.
# A block on the showrunner or another unit prints as a BLOCK line with its age.
# No pipefail: each test reads grep's own status, and an early `grep -q` exit
# would fail the `tail` before it with SIGPIPE.

if (( $# < 3 )); then
  print -u2 'usage: unit_status.sh <state-dir> <user-zone> <session>...'
  exit 2
fi
DIR=$1
ZONE=$2
shift 2
units=("$@")
# `^out` alone: `nixpkgs#tmux` without it also prints the man output's path.
TM=$(command -v tmux) || TM=$(nix build --no-link --print-out-paths 'nixpkgs#tmux^out')/bin/tmux
REPO=${0:A:h:h:h}
PY=$REPO/scripts/lib/py
SESSIONS=$REPO/scripts/message/sessions.py
NOTIFIER=$REPO/scripts/message/notifier.sh
ACTIVE_DIR=${PLAN_DELEGATE_ACTIVE_DIR:-/tmp/claude/delegate/active}
mkdir -p "$DIR"
SEEN=$DIR/decisions_seen
BLOCKS=$DIR/blocks_open
touch "$SEEN" "$BLOCKS"

# Prints how long unit $1's block, text $2, has been open, counted from the
# first run that saw it. Each unit keeps one entry; a new block replaces it.
block_age() {
  local u=$1 now first
  now=$(date +%s)
  first=$(T=$2 awk -F'\t' -v u="$u" '$1 == u && $3 == ENVIRON["T"] { print $2 }' "$BLOCKS")
  if [[ -z $first ]]; then
    first=$now
    awk -F'\t' -v u="$u" '$1 != u' "$BLOCKS" > "$BLOCKS.new"
    print -r -- "$u"$'\t'"$now"$'\t'"$2" >> "$BLOCKS.new"
    mv "$BLOCKS.new" "$BLOCKS"
  fi
  print -r -- "$(( (now - first) / 3600 ))h$(( (now - first) % 3600 / 60 ))m"
}

# Drops unit $1's block entry, once its latest turn-end line is no longer one.
clear_block() {
  awk -F'\t' -v u="$1" '$1 != u' "$BLOCKS" > "$BLOCKS.new" && mv "$BLOCKS.new" "$BLOCKS"
}

# Prints what waits on the user in unit $1, whose pane text is $2.
# A unit director waits on the user when it is idle and its latest turn-end line is decision or blocked.
waiting_on_user() {
  local u=$1 p=$2 last gate_no gate_text key start o peer=
  print -r -- "$p" | tail -12 | grep -qE '^\s*[✢✻✽✶·*] [A-Z][a-z]+( [a-z]+)?…' && return
  if print -r -- "$p" | tail -15 | grep -q 'Enter to select'; then
    echo "FORM WAITING on you in $u: a question form is on its screen"
    return
  fi
  last=$(print -r -- "$p" | grep -nE '^\s*(— )?(holding|gate|decision|blocked|done):' | tail -1)
  if [[ $last != *'decision:'* && $last != *'blocked:'* ]]; then
    clear_block "$u"
    return
  fi
  for o in $units; do [[ $o != $u && $last == *$o* ]] && peer=1; done
  # A wait on the showrunner or another unit is the showrunner's to clear, not the user's.
  if [[ $last == *showrunner* || -n $peer ]]; then
    gate_text=${last#*:}
    [[ $last == *'blocked:'* ]] && echo "BLOCK in $u, open $(block_age "$u" "${gate_text## #}"):${gate_text}"
    return
  fi
  clear_block "$u"
  gate_no=${last%%:*}
  gate_text=${last#*:}
  key="$u|${gate_text## #}"
  if grep -qxF -- "$key" "$SEEN"; then
    echo "STILL WAITING on you, $u:${gate_text}"
    return
  fi
  start=$(print -r -- "$p" | head -$gate_no | grep -nE '^● ' \
    | grep -vE '^[0-9]+:● ([A-Z][A-Za-z]+\(|Background command|Skill\()' | tail -1 | cut -d: -f1)
  [[ -z $start ]] && start=$(( gate_no > 60 ? gate_no - 60 : 1 ))
  (( gate_no - start > 80 )) && start=$(( gate_no - 80 ))
  echo "=== DECISION for you from $u ==="
  print -r -- "$p" | sed -n "${start},${gate_no}p" | sed 's/[[:space:]]*$//'
  echo "=== end $u ==="
  print -r -- "$key" >> "$SEEN"
}

# The pane's Claude may have a different remote-control name from its tmux session.
# Search descendants in process-tree order, closest to the pane first.
pane_claude_pid() {
  print -r -- "$processes" | awk -v root="$1" '
    {
      child[NR] = $1
      parent[NR] = $2
      command[NR] = $0
      sub(/^[[:space:]]*[0-9]+[[:space:]]+[0-9]+[[:space:]]*/, "", command[NR])
    }
    END {
      queue[1] = root
      end = 1
      for (head = 1; head <= end; head++) {
        for (i = 1; i <= NR; i++) {
          if (parent[i] != queue[head]) continue
          if (command[i] ~ /^claude([[:space:]]|$)/) {
            print child[i]
            exit
          }
          queue[++end] = child[i]
        }
      }
    }'
}

echo "at $(TZ=$ZONE date '+%H:%M %Z') / $(date -u +%H:%M) UTC"
processes=$(ps -eo pid=,ppid=,args=)
for u in $units; do
  echo "== $u"
  if ! $TM has-session -t "$u" 2>/dev/null; then echo 'SESSION GONE'; continue; fi
  pane_pid=$($TM display-message -p -t "$u" '#{pane_pid}')
  pid=$(pane_claude_pid "$pane_pid")
  [[ -z $pid ]] && echo 'CLAUDE NOT RUNNING'
  if [[ -n $pid ]]; then
    session_id=$("$PY" "$SESSIONS" id "$pid" 2>/dev/null)
    if [[ -n $session_id && -f $ACTIVE_DIR/$session_id ]]; then
      session_dir=$(< "$ACTIVE_DIR/$session_id")
      if [[ -n $session_dir ]]; then
        health=$(zsh "$NOTIFIER" health "delegate-${session_dir:t}" 2>&1)
        if (( $? == 1 )); then
          print -r -- "TICKS FAILING (${health#failing: })"
        fi
      fi
    fi
  fi
  p=$($TM capture-pane -p -J -S -400 -t "$u")
  waiting_on_user "$u" "$p"
  pane=$(print -r -- "$p" | tail -150)
  print -r -- "$pane" | grep -E '^● ' | grep -vE 'says:|^● (Bash|Read|Write|Edit|Skill|Update|Search)\(' | tail -2 | cut -c1-220
  print -r -- "$pane" | grep -E '^\s*▸ ' | tail -1 | sed 's/^ *//' | cut -c1-160
  print -r -- "$pane" | grep -E '^\s*[✢✻✽✶·*] [A-Z][a-z]+( [a-z]+)?…' | tail -1 | sed 's/^ *//' | cut -c1-80
  print -r -- "$pane" | grep -nE -- '^\s*(— )?(decision|blocked|gate):' | tail -1 | cut -c1-200
  print -r -- "$pane" | grep -wE 'ETA' | grep -v 'From the user' | tail -1 | sed 's/^ *//' | cut -c1-160
done
