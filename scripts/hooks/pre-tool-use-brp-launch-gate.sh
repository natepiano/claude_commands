#!/usr/bin/env bash
# Admit a BRP launch through the same memory gate as build steps.

session_id="$(python3 -c 'import json,sys; value=json.load(sys.stdin).get("session_id", ""); print(value if isinstance(value, str) else "")' 2>/dev/null)" || session_id=""
export CLAUDE_CODE_SESSION_ID="$session_id"
source "$(dirname "${BASH_SOURCE[0]}")/../lint/memory_gate.sh"
unset BUILDLOG_CALL_ID
admission=0
reason="$(python3 "$BUILD_TARGETS_SCRIPT" check-local)" || admission=$?
if [[ "$admission" != 0 ]]; then
    [[ -n "$reason" ]] || reason="natedev's hold state could not be read; not starting."
    python3 - "$reason" <<'PY_DENY'
import json
import sys
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
      "permissionDecision": "deny", "permissionDecisionReason": sys.argv[1]}}))
PY_DENY
    exit 0
fi
buildlog_wait_for_memory || true
exit 0
