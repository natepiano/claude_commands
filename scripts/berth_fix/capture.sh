#!/usr/bin/env bash

# Capture one cargo-berth failure into a report directory the fixer can read.
#
# The journal is append-only and every session keeps writing to it, so the state
# that produced a failure is gone minutes later. This runs before anything else
# the reporter does.

set -u

client=
while [ $# -gt 0 ]; do
    case $1 in
        --client) shift; client=${1-} ;;
        -h|--help) printf 'usage: capture.sh --client <reporting session name>\n'; exit 0 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done
[ -n "$client" ] || { printf -- '--client is required\n' >&2; exit 2; }

repository_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
    printf 'not inside a git repository\n' >&2
    exit 1
}
common_git_directory=$(git rev-parse --git-common-dir 2>/dev/null)
case $common_git_directory in
    /*) ;;
    *) common_git_directory=$repository_root/$common_git_directory ;;
esac
branch=$(git -C "$repository_root" branch --show-current 2>/dev/null)

slug=$(printf '%s' "$client" | tr -c 'A-Za-z0-9._-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')
report=$HOME/.claude/state/berth-fix/inbox/$(date -u '+%Y%m%dT%H%M%SZ')-${slug:-reporter}
mkdir -p "$report" || exit 1

engine=$(command -v cargo-berth 2>/dev/null)
engine_version=$(cargo-berth --version 2>&1 | head -1)
engine_modified=$([ -n "$engine" ] && date -u -r "$engine" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)
ledger=$common_git_directory/cargo-berth

REPORT_DIR=$report CLIENT=$client REPOSITORY=$repository_root COMMON_GIT=$common_git_directory \
BRANCH=$branch ENGINE=$engine ENGINE_VERSION=$engine_version ENGINE_MODIFIED=$engine_modified \
WORKING_DIRECTORY=$PWD LEDGER=$ledger python3 -c '
import json, os, subprocess

ledger = os.environ["LEDGER"]
journal = os.path.join(ledger, "journal.ndjson")
meta = {
    "captured_at": subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                                  capture_output=True, text=True).stdout.strip(),
    "client": os.environ["CLIENT"],
    "working_directory": os.environ["WORKING_DIRECTORY"],
    "repository_root": os.environ["REPOSITORY"],
    "common_git_directory": os.environ["COMMON_GIT"],
    "branch": os.environ["BRANCH"],
    "engine_path": os.environ["ENGINE"],
    "engine_version": os.environ["ENGINE_VERSION"],
    "engine_modified": os.environ["ENGINE_MODIFIED"],
    "ledger_present": os.path.isdir(ledger),
    "journal_bytes": os.path.getsize(journal) if os.path.exists(journal) else None,
}
with open(os.path.join(os.environ["REPORT_DIR"], "meta.json"), "w") as handle:
    json.dump(meta, handle, indent=2)
    handle.write("\n")
'

if [ -d "$ledger" ]; then
    tar -czf "$report/ledger-snapshot.tar.gz" -C "$(dirname "$ledger")" "$(basename "$ledger")" 2>/dev/null \
        || printf 'ledger snapshot incomplete\n' >&2
fi

printf '%s\n' "$report"
