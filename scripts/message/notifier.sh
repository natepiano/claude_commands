#!/usr/bin/env zsh
# Schedule messages to live Claude sessions. The declared job runs `tick`.

setopt no_unset pipe_fail extended_glob
zmodload zsh/datetime
zmodload zsh/system
zmodload zsh/zselect

SCRIPT=${0:A}
PY=${SCRIPT:h:h}/lib/py
SEND=${SCRIPT:h}/send.py
SESSIONS=${SCRIPT:h}/sessions.py
STATE_DIR=${NOTIFIER_STATE_DIR:-$HOME/.local/state/notifier}
SESSIONS_DIR=${NOTIFIER_SESSIONS_DIR:-$HOME/.claude/sessions}
NOW=${NOTIFIER_NOW_EPOCH:-$EPOCHSECONDS}

die() { print -u2 -r -- "notifier.sh: $*"; exit 2 }
usage() { die 'usage: notifier.sh new <instance> --to <target> --every <min> (--command <text> | --prompt-file <path>) [--from <sender>] [--check <cmd>] [--hold] [--aligned] [--timeout <s>]; start|stop|status|fire|restart|remove|health <instance>; interval <instance> <min>; align <instance> on|off; status|tick' }

[[ $NOW == <0-> ]] || die "invalid clock: $NOW"

valid_name() { [[ $1 =~ '^[A-Za-z0-9][A-Za-z0-9._-]*$' ]] || usage }
valid_minutes() { [[ $1 == <1-> ]] || die "minutes must be a whole number above 0: $1" }
instance_dir() { print -r -- "$STATE_DIR/$1" }
require_instance() { [[ -d $STATE_DIR/$1 && -r $STATE_DIR/$1/conf && -r $STATE_DIR/$1/state ]] || { print -u2 -r -- "no such instance: $1"; return 1; } }

# The caller owns the instance lock. zsh's dynamic scope gives these helpers
# the caller's conf, state and dir variables.
read_conf() {
  conf=()
  local line
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line == *=* ]] && conf[${line%%=*}]=${line#*=}
  done < "$dir/conf"
}

read_state() {
  state=()
  local line
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line == *=* ]] && state[${line%%=*}]=${line#*=}
  done < "$dir/state"
}

write_conf() {
  local tmp="$dir/conf.$$.$RANDOM"
  {
    print -r -- "TARGET=${conf[TARGET]}"
    print -r -- "EVERY=${conf[EVERY]}"
    [[ -n ${conf[COMMAND]:-} ]] && print -r -- "COMMAND=${conf[COMMAND]}"
    [[ -n ${conf[PROMPT_FILE]:-} ]] && print -r -- "PROMPT_FILE=${conf[PROMPT_FILE]}"
    print -r -- "FROM=${conf[FROM]}"
    print -r -- "CHECK=${conf[CHECK]:-}"
    print -r -- "HOLD=${conf[HOLD]}"
    print -r -- "ALIGN=${conf[ALIGN]:-0}"
    print -r -- "TIMEOUT=${conf[TIMEOUT]}"
  } > "$tmp"
  mv -f -- "$tmp" "$dir/conf"
}

write_state() {
  local tmp="$dir/state.$$.$RANDOM"
  {
    print -r -- "ENABLED=${state[ENABLED]}"
    print -r -- "NEXT_DUE=${state[NEXT_DUE]}"
    print -r -- "LAST_SENT=${state[LAST_SENT]}"
    print -r -- "LAST_RESTART=${state[LAST_RESTART]}"
    print -r -- "LAST_TARGET=${state[LAST_TARGET]}"
  } > "$tmp"
  mv -f -- "$tmp" "$dir/state"
}

# An aligned instance fires on the local clock's multiples of EVERY (every 60:
# on the hour), and skips a slot less than half an interval away.
schedule() {
  local period=$(( conf[EVERY] * 60 )) offset local_now
  if [[ ${conf[ALIGN]:-0} == 1 ]]; then
    offset=$(utc_offset)
    local_now=$(( NOW + offset ))
    state[NEXT_DUE]=$(( local_now - local_now % period + period - offset ))
    (( state[NEXT_DUE] - NOW < period / 2 )) && state[NEXT_DUE]=$(( state[NEXT_DUE] + period ))
  else
    state[NEXT_DUE]=$(( NOW - NOW % 60 + period ))
  fi
  write_state
}

