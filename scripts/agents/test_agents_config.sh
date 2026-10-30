#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../lib/test_agents_registry.sh"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agents-config.XXXXXX")"
trap 'rm -rf "$TEST_DIR"' EXIT

AGENTS_CONFIG_FILE="$TEST_DIR/agents.conf"
AGENTS_LOCAL_CONFIG_FILE="$TEST_DIR/agents.local.conf"
CODEX_CONFIG_FILE="$TEST_DIR/codex.toml"
CODEX_MODELS_CACHE_FILE="$TEST_DIR/models.json"
CODEX_CATALOG_SYNC_STATE_FILE="$TEST_DIR/last_success"
export AGENTS_CONFIG_FILE AGENTS_LOCAL_CONFIG_FILE CODEX_CONFIG_FILE
export CODEX_MODELS_CACHE_FILE CODEX_CATALOG_SYNC_STATE_FILE

touch "$CODEX_CATALOG_SYNC_STATE_FILE"

write_fixture() {
    # Model editable machine rows above immutable, empty defaults. Deleting a
    # local override is different from trying to delete a committed default.
    : > "$AGENTS_CONFIG_FILE"
    cp "$1" "$AGENTS_LOCAL_CONFIG_FILE"
}

write_test_registry "$TEST_DIR/base.conf" --replace-provided-sections --replace-section claude.families \
    assignments/alpha=codex \
    assignments/alpha.claude_task=claude \
    assignments/bare=codex \
    assignments/bare_catalog=codex \
    assignments/bare_catalog_effort=codex \
    assignments/missing_set=codex \
    assignments/bad_agent=codex \
    assignments/bad_effort=codex \
    assignments/empty_effort=codex \
    assignments/switchable=codex \
    assignments/assignable=codex \
    assignments/editable=codex \
    assignments/literal=codex \
    assignments/literalxwork=claude \
    alpha.codex/work=gpt-test:high \
    alpha.claude/claude_task=opus:max \
    bare.codex/work=gpt-test \
    bare_catalog.codex/work=gpt-bare \
    bare_catalog_effort.codex/work=gpt-bare:high \
    bad_agent.codex/work=gpt-missing:high \
    bad_effort.codex/work=gpt-test:xhigh \
    empty_effort.codex/work=gpt-test: \
    switchable.codex/work=gpt-test:high \
    switchable.claude/work=opus:max \
    switchable.claude/broken=missing:max \
    assignable.codex/work=gpt-test:high \
    assignable.claude/work=opus:max \
    'editable.codex/work=gpt-test:high    # alias: edit' \
    editable.claude/work=opus:max \
    literal.codex/work=gpt-test:high \
    literal.claude/work=opus:max \
    codex.agents/gpt-test=low,medium,high \
    codex.agents/gpt-bare= \
    'codex.agents/gpt\bs=low' \
    codex.agents/collide=low \
    claude.agents/opus=low,medium,high,max \
    claude.agents/collide=low

write_test_registry "$TEST_DIR/list.conf" --replace-provided-sections \
    assignments/override=codex \
    assignments/override.work=claude \
    override.codex/work=gpt-test:high \
    override.claude/work=opus:max \
    codex.agents/gpt-test=low,medium,high \
    claude.agents/opus=low,medium,high,max

write_test_registry "$TEST_DIR/switchall.conf" --replace-provided-sections \
    assignments/one=codex \
    assignments/one.work=codex \
    'assignments/two=codex    # trailing comment' \
    one.codex/work=gpt-test:high \
    one.claude/work=opus:max \
    two.codex/work=gpt-test:high \
    two.claude/work=opus:max \
    codex.agents/gpt-test=low,medium,high \
    claude.agents/opus=low,medium,high,max

write_test_registry "$TEST_DIR/model.conf" --replace-provided-sections \
    assignments/one=claude \
    assignments/one.work=claude \
    'assignments/two=claude    # trailing comment' \
    assignments/friend=caller \
    one.codex/work=gpt-test:high \
    one.codex/bare=gpt-test \
    one.claude/work=opus:max \
    one.claude/bare=opus \
    'two.codex/work=gpt-test:medium    # alias: w' \
    two.claude/work=opus:max \
    friend.codex/work=gpt-test:high \
    friend.claude/work=opus:max \
    codex.agents/gpt-test=low,medium,high \
    codex.agents/gpt-new=low,medium,high \
    codex.agents/gpt-low=low \
    claude.agents/opus=low,medium,high,max \
    claude.agents/sonnet=low,medium,high,max

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

# Overlay writers preserve rows, while section order and comments belong to
# the committed defaults. Compare the effective records, including every row,
# so an unexpected change outside the requested edit still fails the test.
assert_registry_matches() {
    diff -u <(registry_records < "$1") <(agents_registry_text | registry_records)
}

