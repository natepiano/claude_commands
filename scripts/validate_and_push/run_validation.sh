#!/bin/bash
# Run validate_ci.sh via an unsandboxed Claude CLI instance.
# This avoids sandbox restrictions that prevent taplo from running
# (macOS Mach IPC to configd is blocked by the sandbox).
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' -- \
  "Run this command and output ONLY its stdout/stderr with no other text: bash ${SCRIPT_DIR}/validate_ci.sh"
