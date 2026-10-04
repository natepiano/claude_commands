#!/usr/bin/env bash
set -euo pipefail

# Top-level validate-and-push workflow. Exits with code 2 when a PR branch name
# needs user confirmation; in that case it prints prepare_pr_push.sh JSON. On the
# direct path it pushes and hands CI watching to the agent; it does not block on CI.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --to and --fix-commit support periodic CI points; showrunner merges also use
# --quick and may use --cancel-prior.
#   --to <branch>          push HEAD to origin/<branch> instead of the current
#                          branch; always the direct path, so a run on the
#                          default branch reaches CI without landing on it
#   --fix-commit <message> commit validation fixes as a new commit with this
#                          message instead of amending the last commit, so a
#                          recorded checkpoint commit is never rewritten
#   --quick                run only the cargo-mend, rustfmt and taplo steps
#                          (see validate_ci.sh)
#   --cancel-prior         cancel superseded queued or running CI after a push
PUSH_TARGET_BRANCH=""
CANCEL_PRIOR=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --to) PUSH_TARGET_BRANCH="${2:?--to needs a branch name}"; shift 2 ;;
    --fix-commit) export VALIDATE_FIX_COMMIT_MESSAGE="${2:?--fix-commit needs a message}"; shift 2 ;;
    --quick) export VALIDATE_QUICK=1; shift ;;
    --cancel-prior) CANCEL_PRIOR=true; shift ;;
    *) echo "validate_and_push.sh: unknown argument: $1" >&2; exit 64 ;;
  esac
done

cancel_prior_runs() {
  local branch sha workflows workflow runs run_id head_sha status live_status
  branch="${PUSH_TARGET_BRANCH:-$(git branch --show-current)}"
  sha="$(git rev-parse HEAD)"
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

echo "=== STEP: validation ==="
bash "${SCRIPT_DIR}/run_validation.sh"

push_status=0
if [ -n "$PUSH_TARGET_BRANCH" ]; then
  echo "=== STEP: direct push to ${PUSH_TARGET_BRANCH} ==="
  PUSH_TARGET_BRANCH="$PUSH_TARGET_BRANCH" bash "${SCRIPT_DIR}/push_direct.sh" || push_status=$?
  if [ "$CANCEL_PRIOR" = true ]; then
    cancel_prior_runs
  fi
  exit "$push_status"
fi

echo "=== STEP: choose push path ==="
PUSH_PATH_JSON="$(bash "${SCRIPT_DIR}/choose_push_path.sh")"
printf '%s\n' "$PUSH_PATH_JSON"

if [[ "$PUSH_PATH_JSON" == *'"push_path":"pr"'* ]]; then
  echo "=== STEP: prepare PR branch ==="
  bash "${SCRIPT_DIR}/prepare_pr_push.sh"
  exit 2
fi

echo "=== STEP: direct push ==="
bash "${SCRIPT_DIR}/push_direct.sh" || push_status=$?
if [ "$CANCEL_PRIOR" = true ]; then
  cancel_prior_runs
fi
exit "$push_status"
