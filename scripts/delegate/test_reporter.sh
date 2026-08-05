#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "${TEST_ROOT}"' EXIT

REPO="${TEST_ROOT}/repo"
RUN_ROOT="${TEST_ROOT}/runs"
SESSION_DIR="${RUN_ROOT}/run-1"
HISTORY_DIR="${TEST_ROOT}/history"
SYNC_STATE="${TEST_ROOT}/catalog-sync"
ASSESSMENT="${TEST_ROOT}/assessment.json"

mkdir -p "${REPO}" "${SESSION_DIR}" "${HISTORY_DIR}/runs"
touch "${SYNC_STATE}"
git -C "${REPO}" init -q -b main
printf 'before\n' > "${REPO}/tracked.txt"
printf '# Test plan\n\n### Phase 3: active\nstatus: active\n' > "${REPO}/plan.md"
git -C "${REPO}" add tracked.txt plan.md
git -C "${REPO}" -c user.name=Test -c user.email=test@example.com commit -qm initial
printf 'after\n' >> "${REPO}/tracked.txt"

cat > "${SESSION_DIR}/progress_history_state.json" <<EOF
{
  "schema_version": 1,
  "run_id": "run-1",
  "history_file": "${HISTORY_DIR}/runs/run-1.jsonl",
  "working_dir": "${REPO}",
  "worktree": "repo",
  "branch": "main",
  "plan_doc": "plan.md",
  "run_started_at": 1000,
  "project_started_at": 900,
  "status": "active",
  "main_agent": {"family": "codex", "model": "gpt-5.6-sol", "effort": "high", "session_id": "test"},
  "phase": {"id": "3", "title": "active", "instance_id": "phase-1", "started_at": 1100, "status": "active"},
  "pass": {
    "kind": "impl",
    "fix_pass": 0,
    "activity": "implementing",
    "called_task": "delegate.implementation",
    "called_agent": {"family": "codex", "model": "gpt-5.6-terra", "effort": "xhigh", "session_id": ""},
    "instance_id": "pass-1",
    "started_at": 1200,
    "status": "active"
  },
  "pending_calibration": null,
  "last_percent": null,
  "percent_started_at": null,
  "phase_last_percent": null,
  "phase_percent_started_at": null,
  "project_last_percent": null,
  "project_percent_started_at": null
}
EOF

mkdir -p "${RUN_ROOT}/stale-run"
jq '.run_id = "stale-run" | .run_started_at = 100 | .phase.status = "completed" | .pass.status = "completed"' \
  "${SESSION_DIR}/progress_history_state.json" \
  > "${RUN_ROOT}/stale-run/progress_history_state.json"

printf 'Implement the active phase.\n' > "${SESSION_DIR}/implementation_prompt.md"
: > "${SESSION_DIR}/progress_baseline_status"
printf '[wrapper] implementing\n' > "${SESSION_DIR}/heartbeat.log"
printf 'implementing\n' > "${SESSION_DIR}/impl_status"
cat > "${ASSESSMENT}" <<'EOF'
{"project_raw_percent": 40, "phase_raw_percent": 55, "cap_stage": "implementation", "activity": "implementing the tracked behavior", "summary": "The tracked behavior is being edited; verification remains."}
EOF

OUTPUT="$({
  cd "${REPO}"
  PLAN_DELEGATE_RUN_ROOT="${RUN_ROOT}" \
  PLAN_DELEGATE_HISTORY_DIR="${HISTORY_DIR}" \
  PLAN_DELEGATE_NOW_EPOCH=1500 \
  PLAN_DELEGATE_REPORTER_ASSESSMENT_FILE="${ASSESSMENT}" \
  CODEX_CATALOG_SYNC_STATE_FILE="${SYNC_STATE}" \
    bash "${SCRIPT_DIR}/reporter.sh" once
})"

[[ "${OUTPUT}" == *'**repo - main**'* ]]
[[ "${OUTPUT}" == *'**40% complete - elapsed'* ]]
[[ "${OUTPUT}" == *'**Phase 3: active**'* ]]
[[ "${OUTPUT}" == *'**55% complete - elapsed'* ]]
[[ "${OUTPUT}" == *'The tracked behavior is being edited; verification remains.'* ]]
[[ "${OUTPUT}" == *'Reporter agent: '* ]]
[[ "${OUTPUT}" == *'Total status time: '* ]]
grep -q '^task=delegate.reporter$' "${SESSION_DIR}/reporter/agent"
jq -e 'select(.event_type == "progress_reported")' "${HISTORY_DIR}/runs/run-1.jsonl" >/dev/null

LIST_OUTPUT="$(PLAN_DELEGATE_RUN_ROOT="${RUN_ROOT}" bash "${SCRIPT_DIR}/reporter.sh" list)"
[[ "${LIST_OUTPUT}" == *$'run-1\n  project: repo\n  branch: main\n  phase: 3 [active]'* ]]
[[ "${LIST_OUTPUT}" != *$'\t'* ]]

set +e
ERROR_OUTPUT="$({
  cd "${TEST_ROOT}"
  PLAN_DELEGATE_RUN_ROOT="${RUN_ROOT}" bash "${SCRIPT_DIR}/reporter.sh" once
} 2>&1)"
ERROR_CODE=$?
set -e
[[ "${ERROR_CODE}" -eq 2 ]]
[[ "${ERROR_OUTPUT}" == *'multiple active delegated-plan runs found'* ]]
[[ "${ERROR_OUTPUT}" == *'reporter --run-id <prefix>'* ]]
[[ "${ERROR_OUTPUT}" != *$'\t'* ]]

