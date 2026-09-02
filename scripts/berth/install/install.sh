#!/usr/bin/env bash

# Refresh the installed cargo-berth engine as one recoverable operation.

set -u

step='argument validation'
repository_path=${1-}
if [[ -z $repository_path || ! -f $repository_path/Cargo.toml ]]; then
    printf 'cargo-berth install failed during %s: pass the cargo-liner repository path\n' "$step" >&2
    exit 1
fi
repository_path=$(cd -- "$repository_path" && pwd -P) || {
    printf 'cargo-berth install failed during %s: repository path is inaccessible\n' "$step" >&2
    exit 1
}

binary_directory=${CARGO_HOME:-$HOME/.cargo}/bin
binary_path=$binary_directory/cargo-berth

case :$PATH: in
    *:$binary_directory:*) ;;
    *)
        printf 'cargo-berth install failed during PATH validation: %s is not on PATH\n' "$binary_directory" >&2
        exit 1
        ;;
esac
mkdir -p -- "$binary_directory" || {
    printf 'cargo-berth install failed during PATH preparation: cannot create %s\n' "$binary_directory" >&2
    exit 1
}

staging_directory=$(mktemp -d "${TMPDIR:-/tmp}/cargo-berth-install.XXXXXX") || {
    printf 'cargo-berth install failed during staging-directory creation\n' >&2
    exit 1
}
cleanup_staging() {
    rm -rf -- "$staging_directory"
}
trap cleanup_staging EXIT HUP INT TERM

binary_existed=0
if [[ -f $binary_path ]]; then
    binary_existed=1
    cp -p -- "$binary_path" "$staging_directory/previous-cargo-berth" || {
        printf 'cargo-berth install failed during existing-engine backup\n' >&2
        exit 1
    }
fi

installed=0
binary_publication_state=Untouched
rollback_installation() {
    if [[ $installed -eq 0 && $binary_publication_state == ReplacementStarted ]]; then
        if [[ $binary_existed -eq 1 && -f $staging_directory/previous-cargo-berth ]]; then
            cp -p -- "$staging_directory/previous-cargo-berth" "$binary_path" 2>/dev/null || true
        elif [[ $binary_existed -eq 0 ]]; then
            rm -f -- "$binary_path"
        fi
    fi
    rm -rf -- "$staging_directory"
}
trap rollback_installation EXIT HUP INT TERM

step='engine build'
if ! CARGO_TARGET_DIR=$staging_directory/target cargo +stable build \
    --release \
    --manifest-path "$repository_path/Cargo.toml" \
    -p cargo-berth; then
    printf 'cargo-berth install failed during %s\n' "$step" >&2
    exit 1
fi

step='engine publication'
binary_publication_state=ReplacementStarted
if ! install -S -m 755 "$staging_directory/target/release/cargo-berth" "$binary_path"; then
    printf 'cargo-berth install failed during %s: engine publication failed\n' "$step" >&2
    exit 1
fi

installed=1
printf 'Installed cargo-berth from %s\n' "$repository_path"
