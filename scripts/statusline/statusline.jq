# The whole status line, computed in one jq process.
#
# This runs on every status line refresh -- a few times a second across all open
# sessions -- so process count is the thing to minimise. The shell version it
# replaced spawned seven processes per refresh (sh, cat, four jq, basename) and
# was the second largest source of short-lived processes on this machine, behind
# only cargo-tile. This is sh plus jq: two.
#
# Called as:  jq -r --arg pwd "$PWD" --arg account "$account" --slurpfile labels <by-label.json> -f statusline.jq
# Input:      the status line JSON on stdin; --slurpfile labels is optional, and
#             /dev/null stands in when the file is missing.
# Output:     Line 1: "<machine> | <account> | <dirname> | <tokens with thousands separators> | <model [effort]>"
#             Line 2, when present: "5-hour <N>% left until <HH:MM zone> | weekly <M>% left until <Day HH:MM zone>",
#             each " until ..." only when its window carries a reset time.

# Thousands separators, matching `printf "%'d"` under en_US: walk the digits
# from the right and insert a comma (codepoint 44) before every third one. The
# comma goes BEFORE the digit because the list is still reversed here -- the
# final reverse puts it after.
def commas: tostring | explode | reverse | to_entries
  | map(if .key > 0 and .key % 3 == 0 then [44, .value] else [.value] end)
  | flatten | reverse | implode;

# The machine's ssh name, told apart by platform as the shell prompt does
# (modules/common/shell.nix in the nixos repo). macOS keeps homes under /Users;
# reading $ENV costs no process.
def machine: if ($ENV.HOME // "" | startswith("/Users/")) then "mac" else "natedev" end;

def remaining($window):
  ($window.used_percentage? // null) as $used
  | if ($used | type) == "number" then
      ((100 - $used) | floor
       | if . < 0 then 0 elif . > 100 then 100 else . end)
    else empty
    end;

# 00:00 UTC on the $n-th Sunday of $month (0-based) in $year, as epoch seconds.
def nth_sunday($year; $month; $n):
  ([$year, $month, 1, 0, 0, 0, 0, 0] | mktime) as $first
  | $first + ((7 - ($first | gmtime | .[6])) % 7 + 7 * ($n - 1)) * 86400;

# Epoch seconds as wall-clock time in the user's zone, formatted by strftime
# $fmt and followed by the zone's abbreviation. The zone is America/New_York,
# from config/user_zone.conf, written out here: jq cannot read that file without
# a change to the settings.json command line, and strflocaltime would use the
# machine's zone, which is Pacific on the Mac. EDT runs from 02:00 EST on the
# second Sunday in March (07:00 UTC) to 02:00 EDT on the first Sunday in
# November (06:00 UTC).
def user_time($fmt):
  . as $t
  | ($t | gmtime | .[0]) as $year
  | ($t >= nth_sunday($year; 2; 2) + 7 * 3600
     and $t < nth_sunday($year; 10; 1) + 6 * 3600) as $dst
  | ($t + (if $dst then -4 else -5 end) * 3600 | gmtime | strftime($fmt))
    + (if $dst then " EDT" else " EST" end);

# The window as agent_notes last read it for this session's account label
# (~/.local/state/agent-notes/by-label.json), which is the number the quota
# alerts use. The response's own figure is a different counter and lags it.
# That reading stands only while it is under 10 minutes old and its window has
# not reset; otherwise the response's figure does, so the line never goes blank.
def window($name; $live):
  (($ARGS.named.labels // [])[0] // {} | .[$ARGS.named.account // ""] // {} | .[$name] // null) as $stored
  | if ($stored | type) == "object"
       and ($stored.at | type) == "number" and (now - $stored.at) <= 600
       and (($stored.resets_at | type) != "number" or $stored.resets_at > now)
       and ($stored.left | type) == "number"
    then {used_percentage: (100 - $stored.left), resets_at: $stored.resets_at}
    else $live
    end;

# "<label> <N>% left", then " until <reset time>" when the window carries one.
def usage($label; $window; $fmt):
  remaining($window) as $left
  | ($window.resets_at? // null) as $reset
  | "\($label) \($left)% left"
    + (if ($reset | type) == "number" then " until " + ($reset | user_time($fmt)) else "" end);

($ARGS.named.account // "") as $account
| ((.workspace.current_dir // .cwd // "") | if . == "" then $pwd else . end) as $d
# Trailing slashes stripped first, so "/etc/nixos/" gives "nixos" and not "".
# "/" survives that to become "", and falls back to the path itself.
| (($d | sub("/+$"; "") | split("/") | last) // "") as $raw
| (if $raw == "" then $d else $raw end) as $base
| ((.model.display_name // "")
   + (if (.effort.level // "") == "" then "" else " " + .effort.level end)) as $m
| ([
     machine,
     # A label is a note name; an address never reaches the line.
     (if $account == "" or ($account | contains("@")) then empty
      elif ($account | test("^claude [0-9]+$")) then $account | sub(" "; "")
      else $account end),
     $base,
     ((.context_window.total_input_tokens // 0) | commas),
     $m
   ] | join(" | ")) as $first_line
| ([
     usage("5-hour"; window("five_hour"; .rate_limits.five_hour?); "%H:%M"),
     usage("weekly"; window("weekly"; .rate_limits.seven_day?); "%a %H:%M")
   ] | join(" | ")) as $second_line
| $first_line + (if $second_line == "" then "" else "\n" + $second_line end)
