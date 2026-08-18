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
# Exit codes: 2 = usage error or missing tooling; 3 = sandbox failure, re-run
# the same command unsandboxed; anything else is the underlying cargo status.

set -euo pipefail

# macOS sandboxes cannot nest. A dependency whose build script shells out to
# Swift Package Manager — apple-cf, apple-metal, screencapturekit, anything
# wrapping a macOS framework — makes SwiftPM call sandbox-exec, which fails
# inside the Claude Code sandbox. The panic names Swift and never names the
# sandbox, so it reads like a broken dependency and costs a round trip to
# diagnose. Name it here instead of leaving it to be rediscovered.
SANDBOX_SIGNATURE='sandbox_apply: Operation not permitted'

# Lint policy. Missing reader means every check runs — a delegate must never be
# silently under-verified because a config script moved.
LINT_CONFIG_READER="$HOME/.claude/scripts/lint/lint_config.sh"
if [[ -f "$LINT_CONFIG_READER" ]]; then
    # shellcheck source=/dev/null
    source "$LINT_CONFIG_READER"
else
    echo "verify.sh: $LINT_CONFIG_READER not found — running every check" >&2
    lint_config_enabled() { return 0; }
    lint_config_skip_notice() { :; }
fi

usage() {
    sed -n '/^# Usage:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

run() {
    printf '+ %s\n' "$*"
    local log="${TMPDIR:-/tmp}/verify_sh.$$.log"
    local status=0
    # tee keeps output streaming: heartbeat_watch.sh digests the agent log to
    # prove the delegate is alive, so buffering a long build looks like a hang.
    set +e
    "$@" 2>&1 | tee "$log"
    status=${PIPESTATUS[0]}
    set -e
    if [[ $status -ne 0 ]] && grep -q "$SANDBOX_SIGNATURE" "$log"; then
        rm -f "$log"
        cat >&2 <<'EOF'

verify.sh: THIS IS A SANDBOX FAILURE, NOT A TEST FAILURE.

A dependency build script shells out to Swift Package Manager, which sandboxes
itself with sandbox-exec. macOS sandboxes cannot nest, so the call fails and the
build script panics naming Swift.

Re-run this exact command with the sandbox disabled — in Claude Code, pass
dangerouslyDisableSandbox: true on the Bash call.

Do not report this as a finding. Do not pin, patch, or change the dependency it
names. Nothing in settings.json fixes it: excludedCommands decides whether an
unsandboxed run needs approval, not whether it runs unsandboxed.
EOF
        exit 3
    fi
    rm -f "$log"
    return $status
}

have_nextest() {
    cargo nextest --version >/dev/null 2>&1
}

# The workspace's rustfmt configuration uses nightly-only options. Formatting with
# stable could accept output that nightly rejects, so it is a verifier error.
fmt_cargo() {
    if ! lint_config_enabled fmt; then
        lint_config_skip_notice fmt "cargo +nightly fmt $*"
        return 0
    fi
    if ! cargo +nightly fmt --version >/dev/null 2>&1; then
        echo "verify.sh: cargo +nightly fmt is required" >&2
        exit 2
    fi
    run cargo +nightly fmt "$@"
}

require_nextest() {
    if ! have_nextest; then
        echo "verify.sh: cargo nextest is required" >&2
        exit 2
    fi
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
            python3 "${PROGRESS_HISTORY}" finish-activity \
                --session-dir "${ACTIVITY_SESSION_DIR}" \
                --status "${status}" >/dev/null 2>&1 || true
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
        require_nextest
        if [[ -n "$TARGET" ]]; then
            run cargo nextest run --no-fail-fast -p "$PKG" --test "$TARGET"
        else
            FLAGS="$(target_flags "$PKG")"
            # shellcheck disable=SC2086
            run cargo nextest run --no-fail-fast -p "$PKG" $FLAGS
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
            run cargo clippy -p "$PKG" $FLAGS --tests -- -D warnings
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
        require_nextest
        if [[ -n "$FEATURES" ]]; then
            run cargo nextest run -p "$PKG" --example "$NAME" --features "$FEATURES"
        else
            run cargo nextest run -p "$PKG" --example "$NAME"
        fi
        ;;
    final)
        fmt_cargo --check
        run cargo check --workspace --all-targets
        require_nextest
        run cargo nextest run --workspace
        ;;
    *)
        usage
        exit 2
        ;;
esac
