# The whole status line, computed in one jq process.
#
# This runs on every status line refresh -- a few times a second across all open
# sessions -- so process count is the thing to minimise. The shell version it
# replaced spawned seven processes per refresh (sh, cat, four jq, basename) and
# was the second largest source of short-lived processes on this machine, behind
# only cargo-tile. statusline.sh reads its inputs with builtins and execs this:
# one process.
#
# Called as:  jq -r --arg pwd "$PWD" --arg account "$account"
#                --rawfile staff <staff.json> --arg own "<session JSON>" --arg counts "<id> <row> lines"
#                --slurpfile labels <by-label.json> --rawfile colors <statusline.conf> -f statusline.jq
# Input:      the status line JSON on stdin; labels, staff, own, counts and
#             colors are optional; /dev/null stands in for missing input files.
#             Empty or damaged staff text leaves the session unassigned.
#             $own is this session's record; its name is used only when its sessionId
#             is this session's.
# Output:     Line 1: "<role> | <path>", the path with "~" for the home folder (user, 2026-10-19).
#             A showrunner reads "<session> showrunner", a unit director "<session> unit for
#             <showrunner name>"; the assignment and its parent name come from the staff row.
#             Line 2: "<account> | <model [effort]> | <tokens with thousands separators> |
#             5-hour <N>% left until <HH:MM zone> | weekly <M>% left until <Day HH:MM zone>",
#             each piece only when present, the 5-hour piece only when under 10% is left,
#             each " until ..." only when its window carries a reset time.
#             Line 3, when present: this session's "For you: …" counts row, from
#             the "<session id> <row>" lines in $counts that statusline.sh reads
#             from each showrunner's counts cache, with each count in bold. Each
#             part ("2 changes") takes its type's counts.<type> color, matched by
#             its word, so the user learns each type by its color (user, 2026-10-20).
#             ANSI colors wrap the staff assignment, account, tokens,
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

# Match StaffLabels.environment: identity, timer target, exact local worktree,
# then the unique normalized name. An unknown explicit id never falls through.
def staff_location($field; $kind; $states):
  if has($field) then .kind == $kind and
    (.[$field] | type == "object" and keys == ["machine", "path", "state"]
     and (.machine | type) == "string" and .machine != "" and (.machine | trim) == .machine
     and (.path | type) == "string" and (.path | startswith("/"))
     and (.state as $state | $states | index($state)) != null)
  else true end;

def staff_rows:
  try ($ARGS.named.staff // "" | fromjson
       | select((.schema == 1 or .schema == 2) and (.rows | type) == "array") | .rows
       | select(all(.[]; (.id | type) == "string" and (.name | type) == "string"
                    and (.kind == "showrunner" or .kind == "unit" or .kind == "worker")
                    and staff_location("worktree"; "unit"; ["present", "removed"])
                    and staff_location("doc"; "showrunner"; ["live", "wrapped"]))))
  catch [];

# The notifier's CHECK is shell-quoted argv, parsed as shlex.split does for the
# script and document tokens. Nothing in the command is evaluated or executed.
def shell_words:
  "(?:[^\\s'\"\\\\]|\\\\.|'[^']*'|\"(?:[^\"\\\\]|\\\\.)*\")+" as $word
  | if test("^\\s*(?:" + $word + "\\s*)*$") then [scan($word)
   | gsub("'(?<single>[^']*)'|\"(?<double>(?:[^\"\\\\]|\\\\.)*)\"|\\\\(?<escaped>.)";
          .single // (if .double != null then (.double | gsub("\\\\(?<c>[\\\\\"$`])"; .c))
                      else .escaped end))] else [] end;

def timer_owners($rows; $session_id; $machine):
  [$ARGS.named | to_entries[] | select(.key | startswith("timer_"))
   | (.key | ltrimstr("timer_")) as $slug
   | (.value | split("\n") | map(select(contains("=")) | split("=")
                                 | {key: .[0], value: (.[1:] | join("="))}) | from_entries) as $conf
   | select($session_id != "" and $conf.TARGET == "session:" + $session_id)
   | ($conf.CHECK // "" | shell_words) as $check
   | ([$check | to_entries[] | select(.value | split("/") | last == "production_check.sh") | .key][0]) as $index
   | (if $index != null then $check[$index + 1] else null end) as $doc
   | select(($doc | type) == "string" and ($doc | startswith("/")))
   | [$rows[] | select(.kind == "showrunner" and .doc.state == "live"
                      and .doc.machine == $machine and .doc.path == $doc)] as $owners
   | (if ($owners | length) > 0 then $owners
      else [$rows[] | select(.kind == "showrunner" and .name == $slug)] end)
   | select(length == 1) | .[0]];

def assignment($rows; $name; $session_id; $cwd):
  ($ENV.STAFF_ID // "") as $id
  | ($ARGS.named.machine // "") as $machine
  | (if $id != "" then [$rows[] | select(.id == $id)]
     else timer_owners($rows; $session_id; $machine) as $timers
       | [$rows[] | select(.kind == "unit" and $cwd != "" and .worktree.state == "present"
                          and .worktree.machine == $machine and .worktree.path == $cwd)] as $folders
       | if ($timers | length) == 1 then $timers
         elif ($folders | length) == 1 then $folders
         else ($ENV.SHOWRUNNER_UNIT_ID // "" | if . == "" then $name // "" else . end
                         | if test("^[A-Za-z0-9][A-Za-z0-9_-]*$")
                         then sub("[-_]unit$"; "") else "" end) as $short
       | [$rows[] | select((.kind == "unit" or .kind == "showrunner") and .name == $short)]
         end
     end)
  | if length == 1 then .[0] else null end;

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

($ARGS.named.colors // "" | color_config) as $colors
| (if ($ARGS.named.legacy_home // "") == "1" then
     ($ARGS.named.home_config // "" | try (fromjson | .oauthAccount.emailAddress // "") catch "") as $login
     | if ($login | type) == "string" and $login != "" then ($ARGS.named.account // "") else "" end
   else ($ARGS.named.account // "") end) as $account
| (.session_id // "") as $session_id
| ($ARGS.named.own // "" | try fromjson catch null
   | if type == "object" and $session_id != "" and .sessionId == $session_id then .name else null end
   | if type == "string" and . != "" then . else null end) as $session_name
| (staff_rows // []) as $rows
| ((.workspace.current_dir // .cwd // "") | if . == "" then $pwd else . end) as $d
| assignment($rows; $session_name; $session_id; $d) as $assignment
| (if $assignment.kind == "unit" then
     [$rows[] | select(.id == $assignment.parent and .kind == "showrunner")]
     | if length == 1 then .[0].name else null end
   else null end) as $showrunner_name
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
     ((if $assignment.kind == "unit" then "unit director"
        else $assignment.kind // "unassigned" end) as $role
      | (if $role == "unit director" then "unit" else $role end)
      | (if $role == "unit director" and ($showrunner_name | type) == "string"
            and $showrunner_name != ""
         then . + " for " + $showrunner_name else . end)
      | if $assignment != null and ($role == "showrunner" or $role == "unit director")
        then $assignment.name + " " + . else . end
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
