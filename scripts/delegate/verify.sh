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
# Usage:
#   verify.sh check <package>              fast compile feedback (lib + bins)
#   verify.sh test <package>               unit tests (lib + bins)
#   verify.sh test <package> <int_test>    one named integration test target
#   verify.sh lint <package>               format, then scoped clippy (warnings denied)
#   verify.sh fmt <package>                format only (checkpoint-commit backstop)
#   verify.sh example <package> <name>     compile one example (only when the
#                                          phase changed that example)
#   verify.sh example-test <package> <name>
#                                          test one example (only when the
#                                          example contains unit tests)
#   verify.sh final                        full workspace gate (orchestrator only)

set -euo pipefail

usage() {
    sed -n '/^# Usage:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

run() {
    printf '+ %s\n' "$*"
    "$@"
}

have_nextest() {
    cargo nextest --version >/dev/null 2>&1
}

# Nightly rustfmt when available (unstable rustfmt.toml options), stable otherwise.
fmt_cargo() {
    if cargo +nightly fmt --version >/dev/null 2>&1; then
        run cargo +nightly fmt "$@"
    else
        run cargo fmt "$@"
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
        if [[ -n "$TARGET" ]]; then
            if have_nextest; then
                run cargo nextest run -p "$PKG" --test "$TARGET"
            else
                run cargo test -p "$PKG" --test "$TARGET"
            fi
        else
            FLAGS="$(target_flags "$PKG")"
            if have_nextest; then
                # shellcheck disable=SC2086
                run cargo nextest run -p "$PKG" $FLAGS
            else
                # shellcheck disable=SC2086
                run cargo test -p "$PKG" $FLAGS
            fi
        fi
        ;;
    lint)
        PKG="${1:?verify.sh lint <package>}"
        FLAGS="$(target_flags "$PKG")"
        fmt_cargo -p "$PKG"
        # shellcheck disable=SC2086
        run cargo clippy -p "$PKG" $FLAGS --tests -- -D warnings
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
        if have_nextest; then
            if [[ -n "$FEATURES" ]]; then
                run cargo nextest run -p "$PKG" --example "$NAME" --features "$FEATURES"
            else
                run cargo nextest run -p "$PKG" --example "$NAME"
            fi
        elif [[ -n "$FEATURES" ]]; then
            run cargo test -p "$PKG" --example "$NAME" --features "$FEATURES"
        else
            run cargo test -p "$PKG" --example "$NAME"
        fi
        ;;
    final)
        fmt_cargo --check
        run cargo check --workspace --all-targets
        if have_nextest; then
            run cargo nextest run --workspace
        else
            run cargo test --workspace
        fi
        ;;
    *)
        usage
        exit 2
        ;;
esac