test_model_families() (
    # Keep these fixtures separate from the legacy mutation cases below. A
    # generated family row may lag the cache until the next catalog sync.
    AGENTS_CONFIG_FILE="$TEST_DIR/families.conf"
    AGENTS_LOCAL_CONFIG_FILE="$TEST_DIR/families.local.conf"
    # /agent sources the resolver in a child; keep the freshness gate closed
    # so these effort-less cache fixtures exercise resolution alone.
    AGENTS_CONFIG_TIER_ONLY=1
    export AGENTS_CONFIG_TIER_ONLY
    write_test_registry "$AGENTS_CONFIG_FILE" --replace-provided-sections \
        assignments/launch=codex \
        assignments/launch.claude=claude \
        launch.codex/work=sol:xhigh \
        launch.codex/bare=sol \
        launch.codex/pinned=gpt-6.9-sol:high \
        launch.codex/bad=sol:max \
        launch.codex/undeclared=terra:high \
        launch.claude/claude=haiku:high \
        codex.agents/gpt-6.9-sol=high,max \
        codex.agents/gpt-6.10-sol=medium,high,xhigh \
        codex.agents/gpt-9-sol=high,max \
        codex.agents/gpt-6-terra=high \
        codex.families/sol=gpt-6.9-sol \
        codex.families/terra \
        claude.agents/haiku=low,medium,high,xhigh,max \
        claude.families/haiku=haiku
    cat > "$CODEX_MODELS_CACHE_FILE" <<'EOF'
{"models":[
  {"slug":"gpt-6.10-sol","visibility":"list"},
  {"slug":"gpt-9-sol","visibility":"hide"},
  {"slug":"gpt-6-terra","visibility":"list"},
  {"slug":"gpt-6.9-sol","visibility":"list"},
  {"slug":"gpt-99-solar","visibility":"list"}
]}
EOF

    agents_resolve launch.work || fail "Codex model family did not resolve"
    [[ "$AGENT_FAMILY" == codex && "$AGENT_MODEL" == gpt-6.10-sol && "$AGENT_EFFORT" == xhigh ]] \
        || fail "sol did not select the highest numeric visible version from the current cache"
    [[ "$(agents_codex_args)" == '-m gpt-6.10-sol -c model_reasoning_effort="xhigh"' ]] \
        || fail "Codex launch arguments did not use the resolved family slug"
    agents_resolve launch.bare || fail "bare Codex model family did not resolve"
    [[ "$AGENT_MODEL" == gpt-6.10-sol && -z "$AGENT_EFFORT" ]] \
        || fail "bare family did not preserve the CLI's default effort"
    agents_resolve launch.pinned || fail "full slug stopped resolving beside a family row"
    [[ "$AGENT_MODEL" == gpt-6.9-sol && "$AGENT_EFFORT" == high ]] \
        || fail "full slug was upgraded to the newest family model"

    assert_fails "family effort only supported by an older slug" agents_resolve launch.bad
    stderr_out="$(agents_resolve launch.bad 2>&1 >/dev/null || true)"
    [[ "$stderr_out" == *"effort 'max' is not allowed"* ]] \
        || fail "invalid family effort was reported as an unknown model"
    assert_fails "cache family absent from [codex.families]" agents_resolve launch.undeclared

    admin_output="$(bash "$SCRIPT_DIR/agent_admin.sh" launch)" \
        || fail "/agent could not display family assignments"
    family_display="$(printf '%s\n' "$admin_output" | awk '$1 == "task=launch.work" { print }')"
    [[ "$family_display" == *" word=sol"* && "$family_display" == *" agent=gpt-6.10-sol "* ]] \
        || fail "/agent did not display both the family word and its current slug"

    # Missing, malformed and hidden-only caches must never fall back to the
    # previously generated family row. Explicit slugs remain usable offline.
    no_model_message="no Codex model for 'sol' in ~/.codex/models_cache.json; run codex once to refresh the cache, or name the full slug"
    for cache_state in other-family hidden-only missing malformed unreadable; do
        case "$cache_state" in
            other-family) printf '%s\n' '{"models":[{"slug":"gpt-6-astra","visibility":"list"}]}' > "$CODEX_MODELS_CACHE_FILE" ;;
            hidden-only) printf '%s\n' '{"models":[{"slug":"gpt-9-sol","visibility":"hide"}]}' > "$CODEX_MODELS_CACHE_FILE" ;;
            missing) rm -f "$CODEX_MODELS_CACHE_FILE" ;;
            malformed) printf '%s\n' '{invalid' > "$CODEX_MODELS_CACHE_FILE" ;;
            unreadable)
                printf '%s\n' '{"models":[{"slug":"gpt-6.10-sol","visibility":"list"}]}' > "$CODEX_MODELS_CACHE_FILE"
                chmod 000 "$CODEX_MODELS_CACHE_FILE"
                ;;
        esac
        # Root can read mode-000 files, so it cannot exercise this case.
        if [[ "$cache_state" == unreadable && -r "$CODEX_MODELS_CACHE_FILE" ]]; then
            chmod 600 "$CODEX_MODELS_CACHE_FILE"
            continue
        fi
        resolve_status=0
        stderr_out="$(agents_resolve launch.work 2>&1 >/dev/null)" || resolve_status=$?
        [[ "$resolve_status" -eq 1 && "$stderr_out" == *"$no_model_message"* ]] \
            || fail "$cache_state cache did not refuse sol with the named recovery message: $stderr_out"
        agents_resolve launch.pinned || fail "full slug required a usable cache ($cache_state)"
        [[ "$AGENT_MODEL" == gpt-6.9-sol && "$AGENT_EFFORT" == high ]] \
            || fail "offline full slug resolved incorrectly ($cache_state)"
        agents_resolve launch.claude || fail "Claude alias required a Codex cache ($cache_state)"
        [[ "$AGENT_MODEL" == haiku && "$AGENT_EFFORT" == high ]] \
            || fail "Claude family alias was not passed to its CLI"
        [[ "$cache_state" != unreadable ]] || chmod 600 "$CODEX_MODELS_CACHE_FILE"
    done
    rm -f "$CODEX_MODELS_CACHE_FILE"
    echo "agents_config model-family tests passed"
)

