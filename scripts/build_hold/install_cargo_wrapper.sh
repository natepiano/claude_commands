#!/usr/bin/env bash
set -euo pipefail

if [[ "$OSTYPE" != linux* ]]; then
    printf 'cargo wrapper is natedev only\n'
    exit 0
fi

installer=$(readlink -f -- "${BASH_SOURCE[0]}")
wrapper=${installer%/*}/cargo
target=$HOME/.local/bin/cargo
if [[ -L "$target" && "$target" -ef "$wrapper" ]]; then
    exit 0
fi
if [[ -e "$target" || -L "$target" ]]; then
    printf 'cargo wrapper: refusing to replace %s\n' "$target" >&2
    exit 1
fi
mkdir -p -- "${target%/*}"
ln -s -- "$wrapper" "$target"
