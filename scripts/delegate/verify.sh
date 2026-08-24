#!/usr/bin/env bash
# verify.sh — The only build/test/lint commands a delegate may run.
#
# Work Orders list exact invocations of this script; the delegate composes no
# cargo flags and makes no scope choices. Cargo's default target selection
# compiles a package's examples even under `-p <pkg>`, so every dev-loop
# subcommand pins explicit targets (--lib/--bins, derived from cargo metadata).
# Nothing below `final` compiles examples or uses --all-targets; `final` is the
# plan-final full gate, run by the orchestrator, never by a phase delegate.
#
# The lint halves — clippy and fmt — are gated by config/lint.conf (edit it with
# /lint_config), so one switch silences a check across /clippy, clean-fix, and
# every delegate phase. A gated-off check prints a SKIPPED line and the command
# still exits 0. Scope is never configurable: the target pinning above is a
# correctness constraint, not a preference. cargo check and cargo nextest are
# never gated — a phase that compiles nothing has verified nothing.
#
# Usage:
#   verify.sh check <package>              fast compile feedback (lib + bins)
#   verify.sh test <package>               unit tests (lib + bins)
#   verify.sh test <package> <int_test>    one named integration test target
#   verify.sh lint <package>               format, then scoped clippy (warnings denied)
#                                          — both halves gated by config/lint.conf
#   verify.sh fmt <package>                format only (checkpoint-commit backstop)
#                                          — gated by config/lint.conf
#   verify.sh example <package> <name>     compile one example (only when the
#                                          phase changed that example)
#   verify.sh example-test <package> <name>
#                                          test one example (only when the
#                                          example contains unit tests)
#   verify.sh final                        full workspace gate (orchestrator only)
#
# Invocation policy — canonical flags, lint.conf gating, sandbox-failure
# detection — lives in scripts/lint/invoke.sh, the
# single bottom layer, sourced below. This file adds only delegate scope rules
# on top. Workspace-scope entry points (aliases, validate_ci, clean-fix,
# release checks) use the lint CLI in that directory instead of this script.
#
# Exit codes: 2 = usage error or missing tooling; 3 = sandbox failure, re-run
# the same command unsandboxed; anything else is the underlying cargo status.

set -euo pipefail

# The single bottom layer: run(), lint.conf gating, fmt_cargo, run_nextest,
# invoke_clippy, and the sandbox-failure detection all come from here.
# shellcheck source=/dev/null
source "$HOME/.claude/scripts/lint/invoke.sh"

usage() {
    sed -n '/^# Usage:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

TARGET_FLAGS_PY='
import json
import sys

package_name = sys.argv[1]
meta = json.load(sys.stdin)
lib_kinds = {"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"}
for package in meta["packages"]:
    if package["name"] != package_name:
        continue
    kinds = {kind for target in package["targets"] for kind in target["kind"]}
    flags = []
    if kinds & lib_kinds:
        flags.append("--lib")
    if "bin" in kinds:
        flags.append("--bins")
    print(" ".join(flags))
    sys.exit(0)
print("verify.sh: package " + package_name + " not found in workspace", file=sys.stderr)
sys.exit(2)
'

EXAMPLE_FEATURES_PY='
import json
import sys

package_name = sys.argv[1]
example_name = sys.argv[2]
meta = json.load(sys.stdin)
for package in meta["packages"]:
    if package["name"] != package_name:
        continue
    for target in package["targets"]:
        if target["name"] == example_name and "example" in target["kind"]:
            print(",".join(target.get("required-features", [])))
            sys.exit(0)
    print(
        "verify.sh: example " + example_name + " not found in package " + package_name,
        file=sys.stderr,
    )
    sys.exit(2)
print("verify.sh: package " + package_name + " not found in workspace", file=sys.stderr)
sys.exit(2)
'

# Emits the explicit target flags (--lib and/or --bins) for a package, so
# lib-only and bin-only crates both work without compiling examples.
target_flags() {
    local flags
    if ! flags="$(cargo metadata --no-deps --format-version 1 | python3 -c "$TARGET_FLAGS_PY" "$1")"; then
        exit 2
    fi
    if [[ -z "$flags" ]]; then
        echo "verify.sh: package $1 has no lib or bin targets" >&2
        exit 2
    fi
    printf '%s' "$flags"
}

example_features() {
    cargo metadata --no-deps --format-version 1 \
        | python3 -c "$EXAMPLE_FEATURES_PY" "$1" "$2"
}

CMD="${1:-}"
if [[ -z "$CMD" ]]; then
    usage
    exit 2
fi
shift

# Open a progress window for the duration of this run when a delegate session is
# in scope. This is what the orchestrator's progress header reports against while
# the main agent runs verification itself: an activity, not a pass, so
# findings.py never counts it toward convergence. The launcher-owns-its-own-pass
# rule applies here too — the thing that runs the work opens the window.
PROGRESS_HISTORY="${HOME}/.claude/scripts/delegate/progress_history.py"
ACTIVITY_SESSION_DIR="${PLAN_DELEGATE_SESSION_DIR:-}"
if [[ -n "${ACTIVITY_SESSION_DIR}" \
      && -f "${ACTIVITY_SESSION_DIR}/progress_history_state.json" ]]; then
    if python3 "${PROGRESS_HISTORY}" start-activity \
        --session-dir "${ACTIVITY_SESSION_DIR}" \
        --label "Verification" \
        --activity "${CMD} ${*:-workspace}" >/dev/null 2>&1; then
        finish_activity() {
            local status=$1
            # The stage table shows an outcome per row, and "completed" only
            # says the window closed. Name what the gate actually did.
            local result=""
            case "${status}" in
                completed) result="pass" ;;
                error) result="fail" ;;
            esac
            python3 "${PROGRESS_HISTORY}" finish-activity \
                --session-dir "${ACTIVITY_SESSION_DIR}" \
                --status "${status}" \
                --result "${result}" >/dev/null 2>&1 || true
        }
        # EXIT alone would report success for a failed cargo run, so branch on the
        # status the trap receives.
        trap '[[ $? -eq 0 ]] && finish_activity completed || finish_activity error' EXIT
        trap 'finish_activity interrupted' INT TERM
    fi
