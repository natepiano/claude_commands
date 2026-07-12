#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

cat > "$TEMP_DIR/agents.conf" <<'EOF'
[codex]
model=test-model
effort=

[codex.models]
test-model

[codex.efforts]
medium
high
xhigh
EOF

cat > "$TEMP_DIR/delegate.conf" <<'EOF'
[implementation]
agent=codex
effort=high

[review]
agent=codex
effort=high

[mechanical]
agent=codex
effort=medium

[escalation]
agent=codex
effort=xhigh

[invalid-effort]
agent=codex
effort=maximum

[invalid-agent]
agent=claude
effort=high
EOF

AGENTS_CONFIG_FILE="$TEMP_DIR/agents.conf"
DELEGATE_CONFIG_FILE="$TEMP_DIR/delegate.conf"
source "$SCRIPT_DIR/delegate_config.sh"

delegate_config_resolve implementation
[[ "$DELEGATE_MODEL" == "test-model" && "$DELEGATE_EFFORT" == "high" ]]
delegate_config_resolve review
[[ "$DELEGATE_EFFORT" == "high" ]]
delegate_config_resolve mechanical
[[ "$DELEGATE_EFFORT" == "medium" ]]
delegate_config_resolve escalation
[[ "$DELEGATE_EFFORT" == "xhigh" ]]

if delegate_config_resolve missing 2>/dev/null; then
    echo "missing profile unexpectedly resolved" >&2
    exit 1
fi
if delegate_config_resolve invalid-effort 2>/dev/null; then
    echo "invalid effort unexpectedly resolved" >&2
    exit 1
fi
if delegate_config_resolve invalid-agent 2>/dev/null; then
    echo "invalid agent unexpectedly resolved" >&2
    exit 1
fi

echo "delegate profile tests passed"
