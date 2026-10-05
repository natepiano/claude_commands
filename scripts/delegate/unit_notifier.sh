#!/usr/bin/env zsh
# Make or retarget the notifier for one delegate run.

setopt no_unset pipe_fail extended_glob

SCRIPT=${0:A}
REPO=${SCRIPT:h:h:h}

(( $# == 1 )) || { print -u2 -r -- 'usage: unit_notifier.sh <claude_session_id>'; exit 2; }
session_id=$1
marker=${PLAN_DELEGATE_ACTIVE_DIR:-/tmp/claude/delegate/active}/$session_id
if [[ ! -f $marker ]]; then
  print -u2 -r -- "no active delegate run marker: $marker"
  exit 1
fi
session_dir=$(< "$marker")
if [[ -z $session_dir ]]; then
  print -u2 -r -- "empty delegate run marker: $marker"
  exit 1
fi
run_id=${session_dir:t}

# The leading digits, as progress_timer.sh reads them, so a trailing comment
# leaves the interval alone.
seconds=900
config=${PLAN_DELEGATE_CONFIG:-$HOME/.claude/config/delegate.conf}
if [[ -r $config ]]; then
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line == (#b)PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=([0-9]##)(|[^0-9]*) ]] && seconds=$match[1]
  done < "$config"
fi
minutes=$(( (seconds + 59) / 60 ))
(( minutes >= 1 )) || minutes=1

check_words=("$REPO/scripts/lib/py" "$REPO/scripts/hooks/delegate_run.py" check "$session_id" "$session_dir")
check=${(j: :)${(q)check_words[@]}}
exec zsh "$REPO/scripts/message/notifier.sh" new "delegate-$run_id" \
  --to "session:$session_id" --every "$minutes" \
  --command '/unit:report' --check "$check" --hold
