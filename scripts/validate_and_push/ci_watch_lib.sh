#!/usr/bin/env bash
# Shared helpers for watching GitHub CI through transient API/network errors.
#
# A dropped connection, a server error or a rate limit can stop `gh run watch`
# or `gh pr checks --watch` while CI still runs. push_pr_branch_and_merge.sh
# uses these helpers to retry temporary errors before it merges the PR.
# The helpers fail when CI fails or the retry limit is reached.
#
# Source this file; it defines watch_run_until_settled and
# watch_pr_checks_until_settled. Both return 0 on green, 1 on a real red
# conclusion or after exhausting transient retries.

# Max consecutive transient retries before giving up, and the wait between them.
CI_WATCH_MAX_TRANSIENT_RETRIES="${CI_WATCH_MAX_TRANSIENT_RETRIES:-10}"
CI_WATCH_RETRY_INTERVAL="${CI_WATCH_RETRY_INTERVAL:-15}"

# watch_run_until_settled <run-id>
# Watches a workflow run. `gh run watch --exit-status` exits non-zero both when
# the run concludes red and on a transient stream error; we query the run's
# actual status/conclusion to tell them apart.
watch_run_until_settled() {
  local run_id="$1"
  local transient=0
  while :; do
    set +e
    gh run watch "$run_id" --exit-status
    local watch_status=$?
    set -e

    if [ "$watch_status" -eq 0 ]; then
      return 0
    fi

    local status conclusion
    set +e
    status="$(gh run view "$run_id" --json status --jq '.status' 2>/dev/null)"
    conclusion="$(gh run view "$run_id" --json conclusion --jq '.conclusion' 2>/dev/null)"
    set -e

    if [ "$status" = "completed" ]; then
      if [ "$conclusion" = "success" ]; then
        return 0
      fi
      echo "ERROR: CI run ${run_id} concluded with '${conclusion:-unknown}'." >&2
      return 1
    fi

    transient=$((transient + 1))
    if [ "$transient" -gt "$CI_WATCH_MAX_TRANSIENT_RETRIES" ]; then
      echo "ERROR: lost contact with GitHub while watching run ${run_id}; exceeded ${CI_WATCH_MAX_TRANSIENT_RETRIES} transient retries." >&2
      return 1
    fi
    echo "Transient error watching run ${run_id} (exit ${watch_status}); run not concluded (status='${status:-unknown}'). Retry ${transient}/${CI_WATCH_MAX_TRANSIENT_RETRIES} in ${CI_WATCH_RETRY_INTERVAL}s..."
    sleep "$CI_WATCH_RETRY_INTERVAL"
  done
}

# watch_pr_checks_until_settled <pr-number>
# Watches PR checks. `gh pr checks --watch` exits non-zero both when a check
# concludes red and on a transient stream error; we inspect the per-check
# `bucket` values (pass|fail|pending|skipping|cancel) to tell them apart.
watch_pr_checks_until_settled() {
  local pr="$1"
  local transient=0
  while :; do
    set +e
    gh pr checks "$pr" --watch
    local watch_status=$?
    set -e

    if [ "$watch_status" -eq 0 ]; then
      return 0
    fi

    local buckets query_status
    set +e
    buckets="$(gh pr checks "$pr" --json bucket --jq '.[].bucket' 2>/dev/null)"
    query_status=$?
    set -e

    if [ "$query_status" -eq 0 ] && [ -n "$buckets" ]; then
      if printf '%s\n' "$buckets" | grep -qE '^(fail|cancel)$'; then
        echo "ERROR: a required check concluded red for PR #${pr}." >&2
        return 1
      fi
      if ! printf '%s\n' "$buckets" | grep -qvE '^(pass|skipping)$'; then
        return 0
      fi
    fi

    transient=$((transient + 1))
    if [ "$transient" -gt "$CI_WATCH_MAX_TRANSIENT_RETRIES" ]; then
      echo "ERROR: lost contact with GitHub while watching PR #${pr} checks; exceeded ${CI_WATCH_MAX_TRANSIENT_RETRIES} transient retries." >&2
      return 1
    fi
    echo "Transient error watching PR #${pr} checks (exit ${watch_status}); checks not concluded. Retry ${transient}/${CI_WATCH_MAX_TRANSIENT_RETRIES} in ${CI_WATCH_RETRY_INTERVAL}s..."
    sleep "$CI_WATCH_RETRY_INTERVAL"
  done
}
