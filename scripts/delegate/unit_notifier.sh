#!/usr/bin/env zsh
# Make or retarget the notifier for one unit run, or retime every unit run's.

setopt no_unset pipe_fail extended_glob

SCRIPT=${0:A}
REPO=${SCRIPT:h:h:h}

usage='usage: unit_notifier.sh <claude_session_id> [on|off] | --all [minutes]'
(( $# == 1 || $# == 2 )) || { print -u2 -r -- $usage; exit 2; }
if [[ $1 == --all ]]; then
  (( $# == 1 )) || [[ $2 == <1-> ]] || { print -u2 -r -- $usage; exit 2; }
elif (( $# == 2 )) && [[ $2 != on && $2 != off ]]; then
  print -u2 -r -- $usage
  exit 2
fi
notifier=$REPO/scripts/message/notifier.sh

# The leading digits, as progress_timer.sh reads them, so a trailing comment
# leaves the interval alone. PLAN_DELEGATE_PROGRESS_UPDATES=on-demand, or no
# line, gives a new run no schedule: /unit:report runs only when someone asks
# for it, and `on` makes the schedule then. Only `scheduled` starts one.
seconds=900
updates=on-demand
config=${PLAN_DELEGATE_CONFIG:-$HOME/.claude/config/delegate.conf}
if [[ -r $config ]]; then
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line == (#b)PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=([0-9]##)(|[^0-9]*) ]] && seconds=$match[1]
    [[ $line == (#b)PLAN_DELEGATE_PROGRESS_UPDATES=([a-z-]##)(|[^a-z-]*) ]] && updates=$match[1]
  done < "$config"
fi
minutes=$(( (seconds + 59) / 60 ))
(( minutes >= 1 )) || minutes=1

# Retime every unit run's schedule at once, to the configured interval unless
# given one. `interval` leaves a stopped schedule stopped.
if [[ $1 == --all ]]; then
  (( $# == 2 )) && minutes=$2
  failed=0
  dirs=(${NOTIFIER_STATE_DIR:-$HOME/.local/state/notifier}/delegate-*(/N))
  (( $#dirs )) || print -r -- 'no unit run has a report schedule'
  for dir in $dirs; do
    [[ -r $dir/conf && -r $dir/state ]] || continue
    if ! next_due=$(zsh "$notifier" interval "${dir:t}" "$minutes"); then
      failed=1
      continue
    fi
    if [[ -n ${(M)${(f)"$(< $dir/state)"}:#ENABLED=1} ]]; then
      print -r -- "${dir:t} every $minutes min, on, $next_due"
    else
      print -r -- "${dir:t} every $minutes min, off"
    fi
  done
  exit $failed
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
instance=delegate-$run_id

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
  # A resumed run keeps the schedule it has, on or off: `new` retargets it and
  # leaves its clock alone (user, 2026-10-13: existing work keeps what it has).
  if zsh "$notifier" status "$instance" >/dev/null 2>&1; then
    make_instance
    exit $?
  fi
  print -r -- "progress updates on demand: $instance has no schedule; /unit:report on starts one"
  exit 0
fi
make_instance
