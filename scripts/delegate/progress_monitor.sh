#!/usr/bin/env bash
# Compatibility entrypoint for the old per-dispatch monitor.
# Usage: progress_monitor.sh <session_dir> [impl|review]

set -euo pipefail

SESSION_DIR="${1:?Usage: progress_monitor.sh <session_dir> [impl|review]}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec bash "${SCRIPT_DIR}/reporter.sh" watch --session-dir "${SESSION_DIR}"
