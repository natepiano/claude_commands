#!/usr/bin/env bash
# Source this from shell tests; arguments are path, then section/key=value rows.
# --replace-section assignments makes the supplied assignment rows exhaustive,
# matching Python's {"assignments": ExactRows({...})} override.
TEST_MODELS_REGISTRY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
write_test_registry() {
    MODELS_CONFIG_PUBLISH=0 "$TEST_MODELS_REGISTRY_DIR/py" "$TEST_MODELS_REGISTRY_DIR/test_models_registry.py" "$@"
}

# Compare complete registry records without depending on comment or section order.
registry_records() {
    awk '
        { sub(/#.*/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, "") }
        /^\[/ { section = $0; next }
        NF { print section " " $0 }
    ' | LC_ALL=C sort
}
