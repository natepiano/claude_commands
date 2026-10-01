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

cat > "$TEST_DIR/switchall.conf" <<'EOF'
[assignments]
one=codex
one.work=codex
two=codex    # trailing comment

[one.codex]
work=gpt-test:high

[one.claude]
work=opus:max

[two.codex]
work=gpt-test:high

[two.claude]
work=opus:max

[codex.agents]
gpt-test=low,medium,high

[claude.agents]
opus=low,medium,high,max
EOF

cat > "$TEST_DIR/model.conf" <<'EOF'
[assignments]
one=claude
one.work=claude
two=claude    # trailing comment
friend=caller

[one.codex]
work=gpt-test:high
bare=gpt-test

[one.claude]
work=opus:max
bare=opus

[two.codex]
work=gpt-test:medium    # alias: w

[two.claude]
work=opus:max

[friend.codex]
work=gpt-test:high

[friend.claude]
work=opus:max

[codex.agents]
gpt-test=low,medium,high
gpt-new=low,medium,high
gpt-low=low

[claude.agents]
opus=low,medium,high,max
sonnet=low,medium,high,max
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
printf '%s\n' "$function_list" | grep -q '^task=editable.work family=codex agent=gpt-test effort=high active=yes tier=inherit$' \
    || fail "active codex row was not marked active"
printf '%s\n' "$function_list" | grep -q '^task=editable.work family=claude agent=opus effort=max active=no tier=-$' \
    || fail "dormant claude row was not marked inactive"

write_fixture "$TEST_DIR/list.conf"
assignment_list="$(agents_list_assignments)"
override_count="$(printf '%s\n' "$assignment_list" | awk '$1 == "task=override.work" { count++ } END { print count + 0 }')"
[[ "$override_count" -eq 1 ]] || fail "exact-task override was listed $override_count times"
printf '%s\n' "$assignment_list" | grep -q '^task=override.work family=claude ' \
    || fail "exact-task override did not use the override family"

# A bare family switches every [assignments] entry, exact-task overrides too.
write_fixture "$TEST_DIR/switchall.conf"
before="$TEST_DIR/switchall-before.conf"
expected="$TEST_DIR/switchall-expected.conf"
cp "$AGENTS_CONFIG_FILE" "$before"
sed -e 's/^one=codex$/one=claude/' \
    -e 's/^one\.work=codex$/one.work=claude/' \
    -e 's/^two=codex    # trailing comment$/two=claude    # trailing comment/' \
    "$before" > "$expected"
agents_set_all_assignments claude
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "switch-all changed lines other than the assignments or comment spacing"
agents_resolve one.work
[[ "$AGENT_FAMILY" == "claude" && "$AGENT_MODEL" == "opus" ]] || fail "switch-all did not move the exact-task override"
agents_resolve two.work
[[ "$AGENT_FAMILY" == "claude" ]] || fail "switch-all did not move a commented assignment"
agents_set_all_assignments codex
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "switch-all reversal was not byte-identical"

before="$TEST_DIR/switchall-reject.conf"
cp "$AGENTS_CONFIG_FILE" "$before"
assert_fails "unknown family" agents_set_all_assignments nosuch
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "unknown family changed the registry"
stderr_out="$(agents_set_all_assignments nosuch 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"Configured families"* ]] || fail "unknown family error did not list the families"

# One missing set or invalid row anywhere rejects the whole switch.
write_fixture "$TEST_DIR/base.conf"
before="$TEST_DIR/switchall-base.conf"
cp "$AGENTS_CONFIG_FILE" "$before"
assert_fails "switch-all with a missing set" agents_set_all_assignments claude
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "rejected switch-all changed the registry"

