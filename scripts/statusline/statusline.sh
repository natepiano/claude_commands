# shellcheck shell=sh
# Source this file from the statusLine command with statusline set to this folder.
# Read the account, colors, zone, quota labels, and cached counts with shell builtins.
# assignment.py reads the staff row and unit configuration, then replaces itself with jq.

if [ -n "${CONF_READ_LOG:-}" ]; then
  # shellcheck source=/dev/null
  . "$statusline/../lib/conf_record.sh"
fi

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
[ -z "${CONF_READ_LOG:-}" ] || [ "$colors" = /dev/null ] || conf_record statusline

zone=${USER_ZONE:-}
if [ -z "$zone" ]; then
  zone_conf=${USER_ZONE_CONF:-$statusline/../../config/user_zone.conf}
  zone_stem=${zone_conf##*/}
  [ -z "${CONF_READ_LOG:-}" ] || conf_record "${zone_stem%.conf}"
  {
    while IFS= read -r line; do
      case $line in zone=*) zone=${line#zone=}; break ;; esac
    done < "$zone_conf"
  } 2>/dev/null
fi
export TZ="${zone:-America/New_York}"
# NixOS's glibc finds zone files only through TZDIR; a bare environment would get UTC.
[ -n "${TZDIR:-}" ] || [ ! -d /etc/zoneinfo ] || export TZDIR=/etc/zoneinfo

counts=
# bash has EPOCHSECONDS built in; a shell with neither leaves a deferred question to the next change.
[ -z "${ZSH_VERSION:-}" ] || zmodload -F zsh/datetime p:EPOCHSECONDS 2>/dev/null
# Staff ids contain no spaces. Select owners before reading their timer folders.
for identity in $(jq -r '.rows[]? | select(.kind == "showrunner") | .id' "$staff" 2>/dev/null); do
  conf="${NOTIFIER_STATE_DIR:-$HOME/.local/state/notifier}/showrunner-$identity/conf"
  [ -f "$conf" ] && [ -r "$conf" ] || continue
  instance=${conf%/conf}
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
      [ -n "$fresh" ] || "$statusline/../lib/py" --isolated "$statusline/counts_row.py" "$instance" >/dev/null 2>&1
      row=
      { read -r row < "$cache"; } 2>/dev/null
      [ -z "$row" ] || counts="$counts${target#TARGET=session:} $row
"
      ;;
  esac
done

# shellcheck disable=SC2154 # statusline is set by the command that sources this.
STAFF_FILE="$staff" exec "$statusline/../lib/py" --isolated "$statusline/assignment.py" -r --arg pwd "$PWD" --arg account "$account" \
  --arg legacy_home "$legacy_home" --rawfile home_config "$home_config" \
  --arg counts "$counts" --slurpfile labels "$labels" \
  --rawfile colors "$colors" -f "$statusline/statusline.jq"
