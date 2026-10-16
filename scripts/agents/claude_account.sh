#!/usr/bin/env bash
# Source this before a Claude launch; keep account policy in accounts.py.
claude_launch_environment() {
    local scripts_dir config_dir
    scripts_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || return 1
    config_dir="$("$scripts_dir/lib/py" - "$scripts_dir/.." <<'PYTHON'
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from scripts.accounts import accounts
selected = accounts.account_for_unscoped_launch(os.environ)
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
