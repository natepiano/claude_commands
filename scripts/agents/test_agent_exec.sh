#!/usr/bin/env bash

set -euo pipefail
if [[ "${1:-}" != --fixture-environment ]]; then
    exec env -i PATH="$PATH" bash "$0" --fixture-environment
fi
shift
export MODELS_CONFIG_PUBLISH=0

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../lib/test_models_registry.sh"
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
"$SCRIPT_DIR/../lib/py" - "$SCRIPT_DIR/../.." <<'PYTHON'
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from scripts.accounts import accounts
from scripts.whoami.account_test_support import write_codex_auth, write_claude_account
notes = Path.home().parent / "notes"
_ = (Path.home() / ".claude.json").write_text("{}")
write_claude_account("claude2", "launcher@example.invalid", 100,
                     datetime.now(timezone.utc) + timedelta(days=2))
login = "workers@example.invalid"
home = Path.home() / ".codex1"
home.mkdir()
write_codex_auth(home, login)
_ = (notes / "codex 1.md").write_text(f"---\nlogin: {login}\n---\n")
accounts.set_role("workers", "codex1")
PYTHON
export CLAUDE_CONFIG_DIR="$HOME/.claude2"
source "$SCRIPT_DIR/agent_exec.sh"

run_exec_fixture() (
    agents_exec_main "$@"
)

export MODELS_CONFIG_FILE="$TEST_DIR/models.conf"
export CODEX_CONFIG_FILE="$TEST_DIR/codex.toml"
export CODEX_MODELS_CACHE_FILE="$TEST_DIR/models.json"
export CODEX_CATALOG_SYNC_STATE_FILE="$TEST_DIR/last_success"

unset AGENT_EXEC_DRY_RUN AGENT_EXEC_EXTRA_ARGS || true
touch "$CODEX_CATALOG_SYNC_STATE_FILE"

write_test_registry "$MODELS_CONFIG_FILE" \
    assignments/codex_case=codex \
    assignments/direct.simplify=claude \
    direct.claude/simplify=opus:max \
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
    AGENT_EXEC_DRY_RUN=1 run_exec_fixture \
        "$1" "$2" "$WORKING_DIR" "$PROMPT_FILE" "$OUTPUT_FILE" "$LOG_FILE"
}

# Keep command discovery independent of the machine's installed Claude binary.
claude_command="$SCRIPT_DIR/../lib/py $SCRIPT_DIR/../lib/nix_env.py exec $TEST_DIR/bin/claude"

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
    "cd $WORKING_DIR && env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 $claude_command --print --dangerously-skip-permissions --settings \\{\\\"sandbox\\\":\\{\\\"enabled\\\":false\\}\\} --verbose --output-format stream-json --model opus --effort max < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$claude_write"

claude_readonly="$(run_dry claude_case.task readonly)"
assert_equal "claude readonly command is wrong" \
    "cd $WORKING_DIR && env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 $claude_command --print --permission-mode plan --settings \\{\\\"sandbox\\\":\\{\\\"enabled\\\":false\\}\\} --verbose --output-format stream-json --model opus --effort max < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$claude_readonly"

simplify_readonly="$(run_dry direct.simplify readonly)"
"${BASH_SOURCE[0]%/*}/../lib/py" - "$simplify_readonly" <<'PYTHON'
import shlex
import sys
arguments = shlex.split(sys.argv[1])
assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1" in arguments, arguments
assert "--print" in arguments, arguments
assert arguments[arguments.index("--permission-mode") + 1] == "plan", arguments
removed = arguments[arguments.index("--disallowedTools") + 1].split(",")
assert set(removed) == {"Edit", "Write", "NotebookEdit"}, removed
assert "Agent" not in removed, removed
assert "--tools" not in arguments, arguments
assert "--allowedTools" not in arguments, arguments
assert "--dangerously-skip-permissions" not in arguments, arguments
PYTHON
[[ ! -e "$LOG_FILE" ]] || fail "simplify dry run wrote a log"
simplify_session="$(AGENT_EXEC_EXTRA_ARGS='--session-id abcdef12-0000-4000-8000-000000000001' run_dry direct.simplify readonly)"
"${BASH_SOURCE[0]%/*}/../lib/py" - "$simplify_session" <<'PYTHON'
import shlex
import sys
arguments = shlex.split(sys.argv[1])
assert arguments.count("--session-id") == 1, arguments
assert arguments[arguments.index("--session-id") + 1] == "abcdef12-0000-4000-8000-000000000001", arguments
PYTHON
[[ ! -e "${LOG_FILE}.session" ]] || fail "the agent launcher wrote a private session file"
if run_dry direct.simplify write > "$TEST_DIR/simplify-write.stdout"; then
    fail "simplify accepted write mode"
