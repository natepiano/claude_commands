#!/usr/bin/env bash

# Reconcile cargo-berth reservations when a session starts.
#
# This wrapper decides one thing: whether the engine can be reached. Everything a
# user reads is the engine's own text, delivered by `exec` so stdout, stderr, and
# the exit status pass through byte for byte. A front end that rebuilt any of that
# from typed facts is what let a newer engine and an older front end disagree
# about what happened.

set -u

# A SessionStart hook has nothing to refuse, so an unreachable engine is stated
# rather than enforced. The literal below carries no interpolated value, so it is
# written directly instead of through jq, which keeps this notice readable when
# nothing else on the path is.
if ! command -v cargo-berth >/dev/null 2>&1; then
    printf '%s\n' '{"systemMessage":"cargo-berth hook installation needs repair.","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"The cargo-berth binary is not on PATH, so reservations were not reconciled. Install cargo-berth before relying on SessionStart reconciliation."}}'
    exit 0
fi

exec cargo-berth hook session-start
