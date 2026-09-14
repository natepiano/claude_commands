# The whole status line, computed in one jq process.
#
# This runs on every status line refresh -- a few times a second across all open
# sessions -- so process count is the thing to minimise. The shell version it
# replaced spawned seven processes per refresh (sh, cat, four jq, basename) and
# was the second largest source of short-lived processes on this machine, behind
# only cargo-tile. This is sh plus jq: two.
#
# Called as:  jq -r --arg pwd "$PWD" -f statusline.jq
# Input:      the status line JSON on stdin.
# Output:     "<dirname> | <tokens with thousands separators> | <model [effort]>"

# Thousands separators, matching `printf "%'d"` under en_US: walk the digits
# from the right and insert a comma (codepoint 44) before every third one. The
# comma goes BEFORE the digit because the list is still reversed here -- the
# final reverse puts it after.
def commas: tostring | explode | reverse | to_entries
  | map(if .key > 0 and .key % 3 == 0 then [44, .value] else [.value] end)
  | flatten | reverse | implode;

((.workspace.current_dir // .cwd // "") | if . == "" then $pwd else . end) as $d
# Trailing slashes stripped first, so "/etc/nixos/" gives "nixos" and not "".
# "/" survives that to become "", and falls back to the path itself.
| (($d | sub("/+$"; "") | split("/") | last) // "") as $raw
| (if $raw == "" then $d else $raw end) as $base
| ((.model.display_name // "")
   + (if (.effort.level // "") == "" then "" else " " + .effort.level end)) as $m
| "\($base) | \((.context_window.total_input_tokens // 0) | commas) | \($m)"
