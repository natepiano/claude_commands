# The status line command. settings.json's statusLine sources this file rather
# than running it, naming its folder first:
#
#   statusline="$HOME/.claude/scripts/statusline"; . "$statusline/statusline.sh"
#
# It runs on every status line refresh, so it uses shell builtins only and ends
# by exec'ing jq: one process. Running it as a script would start a second
# shell, which measured 1.3 ms of a 4.3 ms render.
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
# jq renders the role, account, directory, tokens and model, then quota windows;
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
# zsh otherwise aborts on an empty notifier glob before jq can render the line.
[ -z "${ZSH_VERSION:-}" ] || setopt nonomatch
for conf in "${NOTIFIER_STATE_DIR:-$HOME/.local/state/notifier}"/showrunner-*/conf; do
  target=
  { read -r target < "$conf"; } 2>/dev/null
  case $target in
    TARGET=session:?*) showrunners="$showrunners ${target#TARGET=session:}" ;;
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
  --arg showrunner "$showrunner" --slurpfile labels "$labels" \
  --rawfile colors "$colors" -f "$statusline/statusline.jq"
