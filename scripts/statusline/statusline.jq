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
#                --arg showrunner "<session JSON>" --arg own "<session JSON>" --arg counts "<id> <row> lines"
#                --slurpfile labels <by-label.json> --rawfile colors <statusline.conf> -f statusline.jq
# Input:      the status line JSON on stdin; labels, showrunner, own, showrunners, counts and
#             colors are optional; /dev/null stands in for missing input files.
#             Empty or damaged showrunner text leaves the unit director unnamed.
#             $own is this session's record; its name is used only when its sessionId
#             is this session's.
# Output:     Line 1: "<role> | <path>", the path with "~" for the home folder (user, 2026-10-19).
#             A showrunner reads "<session> showrunner", a unit director "<session> unit for
#             <showrunner name>"; the session name comes only from this session's record, and
#             " for <name>" only when the live showrunner record has a name.
#             Line 2: "<account> | <model [effort]> | <tokens with thousands separators> |
#             5-hour <N>% left until <HH:MM zone> | weekly <M>% left until <Day HH:MM zone>",
#             each piece only when present, the 5-hour piece only when under 10% is left,
#             each " until ..." only when its window carries a reset time.
#             Line 3, when present: this session's "For you: …" counts row, from
#             the "<session id> <row>" lines in $counts that statusline.sh reads
#             from each showrunner's counts cache, with each count in bold. Each
#             part ("2 changes") takes its type's counts.<type> color, matched by
#             its word, so the user learns each type by its color (user, 2026-10-20).
#             ANSI colors wrap the role (with the session name before it), account, tokens,
#             percentages and the counts row's parts only.

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
      "bright-magenta": 95, "bright-cyan": 96, "bright-white": 97,
      orange: "38;5;208", purple: "38;5;141", pink: "38;5;211",
      teal: "38;5;30", brown: "38;5;137"}
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
# worker:        agent_bg.sh hands a worker UNIT_TEAM_ROLE through
#                --settings. It comes before unit director because a background
#                session inherits the daemon's environment, and a daemon a unit
#                director started carries SHOWRUNNER_UNIT.
# unit director: add_unit.py launches the unit director's tmux session with SHOWRUNNER_UNIT.
def role:
  (.session_id // "") as $id
  | if $id != "" and ($ARGS.named.showrunners // "" | split(" ") | any(. == $id)) then "showrunner"
    elif ($ENV.UNIT_TEAM_ROLE // "") != "" then "worker"
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
| (if ($ARGS.named.legacy_home // "") == "1" then
     ($ARGS.named.home_config // "" | try (fromjson | .oauthAccount.emailAddress // "") catch "") as $login
     | if ($login | type) == "string" and $login != "" then ($ARGS.named.account // "") else "" end
   else ($ARGS.named.account // "") end) as $account
| ($ARGS.named.showrunner // "" | try fromjson catch null
   | if type == "object" then .name else null end
   | if type == "string" and . != "" then . else null end) as $showrunner_name
| (.session_id // "") as $session_id
| ($ARGS.named.own // "" | try fromjson catch null
   | if type == "object" and $session_id != "" and .sessionId == $session_id then .name else null end
   | if type == "string" and . != "" then . else null end) as $session_name
| ((.workspace.current_dir // .cwd // "") | if . == "" then $pwd else . end) as $d
# Trailing slashes stripped first, so "/etc/nixos/" gives "/etc/nixos"; "/" stays "/".
| ($d | sub("(?<=.)/+$"; "")) as $full
| ($ENV.HOME // "" | sub("/+$"; "")) as $home
| (if $home == "" then $full
   elif $full == $home then "~"
   elif ($full | startswith($home + "/")) then "~" + $full[($home | length):]
   else $full end) as $path
| ((.model.display_name // "")
   + (if (.effort.level // "") == "" then "" else " " + .effort.level end)) as $m
| ([
     (role as $role
      | (if $role == "unit director" then "unit" else $role end)
      | (if $role == "unit director" and ($showrunner_name | type) == "string"
            and $showrunner_name != ""
         then . + " for " + $showrunner_name else . end)
      | if $session_name != null and ($role == "showrunner" or $role == "unit director")
        then $session_name + " " + . else . end
      # The session name takes the role's color.
      | color($colors; "role." + ($role | gsub(" "; "-")))),
     $path
   ] | join(" | ")) as $first_line
| ([
     # A label is a note name; an address never reaches the line.
     (if $account == "" or ($account | contains("@")) then empty
      elif ($account | test("^claude [0-9]+$")) then $account | sub(" "; "")
      else $account end | . as $label | color($colors; "account." + $label)),
     ($m | select(. != "")),
     # Tokens show once the response carries a count.
     (.context_window as $context
      | select(($context.total_input_tokens? | type) == "number")
      | ($context.total_input_tokens | commas)
      | if ($context.context_window_size | type) == "number" and $context.context_window_size > 0
           and ($context.total_input_tokens | type) == "number"
        then level_color($colors; "tokens"; $context.total_input_tokens * 100 / $context.context_window_size)
        else . end),
     # The 5-hour window shows only when under 10% of it is left (user, 2026-10-18).
     (window("five_hour"; .rate_limits.five_hour?) as $five_hour
      | select((remaining($five_hour) // 100) < 10)
      | usage($colors; "5-hour"; $five_hour; "%H:%M")),
     usage($colors; "weekly"; window("weekly"; .rate_limits.seven_day?); "%a %H:%M")
   ] | join(" | ")) as $second_line
| ((.session_id // "") as $id
   | [$ARGS.named.counts // "" | split("\n")[]
      | select($id != "" and startswith($id + " ")) | .[($id | length) + 1:]
      # The key is the plural word: counts.questions, counts.efficiency and so on.
      | gsub("(?<count>[0-9]+)(?: (?<word>question|change|efficiency|proposal|project)(?<s>s?))?";
             . as $part | "\u001b[1m\($part.count)\u001b[22m"
             | if $part.word == null then .
               else (. + " " + $part.word + $part.s)
                 | color($colors; "counts." + $part.word + (if $part.word == "efficiency" then "" else "s" end))
               end)]
   | join("\n")) as $counts_row
| [$first_line, $second_line, $counts_row] | map(select(. != "")) | join("\n")
