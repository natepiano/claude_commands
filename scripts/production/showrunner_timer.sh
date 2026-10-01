#!/usr/bin/env zsh
# The showrunner's scheduled update, sent from outside its session.
# A systemd --user timer runs `fire` every N minutes. Each fire sends the prompt
# file's text to the showrunner through ~/.claude/scripts/message/send.py, as
# the sender UNIT. The showrunner queues the message until it can next take it,
# as it does any peer message.
#
# Why not Claude Code's own cron (CronCreate): its ticks came about four minutes
# after their slot while the session sat idle. This timer fires on the second.
# Linux only: it needs systemd.
#
# The production doc is the configuration. Each fire reads two of its lines:
#   > **Status: PRODUCTION — <planned | running | wrapped>.**
#       Only `running` sends. On `wrapped` the fire stops this timer.
#   - **Showrunner session:** <name>
#       The send target: the session's name as ListAgents prints it. The
#       showrunner writes it at start and on every resume, so a resumed session
#       is found with no restart here.
# `start` reads a third, `- **Updates:** every <N> minutes` (15 when absent).
# A change to it takes effect at the next `start`; `interval` makes the change
# and restarts.
#
# The timer lives as long as the production, not the session: exiting the
# showrunner leaves it running. A reboot removes it, so the showrunner runs
# `start` at every start and resume; `start` does nothing while it runs.
# Each production has its own timer, config and log, so several showrunners in
# different projects run at once.
#
# Usage: zsh showrunner_timer.sh start|stop|status|fire <conf>
#        zsh showrunner_timer.sh interval <conf> <minutes>
#   start     start this production's timer; does nothing while it runs
#   stop      stop this production's timer and any fire in progress
#   status    the doc's lines, the timer's next fire and the last lines of LOG
#   fire      send the message once, now, and log it
#   interval  set the update interval in the doc's **Updates:** line and the
#             prompt file, then restart the timer: the next tick comes
#             <minutes> from now
#
# Config: one per production, at ~/.local/state/showrunner/<slug>/timer.conf,
# where <slug> is the production doc's file name less `-production.md`. One
# KEY=VALUE per line; a line starting with # is a comment. Values are taken as
# written, less one pair of surrounding quotes. A relative path is relative to
# the config's directory.
#   PRODUCTION_DOC   required. The production doc's absolute path.
#   PROMPT_FILE      the exact message text. Default prompt.txt.
#   UNIT             the transient unit name. Default showrunner-timer-<slug>.
#                    It is the sender name, so the showrunner sees the message
#                    from "<UNIT>".
#   LOG              the fire log. Default fire.log.
#   TIMEOUT          seconds send.py gives its relay before it stops it and
#                    queues the message. Default 120; a fire takes about 5.
#
# Each fire appends one line to LOG: the fire time (local and UTC), send.py's
# exit code, the target, and send.py's outcome line, which says whether the
# text went out word for word. A fire that sends nothing logs why. The relay's
# last stream stays in ~/.local/state/message/relay/<target>.jsonl.

setopt no_unset pipe_fail

SCRIPT=${0:A}
PY=${SCRIPT:h:h}/lib/py
SEND=${SCRIPT:h:h}/message/send.py

die() {
  print -u2 -r -- "showrunner_timer.sh: $*"
  exit 2
}

