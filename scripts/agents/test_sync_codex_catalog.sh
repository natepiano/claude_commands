#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../lib/test_agents_registry.sh"
SYNC_SCRIPT="$SCRIPT_DIR/sync_codex_catalog.sh"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sync-codex-catalog.XXXXXX")"
# The unwritable-state-directory case chmods a directory read-only, and rm needs
# it writable again to clear the tree.
trap 'chmod -R u+rwx "$TEST_DIR" 2>/dev/null; rm -rf "$TEST_DIR"' EXIT

AGENTS="$TEST_DIR/agents.conf"
export AGENTS_CONFIG_PUBLISH=0
CODEX_CONFIG="$TEST_DIR/config.toml"
MODELS_CACHE="$TEST_DIR/models_cache.json"
SYNC_STATE="$TEST_DIR/state/last_success"
STDERR_FILE="$TEST_DIR/stderr"
FAKE_BIN="$TEST_DIR/bin"

mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/claude" <<'EOF'
#!/usr/bin/env bash
if [[ "${CLAUDE_HELP_MODE:-aliases}" == "aliases" ]]; then
    cat <<'HELP'
Usage: claude [options]
  --model <model>  Model for the session. Use an alias (e.g.
                   'fable', 'opus', or 'sonnet') or a full name.
  --print          Print response and exit.
HELP
else
    echo 'Usage: claude [options]'
    echo '  --print  Print response and exit.'
fi
EOF
chmod +x "$FAKE_BIN/claude"

fail() {
    echo "$1" >&2
    exit 1
}

run_sync() {
    PATH="$FAKE_BIN:$PATH" \
    AGENTS_CONFIG_FILE="$AGENTS" \
    CODEX_CONFIG_FILE="$CODEX_CONFIG" \
    CODEX_MODELS_CACHE_FILE="$MODELS_CACHE" \
    CODEX_CATALOG_SYNC_STATE_FILE="${SYNC_STATE_OVERRIDE:-$SYNC_STATE}" \
        "$SYNC_SCRIPT" "$@"
}

assert_contains() {
    local text="$1" expected="$2" description="$3"
    [[ "$text" == *"$expected"* ]] || fail "$description"
}

effective_registry() {
    cat "$AGENTS"
}