test_model_families

test_family_repairs() (
    AGENTS_CONFIG_FILE="$TEST_DIR/family-repairs.conf"
    AGENTS_LOCAL_CONFIG_FILE="$TEST_DIR/family-repairs.local.conf"
    CODEX_MODELS_CACHE_FILE="$TEST_DIR/family-repairs.json"
    AGENTS_CONFIG_TIER_ONLY=1
    export AGENTS_CONFIG_TIER_ONLY
    : > "$AGENTS_CONFIG_FILE"
    write_test_registry "$AGENTS_LOCAL_CONFIG_FILE" --replace-provided-sections \
        assignments/direct=codex \
        assignments/probe=codex \
        assignments/dormant=claude \
        direct.codex/impl=gpt-6.1-sol:xhigh \
        direct.codex/review=gpt-6.1-sol:high \
        direct.claude/impl=haiku:high \
        direct.claude/review=haiku:max \
        probe.codex/work=sol:high \
        dormant.codex/work=gpt-6.1-sol:high \
        dormant.claude/work=haiku:high \
        codex.agents/gpt-6.1-sol=high,xhigh,max \
        codex.agents/gpt-6.01-sol=high,xhigh,max \
        codex.families/sol=gpt-6.1-sol \
        claude.agents/haiku=low,medium,high,xhigh,max \
        claude.families/haiku=haiku
    # A newly visible release cannot launch until its catalog row is known.
    cat > "$CODEX_MODELS_CACHE_FILE" <<'EOF'
{"models":[
  {"slug":"gpt-6.1-sol","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-7-sol","visibility":"list"}
]}
EOF
    agents_resolve probe.work || fail "incomplete newest release blocked the usable family model"
    [[ "$AGENT_MODEL" == gpt-6.1-sol ]] \
        || fail "family selected a new release without a catalog row"

    # Equal numeric versions use the same slug regardless of cache order.
    for slug_order in 'gpt-6.1-sol gpt-6.01-sol' 'gpt-6.01-sol gpt-6.1-sol'; do
        read -r first_slug second_slug <<< "$slug_order"
        printf '{"models":[{"slug":"%s","visibility":"list"},{"slug":"%s","visibility":"list"}]}\n' \
            "$first_slug" "$second_slug" > "$CODEX_MODELS_CACHE_FILE"
        agents_resolve probe.work || fail "equal-version family candidates did not resolve"
        [[ "$AGENT_MODEL" == gpt-6.1-sol ]] \
            || fail "numeric version tie depended on cache order: $slug_order"
    done

    agents_registry_text > "$TEST_DIR/family-row-before.conf"
    awk '/^\[/ { editing = ($0 == "[direct.codex]") }
         editing { sub(/^review=gpt-6.1-sol:high$/, "review=sol:max") }
         { print }' "$TEST_DIR/family-row-before.conf" > "$TEST_DIR/family-row-expected.conf"
    bash "$SCRIPT_DIR/agent_admin.sh" direct.review sol:max > "$TEST_DIR/family-admin-output" \
        || fail "/agent direct.review sol:max rejected a valid family edit"
    assert_registry_matches "$TEST_DIR/family-row-expected.conf" \
        || fail "family row edit stored a slug or changed another registry row"
    agents_resolve direct.review || fail "stored family review row did not resolve"
    [[ "$AGENT_MODEL" == gpt-6.1-sol && "$AGENT_EFFORT" == max ]] \
        || fail "stored family review row lost its effort"

    # The all-function command stores the word while preserving each effort.
    awk '/^\[/ { codex_rows = ($0 ~ /^\[(direct|probe|dormant)\.codex\]$/) }
         /^dormant=claude$/ { $0 = "dormant=codex" }
         codex_rows { sub(/=gpt-6.1-sol:/, "=sol:") }
         { print }' "$TEST_DIR/family-row-expected.conf" > "$TEST_DIR/family-sweep-expected.conf"
    bash "$SCRIPT_DIR/agent_admin.sh" sol > "$TEST_DIR/family-admin-output" \
        || fail "/agent sol rejected a valid family sweep"
    assert_registry_matches "$TEST_DIR/family-sweep-expected.conf" \
        || fail "family sweep expanded the word to a slug, lost efforts, or changed unrelated rows"
    agents_resolve direct.impl || fail "swept family implementation row did not resolve"
    [[ "$AGENT_MODEL" == gpt-6.1-sol && "$AGENT_EFFORT" == xhigh ]] \
        || fail "family sweep changed the implementation effort"

    agents_set_assignment dormant claude
    rm "$CODEX_MODELS_CACHE_FILE"
    no_model_message="no Codex model for 'sol' in ~/.codex/models_cache.json; run codex once to refresh the cache, or name the full slug"
    admin_output="$(bash "$SCRIPT_DIR/agent_admin.sh" dormant)" \
        || fail "missing cache blocked status for a function running on Claude"
    dormant_display="$(printf '%s\n' "$admin_output" | awk '/^task=dormant.work family=codex /')"
    [[ "$dormant_display" == *"sol (unresolved: $no_model_message)"* && "$dormant_display" == *"active=no"* ]] \
        || fail "status omitted the unresolved dormant family and its recovery reason: $admin_output"
    [[ "$admin_output" == *"task=dormant.work family=claude agent=haiku effort=high active=yes"* \
        && "$admin_output" == *"# current family: claude"* ]] \
        || fail "unresolved Codex status stopped before the active Claude row"
    agents_resolve dormant.work || fail "unresolved dormant family blocked a Claude launch"
    [[ "$AGENT_MODEL" == haiku ]] || fail "Claude launch used the dormant family"
    admin_output="$(bash "$SCRIPT_DIR/agent_admin.sh" probe)" \
        || fail "missing cache blocked status for an active family row"
    [[ "$admin_output" == *"sol (unresolved: $no_model_message)"* ]] \
        || fail "active family status did not show its resolution failure"
    resolve_status=0
    stderr_out="$(agents_resolve probe.work 2>&1 >/dev/null)" || resolve_status=$?
    [[ "$resolve_status" -eq 1 && "$stderr_out" == *"$no_model_message"* ]] \
        || fail "a launch needing the unresolved family did not fail with the recovery reason"
    echo "agents_config family repair tests passed"
)