# A bare agent puts every function on it: fixed assignments switch to the
# agent's family, every row of that family takes the agent with its effort
# kept, and a caller function's set for that family moves too.
write_fixture "$TEST_DIR/model.conf"
before="$TEST_DIR/model-before.conf"
expected="$TEST_DIR/model-expected.conf"
cp "$AGENTS_CONFIG_FILE" "$before"
sed -e 's/^one=claude$/one=codex/' \
    -e 's/^one\.work=claude$/one.work=codex/' \
    -e 's/^two=claude    # trailing comment$/two=codex    # trailing comment/' \
    -e 's/^work=gpt-test:/work=gpt-new:/' \
    -e 's/^bare=gpt-test$/bare=gpt-new/' \
    "$before" > "$expected"
agents_set_model gpt-new
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "agent sweep changed the wrong lines"
[[ "$AGENT_SWEEP_FAMILY" == "codex" ]] || fail "agent sweep reported the wrong family"
agents_resolve two.work
[[ "$AGENT_FAMILY" == "codex" && "$AGENT_MODEL" == "gpt-new" && "$AGENT_EFFORT" == "medium" ]] \
    || fail "agent sweep did not keep the row's effort"

# The other family's rows follow the same rule.
agents_set_model sonnet
agents_resolve one.work
[[ "$AGENT_FAMILY" == "claude" && "$AGENT_MODEL" == "sonnet" && "$AGENT_EFFORT" == "max" ]] \
    || fail "claude agent sweep did not switch and keep effort"
AGENTS_CALLER_FAMILY=codex agents_resolve friend.work
[[ "$AGENT_MODEL" == "gpt-new" ]] || fail "claude agent sweep touched the caller function's codex set"

# A kept effort the agent's catalog lacks rejects the whole sweep.
write_fixture "$TEST_DIR/model.conf"
assert_fails "sweep to an agent missing a kept effort" agents_set_model gpt-low
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "rejected agent sweep changed the registry"
assert_fails "sweep with an effort" agents_set_model gpt-new:high
assert_fails "sweep to an unknown agent" agents_set_model nosuch
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "rejected agent sweep changed the registry"

# Scoped to one function, only that function and its overrides move.
sed -e 's/^one=claude$/one=codex/' \
    -e 's/^one\.work=claude$/one.work=codex/' \
    "$before" > "$expected"
awk '/^\[/ { in_sec = ($0 == "[one.codex]") }
     in_sec { sub(/=gpt-test/, "=gpt-new") }
     { print }' "$expected" > "$expected.tmp" && mv "$expected.tmp" "$expected"
agents_set_model gpt-new one
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "scoped agent sweep changed lines outside its function"
assert_fails "scoped sweep of an unknown function" agents_set_model gpt-new absent

# A caller function keeps its assignment; only its set for that family moves.
write_fixture "$TEST_DIR/model.conf"
awk '/^\[/ { in_sec = ($0 == "[friend.codex]") }
     in_sec { sub(/=gpt-test/, "=gpt-new") }
     { print }' "$before" > "$expected"
agents_set_model gpt-new friend
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "scoped sweep of a caller function changed its assignment"

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

# `caller` resolves through the agent running the shell, never through a switch.
cat > "$TEST_DIR/caller.conf" <<'EOF'
[assignments]
friend=caller
plain=codex

[friend.codex]
work=gpt-test:high

[friend.claude]
work=opus:max

[plain.codex]
work=gpt-test:high

[plain.claude]
work=opus:max

[codex.agents]
gpt-test=low,medium,high

[claude.agents]
opus=low,medium,high,max
EOF
write_fixture "$TEST_DIR/caller.conf"
unset AGENTS_CALLER_FAMILY CODEX_THREAD_ID CLAUDE_CODE_SESSION_ID

