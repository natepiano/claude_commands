#!/usr/bin/env bash
set -euo pipefail

# Polls for a GitHub CI run matching a commit SHA, then watches it.
# Usage: watch_ci.sh <branch> <sha>
#
# Designed to be launched via `run_in_background` so it doesn't block
# the conversation. The background task notification reports completion.

BRANCH="${1:?Usage: watch_ci.sh <branch> <sha>}"
SHORT_SHA="${2:?Usage: watch_ci.sh <branch> <sha>}"

# gh run list --commit requires a full SHA; short SHAs silently return nothing
SHA=$(git rev-parse "$SHORT_SHA")
MAX_ATTEMPTS=10
POLL_INTERVAL=3

RUN_ID=""
for i in $(seq 1 "$MAX_ATTEMPTS"); do
  RUN_ID=$(gh run list --branch "$BRANCH" --commit "$SHA" --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)
  if [ -n "$RUN_ID" ]; then
    break
  fi
  echo "Attempt $i/$MAX_ATTEMPTS: no CI run yet, waiting ${POLL_INTERVAL}s..."
  sleep "$POLL_INTERVAL"
done

if [ -z "$RUN_ID" ]; then
  echo "ERROR: No CI run found for commit $SHA on branch $BRANCH after $MAX_ATTEMPTS attempts"
  exit 1
fi

echo "Found CI run $RUN_ID for commit $SHA"
gh run watch "$RUN_ID"
