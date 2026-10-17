# The status line command. settings.json's statusLine sources this file rather
# than running it, naming its folder first:
#
#   statusline="$HOME/.claude/scripts/statusline"; . "$statusline/statusline.sh"
#
# It runs on every status line refresh, so it uses shell builtins only and ends
# by exec'ing jq: one process. Running it as a script would start a second
# shell, which measured 1.3 ms of a 4.3 ms render. The one exception is a
# counts cache out of date, below: counts_row.py rebuilds it once per change.
#
# It hands jq everything statusline.jq cannot read for itself:
#   account      the session's label, from its config dir's account-label.
#   legacy_home  whether that cache needs the legacy home login to remain valid.
#   home_config  the home JSON for that check, /dev/null for numbered folders.
#   labels       agent_notes' readings by label, /dev/null when missing.
#   colors       config/statusline.conf, /dev/null when missing or unreadable.
#   showrunners  the session ids every showrunner's update timer targets: the
#                TARGET line of each notifier showrunner-<slug>/conf, which
#                notifier.sh's write_conf always writes first.
#   showrunner   the unit director's live showrunner record text, selected
#                through SHOWRUNNER_UNIT's notifier TARGET, or empty.
#                NOTIFIER_STATE_DIR and NOTIFIER_SESSIONS_DIR override the
#                notifier and session folders. jq ignores damaged record text.
#                Its name is read each refresh without reopening the record.
#   counts       "<session id> <counts line>" lines: each showrunner's `For you: …`
#                row, from the cache counts_row.py keeps in
#                <showrunner state>/counts/<slug>. A cache is rebuilt only when a
#                file it names is newer than it, or its deferred question's time
#                has come; SHOWRUNNER_STATE_DIR overrides the state folder.
# jq renders the role, account, directory, tokens and model, then quota windows,
# then the session's counts row;
# a unit director with a live showrunner name reads "unit director for <name>".

config="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
account=
{ read -r account < "$config/account-label"; } 2>/dev/null
# The cache in the config home is valid only while its legacy login remains.
home_config=/dev/null
if [ "$config" = "$HOME/.claude" ] && [ -f "$HOME/.claude.json" ] && [ -r "$HOME/.claude.json" ]; then
  home_config="$HOME/.claude.json"
fi
legacy_home=
[ "$config" != "$HOME/.claude" ] || legacy_home=1

labels="$HOME/.local/state/agent-notes/by-label.json"
[ -r "$labels" ] || labels=/dev/null

colors="$statusline/../../config/statusline.conf"
[ -r "$colors" ] || colors=/dev/null

showrunners=
counts=
# zsh otherwise aborts on an empty notifier glob before jq can render the line.
[ -z "${ZSH_VERSION:-}" ] || setopt nonomatch
# bash has EPOCHSECONDS built in; a shell with neither leaves a deferred question to the next change.
[ -z "${ZSH_VERSION:-}" ] || zmodload -F zsh/datetime p:EPOCHSECONDS 2>/dev/null
for conf in "${NOTIFIER_STATE_DIR:-$HOME/.local/state/notifier}"/showrunner-*/conf; do
  target=
  { read -r target < "$conf"; } 2>/dev/null
  case $target in
    TARGET=session:?*)
      showrunners="$showrunners ${target#TARGET=session:}"
      instance=${conf%/conf}
      cache="${SHOWRUNNER_STATE_DIR:-$HOME/.local/state/showrunner}/counts/${instance##*/showrunner-}"
      fresh=
      if [ -f "$cache" ]; then
        fresh=1
        {
          read -r row
          read -r due
          case $due in
            ''|*[!0-9]*) ;;
            *) [ "${EPOCHSECONDS:-0}" -lt "$due" ] || fresh= ;;
          esac
          while [ -n "$fresh" ] && IFS= read -r read_from; do
            [ ! "$read_from" -nt "$cache" ] || fresh=
          done
        } < "$cache"
      fi
      [ -n "$fresh" ] || python3 -I "$statusline/counts_row.py" "$instance" >/dev/null 2>&1
      row=
      { read -r row < "$cache"; } 2>/dev/null
      [ -z "$row" ] || counts="$counts${target#TARGET=session:} $row
"
      ;;
  esac
done

showrunner=
if [ -n "${SHOWRUNNER_UNIT:-}" ] && [ -z "${PLAN_DELEGATE_TEAM_ROLE:-}" ]; then
  target=
  { read -r target < "${NOTIFIER_STATE_DIR:-$HOME/.local/state/notifier}/showrunner-$SHOWRUNNER_UNIT/conf"; } 2>/dev/null
  case $target in
    TARGET=session:?*)
      showrunner_id=${target#TARGET=session:}
      for session in "${NOTIFIER_SESSIONS_DIR:-$HOME/.claude/sessions}"/*.json; do
        session_line=
        if [ -n "${ZSH_VERSION:-}" ]; then
          { session_line=$(<"$session"); } 2>/dev/null
        else
          { IFS= read -r session_line < "$session"; } 2>/dev/null
        fi
        case $session_line in
          *'"sessionId":"'"$showrunner_id"'"'*)
            pid=${session##*/}
            pid=${pid%.json}
            case $pid in ''|*[!0-9]*) continue ;; esac
            if kill -0 "$pid" 2>/dev/null; then
              showrunner=$session_line
              break
            fi
            ;;
        esac
      done
      ;;
  esac
fi

# shellcheck disable=SC2154 # statusline is set by the command that sources this.
exec jq -r --arg pwd "$PWD" --arg account "$account" --arg showrunners "$showrunners" \
  --arg legacy_home "$legacy_home" --rawfile home_config "$home_config" \
  --arg showrunner "$showrunner" --arg counts "$counts" --slurpfile labels "$labels" \
  --rawfile colors "$colors" -f "$statusline/statusline.jq"