SPINNER_OUTPUT="$({
  cd "${TEST_ROOT}"
  PLAN_DELEGATE_RUN_ROOT="${RUN_ROOT}" \
  PLAN_DELEGATE_HISTORY_DIR="${HISTORY_DIR}" \
  PLAN_DELEGATE_NOW_EPOCH=1520 \
  PLAN_DELEGATE_REPORTER_ASSESSMENT_FILE="${ASSESSMENT}" \
  PLAN_DELEGATE_REPORTER_ASSESSMENT_DELAY_SECONDS=1 \
  PLAN_DELEGATE_REPORTER_FORCE_SPINNER=1 \
  CODEX_CATALOG_SYNC_STATE_FILE="${SYNC_STATE}" \
    bash "${SCRIPT_DIR}/reporter.sh" once --run-id run
} 2>&1)"
[[ "${SPINNER_OUTPUT}" == *'Reporter: checking status'* ]]
[[ "${SPINNER_OUTPUT}" == *'Reporter agent: 1s.'* || "${SPINNER_OUTPUT}" == *'Reporter agent: 2s.'* ]]
[[ "${SPINNER_OUTPUT}" == *'Total status time: 1s.'* || "${SPINNER_OUTPUT}" == *'Total status time: 2s.'* ]]

DRY_PROMPT="${TEST_ROOT}/dry-prompt.md"
DRY_CONFIG="${TEST_ROOT}/agents.conf"
printf 'Report progress.\n' > "${DRY_PROMPT}"
CODEX_DRY="$(
  AGENT_EXEC_DRY_RUN=1 CODEX_CATALOG_SYNC_STATE_FILE="${SYNC_STATE}" \
    bash "${SCRIPT_DIR}/../agents/agent_exec.sh" delegate.reporter readonly \
    "${REPO}" "${DRY_PROMPT}" "${TEST_ROOT}/codex.out" "${TEST_ROOT}/codex.log"
)"
[[ "${CODEX_DRY}" == *'codex exec'* ]]
[[ "${CODEX_DRY}" == *'gpt-5.6-sol'* ]]
[[ "${CODEX_DRY}" == *'--sandbox read-only'* ]]

sed 's/^delegate=codex$/delegate=claude/' "${HOME}/.claude/config/agents.conf" > "${DRY_CONFIG}"
touch "${SYNC_STATE}"
CLAUDE_DRY="$(
  AGENT_EXEC_DRY_RUN=1 AGENTS_CONFIG_FILE="${DRY_CONFIG}" \
  CODEX_CATALOG_SYNC_STATE_FILE="${SYNC_STATE}" \
    bash "${SCRIPT_DIR}/../agents/agent_exec.sh" delegate.reporter readonly \
    "${REPO}" "${DRY_PROMPT}" "${TEST_ROOT}/claude.out" "${TEST_ROOT}/claude.log"
)"
[[ "${CLAUDE_DRY}" == *'claude --print'* ]]
[[ "${CLAUDE_DRY}" == *'--permission-mode plan'* ]]
[[ "${CLAUDE_DRY}" == *'--model sonnet --effort medium'* ]]

PLAN_DELEGATE_HISTORY_DIR="${HISTORY_DIR}" PLAN_DELEGATE_NOW_EPOCH=1540 \
  python3 "${SCRIPT_DIR}/progress_history.py" finish-phase \
  --session-dir "${SESSION_DIR}" --status completed
BETWEEN_OUTPUT="$({
  cd "${REPO}"
  PLAN_DELEGATE_RUN_ROOT="${RUN_ROOT}" bash "${SCRIPT_DIR}/reporter.sh" once
})"
[[ "${BETWEEN_OUTPUT}" == *'Reporter: run run-1 is active between phases.'* ]]
[[ "${BETWEEN_OUTPUT}" == *'Latest phase: 3: active [completed]'* ]]
[[ "${BETWEEN_OUTPUT}" == *'Reporter agent: not run.'* ]]
[[ "${BETWEEN_OUTPUT}" == *'Total status time: 0s.'* ]]

WATCH_OUTPUT="${TEST_ROOT}/watch.out"
(
  PLAN_DELEGATE_RUN_ROOT="${RUN_ROOT}" \
  PLAN_DELEGATE_HISTORY_DIR="${HISTORY_DIR}" \
  PLAN_DELEGATE_NOW_EPOCH=1550 \
  PLAN_DELEGATE_REPORTER_ASSESSMENT_FILE="${ASSESSMENT}" \
  CODEX_CATALOG_SYNC_STATE_FILE="${SYNC_STATE}" \
    bash "${SCRIPT_DIR}/reporter.sh" watch --session-dir "${SESSION_DIR}" --interval 1
) > "${WATCH_OUTPUT}" &
WATCH_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  [[ -r "${SESSION_DIR}/reporter/watch.lock/pid" ]] && break
  sleep 0.05
done
[[ -r "${SESSION_DIR}/reporter/watch.lock/pid" ]]
PLAN_DELEGATE_HISTORY_DIR="${HISTORY_DIR}" PLAN_DELEGATE_NOW_EPOCH=1600 \
  python3 "${SCRIPT_DIR}/progress_history.py" finish-run \
  --session-dir "${SESSION_DIR}" --status completed
wait "${WATCH_PID}"
grep -q '^Reporter watch: run run-1 every 1s.$' "${WATCH_OUTPUT}"
grep -q '^Reporter: run run-1 ended with status completed.$' "${WATCH_OUTPUT}"

echo "reporter tests passed"
