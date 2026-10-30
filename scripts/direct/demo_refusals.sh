#!/usr/bin/env bash
# Show the five repair and launch refusals without using a live run.
set -euo pipefail

if (( $# )); then
    echo "demo_refusals.sh takes no arguments" >&2
    exit 2
fi
# A terminal may omit the installed shell tools and Git from PATH.
export PATH="$PATH:/run/current-system/sw/bin:/etc/profiles/per-user/${USER:-}/bin:/opt/homebrew/bin:/usr/local/bin"
tools_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
scratch=$(mktemp -d /tmp/direct-refusals.XXXXXXXX)
trap 'rm -rf -- "$scratch"' EXIT
step="scratch setup"
trap 'printf "FAILED: %s (exit %s)\n" "$step" "$?" >&2' ERR
mkdir -p "$scratch/home" "$scratch/history" "$scratch/ledger" "$scratch/run"
export HOME="$scratch/home"
export XDG_CACHE_HOME="$scratch/cache"
export UNIT_HISTORY_DIR="$scratch/history" PLAN_DELEGATE_HISTORY_DIR="$scratch/history"
export AGENTS_CONFIG_FILE="$scratch/agents.conf"
export AGENTS_CONFIG_PUBLISH=0
export UNIT_CONFIG="$scratch/direct.conf" PLAN_DELEGATE_CONFIG="$scratch/direct.conf"
unset CLAUDE_CODE_SESSION_ID
: > "$AGENTS_CONFIG_FILE"
# Fix the demo's thresholds so local tuning cannot change its examples.
cat > "$UNIT_CONFIG" <<'CONFIG'
MIN_REPAIR_BUDGET=3
REPAIR_ROUNDS_PER_FINDING=0.5
RUNAWAY_ROUNDS=5
MAX_FIX_ATTEMPTS=2
MAX_REOPENS=2
STALLED_ROUNDS=2
MAX_CONSECUTIVE_SAME_KIND_PASSES=3
MAX_REVIEW_CANCELLATIONS=1
ROUTING_FROM_ROUND=4
ASTRA_AFTER_FAILED_FIXES=2
CONFIG

F() { "$tools_dir/../lib/py" "$tools_dir/findings.py" "$@" --session-dir "$scratch/ledger"; }
P() { "$tools_dir/../lib/py" "$tools_dir/progress_history.py" "$@" --session-dir "$scratch/run"; }
setup() {
    step="$1"; shift
    local output
    if output=$("$@" 2>&1); then return; fi
    printf 'FAILED: %s\n%s\n' "$step" "$output" >&2
    exit 1
}
refuse() {
    step="$1"; shift
    local expected="$1" output code; shift
    printf '\n%s\n' "$step"
    if output=$("$@" 2>&1); then code=0; else code=$?; fi
    if [[ "$code" != 1 || "$output" != "$expected" ]]; then
        printf 'FAILED: %s: expected refusal exit 1 and text:\n%s\nGot exit %s:\n%s\n' \
            "$step" "$expected" "$code" "$output" >&2
        exit 1
    fi
    printf '%s\n' "$output"
}
passes() {
    # F and P add the scratch session; print the usable call after each refusal.
    printf 'Pass: '
    if [[ "$1" == F ]]; then printf 'findings.py '; else printf 'progress_history.py '; fi
    printf '%q ' "${@:2}"
    printf '\n'
    setup "$step: passing call" "$@"
    printf 'exit=0\n'
}

setup "open finding" F open --severity blocker --title "Demo finding" --caught-by main
refuse "1. Accepted checks are required" \
    '--accepted-checks is required: name affected accepted checks or "none: <why>"' \
    F dispatch --covers F001
passes F dispatch --covers F001 --accepted-checks "none: demo edits reach no accepted behavior"
setup "land first repair" F landed
setup "first failed fix" F verdict --id F001 --state still_open
setup "second repair" F dispatch --covers F001 --accepted-checks "none: demo"
setup "land second repair" F landed
setup "second failed fix" F verdict --id F001 --state still_open
printf 'F001: two fixes failed. Try a different approach.\n' > "$scratch/note.md"
refuse "2. Astra review is due after two failed fixes" \
    'Astra review due for F001; record it with findings.py astra' \
    F dispatch --covers F001 --accepted-checks "none: demo"
passes F astra --id F001 --note "$scratch/note.md" --approach "Use a different repair approach"
passes F dispatch --covers F001 --accepted-checks "none: demo"
setup "land third repair" F landed
setup "third failed fix" F verdict --id F001 --state still_open
setup "round four Astra record" F astra --id F001 --note "$scratch/note.md" --approach "Check the failure before editing"
refuse "3. Round four requires kept evidence" \
    'Missing --keep for F001: route it with `findings.py route` or give the kept category and its evidence' \
    F dispatch --covers F001 --accepted-checks "none: demo"
passes F dispatch --covers F001 --accepted-checks "none: demo" --keep "F001=broken:demo evidence"

printf 'Phase 1 — Demo phase\n' > "$scratch/phase.md"
printf 'Phase 9 — Some other phase\n' > "$scratch/stale.md"
setup "start recorded run" P start-run --working-dir "$scratch" \
    --main-family claude --main-model opus --main-effort high --main-session-id demo
setup "open demo phase" P start-phase --phase-id 1 --phase-title "Demo phase" --work-order-file "$scratch/phase.md"
refuse "4. A recorded launch requires its phase" \
    'Missing phase: --phase-id is required for a recorded run' \
    P check-launch --pass-kind impl
passes P check-launch --pass-kind impl --phase-id 1
refuse "5. The prompt identity must match the open phase" \
    'Stale prompt phase: Phase 9 — Some other phase; open phase is Phase 1 — Demo phase' \
    P check-launch --pass-kind impl --phase-id 1 --prompt-file "$scratch/stale.md"
passes P check-launch --pass-kind impl --phase-id 1 --prompt-file "$scratch/phase.md"
printf '\nall five refusals shown\n'
