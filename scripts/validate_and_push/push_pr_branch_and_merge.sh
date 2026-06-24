#!/usr/bin/env bash
set -euo pipefail

# Move unpushed default-branch commits onto a PR branch, wait for checks, merge,
# and restore the local default branch.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ci_watch_lib.sh
source "${SCRIPT_DIR}/ci_watch_lib.sh"

BRANCH_NAME="${1:?Usage: push_pr_branch_and_merge.sh <branch-name>}"

DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)"
CURRENT_BRANCH="$(git branch --show-current)"

if ! git check-ref-format --branch "$BRANCH_NAME" >/dev/null; then
  echo "ERROR: invalid branch name: ${BRANCH_NAME}" >&2
  exit 1
fi

if [[ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" && "$CURRENT_BRANCH" != "$BRANCH_NAME" ]]; then
  echo "ERROR: PR path must start on ${DEFAULT_BRANCH} or ${BRANCH_NAME}" >&2
  exit 1
fi

if [[ "$CURRENT_BRANCH" == "$DEFAULT_BRANCH" && -z "$(git log --format="%h %s" "origin/${DEFAULT_BRANCH}..HEAD")" ]]; then
  echo "Nothing to push."
  exit 0
fi

if [[ "$CURRENT_BRANCH" == "$DEFAULT_BRANCH" ]]; then
  echo "Creating PR branch ${BRANCH_NAME} from current HEAD..."
  git switch -c "$BRANCH_NAME"
  git branch -f "$DEFAULT_BRANCH" "origin/${DEFAULT_BRANCH}"
else
  echo "Resuming PR branch ${BRANCH_NAME}..."
fi

echo "Pushing ${BRANCH_NAME}..."
git push -u origin "$BRANCH_NAME"

if PR_NUMBER="$(gh pr view --json number -q .number 2>/dev/null)"; then
  echo "Using existing PR #${PR_NUMBER}."
else
  echo "Opening pull request..."
  gh pr create --fill
  PR_NUMBER="$(gh pr view --json number -q .number)"
fi

echo "Watching checks for PR #${PR_NUMBER}..."
MAX_ATTEMPTS=24
POLL_INTERVAL=5
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  set +e
  CHECK_OUTPUT="$(gh pr checks "$PR_NUMBER" 2>&1)"
  CHECK_STATUS="$?"
  set -e

  if [[ "$CHECK_OUTPUT" != *"no checks reported"* ]]; then
    break
  fi

  echo "Attempt ${attempt}/${MAX_ATTEMPTS}: no checks reported yet, waiting ${POLL_INTERVAL}s..."
  sleep "$POLL_INTERVAL"
done

if [[ "$CHECK_OUTPUT" == *"no checks reported"* ]]; then
  echo "ERROR: no checks reported for PR #${PR_NUMBER} after polling." >&2
  exit 1
fi

if ! watch_pr_checks_until_settled "$PR_NUMBER"; then
  echo "ERROR: CI failed for PR #${PR_NUMBER}. Leaving ${BRANCH_NAME} checked out for iteration." >&2
  exit 1
fi

echo "Merging PR #${PR_NUMBER}..."
set +e
MERGE_OUTPUT="$(gh pr merge "$PR_NUMBER" --rebase --delete-branch 2>&1)"
MERGE_STATUS="$?"
set -e

if [[ -n "$MERGE_OUTPUT" ]]; then
  printf '%s\n' "$MERGE_OUTPUT"
fi

if [[ "$MERGE_STATUS" -ne 0 ]]; then
  PR_STATE="$(gh pr view "$PR_NUMBER" --json state -q .state 2>/dev/null || true)"

  if [[ "$PR_STATE" == "MERGED" && "$MERGE_OUTPUT" == *"Reference does not exist"* ]]; then
    echo "PR #${PR_NUMBER} merged and remote branch ${BRANCH_NAME} was already deleted; continuing."
  else
    echo "ERROR: failed to merge PR #${PR_NUMBER}." >&2
    exit "$MERGE_STATUS"
  fi
fi
MERGE_SHA="$(gh pr view "$PR_NUMBER" --json mergeCommit -q .mergeCommit.oid 2>/dev/null || true)"

git switch "$DEFAULT_BRANCH"
git pull --ff-only

if [[ -n "$MERGE_SHA" ]]; then
  echo "Merged PR #${PR_NUMBER} as ${MERGE_SHA}. CI was green before merge."
else
  echo "Merged PR #${PR_NUMBER}. CI was green before merge."
fi
