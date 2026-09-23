#!/usr/bin/env bash
# verify.sh — The only build/test/lint commands a delegate may run.
#
# Work Orders list exact invocations of this script; the delegate composes no
# cargo flags and makes no scope choices. Cargo's default target selection
# compiles a package's examples even under `-p <pkg>`, so every dev-loop
# subcommand pins explicit targets (--lib/--bins, derived from cargo metadata).
# Nothing below `final` compiles examples or uses --all-targets (mend excepted,
# see `lint`); `final` is the
# plan-final full gate, run by the orchestrator, never by a phase delegate.
#
# The lint stages — mend, fmt, clippy, doc — are gated by config/lint.conf (edit
# it with /lint_config), so one switch silences a check across /clippy, the fix
# pipeline, and every delegate phase. A phase runs all four over its package;
# only the branch-wide style review is left to the plan-final gate. A gated-off check prints a SKIPPED line and the command
# still exits 0. Scope is never configurable: the target pinning above is a
# correctness constraint, not a preference. cargo check and cargo nextest are
# never gated — a phase that compiles nothing has verified nothing.
#
# Usage:
#   verify.sh check <package>              fast compile feedback (lib + bins)
#   verify.sh test <package>               unit + integration tests
#                                          (lib + bins + tests)
#   verify.sh test <package> <int_test>    one named integration test target,
#                                          for re-running it alone
#   verify.sh lint <package>               mend --fix, nightly fmt, scoped clippy
#                                          (warnings denied), then rustdoc — every
#                                          stage gated by config/lint.conf
#   … [--features <list>]                  check, test, and lint accept one trailing
#                                          `--features a,b` when the Work Order names
#                                          it: code behind a non-default feature has
#                                          no other route to a scoped gate
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
# on top. Workspace-scope entry points (aliases, validate_ci, the fix pipeline,
# release checks) use the lint CLI in that directory instead of this script.
#
# Exit codes: 2 = usage error or missing tooling; 3 = sandbox failure, re-run
# the same command unsandboxed; anything else is the underlying cargo status.

set -euo pipefail

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.10.
PY="${HOME}/.claude/scripts/lib/py"

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
    if ! flags="$(cargo metadata --no-deps --format-version 1 | "$PY" -c "$TARGET_FLAGS_PY" "$1")"; then
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
        | "$PY" -c "$EXAMPLE_FEATURES_PY" "$1" "$2"
}

