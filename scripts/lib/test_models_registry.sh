#!/usr/bin/env bash
# Source this from shell tests; arguments are path, then section/key=value rows.
# --replace-section assignments makes the supplied assignment rows exhaustive,
# matching Python's {"assignments": ExactRows({...})} override.
TEST_MODELS_REGISTRY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
write_test_registry() {
    MODELS_CONFIG_PUBLISH=0 "$TEST_MODELS_REGISTRY_DIR/py" "$TEST_MODELS_REGISTRY_DIR/test_models_registry.py" "$@"
}

registry_read_probe() {
    eval "$("$TEST_MODELS_REGISTRY_DIR/py" - "$TEST_MODELS_REGISTRY_DIR/../.." "$1" "$2" <<'PYTHON'
import os, shlex, sys
from pathlib import Path
from scripts.lib.test_models_registry import RegistryReadProbe
environment = RegistryReadProbe(Path(sys.argv[2]), Path(sys.argv[3])).environment(os.environ)
for name in ("TEST_REGISTRY_PATH", "TEST_REGISTRY_READS", "PYTHONPATH"):
    print(f"export {name}={shlex.quote(environment[name])}")
PYTHON
    )"
}

# Compare complete registry records without depending on comment or section order.
registry_records() {
    awk '
        { sub(/#.*/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, "") }
        /^\[/ { section = $0; next }
        NF { print section " " $0 }
    ' | LC_ALL=C sort
}
