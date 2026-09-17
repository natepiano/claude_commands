#!/usr/bin/env bash
set -euo pipefail

# One combined stage table across one or more CI runs, for the /validate_and_push
# progress tick. Columns are the runs; rows are the union of their stage names, so
# a stage only one repo has reads `n/a` in the other. A settled run gets a bold
# note under the table.
#
# Usage: ci_tick.sh <owner/repo> <run-id> [<owner/repo> <run-id> ...]

if [ "$#" -lt 2 ] || [ $(($# % 2)) -ne 0 ]; then
  echo "Usage: ci_tick.sh <owner/repo> <run-id> [<owner/repo> <run-id> ...]" >&2
  exit 2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

i=0
while [ "$#" -gt 0 ]; do
  repo="$1"
  run="$2"
  shift 2
  gh run view "$run" --repo "$repo" \
    --json createdAt,updatedAt,status,conclusion,jobs |
    jq --arg label "${repo##*/}" --arg run "$run" '. + {label: $label, run_id: $run}' \
      >"$tmp/$i.json"
  i=$((i + 1))
done

jq -s -r --arg clock "$(date '+%H:%M:%S %Z')" '
  def secs: if . >= 60 then "\((./60)|floor)m \((.%60)|floor)s" else "\(.|floor)s" end;
  def icon: if .status != "completed" then "…"
            elif .conclusion == "success" then "green"
            elif .conclusion == "skipped" then "skipped"
            elif .conclusion == "cancelled" then "cancelled"
            else "RED" end;
  def cell:
    (if .startedAt == null or (.startedAt|startswith("0001")) then null
     else (.startedAt|fromdateiso8601) end) as $b
    | (if .completedAt == null or (.completedAt|startswith("0001")) then now
       else (.completedAt|fromdateiso8601) end) as $e
    | "\(icon) \(if $b == null then "-" else (($e-$b) | (if . < 0 then 0 else . end) | secs) end)";

  . as $runs
  | [$runs[].label] as $labels
  | ([$runs[] | .jobs[].name]
     | reduce .[] as $n ([]; if index($n) then . else . + [$n] end)) as $stages
  | ($runs | map({(.label): (.jobs | map({key: .name, value: cell}) | from_entries)})
     | add) as $cells

  | "**\($clock)**",
    "",
    ($runs[]
     | (if .status == "completed"
        then ((.updatedAt|fromdateiso8601) - (.createdAt|fromdateiso8601))
        else (now - (.createdAt|fromdateiso8601)) end) as $elapsed
     | ([.jobs[]|select(.conclusion=="success")]|length) as $g
     | ([.jobs[]|select(.conclusion=="skipped")]|length) as $s
     | "- **\(.label)** run `\(.run_id)` · \(.status) \(.conclusion // "-") · elapsed **\($elapsed|secs)** · **\($g) of \(.jobs|length) green\(if $s > 0 then " (\($s) skipped)" else "" end)**"),
    "",
    ("| stage | " + ($labels | join(" | ")) + " |"),
    ("|---|" + ($labels | map("---|") | join(""))),
    ($stages[] as $st
     | "| \($st) | " + ([$labels[] as $l | $cells[$l][$st] // "n/a"] | join(" | ")) + " |"),
    "",
    ($runs[] | select(.status == "completed")
     | "- **\(.label) finished — \(.conclusion)**")
' "$tmp"/*.json
