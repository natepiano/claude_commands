#!/usr/bin/env bash
# Delete review prose after one delegated phase is safely shrunk and checkpointed.

set -euo pipefail

SESSION_DIR="${1:?Usage: clear_phase_review.sh <session_dir> <phase_id>}"
PHASE_ID="${2:?Usage: clear_phase_review.sh <session_dir> <phase_id>}"

if [[ ! -d "${SESSION_DIR}" || "${SESSION_DIR}" == "/" || "${SESSION_DIR}" == "${HOME}" \
    || ! -f "${SESSION_DIR}/progress_history_state.json" ]]; then
  printf 'ERROR: unsafe or missing session directory: %s\n' "${SESSION_DIR}" >&2
  exit 2
fi
if [[ ! "${PHASE_ID}" =~ ^[[:alnum:]._-]+$ ]]; then
  printf 'ERROR: invalid phase id: %s\n' "${PHASE_ID}" >&2
  exit 2
fi

shopt -s nullglob
FILES=(
  "${SESSION_DIR}/phase_review_retrospective_${PHASE_ID}.md"
  "${SESSION_DIR}/phase_review_outcomes_${PHASE_ID}.md"
  "${SESSION_DIR}/architect_prompt_${PHASE_ID}.md"
  "${SESSION_DIR}/review_findings.txt"
  "${SESSION_DIR}/review_agent.log"
  "${SESSION_DIR}/review_agent"
  "${SESSION_DIR}/review_status"
  "${SESSION_DIR}"/review_prompt_*.md
  "${SESSION_DIR}"/review_findings_*.txt
  "${SESSION_DIR}"/review_agent_*.log
  # A phase's broad review runs one reviewer per lens, and each suffixes its own
  # copy of the four unnumbered names above.
  "${SESSION_DIR}"/review_findings_*
  "${SESSION_DIR}"/review_agent_*
  "${SESSION_DIR}"/review_status_*
  # A launch that names its phase keeps its review files in that phase's directory,
  # so only this phase's are cleared and a phase running beside it keeps its own.
  "${SESSION_DIR}/phases/${PHASE_ID}"/review_*
  "${SESSION_DIR}/phases/${PHASE_ID}"/ux_shots_*
)

if ((${#FILES[@]})); then
  rm -f -- "${FILES[@]}"
fi