AGENTS_CALLER_FAMILY=codex agents_resolve friend.work
[[ "$AGENT_FAMILY" == "codex" && "$AGENT_MODEL" == "gpt-test" ]] || fail "caller override to codex did not resolve the codex row"
AGENTS_CALLER_FAMILY=claude agents_resolve friend.work
[[ "$AGENT_FAMILY" == "claude" && "$AGENT_MODEL" == "opus" ]] || fail "caller override to claude did not resolve the claude row"
CODEX_THREAD_ID=t agents_resolve friend.work
[[ "$AGENT_FAMILY" == "codex" ]] || fail "CODEX_THREAD_ID did not identify a codex caller"
CLAUDE_CODE_SESSION_ID=s agents_resolve friend.work
[[ "$AGENT_FAMILY" == "claude" ]] || fail "CLAUDE_CODE_SESSION_ID did not identify a claude caller"
CODEX_THREAD_ID=t CLAUDE_CODE_SESSION_ID=s agents_resolve friend.work
[[ "$AGENT_FAMILY" == "codex" ]] || fail "codex did not win when both identity variables are set"
assert_fails "caller with no detectable agent" agents_resolve friend.work
stderr_out="$(agents_resolve friend.work 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"no calling agent is detectable"* ]] || fail "undetectable caller did not fail as a caller error"
agents_resolve plain.work
[[ "$AGENT_FAMILY" == "codex" ]] || fail "a fixed assignment beside a caller stopped resolving"

before="$TEST_DIR/caller-before.conf"
cp "$AGENTS_CONFIG_FILE" "$before"
assert_fails "switching a caller function" agents_set_assignment friend codex
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "rejected caller switch changed the registry"
stderr_out="$(agents_set_assignment friend codex 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"has no switch"* ]] || fail "caller switch did not explain itself"
assert_fails "switching a function to caller" agents_set_assignment plain caller
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "rejected switch to caller changed the registry"

expected="$TEST_DIR/caller-expected.conf"
sed 's/^plain=codex$/plain=claude/' "$before" > "$expected"
agents_set_all_assignments claude
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "switch-all moved the caller assignment"
agents_set_all_assignments codex
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "switch-all reversal around a caller was not byte-identical"

function_list="$(AGENTS_CALLER_FAMILY=claude agents_list_function friend)"
printf '%s\n' "$function_list" | grep -q '^task=friend.work family=claude agent=opus effort=max active=yes tier=-$' \
    || fail "caller function did not mark the calling family live"
printf '%s\n' "$function_list" | grep -q '^task=friend.work family=codex agent=gpt-test effort=high active=no tier=inherit$' \
    || fail "caller function marked the other family live"
printf '%s\n' "$function_list" | grep -q '^# current family: caller .*claude here' \
    || fail "caller function did not report the detected family"

assignment_list="$(AGENTS_CALLER_FAMILY=codex agents_list_assignments friend)"
[[ "$(printf '%s\n' "$assignment_list" | grep -c '^task=friend.work ')" -eq 1 ]] || fail "detectable caller listed more than one family"
printf '%s\n' "$assignment_list" | grep -q '^task=friend.work family=codex ' || fail "detectable caller listed the wrong family"
assignment_list="$(agents_list_assignments friend)"
[[ "$(printf '%s\n' "$assignment_list" | grep -c '^task=friend.work ')" -eq 2 ]] || fail "undetectable caller did not list both families"
printf '%s\n' "$assignment_list" | grep -q 'none detectable here' || fail "undetectable caller listing did not say so"

AGENTS_CALLER_FAMILY=claude agents_set_row friend.work opus:high
[[ "$AGENT_ROW_ASSIGNMENT" == "caller" ]] || fail "row edit on a caller function did not expose the raw assignment"
[[ "$AGENT_ROW_ACTIVE" == "yes" && "$AGENT_ROW_ACTIVE_FAMILY" == "claude" ]] || fail "row edit on a caller function did not report the calling family live"
AGENTS_CALLER_FAMILY=codex agents_set_row friend.work opus:max
[[ "$AGENT_ROW_ACTIVE" == "no" ]] || fail "row edit for the other family was reported live under a caller"

# Only a codex row gets a speed tier: Claude's fast mode bills extra usage.
cat > "$TEST_DIR/tier.conf" <<'EOF'
[assignments]
tiered=codex
tiered.claude_task=claude
untiered=codex
mistyped=codex