fi

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
    "cd $WORKING_DIR && env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 $claude_command --print --permission-mode plan --settings \\{\\\"sandbox\\\":\\{\\\"enabled\\\":false\\}\\} --verbose --output-format stream-json --model opus --effort max < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$tier_claude_command"

extra_command="$(AGENT_EXEC_EXTRA_ARGS='--add-dir /tmp/extra' run_dry codex_case.task write)"
assert_equal "extra arguments were not appended" \
    "env CODEX_HOME=$HOME/.codex1 codex exec -m gpt-test -c model_reasoning_effort=\\\"high\\\" --add-dir /tmp/extra --ephemeral --sandbox danger-full-access -C $WORKING_DIR -o $OUTPUT_FILE - < $PROMPT_FILE > $LOG_FILE 2>&1" \
    "$extra_command"

MISSING_PROMPT="$TEST_DIR/missing.txt"
MISSING_LOG="$TEST_DIR/missing.log"
if AGENT_EXEC_DRY_RUN=1 run_exec_fixture \
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

CODEX_HOME="$TEST_DIR/caller" run_exec_fixture \
    codex_case.task write "$WORKING_DIR" "$PROMPT_FILE" "$OUTPUT_FILE" "$LOG_FILE"
assert_equal "fallback followed its caller instead of the workers role" "$HOME/.codex1" "$(cat "$TEST_CODEX_HOME_FILE")"

# A named Claude route applies only to worker launches and lasts until it closes.
"$SCRIPT_DIR/../lib/py" - "$SCRIPT_DIR/../.." <<'PYTHON'
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from scripts.accounts import role_map
from scripts.whoami.account_test_support import create_claude_account
create_claude_account("claude4", "route@example.invalid")
role_map.write_role_map(role_map.WorkersClaudeRouteChange(
    role_map.NamedWorkersClaudeAccount("claude4", datetime.now(timezone.utc) + timedelta(days=2)), "queued workers route", datetime.now(timezone.utc).isoformat()))
PYTHON
cat > "$TEST_DIR/bin/claude" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$CLAUDE_CONFIG_DIR" > "$TEST_CLAUDE_HOME_FILE"
cat > "$TEST_CLAUDE_PROMPT_FILE"
if [[ -n "${TEST_CLAUDE_RESULT:-}" ]]; then
    printf '%s\n' "$TEST_CLAUDE_RESULT"
else
    printf '%s\n' '{"type":"result","result":"done"}'
fi
printf '%s\n' "$CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" > "$TEST_CLAUDE_BACKGROUND_FILE"
exit "${TEST_CLAUDE_EXIT_CODE:-0}"
EOF
chmod +x "$TEST_DIR/bin/claude"
export TEST_CLAUDE_HOME_FILE="$TEST_DIR/claude-home"
export TEST_CLAUDE_PROMPT_FILE="$TEST_DIR/claude-prompt"
export TEST_CLAUDE_BACKGROUND_FILE="$TEST_DIR/background-disabled"
run_exec_fixture claude_case.task write "$WORKING_DIR" "$PROMPT_FILE" "$OUTPUT_FILE" "$LOG_FILE"
assert_equal "workers ignored the named Claude account" "$HOME/.claude4" "$(cat "$TEST_CLAUDE_HOME_FILE")"
assert_equal "Claude worker lost its prompt" "test prompt" "$(cat "$TEST_CLAUDE_PROMPT_FILE")"

