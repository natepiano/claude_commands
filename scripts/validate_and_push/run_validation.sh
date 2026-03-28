#!/bin/bash
# Run the local CI validation script.
# IMPORTANT: This script must be invoked with dangerouslyDisableSandbox: true
# because taplo panics under the macOS Mach IPC sandbox restrictions.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "${SCRIPT_DIR}/validate_ci.sh"
