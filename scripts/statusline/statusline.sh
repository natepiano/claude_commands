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
#   TZ           the user's zone, as user_zone.py reads it: USER_ZONE, else the
#                zone= line of USER_ZONE_CONF or config/user_zone.conf, else
#                user_zone.py's default. jq's strflocaltime writes reset times in it.
#   staff        the current staff rows, from STAFF_FILE or the shared staff.json.
#                jq joins the native runtime identity to the
#                unique normalized name, just as session_labels.StaffLabels does.
#   machine      the local hostname, matching shutdown.remote.local_machine.
#   timer_*      each notifier conf, for its target and checked document path.
#                No production document is opened for assignment.
#   counts       "<session id> <counts line>" lines: each showrunner's `For you: …`
#                row, from the cache counts_row.py keeps in
#                <showrunner state>/counts/<slug>. A cache is rebuilt only when a
#                file it names is newer than it, or its deferred question's time
#                has come; SHOWRUNNER_STATE_DIR overrides the state folder.
# jq renders the staff name, role and path, then the account, model, tokens and
# quota windows, then the session's counts row, each part in its counts.<type> color;
# a unit director reads "<unit> unit for <staff parent name>".

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

staff="${STAFF_FILE:-$HOME/.local/state/staff/staff.json}"
[ -f "$staff" ] && [ -r "$staff" ] || staff=/dev/null

labels="$HOME/.local/state/agent-notes/by-label.json"
[ -r "$labels" ] || labels=/dev/null

colors="$statusline/../../config/statusline.conf"
[ -r "$colors" ] || colors=/dev/null

zone=${USER_ZONE:-}
if [ -z "$zone" ]; then
  {
    while IFS= read -r line; do
      case $line in zone=*) zone=${line#zone=}; break ;; esac
    done < "${USER_ZONE_CONF:-$statusline/../../config/user_zone.conf}"
  } 2>/dev/null
fi
export TZ="${zone:-America/New_York}"
# NixOS's glibc finds zone files only through TZDIR; a bare environment would get UTC.
[ -n "${TZDIR:-}" ] || [ ! -d /etc/zoneinfo ] || export TZDIR=/etc/zoneinfo

counts=
machine=${HOST:-${HOSTNAME:-}}
{ [ ! -r /proc/sys/kernel/hostname ] || read -r machine < /proc/sys/kernel/hostname; } 2>/dev/null
machine=${machine%%.*}
set --
# zsh otherwise aborts on an empty notifier glob before jq can render the line.
[ -z "${ZSH_VERSION:-}" ] || setopt nonomatch
# bash has EPOCHSECONDS built in; a shell with neither leaves a deferred question to the next change.
[ -z "${ZSH_VERSION:-}" ] || zmodload -F zsh/datetime p:EPOCHSECONDS 2>/dev/null
for conf in "${NOTIFIER_STATE_DIR:-$HOME/.local/state/notifier}"/showrunner-*/conf; do
  [ -f "$conf" ] && [ -r "$conf" ] || continue
  instance=${conf%/conf}
  set -- "$@" --rawfile "timer_${instance##*/showrunner-}" "$conf"
  target=
  { read -r target < "$conf"; } 2>/dev/null
  case $target in
    TARGET=session:?*)
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

# This session's own record names it: Claude Code runs the status line command, so its pid is $PPID.
# jq checks the record's session id against the session's own, so a reused pid names nobody.
own=
{
  if [ -n "${ZSH_VERSION:-}" ]; then
    own=$(<"${NOTIFIER_SESSIONS_DIR:-$HOME/.claude/sessions}/$PPID.json")
  else
    IFS= read -r own < "${NOTIFIER_SESSIONS_DIR:-$HOME/.claude/sessions}/$PPID.json"
  fi
} 2>/dev/null

# shellcheck disable=SC2154 # statusline is set by the command that sources this.
exec jq -r --arg pwd "$PWD" --arg account "$account" \
  --arg machine "$machine" "$@" \
  --arg legacy_home "$legacy_home" --rawfile home_config "$home_config" \
  --rawfile staff "$staff" --arg own "$own" --arg counts "$counts" --slurpfile labels "$labels" \
  --rawfile colors "$colors" -f "$statusline/statusline.jq"
