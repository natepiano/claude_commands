#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agents-config.XXXXXX")"
trap 'rm -rf "$TEST_DIR"' EXIT

AGENTS_CONFIG_FILE="$TEST_DIR/agents.conf"
CODEX_CONFIG_FILE="$TEST_DIR/codex.toml"
CODEX_MODELS_CACHE_FILE="$TEST_DIR/models.json"
CODEX_CATALOG_SYNC_STATE_FILE="$TEST_DIR/last_success"

touch "$CODEX_CATALOG_SYNC_STATE_FILE"

write_fixture() {
    cp "$1" "$AGENTS_CONFIG_FILE"
}

cat > "$TEST_DIR/base.conf" <<'EOF'
[assignments]
alpha=codex
alpha.claude_task=claude
bare=codex
bare_catalog=codex
bare_catalog_effort=codex
missing_set=codex
bad_agent=codex
bad_effort=codex
empty_effort=codex
switchable=codex
assignable=codex
literal=codex
literalxwork=claude

[alpha.codex]
work=gpt-test:high

[alpha.claude]
claude_task=opus:max

[bare.codex]
work=gpt-test

[bare_catalog.codex]
work=gpt-bare

[bare_catalog_effort.codex]
work=gpt-bare:high

[bad_agent.codex]
work=gpt-missing:high

[bad_effort.codex]
work=gpt-test:xhigh

[empty_effort.codex]
work=gpt-test:

[switchable.codex]
work=gpt-test:high

[switchable.claude]
work=opus:max
broken=missing:max

[assignable.codex]
work=gpt-test:high

[assignable.claude]
work=opus:max

[literal.codex]
work=gpt-test:high

[literal.claude]
work=opus:max

[codex.agents]
gpt-test=low,medium,high
gpt-bare=

[claude.agents]
opus=low,medium,high,max
EOF

cat > "$TEST_DIR/list.conf" <<'EOF'
[assignments]
override=codex
override.work=claude

[override.codex]
work=gpt-test:high

[override.claude]
work=opus:max

[codex.agents]
gpt-test=low,medium,high

[claude.agents]
opus=low,medium,high,max
EOF

write_fixture "$TEST_DIR/base.conf"
source "$SCRIPT_DIR/agents_config.sh"

fail() {
    echo "$1" >&2
    exit 1
}

assert_fails() {
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        fail "$description unexpectedly succeeded"
    fi
}

agents_resolve alpha.work
[[ "$AGENT_FAMILY" == "codex" ]] || fail "codex family did not resolve"
[[ "$AGENT_MODEL" == "gpt-test" ]] || fail "codex agent did not resolve"
[[ "$AGENT_EFFORT" == "high" ]] || fail "codex effort did not resolve"

agents_resolve alpha.claude_task
[[ "$AGENT_FAMILY" == "claude" ]] || fail "exact-task override did not beat function assignment"
[[ "$AGENT_MODEL" == "opus" && "$AGENT_EFFORT" == "max" ]] || fail "claude pair did not resolve"

assert_fails "missing assignment" agents_resolve absent.work
assert_fails "missing set section" agents_resolve missing_set.work
assert_fails "missing sub-task row" agents_resolve alpha.absent
assert_fails "unknown agent" agents_resolve bad_agent.work
assert_fails "invalid effort" agents_resolve bad_effort.work
assert_fails "empty effort" agents_resolve empty_effort.work

agents_resolve literal.work
[[ "$AGENT_FAMILY" == "codex" ]] || fail "regex-like exact-task key matched literal.work"
[[ -z "$(_agents_registry_get assignments literal.work)" ]] || fail "literal lookup matched literalxwork"
[[ "$(_agents_registry_get assignments literalxwork)" == "claude" ]] || fail "literal lookup missed literalxwork"

agents_resolve bare.work
[[ "$AGENT_MODEL" == "gpt-test" && -z "$AGENT_EFFORT" ]] || fail "bare agent did not produce empty effort"

agents_resolve bare_catalog.work
[[ "$AGENT_MODEL" == "gpt-bare" && -z "$AGENT_EFFORT" ]] || fail "empty-effort catalog agent did not resolve bare"
assert_fails "effort on empty-effort catalog agent" agents_resolve bare_catalog_effort.work
stderr_out="$(agents_resolve bare_catalog_effort.work 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"effort 'high' is not allowed"* ]] || fail "empty-effort catalog agent with effort did not fail as an effort error"
[[ "$stderr_out" != *"is not allowed for family"* ]] || fail "empty-effort catalog agent with effort failed as an unknown-agent error"

before="$TEST_DIR/before.conf"
cp "$AGENTS_CONFIG_FILE" "$before"
assert_fails "invalid assignment set" agents_set_assignment switchable claude
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "rejected assignment changed the registry"

before="$TEST_DIR/assignable-before.conf"
expected="$TEST_DIR/assignable-expected.conf"
cp "$AGENTS_CONFIG_FILE" "$before"
sed 's/^assignable=codex$/assignable=claude/' "$before" > "$expected"
agents_set_assignment assignable claude
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "successful assignment changed lines other than its assignment"
agents_resolve assignable.work
[[ "$AGENT_FAMILY" == "claude" ]] || fail "updated assignment did not resolve to claude"
[[ "$AGENT_MODEL" == "opus" && "$AGENT_EFFORT" == "max" ]] || fail "updated assignment resolved the wrong pair"

write_fixture "$TEST_DIR/list.conf"
assignment_list="$(agents_list_assignments)"
override_count="$(printf '%s\n' "$assignment_list" | awk '$1 == "task=override.work" { count++ } END { print count + 0 }')"
[[ "$override_count" -eq 1 ]] || fail "exact-task override was listed $override_count times"
printf '%s\n' "$assignment_list" | grep -q '^task=override.work family=claude ' \
    || fail "exact-task override did not use the override family"

AGENT_MODEL="gpt-test"
AGENT_EFFORT="high"
[[ "$(agents_codex_args)" == '-m gpt-test -c model_reasoning_effort="high"' ]] || fail "codex args with effort are wrong"
AGENT_EFFORT=""
[[ "$(agents_codex_args)" == "-m gpt-test" ]] || fail "codex args without effort are wrong"

AGENT_MODEL="opus"
AGENT_EFFORT="max"
[[ "$(agents_claude_args)" == "--model opus --effort max" ]] || fail "claude args with effort are wrong"
AGENT_EFFORT=""
[[ "$(agents_claude_args)" == "--model opus" ]] || fail "claude args without effort are wrong"

echo "agents_config tests passed"
