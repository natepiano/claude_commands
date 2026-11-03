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
# PUSH_TARGET_BRANCH (from validate_and_push.sh --to) names the remote branch
# to push HEAD to; otherwise the current branch is pushed under its own name.
BRANCH="${PUSH_TARGET_BRANCH:-$(git branch --show-current)}"

SHA="$(git rev-parse HEAD)"
PUSHED=true
PUBLISH_ONLY=false
FOLLOW_PUBLISHED=false
# CI points release the merge lock between publication and CI discovery.
# The publication receipt is the final line: <sha> <pushed|contained>.
case "${1:-}" in
  --publish-only)
    [ "$#" -eq 2 ] || { echo 'usage: push_direct.sh --publish-only <sha>' >&2; exit 64; }
    PUBLISH_ONLY=true
    SHA="$2"
    ;;
  --follow-published)
    [ "$#" -eq 3 ] || { echo 'usage: push_direct.sh --follow-published <sha> <pushed|contained>' >&2; exit 64; }
    FOLLOW_PUBLISHED=true
    SHA="$2"
    case "$3" in
      pushed) PUSHED=true ;;
      contained) PUSHED=false ;;
      *) echo 'push_direct.sh: invalid publication state' >&2; exit 64 ;;
    esac
    ;;
  '') ;;
  *) echo "push_direct.sh: unknown argument: $1" >&2; exit 64 ;;
esac

if [ "$FOLLOW_PUBLISHED" = false ]; then
  if [ -n "${CI_VALIDATED_BASE:-}" ]; then
    git fetch --no-tags origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"
    REMOTE_TIP="$(git rev-parse "refs/remotes/origin/${BRANCH}")"
    if [ "$SHA" = "$CI_VALIDATED_BASE" ] &&
       git merge-base --is-ancestor "$SHA" "$REMOTE_TIP"; then
      PUSHED=false
      SHA="$REMOTE_TIP"
      echo "Validated ${CI_VALIDATED_BASE}; origin/${BRANCH} contains it; no push."
    elif ! git merge-base --is-ancestor "$CI_VALIDATED_BASE" "$SHA" ||
         ! git merge-base --is-ancestor "$REMOTE_TIP" "$SHA"; then
      echo "merge branch moved during validation" >&2
      exit 1
    fi
  fi

  if [ "$PUSHED" = true ]; then
    echo "Pushing ${BRANCH} to origin..."
    PUSH_COMMIT=HEAD
    if [ -n "${CI_VALIDATED_BASE:-}" ]; then
      PUSH_COMMIT="$SHA"
    fi
    PUSH_STATUS=0
    git push origin "${PUSH_COMMIT}:refs/heads/${BRANCH}" || PUSH_STATUS=$?
    if [ "$PUSH_STATUS" -ne 0 ]; then
      if [ -n "${CI_VALIDATED_BASE:-}" ]; then
        echo "merge branch moved during validation" >&2
      fi
      exit "$PUSH_STATUS"
    fi
  fi
  if [ "$PUBLISH_ONLY" = true ]; then
    if [ "$PUSHED" = true ]; then
      printf '%s pushed\n' "$SHA"
    else
      printf '%s contained\n' "$SHA"
    fi
    exit 0
  fi
fi
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"

HOOK_STATUS=0
if [ "$PUSHED" = true ]; then
  bash "${SCRIPT_DIR}/post_push_hook.sh" "$BRANCH" "$SHA" || HOOK_STATUS=$?
fi

# `gh run list --commit` requires a full SHA; a short SHA silently returns
# nothing. The run also does not exist the instant the push lands.
RUN=""
for i in $(seq 1 40); do
  RUN="$(gh run list --branch "$BRANCH" --commit "$SHA" --json databaseId,createdAt \
    --jq '.[0] | select(. != null) | "\(.databaseId) \(.createdAt | fromdateiso8601 | strflocaltime("%Y-%m-%d %H:%M:%S %Z"))"' \
    2>/dev/null || true)"
  if [ -n "$RUN" ]; then
    break
  fi
  echo "Attempt ${i}/40: no CI run yet for ${SHA}, waiting 3s..."
  sleep 3
done

echo
echo "=== CI HANDOFF TO AGENT ==="
echo "repo:    ${REPO}"
echo "branch:  ${BRANCH}"
echo "sha:     ${SHA}"
if [ -n "$RUN" ]; then
  RUN_ID="${RUN%% *}"
  echo "run_id:  ${RUN_ID}"
  echo "url:     https://github.com/${REPO}/actions/runs/${RUN_ID}"
  echo "started: ${RUN#* }"
else
  echo "run_id:  none"
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
cancel_prior_runs() {
  local branch sha workflows workflow runs run_id head_sha status live_status
  branch="$BRANCH"
  sha="$SHA"
  if ! workflows="$(gh run list --branch "$branch" --commit "$sha" --json workflowName \
    --jq '[.[].workflowName | select(. != null)] | unique[]' 2>/dev/null)"; then
    printf 'warning: failed to list runs for %s on %s\n' "${sha:0:7}" "$branch" >&2
    return 0
  fi
  if [ -z "$workflows" ]; then
    printf 'no run for %s yet; nothing cancelled\n' "${sha:0:7}"
    return 0
  fi

  while IFS= read -r workflow; do
    for live_status in queued in_progress; do
      if ! runs="$(gh run list --workflow "$workflow" --branch "$branch" --status "$live_status" \
        --json databaseId,headSha,status \
        --jq '.[] | "\(.databaseId)\t\(.headSha)\t\(.status)"' 2>/dev/null)"; then
        printf 'warning: failed to list %s runs for %s on %s\n' "$live_status" "$workflow" "$branch" >&2
        continue
      fi
      [ -n "$runs" ] || continue
      while IFS=$'\t' read -r run_id head_sha status; do
        if [ "$head_sha" != "$sha" ] && { [ "$status" = queued ] || [ "$status" = in_progress ]; }; then
          if gh run cancel "$run_id" >/dev/null 2>&1; then
            printf 'cancelled run %s (%s)\n' "$run_id" "${head_sha:0:7}"
          else
            printf 'warning: failed to cancel run %s\n' "$run_id" >&2
          fi
        fi
      done <<< "$runs"
    done
  done <<< "$workflows"
}

if [ "${CANCEL_PRIOR:-false}" = true ]; then
  cancel_prior_runs
fi
exit "$HOOK_STATUS"