test_family_repairs

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
agents_registry_text > "$before"
assert_fails "invalid assignment set" agents_set_assignment switchable claude
assert_registry_matches "$before" || fail "rejected assignment changed the registry"

before="$TEST_DIR/assignable-before.conf"
expected="$TEST_DIR/assignable-expected.conf"
agents_registry_text > "$before"
sed 's/^assignable=codex$/assignable=claude/' "$before" > "$expected"
agents_set_assignment assignable claude
assert_registry_matches "$expected" || fail "successful assignment changed rows other than its assignment"
agents_resolve assignable.work
[[ "$AGENT_FAMILY" == "claude" ]] || fail "updated assignment did not resolve to claude"
[[ "$AGENT_MODEL" == "opus" && "$AGENT_EFFORT" == "max" ]] || fail "updated assignment resolved the wrong pair"

before="$TEST_DIR/editable-before.conf"
expected="$TEST_DIR/editable-expected.conf"
agents_registry_text > "$before"
assert_fails "invalid row agent" agents_set_row editable.work nosuch:high
assert_registry_matches "$before" || fail "rejected row agent changed the registry"
assert_fails "invalid row effort" agents_set_row editable.work gpt-test:max
assert_registry_matches "$before" || fail "rejected row effort changed the registry"
awk '/^\[/ { editing = ($0 == "[editable.codex]") }
     editing { sub(/^work=gpt-test:high$/, "work=gpt-bare") }
     { print }' "$before" > "$expected"
agents_set_row editable.work gpt-bare
assert_registry_matches "$expected" || fail "successful row edit changed rows other than its target"
agents_resolve editable.work
[[ "$AGENT_MODEL" == "gpt-bare" && -z "$AGENT_EFFORT" ]] || fail "updated row resolved the wrong pair"
agents_set_row editable.work gpt-test:high
assert_registry_matches "$before" || fail "row edit reversal changed registry rows"
awk '/^\[/ { editing = ($0 == "[editable.codex]") }
     editing && $0 == "work=gpt-test:high" { print "work=gpt\\bs:low"; next }
     { print }' "$before" > "$expected"