fi

case "$CMD" in
    check)
        PKG="${1:?verify.sh check <package>}"
        FLAGS="$(target_flags "$PKG")"
        # shellcheck disable=SC2086
        run cargo check -p "$PKG" $FLAGS
        ;;
    test)
        PKG="${1:?verify.sh test <package> [integration_test]}"
        TARGET="${2:-}"
        # --no-fail-fast: nextest cancels every remaining test after the first
        # failure, so one broken test silently hides the rest of the suite. A
        # phase gate has to report the whole result, not the first stop.
        if [[ -n "$TARGET" ]]; then
            run_nextest --no-fail-fast -p "$PKG" --test "$TARGET"
        else
            FLAGS="$(target_flags "$PKG")"
            # shellcheck disable=SC2086
            run_nextest --no-fail-fast -p "$PKG" $FLAGS
        fi
        ;;
    lint)
        PKG="${1:?verify.sh lint <package>}"
        fmt_cargo -p "$PKG"
        # target_flags shells out to cargo metadata, so resolve it only when
        # clippy is actually going to run.
        if lint_config_enabled clippy; then
            FLAGS="$(target_flags "$PKG")"
            # shellcheck disable=SC2086
            invoke_clippy -p "$PKG" $FLAGS --tests
        else
            lint_config_skip_notice clippy "cargo clippy -p $PKG"
        fi
        ;;
    fmt)
        PKG="${1:?verify.sh fmt <package>}"
        fmt_cargo -p "$PKG"
        ;;
    example)
        PKG="${1:?verify.sh example <package> <name>}"
        NAME="${2:?verify.sh example <package> <name>}"
        FEATURES="$(example_features "$PKG" "$NAME")"
        if [[ -n "$FEATURES" ]]; then
            run cargo check -p "$PKG" --example "$NAME" --features "$FEATURES"
        else
            run cargo check -p "$PKG" --example "$NAME"
        fi
        ;;
    example-test)
        PKG="${1:?verify.sh example-test <package> <name>}"
        NAME="${2:?verify.sh example-test <package> <name>}"
        FEATURES="$(example_features "$PKG" "$NAME")"
        if [[ -n "$FEATURES" ]]; then
            run_nextest -p "$PKG" --example "$NAME" --features "$FEATURES"
        else
            run_nextest -p "$PKG" --example "$NAME"
        fi
        ;;
    final)
        fmt_cargo --check
        run cargo check --workspace --all-targets
        run_nextest --workspace
        ;;
    *)
        usage
        exit 2
        ;;
esac
