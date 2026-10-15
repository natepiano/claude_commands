# The whole status line, computed in one jq process.
#
# This runs on every status line refresh -- a few times a second across all open
# sessions -- so process count is the thing to minimise. The shell version it
# replaced spawned seven processes per refresh (sh, cat, four jq, basename) and
# was the second largest source of short-lived processes on this machine, behind
# only cargo-tile. statusline.sh reads its inputs with builtins and execs this:
# one process.
#
# Called as:  jq -r --arg pwd "$PWD" --arg account "$account" --arg showrunners "<ids>"
#                --slurpfile labels <by-label.json> --rawfile colors <statusline.conf> -f statusline.jq
# Input:      the status line JSON on stdin; labels, showrunners and colors are
#             optional, and /dev/null stands in for missing labels or colors files.
# Output:     Line 1: "<role> | <account> | <dirname> | <tokens with thousands separators> | <model [effort]>"
#             Line 2, when present: "5-hour <N>% left until <HH:MM zone> | weekly <M>% left until <Day HH:MM zone>",
#             each " until ..." only when its window carries a reset time.
#             ANSI colors wrap the role, account, tokens and percentages only.

def trim: gsub("^\\s+|\\s+$"; "");

# Parse once per render. Values remain text until their field validates them.
def color_config:
  reduce (split("\n")[] | (split("#")[0] // "") | trim
          | select(. != "") | index("=") as $equals
          | select($equals != null)
          | {key: (.[:$equals] | trim), value: (.[$equals + 1:] | trim)}) as $entry
    ({}; .[$entry.key] = $entry.value);

def color($config; $key):
  . as $text
  | ({black: 30, red: 31, green: 32, yellow: 33, blue: 34, magenta: 35,
      cyan: 36, white: 37, "bright-black": 90, "bright-red": 91,
      "bright-green": 92, "bright-yellow": 93, "bright-blue": 94,
      "bright-magenta": 95, "bright-cyan": 96, "bright-white": 97}
     [($config[$key] // "")]) as $code
  | if $code == null then $text else "\u001b[\($code)m\($text)\u001b[0m" end;

def level_color($config; $family; $value):
  (try ($config[$family + ".warn"] | tonumber
        | if isfinite and (isnan | not) then . else null end) catch null) as $warn
  | (try ($config[$family + ".alarm"] | tonumber
          | if isfinite and (isnan | not) then . else null end) catch null) as $alarm
  | if ($warn | type) != "number" or ($alarm | type) != "number" then .
    else color($config; "level." +
      (if (if $family == "tokens" then $value >= $alarm else $value <= $alarm end) then "alarm"
       elif (if $family == "tokens" then $value >= $warn else $value <= $warn end) then "warn"
       else "ok" end))
    end;

# Thousands separators, matching `printf "%'d"` under en_US: walk the digits
# from the right and insert a comma (codepoint 44) before every third one. The
# comma goes BEFORE the digit because the list is still reversed here -- the
# final reverse puts it after.
def commas: tostring | explode | reverse | to_entries
  | map(if .key > 0 and .key % 3 == 0 then [44, .value] else [.value] end)
  | flatten | reverse | implode;

# The part this session plays, from the signals the production scripts use
# (shutdown's session kinds, docs/as-built/shutdown-and-restart.md), first match wins.
# showrunner:    its session id is one a showrunner update timer targets.
# worker:        agent_bg.sh hands a delegate worker PLAN_DELEGATE_TEAM_ROLE through
#                --settings. It comes before unit director because a background
#                session inherits the daemon's environment, and a daemon a unit
#                director started carries SHOWRUNNER_UNIT.
# unit director: add_unit.py launches the unit director's tmux session with SHOWRUNNER_UNIT.
def role:
  (.session_id // "") as $id
  | if $id != "" and ($ARGS.named.showrunners // "" | split(" ") | any(. == $id)) then "showrunner"
    elif ($ENV.PLAN_DELEGATE_TEAM_ROLE // "") != "" then "worker"
    elif ($ENV.SHOWRUNNER_UNIT // "") != "" then "unit director"
    else "unassigned"
    end;

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
# from config/user_zone.conf, written out here: jq cannot read that file unless
# statusline.sh passes it in, and strflocaltime would use the
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
  (($ARGS.named.labels // [])[0] // {} | .[(($ARGS.named.account // "") | gsub(" "; ""))] // {} | .[$name] // null) as $stored
  | if ($stored | type) == "object"
       and ($stored.at | type) == "number" and (now - $stored.at) <= 600
       and (($stored.resets_at | type) != "number" or $stored.resets_at > now)
       and ($stored.left | type) == "number"
    then {used_percentage: (100 - $stored.left), resets_at: $stored.resets_at}
    else $live
    end;

# "<label> <N>% left", then " until <reset time>" when the window carries one.
def usage($config; $label; $window; $fmt):
  remaining($window) as $left
  | ($window.resets_at? // null) as $reset
  | $label + " " + ("\($left)% left" | level_color($config; "left"; $left))
    + (if ($reset | type) == "number" then " until " + ($reset | user_time($fmt)) else "" end);

($ARGS.named.colors // "" | color_config) as $colors
| ($ARGS.named.account // "") as $account
| ((.workspace.current_dir // .cwd // "") | if . == "" then $pwd else . end) as $d
# Trailing slashes stripped first, so "/etc/nixos/" gives "nixos" and not "".
# "/" survives that to become "", and falls back to the path itself.
| (($d | sub("/+$"; "") | split("/") | last) // "") as $raw
| (if $raw == "" then $d else $raw end) as $base
| ((.model.display_name // "")
   + (if (.effort.level // "") == "" then "" else " " + .effort.level end)) as $m
| ([
     (role | if . == "unassigned" then .
             else . as $role | color($colors; "role." + ($role | gsub(" "; "-"))) end),
     # A label is a note name; an address never reaches the line.
     (if $account == "" or ($account | contains("@")) then empty
      elif ($account | test("^claude [0-9]+$")) then $account | sub(" "; "")
      else $account end | . as $label | color($colors; "account." + $label)),
     $base,
     (.context_window as $context
      | (($context.total_input_tokens // 0) | commas)
      | if ($context.context_window_size | type) == "number" and $context.context_window_size > 0
           and ($context.total_input_tokens | type) == "number"
        then level_color($colors; "tokens"; $context.total_input_tokens * 100 / $context.context_window_size)
        else . end),
     $m
   ] | join(" | ")) as $first_line
| ([
     usage($colors; "5-hour"; window("five_hour"; .rate_limits.five_hour?); "%H:%M"),
     usage($colors; "weekly"; window("weekly"; .rate_limits.seven_day?); "%a %H:%M")
   ] | join(" | ")) as $second_line
| $first_line + (if $second_line == "" then "" else "\n" + $second_line end)
