# Render the assignment, path, account, model, usage, and cached counts.
# assignment.py supplies the assignment text, role, and input payload.
# Direct callers can supply the input payload through stdin.

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
      teal: "38;5;30", brown: "38;5;137", mauve: "38;5;139"}
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

def remaining($window):
  ($window.used_percentage? // null) as $used
  | if ($used | type) == "number" then
      ((100 - $used) | floor
       | if . < 0 then 0 elif . > 100 then 100 else . end)
    else empty
    end;

# Seconds east of UTC that the user's clock shows at this epoch second, and that
# offset as strftime's %z writes it.
def offset: (localtime | mktime) - .;
def as_z: (if . < 0 then "-" else "+" end) + ((fabs / 3600 | floor) + 100 | tostring | .[1:])
  + ((fabs % 3600 / 60 | floor) + 100 | tostring | .[1:]);

# Epoch seconds as wall-clock time in the user's zone, formatted by strftime
# $fmt and followed by the zone's abbreviation. statusline.sh sets TZ to the
# zone in config/user_zone.conf, so strflocaltime does not use the machine's
# zone (natedev's is EDT, the Mac's Pacific). In the hour a clock repeats when
# daylight saving ends, jq can label a time with the other pass's name (01:30
# EDT for 01:30 EST): its label comes from glibc's mktime, whose guess depends on
# earlier calls. So the name comes from this second or one two hours either
# side, the first whose own %z, read in the same call, is the offset it shows.
def user_time($fmt):
  . as $t
  | ($t | offset) as $off
  | [$t, $t + 7200, $t - 7200]
  | map(select(offset == $off) | strflocaltime("%z %Z") | split(" ")
        | select(.[0] == ($off | as_z)) | .[1])
  | ($t | strflocaltime($fmt)) + " " + (first // ($t | strflocaltime("%Z")));

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

($ARGS.named.payload // .)
| ($ARGS.named.colors // "" | color_config) as $colors
| (if ($ARGS.named.legacy_home // "") == "1" then
     ($ARGS.named.home_config // "" | try (fromjson | .oauthAccount.emailAddress // "") catch "") as $login
     | if ($login | type) == "string" and $login != "" then ($ARGS.named.account // "") else "" end
   else ($ARGS.named.account // "") end) as $account
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
     ($ARGS.named.assignment // "" | select(. != "") | color($colors; "role." + $ARGS.named.role)),
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
   | ([$ARGS.named.counts // "" | split("\n")[]
       | select($id != "" and startswith($id + " ")) | .[($id | length) + 1:]
       | sub("^For you: "; "") | split(", ")[]] ) as $parts
   | ($ARGS.named.rebuild // "") as $rebuild
   | ((if ($ARGS.named.adhoc // "") == "" then [] else [$ARGS.named.adhoc] end)
      + ($parts | map(select(test("^[0-9]+ (question|change)s?$"))))
      + (if $rebuild == "" then [] else [$rebuild] end)
      + ($parts | map(select(test("^[0-9]+ (question|change)s?$") | not))))
   | if length == 0 then "" else "For you: " + join(", ") end
      # Use the plural key with underscores: counts.adhoc_items; efficiency stays singular.
      | gsub("(?<count>[0-9]+)(?: (?<word>adhoc item|question|change|efficiency|proposal|project)(?<s>s?))?";
             . as $part | "\u001b[1m\($part.count)\u001b[22m"
             | if $part.word == null then .
               else (. + " " + $part.word + $part.s)
                 | color($colors; "counts." + ($part.word | gsub(" "; "_")) + (if $part.word == "efficiency" then "" else "s" end))
               end)) as $counts_row
| [$first_line, $second_line, $counts_row] | map(select(. != "")) | join("\n")
