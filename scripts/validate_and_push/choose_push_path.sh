#!/usr/bin/env bash
set -euo pipefail

# Determine whether validate_and_push should push directly or use the PR path.
# Keep GitHub calls inside this script so the agent can invoke one stable command
# instead of shelling through nested `gh` substitutions.

json_string() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '"%s"' "$value"
}

current_branch="$(git branch --show-current)"
default_branch="$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)"

requires_pr="false"
rules_status="not_checked"
push_path="direct"

if [[ "$current_branch" == "$default_branch" ]]; then
  name_with_owner="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
  rules_status="ok"

  if ! requires_pr="$(
    gh api "repos/${name_with_owner}/rules/branches/${default_branch}" \
      --jq 'any(.[]; .type=="pull_request")' 2>/dev/null
  )"; then
    rules_status="unavailable"
    requires_pr="false"
  fi

  if [[ "$requires_pr" == "true" ]]; then
    push_path="pr"
  fi
fi

printf '{'
printf '"current_branch":%s,' "$(json_string "$current_branch")"
printf '"default_branch":%s,' "$(json_string "$default_branch")"
printf '"requires_pr":%s,' "$requires_pr"
printf '"rules_status":%s,' "$(json_string "$rules_status")"
printf '"push_path":%s' "$(json_string "$push_path")"
printf '}\n'
