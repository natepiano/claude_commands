#!/usr/bin/env zsh
# The showrunner's scheduled update, sent from outside its session.
# A systemd --user timer runs `fire` every N minutes. Each fire starts a
# headless Claude that sends the prompt file's text to the showrunner with
# SendMessage, then exits. The showrunner queues the message until it can next
# take it, as it does any peer message.
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
#   MODEL            the sender's model. Default sonnet. Not haiku: haiku cannot
#                    run in auto mode, and Claude Code starts it in default mode
#                    without a word (measured 2026-09-29, Claude Code 2.1.284).
#   UNIT             the transient unit name. Default showrunner-timer-<slug>.
#                    The sender takes it as its session name, so the showrunner
#                    sees the message from "<UNIT>".
#   LOG              the fire log. Default fire.log.
#   TIMEOUT          seconds the headless Claude may run before the fire kills it
#                    and logs a timeout. Default 120; a fire takes about 5.
#   PERMISSION_MODE  the sender's permission mode. Default auto. It must match
#                    the showrunner's mode: a session holds a message from a
#                    sender in another mode for its user's approval.
#
# Each fire appends one line to LOG: the fire time (local and UTC), claude's
# exit code, the sender's actual permission mode, the send status, whether the
# text went out word for word, and the SendMessage result. A fire that sends
# nothing logs why. The last fire's raw stream stays in LOG.last.jsonl.

setopt no_unset pipe_fail

SCRIPT=${0:A}

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
MODEL=${conf[MODEL]:-sonnet}
UNIT=${conf[UNIT]:-showrunner-timer-$SLUG}
LOG=$(conf_path ${conf[LOG]:-fire.log})
TIMEOUT=${conf[TIMEOUT]:-120}
PERMISSION_MODE=${conf[PERMISSION_MODE]:-auto}
[[ $TIMEOUT == <1-> ]] || die "TIMEOUT must be whole seconds: $TIMEOUT"

# Refuses a model that cannot run in the configured mode (MODEL above).
check_mode() {
  if [[ $PERMISSION_MODE == auto && $MODEL == *haiku* ]]; then
    die "MODEL $MODEL cannot run in auto mode; the showrunner would hold every message"
  fi
}

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

# Never the bare name `claude`: the interactive shell aliases it to add
# --remote-control. The ~/.local/bin link follows Claude Code's own updates.
find_claude() {
  if [[ -x $HOME/.local/bin/claude ]]; then
    print -r -- $HOME/.local/bin/claude
  else
    whence -p claude
  fi
}