agents_set_row editable.work 'gpt\bs:low'
assert_registry_matches "$expected" || fail "row edit did not preserve a literal backslash in the agent name"

write_fixture "$TEST_DIR/base.conf"
before="$TEST_DIR/infer-before.conf"
expected="$TEST_DIR/infer-expected.conf"
agents_registry_text > "$before"

# The agent names its own family: a claude agent edits the claude row even
# though the function is assigned to codex, and reports the row as dormant.
awk '/^\[/ { in_sec = ($0 == "[editable.claude]") }
     in_sec && $0 == "work=opus:max" { print "work=opus:high"; next }
     { print }' "$before" > "$expected"
agents_set_row editable.work opus:high
assert_registry_matches "$expected" || fail "cross-family row edit changed more than the claude row"
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
agents_registry_text > "$before"

assert_fails "agent in two catalogs" agents_set_row editable.work collide:low
assert_registry_matches "$before" || fail "ambiguous agent changed the registry"
stderr_out="$(agents_set_row editable.work collide:low 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"more than one family"* ]] || fail "ambiguous agent did not fail as an ambiguity error"

stderr_out="$(agents_set_row editable.work nosuch:high 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"[codex.agents]"* && "$stderr_out" == *"[claude.agents]"* ]] \
    || fail "unknown agent error did not list both catalogs"

# A Claude agent cannot move a function that has only a Codex set.
assert_fails "switching a Codex-only row to Claude" agents_set_row bare.work opus:high
assert_registry_matches "$before" || fail "rejected Codex-only row edit changed the registry"
stderr_out="$(agents_set_row bare.work opus:high 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"'bare' runs only on codex: [bare.codex] is its only set."* ]] \
    || fail "one-family row refusal did not name its only set"

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
agents_registry_text > "$before"
sed -e 's/^one=codex$/one=claude/' \
    -e 's/^one\.work=codex$/one.work=claude/' \
    -e 's/^two=codex$/two=claude/' \
    "$before" > "$expected"
agents_set_all_assignments claude
assert_registry_matches "$expected" || fail "switch-all changed rows other than the assignments"
agents_resolve one.work
[[ "$AGENT_FAMILY" == "claude" && "$AGENT_MODEL" == "opus" ]] || fail "switch-all did not move the exact-task override"
agents_resolve two.work
[[ "$AGENT_FAMILY" == "claude" ]] || fail "switch-all did not move a commented assignment"
agents_set_all_assignments codex
assert_registry_matches "$before" || fail "switch-all reversal changed registry rows"

before="$TEST_DIR/switchall-reject.conf"
agents_registry_text > "$before"
assert_fails "unknown family" agents_set_all_assignments nosuch
assert_registry_matches "$before" || fail "unknown family changed the registry"
stderr_out="$(agents_set_all_assignments nosuch 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"Configured families"* ]] || fail "unknown family error did not list the families"

# A function with no set in either family still rejects a whole switch;
# codex-only functions in this fixture are pinned and do not cause it.
write_fixture "$TEST_DIR/base.conf"
before="$TEST_DIR/switchall-base.conf"
agents_registry_text > "$before"
assert_fails "switch-all with a missing set" agents_set_all_assignments claude
assert_registry_matches "$before" || fail "rejected switch-all changed the registry"
stderr_out="$(agents_set_all_assignments claude 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"missing [missing_set.claude]"* ]] \
    || fail "switch-all refusal was not caused by the function with no set"

# A bare agent puts every function on it: fixed assignments switch to the
# agent's family, every row of that family takes the agent with its effort
# kept, and a caller function's set for that family moves too.
write_fixture "$TEST_DIR/model.conf"
before="$TEST_DIR/model-before.conf"
expected="$TEST_DIR/model-expected.conf"
agents_registry_text > "$before"
sed -e 's/^one=claude$/one=codex/' \
    -e 's/^one\.work=claude$/one.work=codex/' \
    -e 's/^two=claude$/two=codex/' \
    -e 's/^work=gpt-test:/work=gpt-new:/' \
    -e 's/^bare=gpt-test$/bare=gpt-new/' \
    "$before" > "$expected"
agents_set_model gpt-new
assert_registry_matches "$expected" || fail "agent sweep changed the wrong lines"
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
assert_registry_matches "$before" || fail "rejected agent sweep changed the registry"
assert_fails "sweep with an effort" agents_set_model gpt-new:high
assert_fails "sweep to an unknown agent" agents_set_model nosuch
assert_registry_matches "$before" || fail "rejected agent sweep changed the registry"

