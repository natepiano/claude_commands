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
#   labels       agent_notes' readings by label, /dev/null when missing.
#   colors       config/statusline.conf, /dev/null when missing or unreadable.
#   showrunners  the session ids every showrunner's update timer targets: the
#                TARGET line of each notifier showrunner-<slug>/conf, which
#                notifier.sh's write_conf always writes first.

config="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
account=
{ read -r account < "$config/account-label"; } 2>/dev/null

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

# shellcheck disable=SC2154 # statusline is set by the command that sources this.
exec jq -r --arg pwd "$PWD" --arg account "$account" --arg showrunners "$showrunners" \
  --slurpfile labels "$labels" --rawfile colors "$colors" -f "$statusline/statusline.jq"
