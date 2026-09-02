#!/usr/bin/env bash

# Report cargo-berth drift for a Bash call after the fact.
#
# This wrapper decides one thing: whether the engine can be reached. Everything a
# user reads is the engine's own text, delivered by `exec` so stdout, stderr, and
# the exit status pass through byte for byte. A front end that rebuilt any of that
# from typed facts is what let a newer engine and an older front end disagree
# about what happened.

set -u

# A PostToolUse hook cannot refuse the call it is reporting on, so an unreachable
# engine is stated rather than enforced. The literal below carries no interpolated
# value, so it is written directly instead of through jq, which keeps this notice
# readable when nothing else on the path is.
if ! command -v cargo-berth >/dev/null 2>&1; then
    printf '%s\n' '{"continue":true,"systemMessage":"cargo-berth hook installation needs repair.","hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"STOP: the cargo-berth binary is not on PATH, so this Bash call was not inspected. Install cargo-berth before relying on drift detection."}}'
    exit 0
fi

exec cargo-berth hook post-tool-use