# Optional trailing `--features <list>` on check, test, and lint. The Work Order
# names the exact list; the delegate still composes no flags of its own. Any
# other leftover argument is a usage error rather than something to pass through.
FEATURE_FLAGS=()
take_features() {
    if [[ $# -gt 0 && "$1" == "--features" && -n "${2:-}" ]]; then
        FEATURE_FLAGS=(--features "$2")
        shift 2
    fi
    if [[ $# -ne 0 ]]; then
        usage
        exit 2
    fi
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

# A phase runs two delegates against one target/ directory and one Cargo lock,
# so every cargo run below has to be serialized against its peers. Taking the
# token here rather than asking each delegate's prompt to take it is deliberate:
# a rule that lives only in a prompt is a rule an agent can drop, and dropping
# this one blocks the whole team behind a lock nobody announced.
#
# PLAN_DELEGATE_BOARD_DIR is deliberately not PLAN_DELEGATE_SESSION_DIR: that
# variable also opens a progress activity window, and concurrent windows
# would collide in a recorder that keeps one.
BOARD_HELPER="${HOME}/.claude/scripts/delegate/board.sh"
BOARD_DIR="${PLAN_DELEGATE_BOARD_DIR:-}"
BOARD_SLOT="${PLAN_DELEGATE_TEAM_ROLE:-}"
TOKEN_HELD=0
if [[ -n "${BOARD_DIR}" && -n "${BOARD_SLOT}" && -f "${BOARD_HELPER}" ]]; then
    if bash "${BOARD_HELPER}" acquire "${BOARD_DIR}" "${BOARD_SLOT}" cargo \
        --hold 3600 --wait 1800 >/dev/null 2>&1; then
        TOKEN_HELD=1
    else
        echo "verify.sh: waited for the cargo token and did not get it; running anyway." >&2
    fi
fi

release_token() {
    [[ "${TOKEN_HELD}" -eq 1 ]] || return 0
    TOKEN_HELD=0
    bash "${BOARD_HELPER}" release "${BOARD_DIR}" "${BOARD_SLOT}" cargo >/dev/null 2>&1 || true
}
ACTIVITY_ACTIVE=0
if [[ -n "${ACTIVITY_SESSION_DIR}" \
      && -f "${ACTIVITY_SESSION_DIR}/progress_history_state.json" ]]; then
    # The gate notes under the stage table print this text verbatim, so it is
    # the invocation itself: "verify.sh test hana" names both the command that
    # ran and the script to open when a gate misbehaves, where a bare
    # "test hana" answered neither.
    if "$PY" "${PROGRESS_HISTORY}" start-activity \
        --session-dir "${ACTIVITY_SESSION_DIR}" \
        --label "Verification" \
        --activity "verify.sh ${CMD}${*:+ $*}" >/dev/null 2>&1; then
        ACTIVITY_ACTIVE=1
        finish_activity() {
            local status=$1
            # An activity row shows an outcome, and "completed" only says the
            # window closed. Name what the gate actually did.
            local result=""
            case "${status}" in
                completed) result="pass" ;;
                error) result="fail" ;;
            esac
            "$PY" "${PROGRESS_HISTORY}" finish-activity \
                --session-dir "${ACTIVITY_SESSION_DIR}" \
                --status "${status}" \
                --result "${result}" >/dev/null 2>&1 || true
        }
    fi
fi

# One exit path for both the progress window and the token, so a run that fails
# or is interrupted still hands the token back instead of leaving its peers to
# wait out the hold.
verify_cleanup() {
    local status=$1
    if [[ "${ACTIVITY_ACTIVE}" -eq 1 ]]; then
        finish_activity "${status}"
    fi
    release_token
}
# EXIT alone would report success for a failed cargo run, so branch on the
# status the trap receives.
trap '[[ $? -eq 0 ]] && verify_cleanup completed || verify_cleanup error' EXIT
trap 'verify_cleanup interrupted' INT TERM

case "$CMD" in
    check)
        PKG="${1:?verify.sh check <package>}"
        shift
        take_features "$@"
        FLAGS="$(target_flags "$PKG")"
        # shellcheck disable=SC2086
        run cargo check -p "$PKG" $FLAGS "${FEATURE_FLAGS[@]}"
        ;;
    test)
        PKG="${1:?verify.sh test <package> [integration_test]}"
        shift
        TARGET=""
        if [[ $# -gt 0 && "$1" != "--features" ]]; then
            TARGET="$1"
            shift
        fi
        take_features "$@"
        # --no-fail-fast: nextest cancels every remaining test after the first
        # failure, so one broken test silently hides the rest of the suite. A
        # phase gate has to report the whole result, not the first stop.
        if [[ -n "$TARGET" ]]; then
            run_nextest --no-fail-fast -p "$PKG" --test "$TARGET" "${FEATURE_FLAGS[@]}"
        else
            FLAGS="$(target_flags "$PKG")"
            # --tests adds the package's integration targets, matching what the
            # lint half already compiles under clippy. Without it a phase could
            # lint an integration test, pass its gate, and checkpoint without
            # ever running it. The build cost is already sunk by lint; the
            # measured runtime cost is seconds, because the expensive
            # compile-fail suites are #[ignore]d and .config/nextest.toml keeps
            # tool_id_boundary's downstream cases out of the default profile.
            # shellcheck disable=SC2086
            run_nextest --no-fail-fast -p "$PKG" $FLAGS --tests "${FEATURE_FLAGS[@]}"
        fi
        ;;
    lint)
        PKG="${1:?verify.sh lint <package>}"
        shift
        take_features "$@"
        # Order: mend rewrites first, fmt formats what mend wrote, then clippy
        # and rustdoc read the settled tree. Every stage is gated by lint.conf.
        # mend is the one stage below `final` that passes --all-targets: it
        # needs the package's test and example targets to see every import
        # and visibility site, and it compiles only this package's own targets.
        if lint_config_enabled mend; then
            MEND_LOG="$(mktemp)"
            # pipefail is set, so a failing mend fails the pipeline; the log
            # only decides which message names the failure.
            if ! invoke_mend -p "$PKG" --fix "${FEATURE_FLAGS[@]}" 2>&1 | tee "$MEND_LOG"; then
                if grep -qiE 'rolled back|revert' "$MEND_LOG"; then
                    echo "verify.sh: cargo mend --fix rolled its rewrites back; the tree reproduces it — run /mend_fix" >&2
                else
                    echo "verify.sh: cargo mend --fix failed; see the output above" >&2
                fi
                rm -f "$MEND_LOG"
                exit 1
            fi
            rm -f "$MEND_LOG"
        else
            lint_config_skip_notice mend "cargo mend --all-targets -p $PKG --fix"
        fi
        fmt_cargo -p "$PKG"
        # target_flags shells out to cargo metadata, so resolve it only when
        # clippy is actually going to run.
        if lint_config_enabled clippy; then
            FLAGS="$(target_flags "$PKG")"
            # shellcheck disable=SC2086
            invoke_clippy -p "$PKG" $FLAGS --tests "${FEATURE_FLAGS[@]}"
        else
            lint_config_skip_notice clippy "cargo clippy -p $PKG"
        fi
        invoke_doc -p "$PKG" "${FEATURE_FLAGS[@]}"
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
