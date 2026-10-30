#!/usr/bin/env bash

set -euo pipefail
export AGENTS_CONFIG_PUBLISH=0

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../lib/test_agents_registry.sh"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-exec.XXXXXX")"
trap 'rm -rf "$TEST_DIR"' EXIT

export HOME="$TEST_DIR/home"
export PYTHONDONTWRITEBYTECODE=1
export XDG_CACHE_HOME="$TEST_DIR/cache"
export XDG_STATE_HOME="$TEST_DIR/state"
mkdir -p "$HOME/.claude/scripts/lib"
ln -s "$SCRIPT_DIR/../lib/py" "$HOME/.claude/scripts/lib/py"
ln -s "$SCRIPT_DIR/../whoami" "$HOME/.claude/scripts/whoami"
ln -s "$SCRIPT_DIR/../accounts" "$HOME/.claude/scripts/accounts"
unset CODEX_HOME
export CLAUDE_BIN="$TEST_DIR/bin/claude"
mkdir -p "$TEST_DIR/bin"
printf '#!/usr/bin/env bash\nexit 1\n' > "$CLAUDE_BIN"
chmod +x "$CLAUDE_BIN"
export PATH="$TEST_DIR/bin:$PATH"
export AGENT_NOTES_DIR="$TEST_DIR/notes"
mkdir -p "$AGENT_NOTES_DIR"
python3 - "$SCRIPT_DIR/../.." <<'PYTHON'
import base64, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from scripts.accounts import accounts
notes = Path.home().parent / "notes"
_ = (Path.home() / ".claude.json").write_text("{}")
_ = (notes / "claude2.md").write_text("---\nlogin: launcher@example.invalid\n---\n")
assert accounts.setup("claude2") == []
_ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "launcher@example.invalid"}}))
login = "workers@example.invalid"
home = Path.home() / ".codex1"
home.mkdir()
claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
_ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
_ = (notes / "codex 1.md").write_text(f"---\nlogin: {login}\n---\n")
accounts.set_role("workers", "codex1")
PYTHON
export CLAUDE_CONFIG_DIR="$HOME/.claude2"

export AGENTS_CONFIG_FILE="$TEST_DIR/agents.conf"
export CODEX_CONFIG_FILE="$TEST_DIR/codex.toml"
export CODEX_MODELS_CACHE_FILE="$TEST_DIR/models.json"
export CODEX_CATALOG_SYNC_STATE_FILE="$TEST_DIR/last_success"

unset AGENT_EXEC_DRY_RUN AGENT_EXEC_EXTRA_ARGS || true
touch "$CODEX_CATALOG_SYNC_STATE_FILE"

write_test_registry "$AGENTS_CONFIG_FILE" \
    assignments/codex_case=codex \
    assignments/claude_case=claude \
    assignments/bare_case=codex \
    assignments/tier_case=codex \
    assignments/tier_case.claude_task=claude \
    codex_case.codex/task=gpt-test:high \
    claude_case.claude/task=opus:max \
    bare_case.codex/task=gpt-bare \
    tier_case.codex/task=gpt-test:high \
    tier_case.claude/claude_task=opus:max \
    tier_case.options/codex_service_tier=fast \
    codex.agents/gpt-test=low,medium,high \
    codex.agents/gpt-bare= \
    claude.agents/opus=low,medium,high,max

PROMPT_FILE="$TEST_DIR/prompt.txt"
OUTPUT_FILE="$TEST_DIR/output.txt"
LOG_FILE="$TEST_DIR/agent.log"
WORKING_DIR="$TEST_DIR/work"
mkdir "$WORKING_DIR"
printf 'test prompt\n' > "$PROMPT_FILE"

fail() {
    echo "$1" >&2
    exit 1
}

assert_equal() {
    local description="$1" expected="$2" actual="$3"
    if [[ "$actual" != "$expected" ]]; then
        echo "$description" >&2
        printf 'expected: %s\n' "$expected" >&2
        printf 'actual:   %s\n' "$actual" >&2
        exit 1
    fi
}

run_dry() {
    AGENT_EXEC_DRY_RUN=1 bash "$SCRIPT_DIR/agent_exec.sh" \
        "$1" "$2" "$WORKING_DIR" "$PROMPT_FILE" "$OUTPUT_FILE" "$LOG_FILE"
}

# Keep command discovery independent of the machine's installed Claude binary.
claude_command="$HOME/.claude/scripts/lib/py $SCRIPT_DIR/../lib/nix_env.py exec $TEST_DIR/bin/claude"

codex_write="$(run_dry codex_case.task write)"
assert_equal "codex write command is wrong" \
    "env CODEX_HOME=$HOME/.codex1 codex exec -m gpt-test -c model_reasoning_effort=\\\"high\\\" --ephemeral --sandbox danger-full-access -C $WORKING_DIR -o $OUTPUT_FILE - < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$codex_write"

