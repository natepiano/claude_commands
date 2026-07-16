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
editable=codex
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

[editable.codex]
work=gpt-test:high    # alias: edit

[editable.claude]
work=opus:max

[literal.codex]
work=gpt-test:high

[literal.claude]
work=opus:max

[codex.agents]
gpt-test=low,medium,high
gpt-bare=
gpt\bs=low
collide=low

[claude.agents]
opus=low,medium,high,max
collide=low
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

before="$TEST_DIR/editable-before.conf"
expected="$TEST_DIR/editable-expected.conf"
cp "$AGENTS_CONFIG_FILE" "$before"
assert_fails "invalid row agent" agents_set_row editable.work nosuch:high
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "rejected row agent changed the registry"
assert_fails "invalid row effort" agents_set_row editable.work gpt-test:max
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "rejected row effort changed the registry"
sed 's/^work=gpt-test:high    # alias: edit$/work=gpt-bare    # alias: edit/' "$before" > "$expected"
agents_set_row editable.work gpt-bare
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "successful row edit changed lines other than its row or comment spacing"
agents_resolve editable.work
[[ "$AGENT_MODEL" == "gpt-bare" && -z "$AGENT_EFFORT" ]] || fail "updated row resolved the wrong pair"
agents_set_row editable.work gpt-test:high
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "row edit reversal was not byte-identical"
sed 's/^work=gpt-test:high    # alias: edit$/work=gpt\\bs:low    # alias: edit/' "$before" > "$expected"
agents_set_row editable.work 'gpt\bs:low'
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "row edit did not preserve a literal backslash in the agent name"

write_fixture "$TEST_DIR/base.conf"
before="$TEST_DIR/infer-before.conf"
expected="$TEST_DIR/infer-expected.conf"
cp "$AGENTS_CONFIG_FILE" "$before"

# The agent names its own family: a claude agent edits the claude row even
# though the function is assigned to codex, and reports the row as dormant.
awk '/^\[/ { in_sec = ($0 == "[editable.claude]") }
     in_sec && $0 == "work=opus:max" { print "work=opus:high"; next }
     { print }' "$before" > "$expected"
agents_set_row editable.work opus:high
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "cross-family row edit did not edit exactly the claude row"
[[ "$AGENT_ROW_FAMILY" == "claude" ]] || fail "cross-family row edit reported the wrong family"
[[ "$AGENT_ROW_ACTIVE" == "no" ]] || fail "dormant row edit was not reported dormant"
[[ "$AGENT_ROW_ACTIVE_FAMILY" == "codex" ]] || fail "dormant row edit reported the wrong active family"
agents_resolve editable.work
[[ "$AGENT_MODEL" == "gpt-test" && "$AGENT_EFFORT" == "high" ]] || fail "dormant row edit changed what the task resolves to"

# Editing the active family's row reports it live.
write_fixture "$TEST_DIR/base.conf"
agents_set_row editable.work gpt-test:medium
[[ "$AGENT_ROW_FAMILY" == "codex" ]] || fail "same-family row edit reported the wrong family"
[[ "$AGENT_ROW_ACTIVE" == "yes" ]] || fail "active row edit was not reported live"
agents_resolve editable.work
[[ "$AGENT_EFFORT" == "medium" ]] || fail "active row edit did not change resolution"

# An exact-task override decides liveness, not the function's assignment.
write_fixture "$TEST_DIR/base.conf"
agents_set_row alpha.claude_task opus:high
[[ "$AGENT_ROW_FAMILY" == "claude" ]] || fail "override row edit wrote the wrong family"
[[ "$AGENT_ROW_ACTIVE" == "yes" ]] || fail "exact-task override row was not reported live"

write_fixture "$TEST_DIR/base.conf"
before="$TEST_DIR/reject-before.conf"
cp "$AGENTS_CONFIG_FILE" "$before"

assert_fails "agent in two catalogs" agents_set_row editable.work collide:low
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "ambiguous agent changed the registry"
stderr_out="$(agents_set_row editable.work collide:low 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"more than one family"* ]] || fail "ambiguous agent did not fail as an ambiguity error"

stderr_out="$(agents_set_row editable.work nosuch:high 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"[codex.agents]"* && "$stderr_out" == *"[claude.agents]"* ]] \
    || fail "unknown agent error did not list both catalogs"

# A claude agent for a function that has no claude set names the real problem.
assert_fails "inferred family has no set" agents_set_row bare.work opus:high
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "missing inferred-family set changed the registry"
stderr_out="$(agents_set_row bare.work opus:high 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"no [bare.claude]"* ]] || fail "missing inferred-family set did not name the section"

function_list="$(agents_list_function editable)"
printf '%s\n' "$function_list" | grep -q '^task=editable.work family=codex agent=gpt-test effort=high active=yes$' \
    || fail "active codex row was not marked active"
printf '%s\n' "$function_list" | grep -q '^task=editable.work family=claude agent=opus effort=max active=no$' \
    || fail "dormant claude row was not marked inactive"

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
