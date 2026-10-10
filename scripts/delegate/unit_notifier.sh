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
notifier=$REPO/scripts/message/notifier.sh
instance=delegate-$run_id

# The leading digits, as progress_timer.sh reads them, so a trailing comment
# leaves the interval alone. PLAN_DELEGATE_PROGRESS_UPDATES=on-demand gives a
# new run no schedule: /unit:report runs only when someone asks for it, and
# `on` makes the schedule then.
seconds=900
updates=scheduled
config=${PLAN_DELEGATE_CONFIG:-$HOME/.claude/config/delegate.conf}
if [[ -r $config ]]; then
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line == (#b)PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=([0-9]##)(|[^0-9]*) ]] && seconds=$match[1]
    [[ $line == (#b)PLAN_DELEGATE_PROGRESS_UPDATES=([a-z-]##)(|[^a-z-]*) ]] && updates=$match[1]
  done < "$config"
fi
minutes=$(( (seconds + 59) / 60 ))
(( minutes >= 1 )) || minutes=1

make_instance() {
  local check_words=("$REPO/scripts/lib/py" "$REPO/scripts/hooks/delegate_run.py" check "$session_id" "$session_dir")
  zsh "$notifier" new "$instance" \
    --to "session:$session_id" --every "$minutes" \
    --command '/unit:report' --check "${(j: :)${(q)check_words[@]}}" --hold
}

if (( $# == 2 )); then
  if [[ $2 == off ]]; then
    zsh "$notifier" stop "$instance" || exit $?
    print -r -- "progress updates off: $instance"
  elif zsh "$notifier" status "$instance" >/dev/null 2>&1; then
    next_due=$(zsh "$notifier" start "$instance") || exit $?
    print -r -- "progress updates on: $instance $next_due"
  else
    # An on-demand run has no instance until it is asked for one.
    next_due=$(make_instance) || exit $?
    print -r -- "progress updates on: $instance ${(M)${(f)next_due}:#next_due=*}"
  fi
  exit 0
fi

if [[ $updates == on-demand ]]; then
  # A resumed run may still hold the schedule an earlier start made.
  if zsh "$notifier" status "$instance" >/dev/null 2>&1; then
    zsh "$notifier" stop "$instance" >/dev/null || exit $?
  fi
  print -r -- "progress updates on demand: $instance has no schedule; /unit:report on starts one"
  exit 0
fi
make_instance