codex_readonly="$(run_dry codex_case.task readonly)"
assert_equal "codex readonly command is wrong" \
    "env CODEX_HOME=$HOME/.codex1 codex exec -m gpt-test -c model_reasoning_effort=\\\"high\\\" --ephemeral --sandbox read-only -C $WORKING_DIR -o $OUTPUT_FILE - < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$codex_readonly"

claude_write="$(run_dry claude_case.task write)"
assert_equal "claude write command is wrong" \
    "cd $WORKING_DIR && $claude_command --print --dangerously-skip-permissions --settings \\{\\\"sandbox\\\":\\{\\\"enabled\\\":false\\}\\} --verbose --output-format stream-json --model opus --effort max < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$claude_write"

claude_readonly="$(run_dry claude_case.task readonly)"
assert_equal "claude readonly command is wrong" \
    "cd $WORKING_DIR && $claude_command --print --permission-mode plan --settings \\{\\\"sandbox\\\":\\{\\\"enabled\\\":false\\}\\} --verbose --output-format stream-json --model opus --effort max < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$claude_readonly"

bare_command="$(run_dry bare_case.task write)"
assert_equal "bare pair did not omit the effort flag" \
    "env CODEX_HOME=$HOME/.codex1 codex exec -m gpt-bare --ephemeral --sandbox danger-full-access -C $WORKING_DIR -o $OUTPUT_FILE - < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$bare_command"

tier_command="$(run_dry tier_case.task readonly)"
assert_equal "codex command did not carry the function's service tier" \
    "env CODEX_HOME=$HOME/.codex1 codex exec -m gpt-test -c model_reasoning_effort=\\\"high\\\" -c service_tier=\\\"fast\\\" --ephemeral --sandbox read-only -C $WORKING_DIR -o $OUTPUT_FILE - < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$tier_command"

tier_claude_command="$(run_dry tier_case.claude_task readonly)"
assert_equal "claude command carried a codex service tier" \
    "cd $WORKING_DIR && $claude_command --print --permission-mode plan --settings \\{\\\"sandbox\\\":\\{\\\"enabled\\\":false\\}\\} --verbose --output-format stream-json --model opus --effort max < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$tier_claude_command"

extra_command="$(AGENT_EXEC_EXTRA_ARGS='--add-dir /tmp/extra' run_dry codex_case.task write)"
assert_equal "extra arguments were not appended" \
    "env CODEX_HOME=$HOME/.codex1 codex exec -m gpt-test -c model_reasoning_effort=\\\"high\\\" --add-dir /tmp/extra --ephemeral --sandbox danger-full-access -C $WORKING_DIR -o $OUTPUT_FILE - < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$extra_command"

MISSING_PROMPT="$TEST_DIR/missing.txt"
MISSING_LOG="$TEST_DIR/missing.log"
if AGENT_EXEC_DRY_RUN=1 bash "$SCRIPT_DIR/agent_exec.sh" \
    codex_case.task write "$WORKING_DIR" "$MISSING_PROMPT" "$OUTPUT_FILE" "$MISSING_LOG" \
    > "$TEST_DIR/missing.stdout" 2> "$TEST_DIR/missing.stderr"; then
    fail "missing prompt unexpectedly succeeded"
fi
assert_equal "missing prompt error was not written to the log" \
    "Prompt not found: $MISSING_PROMPT" \
    "$(cat "$MISSING_LOG")"

mkdir -p "$TEST_DIR/bin" "$HOME/.codex1" "$TEST_DIR/notes"
cat > "$TEST_DIR/bin/codex" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$CODEX_HOME" > "$TEST_CODEX_HOME_FILE"
cat > "$TEST_CODEX_PROMPT_FILE"
EOF
chmod +x "$TEST_DIR/bin/codex"
export TEST_CODEX_HOME_FILE="$TEST_DIR/child-home"
export TEST_CODEX_PROMPT_FILE="$TEST_DIR/child-prompt"
export PATH="$TEST_DIR/bin:$PATH"
export AGENT_NOTES_DIR="$TEST_DIR/notes"

CODEX_HOME="$TEST_DIR/caller" bash "$SCRIPT_DIR/agent_exec.sh" \
    codex_case.task write "$WORKING_DIR" "$PROMPT_FILE" "$OUTPUT_FILE" "$LOG_FILE"
assert_equal "fallback followed its caller instead of the workers role" "$HOME/.codex1" "$(cat "$TEST_CODEX_HOME_FILE")"

# A named Claude route applies only to worker launches and lasts until it closes.
python3 - "$SCRIPT_DIR/../.." <<'PYTHON'
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from scripts.accounts import accounts, role_map
import json
notes = Path.home().parent / "notes"
_ = (notes / "claude4.md").write_text("---\nlogin: route@example.invalid\n---\n")
assert accounts.setup("claude4") == []
_ = (accounts.account_dir("claude4") / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "route@example.invalid"}}))
role_map.write_role_map(role_map.WorkersClaudeRouteChange(
    role_map.NamedWorkersClaudeAccount("claude4", datetime.now(timezone.utc) + timedelta(days=2)), "queued workers route", datetime.now(timezone.utc).isoformat()))
