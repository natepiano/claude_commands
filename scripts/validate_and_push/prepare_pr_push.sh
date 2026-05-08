#!/usr/bin/env bash
set -euo pipefail

# Print the commits that need a PR and propose a branch name.

json_string() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '"%s"' "$value"
}

slugify() {
  local subject="$1"
  subject="$(printf '%s' "$subject" | sed -E 's/^(style|refactor|fix|feat|chore|docs)(\([^)]+\))?:[[:space:]]*//')"
  printf '%s' "$subject" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' \
    | cut -c 1-50 \
    | sed -E 's/-+$//'
}

DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)"
COMMITS=()
while IFS= read -r commit; do
  COMMITS+=("$commit")
done < <(git log --format="%h %s" "origin/${DEFAULT_BRANCH}..HEAD")

if [[ "${#COMMITS[@]}" -eq 0 ]]; then
  printf '{"status":"nothing_to_push","default_branch":%s}\n' "$(json_string "$DEFAULT_BRANCH")"
  exit 0
fi

SUBJECTS=()
while IFS= read -r subject; do
  SUBJECTS+=("$subject")
done < <(git log --format="%s" "origin/${DEFAULT_BRANCH}..HEAD")

shared_prefix=""
all_share_prefix="true"
conventional_subject_re='^(style|refactor|fix|feat|chore|docs)(\([^)]+\))?:[[:space:]]+(.+)'
for subject in "${SUBJECTS[@]}"; do
  if [[ "$subject" =~ $conventional_subject_re ]]; then
    prefix="${BASH_REMATCH[1]}"
    if [[ -z "$shared_prefix" ]]; then
      shared_prefix="$prefix"
    elif [[ "$shared_prefix" != "$prefix" ]]; then
      all_share_prefix="false"
      break
    fi
  else
    all_share_prefix="false"
    break
  fi
done

shortest_subject="${SUBJECTS[0]}"
if [[ "$all_share_prefix" == "true" ]]; then
  for subject in "${SUBJECTS[@]}"; do
    stripped="$(printf '%s' "$subject" | sed -E 's/^(style|refactor|fix|feat|chore|docs)(\([^)]+\))?:[[:space:]]*//')"
    current_stripped="$(printf '%s' "$shortest_subject" | sed -E 's/^(style|refactor|fix|feat|chore|docs)(\([^)]+\))?:[[:space:]]*//')"
    if [[ "${#stripped}" -lt "${#current_stripped}" ]]; then
      shortest_subject="$subject"
    fi
  done
  proposed_branch="${shared_prefix}/$(slugify "$shortest_subject")"
else
  proposed_branch="$(slugify "${SUBJECTS[0]}")"
fi

printf '{"status":"needs_pr_branch",'
printf '"default_branch":%s,' "$(json_string "$DEFAULT_BRANCH")"
printf '"proposed_branch":%s,' "$(json_string "$proposed_branch")"
printf '"commits":['
for i in "${!COMMITS[@]}"; do
  if [[ "$i" -gt 0 ]]; then
    printf ','
  fi
  json_string "${COMMITS[$i]}"
done
printf ']}\n'
