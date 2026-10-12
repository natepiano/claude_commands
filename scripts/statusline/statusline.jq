# The whole status line, computed in one jq process.
#
# This runs on every status line refresh -- a few times a second across all open
# sessions -- so process count is the thing to minimise. The shell version it
# replaced spawned seven processes per refresh (sh, cat, four jq, basename) and
# was the second largest source of short-lived processes on this machine, behind
# only cargo-tile. This is sh plus jq: two.
#
# Called as:  jq -r --arg pwd "$PWD" --arg account "$account" -f statusline.jq
# Input:      the status line JSON on stdin.
# Output:     Line 1: "<machine> | <account> | <dirname> | <tokens with thousands separators> | <model [effort]>"
#             Line 2, when present: "5-hour <N>% left | weekly <M>% left"

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
     (if $account == "" then empty else $account end),
     $base,
     ((.context_window.total_input_tokens // 0) | commas),
     $m
   ] | join(" | ")) as $first_line
| ([
     (remaining(.rate_limits.five_hour?) | "5-hour \(.)% left"),
     (remaining(.rate_limits.seven_day?) | "weekly \(.)% left")
   ] | join(" | ")) as $second_line
| $first_line + (if $second_line == "" then "" else "\n" + $second_line end)