cmd_start() {
  [[ -r $PROMPT_FILE ]] || die "PROMPT_FILE not readable: $PROMPT_FILE"
  check_mode
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
  # systemd --user has neither the shell's aliases nor its PATH: pass every
  # program the fire needs as an absolute path.
  local claude_bin jq_bin zsh_bin
  claude_bin=$(find_claude) || die 'no claude binary found'
  jq_bin=$(whence -p jq) || die 'no jq found'
  zsh_bin=$(whence -p zsh) || die 'no zsh found'
  # The first tick comes N minutes after start, and each next one N minutes
  # after the last began.
  systemd-run --user --unit=$UNIT \
    --description="Showrunner update timer for $CONF" \
    --on-active=${doc_interval}min --on-unit-active=${doc_interval}min \
    --timer-property=AccuracySec=1s \
    --working-directory=$HOME \
    --setenv=SHOWRUNNER_TIMER_CLAUDE=$claude_bin \
    --setenv=SHOWRUNNER_TIMER_JQ=$jq_bin \
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
  local fired claude_bin jq_bin target text tools prompt raw rc timed_out summary
  local mode sent_status sends verbatim denials result
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
  check_mode
  target=$doc_session
  claude_bin=${SHOWRUNNER_TIMER_CLAUDE:-$(find_claude)}
  jq_bin=${SHOWRUNNER_TIMER_JQ:-$(whence -p jq)}
  raw=$LOG.last.jsonl
  text=$(<$PROMPT_FILE)

  # The name form may need ListAgents for the [ref] when the bare name fails.
  if [[ $target == uds:* ]]; then
    tools=SendMessage
  else
    tools=SendMessage,ListAgents
  fi
  prompt="You are a timer that delivers one message. Make exactly one SendMessage call:
- to: $target
- summary: scheduled update
- message: the text between the BEGIN and END lines below, copied exactly. Keep every character, space, line break, backtick and placeholder as written. Add nothing, drop nothing, reword nothing. Leave out the BEGIN and END lines."
  if [[ $tools == *ListAgents ]]; then
    prompt+="
If that call fails because the name matched no session or more than one, call ListAgents once, then make the call once more with \`to\` set to the row whose name is exactly \"$target\", followed by its [ref]."
  fi
  prompt+="
Then stop: reply with one line, the SendMessage result. Call no other tool.
----- BEGIN MESSAGE -----
$text
----- END MESSAGE -----"

  # A CLAUDE_* variable inherited from a session would make the sender pass for
  # that session: CLAUDE_CODE_MESSAGING_SOCKET, for one, is its inbox.
  unset -m 'CLAUDE*'
  # Hooks off: the user's Stop and PostToolUse hooks can hold or extend a turn.
  timeout --kill-after=10 $TIMEOUT $claude_bin -p \
    --model $MODEL --permission-mode $PERMISSION_MODE --permission-prompts none \
    --tools $tools --allowedTools $tools \
    --setting-sources user --settings '{"disableAllHooks": true}' \
    --strict-mcp-config --disable-slash-commands --no-session-persistence \
    -n $UNIT --output-format stream-json --verbose \
    <<<"$prompt" >$raw 2>&1
  rc=$?
  # 124: timeout stopped it; 137: it ignored that and was killed.
  timed_out=
  (( rc == 124 || rc == 137 )) && timed_out=" (TIMED OUT after ${TIMEOUT}s)"

  # Read line by line: stderr shares the file, and one non-JSON line would
  # sink a whole-file parse.
  summary=$($jq_bin -Rrn --rawfile expected $PROMPT_FILE '
    def trim_end: sub("\\s+$"; "");
    def one_line: gsub("[\\t\\r\\n]+"; " ");
    [inputs | fromjson? | objects] as $events
    | [$events[] | select(.type == "assistant") | .message.content[]?
      | select(.type == "tool_use" and .name == "SendMessage")] as $sends
    | [$events[] | select(.type == "user") | .message.content[]?
      | select(.type == "tool_result")] as $results
    | ([$events[] | select(.type == "result") | .permission_denials // [] | length] | add // 0) as $denials
    | ([$events[] | select(.type == "system" and .subtype == "init") | .permissionMode] | first // "unknown") as $mode
    | ($sends | last) as $s
    | if $s == null then [$mode, "no send", 0, "-", $denials, "-"]
      else
        ($results | map(select(.tool_use_id == $s.id)) | first) as $r
        | [ $mode,
            (if $r == null then "no result"
             elif ($r.is_error // false) then "send failed"
             else "sent" end),
            ($sends | length),
            (if (($s.input.message // "") | trim_end) == ($expected | trim_end)
             then "verbatim" else "NOT VERBATIM" end),
            $denials,
            ($r.content // ""
              | if type == "array" then map(.text? // "") | join(" ") else tostring end
              | one_line | .[0:300]) ]
      end
    | @tsv' $raw 2>/dev/null)
  if [[ -z $summary ]]; then
    summary=unknown$'\t'"unreadable output"$'\t'0$'\t'-$'\t'0$'\t'"$(tail -c 300 $raw | tr '\n\t' '  ')"
  fi
  IFS=$'\t' read -r mode sent_status sends verbatim denials result <<<"$summary"
  # The showrunner holds a message from a sender in another mode.
  [[ $mode == $PERMISSION_MODE ]] || mode="MODE $mode, NOT $PERMISSION_MODE"

  print -r -- "$fired | exit $rc$timed_out | mode $mode | $sent_status ($sends sends) | $verbatim | denials $denials | to $target | $result" >> $LOG
  [[ $rc == 0 && $mode == $PERMISSION_MODE && $sent_status == sent && $verbatim == verbatim ]]
}

case $CMD in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  fire) cmd_fire ;;
  interval) cmd_interval $3 ;;
  *) die "unknown subcommand: $CMD (start, stop, status, fire or interval)" ;;
esac