PYTHON
cat > "$TEST_DIR/bin/claude" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$CLAUDE_CONFIG_DIR" > "$TEST_CLAUDE_HOME_FILE"
cat > "$TEST_CLAUDE_PROMPT_FILE"
printf '%s\n' '{"type":"result","result":"done"}'
EOF
chmod +x "$TEST_DIR/bin/claude"
export TEST_CLAUDE_HOME_FILE="$TEST_DIR/claude-home"
export TEST_CLAUDE_PROMPT_FILE="$TEST_DIR/claude-prompt"
bash "$SCRIPT_DIR/agent_exec.sh" claude_case.task write "$WORKING_DIR" "$PROMPT_FILE" "$OUTPUT_FILE" "$LOG_FILE"
assert_equal "workers ignored the named Claude account" "$HOME/.claude4" "$(cat "$TEST_CLAUDE_HOME_FILE")"
assert_equal "Claude worker lost its prompt" "test prompt" "$(cat "$TEST_CLAUDE_PROMPT_FILE")"
# The worker's registry-selected Claude path uses the same account.
cat > "$TEST_DIR/bin/claude-bg" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$CLAUDE_CONFIG_DIR" > "$TEST_CLAUDE_HOME_FILE"
printf '%s\n' "${AGENT_BG_WORKERS+set}" > "$TEST_CLAUDE_WORKERS_FLAG_FILE"
printf '%s\n' 'backgrounded · abcdef12 · fixture-worker'
EOF
chmod +x "$TEST_DIR/bin/claude-bg"
export TEST_CLAUDE_WORKERS_FLAG_FILE="$TEST_DIR/claude-workers-flag"
CLAUDE_BIN="$TEST_DIR/bin/claude-bg" AGENT_BG_WORKERS=1 AGENT_BG_DETACH=1 bash "$SCRIPT_DIR/agent_bg.sh" \
    fixture-worker "$WORKING_DIR" "$PROMPT_FILE" "$TEST_DIR/summary" "$LOG_FILE" "$TEST_DIR/bg-id"
assert_equal "background worker ignored the named Claude account" "$HOME/.claude4" "$(cat "$TEST_CLAUDE_HOME_FILE")"
assert_equal "background worker did not launch" "abcdef12" "$(cat "$TEST_DIR/bg-id")"
assert_equal "background worker passed its route opt-in to its children" "" "$(cat "$TEST_CLAUDE_WORKERS_FLAG_FILE")"

# A caller's helper does not opt in to the workers route.
CLAUDE_BIN="$TEST_DIR/bin/claude-bg" AGENT_BG_DETACH=1 env -u AGENT_BG_WORKERS bash "$SCRIPT_DIR/agent_bg.sh" \
    fixture-friend "$WORKING_DIR" "$PROMPT_FILE" "$TEST_DIR/friend-summary" "$LOG_FILE" "$TEST_DIR/friend-id"
assert_equal "friend took the workers account" "$HOME/.claude2" "$(cat "$TEST_CLAUDE_HOME_FILE")"
assert_equal "friend did not launch" "abcdef12" "$(cat "$TEST_DIR/friend-id")"

# CLI assignments also come from the registry.
write_test_registry "$AGENTS_CONFIG_FILE" --update cli.claude/commit_prep=opus:max
cp "$AGENTS_CONFIG_FILE" "$TEST_DIR/cli.conf"
write_test_registry "$TEST_DIR/cli.conf" --update assignments/cli.commit_prep=claude
AGENTS_CONFIG_FILE="$TEST_DIR/cli.conf" bash "$SCRIPT_DIR/../cli_agent/cli_agent.sh" commit_prep < /dev/null > "$TEST_DIR/cli-output"
assert_equal "CLI worker ignored the named Claude account" "$HOME/.claude4" "$(cat "$TEST_CLAUDE_HOME_FILE")"
python3 - "$SCRIPT_DIR/../.." <<'PYTHON'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from scripts.accounts import role_map
from datetime import datetime, timezone
role_map.write_role_map(role_map.WorkersClaudeRouteChange(
    role_map.DEFAULT_WORKERS_CLAUDE_ACCOUNT, "Codex recovered", datetime.now(timezone.utc).isoformat()))
PYTHON
bash "$SCRIPT_DIR/agent_exec.sh" claude_case.task write "$WORKING_DIR" "$PROMPT_FILE" "$OUTPUT_FILE" "$LOG_FILE"
assert_equal "a closed route still replaced the caller account" "$HOME/.claude2" "$(cat "$TEST_CLAUDE_HOME_FILE")"

echo "agent_exec tests passed"
