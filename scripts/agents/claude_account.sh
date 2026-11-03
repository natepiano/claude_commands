#!/usr/bin/env bash
# Source this before a Claude launch; keep account policy in accounts.py.
claude_launch_environment() {
    local scripts_dir config_dir
    scripts_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || return 1
    config_dir="$("$scripts_dir/lib/py" - "$scripts_dir/.." "${1:-}" <<'PYTHON'
import os
from pathlib import Path
from datetime import datetime, timezone
import sys
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from scripts.accounts import accounts
try:
    if sys.argv[2] == "quota":
        selected = accounts.claude_account_with_room(datetime.now(timezone.utc))
        print(f"Claude account {selected.label}", file=sys.stderr)
    else:
        selected = accounts.account_for_unscoped_launch(os.environ, workers=sys.argv[2] == "workers")
except accounts.AccountsError as error:
    print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(1)
print("" if isinstance(selected, accounts.LegacyHomeLaunch) else selected.dir)
PYTHON
)" || return 1
    if [[ -n "$config_dir" ]]; then
        export CLAUDE_CONFIG_DIR="$config_dir"
        export CLAUDE_CODE_REMOTE_MEMORY_DIR="$HOME/.claude"
    else
        unset CLAUDE_CONFIG_DIR CLAUDE_CODE_REMOTE_MEMORY_DIR
    fi
    unset CLAUDE_CODE_OAUTH_TOKEN
}