[tiered.codex]
work=gpt-test:high

[tiered.claude]
claude_task=opus:max

[tiered.options]
codex_service_tier=fast

[untiered.codex]
work=gpt-test:high

[mistyped.codex]
work=gpt-test:high

[mistyped.options]
codex_service_tier=fsat

[codex.agents]
gpt-test=low,medium,high

[claude.agents]
opus=low,medium,high,max
EOF
write_fixture "$TEST_DIR/tier.conf"

agents_resolve tiered.work
[[ "$AGENT_SERVICE_TIER" == "fast" ]] || fail "codex row did not carry its function's tier"
[[ "$(agents_codex_args)" == '-m gpt-test -c model_reasoning_effort="high" -c service_tier="fast"' ]] \
    || fail "codex args did not carry the tier"
agents_resolve tiered.claude_task
[[ -z "$AGENT_SERVICE_TIER" ]] || fail "claude row carried a codex tier"
agents_resolve untiered.work
[[ -z "$AGENT_SERVICE_TIER" ]] || fail "a function without the key did not inherit the codex config"
[[ "$(agents_codex_args)" == '-m gpt-test -c model_reasoning_effort="high"' ]] \
    || fail "codex args named a tier the registry does not set"
# Codex silently runs an unknown tier at standard speed, so a typo must stop here.
assert_fails "unknown service tier" agents_resolve mistyped.work
stderr_out="$(agents_resolve mistyped.work 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"Allowed values: fast, flex, default"* ]] || fail "unknown tier error did not list the allowed values"

# A codex row with no registry tier shows what ~/.codex/config.toml gives it.
printf 'model = "gpt-test"\nservice_tier = "fast"\n\n[profiles.x]\nservice_tier = "flex"\n' > "$TEST_DIR/codex-fast.toml"
assignment_list="$(CODEX_CONFIG_FILE="$TEST_DIR/codex-fast.toml" agents_list_assignments untiered)"
[[ "$assignment_list" == *"task=untiered.work family=codex agent=gpt-test effort=high tier=inherit(fast)"* ]] \
    || fail "inherited tier did not show the Codex config value"
assignment_list="$(agents_list_assignments tiered)"
[[ "$assignment_list" == *"task=tiered.work family=codex agent=gpt-test effort=high tier=fast"* ]] \
    || fail "resolved listing did not show the registry tier"
[[ "$assignment_list" == *"task=tiered.claude_task family=claude agent=opus effort=max tier=-"* ]] \
    || fail "claude row showed a tier"

# Setting the tier at each level. `resting` runs claude, so its tier is stored
# and dormant; `clauded` has no codex set, so nothing is written for it.
cat > "$TEST_DIR/tierset.conf" <<'EOF'
[assignments]
kept=codex
bare=codex
resting=claude
friend=caller
clauded=claude

[kept.codex]
work=gpt-test:high
check=gpt-test:high

[kept.claude]
work=opus:max
check=opus:max

[kept.options]
codex_mesh=1    # another launch flag
codex_service_tier=flex    # tuned

[bare.codex]
work=gpt-test:high

[bare.claude]
work=opus:max

# ── next block ──
[resting.codex]
work=gpt-test:high

[resting.claude]
work=opus:max

[friend.codex]
work=gpt-test:high

[friend.claude]
work=opus:max

[clauded.claude]
work=opus:max

[codex.agents]
gpt-test=low,medium,high

[claude.agents]
opus=low,medium,high,max
EOF
write_fixture "$TEST_DIR/tierset.conf"
before="$TEST_DIR/tierset-before.conf"
expected="$TEST_DIR/tierset-expected.conf"
cp "$AGENTS_CONFIG_FILE" "$before"

# A function without an options section gets one after its last row.
awk '{ print }
     $0 == "[bare.claude]" { in_sec = 1 }
     in_sec && $0 == "work=opus:max" { print ""; print "[bare.options]"; print "codex_service_tier=fast"; in_sec = 0 }' \
    "$before" > "$expected"
