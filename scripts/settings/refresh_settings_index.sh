#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cat >/dev/null
git -C "$REPO_ROOT" add --refresh -- settings.json >/dev/null 2>&1 || true
