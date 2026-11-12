#!/usr/bin/env zsh
# Unit status for /showrunner:produce's update schedule.
# Usage: unit_status.sh <state-dir> <user-zone> --production <staff name>
# The units are the live rows of the production doc.
# Native session ids select panes. Staff ids identify saved state.
# Each call checks every unit director: that its session and Claude are running, tick
# health for every unit with an active run, any form or decision waiting on the user,
# and its latest step, gate and ETA.
# A block on the showrunner or another unit prints as a BLOCK line with its age.
# No pipefail: each test reads grep's own status, and an early `grep -q` exit
# would fail the `tail` before it with SIGPIPE.

if (( $# != 4 )) || [[ $3 != --production && $3 != --showrunner ]]; then
  print -u2 'usage: unit_status.sh <state-dir> <user-zone> --production <staff name>'
  exit 2
fi
# An update prompt written before a production was named by its doc passes `--showrunner <session>`.
[[ $3 == --production ]] && production=(--production "$4") || production=("$4")
DIR=$1
ZONE=$2
REPO=${0:A:h:h:h}
# One line per unit: its id, its place now, whether Claude runs there, its session name now.
unit_lines=$("$REPO/scripts/lib/py" "$REPO/scripts/production/live_units.py" --status "${production[@]}") || exit 1
[[ -n $unit_lines ]] && units=("${(@f)unit_lines}") || units=()
# What a gate line may call a peer: its unit id, or its session name now.
names=()
for line in $units; do
  fields=("${(@ps:\t:)line}")
  names+=($fields[1] $fields[4])
done
PY=$REPO/scripts/lib/py
staff_lines=$("$PY" - "$REPO" "${production[@]}" <<'PYTHON'
import sys
from scripts.production import staff
from scripts.production.showrunners import owner_for
arguments = sys.argv[2:]
record = staff.snapshot()
owner = owner_for(arguments[1] if arguments[0] == "--production" else arguments[0], record=record)
for row in record.find(parent=owner.id, kind="unit"):
    run = str(row.run.path) if isinstance(row.run, staff.OpenRun) else ""
    print(f"{row.name}\t{row.id}\t{run}")
PYTHON
) || exit 1
staff_rows=("${(@f)staff_lines}")
typeset -A state_keys open_runs
for line in $staff_rows; do
  fields=("${(@ps:\t:)line}")
  state_keys[$fields[1]]=$fields[2]
  open_runs[$fields[1]]=$fields[3]
done
NOTIFIER=$REPO/scripts/message/notifier.sh
mkdir -p "$DIR"
SEEN=$DIR/decisions_seen
BLOCKS=$DIR/blocks_open
touch "$SEEN" "$BLOCKS"

# Apply each state change through the shared atomic writer.
state_record() {
  "$PY" - "$REPO" "$@" <<'PYTHON'
import sys
import time
from pathlib import Path
from scripts.lib.atomic_file import write_text_atomic
action, filename, identity = sys.argv[2:5]
path = Path(filename)
content = path.read_text() if path.exists() else ""
lines = content.splitlines()
if action == "decision":
    updated = content + identity + "\n"
else:
    text = sys.argv[5] if action == "block" else ""
    now = int(time.time())
    first = next((int(parts[1]) for line in lines
                  if len(parts := line.split("\t", 2)) == 3 and parts[0] == identity and parts[2] == text), now)
    kept = [line for line in lines if line.split("\t", 1)[0] != identity]
    if action == "block":
        kept.append(f"{identity}\t{first}\t{text}")
        age = now - first
        print(f"{age // 3600}h{age % 3600 // 60}m")
    updated = "".join(line + "\n" for line in kept)
if updated != content:
    write_text_atomic(path, updated)
PYTHON
}

block_age() {
  state_record block "$BLOCKS" "${state_keys[$1]}" "$2"
}

clear_block() {
  state_record clear "$BLOCKS" "${state_keys[$1]}"
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
  key="${state_keys[$u]}|${gate_text## #}"
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
  state_record decision "$SEEN" "$key"
}

echo "at $(TZ=$ZONE date '+%H:%M %Z') / $(date -u +%H:%M) UTC"
for line in $units; do
  fields=("${(@ps:\t:)line}")
  u=$fields[1]
  [[ -n ${state_keys[$u]:-} ]] || { print -u2 "No staff id for $u"; exit 1; }
  target=$fields[2]
  echo "== $u"
  if [[ -z $target ]]; then echo 'SESSION GONE'; continue; fi
  # Status columns: unit, place, state, Claude name, session id.
  claude_state=$fields[3]
  session_id=$fields[5]
  case $claude_state in
    unknown) echo 'CLAUDE STATE UNKNOWN' ;;
    stopped) echo 'CLAUDE NOT RUNNING' ;;
  esac
  session_dir=${open_runs[$u]}
  if [[ -n $session_id && -n $session_dir ]]; then
    instance=unit-${session_dir:t}
    [[ ${${session_dir:h}:t} == delegate ]] && instance=delegate-${session_dir:t}
    health=$(zsh "$NOTIFIER" health "$instance" 2>&1)
    if (( $? == 1 )); then
      print -r -- "TICKS FAILING (${health#failing: })"
    fi
  fi
  [[ $claude_state == unknown || $claude_state == stopped ]] && continue
  p=$("$PY" "$REPO/scripts/production/session_pane.py" read session "$session_id" --lines 400 --joined)
  waiting_on_user "$u" "$p"
  pane=$(print -r -- "$p" | tail -150)
  print -r -- "$pane" | grep -E '^● ' | grep -vE 'says:|^● (Bash|Read|Write|Edit|Skill|Update|Search)\(' | tail -2 | cut -c1-220
  print -r -- "$pane" | grep -E '^\s*▸ ' | tail -1 | sed 's/^ *//' | cut -c1-160
  print -r -- "$pane" | grep -E '^\s*[✢✻✽✶·*] [A-Z][a-z]+( [a-z]+)?…' | tail -1 | sed 's/^ *//' | cut -c1-80
  print -r -- "$pane" | grep -nE -- '^\s*(— )?(decision|blocked|gate):' | tail -1 | cut -c1-200
  # The last three distinct ETA lines, in pane order. The caller, which remembers when
  # it first saw each, picks the newest: the lowest line may be an old one pinned under later text.
  print -r -- "$pane" | grep -wE 'ETA' | grep -v 'From the user' | sed 's/^ *//' | cut -c1-160 \
    | awk '{ line[NR] = $0; last[$0] = NR }
           END { for (i = NR; i >= 1 && n < 3; i--) if (last[line[i]] == i) keep[++n] = line[i]
                 for (i = n; i >= 1; i--) print keep[i] }'
done
