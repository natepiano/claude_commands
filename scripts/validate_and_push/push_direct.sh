#!/usr/bin/env bash
set -euo pipefail

# Push the current branch, identify the CI run it triggered, and hand watching
# to the agent.
#
# The agent watches with a 3-minute ScheduleWakeup tick instead of a blocking
# `gh run watch`. That surfaces a per-job status line on every tick, and a red
# job gets diagnosed the tick it turns red rather than after the whole run
# settles. This script therefore stops at the push and prints the identifiers
# the tick loop needs.
#
# A repo's post-push hook (see post_push_hook.sh) runs right after the push.
# Its failure is reported after the CI handoff and becomes this script's exit
# status, so the handoff block is always printed.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRANCH="$(git branch --show-current)"

echo "Pushing ${BRANCH} to origin..."
git push origin "$BRANCH"

SHA="$(git rev-parse HEAD)"
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"

HOOK_STATUS=0
bash "${SCRIPT_DIR}/post_push_hook.sh" "$BRANCH" "$SHA" || HOOK_STATUS=$?

# `gh run list --commit` requires a full SHA; a short SHA silently returns
# nothing. The run also does not exist the instant the push lands.
RUN_ID=""
for i in $(seq 1 40); do
  RUN_ID="$(gh run list --branch "$BRANCH" --commit "$SHA" --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)"
  if [ -n "$RUN_ID" ]; then
    break
  fi
  echo "Attempt ${i}/40: no CI run yet for ${SHA}, waiting 3s..."
  sleep 3
done

echo
echo "=== CI HANDOFF TO AGENT ==="
echo "repo:   ${REPO}"
echo "branch: ${BRANCH}"
echo "sha:    ${SHA}"
if [ -n "$RUN_ID" ]; then
  echo "run_id: ${RUN_ID}"
  echo "url:    https://github.com/${REPO}/actions/runs/${RUN_ID}"
else
  echo "run_id: none"
  echo "No run appeared within 120s. Find it with:"
  echo "  gh run list --branch ${BRANCH} --commit ${SHA} --json databaseId,status"
fi
echo
echo "Watch this run with a 3-minute ScheduleWakeup tick. Do not block on"
echo "\`gh run watch\`, and do not sleep-poll in-band."

if [ "$HOOK_STATUS" -ne 0 ]; then
  echo
  echo "The post-push hook failed (see above); the push itself succeeded." >&2
fi
exit "$HOOK_STATUS"