agents_set_service_tier fast bare
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "function tier did not add an options section after the function's rows"
agents_resolve bare.work
[[ "$AGENT_SERVICE_TIER" == "fast" ]] || fail "function tier did not resolve"
agents_set_service_tier inherit bare
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "inherit did not remove the section it emptied, byte for byte"

# An existing key is replaced in place; inherit keeps the section's other flags.
sed 's/^codex_service_tier=flex    # tuned$/codex_service_tier=fast    # tuned/' "$before" > "$expected"
agents_set_service_tier fast kept
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "function tier did not replace the key in place"
sed '/^codex_service_tier=flex/d' "$before" > "$expected"
agents_set_service_tier inherit kept
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "inherit removed more than the tier key"
[[ "$(_agents_registry_get kept.options codex_mesh)" == "1" ]] || fail "inherit dropped another launch flag"

# A row key beats its function's key; inherit on the row falls back to it.
write_fixture "$TEST_DIR/tierset.conf"
awk '{ print } /^codex_service_tier=flex/ { print "codex_service_tier.check=default" }' "$before" > "$expected"
agents_set_service_tier default kept.check
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "row tier was not written after the section's last key"
agents_resolve kept.check
[[ "$AGENT_SERVICE_TIER" == "default" ]] || fail "row tier did not beat the function's tier"
agents_resolve kept.work
[[ "$AGENT_SERVICE_TIER" == "flex" ]] || fail "row tier leaked onto a sibling row"
function_list="$(agents_list_function kept)"
printf '%s\n' "$function_list" | grep -q '^task=kept.check family=codex agent=gpt-test effort=high active=yes tier=default$' \
    || fail "row tier was not shown on its row"
agents_set_service_tier inherit kept.check
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "row inherit did not restore the file"

# A function-level write clears the row keys beneath it.
agents_set_service_tier default kept.check
agents_set_service_tier fast kept
agents_resolve kept.check
[[ "$AGENT_SERVICE_TIER" == "fast" ]] || fail "function tier left a row key in charge"
if grep -q '^codex_service_tier.check=' "$AGENTS_CONFIG_FILE"; then fail "function tier did not clear the row key"; fi

# Every function at once: each function with a codex set, row keys cleared.
write_fixture "$TEST_DIR/tierset.conf"
agents_set_service_tier default kept.check
agents_set_service_tier fast
[[ "$AGENT_TIER_FUNCTIONS" == "kept bare resting friend" ]] || fail "every-function tier covered '$AGENT_TIER_FUNCTIONS'"
for fn in kept bare resting friend; do
    [[ "$(_agents_registry_get "$fn.options" codex_service_tier)" == "fast" ]] || fail "every-function tier missed $fn"
done
if _agents_config_has_section clauded.options; then fail "every-function tier wrote a function with no codex set"; fi
if grep -q '^codex_service_tier.check=' "$AGENTS_CONFIG_FILE"; then fail "every-function tier did not clear a row key"; fi

# A claude function keeps its tier dormant: stored, shown, never passed on.
agents_resolve resting.work
[[ "$AGENT_FAMILY" == "claude" && -z "$AGENT_SERVICE_TIER" ]] || fail "a claude function carried its dormant tier"
function_list="$(agents_list_function resting)"
printf '%s\n' "$function_list" | grep -q '^task=resting.work family=codex agent=gpt-test effort=high active=no tier=fast$' \
    || fail "dormant tier was not shown on the codex row"
sed '/^codex_service_tier=flex/d' "$before" > "$expected"
agents_set_service_tier inherit
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "every-function inherit did not clear every key and emptied section"

