#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-exec.XXXXXX")"
trap 'rm -rf "$TEST_DIR"' EXIT

export AGENTS_CONFIG_FILE="$TEST_DIR/agents.conf"
export CODEX_CONFIG_FILE="$TEST_DIR/codex.toml"
export CODEX_MODELS_CACHE_FILE="$TEST_DIR/models.json"
export CODEX_CATALOG_SYNC_STATE_FILE="$TEST_DIR/last_success"

unset AGENT_EXEC_DRY_RUN AGENT_EXEC_EXTRA_ARGS || true
touch "$CODEX_CATALOG_SYNC_STATE_FILE"

cat > "$AGENTS_CONFIG_FILE" <<'EOF'
[assignments]
codex_case=codex
claude_case=claude
bare_case=codex

[codex_case.codex]
task=gpt-test:high

[claude_case.claude]
task=opus:max

[bare_case.codex]
task=gpt-bare

[codex.agents]
gpt-test=low,medium,high
gpt-bare=

[claude.agents]
opus=low,medium,high,max
EOF

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

codex_write="$(run_dry codex_case.task write)"
assert_equal "codex write command is wrong" \
    "codex exec -m gpt-test -c model_reasoning_effort=\\\"high\\\" --ephemeral --full-auto -C $WORKING_DIR -o $OUTPUT_FILE test\\ prompt > $LOG_FILE 2>&1" \
    "$codex_write"

codex_readonly="$(run_dry codex_case.task readonly)"
assert_equal "codex readonly command is wrong" \
    "codex exec -m gpt-test -c model_reasoning_effort=\\\"high\\\" --ephemeral --sandbox read-only -C $WORKING_DIR -o $OUTPUT_FILE test\\ prompt > $LOG_FILE 2>&1" \
    "$codex_readonly"

claude_write="$(run_dry claude_case.task write)"
assert_equal "claude write command is wrong" \
    "cd $WORKING_DIR && claude --print --dangerously-skip-permissions --settings \\{\\\"sandbox\\\":\\{\\\"enabled\\\":false\\}\\} --verbose --output-format stream-json --model opus --effort max -- test\\ prompt > $LOG_FILE 2>&1" \
    "$claude_write"

claude_readonly="$(run_dry claude_case.task readonly)"
assert_equal "claude readonly command is wrong" \
    "cd $WORKING_DIR && claude --print --permission-mode plan --settings \\{\\\"sandbox\\\":\\{\\\"enabled\\\":false\\}\\} --verbose --output-format stream-json --model opus --effort max -- test\\ prompt > $LOG_FILE 2>&1" \
    "$claude_readonly"

bare_command="$(run_dry bare_case.task write)"
assert_equal "bare pair did not omit the effort flag" \
    "codex exec -m gpt-bare --ephemeral --full-auto -C $WORKING_DIR -o $OUTPUT_FILE test\\ prompt > $LOG_FILE 2>&1" \
    "$bare_command"

extra_command="$(AGENT_EXEC_EXTRA_ARGS='--add-dir /tmp/extra' run_dry codex_case.task write)"
assert_equal "extra arguments were not appended" \
    "codex exec -m gpt-test -c model_reasoning_effort=\\\"high\\\" --add-dir /tmp/extra --ephemeral --full-auto -C $WORKING_DIR -o $OUTPUT_FILE test\\ prompt > $LOG_FILE 2>&1" \
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

echo "agent_exec tests passed"
