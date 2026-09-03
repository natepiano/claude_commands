#!/usr/bin/env bash
# end_friend.sh — Finish the consultation friend and release whatever hosts it.
#
# Usage: end_friend.sh <session_dir>
#
# claude: stops the background session named in friend_id.
# codex: asks codex_mesh.py to end the resident thread — interrupting a turn in
#   flight — waits for launch_friend.sh to let go, then stops the session's
#   app-server so nothing outlives the consultation.
#
# Safe to run twice; a friend that is already gone is reported, not an error.

set -euo pipefail

SESSION_DIR="${1:?Usage: end_friend.sh <session_dir>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.10.
PY="${SCRIPT_DIR}/../lib/py"
STATUS_FILE="${SESSION_DIR}/status"
AGENT_FILE="${SESSION_DIR}/consult_agent"
MESH="${SCRIPT_DIR}/../agents/codex_mesh.py"

if [[ ! -f "${AGENT_FILE}" ]]; then
  echo "end_friend.sh: no friend was launched in ${SESSION_DIR}"
  exit 0
fi
FAMILY="$(sed -n 's/^family=//p' "${AGENT_FILE}")"
FRIEND="$(cat "${SESSION_DIR}/friend_name")"

case "${FAMILY}" in
  claude)
    FRIEND_ID="$(cat "${SESSION_DIR}/friend_id" 2>/dev/null || true)"
    if [[ -z "${FRIEND_ID}" ]]; then
      echo "end_friend.sh: ${FRIEND} never started"
    else
      # Never the bare name: the interactive shell aliases `claude`; see agent_bg.sh.
      CLAUDE_BIN="${CLAUDE_BIN:-}"
      if [[ -z "${CLAUDE_BIN}" ]]; then
        if [[ -x "${HOME}/.local/bin/claude" ]]; then
          CLAUDE_BIN="${HOME}/.local/bin/claude"
        else
          CLAUDE_BIN="$(command -v claude || true)"
        fi
      fi
      if "${CLAUDE_BIN}" stop "${FRIEND_ID}" >/dev/null 2>&1; then
        echo "stopped ${FRIEND} (${FRIEND_ID})"
      else
        echo "${FRIEND} (${FRIEND_ID}) was already gone"
      fi
    fi
    ;;
  codex)
    "$PY" "${MESH}" end --session-dir "${SESSION_DIR}" --to "${FRIEND}" || true
    # The resident loop notices the marker within about a second; give it a
    # moment to write its last status before the server under it goes away.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if "$PY" "${MESH}" list --session-dir "${SESSION_DIR}" | grep -q "^${FRIEND}	running	"; then
        sleep 0.5
      else
        break
      fi
    done
    "$PY" "${MESH}" stop --session-dir "${SESSION_DIR}" || true
    ;;
  *)
    echo "end_friend.sh: unknown family '${FAMILY}' in ${AGENT_FILE}" >&2
    exit 1
    ;;
esac
echo "ended" > "${STATUS_FILE}"