# Rejections leave the file alone.
write_fixture "$TEST_DIR/tierset.conf"
assert_fails "unknown tier word" agents_set_service_tier fsat kept
assert_fails "tier for an unknown function" agents_set_service_tier fast absent
assert_fails "tier for a function with no codex set" agents_set_service_tier fast clauded
assert_fails "tier for an unknown row" agents_set_service_tier fast kept.absent
assert_fails "tier for a three-segment task" agents_set_service_tier fast kept.work.x
assert_fails "tier for an empty sub-task" agents_set_service_tier fast kept.
cmp "$before" "$AGENTS_CONFIG_FILE" || fail "a rejected tier changed the registry"

# agent_admin.sh routes a tier word at each level and says whether it runs.
admin_first_line() {
    local output
    output="$(AGENTS_CONFIG_FILE="$AGENTS_CONFIG_FILE" CODEX_CONFIG_FILE="$CODEX_CONFIG_FILE" \
        CODEX_MODELS_CACHE_FILE="$CODEX_MODELS_CACHE_FILE" \
        CODEX_CATALOG_SYNC_STATE_FILE="$CODEX_CATALOG_SYNC_STATE_FILE" \
        bash "$SCRIPT_DIR/agent_admin.sh" "$@")"
    printf '%s' "${output%%$'\n'*}"
}
[[ "$(admin_first_line bare fast)" == "# set [bare.options] codex_service_tier=fast — live" ]] \
    || fail "function tier message is wrong"
[[ "$(admin_first_line resting.work flex)" == "# set [resting.options] codex_service_tier.work=flex — dormant: resting.work runs on claude. Make it live with: agent_admin.sh resting codex" ]] \
    || fail "dormant row tier message is wrong"
[[ "$(admin_first_line friend fast)" == "# set [friend.options] codex_service_tier=fast — live whenever a codex session runs friend" ]] \
    || fail "caller tier message is wrong"
[[ "$(admin_first_line kept.check inherit)" == "# cleared kept.check's own tier — it follows [kept.options] codex_service_tier: flex" ]] \
    || fail "row inherit message is wrong"
[[ "$(admin_first_line fast)" == "# set codex_service_tier=fast for every function — dormant for resting: they run on claude" ]] \
    || fail "every-function tier message is wrong"
[[ "$(admin_first_line inherit)" == "# cleared every function's codex tier — codex rows follow ~/.codex/config.toml, which sets no service_tier" ]] \
    || fail "every-function inherit message is wrong"
cmp "$expected" "$AGENTS_CONFIG_FILE" || fail "agent_admin.sh inherit did not clear every key"

# `pace` takes the pacer's fresh decision, and default when it is stale or absent.
cat > "$TEST_DIR/pace.conf" <<'EOF'
[assignments]
paced=codex

[paced.codex]
work=gpt-test:high

[paced.options]
codex_service_tier=pace

[codex.agents]
gpt-test=low,medium,high

[claude.agents]
opus=low,medium,high,max
EOF
write_fixture "$TEST_DIR/pace.conf"
CODEX_PACER_TIER_FILE="$TEST_DIR/pacer-tier"
printf 'fast\n' > "$CODEX_PACER_TIER_FILE"
agents_resolve paced.work
[[ "$AGENT_SERVICE_TIER" == "fast" ]] || fail "pace did not take the pacer's fast"
[[ "$(agents_list_assignments paced)" == *"task=paced.work family=codex agent=gpt-test effort=high tier=pace(fast)"* ]] \
    || fail "pace did not show its current decision"
touch -t 202001010000 "$CODEX_PACER_TIER_FILE"
agents_resolve paced.work
[[ "$AGENT_SERVICE_TIER" == "default" ]] || fail "a stale pacer decision was trusted"
rm -f "$CODEX_PACER_TIER_FILE"
agents_resolve paced.work
[[ "$AGENT_SERVICE_TIER" == "default" ]] || fail "a missing pacer decision did not fall back to default"
agents_set_service_tier default paced
agents_set_service_tier pace paced
[[ "$(_agents_registry_get paced.options codex_service_tier)" == "pace" ]] || fail "pace could not be set"

echo "agents_config tests passed"
