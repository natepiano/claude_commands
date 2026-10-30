#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../lib/test_agents_registry.sh"
REPO="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
export HOME="${TEST_DIR}/home"
export AGENTS_CONFIG_FILE="${TEST_DIR}/agents.conf"
export CODEX_CONFIG_FILE="${TEST_DIR}/codex.toml"
unset CLAUDE_CONFIG_DIR CLAUDE_CODE_OAUTH_TOKEN CODEX_HOME
export PYTHONDONTWRITEBYTECODE=1 HERDR_BIN=/nonexistent/herdr
mkdir -p "$HOME" "${TEST_DIR}/scripts/ask_a_friend" "${TEST_DIR}/scripts/agents" \
  "${TEST_DIR}/scripts/lib" "${TEST_DIR}/session"

write_test_registry "$AGENTS_CONFIG_FILE"

# Run the real direct launchers with their fake mesh. Check start and follow.
cd "$REPO"
python3 -m unittest \
  scripts.direct.test_implement_launcher.ImplementLauncherWorkerTests.test_start_and_follow_write_the_current_kind_as_the_roster_role \
  scripts.direct.test_review_launcher.ReviewLauncherPassTests.test_a_named_open_worker_receives_the_review_and_writes_findings \
  scripts.direct.test_review_launcher.ReviewLauncherPassTests.test_a_review_opened_worker_has_durable_identity_for_an_implementation_claim

# Run the real friend launcher with a fake resolver and mesh.
cp "${REPO}/scripts/ask_a_friend/launch_friend.sh" "${TEST_DIR}/scripts/ask_a_friend/"
cp "${REPO}/scripts/lib/py" "${TEST_DIR}/scripts/lib/"
cat > "${TEST_DIR}/scripts/agents/agents_config.sh" <<'SH'
agents_resolve() {
  AGENT_FAMILY=codex AGENT_MODEL=gpt-fixture AGENT_EFFORT=high AGENT_SERVICE_TIER=default
}
SH
cat > "${TEST_DIR}/scripts/agents/codex_mesh.py" <<'PY'
import json
import pathlib
import sys

arguments = sys.argv[1:]
session = pathlib.Path(arguments[arguments.index('--session-dir') + 1])
(session / 'arguments.json').write_text(json.dumps(arguments))
PY
printf 'fixture-friend\n' > "${TEST_DIR}/session/friend_name"
printf 'Check the change.\n' > "${TEST_DIR}/session/question.md"
bash "${TEST_DIR}/scripts/ask_a_friend/launch_friend.sh" "${TEST_DIR}/session" "$TEST_DIR"
python3 - "${TEST_DIR}/session" <<'PY'
import json
import pathlib
import sys

session = pathlib.Path(sys.argv[1])
arguments = json.loads((session / 'arguments.json').read_text())
assert arguments[0] == 'start', arguments
assert arguments[arguments.index('--task') + 1] == 'ask_a_friend.consultation', arguments
assert (session / 'status').read_text().strip() == 'ended'
PY
echo 'launcher task tests passed'
