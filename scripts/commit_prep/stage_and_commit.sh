#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  stage_and_commit.sh /path/to/commit-message.txt
  printf 'title\n\nbody\n' | stage_and_commit.sh

Stages all current changes with `git add -A` and commits using the provided
commit message.
EOF
}

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "Error: Current directory is not a git repository" >&2
    exit 1
fi

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

if [[ $# -gt 1 ]]; then
    usage >&2
    exit 2
fi

cleanup() {
    if [[ -n "${tmp_message_file:-}" && -f "${tmp_message_file}" ]]; then
        rm -f "${tmp_message_file}"
    fi
}
trap cleanup EXIT

repo_root="$(git rev-parse --show-toplevel)"

if [[ $# -eq 1 ]]; then
    message_file="$1"
    if [[ ! -f "$message_file" ]]; then
        echo "Error: Commit message file not found: $message_file" >&2
        exit 1
    fi

    case "$(cd "$(dirname "$message_file")" && pwd)/$(basename "$message_file")" in
        "$repo_root"/*)
            echo "Error: Commit message file must live outside the repository" >&2
            echo "Use a system temp file (for example from mktemp) or pipe the message on stdin." >&2
            exit 1
            ;;
    esac
else
    tmp_message_file="$(mktemp)"
    cat >"$tmp_message_file"
    message_file="$tmp_message_file"
fi

if [[ ! -s "$message_file" ]]; then
    echo "Error: Commit message is empty" >&2
    exit 1
fi

git add -A
git commit -F "$message_file"
