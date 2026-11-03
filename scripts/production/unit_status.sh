#!/usr/bin/env zsh
# Unit status for /showrunner:produce's update schedule.
# Usage: unit_status.sh <state-dir> <user-zone> --production <production doc>
# The units are the live rows of the showrunner's production doc. Each is found by the mark on
# its host session, so a block is headed by the unit id, which no rename changes.
# Each call checks every unit director: that its session and Claude are running, tick
# health for every unit with an active run, any form or decision waiting on the user,
# and its latest step, gate and ETA.
# A block on the showrunner or another unit prints as a BLOCK line with its age.
# No pipefail: each test reads grep's own status, and an early `grep -q` exit
# would fail the `tail` before it with SIGPIPE.

if (( $# != 4 )) || [[ $3 != --production && $3 != --showrunner ]]; then
  print -u2 'usage: unit_status.sh <state-dir> <user-zone> --production <production doc>'
  exit 2
fi
# An update prompt written before a production was named by its doc passes `--showrunner <session>`.
[[ $3 == --production ]] && production=(--production "$4") || production=("$4")
DIR=$1
ZONE=$2
REPO=${0:A:h:h:h}
# One line per unit: its id, its place now, whether Claude runs there, its session name now.
unit_lines=$("$REPO/scripts/lib/py" "$REPO/scripts/production/live_units.py" "${production[@]}") || exit 1
[[ -n $unit_lines ]] && units=("${(@f)unit_lines}") || units=()
# What a gate line may call a peer: its unit id, or its session name now.
names=()
for line in $units; do
  fields=("${(@ps:\t:)line}")
  names+=($fields[1] $fields[4])
done
PY=$REPO/scripts/lib/py
slug=$("$PY" - "$REPO" "${production[@]}" <<'PYTHON'
import sys
sys.path.insert(0, sys.argv[1])
from scripts.production.showrunners import named, production_slug
arguments = sys.argv[2:]
print(production_slug(arguments[1]) if arguments[0] == "--production" else named(arguments[0])["slug"])
PYTHON
) || exit 1
marked_lines=$("$PY" "$REPO/scripts/production/unit_lookup.py" list "$slug") || exit 1
NOTIFIER=$REPO/scripts/message/notifier.sh
# Run markers sit under unit/, or delegate/ for a run started before the rename.
if [[ -n ${UNIT_ACTIVE_DIR:-${PLAN_DELEGATE_ACTIVE_DIR:-}} ]]; then
  ACTIVE_DIRS=(${UNIT_ACTIVE_DIR:-$PLAN_DELEGATE_ACTIVE_DIR})
else
  ACTIVE_DIRS=(/tmp/claude/unit/active /tmp/claude/delegate/active)
fi
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
# A unit director waits on the user when it is idle and its latest turn-end line
# is decision, gate or blocked.
waiting_on_user() {
  local u=$1 p=$2 last turn_end gate_no gate_text actor_text key start o peer=
  print -r -- "$p" | tail -12 | grep -qE '^\s*[✢✻✽✶·*] [A-Z][a-z]+( [a-z]+)?…' && return
  if print -r -- "$p" | tail -15 | grep -q 'Enter to select'; then
    echo "FORM WAITING on you in $u: a question form is on its screen"
    return
  fi
  last=$(print -r -- "$p" | grep -nE '^\s*(— )?(holding|gate|decision|blocked|done):' | tail -1)
  turn_end=${last#*:}
  turn_end=${turn_end%%:*}
  turn_end=${turn_end##*[[:space:]]}
  if [[ $turn_end != decision && $turn_end != gate && $turn_end != blocked ]]; then
    clear_block "$u"
    return
  fi
  gate_text=${last#*:}
  actor_text=${gate_text#*:}
  for o in $names; do
    [[ $o != $u && " $actor_text " == *[^[:alnum:]_-]"$o"[^[:alnum:]_-]* ]] && peer=1
  done
  # A wait on the showrunner or another unit is the showrunner's to clear, not the user's.
  if [[ " $actor_text " == *[^[:alnum:]_-]showrunner[^[:alnum:]_-]* || -n $peer ]]; then
    [[ $turn_end == blocked ]] \
      && echo "BLOCK in $u, open $(block_age "$u" "${gate_text## #}"):${gate_text}"
    return
  fi
  clear_block "$u"
  gate_no=${last%%:*}
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

echo "at $(TZ=$ZONE date '+%H:%M %Z') / $(date -u +%H:%M) UTC"
for line in $units; do
  fields=("${(@ps:\t:)line}")
  u=$fields[1]
  target=$fields[2]
  echo "== $u"
  if [[ -z $target ]]; then echo 'SESSION GONE'; continue; fi
  # Lookup columns: unit, place, label, state, Claude name, session id, socket.
  claude_state=$(print -r -- "$marked_lines" | awk -F'\t' -v u="$u" '$1 == u { print $4 }')
  session_id=$(print -r -- "$marked_lines" | awk -F'\t' -v u="$u" '$1 == u { print $6 }')
  case $claude_state in
    unknown) echo 'CLAUDE STATE UNKNOWN' ;;
    stopped) echo 'CLAUDE NOT RUNNING' ;;
  esac
  marker=
  for active in $ACTIVE_DIRS; do
    if [[ -n $session_id && -f $active/$session_id ]]; then marker=$active/$session_id; break; fi
  done
  if [[ -n $marker ]]; then
    session_dir=$(< "$marker")
    if [[ -n $session_dir ]]; then
      instance=unit-${session_dir:t}
      [[ ${${session_dir:h}:t} == delegate ]] && instance=delegate-${session_dir:t}
      health=$(zsh "$NOTIFIER" health "$instance" 2>&1)
      if (( $? == 1 )); then
        print -r -- "TICKS FAILING (${health#failing: })"
      fi
    fi
  fi
  p=$("$PY" "$REPO/scripts/production/session_pane.py" read unit "$slug" "$u" --lines 400 --joined)
  waiting_on_user "$u" "$p"
  pane=$(print -r -- "$p" | tail -150)
  print -r -- "$pane" | grep -E '^● ' | grep -vE 'says:|^● (Bash|Read|Write|Edit|Skill|Update|Search)\(' | tail -2 | cut -c1-220
  print -r -- "$pane" | grep -E '^\s*▸ ' | tail -1 | sed 's/^ *//' | cut -c1-160
  print -r -- "$pane" | grep -E '^\s*[✢✻✽✶·*] [A-Z][a-z]+( [a-z]+)?…' | tail -1 | sed 's/^ *//' | cut -c1-80
  print -r -- "$pane" | grep -nE -- '^\s*(— )?(decision|blocked|gate):' | tail -1 | cut -c1-200
  # The last three distinct ETA lines, in pane order. The dailies builder, which remembers when
  # it first saw each, picks the newest: the lowest line may be an old one pinned under later text.
  print -r -- "$pane" | grep -wE 'ETA' | grep -v 'From the user' | sed 's/^ *//' | cut -c1-160 \
    | awk '{ line[NR] = $0; last[$0] = NR }
           END { for (i = NR; i >= 1 && n < 3; i--) if (last[line[i]] == i) keep[++n] = line[i]
                 for (i = n; i >= 1; i--) print keep[i] }'
done