test_extraction_failure_preserves_the_task_exit_policy() {
    local task claude_exit expected actual
    while read -r task claude_exit expected; do
        actual=0
        TEST_CLAUDE_EXIT_CODE="$claude_exit" run_exec_fixture \
            "$task" readonly "$WORKING_DIR" "$PROMPT_FILE" "$WORKING_DIR" "$LOG_FILE" \
            2> "$TEST_DIR/extraction.stderr" || actual=$?
        [[ "$(cat "$TEST_DIR/extraction.stderr")" == *IsADirectoryError* ]] \
            || fail "the launcher did not reach the output write failure"
        assert_equal "$task changed exit $claude_exit after extraction failed" "$expected" "$actual"
    done <<'CASES'
claude_case.task 0 0
claude_case.task 7 7
direct.simplify 0 0
direct.simplify 7 7
CASES
}

test_extraction_failure_preserves_the_task_exit_policy

test_every_claude_print_launch_keeps_agents_and_reports_errors() {
    local task event expected actual
    for task in claude_case.task direct.simplify; do
        while IFS=$'\t' read -r event expected; do
            actual=0
            TEST_CLAUDE_RESULT="$event" run_exec_fixture \
                "$task" readonly "$WORKING_DIR" "$PROMPT_FILE" "$OUTPUT_FILE" "$LOG_FILE" \
                2> "$TEST_DIR/print-error.stderr" || actual=$?
            assert_equal "$task accepted a Claude error" 1 "$actual"
            assert_equal "$task did not retain foreground agents" 1 "$(cat "$TEST_CLAUDE_BACKGROUND_FILE")"
            assert_equal "$task lost the execution reason" "$expected" "$(cat "$OUTPUT_FILE")"
            [[ "$(cat "$TEST_DIR/print-error.stderr")" != *"$expected"* ]] \
                || fail "$task printed the output error on stderr"
        done <<'CASES'
{"type":"result","is_error":true,"result":"weekly limit"}	ERROR: weekly limit
{"type":"result","errors":["credentials expired"],"result":"Pass"}	ERROR: credentials expired
CASES
    done
}

test_every_claude_print_launch_keeps_agents_and_reports_errors

export CLAUDE_CONFIG_DIR="$HOME/.claude2"
run_exec_fixture direct.simplify readonly "$WORKING_DIR" "$PROMPT_FILE" "$OUTPUT_FILE" "$LOG_FILE"
assert_equal "simplify replaced the caller account with the workers account" "$HOME/.claude2" "$(cat "$TEST_CLAUDE_HOME_FILE")"

# The worker's registry-selected Claude path uses the same account.
export STAFF_FILE="$TEST_DIR/staff.json"
export FAKE_CLAUDE_PID=$$
export FIXTURE_CHECKOUT="$SCRIPT_DIR/../.."
"$SCRIPT_DIR/../lib/py" - "$TEST_DIR" <<'PYTHON'
import os, shlex, sys
from pathlib import Path
from scripts.production.staff_test_support import source_environment, unit_membership
root = Path(sys.argv[1])
environment = source_environment(root / "sources")
os.environ.update(environment)
unit = unit_membership("agent-exec", "fixture-unit")
environment["CODEX_THREAD_ID"] = str(unit.id)
_ = (root / "staff-environment").write_text("".join(
    f"export {name}={shlex.quote(value)}\n" for name, value in environment.items()))
