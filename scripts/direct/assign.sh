#!/usr/bin/env bash
# Submit the assignment. The turn hook records completion.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/assign.py" "$@"
