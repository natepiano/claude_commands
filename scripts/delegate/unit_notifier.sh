#!/usr/bin/env zsh
# Make or retarget the notifier for one unit run.

setopt no_unset pipe_fail extended_glob

SCRIPT=${0:A}
REPO=${SCRIPT:h:h:h}

(( $# == 1 || $# == 2 )) || { print -u2 -r -- 'usage: unit_notifier.sh <claude_session_id> [on|off]'; exit 2; }
if (( $# == 2 )) && [[ $2 != on && $2 != off ]]; then
  print -u2 -r -- 'usage: unit_notifier.sh <claude_session_id> [on|off]'
  exit 2
fi
session_id=$1
marker=${PLAN_DELEGATE_ACTIVE_DIR:-/tmp/claude/delegate/active}/$session_id
if [[ ! -f $marker ]]; then
  print -u2 -r -- "no active unit run marker: $marker"
  exit 1
fi
session_dir=$(< "$marker")
if [[ -z $session_dir ]]; then
  print -u2 -r -- "empty unit run marker: $marker"
  exit 1
fi
run_id=${session_dir:t}

if (( $# == 2 )); then
  if [[ $2 == off ]]; then
    zsh "$REPO/scripts/message/notifier.sh" stop "delegate-$run_id" || exit $?
    print -r -- "progress updates off: delegate-$run_id"
  else
    next_due=$(zsh "$REPO/scripts/message/notifier.sh" start "delegate-$run_id") || exit $?
    print -r -- "progress updates on: delegate-$run_id $next_due"
  fi
  exit 0
fi

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