test_model_families() (
    AGENTS="$TEST_DIR/families.conf"
    CODEX_CONFIG="$TEST_DIR/families.toml"
    MODELS_CACHE="$TEST_DIR/families.json"
    SYNC_STATE="$TEST_DIR/family-state/last_success"
    write_test_registry "$AGENTS" --replace-provided-sections \
        assignments/direct=codex \
        direct.codex/impl=sol:xhigh \
        direct.codex/review=astra:high \
        codex.agents/gpt-9-sol=high \
        codex.agents/gpt-6.9-sol=high,xhigh \
        codex.agents/gpt-6.10-sol=high,xhigh \
        codex.agents/gpt-6-astra=high \
        codex.agents/gpt-6.1-terra=high \
        codex.agents/gpt-6-luna=high \
        codex.families/astra=gpt-6-astra \
        codex.families/luna=gpt-6-luna \
        codex.families/sol=gpt-6.9-sol \
        codex.families/terra=gpt-6.1-terra \
        claude.agents/fable=low,medium,high,xhigh,max \
        claude.agents/opus=low,medium,high,xhigh,max \
        claude.agents/sonnet=low,medium,high,xhigh,max \
        claude.agents/haiku=low,medium,high,xhigh,max \
        claude.families/fable=fable \
        claude.families/opus=opus \
        claude.families/sonnet=sonnet \
        claude.families/haiku=haiku
    printf '%s\n' 'model = "gpt-9-sol"' > "$CODEX_CONFIG"
    cat > "$MODELS_CACHE" <<'EOF'
{"models":[
  {"slug":"gpt-6.9-sol","visibility":"list","supported_reasoning_levels":[{"effort":"high"},{"effort":"xhigh"}]},
  {"slug":"gpt-6.10-sol","visibility":"list","supported_reasoning_levels":[{"effort":"high"},{"effort":"xhigh"}]},
  {"slug":"gpt-9-sol","visibility":"hide","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-6-astra","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-6.1-terra","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-6-luna","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]}
]}
EOF
    cp "$AGENTS" "$TEST_DIR/families-before.conf"
    if run_sync --check >/dev/null 2>"$STDERR_FILE"; then
        fail "--check accepted a stale [codex.families] with a current agent catalog"
    fi
    cmp "$TEST_DIR/families-before.conf" "$AGENTS" \
        || fail "family --check changed the committed fixture"

    run_sync >/dev/null 2>"$STDERR_FILE" \
        || fail "sync rejected assignments that use family words: $(<"$STDERR_FILE")"
    [[ "$(<"$STDERR_FILE")" != *"is assigned to"* ]] \
        || fail "sync treated a family assignment as a retired model"
    effective_registry > "$TEST_DIR/families-view.conf"
    family_rows="$(awk '/^\[/ { in_section = ($0 == "[codex.families]"); next }
        in_section && /^[a-z]+=/{ print }' "$TEST_DIR/families-view.conf" | sort)"
    [[ "$family_rows" == $'astra=gpt-6-astra\nluna=gpt-6-luna\nsol=gpt-6.10-sol\nterra=gpt-6.1-terra' ]] \
        || fail "generated families did not choose the newest visible numeric versions: $family_rows"
    [[ "$(grep -c '^\[codex.families\]$' "$TEST_DIR/families-view.conf")" -eq 1 ]] \
        || fail "sync duplicated the generated family section"
    grep -q '^sol=gpt-6.10-sol$' "$AGENTS" \
        || fail "family sync did not update the shared registry"
    [[ -f "$SYNC_STATE" ]] || fail "complete family sync did not close the freshness gate"
    run_sync --check >/dev/null 2>"$STDERR_FILE" \
        || fail "--check rejected the synced family catalog: $(<"$STDERR_FILE")"

    # A new release changes both the generated family target and agent catalog.
    # Re-check reads the same shared registry as the launch resolver.
    cat > "$MODELS_CACHE" <<'EOF'
{"models":[
  {"slug":"gpt-6.9-sol","visibility":"list","supported_reasoning_levels":[{"effort":"high"},{"effort":"xhigh"}]},
  {"slug":"gpt-6.10-sol","visibility":"list","supported_reasoning_levels":[{"effort":"high"},{"effort":"xhigh"}]},
  {"slug":"gpt-9-sol","visibility":"hide","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-6-astra","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-6.1-terra","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-6-luna","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-7-sol","visibility":"list","supported_reasoning_levels":[{"effort":"medium"},{"effort":"high"},{"effort":"xhigh"}]}
]}
EOF
    cp "$AGENTS" "$TEST_DIR/families-synced-before.conf"
    if run_sync --check >/dev/null 2>"$STDERR_FILE"; then
        fail "--check accepted the catalog after a new family release appeared"
    fi
    cmp "$TEST_DIR/families-synced-before.conf" "$AGENTS" \
        || fail "stale-release --check wrote the registry"
    run_sync >/dev/null 2>"$STDERR_FILE" \
        || fail "sync failed after a new family release: $(<"$STDERR_FILE")"
    effective_registry > "$TEST_DIR/families-view.conf"
    grep -q '^sol=gpt-7-sol$' "$TEST_DIR/families-view.conf" \
        || fail "new release did not advance the generated family target"
    grep -q '^gpt-7-sol=medium,high,xhigh$' "$TEST_DIR/families-view.conf" \
        || fail "new release was omitted from the generated agent catalog"
    run_sync --check >/dev/null 2>"$STDERR_FILE" \
        || fail "--check was not clean after syncing the new release: $(<"$STDERR_FILE")"
    echo "sync_codex_catalog model-family tests passed"
)

test_model_families

test_family_repairs() (
    AGENTS="$TEST_DIR/family-repairs.conf"
    CODEX_CONFIG="$TEST_DIR/family-repairs.toml"
    MODELS_CACHE="$TEST_DIR/family-repairs.json"
    SYNC_STATE="$TEST_DIR/family-repairs-state/last_success"
    write_test_registry "$AGENTS" --replace-provided-sections \
        assignments/direct=codex \
        direct.codex/impl=sol:high \
        codex.agents/gpt-6.1-sol=high \
        codex.agents/gpt-6-luna=high \
        codex.families/luna=gpt-6-luna \
        codex.families/sol=gpt-6.1-sol \
        claude.agents/fable=low,medium,high,xhigh,max \
        claude.agents/opus=low,medium,high,xhigh,max \
        claude.agents/sonnet=low,medium,high,xhigh,max \
        claude.agents/haiku=low,medium,high,xhigh,max
    # A hidden selected slug remains usable explicitly, but cannot supply a
    # family alias. A visible release without efforts cannot supply one either.
    printf '%s\n' 'model = "gpt-6-luna"' > "$CODEX_CONFIG"
    cat > "$MODELS_CACHE" <<'EOF'
{"models":[
  {"slug":"gpt-6.1-sol","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-7-sol","visibility":"list"},
  {"slug":"gpt-6-luna","visibility":"hide","supported_reasoning_levels":[{"effort":"high"}]}
]}
EOF
    run_sync >/dev/null 2>"$STDERR_FILE" \
        || fail "sync failed with an incomplete newest release: $(<"$STDERR_FILE")"
    effective_registry > "$TEST_DIR/family-repairs-view.conf"
    family_rows="$(awk '/^\[/ { families = ($0 == "[codex.families]"); next }
        families && /^[a-z]+=/{ print }' "$TEST_DIR/family-repairs-view.conf")"
    [[ "$(printf '%s\n' "$family_rows" | sort)" == $'luna=gpt-6-luna\nsol=gpt-6.1-sol' ]] \
        || fail "incomplete newest release changed the usable aliases or removed a known one: $family_rows"
    grep -q '^gpt-6-luna=high$' "$TEST_DIR/family-repairs-view.conf" \
        || fail "excluding a hidden family also removed the selected explicit model"
    if grep -q '^gpt-7-sol=' "$TEST_DIR/family-repairs-view.conf"; then
        fail "sync invented efforts for the incomplete newest release"
    fi
    [[ ! -f "$SYNC_STATE" ]] || fail "incomplete family sync closed the freshness gate"
    run_sync --check >/dev/null 2>"$STDERR_FILE" \
        || fail "usable fallback did not produce a current catalog: $(<"$STDERR_FILE")"

    cp "$AGENTS" "$TEST_DIR/family-tie-base.conf"

    # Once a release's efforts have been learned, an incomplete refresh retains
    # that usable catalog row and may keep selecting it for its family.
    cat > "$MODELS_CACHE" <<'EOF'
{"models":[
  {"slug":"gpt-6.1-sol","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-7-sol","visibility":"list","supported_reasoning_levels":[{"effort":"high"},{"effort":"xhigh"}]},
  {"slug":"gpt-6-luna","visibility":"hide","supported_reasoning_levels":[{"effort":"high"}]}
]}
EOF
    run_sync >/dev/null 2>"$STDERR_FILE" \
        || fail "sync could not learn the new release's efforts: $(<"$STDERR_FILE")"
    [[ -f "$SYNC_STATE" ]] || fail "complete family catalog did not record freshness"
    jq 'del(.models[] | select(.slug == "gpt-7-sol") | .supported_reasoning_levels)' \
        "$MODELS_CACHE" > "$TEST_DIR/family-incomplete.json"
    mv "$TEST_DIR/family-incomplete.json" "$MODELS_CACHE"
    rm "$SYNC_STATE"
    run_sync >/dev/null 2>"$STDERR_FILE" \
        || fail "sync rejected the last known efforts for a family: $(<"$STDERR_FILE")"
    assert_contains "$(<"$STDERR_FILE")" "keeping its last known efforts" \
        "family refresh did not exercise the incomplete-cache retention path"
    effective_registry > "$TEST_DIR/family-repairs-view.conf"
    grep -q '^sol=gpt-7-sol$' "$TEST_DIR/family-repairs-view.conf" \
        || fail "incomplete cache discarded a family target with last known efforts"
    grep -q '^gpt-7-sol=high,xhigh$' "$TEST_DIR/family-repairs-view.conf" \
        || fail "family sync did not retain the model's last known efforts"
    [[ ! -f "$SYNC_STATE" ]] || fail "retained efforts incorrectly marked the cache complete"

    # Use an independent history: additive sync must retain an already learned gpt-7 target.
    cp "$TEST_DIR/family-tie-base.conf" "$AGENTS"

    # Numeric-equivalent spellings must produce the same generated mapping in
    # either input order. The check also ensures a permutation is already clean.
    for slug_order in 'gpt-6.1-sol gpt-6.01-sol' 'gpt-6.01-sol gpt-6.1-sol'; do
        read -r first_slug second_slug <<< "$slug_order"
        printf '{"models":[{"slug":"%s","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]},{"slug":"%s","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]}]}\n' \
            "$first_slug" "$second_slug" > "$MODELS_CACHE"
        if [[ "$first_slug" == gpt-6.01-sol ]]; then
            run_sync --check >/dev/null 2>"$STDERR_FILE" \
                || fail "permuting equal-version candidates made the catalog stale: $(<"$STDERR_FILE")"
        fi
        run_sync >/dev/null 2>"$STDERR_FILE" \
            || fail "sync rejected equal-version family candidates: $(<"$STDERR_FILE")"
        grep -q '^sol=gpt-6.1-sol$' <(effective_registry) \
            || fail "generated family version tie depended on cache order: $slug_order"
    done
    grep -q '^impl=sol:high$' "$AGENTS" || fail "family repairs changed the assignment"
    echo "sync_codex_catalog family repair tests passed"
)

test_family_repairs

legacy_rows=(--bare-row unmanaged.models/gpt-legacy
             --bare-row unmanaged.efforts/medium --bare-row unmanaged.efforts/high)
write_test_registry "$AGENTS" "${legacy_rows[@]}" --replace-provided-sections --replace-section codex.families \
    unmanaged.defaults/model=gpt-legacy \
    unmanaged.defaults/effort=medium \
    assignments/direct=codex \
    direct.codex/impl=gpt-new:high \
    codex.agents/gpt-legacy=medium \
    codex.agents/gpt-new=low,medium,high \
    claude.agents/fable=low,medium,high,max \
    claude.agents/sonnet=low,medium,high,max
cp "$AGENTS" "$TEST_DIR/legacy-before.conf"

printf '%s\n' 'model = "gpt-selected"' 'model_reasoning_effort = "high"' > "$CODEX_CONFIG"
cat > "$MODELS_CACHE" <<'EOF'
{"models":[
  {"slug":"gpt-new","visibility":"list","supported_reasoning_levels":[{"effort":"medium"},{"effort":"high"},{"effort":"xhigh"}]},
  {"slug":"gpt-empty","visibility":"list"},
  {"slug":"bad:slug","visibility":"list","supported_reasoning_levels":[{"effort":"high"}]},
  {"slug":"gpt-selected","visibility":"hide","supported_reasoning_levels":[{"effort":"low"},{"effort":"high"}]},
  {"slug":"internal","visibility":"hide"}
]}
EOF

before_check="$TEST_DIR/before-stale-check.conf"
cp "$AGENTS" "$before_check"
if run_sync --check >/dev/null 2>"$STDERR_FILE"; then
    fail "--check accepted a stale catalog"
fi
cmp "$before_check" "$AGENTS" || fail "--check changed a stale catalog"

if ! run_sync >/dev/null 2>"$STDERR_FILE"; then
    sed -n '1,120p' "$STDERR_FILE" >&2
    fail "initial catalog sync failed"
fi
stderr_text="$(<"$STDERR_FILE")"
assert_contains "$stderr_text" "skipping Codex model 'bad:slug'" \
    "colon-bearing slug did not produce a warning"
assert_contains "$stderr_text" "Claude model alias 'opus' is missing from [claude.agents]" \
    "missing Claude alias did not produce a warning"
assert_contains "$stderr_text" "Codex model 'gpt-empty' has no reasoning levels" \
    "effort-less visible model did not produce a warning"
assert_contains "$stderr_text" "leaving it out of [codex.agents]" \
    "effort-less visible model with no previous row was not left out"
[[ ! -f "$SYNC_STATE" ]] \
    || fail "an incomplete catalog closed the freshness gate"

expected="$TEST_DIR/expected.conf"
write_test_registry "$expected" "${legacy_rows[@]}" --replace-provided-sections --replace-section codex.families \
    unmanaged.defaults/model=gpt-legacy \
    unmanaged.defaults/effort=medium \
    assignments/direct=codex \
    direct.codex/impl=gpt-new:high \
    codex.agents/gpt-selected=low,high \
    codex.agents/gpt-legacy=medium \
    codex.agents/gpt-new=low,medium,high,xhigh \
    claude.agents/fable=low,medium,high,max \
    claude.agents/sonnet=low,medium,high,max
diff -u <(registry_records < "$expected") <(effective_registry | registry_records) || fail "sync wrote the wrong [codex.agents] output"

before_legacy="$TEST_DIR/legacy-prefix-before"
after_legacy="$TEST_DIR/legacy-prefix-after"
sed -n '1,/^\[codex.agents\]$/p' "$TEST_DIR/legacy-before.conf" | sed '$d' > "$before_legacy"
sed -n '1,/^\[codex.agents\]$/p' "$AGENTS" | sed '$d' > "$after_legacy"
cmp "$before_legacy" "$after_legacy" || fail "sync changed a legacy registry section"

run_sync --check >/dev/null 2>"$STDERR_FILE" \
    || fail "--check rejected a current catalog"

CLAUDE_HELP_MODE=unparseable run_sync --check >/dev/null 2>"$STDERR_FILE" \
    || fail "unparseable Claude help changed --check behavior"
stderr_text="$(<"$STDERR_FILE")"
[[ "$stderr_text" != *"Claude model alias"* ]] \
    || fail "unparseable Claude help produced an alias warning"

write_test_registry "$AGENTS" --replace-provided-sections \
    assignments/direct=codex \
    direct.codex/impl=gpt-new:high \
    codex.agents/gpt-new=high \
    claude.agents/fable=low,medium,high,max \
    claude.agents/opus=low,medium,high,max \
    claude.agents/sonnet=low,medium,high,max
printf '%s\n' 'model = "gpt-uncached"' > "$CODEX_CONFIG"
run_sync >/dev/null 2>"$STDERR_FILE" \
    || fail "sync required a legacy section"
stderr_text="$(<"$STDERR_FILE")"
[[ "$stderr_text" != *"Claude model alias"* && "$stderr_text" != *"is assigned to"* ]] \
    || fail "new-sections-only sync produced an unexpected registry warning"
if grep -q '^gpt-empty' <(effective_registry); then
    fail "model without reasoning levels was written to [codex.agents]"
fi
# A selected model absent from the cache is a settled fact, not a cache still
# being written: nothing will ever name its efforts, so it keeps an empty list
# and stays assignable at the agent CLI's own default.
grep -q '^gpt-uncached=$' <(effective_registry) || fail "uncached selected model did not get an empty effort list"
if grep -q '^bad:slug=' <(effective_registry); then
    fail "colon-bearing slug was written to [codex.agents]"
fi

write_test_registry "$AGENTS" --replace-provided-sections \
    assignments/direct=codex \
    direct.codex/impl=gpt-retired:high \
    direct.codex/test=gpt-retired:medium \
    codex.agents/gpt-retired=low,medium,high,xhigh \
    codex.agents/gpt-current=medium,high \
    claude.agents/fable=low,medium,high,max \
    claude.agents/opus=low,medium,high,max \
    claude.agents/sonnet=low,medium,high,max
printf '%s\n' 'model = "gpt-retired"' > "$CODEX_CONFIG"
cat > "$MODELS_CACHE" <<'EOF'
{"models":[
  {"slug":"gpt-current","visibility":"list","supported_reasoning_levels":[{"effort":"medium"},{"effort":"high"}]}
]}
EOF
run_sync >/dev/null 2>"$STDERR_FILE"
stderr_text="$(<"$STDERR_FILE")"
assert_contains "$stderr_text" "direct.impl is assigned to 'gpt-retired'" \
    "stale impl row warning did not name the row"
assert_contains "$stderr_text" "/agent direct.impl <agent>[:<effort>]" \
    "stale row warning did not name the task reconfiguration command"
assert_contains "$stderr_text" "/agent direct <family>" \
    "stale row warning did not name the family reconfiguration command"
assert_contains "$stderr_text" "direct.test is assigned to 'gpt-retired'" \
    "sync did not warn once for each stale assigned row"
[[ "$(grep -c "is assigned to 'gpt-retired'" "$STDERR_FILE")" -eq 2 ]] \
    || fail "sync emitted the wrong number of stale-row warnings"
[[ "$(grep -c '^gpt-retired=low,medium,high,xhigh$' <(effective_registry))" -eq 1 ]] \
    || fail "retired assigned agent's previous catalog row was not preserved exactly once"

write_test_registry "$AGENTS" --replace-provided-sections \
    assignments/direct=codex \
    direct.codex/impl=gpt-current:high \
    codex.agents/gpt-current=medium,high \
    claude.agents/fable=low,medium,high,max \
    claude.agents/opus=low,medium,high,max \
    claude.agents/sonnet=low,medium,high,max
printf '%s\n' 'model = "gpt-current"' > "$CODEX_CONFIG"
cat > "$MODELS_CACHE" <<'EOF'
{"models":[
  {"slug":"gpt-current","visibility":"list","supported_reasoning_levels":[]}
]}
EOF
rm -f "$SYNC_STATE"
run_sync >/dev/null 2>"$STDERR_FILE" || fail "a cache mid-write failed the sync outright"
stderr_text="$(<"$STDERR_FILE")"
assert_contains "$stderr_text" "keeping its last known efforts" \
    "a model whose efforts left the cache lost its previous row"
grep -q '^gpt-current=medium,high$' <(effective_registry) \
    || fail "previous effort list was not preserved across a cache mid-write"
[[ ! -f "$SYNC_STATE" ]] || fail "a preserved-row run closed the freshness gate"

# A peer holding the lock is the one failure that should exit 0: it will write
# the catalog and the state file itself.
mkdir -p "$(dirname "$SYNC_STATE")"
mkdir "$(dirname "$SYNC_STATE")/sync.lock"
run_sync >"$TEST_DIR/stdout" 2>"$STDERR_FILE" || fail "a held lock did not exit 0"
assert_contains "$(<"$TEST_DIR/stdout")" "already running" \
    "a held lock was not reported as contention"
rmdir "$(dirname "$SYNC_STATE")/sync.lock"

# A sandboxed caller can create nothing under the state directory. Reporting
# that as a busy peer exits 0, and agents_config.sh then uses whatever stale
# catalog is on disk with no warning -- so it has to be told apart from
# contention and reported as the failure it is.
readonly_state="$TEST_DIR/readonly-state"
mkdir -p "$readonly_state"
chmod 500 "$readonly_state"
SYNC_STATE_OVERRIDE="$readonly_state/last_success"
set +e
run_sync >/dev/null 2>"$STDERR_FILE"
lock_status=$?
set -e
assert_contains "$(<"$STDERR_FILE")" "cannot acquire the sync lock" \
    "an unwritable state directory was reported as a busy peer"
[[ "$lock_status" -ne 0 ]] || fail "an unwritable state directory exited 0"

SYNC_STATE_OVERRIDE="$readonly_state/nested/last_success"
set +e
run_sync >/dev/null 2>"$STDERR_FILE"
lock_status=$?
set -e
unset SYNC_STATE_OVERRIDE
chmod 700 "$readonly_state"
assert_contains "$(<"$STDERR_FILE")" "cannot create the sync state directory" \
    "an uncreatable state directory was not reported"
[[ "$lock_status" -ne 0 ]] || fail "an uncreatable state directory exited 0"

before="$TEST_DIR/before-invalid.conf"
cp "$AGENTS" "$before"
printf '%s\n' '{invalid' > "$MODELS_CACHE"
if run_sync >/dev/null 2>&1; then
    fail "invalid cache unexpectedly succeeded"
fi
cmp "$before" "$AGENTS" || fail "invalid cache changed the registry"

# Updating the shared catalog must keep the registry and its mode
# intact, without passing non-mode diagnostics from stat into chmod.
write_test_registry "$AGENTS" --replace-provided-sections \
    assignments/direct=codex \
    direct.codex/impl=gpt-current:high \
    codex.agents/gpt-stale=medium \
    claude.agents/fable=low,medium,high,max \
    claude.agents/sonnet=low,medium,high,max
printf '%s\n' 'model = "gpt-current"' > "$CODEX_CONFIG"
cat > "$MODELS_CACHE" <<'EOF'
{"models":[
  {"slug":"gpt-current","visibility":"list","supported_reasoning_levels":[{"effort":"medium"},{"effort":"high"}]}
]}
EOF
rm -f "$SYNC_STATE"
chmod 640 "$AGENTS"

file_mode() {
    local mode
    for mode in "$(stat -c '%a' "$1" 2>/dev/null)" "$(stat -f '%Lp' "$1" 2>/dev/null)"; do
        if [[ "$mode" =~ ^[0-7]{3,4}$ ]]; then
            printf '%s\n' "$mode"
            return 0
        fi
    done
    printf 'unreadable\n'
}

run_sync >/dev/null 2>"$STDERR_FILE" || fail "sync failed while rewriting the catalog"
stderr_text="$(<"$STDERR_FILE")"
[[ "$stderr_text" != *chmod* ]] \
    || fail "sync passed something other than a mode to chmod: $stderr_text"
grep -q '^gpt-current=medium,high$' <(effective_registry) \
    || fail "sync did not rewrite the catalog, so mode preservation was never exercised"
[[ "$(file_mode "$AGENTS")" == 640 ]] \
    || fail "sync did not preserve the defaults' mode: $(file_mode "$AGENTS")"

echo "sync_codex_catalog tests passed"
