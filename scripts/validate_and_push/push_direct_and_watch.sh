#!/usr/bin/env bash
set -euo pipefail

# Push the current branch and watch the CI run for the pushed HEAD.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRANCH="$(git branch --show-current)"
SHA="$(git rev-parse HEAD)"

echo "Pushing ${BRANCH} to origin..."
git push origin "$BRANCH"

echo "Watching CI for ${BRANCH} at ${SHA}..."
bash "${SCRIPT_DIR}/watch_ci.sh" "$BRANCH" "$SHA"

echo "Pushed ${BRANCH} to origin and CI completed successfully for ${SHA}."
