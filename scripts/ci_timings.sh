#!/usr/bin/env bash
# Show CI job durations for recent runs.
# Usage: ci_timings.sh [branch] [run-count]
#   branch:    if omitted, shows runs across all branches
#   run-count: number of recent runs to show (default 1)

set -euo pipefail

branch="${1:-}"
count="${2:-1}"

branch_flag=()
if [[ -n "$branch" ]]; then
    branch_flag=(--branch "$branch")
fi

run_ids=$(gh run list "${branch_flag[@]}" --limit "$count" --status completed --json databaseId,createdAt,headBranch --jq '.[] | "\(.databaseId) \(.createdAt) \(.headBranch)"')

if [[ -z "$run_ids" ]]; then
    if [[ -n "$branch" ]]; then
        echo "No completed runs found on branch: $branch"
    else
        echo "No completed runs found"
    fi
    exit 1
fi

while IFS= read -r line; do
    run_id=$(echo "$line" | cut -d' ' -f1)
    created=$(echo "$line" | cut -d' ' -f2)
    run_branch=$(echo "$line" | cut -d' ' -f3-)

    if [[ -n "$branch" ]]; then
        echo "=== Run $run_id ($created) ==="
    else
        echo "=== Run $run_id ($created) [$run_branch] ==="
    fi
    gh run view "$run_id" --json jobs --jq '
        .jobs | sort_by(.name) | .[] |
        (if .startedAt and .completedAt then
            ((.completedAt | fromdateiso8601) - (.startedAt | fromdateiso8601)) | . as $secs |
            if . >= 60 then "\(. / 60 | floor)m \(. % 60)s"
            else "\(.)s"
            end
        else "—" end) as $dur |
        "\(.conclusion | if . == "success" then "✓" elif . == "failure" then "✗" elif . == "cancelled" then "⊘" else . end)  \($dur | if (. | length) < 6 then "  \(.)" else . end)  \(.name)"
    '
    echo ""
done <<< "$run_ids"
