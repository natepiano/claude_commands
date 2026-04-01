#!/bin/bash

cache_root() {
    if [[ -n "${XDG_CACHE_HOME:-}" ]]; then
        printf '%s\n' "$XDG_CACHE_HOME/cargo-port"
    elif [[ -n "${LOCALAPPDATA:-}" ]]; then
        printf '%s\n' "$LOCALAPPDATA/cargo-port"
    elif [[ "$OSTYPE" == darwin* ]]; then
        printf '%s\n' "$HOME/Library/Caches/cargo-port"
    else
        printf '%s\n' "$HOME/.cache/cargo-port"
    fi
}