PYTHON
source "$TEST_DIR/staff-environment"
cat > "$TEST_DIR/send.py" <<'PYTHON'
import os
from scripts.production import staff
assert staff.by_id(staff.StaffId(os.environ["FAKE_CLAUDE_SESSION_ID"])) is not None
print("SENT: fixture")
PYTHON
export CLAUDE_WORKER_SEND_SCRIPT="$TEST_DIR/send.py"
run_background_fixture() {
    "$SCRIPT_DIR/../lib/py" - "$@" <<'PYTHON'
import os, sys
from pathlib import Path
from scripts.direct import claude_worker
name, run, cwd, prompt, summary, log, workers = sys.argv[1:]
turn = claude_worker.ClaudeTurn(name, Path(run), Path(cwd), Path(prompt), Path(summary), Path(log),
    "", "", "", "", workers == "1", dict(os.environ))
result = claude_worker.open_worker(turn)
raise SystemExit(result.code if isinstance(result, claude_worker.NotDelivered) else 0)
PYTHON
}
recorded_worker_session() {
    "$SCRIPT_DIR/../lib/py" - "$WORKING_DIR" "$1" <<'PYTHON'
import sys
from pathlib import Path
from scripts.production import worker_admission
worker = worker_admission.member(Path(sys.argv[1]), sys.argv[2])
print(worker.current_session if worker is not None else "")
PYTHON
}
cat > "$TEST_DIR/bin/claude-bg" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$CLAUDE_CONFIG_DIR" > "$TEST_CLAUDE_HOME_FILE"
"$FIXTURE_CHECKOUT/scripts/lib/py" - <<'PYTHON'
import os, sys
from scripts.production.staff_test_support import record_fixture_claude_start
record_fixture_claude_start(sys.argv[1:])
PYTHON
printf '%s\n' 'backgrounded · abcdef12 · fixture-worker'
EOF
chmod +x "$TEST_DIR/bin/claude-bg"
export FAKE_CLAUDE_SESSION_ID=abcdef12-0000-4000-8000-000000000001
CLAUDE_BIN="$TEST_DIR/bin/claude-bg" run_background_fixture \
    fixture-worker "$WORKING_DIR" "$WORKING_DIR" "$PROMPT_FILE" "$TEST_DIR/summary" "$LOG_FILE" 1
assert_equal "background worker ignored the named Claude account" "$HOME/.claude4" "$(cat "$TEST_CLAUDE_HOME_FILE")"
assert_equal "background worker did not launch" "$FAKE_CLAUDE_SESSION_ID" "$(recorded_worker_session fixture-worker)"

# A caller's helper does not opt in to the workers route.
export FAKE_CLAUDE_SESSION_ID=abcdef12-0000-4000-8000-000000000002
CLAUDE_BIN="$TEST_DIR/bin/claude-bg" run_background_fixture \
    fixture-friend "$WORKING_DIR" "$WORKING_DIR" "$PROMPT_FILE" "$TEST_DIR/friend-summary" "$LOG_FILE" 0
assert_equal "friend took the workers account" "$HOME/.claude2" "$(cat "$TEST_CLAUDE_HOME_FILE")"
assert_equal "friend did not launch" "$FAKE_CLAUDE_SESSION_ID" "$(recorded_worker_session fixture-friend)"

# CLI assignments also come from the registry.
write_test_registry "$MODELS_CONFIG_FILE" --update cli.claude/commit_prep=opus:max
cp "$MODELS_CONFIG_FILE" "$TEST_DIR/cli.conf"
write_test_registry "$TEST_DIR/cli.conf" --update assignments/cli.commit_prep=claude
MODELS_CONFIG_FILE="$TEST_DIR/cli.conf" bash "$SCRIPT_DIR/../cli_agent/cli_agent.sh" commit_prep < /dev/null > "$TEST_DIR/cli-output"
assert_equal "CLI worker ignored the named Claude account" "$HOME/.claude4" "$(cat "$TEST_CLAUDE_HOME_FILE")"
"$SCRIPT_DIR/../lib/py" - "$SCRIPT_DIR/../.." <<'PYTHON'
import sys
from pathlib import Path
from scripts.accounts import role_map
from datetime import datetime, timezone
role_map.write_role_map(role_map.WorkersClaudeRouteChange(
    role_map.DEFAULT_WORKERS_CLAUDE_ACCOUNT, "Codex recovered", datetime.now(timezone.utc).isoformat()))
PYTHON
run_exec_fixture claude_case.task write "$WORKING_DIR" "$PROMPT_FILE" "$OUTPUT_FILE" "$LOG_FILE"
assert_equal "a closed route still replaced the caller account" "$HOME/.claude2" "$(cat "$TEST_CLAUDE_HOME_FILE")"

echo "agent_exec tests passed"