# Scoped to one function, only that function and its overrides move.
sed -e 's/^one=claude$/one=codex/' \
    -e 's/^one\.work=claude$/one.work=codex/' \
    "$before" > "$expected"
awk '/^\[/ { in_sec = ($0 == "[one.codex]") }
     in_sec { sub(/=gpt-test/, "=gpt-new") }
     { print }' "$expected" > "$expected.tmp" && mv "$expected.tmp" "$expected"
agents_set_model gpt-new one
assert_registry_matches "$expected" || fail "scoped agent sweep changed lines outside its function"
assert_fails "scoped sweep of an unknown function" agents_set_model gpt-new absent

# A caller function keeps its assignment; only its set for that family moves.
write_fixture "$TEST_DIR/model.conf"
awk '/^\[/ { in_sec = ($0 == "[friend.codex]") }
     in_sec { sub(/=gpt-test/, "=gpt-new") }
     { print }' "$before" > "$expected"
agents_set_model gpt-new friend
assert_registry_matches "$expected" || fail "scoped sweep of a caller function changed its assignment"

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
write_test_registry "$TEST_DIR/caller.conf" --replace-provided-sections --replace-section claude.families \
    assignments/friend=caller \
    assignments/plain=codex \
    friend.codex/work=gpt-test:high \
    friend.claude/work=opus:max \
    plain.codex/work=gpt-test:high \
    plain.claude/work=opus:max \
    codex.agents/gpt-test=low,medium,high \
    claude.agents/opus=low,medium,high,max
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
agents_registry_text > "$before"
assert_fails "switching a caller function" agents_set_assignment friend codex
assert_registry_matches "$before" || fail "rejected caller switch changed the registry"
stderr_out="$(agents_set_assignment friend codex 2>&1 >/dev/null || true)"
[[ "$stderr_out" == *"has no switch"* ]] || fail "caller switch did not explain itself"
assert_fails "switching a function to caller" agents_set_assignment plain caller
assert_registry_matches "$before" || fail "rejected switch to caller changed the registry"

expected="$TEST_DIR/caller-expected.conf"
sed 's/^plain=codex$/plain=claude/' "$before" > "$expected"
agents_set_all_assignments claude
assert_registry_matches "$expected" || fail "switch-all moved the caller assignment"
agents_set_all_assignments codex
assert_registry_matches "$before" || fail "switch-all reversal around a caller changed registry rows"

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
write_test_registry "$TEST_DIR/tier.conf" --replace-provided-sections \
    assignments/tiered=codex \
    assignments/tiered.claude_task=claude \
    assignments/untiered=codex \
    assignments/mistyped=codex \
    tiered.codex/work=gpt-test:high \
    tiered.claude/claude_task=opus:max \
    tiered.options/codex_service_tier=fast \
    untiered.codex/work=gpt-test:high \
    mistyped.codex/work=gpt-test:high \
    mistyped.options/codex_service_tier=fsat \
    codex.agents/gpt-test=low,medium,high \
    claude.agents/opus=low,medium,high,max
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
write_test_registry "$TEST_DIR/tierset.conf" --replace-provided-sections \
    assignments/kept=codex \
    assignments/bare=codex \
    assignments/resting=claude \
    assignments/friend=caller \
    assignments/clauded=claude \
    kept.codex/work=gpt-test:high \
    kept.codex/check=gpt-test:high \
    kept.claude/work=opus:max \
    kept.claude/check=opus:max \
    'kept.options/codex_mesh=1    # another launch flag' \
    'kept.options/codex_service_tier=flex    # tuned' \
    bare.codex/work=gpt-test:high \
    bare.claude/work=opus:max \
    resting.codex/work=gpt-test:high \
    resting.claude/work=opus:max \
    friend.codex/work=gpt-test:high \
    friend.claude/work=opus:max \
    clauded.claude/work=opus:max \
    codex.agents/gpt-test=low,medium,high \
    claude.agents/opus=low,medium,high,max
write_fixture "$TEST_DIR/tierset.conf"
before="$TEST_DIR/tierset-before.conf"
expected="$TEST_DIR/tierset-expected.conf"
agents_registry_text > "$before"

# A function without an options section gets one after its last row.
awk '{ print }
     $0 == "[bare.claude]" { in_sec = 1 }
     in_sec && $0 == "work=opus:max" { print ""; print "[bare.options]"; print "codex_service_tier=fast"; in_sec = 0 }' \
    "$before" > "$expected"
agents_set_service_tier fast bare
assert_registry_matches "$expected" || fail "function tier did not add its options row"
agents_resolve bare.work
[[ "$AGENT_SERVICE_TIER" == "fast" ]] || fail "function tier did not resolve"
agents_set_service_tier inherit bare
assert_registry_matches "$before" || fail "inherit did not clear the local tier row"

