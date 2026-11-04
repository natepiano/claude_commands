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
FAMILY="$("$PY" - "$SCRIPT_DIR/../.." "$SESSION_DIR" "$FRIEND" <<'PYMEMBER'
import sys
from pathlib import Path
from contextlib import redirect_stdout
sys.path.insert(0, sys.argv[1])
from scripts.production import staff, worker_admission
from scripts.shutdown import stop_resume
row = worker_admission.member(Path(sys.argv[2]), sys.argv[3])
if row is None:
    print("gone")
elif isinstance(staff.local_snapshot().backends.get(row.id), staff.CodexBackend):
    print("codex")
else:
    with redirect_stdout(sys.stderr):
        result = stop_resume.stop(row.id)
    if isinstance(result, stop_resume.StopFailed):
        raise SystemExit(result.reason)
    print("claude")
PYMEMBER
)"
case "$FAMILY" in
  claude|gone) ;;
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
    "$PY" "${MESH}" stop --session-dir "${SESSION_DIR}" || true
    ;;
  *)
    echo "end_friend.sh: unknown family '${FAMILY}' in ${AGENT_FILE}" >&2
    exit 1
    ;;
esac
echo "ended" > "${STATUS_FILE}"
