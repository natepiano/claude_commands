#!/usr/bin/env bash
# Keep the launcher pid for the whole dispatch and its allocation reservation.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/../lib/py" "${SCRIPT_DIR}/assign.py" "$@"
