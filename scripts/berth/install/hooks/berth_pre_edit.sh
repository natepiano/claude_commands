#!/usr/bin/env bash

# Authorize Claude Code file-writing tools against cargo-berth's exact-file check.
# Bash writes are deliberately outside this hook and are observed after the fact.
#
# This wrapper decides one thing: whether the engine can be reached. Everything a
# user reads is the engine's own text, delivered by `exec` so stdout, stderr, and
# the exit status pass through byte for byte. A front end that rebuilt any of that
# from typed facts is what let a newer engine and an older front end disagree
# about what happened.

set -u

# Refusing is the failure mode this hook has always taken when it could not reach
# a working installation, and it is the safe direction: an unchecked edit can
# collide with a foreign reservation, where a refused one only costs a retry.
if ! command -v cargo-berth >/dev/null 2>&1; then
    printf 'cargo-berth refused this edit hook request: %s\n' \
        'the cargo-berth binary is not on PATH; install it before editing reserved paths' >&2
    exit 2
fi

exec cargo-berth hook pre-tool-use
