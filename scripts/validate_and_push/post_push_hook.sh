#!/usr/bin/env bash
set -euo pipefail

# Run the repository's post-push hook after validate_and_push lands the default
# branch on origin.
#
# Opt-in per repo: a `.claude/config/post_push.sh` at the repo root. It runs from
# the repo root under bash with LANDED_SHA and LANDED_BRANCH exported, and only
# when the landed branch is the default branch -- a feature-branch push never
# triggers it. The push has already happened by the time this runs, so a failing
# hook rolls nothing back; it prints how to rerun the hook by hand and exits
# non-zero so the agent reports the failure instead of calling the push done.
#
# Usage: post_push_hook.sh <landed-branch> [<landed-sha>]

LANDED_BRANCH="${1:?Usage: post_push_hook.sh <landed-branch> [<landed-sha>]}"
LANDED_SHA="${2:-$(git rev-parse HEAD)}"
DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)"
REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK="${REPO_ROOT}/.claude/config/post_push.sh"

if [[ "$LANDED_BRANCH" != "$DEFAULT_BRANCH" ]]; then
  exit 0
fi
if [[ ! -f "$HOOK" ]]; then
  exit 0
fi

echo "=== STEP: post-push hook (${DEFAULT_BRANCH} at ${LANDED_SHA:0:9}) ==="
export LANDED_SHA LANDED_BRANCH
if ! (cd "$REPO_ROOT" && bash "$HOOK"); then
  echo "ERROR: post-push hook failed: ${HOOK}" >&2
  echo "The push itself succeeded. Fix the cause, then rerun the hook from ${REPO_ROOT}:" >&2
  echo "  LANDED_SHA=${LANDED_SHA} LANDED_BRANCH=${LANDED_BRANCH} bash ${HOOK}" >&2
  exit 1
fi