utc_offset() {
  local zone=$(strftime %z $NOW)
  local seconds=$(( ${zone[2,3]} * 3600 + ${zone[4,5]} * 60 ))
  [[ ${zone[1]} == - ]] && seconds=$(( -seconds ))
  print -r -- $seconds
}

local_time() { strftime "$1" "$2" }
next_line() { print -r -- "next_due=${state[NEXT_DUE]} ($(local_time '%Y-%m-%d %H:%M %Z' ${state[NEXT_DUE]}))" }
time_line() {
  if (( $2 == 0 )); then
    print -r -- "$1=0 (never)"
  else
    print -r -- "$1=$2 ($(local_time '%Y-%m-%d %H:%M:%S %Z' $2))"
  fi
}
fired_stamp() {
  print -r -- "$(local_time '%Y-%m-%d %H:%M:%S %Z' $NOW) / $(TZ=UTC strftime '%H:%M:%S' $NOW) UTC"
}
log_skip() { print -r -- "$fired | skip $1" >> "$dir/fire.log" }
log_error() { print -r -- "$fired | $1" >> "$STATE_DIR/notifier.log" }

cmd_new() {
  (( $# >= 2 )) || usage
  local name=$1 dir lock_fd target='' every='' command='' prompt_file='' sender='' check='' hold=0 align=0 timeout=120
  local command_given=0 prompt_given=0
  shift
  valid_name "$name"
  while (( $# )); do
    case $1 in
      --to|--every|--command|--prompt-file|--from|--check|--timeout)
        (( $# >= 2 )) || usage
        case $1 in
          --to) target=$2 ;;
          --every) every=$2 ;;
          --command) command=$2; command_given=1 ;;
          --prompt-file) prompt_file=$2; prompt_given=1 ;;
          --from) sender=$2 ;;
          --check) check=$2 ;;
          --timeout) timeout=$2 ;;
        esac
        shift 2 ;;
      --hold) hold=1; shift ;;
      --aligned) align=1; shift ;;
      *) usage ;;
    esac
  done
  [[ -n $target ]] || usage
  valid_minutes "$every"
  (( command_given + prompt_given == 1 )) || usage
  (( command_given )) && [[ -z $command ]] && usage
  (( prompt_given )) && [[ -z $prompt_file ]] && usage
  [[ $timeout == <1-> ]] || die "timeout must be whole seconds above 0: $timeout"
  [[ -z $prompt_file || $prompt_file == /* ]] || die 'prompt file must be absolute'
  [[ -n $sender ]] || sender=$name
  dir=$(instance_dir "$name")
  mkdir -p -- "$dir" || die "cannot create $dir"
  [[ -e $dir/lock ]] || : > "$dir/lock"
  zsystem flock -f lock_fd "$dir/lock" || die "cannot lock $name"
  typeset -A conf state
  conf=(TARGET "$target" EVERY "$every" FROM "$sender" CHECK "$check" HOLD "$hold" ALIGN "$align" TIMEOUT "$timeout")
  (( command_given )) && conf[COMMAND]=$command
  (( prompt_given )) && conf[PROMPT_FILE]=$prompt_file
  write_conf
  if [[ ! -e $dir/state ]]; then
    state=(ENABLED 1 NEXT_DUE 0 LAST_SENT 0 LAST_RESTART 0 LAST_TARGET '')
    schedule
    next_line
  fi
  zsystem flock -u "$lock_fd"
}

cmd_state() {
  local action=$1 name=$2 dir lock_fd
  valid_name "$name"
  [[ $action == interval ]] && valid_minutes "$3"
  require_instance "$name" || return 1
  dir=$(instance_dir "$name")
  zsystem flock -f lock_fd "$dir/lock" || die "cannot lock $name"
  typeset -A conf state
  read_conf
  read_state
  case $action in
    start) state[ENABLED]=1; schedule; next_line ;;
    stop) state[ENABLED]=0; write_state ;;
    restart) state[LAST_RESTART]=$NOW; schedule; next_line ;;
    interval)
      conf[EVERY]=$3
      write_conf
      state[LAST_RESTART]=$NOW
      schedule
      next_line ;;
    align)
      conf[ALIGN]=$3
      write_conf
      state[LAST_RESTART]=$NOW
      schedule
      next_line ;;
  esac
  zsystem flock -u "$lock_fd"
}

# Claim before checking or sending. A concurrent tick or manual fire cannot
# claim the same scheduled slot twice.
instance_tick() {
  local name=$1 forced=$2 show_next=$3 dir lock_fd fired result rc socket
  local check_pid watchdog_pid timeout_marker
  local -a check_words send_args
  typeset -A conf state
  dir=$(instance_dir "$name")
  [[ -r $dir/conf && -r $dir/state ]] || return 0
  zsystem flock -f lock_fd "$dir/lock" || { fired=$(fired_stamp); log_error "lock failed: $name"; return 1; }
  read_conf
  read_state
  if (( ! forced )) && { [[ ${state[ENABLED]:-0} != 1 ]] || (( NOW < ${state[NEXT_DUE]:-0} )); }; then
    zsystem flock -u "$lock_fd"
    return 0
  fi
  schedule
  (( show_next )) && next_line
  zsystem flock -u "$lock_fd"
  fired=$(fired_stamp)

  if [[ -n ${conf[CHECK]:-} ]]; then
    check_words=(${(Q)${(z)conf[CHECK]}})
    timeout_marker="$dir/.check-timeout.$$.$RANDOM"
    "${check_words[@]}" &
    check_pid=$!
    (
      zselect -t "$(( conf[TIMEOUT] * 100 ))" >/dev/null 2>&1
      if kill -0 "$check_pid" 2>/dev/null; then
        : > "$timeout_marker"
        kill -TERM "$check_pid" 2>/dev/null
      fi
    ) &
    watchdog_pid=$!
    wait "$check_pid"
    rc=$?
    kill -TERM "$watchdog_pid" 2>/dev/null
    wait "$watchdog_pid" 2>/dev/null
    if [[ -e $timeout_marker ]]; then
      rm -f -- "$timeout_marker"
      log_skip 'check timeout'
      return 0
    fi
    if (( rc == 2 )); then
      rm -rf -- "$dir"
      log_error "removed $name (check exit 2)"
      return 0
    fi
    if (( rc != 0 )); then
      log_skip "check exit $rc"
      return 0
    fi
  fi

  socket=$(NOTIFIER_SESSIONS_DIR="$SESSIONS_DIR" "$PY" "$SESSIONS" socket "${conf[TARGET]}" 2>/dev/null)
  if [[ -z $socket ]]; then
    log_skip 'session not running'
    return 0
  fi

  if (( ! forced )) && [[ ${conf[HOLD]:-0} == 1 ]] && (( ${state[LAST_SENT]:-0} > ${state[LAST_RESTART]:-0} )); then
    if [[ $socket != ${state[LAST_TARGET]:-} ]]; then
      print -r -- "$fired | hold released: socket changed" >> "$dir/fire.log"
    elif (( NOW - ${state[LAST_SENT]:-0} >= 2 * conf[EVERY] * 60 )); then
      print -r -- "$fired | hold released: two intervals" >> "$dir/fire.log"
    else
      log_skip hold
      return 0
    fi
  fi

  zsystem flock -f lock_fd "$dir/lock" || { log_error "lock failed: $name"; return 1; }
  read_state
  state[LAST_SENT]=$NOW
  state[LAST_TARGET]=$socket
  write_state
  zsystem flock -u "$lock_fd"

  send_args=(--to "uds:$socket" --from "${conf[FROM]}" --key "notifier-$name" --summary 'scheduled update' --timeout "${conf[TIMEOUT]}")
  if [[ -n ${conf[COMMAND]:-} ]]; then
    send_args+=(--text "${conf[COMMAND]}")
  else
    send_args+=(--file "${conf[PROMPT_FILE]}")
  fi
  if [[ -n ${NOTIFIER_SEND:-} ]]; then
    result=$("$NOTIFIER_SEND" "${send_args[@]}" 2>&1)
  else
    result=$("$PY" "$SEND" "${send_args[@]}" 2>&1)
  fi
  rc=$?
  print -r -- "$fired | exit $rc | to ${conf[TARGET]} uds:$socket | ${(j: :)${(f)result}}" >> "$dir/fire.log"
  return 0
}

last_tick_line() {
  local last=0
  [[ -r $STATE_DIR/.last_tick ]] && last=$(<"$STATE_DIR/.last_tick")
  if [[ $last == <1-> ]]; then
    print -r -- "last_tick=$last ($(( NOW - last )) s ago)"
  else
    print -r -- 'last_tick=0 (never)'
  fi
}

cmd_status() {
  local name=$1 dir
  typeset -A conf state
  valid_name "$name"
  require_instance "$name" || return 1
  dir=$(instance_dir "$name")
  read_conf
  read_state
  local mode=stopped
  [[ ${state[ENABLED]:-0} == 1 ]] && mode=enabled
  local aligned=''
  [[ ${conf[ALIGN]:-0} == 1 ]] && aligned=' on the clock'
  print -r -- "$name → ${conf[TARGET]} every ${conf[EVERY]} min$aligned, $mode"
  next_line
  time_line last_sent "${state[LAST_SENT]}"
  time_line last_restart "${state[LAST_RESTART]}"
  last_tick_line
  [[ -r $dir/fire.log ]] && tail -n 5 -- "$dir/fire.log"
  return 0
}

cmd_status_all() {
  local dir name mode
  typeset -A conf state
  for dir in "$STATE_DIR"/*(/N); do
    [[ -r $dir/conf && -r $dir/state ]] || continue
    name=${dir:t}
    read_conf
    read_state
    mode=stopped
    [[ ${state[ENABLED]:-0} == 1 ]] && mode=enabled
    print -r -- "$name every ${conf[EVERY]} min next $(local_time '%H:%M' ${state[NEXT_DUE]}) $mode"
  done
  last_tick_line
}

cmd_health() {
  local name=$1 dir last=0 line rc previous='' latest=''
  typeset -A state
  valid_name "$name"
  if ! require_instance "$name" 2>/dev/null; then
    print -r -- 'failing: no instance'
    return 1
  fi
  dir=$(instance_dir "$name")
  read_state
  [[ -r $STATE_DIR/.last_tick ]] && last=$(<"$STATE_DIR/.last_tick")
  if [[ $last != <1-> ]] || (( NOW - last > 120 )); then
    if [[ $last == <1-> ]]; then
      print -r -- "failing: no tick since $(local_time '%Y-%m-%d %H:%M:%S %Z' $last)"
    else
      print -r -- 'failing: no tick since never'
    fi
    return 1
  fi
  if [[ ${state[ENABLED]:-0} != 1 ]]; then
    print -r -- 'ok: stopped'
    return 0
  fi
  if [[ -r $dir/fire.log ]]; then
    while IFS= read -r line; do
      if [[ $line =~ '\| exit ([0-9]+) \|' ]]; then
        previous=$latest
        latest=$match[1]
      fi
    done < "$dir/fire.log"
  fi
  if [[ -n $previous && -n $latest ]] && (( previous != 0 && latest != 0 )); then
    print -r -- "failing: last two sends exit $previous, $latest"
    return 1
  fi
  print -r -- ok
}

cmd_tick() {
  local tick_fd dir name
  typeset -A state
  [[ -d $STATE_DIR ]] || mkdir -p -- "$STATE_DIR" || return 0
  [[ -e $STATE_DIR/.tick.lock ]] || : > "$STATE_DIR/.tick.lock"
  zsystem flock -t 0 -f tick_fd "$STATE_DIR/.tick.lock" || return 0
  print -r -- "$NOW" > "$STATE_DIR/.last_tick"
  for dir in "$STATE_DIR"/*(/N); do
    [[ -r $dir/conf && -r $dir/state ]] || continue
    read_state
    [[ ${state[ENABLED]:-0} == 1 && ${state[NEXT_DUE]:-0} == <0-> ]] || continue
    (( NOW >= state[NEXT_DUE] )) || continue
    name=${dir:t}
    ( instance_tick "$name" 0 0 || { fired=$(fired_stamp); log_error "tick failed: $name"; } ) &
  done
  wait
  zsystem flock -u "$tick_fd"
  return 0
}

(( $# >= 1 )) || usage
action=$1
shift
case $action in
  new) cmd_new "$@" ;;
  start|stop|restart)
    (( $# == 1 )) || usage
    cmd_state "$action" "$1" ;;
  interval)
    (( $# == 2 )) || usage
    cmd_state interval "$1" "$2" ;;
  align)
    (( $# == 2 )) || usage
    case $2 in
      on) cmd_state align "$1" 1 ;;
      off) cmd_state align "$1" 0 ;;
      *) usage ;;
    esac ;;
  fire)
    (( $# == 1 )) || usage
    valid_name "$1"
    require_instance "$1" || exit 1
    instance_tick "$1" 1 1 ;;
  remove)
    (( $# == 1 )) || usage
    valid_name "$1"
    rm -rf -- "$(instance_dir "$1")" ;;
  status)
    (( $# <= 1 )) || usage
    if (( $# )); then cmd_status "$1"; else cmd_status_all; fi ;;
  health)
    (( $# == 1 )) || usage
    cmd_health "$1" ;;
  tick)
    (( $# == 0 )) || usage
    cmd_tick ;;
  *) usage ;;
esac