USAGE='usage: showrunner_timer.sh start|stop|status|fire <conf>, or interval <conf> <minutes>'
if [[ ${1:-} == interval ]]; then
  (( $# == 3 )) || die $USAGE
else
  (( $# == 2 )) || die $USAGE
fi
CMD=$1
CONF=${2:A}
[[ -r $CONF ]] || die "config not readable: $CONF"

typeset -A conf
while IFS= read -r line || [[ -n $line ]]; do
  [[ $line == '#'* || $line != *=* ]] && continue
  value=${line#*=}
  [[ $value == \"*\" || $value == \'*\' ]] && value=${value[2,-2]}
  conf[${line%%=*}]=$value
done < $CONF

# Prints config path $1 made absolute against the config's directory.
conf_path() {
  [[ $1 == /* ]] && print -r -- $1 || print -r -- ${CONF:h}/$1
}

PRODUCTION_DOC=${conf[PRODUCTION_DOC]:-}
[[ -n $PRODUCTION_DOC ]] || die "PRODUCTION_DOC missing in $CONF"
PRODUCTION_DOC=$(conf_path $PRODUCTION_DOC)
SLUG=${${PRODUCTION_DOC:t:r}%-production}
SLUG=${SLUG//[^A-Za-z0-9_.-]/-}
PROMPT_FILE=$(conf_path ${conf[PROMPT_FILE]:-prompt.txt})
UNIT=${conf[UNIT]:-showrunner-timer-$SLUG}
LOG=$(conf_path ${conf[LOG]:-fire.log})
TIMEOUT=${conf[TIMEOUT]:-120}
[[ $TIMEOUT == <1-> ]] || die "TIMEOUT must be whole seconds: $TIMEOUT"

# Reads the production doc's status, showrunner session and update interval
# into doc_status, doc_session and doc_interval. On failure, doc_error says why.
doc_status=unknown
doc_session=
doc_interval=15
doc_error=
read_doc() {
  setopt local_options extended_glob
  local line
  if [[ ! -r $PRODUCTION_DOC ]]; then
    doc_error="production doc not readable: $PRODUCTION_DOC"
    return 1
  fi
  for line in "${(@f)$(<$PRODUCTION_DOC)}"; do
    case $line in
      (*'Status: PRODUCTION — '*)
        doc_status=${${line#*'Status: PRODUCTION — '}%%[^a-z]*} ;;
      (*'**Showrunner session:**'*)
        line=${${line#*'**Showrunner session:**'}%% — *}
        line=${line//\`/}
        doc_session=${${line##[[:space:]]#}%%[[:space:]]#} ;;
      (*'**Updates:**'*)
        [[ $line =~ 'every ([0-9]+) min' ]] && doc_interval=$match[1] ;;
    esac
  done
}

cmd_start() {
  [[ -r $PROMPT_FILE ]] || die "PROMPT_FILE not readable: $PROMPT_FILE"
  read_doc || die $doc_error
  [[ $doc_status == running ]] || die "the production is $doc_status, not running: $PRODUCTION_DOC"
  [[ -n $doc_session ]] || die "no **Showrunner session:** line in $PRODUCTION_DOC"
  if systemctl --user is-active --quiet $UNIT.timer; then
    local owner
    owner=$(systemctl --user show -P Description $UNIT.timer)
    [[ $owner == *"$CONF" ]] || die "$UNIT.timer belongs to another production ($owner); set UNIT in $CONF"
    print -r -- "$UNIT.timer already runs"
    cmd_status
    return 0
  fi
  # A stopped or failed unit of the same name still blocks systemd-run.
  systemctl --user stop $UNIT.timer $UNIT.service 2>/dev/null
  systemctl --user reset-failed $UNIT.timer $UNIT.service 2>/dev/null
  # systemd --user has not the shell's PATH: zsh goes by absolute path, and
  # send.py finds python and claude itself.
  local zsh_bin
  zsh_bin=$(whence -p zsh) || die 'no zsh found'
  # The first tick comes N minutes after start, and each next one N minutes
  # after the last began.
  systemd-run --user --unit=$UNIT \
    --description="Showrunner update timer for $CONF" \
    --on-active=${doc_interval}min --on-unit-active=${doc_interval}min \
    --timer-property=AccuracySec=1s \
    --working-directory=$HOME \
    $zsh_bin $SCRIPT fire $CONF || die "systemd-run failed for $UNIT"
  cmd_status
}

cmd_stop() {
  if systemctl --user is-active --quiet $UNIT.timer; then
    systemctl --user stop $UNIT.timer || die "could not stop $UNIT.timer"
    print -r -- "$UNIT.timer stopped"
  else
    print -r -- "$UNIT.timer was not running"
  fi
  # Stopping the timer leaves a fire in progress running.
  if systemctl --user is-active --quiet $UNIT.service; then
    systemctl --user stop $UNIT.service
  fi
  systemctl --user reset-failed $UNIT.timer $UNIT.service 2>/dev/null
  return 0
}

cmd_status() {
  if read_doc; then
    print -r -- "production $doc_status; showrunner session ${doc_session:-none}; updates every $doc_interval min"
  else
    print -r -- $doc_error
  fi
  systemctl --user list-timers --all --no-pager $UNIT.timer
  print
  if [[ -r $LOG ]]; then
    tail -n 5 $LOG
  else
    print -r -- "no fires logged yet ($LOG)"
  fi
}

# Sets the update interval to $1 minutes. The doc's **Updates:** line holds it
# and the prompt file says it, so both change; then the timer restarts, so the
# next tick comes $1 minutes from now.
cmd_interval() {
  local minutes=$1 old
  [[ $minutes == <1-> ]] || die "minutes must be a whole number above 0: $minutes"
  [[ -r $PROMPT_FILE ]] || die "PROMPT_FILE not readable: $PROMPT_FILE"
  read_doc || die $doc_error
  [[ $doc_status == running ]] || die "the production is $doc_status, not running: $PRODUCTION_DOC"
  grep -Eq '\*\*Updates:\*\* every [0-9]+ minutes' $PRODUCTION_DOC \
    || die "no '**Updates:** every <N> minutes' line in $PRODUCTION_DOC"
  grep -Eq 'every [0-9]+ minutes' $PROMPT_FILE \
    || die "no 'every <N> minutes' in $PROMPT_FILE"
  old=$doc_interval
  sed -i -E "s/(\*\*Updates:\*\* every )[0-9]+ minutes/\1$minutes minutes/" $PRODUCTION_DOC \
    || die "could not edit $PRODUCTION_DOC"
  sed -i -E "0,/every [0-9]+ minutes/s//every $minutes minutes/" $PROMPT_FILE \
    || die "could not edit $PROMPT_FILE"
  print -r -- "updates every $old min -> every $minutes min"
  cmd_stop
  cmd_start
}

cmd_fire() {
  local fired result rc
  fired="$(date '+%Y-%m-%d %H:%M:%S %Z') / $(date -u '+%H:%M:%S UTC')"
  mkdir -p ${LOG:h}

  if ! read_doc; then
    print -r -- "$fired | $doc_error | nothing sent" >> $LOG
    return 1
  fi
  case $doc_status in
    (wrapped)
      # This fire runs in the service, which ends when it exits.
      systemctl --user stop $UNIT.timer 2>/dev/null
      systemctl --user reset-failed $UNIT.timer 2>/dev/null
      print -r -- "$fired | production wrapped | $UNIT.timer stopped | nothing sent" >> $LOG
      return 0 ;;
    (running) ;;
    (*)
      print -r -- "$fired | production $doc_status | nothing sent" >> $LOG
      return 0 ;;
  esac
  if [[ -z $doc_session ]]; then
    print -r -- "$fired | no **Showrunner session:** line in $PRODUCTION_DOC | nothing sent" >> $LOG
    return 1
  fi
  if [[ ! -r $PROMPT_FILE ]]; then
    print -r -- "$fired | PROMPT_FILE not readable: $PROMPT_FILE | nothing sent" >> $LOG
    return 1
  fi

  result=$($PY $SEND --to $doc_session --from $UNIT --summary 'scheduled update' \
    --timeout $TIMEOUT --file $PROMPT_FILE 2>&1)
  rc=$?
  print -r -- "$fired | exit $rc | to $doc_session | ${(j: :)${(f)result}}" >> $LOG
  (( rc == 0 ))
}

case $CMD in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  fire) cmd_fire ;;
  interval) cmd_interval $3 ;;
  *) die "unknown subcommand: $CMD (start, stop, status, fire or interval)" ;;
esac
