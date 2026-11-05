#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "${TEST_DIR}/scripts/ask_a_friend" "${TEST_DIR}/scripts/agents" \
  "${TEST_DIR}/scripts/direct" "${TEST_DIR}/session"

# Check the resident arguments for both families without starting a worker.
cp "${REPO}/scripts/ask_a_friend/launch_friend.sh" "${TEST_DIR}/scripts/ask_a_friend/"
cat > "${TEST_DIR}/scripts/agents/models_config.sh" <<'SH'
agents_resolve() {
  AGENT_FAMILY="$FRIEND_TEST_FAMILY" AGENT_MODEL=fixture-model AGENT_EFFORT=high
}
SH
cat > "${TEST_DIR}/scripts/direct/assign.sh" <<'SH'
printf '%s\n' "$@" > "${FRIEND_TEST_SESSION}/arguments"
printf 'fixture-id\n' > "${FRIEND_TEST_SESSION}/friend_id"
exit "${FRIEND_TEST_EXIT:-0}"
SH
export FRIEND_TEST_SESSION="${TEST_DIR}/session"
printf 'fixture-friend\n' > "${FRIEND_TEST_SESSION}/friend_name"
printf 'Check the change.\n' > "${FRIEND_TEST_SESSION}/question.md"
for FRIEND_TEST_FAMILY in claude codex; do
  export FRIEND_TEST_FAMILY
  bash "${TEST_DIR}/scripts/ask_a_friend/launch_friend.sh" "$FRIEND_TEST_SESSION" "$TEST_DIR"
  "${BASH_SOURCE[0]%/*}/../lib/py" - "$FRIEND_TEST_SESSION" "$TEST_DIR" "$FRIEND_TEST_FAMILY" <<'PYTEST'
import pathlib
import sys

session = pathlib.Path(sys.argv[1])
family = sys.argv[3]
arguments = (session / 'arguments').read_text().splitlines()
assert arguments == [
    '--role', 'review', '--operation', 'resident', '--session-dir', str(session),
    '--new-worker', 'fixture-friend', '--cwd', sys.argv[2],
    '--prompt', str(session / 'question.md'), '--summary', str(session / 'answer.txt'),
    '--log', str(session / 'agent.log'), '--id-file', str(session / 'friend_id'),
    '--family', family, '--model', 'fixture-model', '--effort', 'high',
], arguments
assert (session / 'status').read_text().strip() == ('running' if family == 'claude' else 'ended')
assert (session / 'consult_agent').read_text() == (
    f'task=ask_a_friend.consultation\nfamily={family}\nagent=fixture-model\neffort=high\n'
)
PYTEST
  if FRIEND_TEST_EXIT=7 bash "${TEST_DIR}/scripts/ask_a_friend/launch_friend.sh" "$FRIEND_TEST_SESSION" "$TEST_DIR"; then
    echo 'The launcher accepted a failed assignment.' >&2
    exit 1
  else
    test "$?" -eq 7
    test "$(cat "${FRIEND_TEST_SESSION}/status")" = error
  fi
done
echo 'launcher task tests passed'
