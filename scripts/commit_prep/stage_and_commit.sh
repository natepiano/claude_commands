#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  stage_and_commit.sh /path/to/commit-message.txt
  printf 'title\n\nbody\n' | stage_and_commit.sh
  printf '%s' "$body" | stage_and_commit.sh --title "feat: message"

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

cleanup() {
    if [[ -n "${tmp_message_file:-}" && -f "${tmp_message_file}" ]]; then
        rm -f "${tmp_message_file}"
    fi

    if [[ -n "${tmp_body_file:-}" && -f "${tmp_body_file}" ]]; then
        rm -f "${tmp_body_file}"
    fi
}
trap cleanup EXIT

repo_root="$(git rev-parse --show-toplevel)"

title=""
body_file=""
message_file=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --title)
            if [[ $# -lt 2 ]]; then
                echo "Error: --title requires a value" >&2
                exit 2
            fi

            title="$2"
            shift 2
            ;;
        --body-file)
            if [[ $# -lt 2 ]]; then
                echo "Error: --body-file requires a path" >&2
                exit 2
            fi

            body_file="$2"
            shift 2
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [[ -n "$message_file" || -n "$title" || -n "$body_file" ]]; then
                usage >&2
                exit 2
            fi

            message_file="$1"
            shift
            ;;
    esac
done

if [[ $# -gt 0 ]]; then
    usage >&2
    exit 2
fi

ensure_external_file() {
    local file_path=$1

    if [[ ! -f "$file_path" ]]; then
        echo "Error: Commit message file not found: $file_path" >&2
        exit 1
    fi

    case "$(cd "$(dirname "$file_path")" && pwd)/$(basename "$file_path")" in
        "$repo_root"/*)
            echo "Error: Commit message file must live outside the repository" >&2
            echo "Use a system temp file (for example from mktemp) or pipe the message on stdin." >&2
            exit 1
            ;;
    esac
}

if [[ -n "$message_file" ]]; then
    ensure_external_file "$message_file"
elif [[ -n "$title" ]]; then
    if [[ -n "$body_file" ]]; then
        ensure_external_file "$body_file"
    elif [[ ! -t 0 ]]; then
        tmp_body_file="$(mktemp "${TMPDIR:-/tmp}/commit-prep.XXXXXXXX")"
        cat >"$tmp_body_file"
        body_file="$tmp_body_file"
    fi

    tmp_message_file="$(mktemp "${TMPDIR:-/tmp}/commit-prep.XXXXXXXX")"
    {
        printf '%s\n' "$title"

        if [[ -n "$body_file" && -s "$body_file" ]]; then
            printf '\n'
            cat "$body_file"
        fi
    } >"$tmp_message_file"
    message_file="$tmp_message_file"
else
    tmp_message_file="$(mktemp "${TMPDIR:-/tmp}/commit-prep.XXXXXXXX")"
    cat >"$tmp_message_file"
    message_file="$tmp_message_file"
fi

if [[ ! -s "$message_file" ]]; then
    echo "Error: Commit message is empty" >&2
    exit 1
fi

git add -A
git commit -F "$message_file"
