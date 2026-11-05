#!/usr/bin/env bash
# Stop the consultation worker through its current staff identity.
# Preserve unrelated sessions and workers on the shared server.
set -euo pipefail

SESSION_DIR="${1:?Usage: end_friend.sh <session_dir>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="${SCRIPT_DIR}/../lib/py"
STATUS_FILE="${SESSION_DIR}/status"
AGENT_FILE="${SESSION_DIR}/consult_agent"
MESH="${SCRIPT_DIR}/../agents/codex_mesh.py"

if [[ ! -f "${AGENT_FILE}" ]]; then
  echo "end_friend.sh: no friend was launched in ${SESSION_DIR}"
  exit 0
fi
FRIEND="$(cat "${SESSION_DIR}/friend_name")"
MEMBER="$("$PY" "$SCRIPT_DIR/../production/worker_fill.py" member \
  --run "$SESSION_DIR" --name "$FRIEND" --allow-absent)"
read -r FAMILY NATIVE_ID REFERENCE <<< "$MEMBER"
case "$FAMILY" in
  gone) ;;
  claude) "$PY" "$SCRIPT_DIR/../shutdown/shutdown.py" stop "$REFERENCE" ;;
  codex)
    "$PY" "${MESH}" end --session-dir "${SESSION_DIR}" --to "${FRIEND}" || true
    # Let the resident launcher record its end before the server stops.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if "$PY" "${MESH}" list --session-dir "${SESSION_DIR}" | grep -q "^${FRIEND}	running	"; then
        sleep 0.5
      else
        break
      fi
    done
    "$PY" "${MESH}" stop --session-dir "${SESSION_DIR}"
    ;;
  *)
    echo "end_friend.sh: unknown family '${FAMILY}' in ${AGENT_FILE}" >&2
    exit 1
    ;;
esac
echo "ended" > "${STATUS_FILE}"