# An existing key is replaced in place; inherit keeps the section's other flags.
sed 's/^codex_service_tier=flex$/codex_service_tier=fast/' "$before" > "$expected"
agents_set_service_tier fast kept
assert_registry_matches "$expected" || fail "function tier changed rows other than its key"
sed '/^codex_service_tier=flex/d' "$before" > "$expected"
agents_set_service_tier inherit kept
assert_registry_matches "$expected" || fail "inherit removed more than the tier key"
[[ "$(_agents_registry_get kept.options codex_mesh)" == "1" ]] || fail "inherit dropped another launch flag"

# A row key beats its function's key; inherit on the row falls back to it.
write_fixture "$TEST_DIR/tierset.conf"
awk '{ print } /^codex_service_tier=flex/ { print "codex_service_tier.check=default" }' "$before" > "$expected"
agents_set_service_tier default kept.check
assert_registry_matches "$expected" || fail "row tier was not written after the section's last key"
agents_resolve kept.check
[[ "$AGENT_SERVICE_TIER" == "default" ]] || fail "row tier did not beat the function's tier"
[[ "$(agents_turn_service_tier kept.check)" == "default" ]] || fail "turn tier missed the row override"
[[ "$(agents_turn_service_tier kept)" == "flex" ]] || fail "unknown task did not use the function tier"
printf 'service_tier = "fast"\n' > "$TEST_DIR/turn-codex-fast.toml"
[[ "$(CODEX_CONFIG_FILE="$TEST_DIR/turn-codex-fast.toml" agents_turn_service_tier bare.work)" == "default" ]] \
    || fail "empty turn tier inherited the Codex config"
agents_resolve kept.work
[[ "$AGENT_SERVICE_TIER" == "flex" ]] || fail "row tier leaked onto a sibling row"
function_list="$(agents_list_function kept)"
printf '%s\n' "$function_list" | grep -q '^task=kept.check family=codex agent=gpt-test effort=high active=yes tier=default$' \
    || fail "row tier was not shown on its row"
agents_set_service_tier inherit kept.check
assert_registry_matches "$before" || fail "row inherit did not restore the file"

# A function-level write clears the row keys beneath it.
agents_set_service_tier default kept.check
agents_set_service_tier fast kept
agents_resolve kept.check
[[ "$AGENT_SERVICE_TIER" == "fast" ]] || fail "function tier left a row key in charge"
if grep -q '^codex_service_tier.check=' <(agents_registry_text); then fail "function tier did not clear the row key"; fi

# Every function at once: each function with a codex set, row keys cleared.
write_fixture "$TEST_DIR/tierset.conf"
agents_set_service_tier default kept.check
agents_set_service_tier fast
[[ "$AGENT_TIER_FUNCTIONS" == "kept bare resting friend" ]] || fail "every-function tier covered '$AGENT_TIER_FUNCTIONS'"
for fn in kept bare resting friend; do
    [[ "$(_agents_registry_get "$fn.options" codex_service_tier)" == "fast" ]] || fail "every-function tier missed $fn"
done
if _agents_config_has_section clauded.options; then fail "every-function tier wrote a function with no codex set"; fi
if grep -q '^codex_service_tier.check=' <(agents_registry_text); then fail "every-function tier did not clear a row key"; fi

# A claude function keeps its tier dormant: stored, shown, never passed on.
agents_resolve resting.work
[[ "$AGENT_FAMILY" == "claude" && -z "$AGENT_SERVICE_TIER" ]] || fail "a claude function carried its dormant tier"
function_list="$(agents_list_function resting)"
printf '%s\n' "$function_list" | grep -q '^task=resting.work family=codex agent=gpt-test effort=high active=no tier=fast$' \
    || fail "dormant tier was not shown on the codex row"
sed '/^codex_service_tier=flex/d' "$before" > "$expected"
agents_set_service_tier inherit
assert_registry_matches "$expected" || fail "every-function inherit did not clear every key and emptied section"

# Rejections leave the file alone.
write_fixture "$TEST_DIR/tierset.conf"
assert_fails "unknown tier word" agents_set_service_tier fsat kept
assert_fails "tier for an unknown function" agents_set_service_tier fast absent
assert_fails "tier for a function with no codex set" agents_set_service_tier fast clauded
assert_fails "tier for an unknown row" agents_set_service_tier fast kept.absent
assert_fails "tier for a three-segment task" agents_set_service_tier fast kept.work.x
assert_fails "tier for an empty sub-task" agents_set_service_tier fast kept.
assert_registry_matches "$before" || fail "a rejected tier changed the registry"

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
assert_registry_matches "$expected" || fail "agent_admin.sh inherit did not clear every key"

