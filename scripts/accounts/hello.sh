#!/usr/bin/env bash

# Start a throwaway probe session on one Claude account, send "hello",
# print the reply and exit with the session's status.

set -euo pipefail

if [ "$#" -ne 1 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    echo "usage: hello.sh <claudeN>" >&2
    exit 2
fi

account="$1"

cd /tmp
echo "hello on $account:"
exec timeout 120 "$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/accounts/accounts.py" \
    exec --probe "$account" -p hello
