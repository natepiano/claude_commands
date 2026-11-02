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
if sys.argv[2] == "quota":
    selected = accounts.pick("claude", datetime.now(timezone.utc))
    if isinstance(selected, accounts.NoAccountWithRoom):
        print(accounts.no_room_line("claude", selected))
        raise SystemExit(1)
    prepared = accounts.prepared_account(selected.label, resume=False)
    if isinstance(prepared, accounts.AccountRefused):
        print(prepared.reason)
        raise SystemExit(1)
    print(f"simplify: Claude account {selected.label}", file=sys.stderr)
else:
    selected = accounts.account_for_unscoped_launch(os.environ, workers=sys.argv[2] == "workers")
print("" if isinstance(selected, accounts.LegacyHomeLaunch) else selected.dir)
PYTHON
)" || { [[ "${1:-}" != quota ]] || printf '%s\n' "$config_dir"; return 1; }
    if [[ -n "$config_dir" ]]; then
        export CLAUDE_CONFIG_DIR="$config_dir"
        export CLAUDE_CODE_REMOTE_MEMORY_DIR="$HOME/.claude"
    else
        unset CLAUDE_CONFIG_DIR CLAUDE_CODE_REMOTE_MEMORY_DIR
    fi
    unset CLAUDE_CODE_OAUTH_TOKEN
    [[ "${1:-}" != quota ]] || printf '%s\n' "$config_dir"
}
