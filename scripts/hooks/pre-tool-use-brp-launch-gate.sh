#!/usr/bin/env bash
# Admit a BRP launch through the same memory gate as build steps.

session_id="$(python3 -c 'import json,sys; value=json.load(sys.stdin).get("session_id", ""); print(value if isinstance(value, str) else "")' 2>/dev/null)" || session_id=""
export CLAUDE_CODE_SESSION_ID="$session_id"
source "$(dirname "${BASH_SOURCE[0]}")/../lint/memory_gate.sh"
buildlog_wait_for_memory || true
build_hold_mark MemoryGateReturned "${BUILDLOG_MEM_OUTCOME:-MeminfoUnavailable}"
exit 0