# `pace` takes the pacer's fresh decision, and default when it is stale or absent.
write_test_registry "$TEST_DIR/pace.conf" --replace-provided-sections \
    assignments/paced=codex \
    paced.codex/work=gpt-test:high \
    paced.options/codex_service_tier=pace \
    codex.agents/gpt-test=low,medium,high \
    claude.agents/opus=low,medium,high,max
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

# Production has only a Claude set. Every-function switches keep it, while a
# direct switch of production must refuse before changing a byte.
write_test_registry "$TEST_DIR/production.conf" --replace-provided-sections \
    assignments/ordinary=claude \
    assignments/production=claude \
    assignments/codex_only=codex \
    ordinary.codex/work=gpt-old:high \
    ordinary.claude/work=opus:high \
    production.claude/director=opus:xhigh \
    codex_only.codex/work=gpt-old:high \
    codex.agents/gpt-old=low,medium,high \
    codex.agents/gpt-test=low,medium,high \
    claude.agents/opus=low,medium,high,xhigh \
    claude.agents/sonnet=low,medium,high,xhigh

admin() {
    AGENTS_CONFIG_FILE="$AGENTS_CONFIG_FILE" CODEX_CONFIG_FILE="$CODEX_CONFIG_FILE" \
        CODEX_MODELS_CACHE_FILE="$CODEX_MODELS_CACHE_FILE" \
        CODEX_CATALOG_SYNC_STATE_FILE="$CODEX_CATALOG_SYNC_STATE_FILE" \
        bash "$SCRIPT_DIR/agent_admin.sh" "$@"
}

assert_production_refusal() {
    local output
    agents_registry_text > "$TEST_DIR/production-before.conf"
    if output="$(admin "$@" 2>&1)"; then
        fail "production switch unexpectedly succeeded: $*"
    fi
    [[ "${output%%$'\n'*}" == "ERROR: 'production' runs only on claude: [production.claude] is its only set." ]] \
        || fail "production switch gave the wrong error: $output"
    assert_registry_matches "$TEST_DIR/production-before.conf" \
        || fail "rejected production switch changed the registry: $*"
}

write_fixture "$TEST_DIR/production.conf"
admin_output="$(admin codex)"
[[ "$admin_output" == *$'# switched every function to codex\n# kept production on claude: its only set'* ]] \
    || fail "family switch did not report kept production after switched line"
[[ "$admin_output" == *"# kept codex_only on codex: its only set"* ]] \
    || fail "family switch did not report kept Codex-only function"
[[ "$(_agents_registry_get assignments ordinary)" == "codex" ]] || fail "family switch missed ordinary"
[[ "$(_agents_registry_get assignments production)" == "claude" ]] || fail "family switch moved production"
[[ "$(_agents_registry_get assignments codex_only)" == "codex" ]] || fail "family switch moved Codex-only function"
agents_resolve production.director
[[ "$AGENT_FAMILY" == "claude" && "$AGENT_MODEL" == "opus" && "$AGENT_EFFORT" == "xhigh" ]] \
    || fail "family switch changed production director"

write_fixture "$TEST_DIR/production.conf"
admin_output="$(admin gpt-test)"
[[ "$admin_output" == *$'# switched every function to gpt-test (codex), efforts kept\n# kept production on claude: its only set'* ]] \
    || fail "agent sweep did not report kept production after switched line"
[[ "$admin_output" == *"# kept codex_only on codex: its only set"* ]] \
    || fail "agent sweep did not report kept Codex-only function"
agents_resolve ordinary.work
[[ "$AGENT_FAMILY" == "codex" && "$AGENT_MODEL" == "gpt-test" && "$AGENT_EFFORT" == "high" ]] \
    || fail "agent sweep did not switch ordinary"
agents_resolve production.director
[[ "$AGENT_FAMILY" == "claude" && "$AGENT_MODEL" == "opus" && "$AGENT_EFFORT" == "xhigh" ]] \
    || fail "agent sweep changed production director"
agents_resolve codex_only.work
[[ "$AGENT_FAMILY" == "codex" && "$AGENT_MODEL" == "gpt-old" && "$AGENT_EFFORT" == "high" ]] \
    || fail "agent sweep changed Codex-only row"

write_fixture "$TEST_DIR/production.conf"
assert_production_refusal production codex
assert_production_refusal production gpt-test
assert_production_refusal production.director gpt-test:xhigh

admin_output="$(admin production.director sonnet:xhigh)"
[[ "$admin_output" == *"# updated [production.claude] production.director — live"* ]] \
    || fail "director row edit did not report a live Claude row"
agents_resolve production.director
[[ "$AGENT_FAMILY" == "claude" && "$AGENT_MODEL" == "sonnet" && "$AGENT_EFFORT" == "xhigh" ]] \
    || fail "director row edit did not resolve sonnet:xhigh"

echo "agents_config tests passed"
