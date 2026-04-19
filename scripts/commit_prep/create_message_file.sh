#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  create_message_file.sh /path/to/output.txt
  create_message_file.sh --stdout-path
  printf 'title\n\nbody\n' | create_message_file.sh --stdout-path
  printf '%s' "$body" | create_message_file.sh --stdout-path --title "feat: message"

Writes a commit message to an explicit output path or to a newly created system
temp file. When `--stdout-path` is used, the created file path is printed to
stdout after the message is written.
EOF
}

title=""
body_file=""
output_path=""
stdout_path=false

cleanup() {
    if [[ -n "${tmp_body_file:-}" && -f "${tmp_body_file}" ]]; then
        rm -f "${tmp_body_file}"
    fi
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
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
        --stdout-path)
            stdout_path=true
            shift
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
            if [[ -n "$output_path" ]]; then
                usage >&2
                exit 2
            fi
            output_path="$1"
            shift
            ;;
    esac
done

if [[ $# -gt 0 ]]; then
    usage >&2
    exit 2
fi

if [[ -n "$output_path" && "$stdout_path" == true ]]; then
    echo "Error: pass either an output path or --stdout-path, not both" >&2
    exit 2
fi

if [[ -n "$body_file" && ! -f "$body_file" ]]; then
    echo "Error: body file not found: $body_file" >&2
    exit 1
fi

if [[ -z "$output_path" ]]; then
    if [[ "$stdout_path" == true ]]; then
        output_path="$(mktemp "${TMPDIR:-/tmp}/commit-msg.XXXXXXXX")"
    else
        echo "Error: missing output path or --stdout-path" >&2
        exit 2
    fi
fi

if [[ -n "$title" ]]; then
    if [[ -z "$body_file" && ! -t 0 ]]; then
        tmp_body_file="$(mktemp "${TMPDIR:-/tmp}/commit-body.XXXXXXXX")"
        cat >"$tmp_body_file"
        body_file="$tmp_body_file"
    fi

    {
        printf '%s\n' "$title"
        if [[ -n "$body_file" && -s "$body_file" ]]; then
            printf '\n'
            cat "$body_file"
        fi
    } >"$output_path"
else
    cat >"$output_path"
fi

if [[ ! -s "$output_path" ]]; then
    echo "Error: commit message is empty" >&2
    exit 1
fi

if [[ "$stdout_path" == true ]]; then
    printf '%s\n' "$output_path"
fi
