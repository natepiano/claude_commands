#!/usr/bin/env zsh
# Unit status for /showrunner:produce's update schedule.
# Usage: unit_status.sh <state-dir> <user-zone> <session>...
# Each call scans every unit for a form or decision waiting on the user, then
# reports the next unit in round-robin order.
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
mkdir -p "$DIR"
STATE=$DIR/rotation
SEEN=$DIR/decisions_seen
touch "$SEEN"

# A unit waits on the user when it is idle and its latest turn-end line is decision or blocked.
for u in $units; do
  $TM has-session -t "$u" 2>/dev/null || continue
  p=$($TM capture-pane -p -J -S -400 -t "$u")
  print -r -- "$p" | tail -12 | grep -qE '^\s*[✢✻✽✶·*] [A-Z][a-z]+( [a-z]+)?…' && continue
  if print -r -- "$p" | tail -15 | grep -q 'Enter to select'; then
    echo "FORM WAITING on you in $u: a question form is on its screen"
    continue
  fi
  last=$(print -r -- "$p" | grep -nE '^\s*(— )?(holding|gate|decision|blocked|done):' | tail -1)
  [[ $last == *'decision:'* || $last == *'blocked:'* ]] || continue
  # A block that waits on the showrunner is the showrunner's to clear, not the user's.
  [[ $last == *showrunner* ]] && continue
  gate_no=${last%%:*}
  gate_text=${last#*:}
  key="$u|${gate_text## #}"
  if grep -qxF -- "$key" "$SEEN"; then
    echo "STILL WAITING on you, $u:${gate_text}"
    continue
  fi
  start=$(print -r -- "$p" | head -$gate_no | grep -nE '^● ' \
    | grep -vE '^[0-9]+:● ([A-Z][A-Za-z]+\(|Background command|Skill\()' | tail -1 | cut -d: -f1)
  [[ -z $start ]] && start=$(( gate_no > 60 ? gate_no - 60 : 1 ))
  (( gate_no - start > 80 )) && start=$(( gate_no - 80 ))
  echo "=== DECISION for you from $u ==="
  print -r -- "$p" | sed -n "${start},${gate_no}p" | sed 's/[[:space:]]*$//'
  echo "=== end $u ==="
  print -r -- "$key" >> "$SEEN"
done

i=$(cat "$STATE" 2>/dev/null || echo 0)
unit=${units[$(( i % ${#units} + 1 ))]}
echo $(( (i + 1) % ${#units} )) > "$STATE"
echo "unit=$unit at $(TZ=$ZONE date '+%H:%M %Z') / $(date -u +%H:%M) UTC"
if ! $TM has-session -t "$unit" 2>/dev/null; then echo 'SESSION GONE'; exit 0; fi
pid=$(pgrep -f "^claude --resume .* --remote-control $unit|^claude --remote-control $unit" | head -1)
[[ -z $pid ]] && echo 'CLAUDE NOT RUNNING'
pane=$($TM capture-pane -p -J -S -150 -t "$unit")
print -r -- "$pane" | grep -E '^● ' | grep -vE 'says:|^● (Bash|Read|Write|Edit|Skill|Update|Search)\(' | tail -2 | cut -c1-220
print -r -- "$pane" | grep -E '^\s*▸ ' | tail -1 | sed 's/^ *//' | cut -c1-160
print -r -- "$pane" | grep -E '^\s*[✢✻✽✶·*] [A-Z][a-z]+( [a-z]+)?…' | tail -1 | sed 's/^ *//' | cut -c1-80
print -r -- "$pane" | grep -nE -- '^\s*(— )?(decision|blocked|gate):' | tail -1 | cut -c1-200
print -r -- "$pane" | grep -wE 'ETA' | grep -v 'From the user' | tail -1 | sed 's/^ *//' | cut -c1-160
