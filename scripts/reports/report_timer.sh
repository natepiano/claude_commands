#!/usr/bin/env zsh
# Standing reports: a timer sends a session a prompt on a schedule, and the
# session runs the report and shows it. The showrunner's dailies timer
# (../production/showrunner_timer.sh) is the model; this one serves any report
# with no production behind it. /watcher runs `start` at every start and resume.
#
# One file per report, ~/.claude/config/reports/<name>.conf (REPORTS_CONFIG_DIR
# moves the folder); adding a report is adding a file and running `start`.
# KEY=VALUE per line, a line starting with # is a comment, values taken as
# written less one pair of surrounding quotes.
#   SESSION   required. The session to send to, as ListAgents prints its name.
#   WHEN      required. A systemd calendar spec: hourly, daily, *:0/30 ...
#             (`systemd-analyze calendar '<spec>'` checks one).
#   PROMPT    required. The exact message text.
#
# Each fire sends PROMPT through ../message/send.py as `report-<name>`, the
# sender the session sees, with key report-<name>: a session that is not
# reached queues only the latest fire. Each fire appends one line to
# ~/.local/state/reports/<name>.log.
#
# The timers are transient systemd --user units, report-<name>.timer. They
# outlive the session; a reboot removes them, and the next `start` brings them
# back. Linux only: it needs systemd.
#
# Usage: zsh report_timer.sh start|status [<name>...]
#        zsh report_timer.sh stop|fire <name>
#   start    make the timers match the config: start each report not running,
#            restart one whose WHEN changed, stop one whose file is gone.
#            Names limit it to those reports.
#   stop     stop one report's timer
#   status   every timer's next fire and the last lines of its log
#   fire     send one report now and log it

setopt no_unset pipe_fail extended_glob

SCRIPT=${0:A}
PY=${SCRIPT:h:h}/lib/py
SEND=${SCRIPT:h:h}/message/send.py
CONFIG_DIR=${REPORTS_CONFIG_DIR:-$HOME/.claude/config/reports}
LOG_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/reports
TIMEOUT=120

die() {
  print -u2 -r -- "report_timer.sh: $*"
  exit 2
}

[[ $(uname) == Linux ]] || die 'Linux only: the timers are systemd units'

typeset -A conf
# Reads report $1's file into conf; dies naming what is missing.
read_conf() {
  local file=$CONFIG_DIR/$1.conf line value key
  [[ -r $file ]] || die "no report named $1 ($file)"
  conf=()
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line == '#'* || $line != *=* ]] && continue
    value=${line#*=}
    [[ $value == \"*\" || $value == \'*\' ]] && value=${value[2,-2]}
    conf[${line%%=*}]=$value
  done < $file
  for key in SESSION WHEN PROMPT; do
    [[ -n ${conf[$key]:-} ]] || die "$key missing in $file"
  done
}

# Every report with a config file, by name.
names() {
  local file
  for file in $CONFIG_DIR/*.conf(N); do
    print -r -- ${file:t:r}
  done
}

# The description a running timer carries: it records WHEN, so `start` can
# tell a changed schedule.
description() {
  print -r -- "Standing report $1: ${conf[WHEN]} to ${conf[SESSION]}"
}

start_one() {
  local name=$1 unit=report-$1 zsh_bin
  read_conf $name
  systemd-analyze calendar ${conf[WHEN]} >/dev/null 2>&1 \
    || die "WHEN is not a calendar spec in $CONFIG_DIR/$name.conf: ${conf[WHEN]}"
  if systemctl --user is-active --quiet $unit.timer; then
    if [[ $(systemctl --user show -P Description $unit.timer) == "$(description $name)" ]]; then
      print -r -- "$name: runs"
      return 0
    fi
    stop_one $name >/dev/null
    print -r -- "$name: schedule changed, restarting"
  fi
  # A stopped or failed unit of the same name still blocks systemd-run.
  systemctl --user stop $unit.timer $unit.service 2>/dev/null
  systemctl --user reset-failed $unit.timer $unit.service 2>/dev/null
  # systemd --user has not the shell's PATH: zsh goes by absolute path.
  zsh_bin=$(whence -p zsh) || die 'no zsh found'
  systemd-run --user --quiet --unit=$unit --description="$(description $name)" \
    --on-calendar=${conf[WHEN]} --timer-property=AccuracySec=1s \
    --working-directory=$HOME \
    $zsh_bin $SCRIPT fire $name || die "systemd-run failed for $unit"
  print -r -- "$name: started, next $(systemctl --user show -P NextElapseUSecRealtime $unit.timer)"
}

stop_one() {
  local unit=report-$1
  if systemctl --user is-active --quiet $unit.timer; then
    systemctl --user stop $unit.timer || die "could not stop $unit.timer"
    print -r -- "$1: stopped"
  else
    print -r -- "$1: was not running"
  fi
  systemctl --user stop $unit.service 2>/dev/null
  systemctl --user reset-failed $unit.timer $unit.service 2>/dev/null
  return 0
}

cmd_start() {
  local name unit
  if (( $# )); then
    for name in $@; do start_one $name; done
    return 0
  fi
  for name in $(names); do start_one $name; done
  # A running timer whose file is gone.
  for unit in ${(f)"$(systemctl --user list-units --all --plain --no-legend 'report-*.timer' | awk '{print $1}')"}; do
    [[ -n $unit ]] || continue
    name=${${unit%.timer}#report-}
    [[ -e $CONFIG_DIR/$name.conf ]] || stop_one $name
  done
}

cmd_status() {
  local name
  (( $# )) || set -- $(names)
  (( $# )) || { print -r -- "no reports in $CONFIG_DIR"; return 0; }
  systemctl --user list-timers --all --no-pager report-${^@}.timer
  for name in $@; do
    print
    if [[ -r $LOG_DIR/$name.log ]]; then
      print -r -- "$name:"
      tail -n 3 $LOG_DIR/$name.log
    else
      print -r -- "$name: no fires logged yet"
    fi
  done
}

cmd_fire() {
  local name=$1 fired result rc
  fired="$(date '+%Y-%m-%d %H:%M:%S %Z')"
  mkdir -p $LOG_DIR
  read_conf $name
  result=$($PY $SEND --to ${conf[SESSION]} --from report-$name --summary "$name report" \
    --key report-$name --timeout $TIMEOUT --text ${conf[PROMPT]} 2>&1)
  rc=$?
  print -r -- "$fired | exit $rc | to ${conf[SESSION]} | ${(j: :)${(f)result}}" >> $LOG_DIR/$name.log
  (( rc == 0 ))
}

CMD=${1:-}
(( $# )) && shift
case $CMD in
  start) cmd_start $@ ;;
  status) cmd_status $@ ;;
  stop) (( $# == 1 )) || die 'usage: report_timer.sh stop <name>'; stop_one $1 ;;
  fire) (( $# == 1 )) || die 'usage: report_timer.sh fire <name>'; cmd_fire $1 ;;
  *) die 'usage: report_timer.sh start|status [<name>...], or stop|fire <name>' ;;
esac
