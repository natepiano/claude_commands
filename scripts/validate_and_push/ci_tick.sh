#!/usr/bin/env bash
set -euo pipefail

# One combined stage table across one or more CI runs, for the /validate_and_push
# progress tick. Columns are the runs; rows are the union of their stage names, so
# a stage only one repo has reads `n/a` in the other. Each run's bullet prints the
# run's bare URL and start time. A running stage shows its elapsed time, a stage
# not yet started reads `waiting`, and a finished one shows only its conclusion.
# A settled run gets a bold note under the table.
#
# Usage: ci_tick.sh <owner/repo> <run-id> [<owner/repo> <run-id> ...]

if [ "$#" -lt 2 ] || [ $(($# % 2)) -ne 0 ]; then
  echo "Usage: ci_tick.sh <owner/repo> <run-id> [<owner/repo> <run-id> ...]" >&2
  exit 2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

i=0
read_failed=0
while [ "$#" -gt 0 ]; do
  repo="$1"
  run="$2"
  shift 2
  if ! gh run view "$run" --repo "$repo" \
    --json createdAt,updatedAt,status,conclusion,url,jobs |
    jq --arg label "${repo##*/}" --arg run "$run" '. + {label: $label, run_id: $run}' \
      >"$tmp/$i.json"; then
    read_failed=1
    jq -n --arg label "${repo##*/}" --arg run "$run" \
      '{label: $label, run_id: $run, status: "unavailable", conclusion: null,
        createdAt: "1970-01-01T00:00:00Z", url: "", error: "error: gh run view failed",
        jobs: [{name: "read", error: "error: gh run view failed"}]}' >"$tmp/$i.json"
  fi
  i=$((i + 1))
done

# The clock is in the user's zone setting; the machine's zone when the setting cannot be read.
lib="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
if zone="$("$lib/py" "$lib/user_zone.py" 2>/dev/null)" && [ -n "$zone" ]; then
  clock="$(TZ="$zone" date '+%H:%M:%S %Z')"
else
  clock="$(date '+%H:%M:%S %Z')"
fi

jq -s -r --arg clock "$clock" '
  def secs: if . >= 60 then "\((./60)|floor)m \((.%60)|floor)s" else "\(.|floor)s" end;
  def icon: if .status != "completed" then "…"
            elif .conclusion == "success" then "green"
            elif .conclusion == "skipped" then "skipped"
            elif .conclusion == "cancelled" then "cancelled"
            else "RED" end;
  # Only a running stage shows a time. GitHub stamps startedAt on a queued job
  # too, so the job status, not startedAt, decides whether it has started.
  def cell:
    if .error then .error
    elif .status == "completed" then icon
    elif .status == "in_progress" then
      (if .startedAt == null or (.startedAt|startswith("0001")) then "…"
       else "… \((now - (.startedAt|fromdateiso8601)) | (if . < 0 then 0 else . end) | secs)" end)
    else "waiting" end;

  . as $runs
  | [$runs[].label] as $labels
  | ([$runs[] | .jobs[].name]
     | reduce .[] as $n ([]; if index($n) then . else . + [$n] end)) as $stages
  | ($runs | map({(.label): (.jobs | map({key: .name, value: cell}) | from_entries)})
     | add) as $cells

  | "**\($clock)**",
    "",
    ($runs[]
     | if .error then "- **\(.label)** run `\(.run_id)` · \(.error)" else
       (if .status == "completed"
        then ((.updatedAt|fromdateiso8601) - (.createdAt|fromdateiso8601))
        else (now - (.createdAt|fromdateiso8601)) end) as $elapsed
     | ([.jobs[]|select(.conclusion=="success")]|length) as $g
     | ([.jobs[]|select(.conclusion=="skipped")]|length) as $s
     | "- **\(.label)** run `\(.run_id)` \(.url) · started **\(.createdAt|fromdateiso8601|strflocaltime("%H:%M:%S %Z"))** · \(.status) \(.conclusion // "-") · elapsed **\($elapsed|secs)** · **\($g) of \(.jobs|length) green\(if $s > 0 then " (\($s) skipped)" else "" end)**" end),
    "",
    ("| stage | " + ($labels | join(" | ")) + " |"),
    ("|---|" + ($labels | map("---|") | join(""))),
    ($stages[] as $st
     | "| \($st) | " + ([$labels[] as $l | $cells[$l][$st] // "n/a"] | join(" | ")) + " |"),
    "",
    ($runs[] | select(.status == "completed")
     | "- **\(.label) finished — \(.conclusion)**")
' "$tmp"/*.json

exit "$read_failed"
