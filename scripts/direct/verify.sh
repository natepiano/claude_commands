#!/usr/bin/env bash
# verify.sh runs the shared Cargo commands for people and workers.
#
# Usage:
#   verify <command> [<package>] [flags]
#
# Commands:
#   check [<package>]      check workspace libraries and binaries
#   test [<package>]       run package tests; try the Mac, else natedev
#   lint [<package>]       repair and format code; check TOML and run Clippy
#   doc [<package>]        document the staged workspace; deny warnings
#   example [<package>]    run Clippy on examples on an eligible target
#   build [<package>]      build binaries on natedev and print their paths
#   final [<package>]      check workspace targets; run tests on natedev
#   help <command>       show details for one command
#
# Flags:
#   --test <name>        test: select one integration target
#   --name <name>        example: select one example
#   --check              lint: report findings without repairs
#   --saved              final: read a saved workspace pass
#   --tree <tree>        final --saved: select a tree
#   --filter <name>      test: select names; repeat to select more names
#   --run-ignored only   test: run only ignored tests, with optional filters
#   --features <list>    check, test, doc, build: enable Cargo features
#   --no-cache           test, lint: ignore saved results
#   --target <name>      pin movable steps to one configured target
#   --mac "<reason>"     pin movable steps to the Mac
#   --local "<reason>"   pin steps to natedev
#   --session-dir <dir>  use a worker session; put this before the command
# End usage.
#
# Help check:
# Usage: verify check [<package>] [--keep-going] [--features <list>]
#
# Check workspace libraries and binaries without producing executables.
# A named package must exist. Cargo resolves features for the whole workspace.
# --keep-going continues after a crate fails.
# Names without a slash in --features belong to the named package.
# Without a package, pass feature names unchanged.
# Add workspace.metadata.dev.features to the requested features.
# Try eligible targets in config/build_targets.conf order.
# --target, --mac and --local pin movable steps to one target.
# Always run this check; do not reuse a saved result.
# End help.
#
# Help test:
# Usage: verify test [<package>] [--test <name>] [flags]
#
# Run package tests with nextest. No package selects the workspace.
# Try eligible targets in config/build_targets.conf order.
# The default order is Mac, natedev, then AWS.
# A busy or unavailable Mac allows natedev to run the tests.
# A test that requires Linux does not run on the Mac.
# A waiting local test can retry on the Mac.
#
# --test <name> runs one integration test target.
# Repeat --filter <name> to select tests that contain any named text.
# --run-ignored only selects ignored tests; with no package, all packages.
# Do not combine an integration test target with --filter.
# --features <list> enables features for the package.
# Also add workspace.metadata.dev.features.
# Cargo builds the package's test targets and workspace libraries as needed.
# A full package run refuses examples that contain tests or set test = true.
#
# --mac "<reason>" waits for the Mac without a local fallback.
# With --mac, repeat --env NAME to forward named environment variables.
# --fetch <folder> retrieves one ignored folder from the Mac.
# --local "<reason>" runs on natedev for a test that cannot pass on macOS.
# --target <name> selects one configured target, including natedev.
# The command waits for that target without a fallback.
#
# A manual run without a worker session always runs the tests.
# UNIT_RUN_DIR, UNIT_SESSION_DIR or --session-dir enables saved results.
# In that session, reuse a pass only when the tree and inputs still match.
# Filtered runs have separate results. Failed tests always run again.
# Test keys include configured result variables and each --env value.
# Tests use fixed terminal values from direct.conf on every target.
# --no-cache ignores saved results. --target also ignores saved results.
# End help.
#
# Help lint:
# Usage: verify lint [<package>] [--check]
#
# Run mend repairs, nightly rustfmt, Taplo, then Clippy judgment.
# Mend and Clippy cover the workspace. Clippy checks all targets and features.
# Deny Clippy warnings. A clean judgment needs no Clippy repairs.
# Status 101 permits Clippy repairs unless a kill or a hold prevents repairs.
# Format after repairs, then judge again.
# config/lint.conf selects mend, rustfmt and Clippy. Taplo always runs.
#
# Format the named package and members that differ from HEAD.
# No package formats all members.
# --check reports findings without applying fixes.
# Tree writes stay on natedev under every routing flag.
# Judgment uses the configured target order, or the pinned target.
#
# A manual run without a worker session always runs lint.
# UNIT_RUN_DIR, UNIT_SESSION_DIR or --session-dir enables saved results.
# In that session, reuse a pass or failure when the tree and inputs match.
# A plain package lint result can answer another package in the workspace.
# Do not save a result if edits change the tree after judgment.
# --no-cache ignores saved results.
# End help.
#
# Help doc:
# Usage: verify doc [<package>] [--features <list>] [--target <name>]
#
# Document the staged workspace with rustdoc. Deny warnings.
# `copies.py hold` locks a standing checkout under the configured copies root.
# Fill that checkout from the staged index. Exclude unstaged and untracked files.
# Keep the real index unchanged.
# Always run rustdoc, even with doc=off in config/lint.conf.
# Try eligible targets in config/build_targets.conf order.
# --target, --mac and --local pin movable steps to one target.
# A named package owns feature names without a slash.
# Also add workspace.metadata.dev.features.
#
# A Rust checkpoint requires a pass for its exact staged tree.
# Save a pass under ~/.local/state/verify/doc_pass/<tree>.json.
# A changed staged tree records no pass. Keep records across worker sessions.
# Remove records older than 14 days when writing a record.
# End help.
#
# Help example:
# Usage: verify example [<package>] [--name <name>]
#
# Run Clippy on examples on the first eligible target. Deny warnings.
# --target, --mac and --local pin movable steps to one target.
# Without --name, check all examples in the selection.
# Enable the example's required features for the named package.
# Cargo resolves features for the whole workspace.
# config/lint.conf can disable Clippy. Do not reuse a saved result.
# Examples contain no tests. Use test for unit and integration tests.
# End help.
#
# Help build:
# Usage: verify build [<package>] [--bin <name> | --example <name>]
#                     [--release] [--features <list>]
#
# Build binaries on natedev unless --target or --mac pins the step.
# --local pins the step to natedev.
# --bin selects one binary. --example selects one example.
# Enable the example's required features when building an example.
# --release selects the release profile.
# Names without a slash in --features belong to the named package.
# Without a package, pass feature names unchanged.
# For a debug build, also add workspace.metadata.dev.features.
# Cargo resolves features for the whole workspace.
# Print local executables as binary: <path>.
# Print remote executables as binary: <target>:<path>.
# Always build; do not reuse a saved result.
# End help.
#
# Help final:
# Usage: verify final [<package>] [--saved] [--tree <tree>]
#
# Check all workspace targets, then run selected tests with nextest on natedev.
# Add workspace.metadata.dev.features to both steps.
# Check examples, but do not link them in nextest.
# Refuse examples that contain tests or set test = true.
# Run the full check and tests every time. Do not run lint or formatting.
# The unit director uses this command for the final workspace gate.
# A clean, unchanged workspace gets a saved pass when both steps succeed.
# A named package runs its tests and saves no pass.
# --target or --mac pins both steps and saves no pass.
# --local pins both steps to natedev and saves no pass.
# A pinned final still requires a clean tree.
# --saved reads a saved workspace pass; --tree selects its tree.
# Return 0 for a matching pass, or 1 when the pass is absent or differs.
# Save it under ~/.local/state/verify/final_pass/<tree>.json.
# VERIFY_STATE_DIR changes the state folder.
# Remove records older than 14 days when writing a record.
# End help.
#
# Help example-test:
# Usage: verify example-test
#
# This command was removed. Examples contain no tests.
# Use verify test [<package>] for unit and integration tests.
# End help.
#
# Help help:
# Usage: verify help <command>
#
# Print details for one command without starting verification.
# -h and --help print short usage and return 0.
# No arguments print short usage to stderr and return 2.
# A missing or unknown help command prints short usage to stderr and returns 2.
# End help.
#
# Shared invocation policy lives in scripts/lint/invoke.sh.
# This script adds command scope, routing, records and worker session rules.
# Work Orders specify the command and flags for workers.
# Workspace feature resolution avoids separate dependency builds per package.
# Test and lint keys exclude session files because the call writes there.
# Test keys cover package inputs. Lint keys include tool and policy versions.
# Keys include arguments, rustc -vV, lint.conf, verify.sh and invoke.sh.
# Keys also include the RUST*, CARGO_* and NEXTEST_* environment variables.
# Test keys also include the result environment selected in direct.conf.
# Fixed terminal values replace caller values locally and on remote targets.
# New result inputs and --env values do not change the build key.
# Lint keys include mend, nightly rustfmt, Clippy and Taplo versions.
# Filter keys sort names and remove duplicate names.
# Do not cache usage errors, sandbox failures or killed runs.
# Do not cache lint failures if the last repair and format pass changed files.
# Lint records use the tree left by repairs and formatting for judgment.
# Session cleanup removes cached results. The build log keeps call records.
# Saved time excludes Cargo build time because unchanged inputs need no build.
# Call records include time, repository, worktree, branch, commit and call id.
# Steps use the same call id. /verify_saved reports the saved time.
# Status 77 means edits changed the tree during judgment.
# Status 76 means a newer call replaced this call in the token queue.
# Exit 2 means a usage error, missing tooling or a Mac-only refusal.
# Exit 3 means a sandbox failure. Other statuses come from the tool.

set -euo pipefail

usage() {
    sed -n '/^# Usage:$/,/^# End usage\./{
        /^# End usage\./d
        s/^# \{0,1\}//
        p
    }' "${BASH_SOURCE[0]}" >&"${1:-2}"
}

command_help() {
    sed -n "/^# Help $1:/,/^# End help\./{
        /^# Help /d
        /^# End help\./d
        s/^# \{0,1\}//
        p
    }" "${BASH_SOURCE[0]}"
}

# Remove routing flags before constructing command words and records.
parse_routing_arguments() {
    NO_CACHE=0
    LOCAL_ONLY=0
    LOCAL_REASON=""
    MAC_ONLY=0
    MAC_REASON=""
    MAC_ENV_NAMES=()
    VERIFY_MAC_ENVIRONMENT=()
    MAC_FETCH=""
    MAC_FETCH_GIVEN=0
    PINNED_TARGET=""
    ARGS=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --target)
                if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == -* || -n "$PINNED_TARGET" ]]; then
                    echo 'verify.sh: --target needs one configured target, including natedev.' >&2
                    exit 2
                fi
                PINNED_TARGET=$2
                shift 2
                ;;
            --no-cache)
                NO_CACHE=1
                shift
                ;;
            --mac)
                if [[ $# -lt 2 || "${2:-}" =~ ^[[:space:]]*$ || "${2:-}" == --* ]]; then
                    echo 'verify.sh: --mac needs a reason: --mac "<why this command must run on macOS>".' >&2
                    exit 2
                fi
                MAC_ONLY=1 MAC_REASON=$2
                shift 2
                ;;
            --env)
                name=${2:-}
                if [[ ! "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
                    printf 'verify.sh: --env %s: not a variable name.\n' "$name" >&2
                    exit 2
                fi
                MAC_ENV_NAMES+=("$name")
                shift 2
                ;;
            --fetch)
                if [[ "$MAC_FETCH_GIVEN" == 1 ]]; then
                    echo 'verify.sh: --fetch is given twice; name one folder.' >&2
                    exit 2
                fi
                if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then
                    echo 'verify.sh: --fetch needs a folder relative to the repository root, such as target/shots.' >&2
                    exit 2
                fi
                MAC_FETCH_GIVEN=1 MAC_FETCH=${2%/}
                shift 2
                ;;
            --local)
                if [[ $# -lt 2 || "${2:-}" =~ ^[[:space:]]*$ || "${2:-}" == --* ]]; then
                    echo 'verify.sh: --local needs a reason: --local "<why this command must run on natedev>". Leave it off to run on the Mac.' >&2
                    exit 2
                fi
                # A reservation used as a --local reason never ends.
                # Use a Mac hold to keep tests off the Mac (user, 2026-10-18).
                if [[ "$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')" =~ (^|[^[:alpha:]])(reserve|reserved|reservation|hold|holds|held|blocked|claim|claims|busy|showrunner|ruling)([^[:alpha:]]|$) ]]; then
                    printf 'verify.sh: --local is only for a test that cannot pass on macOS, not to keep tests off the Mac ("%s"). To reserve the Mac, its holder runs /build:hold hold <why> target mac: tests then skip the Mac by themselves, /build:status shows the hold, and it ends at its expiry. Leave --local off.\n' "$2" >&2
                    exit 2
                fi
                LOCAL_ONLY=1
                LOCAL_REASON=$2
                shift 2
                ;;
            *)
                ARGS+=("$1")
                shift
                ;;
        esac
    done
    if [[ "$MAC_ONLY" == 1 && "$LOCAL_ONLY" == 1 ]]; then
        echo 'verify.sh: --mac and --local cannot be used together.' >&2
        exit 2
    fi
    if [[ "$MAC_ONLY" == 0 && ( ${#MAC_ENV_NAMES[@]} -gt 0 || "$MAC_FETCH_GIVEN" == 1 ) ]]; then
        echo 'verify.sh: --env and --fetch need --mac "<why>".' >&2
        exit 2
    fi
    for name in ${MAC_ENV_NAMES[@]+"${MAC_ENV_NAMES[@]}"}; do
        case "$name" in
            PATH|HOME|MACOSX_DEPLOYMENT_TARGET|RUSTFLAGS|RUSTDOCFLAGS|BUILDLOG_*|LINT_*|CARGO_*|RUSTC*|NEXTEST_*|DYLD_*|MAC_TEST_*|VERIFY_*)
                printf 'verify.sh: --env %s: the runner sets it, or it changes what Cargo builds; it is not forwarded.\n' "$name" >&2
                exit 2 ;;
        esac
        VERIFY_ENV_FOUND=0
        for VERIFY_ENV_ENTRY in ${VERIFY_CALL_ENVIRONMENT[@]+"${VERIFY_CALL_ENVIRONMENT[@]}"}; do
            if [[ "${VERIFY_ENV_ENTRY%%=*}" == "$name" ]]; then
                VERIFY_MAC_ENVIRONMENT+=("$VERIFY_ENV_ENTRY")
                VERIFY_ENV_FOUND=1
                break
            fi
        done
        if [[ "$VERIFY_ENV_FOUND" == 0 ]]; then
            printf 'verify.sh: --env %s: not set here; set it on the call (NAME=value verify.sh …).\n' "$name" >&2
            exit 2
        fi
    done
    if [[ "$MAC_FETCH_GIVEN" == 1 ]]; then
        if [[ ! "$MAC_FETCH" =~ ^[A-Za-z0-9._/-]+$ || "$MAC_FETCH" == /* || "$MAC_FETCH" == -* \
              || "/$MAC_FETCH/" == *//* || "/$MAC_FETCH/" == */./* || "/$MAC_FETCH/" == */../* ]]; then
            printf 'verify.sh: --fetch %s: not a plain relative folder.\n' "$MAC_FETCH" >&2
            exit 2
        fi
        if ! git -C "$(git rev-parse --show-toplevel)" check-ignore -q -- "$MAC_FETCH/"; then
            printf 'verify.sh: --fetch %s: git does not ignore it; name a folder the tests write, such as one under target/.\n' "$MAC_FETCH" >&2
            exit 2
        fi
    fi
    if [[ -n "$PINNED_TARGET" ]]; then
        if [[ "$LOCAL_ONLY" == 1 || "$MAC_ONLY" == 1 ]]; then
            echo 'verify.sh: --target cannot combine with --local or --mac.' >&2
            exit 2
        fi
        NO_CACHE=1
    fi
    export BUILDLOG_PINNED_TARGET="$PINNED_TARGET"
}


# Parse the package separately from flags and their values.
parse_command_arguments() {
    local ignored_only=0 integration_target=0
    VERIFY_PACKAGE=""
    COMMAND_ARGS=()
    SAVED_FINAL=0
    SAVED_TREE=""
    EXAMPLE_NAME=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --workspace) command_help "$CMD" >&2; exit 2 ;;
            --saved)
                [[ "$CMD" == final ]] || { usage; exit 2; }
                SAVED_FINAL=1; shift ;;
            --tree)
                [[ "$CMD" == final && -n "${2:-}" ]] || { usage; exit 2; }
                SAVED_TREE=$2; shift 2 ;;
            --test|--name)
                if [[ ( "$1" == --test && "$CMD" != test ) || ( "$1" == --name && "$CMD" != example ) || -z "${2:-}" || "$2" == --* ]]; then
                    command_help "$CMD" >&2; exit 2
                fi
                if [[ "$1" == --name ]]; then EXAMPLE_NAME=$2; else integration_target=1; fi
                COMMAND_ARGS+=("$1" "$2"); shift 2 ;;
            --run-ignored)
                if [[ "$CMD" != test || "${2:-}" != only ]]; then
                    echo 'verify.sh: use test [<package>] --run-ignored only, without --test.' >&2
                    exit 2
                fi
                ignored_only=1
                COMMAND_ARGS+=("$1" "$2"); shift 2 ;;
            --features|--filter|--bin|--example|--target|--env|--fetch|--mac|--local)
                [[ $# -ge 2 ]] || { command_help "$CMD" >&2; exit 2; }
                COMMAND_ARGS+=("$1" "$2"); shift 2 ;;
            --*) COMMAND_ARGS+=("$1"); shift ;;
            *)
                [[ -z "$VERIFY_PACKAGE" ]] || { command_help "$CMD" >&2; exit 2; }
                VERIFY_PACKAGE=$1; shift ;;
        esac
    done
    if [[ "$ignored_only" == 1 && "$integration_target" == 1 ]]; then
        echo 'verify.sh: use test [<package>] --run-ignored only, without --test.' >&2
        exit 2
    fi
    [[ -z "$SAVED_TREE" || "$SAVED_FINAL" == 1 ]] || { command_help final >&2; exit 2; }
    [[ "$SAVED_FINAL" == 0 || ${#COMMAND_ARGS[@]} == 0 ]] || { command_help final >&2; exit 2; }
    ARGS=("$VERIFY_PACKAGE" ${COMMAND_ARGS[@]+"${COMMAND_ARGS[@]}"})
}

# Help returns before environment setup, target queries or build records.
case "${1:-}" in
    -h|--help) usage 1; exit 0 ;;
    '') usage; exit 2 ;;
    help)
        if [[ $# -eq 2 ]]; then
            case "$2" in
                check|test|lint|doc|example|build|final|example-test|help)
                    command_help "$2"; exit 0 ;;
            esac
        fi
        usage; exit 2
        ;;
esac

# Save exported values before script variables replace caller names.
# The --env rule refuses the VERIFY_ prefix. env -0 works in every bash build;
# a bash built without programmable completion has no compgen.
VERIFY_CALL_ENVIRONMENT=()
while IFS= read -r -d '' VERIFY_ENV_ENTRY; do
    case "$VERIFY_ENV_ENTRY" in VERIFY_*) continue ;; esac
    VERIFY_CALL_ENVIRONMENT+=("$VERIFY_ENV_ENTRY")
done < <(env -0)
# Trace the helper in its interpreter. CPU time excludes child processes.
# The normal path starts no extra interpreter.
VERIFY_START_DIR="${BASH_SOURCE[0]%/*}"
[[ "$VERIFY_START_DIR" != "${BASH_SOURCE[0]}" ]] || VERIFY_START_DIR=.
VERIFY_START_DIR="$(cd -- "$VERIFY_START_DIR" && pwd -P)"
PY="$VERIFY_START_DIR/../lib/py"
if [[ -n "${VERIFY_PY_TRACE:-}" ]]; then
    if [[ "$VERIFY_PY_TRACE" != /* ]]; then export VERIFY_PY_TRACE="$PWD/$VERIFY_PY_TRACE"; fi
    export VERIFY_TRACE_REAL_PY="$PY"
    PY="${VERIFY_PY_TRACE}.$$.runner"
    cat > "$PY" <<'VERIFY_TRACE_RUNNER'
#!/usr/bin/env bash
options=()
while [[ "${1:-}" == -S || "${1:-}" == -u ]]; do
    options+=("$1"); shift
done
exec "$VERIFY_TRACE_REAL_PY" "${options[@]}" -c '
import atexit, os, signal, sys, time, types
started = float(sys.argv[1])
arguments = sys.argv[2:]
helper = os.environ.get("VERIFY_PY_HELPER", "") or os.path.basename(arguments[0])
if helper == "-c": helper = "INLINE_PY"
if helper == "-": helper = "STDIN_PY"
process = os.getpid()
recorded = False

def record_trace():
    global recorded
    if recorded or os.getpid() != process:
        return
    recorded = True
    with open(os.environ["VERIFY_PY_TRACE"], "a") as trace:
        trace.write(f"{helper}\t{round((time.time() - started) * 1000)}\t{round(time.process_time() * 1000)}\n")

def terminate_trace(signum, _frame):
    record_trace()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)

signal.signal(signal.SIGTERM, terminate_trace)
atexit.register(record_trace)
sys.addaudithook(lambda event, _arguments: record_trace() if event == "os.exec" else None)
if arguments[0] == "-c":
    helper = helper or "INLINE_PY"
    source, filename = arguments[1], "<string>"
    sys.argv = ["-c", *arguments[2:]]
elif arguments[0] == "-":
    helper = helper or "STDIN_PY"
    source, filename = sys.stdin.read(), "<stdin>"
    sys.argv = arguments
else:
    filename = arguments[0]
    helper = os.path.basename(filename)
    with open(filename, "rb") as script:
        source = script.read()
    sys.argv = arguments
module = types.ModuleType("__main__")
module.__file__ = filename
sys.modules["__main__"] = module
exec(compile(source, filename, "exec"), module.__dict__)
' "${EPOCHREALTIME:-$(date +%s)}" "$@"
VERIFY_TRACE_RUNNER
    chmod +x "$PY"
fi
# Read-only queries run before environment setup and build admission.
VERIFY_QUERY_ARGS=("$@")
[[ "${VERIFY_QUERY_ARGS[0]:-}" != --session-dir ]] || VERIFY_QUERY_ARGS=("${VERIFY_QUERY_ARGS[@]:2}")
if [[ "${VERIFY_QUERY_ARGS[0]:-}" == final-pass ]]; then
    command_help final >&2
    exit 2
fi
if [[ "${VERIFY_QUERY_ARGS[0]:-}" == final && " ${VERIFY_QUERY_ARGS[*]} " == *" --saved "* ]]; then
    CMD=final
    parse_command_arguments "${VERIFY_QUERY_ARGS[@]:1}"
    export CLAUDE_PYTHON="${CLAUDE_PYTHON:-python3}"
    exec "$PY" "$VERIFY_START_DIR/final_pass.py" query "$VERIFY_START_DIR/verify.sh" "$SAVED_TREE"
fi
VERIFY_REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
if [[ ! -f "$VERIFY_REPO_ROOT/Cargo.toml" ]]; then
    printf 'verify.sh: %s has no Cargo.toml; verify.sh checks Cargo repositories only\n' "$VERIFY_REPO_ROOT" >&2
    exit 2
fi
# Create the inherited temporary parent before environment commands use it.
VERIFY_INVOKE_DIR="${BASH_SOURCE[0]%/*}"
[[ "$VERIFY_INVOKE_DIR" != "${BASH_SOURCE[0]}" ]] || VERIFY_INVOKE_DIR=.
source "$VERIFY_INVOKE_DIR/../lint/invoke.sh" --prepare-temp || exit 2
# Load the environment once through the shared helper, before tool setup or records.
if [[ -n "${VERIFY_NIX_ENV_REEXEC:-}" ]]; then
    if [[ -n "${IN_NIX_SHELL:-}" ]]; then
        printf '%s\n' 'verify.sh: not in the nix shell; re-running under nix_env.py exec' >&2
    elif [[ -n "${NIX_ENV_ENVRC:-}" ]]; then
        printf '%s\n' 'verify.sh: nix_env.py re-exec still has no nix shell (IN_NIX_SHELL unset)' >&2
        exit 1
    fi
elif [[ -z "${IN_NIX_SHELL:-}" ]]; then
    VERIFY_NIX_SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
    [[ "$VERIFY_NIX_SCRIPT_DIR" != "${BASH_SOURCE[0]}" ]] || VERIFY_NIX_SCRIPT_DIR=.
    VERIFY_NIX_SCRIPT_DIR="$(cd -- "$VERIFY_NIX_SCRIPT_DIR" && pwd -P)"
    export VERIFY_NIX_ENV_REEXEC=1
    exec "$PY" -S "${VERIFY_NIX_SCRIPT_DIR%/*}/lib/nix_env.py" \
        exec "$VERIFY_NIX_SCRIPT_DIR/${BASH_SOURCE[0]##*/}" "$@"
fi
unset VERIFY_NIX_ENV_REEXEC

BUILD_OVERRIDE_GUARD_DIR="${BASH_SOURCE[0]%/*}"
[[ "$BUILD_OVERRIDE_GUARD_DIR" != "${BASH_SOURCE[0]}" ]] || BUILD_OVERRIDE_GUARD_DIR=.
source "$BUILD_OVERRIDE_GUARD_DIR/../build_hold/override_guard.sh" || exit 2
build_override_refusal "${BASH_SOURCE[0]}" direct/verify.sh || exit 2
UNIT_ENV_SHIM="${BASH_SOURCE[0]%/*}/../lib/unit_env.sh"
[[ ! -r "$UNIT_ENV_SHIM" ]] || source "$UNIT_ENV_SHIM"

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.12.
if [[ -n "${VERIFY_PY_TRACE:-}" ]]; then
    export VERIFY_TRACE_REAL_PY="$VERIFY_START_DIR/../lib/py"
else
    PY="$VERIFY_START_DIR/../lib/py"
fi

# One parent-owned helper serves command substitutions and cleanup traps.
verify_helper() {
    if [[ "${1:-}" == --start ]]; then
        [[ "${VERIFY_HELPER_STARTED:-0}" == 0 ]] || return 0
        VERIFY_HELPER_STARTED=1
        local script="$VERIFY_START_DIR/verify_helper.py" input output
        [[ -f "$script" ]] || return 0
        VERIFY_HELPER_LOCK="$(mktemp 2>/dev/null)" || return 0
        coproc VERIFY_CALLS { exec "$VERIFY_START_DIR/../lib/py" "$script" "$VERIFY_HELPER_LOCK"; } 2>/dev/null
        input=${VERIFY_CALLS[1]:-}
        output=${VERIFY_CALLS[0]:-}
        [[ -n "$input" && -n "$output" ]] || return 0
        # Bash places coprocess and process-substitution descriptors at 63 and below, so the helper uses 70-73.
        if { exec 70>&"$input" 71<&"$output"; } 2>/dev/null; then
            VERIFY_HELPER_INPUT=70
            VERIFY_HELPER_OUTPUT=71
            eval "exec $input>&- $output<&-"
        fi
        return 0
    fi
    (
    local input="" has_input=0 status output errors entry
    local forwarded=()
    if [[ "${1:-}" == --stdin ]]; then
        shift
        has_input=1
        input="$(cat; printf '\001')"
        input="${input%$'\001'}"
    fi
    if [[ -n "${VERIFY_HELPER_INPUT:-}" && -n "${VERIFY_HELPER_OUTPUT:-}" ]]; then
        if [[ -n "${VERIFY_RESULT_ENVIRONMENT_FD:-}" ]]; then
            while IFS= read -r -d '' entry <&"$VERIFY_RESULT_ENVIRONMENT_FD"; do forwarded+=("$entry"); done
        fi
        exec 72>"$VERIFY_HELPER_LOCK"
        if flock -x 72 2>/dev/null && (
            printf '%s\0' "$1" "$PWD" "$(( $# - 1 ))" "${@:2}"
            env -0
            printf '\0%s\0' "$input"
            if [[ ${#forwarded[@]} -gt 0 ]]; then printf '%s\0' "${forwarded[@]}"; fi
            printf '\0'
        ) >&"$VERIFY_HELPER_INPUT" 2>/dev/null \
            && IFS= read -r -d '' status <&"$VERIFY_HELPER_OUTPUT" \
            && IFS= read -r -d '' output <&"$VERIFY_HELPER_OUTPUT" \
            && IFS= read -r -d '' errors <&"$VERIFY_HELPER_OUTPUT"; then
            exec 72>&-
            if [[ "$status" =~ ^[0-9]+$ ]]; then
                printf '%s' "$output"
                printf '%s' "$errors" >&2
                return "$status"
            fi
        else
            exec 72>&-
        fi
    fi
    if [[ -n "${VERIFY_RESULT_ENVIRONMENT_FD:-}" && ${#forwarded[@]} -gt 0 ]]; then
        exec 73< <(printf '%s\0' "${forwarded[@]}")
        export VERIFY_RESULT_ENVIRONMENT_FD=73
    fi
    if [[ "$has_input" == 1 ]]; then
        printf '%s' "$input" | "$PY" "$@"
    else
        "$PY" "$@"
    fi
    )
}

hash_stream() {
    local hash
    if command -v sha256sum >/dev/null 2>&1; then
        hash="$(sha256sum)" || return 1
    else
        hash="$(shasum -a 256)" || return 1
    fi
    printf '%s\n' "${hash%% *}"
}

# shellcheck source=/dev/null
source "${BASH_SOURCE[0]%/*}/../lib/conf_record.sh"

lint_code_key() {
    local file hash
    hash="$(
        set -o pipefail
        {
            for file in "${BASH_SOURCE[0]}" "$HOME/.claude/scripts/lint/invoke.sh" \
                "${LINT_CONFIG_FILE:-$HOME/.claude/config/lint.conf}"; do
                if [[ -f "$file" ]]; then
                    printf 'present\0'
                    case "$file" in *.conf) stem="${file##*/}"; conf_record "${stem%.conf}" ;; esac
                    cat -- "$file" || exit 1
                else
                    printf 'absent\0'
                fi
                printf '\0'
            done
        } | hash_stream
    )" || hash=""
    printf '%s\n' "$hash"
}
LINT_LOADED_CODE_KEY="$(lint_code_key)"

# The single bottom layer: run(), lint.conf gating, fmt_cargo, run_nextest,
# invoke_clippy_fix, invoke_clippy, and sandbox-failure detection come from here.
# shellcheck source=/dev/null
source "$HOME/.claude/scripts/lint/invoke.sh" || {
    printf 'verify.sh: cannot load admission and hold checks; step refused\n' >&2
    exit 2
}
BUILD_TARGETS_SCRIPT="${BUILD_TARGETS_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../build_targets" && pwd)/build_targets.py}"
LOCAL_RELEASE_WAIT_SCRIPT="${VERIFY_LOCAL_RELEASE_WAIT_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/local_release_wait.py}"
STEP_RECORD_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/step_record.py"
FINAL_PASS_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/final_pass.py"
VERIFY_METADATA_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/verify_metadata.py"
FINAL_PASS_START=""
final_pass_finish() {
    [[ -n "$FINAL_PASS_START" ]] || return 0
    env -u BUILDLOG_CALL_ID "$PY" "$FINAL_PASS_SCRIPT" finish "${BASH_SOURCE[0]}" "$FINAL_PASS_START" "$1" "${FINAL_FEATURES:-}" || true
    FINAL_PASS_START=""
}
STEP_RECORD_ACTIVE=0
STEP_RECORD_PATH=""
STEP_STATE_PATH=""
STEP_RECORD_TEXT=""
STEP_RUNS_ON=natedev_only
STEP_TARGET_TABLE=""
HOLD_LINT=0
STEP_DEFER_LOCAL=0
STEP_FORCE_MAC=0

# Status 101 permits repairs for lint findings and compile errors.
# A kill or a lint hold prevents repairs.
first_judgment_permits_repair() {
    [[ "$HOLD_LINT" == 0 && "$1" == 101 && "${RUN_LAST_ATTEMPT_KILLED:-0}" == 0 ]] && ! grep -qE "process didn.t exit successfully:.*signal:" "$2"
}

publish_step_state() {
    local temporary
    if ! printf '%s' "$STEP_RECORD_TEXT" | cmp -s - "$STEP_RECORD_PATH"; then
        printf 'step record: cannot update step record: record missing or changed; step refused\n' >&2
        return 2
    fi
    temporary="${STEP_STATE_PATH%/*}/.step-${STEP_STATE_PATH##*/}-$RANDOM"
    if ! { printf '%s\n' "$*" > "$temporary" && mv -f -- "$temporary" "$STEP_STATE_PATH"; }; then
        rm -f -- "$temporary"
        return 2
    fi
}

step_state() {
    [[ "$STEP_RECORD_ACTIVE" == 1 ]] || return 0
    if [[ "${STEP_WRITES_TREE:-0}" == 1 ]]; then
        publish_step_state writes_tree "$@" || exit 2
    else
        publish_step_state "$@" || exit 2
    fi
}

remove_step_record() {
    if [[ "$STEP_RECORD_ACTIVE" == 1 ]]; then
        rm -f -- "$STEP_RECORD_PATH" "$STEP_STATE_PATH" "${STEP_STATE_PATH%/*}/.step-${STEP_STATE_PATH##*/}-"* || true
        STEP_RECORD_ACTIVE=0
    fi
}

# One classifier preserves signal retries and checks the failed crate for silent exits.
memory_kill_in_journal() {
    local started=$1 ended=$2 log=$3
    "$PY" "$VERIFY_START_DIR/memory_kill.py" "$started" "$ended" "$log"
}

step_was_killed_for_memory() {
    local log=$1 started=$2 ended=$3
    memory_kill_in_journal "$started" "$ended" "$log"
}

step_writes_tree() {
    if [[ "${1:-}" == env ]]; then
        shift
        while [[ "${1:-}" == *=* || "${1:-}" == -u ]]; do
            if [[ "$1" == -u ]]; then shift 2; else shift; fi
        done
    fi
    [[ "${1:-}" == cargo ]] || return 1
    shift
    if [[ "${1:-}" == +* ]]; then shift; fi
    case "${1:-}" in
        fmt) return 0 ;;
        mend|clippy)
            shift
            while (( $# )); do
                [[ "$1" != -- ]] || break
                [[ "$1" != --fix ]] || return 0
                shift
            done
            ;;
    esac
    return 1
}

finish_step_token() {
    local held_before=$1 status=$2
    if [[ "${TOKEN_USED:-0}" == 1 ]]; then
        sync_token_state
        if [[ "$held_before" == 0 ]] && ! release_token; then
            (( status != 0 )) || status=1
        fi
    fi
    return "$status"
}

run() {
    if [[ "${TOKEN_USED:-0}" == 1 ]]; then sync_token_state; fi
    local held_before=${TOKEN_HELD:-0} status=0
    run_step "$@" || status=$?
    finish_step_token "$held_before" "$status" || status=$?
    if [[ "${STEP_RECORD_ACTIVE:-0}" == 1 ]] && step_writes_tree "$@"; then
        step_state local_noncompiling
    fi
    if [[ "$status" != 0 && -n "${BUILDLOG_CHECK_FAILED_FILE:-}" && -f "$BUILDLOG_CHECK_FAILED_FILE" ]]; then
        exit "$status"
    fi
    return "$status"
}

run_step() {
    if [[ "${STEP_ELIGIBILITY_READY:-0}" == 2 ]]; then
        local eligibility_answer eligibility_status=0 probe_mark
        if [[ -f "$HOLD_WAIT_DIR/probe-marks" ]]; then
            while IFS= read -r probe_mark; do
                [[ -f "$probe_mark" ]] || continue
                eligibility_answer=$("$PY" "$BUILD_TARGETS_SCRIPT" "${STEP_ELIGIBILITY_ARGUMENTS[@]}") || eligibility_status=$?
                if (( eligibility_status != 0 )); then return "$eligibility_status"; fi
                STEP_TARGET_TABLE=${eligibility_answer#*$'\n'}
                break
            done < "$HOLD_WAIT_DIR/probe-marks"
        fi
    elif [[ "${STEP_ELIGIBILITY_READY:-0}" == 1 ]]; then
        STEP_ELIGIBILITY_READY=2
    fi
    # A reservation for this call must not cover the next step's local waits.
    if [[ -n "${HOLD_WAIT_DIR:-}" && -s "$HOLD_WAIT_DIR/ssh-reservation" ]]; then
        release_wait_claim
    fi
    local attempt status started ended step_log monitor=0
    local STEP_WRITES_TREE=0
    if step_writes_tree "$@"; then
        STEP_WRITES_TREE=1
        # Keep the call's decision for the next step.
        if [[ "${STEP_RECORD_ACTIVE:-0}" == 1 ]]; then
            step_state local_noncompiling
        fi
        # Both queue hooks must keep this step in the original tree.
        local LINT_QUEUE_LEAVE_HOOK=false BUILDLOG_MAC_IDLE_HOOK=false
        export LINT_QUEUE_LEAVE_HOOK BUILDLOG_MAC_IDLE_HOOK
    fi
    RUN_LAST_ATTEMPT_KILLED=0
    step_log="$(mktemp "${HOLD_WAIT_DIR:-${TMPDIR:-/tmp}}/step-output.XXXXXX")"
    for attempt in 1 2; do
        if [[ -f "${HOLD_WAIT_DIR:-/nonexistent}/lint-held" ]]; then HOLD_LINT=1; fi
        if buildlog_step_compiles "$@"; then
            route_compiling_step "$@" || { status=$?; rm -f "$step_log"; return "$status"; }
            if [[ "$STEP_RAN_ON_MAC" == 1 ]]; then rm -f "$step_log"; return 0; fi
        fi
        if buildlog_step_compiles "$@"; then
            step_state on_natedev
        else
            acquire_token local || { status=$?; rm -f "$step_log"; return "$status"; }
            record_token_wait
            step_state local_noncompiling
        fi
        export BUILDLOG_TARGET=natedev
        started="$(date '+%Y-%m-%d %H:%M:%S %z')"
        monitor=0
        [[ $- != *m* ]] || monitor=1
        set -m
        (
            set +m
            if [[ "$CMD" != test && "$STEP_WRITES_TREE" == 0 ]] && step_can_retry && buildlog_step_compiles "$@"; then
                export LINT_QUEUE_LEAVE_HOOK="$(ssh_retry_hook)"
            fi
            if (
                step_token_before=${TOKEN_HELD:-0}
                # The pipeline shell owns tokens taken during memory admission.
                trap 'status=$?; finish_step_token "$step_token_before" "$status"; exit $?' EXIT
                run_once "$@"
            ) 2>&1 | tee "$step_log" ${CLIPPY_LOG:+"$CLIPPY_LOG"} ${MEND_LOG:+"$MEND_LOG"}; then
                exit 0
            else
                exit "${PIPESTATUS[0]}"
            fi
        ) &
        RUN_STEP_PID=$!
        if [[ "$monitor" == 0 ]]; then set +m; fi
        # The Mac copy watcher (copy_watch.py) reads the step's group and log here.
        if [[ -n "${VERIFY_STEP_INFO_FILE:-}" ]]; then
            printf 'pgid=%s\nlog=%s\n' "$RUN_STEP_PID" "$step_log" > "$VERIFY_STEP_INFO_FILE.new"
            mv -f "$VERIFY_STEP_INFO_FILE.new" "$VERIFY_STEP_INFO_FILE"
        fi
        if wait "$RUN_STEP_PID"; then
            status=0
        else
            status=$?
        fi
        RUN_STEP_PID=""
        if [[ -n "${TREE_WATCH_DIR:-}" ]]; then cancel_tree_run; fi
        step_state local_noncompiling
        if [[ "$status" == 0 ]]; then
            RUN_LAST_ATTEMPT_KILLED=0
            rm -f "$step_log"
            return 0
        fi
        # A repairable tool failure may continue a lint pass. A failed check
        # must end the call even when that tool's caller handles failures.
        if [[ -n "${BUILDLOG_CHECK_FAILED_FILE:-}" && -f "$BUILDLOG_CHECK_FAILED_FILE" ]]; then
            rm -f "$step_log"
            return "$status"
        fi
        # A Mac copy that finished first stopped this step: no memory retry.
        if [[ -n "${VERIFY_STEP_INFO_FILE:-}" && -e "$VERIFY_STEP_INFO_FILE.stopped" ]]; then
            rm -f "$step_log"
            return "$status"
        fi
        if [[ "$status" == "$LINT_LEFT_QUEUE_STATUS" && "${LINT_MEND_WAIT:-0}" == 1 ]]; then
            HOLD_LINT=1
            : > "$HOLD_WAIT_DIR/lint-held"
            rm -f "$step_log"
            return "$status"
        fi
        if buildlog_step_compiles "$@" && { { [[ "$status" == "$LINT_LEFT_QUEUE_STATUS" && "$CMD" != test ]] && step_can_retry; } || step_stopped_by_hold; }; then
            if [[ "$status" == "$LINT_LEFT_QUEUE_STATUS" ]]; then
                STEP_FORCE_MAC=1
            else
                echo 'verify.sh: natedev hold stopped this step; choosing its target again.' >&2
            fi
            route_compiling_step "$@" || { status=$?; rm -f "$step_log"; return "$status"; }
            if [[ "$STEP_RAN_ON_MAC" == 1 ]]; then rm -f "$step_log"; return 0; fi
            status=0
            run "$@" || status=$?
            rm -f "$step_log"
            return "$status"
        fi
        ended="$(date '+%Y-%m-%d %H:%M:%S %z')"
        RUN_LAST_ATTEMPT_KILLED=0
        if grep -qaE 'SIGKILL|signal: 9' "$step_log"; then
            RUN_LAST_ATTEMPT_KILLED=1
        fi
        if ! step_was_killed_for_memory "$step_log" "$started" "$ended"; then
            rm -f "$step_log"
            return "$status"
        fi
        RUN_LAST_ATTEMPT_KILLED=1
        if [[ -n "${MEM_KILL_FILE:-}" ]]; then
            printf x >> "$MEM_KILL_FILE"
        fi
        if (( attempt == 2 )); then
            [[ -z "${MEM_KILL_FILE:-}" ]] || : > "${MEM_KILL_FILE}.stopped"
            echo "killed for memory twice: $*" >&2
            rm -f "$step_log"
            return 137
        fi
        echo "killed for memory: $*; retrying once" >&2
        : > "$step_log"
    done
}

DEV_FEATURES_PY='
from pathlib import Path
import sys
import tomllib

try:
    with Path(sys.argv[1]).open("rb") as source:
        value = tomllib.load(source)
    for key in ("workspace", "metadata", "dev"):
        if not isinstance(value, dict):
            raise ValueError("expected a table")
        value = value.get(key, {})
    if not isinstance(value, dict):
        raise ValueError("expected a table")
    features = value.get("features", [])
    if not isinstance(features, list) or any(not isinstance(name, str) for name in features):
        raise ValueError("expected a list of strings")
except FileNotFoundError:
    features = []
except (OSError, ValueError):
    print("verify.sh: workspace.metadata.dev.features must be a list of strings", file=sys.stderr)
    sys.exit(2)
print(",".join(features))
if len(sys.argv) > 2 and sys.argv[2] in ("test", "final"):
    with open(Path(sys.argv[3]).resolve().parents[2] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
    from scripts.direct.result_environment import read_policy
    policy = read_policy()
    print(" ".join(policy.forwarded_names))
    print(" ".join(policy.prefixes))
    print(" ".join(name + "=" + value for name, value in policy.fixed))
'

# Workspace members with a file that differs from HEAD: staged, unstaged,
# deleted, or new and not ignored. A phase commits nothing before its
# checkpoint, so HEAD is where the phase started and this is the whole phase's
# work, not its last edit. A file belongs to the member whose folder holds it,
# the deepest one when members nest; a file outside every member (the root
# manifest, Cargo.lock, docs/) adds none. Used only for fmt scope.
CHANGED_MEMBERS_PY='
import json
import os
import subprocess
import sys


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        print("verify.sh: git " + " ".join(args) + " failed: " + result.stderr.strip(), file=sys.stderr)
        sys.exit(2)
    return result.stdout


meta = json.load(sys.stdin)
members = set(meta["workspace_members"])
roots = sorted(
    (
        (os.path.realpath(os.path.dirname(package["manifest_path"])), package["name"])
        for package in meta["packages"]
        if package["id"] in members
    ),
    key=lambda root: len(root[0]),
    reverse=True,
)
top = os.path.realpath(git("rev-parse", "--show-toplevel").strip())
paths = git("-C", top, "diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0")
paths += git("-C", top, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
changed = set()
for path in filter(None, paths):
    full = os.path.join(top, path)
    for root, name in roots:
        if full.startswith(root + os.sep):
            changed.add(name)
            break
print("\n".join(sorted(changed)))
'

# The pass-record key (see the header). argv is the files whose content counts,
# then `--`, then the call's words. The tree part is HEAD's tree plus the content
# of every path that differs from it, staged, unstaged, deleted, or new and not
# ignored; it writes no git objects. Any failure prints nothing, and an empty
# key turns the record off for that run.
#
# A `test` key (argv starting `--package <name>`, cargo metadata on stdin)
# holds only the paths that package's tests are built from: its own folder and
# those of the members it depends on by path, transitively; every member's
# Cargo.toml, since workspace feature resolution reads them all; and every path
# outside the members (the root manifest, Cargo.lock, .cargo/). Each counts by
# its git mode and blob id, from HEAD or from the file when it differs, so a
# phase that edits or commits another member in the same worktree leaves the
# key alone (two phases side by side, 2026-10-17). A test that reads a file in
# an unrelated member's folder is not keyed on it.
TREE_WATCH_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tree_watch.py"

# The compile covers the workspace, so a misspelled package would otherwise
# pass check and lint silently and leave nextest with an empty filter. Fail it
# as a usage error instead.
read_metadata() {
    if [[ -n "${GATE_METADATA:-}" ]]; then
        printf '%s\n' "$GATE_METADATA"
    else
        cargo metadata --no-deps --format-version 1
    fi
}

set_gate_metadata() {
    export BUILDLOG_OUTPUT_ROOTS=""
    GATE_METADATA="$(cargo metadata --no-deps --format-version 1)" || { GATE_METADATA=""; return 1; }
    BUILDLOG_OUTPUT_ROOTS="$(verify_helper --stdin "$VERIFY_START_DIR/../buildlog/step_context.py" output-roots \
        <<< "$GATE_METADATA" 2>/dev/null)" || BUILDLOG_OUTPUT_ROOTS=""
}

# Read the metadata once. Later requests use the shell values.
# Validate dev features before Cargo. Find changed members after mend.
METADATA_READY=0
load_metadata() {
    [[ "$METADATA_READY" == 0 ]] || return 0
    local values features="" kind="" name="" valid=1
    local requests=() words=("${ARGS[@]:1}")
    local package="${ARGS[0]:-}" index word
    if [[ "${CHECK_UNTESTED_EXAMPLES:-0}" == 1 ]]; then
        requests+=("untested-examples:$EXAMPLE_SCOPE")
    fi
    case "$CMD" in
        check|build|test|final) if [[ -n "$package" ]]; then requests+=("member:$package"); fi ;;
        lint)
            if [[ -n "$package" ]]; then requests+=("member:$package"); fi
            requests+=(workspace-root)
            ;;
        example) if [[ -n "$package" ]]; then requests+=("member:$package"); fi; if [[ -n "$EXAMPLE_NAME" ]]; then requests+=("example-features:$package:$EXAMPLE_NAME"); fi ;;
        doc) if [[ -n "$package" ]]; then requests+=("member:$package"); fi; requests+=(target-directory) ;;
    esac
    case "$CMD" in
        check) requests+=(check-targets) ;;
        test)
            for ((index=0; index<${#words[@]}; index++)); do
                if [[ "${words[index]}" == --features ]]; then
                    features="${words[index+1]:-}"
                fi
            done
            features="$(merge_dev_features "$features")"
            features="$(qualify_features "$package" "$features")"
            requests+=("test-targets:$package:$features")
            ;;
        build)
            # Match take_build_args without emitting its usage error early:
            # the existing member check still wins over malformed arguments.
            while [[ ${#words[@]} -gt 0 ]]; do
                word=${words[0]}
                case "$word" in
                    --bin|--example)
                        if [[ -n "$kind" || -z "${words[1]:-}" || "${words[1]:-}" == --* ]]; then
                            valid=0; break
                        fi
                        kind=${word#--}; name=${words[1]}; words=("${words[@]:2}")
                        ;;
                    --release) words=("${words[@]:1}") ;;
                    --features)
                        if [[ -z "${words[1]:-}" ]]; then valid=0; break; fi
                        words=("${words[@]:2}")
                        ;;
                    *) valid=0; break ;;
                esac
            done
            if [[ "$valid" == 1 ]]; then requests+=("build-targets:$package:$kind:$name"); fi
            ;;
    esac
    if [[ -z "${GATE_METADATA:-}" ]]; then set_gate_metadata || exit 2; fi
    values="$(verify_helper --stdin "$VERIFY_METADATA_SCRIPT" "${requests[@]}" <<< "$GATE_METADATA")" || exit 2
    eval "$values"
    METADATA_READY=1
}

require_member() {
    load_metadata
}

require_taplo() {
    if ! command -v taplo >/dev/null 2>&1; then
        echo "taplo is required" >&2
        exit 2
    fi
}

workspace_taplo() (
    # Explicit paths retain Taplo's include/exclude rules and omit untracked files.
    cd "$WORKSPACE_ROOT" || return
    local listing file pattern character index
    local -a files=()
    listing="$(mktemp)" || return
    if ! git ls-files -z -- '*.toml' > "$listing"; then
        rm -f "$listing"
        return 1
    fi
    while IFS= read -r -d '' file; do
        # Taplo expands arguments as globs, even after --. Select each literal path.
        pattern=""
        for ((index=0; index<${#file}; index++)); do
            character=${file:index:1}
            case "$character" in
                '['|']'|'*'|'?') pattern+="[$character]" ;;
                *) pattern+="$character" ;;
            esac
        done
        files+=("$pattern")
    done < "$listing"
    rm -f "$listing"
    # No paths means a repository scan to Taplo, so skip the empty set.
    (( ${#files[@]} )) || return 0
    local status=0
    run taplo fmt "$@" -- "${files[@]}" || status=$?
    if [[ "$status" != 0 && "${1:-}" == --check ]]; then
        echo 'verify.sh: TOML check failed; files can be invalid or not properly formatted.' >&2
    fi
    return "$status"
)

example_features() {
    load_metadata
    printf '%s\n' "$EXAMPLE_FEATURES"
}

TEST_SELECTION=()
take_test_targets() {
    local word
    load_metadata
    while IFS= read -r word; do
        if [[ -n "$word" ]]; then
            TEST_SELECTION+=("$word")
        fi
    done <<< "$TEST_TARGETS"
}

CHECK_SELECTION=()
take_check_targets() {
    local word
    load_metadata
    while IFS= read -r word; do
        if [[ -n "$word" ]]; then
            CHECK_SELECTION+=("$word")
        fi
    done <<< "$CHECK_TARGETS"
}

# Qualify each feature name in a comma list with the package, leaving names
# that already carry a `/` (dep/feature, member/feature) as written. Under
# --workspace an unqualified name turns the feature on in every selected member
# that defines one by that name, and test-support and test each exist in two.
qualify_features() {
    local pkg="$1" list="$2" name out=""
    local IFS=,
    for name in $list; do
        [[ -z "$name" ]] && continue
        [[ -z "$pkg" || "$name" == */* ]] || name="$pkg/$name"
        if [[ -n "${DEV_FEATURES:-}" ]]; then
            case ",$out," in *",$name,"*) continue ;; esac
        fi
        out="${out:+$out,}$name"
    done
    printf '%s' "$out"
}

# With no dev list, retain the caller's exact feature list.
merge_dev_features() {
    local list="$1" name out="" dev="${DEV_FEATURES:-}"
    if [[ -z "$dev" ]]; then
        printf '%s' "$list"
        return
    fi
    local IFS=,
    for name in ${list:+$list,}$dev; do
        [[ -z "$name" ]] && continue
        case ",$out," in *",$name,"*) continue ;; esac
        out="${out:+$out,}$name"
    done
    printf '%s' "$out"
}

# Optional trailing Work Order features on check and test join the repo list.
# Any other leftover argument is a usage error. The caller sets PKG first.
FEATURE_FLAGS=()
take_features() {
    local features=""
    if [[ $# -gt 0 && "$1" == "--features" && -n "${2:-}" ]]; then
        features=$2
        shift 2
    fi
    if [[ $# -ne 0 ]]; then
        usage
        exit 2
    fi
    features="$(merge_dev_features "$features")"
    if [[ -n "$features" ]]; then
        FEATURE_FLAGS=(--features "$(qualify_features "$PKG" "$features")")
    fi
}

# `build` arguments: at most one of --bin or --example, --release, and
# --features in any order. An example's required features join the list.
BUILD_ARGS=()
BUILD_BINARIES=()
BUILD_PROFILE_DIR=debug
BUILD_TARGET_DIR=""
take_build_args() {
    local kind="" name="" features="" line words
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --bin|--example)
                if [[ -n "$kind" || -z "${2:-}" || "${2:-}" == --* ]]; then
                    usage
                    exit 2
                fi
                kind=${1#--}
                name=$2
                shift 2
                ;;
            --release)
                BUILD_PROFILE_DIR=release
                shift
                ;;
            --features)
                if [[ -z "${2:-}" ]]; then
                    usage
                    exit 2
                fi
                features=$2
                shift 2
                ;;
            *)
                usage
                exit 2
                ;;
        esac
    done
    words="$BUILD_TARGETS"
    while IFS= read -r line; do
        case "$line" in
            "select "*) BUILD_ARGS+=("${line#select }") ;;
            "features "*) features="${features:+$features,}${line#features }" ;;
            "binary "*) BUILD_BINARIES+=("${line#binary }") ;;
            "target "*) BUILD_TARGET_DIR="${line#target }" ;;
        esac
    done <<< "$words"
    if [[ "$BUILD_PROFILE_DIR" == release ]]; then
        BUILD_ARGS+=(--release)
    else
        features="$(merge_dev_features "$features")"
    fi
    if [[ -n "$features" ]]; then
        BUILD_ARGS+=(--features "$(qualify_features "$PKG" "$features")")
    fi
}

# One `binary: <path>` line per binary the build made, for the caller to launch.
print_built_binaries() {
    local binary path
    for binary in "${BUILD_BINARIES[@]}"; do
        path="${BUILD_TARGET_DIR}/${BUILD_PROFILE_DIR}/${binary}"
        if [[ "$STEP_RAN_ON_MAC" == 1 ]]; then
            printf 'binary: %s:%s/%s/%s\n' "$BUILDLOG_TARGET" "$REMOTE_BUILD_TARGET_DIR" "$BUILD_PROFILE_DIR" "$binary"
        elif [[ -x "$path" ]]; then
            echo "binary: $path"
        else
            echo "verify.sh: cargo build passed but made no ${path}" >&2
            exit 1
        fi
    done
}

NEXTEST_ARGS=()
compose_nextest_args() {
    local ignored_only=0
    set -- "${ARGS[@]}"
    PKG="${1:-}"
    shift
    TARGET=""
    FILTERS=()
    # --filter, as often as named, and --features in any order; a target
    # takes no filter.
    REST=()
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == --test && -z "$TARGET" && ${#FILTERS[@]} -eq 0 && "$ignored_only" == 0 && -n "${2:-}" ]]; then
            TARGET=$2
            shift 2
        elif [[ "$1" == "--filter" && -z "$TARGET" \
              && "${2:-}" =~ ^[A-Za-z0-9_:]+$ ]]; then
            FILTERS+=("$2")
            shift 2
        elif [[ "$1" == --run-ignored && "${2:-}" == only && -z "$TARGET" ]]; then
            ignored_only=1
            shift 2
        else
            REST+=("$1")
            shift
        fi
    done
    take_features ${REST[@]+"${REST[@]}"}
    require_member "$PKG"
    # --no-fail-fast: nextest cancels every remaining test after the first
    # failure, so one broken test silently hides the rest of the suite. A
    # phase gate has to report the whole result, not the first stop.
    # Keep workspace feature resolution; -E runs only this package's tests.
    if [[ ${#FILTERS[@]} -gt 0 ]]; then
        # Any of the names: & binds tighter than |, so a union of two or
        # more goes in parentheses.
        FILTER="test(${FILTERS[0]})"
        for name in "${FILTERS[@]:1}"; do
            FILTER+=" | test($name)"
        done
        if [[ ${#FILTERS[@]} -gt 1 ]]; then
            FILTER="($FILTER)"
        fi
        TEST_FILTER="${PKG:+package($PKG) & }$FILTER"
    elif [[ -n "$TARGET" ]]; then
        # Integration test target names are unique across the workspace,
        # so --test builds just that binary, under the workspace's
        # feature resolution.
        NEXTEST_ARGS=(--no-fail-fast --workspace --test "$TARGET")
        if [[ -n "$PKG" ]]; then NEXTEST_ARGS+=(-E "package($PKG)"); fi
        NEXTEST_ARGS+=("${FEATURE_FLAGS[@]}")
        return
    else
        TEST_FILTER="${PKG:+package($PKG)}"
    fi
    # Build only the targets this package's tests live in
    # (verify_metadata.py): --lib, which cannot be narrowed to one member
    # under --workspace and is left out for a package without a lib,
    # and the package's own bins and test-enabled targets. --bins
    # --tests linked every member's test executables, 78 in hana, to
    # run one package's; this links 4 (2026-10-04). The integration
    # tests stay in so a phase runs the package's integration tests
    # alongside its unit tests. The lint half's clippy checks all
    # workspace targets with all features.
    take_test_targets
    NEXTEST_ARGS=(--no-fail-fast --workspace "${TEST_SELECTION[@]}")
    if [[ -n "$TEST_FILTER" ]]; then NEXTEST_ARGS+=(-E "$TEST_FILTER"); fi
    NEXTEST_ARGS+=("${FEATURE_FLAGS[@]}")
    if [[ "$ignored_only" == 1 ]]; then
        NEXTEST_ARGS+=(--run-ignored only)
    fi
    # Filters and ignored-test selections must match tests.
    if [[ ${#FILTERS[@]} -eq 0 && "$ignored_only" == 0 ]]; then
        NEXTEST_ARGS+=(--no-tests=warn)
    fi
}

VERIFY_ORIGINAL_ARGS=("$@")
# A flag, not an env prefix, so a call stays one plain command that the
# settings.json allow rule matches.
if [[ "${1:-}" == "--session-dir" ]]; then
    export UNIT_SESSION_DIR="${2:?verify.sh --session-dir <dir> <command> ...}"
    shift 2
    # Test temp folders land in the run folder and end with it (tmp_sweep.py judges each entry).
    verify_helper --start
    SESSION_TMPDIR="$(verify_helper "$BUILD_FOLDERS_SCRIPT" run-temp-path "$UNIT_SESSION_DIR")"
    # Other worker commands use the session TMPDIR after this verify process exits.
    if [[ -d "$SESSION_TMPDIR" ]] || verify_helper "$BUILD_FOLDERS_SCRIPT" stable --run-temp "$UNIT_SESSION_DIR" >/dev/null 2>&1; then
        export TMPDIR="$SESSION_TMPDIR"
    fi
fi

CMD="${1:-}"
case "$CMD" in
    check|test|lint|doc|build|example|final|example-test) ;;
    *) usage; exit 2 ;;
esac

# A native worker's command dies with its turn, and a worker that waits on
# one spends a model call per wait. So a worker's call detaches: detached_runs.py
# runs this same call under a supervisor outside the worker's process group,
# returns at once, and sends the result to the worker as its next message.
# Exit 3 means the caller is not a registered worker; this call then runs attached.
# Exit 4 refuses the run at its live-run limit; never run that command attached.
# The detached run sets UNIT_DETACHED_RUN, so it does not detach again.
DETACHED_RUNS_SCRIPT="${VERIFY_DETACHED_RUNS_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../agents" && pwd)/detached_runs.py}"
if [[ -z "${UNIT_DETACHED_RUN:-}" && -n "${CODEX_THREAD_ID:-}" ]]; then
    VERIFY_SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
    DETACH_STATUS=0
    "$PY" "$DETACHED_RUNS_SCRIPT" start --label "verify.sh ${VERIFY_ORIGINAL_ARGS[*]-}" -- \
        bash "$VERIFY_SELF" ${VERIFY_ORIGINAL_ARGS[@]+"${VERIFY_ORIGINAL_ARGS[@]}"} || DETACH_STATUS=$?
    case "$DETACH_STATUS" in
        0) exit 0 ;;
        3) ;;
        4) exit 4 ;;
        *) echo "verify.sh: detached_runs.py could not detach this run (exit $DETACH_STATUS); running attached" >&2 ;;
    esac
fi

# Set the run identity after detachment so the run owns its queue place.
verify_helper --start
if [[ "${OSTYPE:-}" != darwin* && -z "${BUILDLOG_RUN+x}" ]]; then
    BUILDLOG_RUN_START="$(verify_helper "$STEP_RECORD_SCRIPT" start "$$")"
    if [[ -n "$BUILDLOG_RUN_START" ]]; then
        export BUILDLOG_RUN="$$:$BUILDLOG_RUN_START"
    fi
fi

shift
if [[ "$CMD" == example-test ]]; then
    echo 'verify.sh: example-test is removed: examples carry no tests (user, 2026-10-04).' >&2
    exit 2
fi


parse_routing_arguments "$@"
parse_command_arguments "${ARGS[@]}"
set -- ${COMMAND_ARGS[@]+"${COMMAND_ARGS[@]}"}
if [[ "$CMD" == final && -z "$VERIFY_PACKAGE" && -z "$PINNED_TARGET" && "$MAC_ONLY" == 0 && "$LOCAL_ONLY" == 0 ]]; then
    FINAL_PASS_START="$("$PY" "$FINAL_PASS_SCRIPT" start "${BASH_SOURCE[0]}")"
    trap 'final_status=$?; final_pass_finish "$final_status"; exit "$final_status"' EXIT
fi
VERIFY_SETUP="$(VERIFY_PY_HELPER=DEV_FEATURES_PY "$PY" -c "$DEV_FEATURES_PY" "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/Cargo.toml" "$CMD" "$VERIFY_START_DIR/result_environment.py")" || exit 2
DEV_FEATURES="${VERIFY_SETUP%%$'\n'*}"
VERIFY_TEST_ENVIRONMENT=()
VERIFY_USER_ENVIRONMENT=( ${VERIFY_MAC_ENVIRONMENT[@]+"${VERIFY_MAC_ENVIRONMENT[@]}"} )
if [[ "$CMD" == test || "$CMD" == final ]]; then
    {
        IFS= read -r VERIFY_RESULT_ENV_NAMES || true
        IFS= read -r VERIFY_RESULT_ENV_PREFIXES || true
        IFS= read -r VERIFY_TEST_FIXED_ENV || true
    } <<< "${VERIFY_SETUP#*$'\n'}"
    for VERIFY_ENV_ENTRY in "${VERIFY_CALL_ENVIRONMENT[@]}"; do
        VERIFY_ENV_NAME=${VERIFY_ENV_ENTRY%%=*}
        VERIFY_ENV_SELECTED=0
        for VERIFY_ENV_MATCH in $VERIFY_RESULT_ENV_NAMES; do
            if [[ "$VERIFY_ENV_NAME" == "$VERIFY_ENV_MATCH" ]]; then VERIFY_ENV_SELECTED=1; fi
        done
        for VERIFY_ENV_MATCH in $VERIFY_RESULT_ENV_PREFIXES; do
            if [[ "$VERIFY_ENV_NAME" == "$VERIFY_ENV_MATCH"* ]]; then VERIFY_ENV_SELECTED=1; fi
        done
        if [[ "$VERIFY_ENV_SELECTED" == 1 ]]; then VERIFY_TEST_ENVIRONMENT+=("$VERIFY_ENV_ENTRY"); fi
    done
    for VERIFY_ENV_ENTRY in $VERIFY_TEST_FIXED_ENV; do
        VERIFY_TEST_ENVIRONMENT+=("$VERIFY_ENV_ENTRY")
    done
fi
if [[ "$CMD" == test ]]; then
    for VERIFY_ENV_ENTRY in ${VERIFY_TEST_ENVIRONMENT[@]+"${VERIFY_TEST_ENVIRONMENT[@]}"}; do export "$VERIFY_ENV_ENTRY"; done
fi
if [[ "$LOCAL_ONLY" -eq 1 ]]; then
    export BUILDLOG_LOCAL_REASON="$LOCAL_REASON"
else
    unset BUILDLOG_LOCAL_REASON
fi

# Every step run() runs below carries this call's id, which buildlog joins to
# the call record note_event writes.
export BUILDLOG_CALLER=verify
BUILDLOG_CALL_ID="v$(date -u +%Y%m%dT%H%M%S)-$$-$RANDOM"
# Children carry this id only after its live record exists.
export -n BUILDLOG_CALL_ID
# The newest call wins a Mac copy (copy_watch.py); bash 3.2 has no EPOCHREALTIME.
VERIFY_CALL_STARTED="${EPOCHREALTIME:-$(date +%s)}"
export BUILDLOG_HOLD_PID=$$
export BUILDLOG_HOLD_WAIT_S=0
HOLD_WAIT_DIR=""
HOLD_WAITER_PID=""
RUN_STEP_PID=""
ROUTING_WAIT_STARTED=""

# Each blocked resource interval has its own recorder id. Files carry ids from
# step subshells to the parent cleanup, which also ends interrupted waits.
PHASE_WAIT_SESSION="${UNIT_RUN_DIR:-${UNIT_SESSION_DIR:-}}"
PHASE_WAIT_RECORDER="$(cd "${BASH_SOURCE[0]%/*}" && pwd)/progress_history.py"
PHASE_TOKEN_OBSERVATION=""
PHASE_TOKEN_HOLDER=""
phase_wait_holder_flags() {
    local record_script="${STEP_RECORD_SCRIPT:-$PHASE_WAIT_RECORDER}"
    VERIFY_PY_HELPER=PHASE_WAIT_HOLDER "$PY" - "$1" "${2:-}" \
        "${BOARD_DIR:-$PHASE_WAIT_SESSION}" "${BUILD_HOLD_DIR-$HOME/.local/state/build-hold}" \
        "$record_script" "${DOC_REPO_TOP:-$PWD}" <<'PY'
import json
import os
from pathlib import Path
import sys
from uuid import UUID

kind, payload, board_name, hold_name, recorder, repository = sys.argv[1:]
with open(Path(recorder).resolve().parents[2] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})


def text(value):
    return value.strip() if isinstance(value, str) and not any(c in value for c in "\r\n\t\0") else ""


def read(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def fields(raw):
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


owner, owner_flag = "", "--holder-session"
resource_kind, resource = "slot", "cargo"
if kind == "token":
    from scripts.production import staff
    from scripts.lib import unit_runs

    board = Path(board_name)
    if payload == "director":
        owner = unit_runs.director(board)
    elif payload and payload != "unknown":
        matches = []
        if not matches:
            for path in board.glob("impl_worker_*.json"):
                worker = fields(read(path))
                if (text(worker.get("slot")) == payload
                        or path.stem == "impl_worker_" + payload):
                    matches.append(worker)
        state = fields(read(board / "progress_history_state.json"))
        if matches:
            reference = text(matches[0].get("staff_id")) if len(matches) == 1 else ""
        else:
            active_pass = state.get("pass", {})
            worker = active_pass.get(payload, {}) if isinstance(active_pass, dict) else {}
            if isinstance(active_pass, dict) and payload not in active_pass:
                workers = [entry for entry in active_pass.values() if isinstance(entry, dict)
                           and entry.get("worker_name") == payload]
                worker = workers[0] if len(workers) == 1 else {}
            reference = text(worker.get("worker_id")) if isinstance(worker, dict) else ""
        try:
            record = staff.snapshot()
            member = record.by_id(staff.StaffId(reference)) if reference else None
            if member is not None:
                owner = member.current_session
            if not owner:
                unit = unit_runs.run_owner(state, record)
                owner_flag = "--holder-unit"
                owner = unit.name if isinstance(unit, staff.StaffRow) else ""
        except (OSError, ValueError):
            pass
elif kind == "hold":
    from scripts.build_hold import build_hold
    from scripts.build_targets import build_targets

    resource = "natedev"
    directory = Path(hold_name)
    holder = build_targets.hold_waited_on(build_hold.natedev_holds(directory),
                                        build_hold.owning_session(), directory)
    if isinstance(holder, build_hold.Holder):
        session = build_hold.holder_session(holder.key)
        try:
            UUID(session)
            owner = session
        except ValueError:
            owner_flag, owner = "--holder-unit", text(holder.name)
elif kind in ("mac", "target"):
    from scripts.mac_test import mac_test

    resource = payload or "mac"
    paths = mac_test.state_paths("" if resource == "mac" else resource)
    scope = mac_test.target_claim_scope(paths, repository)
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for claim in mac_test.read_claims(paths):
        if mac_test.scopes_overlap(claim.scope, scope) and mac_test.run_is_live(claim.record):
            candidate = text(claim.record.get("holder_session"))
            if candidate and candidate != session:
                owner = candidate
                break
elif kind == "memory":
    resource_kind, resource = "memory", "memory"
    holder = fields(payload)
    if holder.get("kind") == "session":
        owner = text(holder.get("session_id"))
    elif holder.get("kind") == "unit":
        owner_flag, owner = "--holder-unit", text(holder.get("unit"))
if owner:
    print("\n".join(("--holder-resource-kind", resource_kind, "--holder-resource", resource, owner_flag, owner)))
PY
}
phase_wait_target_busy() {
    local holder
    holder="$(phase_wait_holder_flags target "$1")" || return 1
    [[ -n "$holder" ]]
}
phase_wait_start() {
    local key=$1 reason=$2 id file holder="" holder_file previous="" flag
    [[ "${CLAUDE_PROBE_SESSION:-}" != 1 && -n "$PHASE_WAIT_SESSION" \
        && -f "$PHASE_WAIT_SESSION/progress_history_state.json" && -n "$HOLD_WAIT_DIR" ]] || return 0
    file="$HOLD_WAIT_DIR/phase-wait.${BASHPID:-$$}.$key"
    holder_file="$HOLD_WAIT_DIR/phase-holder.${BASHPID:-$$}.$key"
    if [[ "$key" == memory && -z "${3:-}" ]]; then
        local pending
        for pending in "$HOLD_WAIT_DIR"/phase-close.*.memory.*; do
            [[ -s "$pending" ]] || continue
            read -r id < "$pending"
            "$PY" "$PHASE_WAIT_RECORDER" finish-wait --session-dir "$PHASE_WAIT_SESSION" --wait-id "$id" \
                || printf 'verify.sh: wait history NotRecorded: cannot close %s\n' "$id" >&2
            rm -f "$pending"
        done
    fi
    local holder_kind=$key payload="${3:-}" observation="" slot="" holder_pid=""
    if [[ "$key" == token ]]; then
        if [[ -f "$BOARD_DIR/locks/cargo.d/holder" ]]; then
            IFS= read -r slot < "$BOARD_DIR/locks/cargo.d/holder" || true
        fi
        if [[ -f "$BOARD_DIR/locks/cargo.d/holder_pid" ]]; then
            IFS= read -r holder_pid < "$BOARD_DIR/locks/cargo.d/holder_pid" || true
        fi
        observation="$slot:$holder_pid"
        if [[ -z "$slot" || "$slot" == unknown ]]; then
            PHASE_TOKEN_OBSERVATION="" PHASE_TOKEN_HOLDER=""
        elif [[ "${PHASE_TOKEN_OBSERVATION:-}" == "$observation" && -n "${PHASE_TOKEN_HOLDER:-}" ]]; then
            holder="${PHASE_TOKEN_HOLDER:-}"
        else
            if ! holder="$(phase_wait_holder_flags token "$slot")"; then
                printf 'verify.sh: wait history NotRecorded: %s\n' "$reason" >&2
                return 0
            fi
            PHASE_TOKEN_OBSERVATION=$observation
            PHASE_TOKEN_HOLDER=$holder
        fi
    elif [[ "$key" != memory || -n "$payload" ]]; then
        if [[ "$reason" == "Mac busy" ]]; then holder_kind=mac; fi
        if ! holder="$(phase_wait_holder_flags "$holder_kind" "$payload")"; then
            printf 'verify.sh: wait history NotRecorded: %s\n' "$reason" >&2
            return 0
        fi
    fi
    local holder_args=()
    if [[ -n "$holder" ]]; then
        while IFS= read -r flag; do holder_args+=("$flag"); done <<< "$holder"
    fi
    if [[ -f "$file" ]]; then
        [[ -s "$file" ]] || return 0
        previous=$(< "$holder_file")
        if [[ "$holder" == "$previous" && ! ( "$key" == memory && -n "$payload" ) ]]; then return 0; fi
        read -r id < "$file"
        if "$PY" "$PHASE_WAIT_RECORDER" wait-holder --session-dir "$PHASE_WAIT_SESSION" \
            --wait-id "$id" ${holder_args[@]+"${holder_args[@]}"}; then
            printf '%s' "$holder" > "$holder_file"
        else
            printf 'verify.sh: wait history NotRecorded: cannot update %s holder\n' "$id" >&2
        fi
        return 0
    fi
    local args=(start-wait --session-dir "$PHASE_WAIT_SESSION"
        --kind WaitingForMemoryOrSlot --reason "$reason")
    local owner="${CODEX_THREAD_ID:-}"
    if [[ -n "$owner" ]]; then
        args+=(--worker-thread-id "$owner" --owner-session "$owner")
    else
        args+=(--director --owner-session "${CLAUDE_CODE_SESSION_ID:-}")
    fi
    if id="$("$PY" "$PHASE_WAIT_RECORDER" "${args[@]}" ${holder_args[@]+"${holder_args[@]}"})"; then
        if [[ -n "$id" ]]; then
            printf '%s' "$holder" > "$holder_file"
            printf '%s\n' "$id" > "$file"
        else
            : > "$file"
            printf 'verify.sh: wait history NotRecorded: %s\n' "$reason" >&2
        fi
    else
        printf 'verify.sh: wait history NotRecorded: %s\n' "$reason" >&2
    fi
}
phase_wait_finish() {
    if [[ "$1" == token ]]; then PHASE_TOKEN_OBSERVATION="" PHASE_TOKEN_HOLDER=""; fi
    local file="$HOLD_WAIT_DIR/phase-wait.${BASHPID:-$$}.$1" id
    if [[ ! -s "$file" ]]; then
        rm -f "$file" "$HOLD_WAIT_DIR/phase-holder.${BASHPID:-$$}.$1"
        return 0
    fi
    read -r id < "$file"
    if "$PY" "$PHASE_WAIT_RECORDER" finish-wait --session-dir "$PHASE_WAIT_SESSION" --wait-id "$id"; then
        rm -f "$file" "$HOLD_WAIT_DIR/phase-holder.${BASHPID:-$$}.$1"
    else
        printf 'verify.sh: wait history NotRecorded: cannot close %s\n' "$id" >&2
        if [[ "$1" == memory ]]; then
            printf '%s\n' "$id" > "$HOLD_WAIT_DIR/phase-close.${BASHPID:-$$}.memory.$id"
            rm -f "$file" "$HOLD_WAIT_DIR/phase-holder.${BASHPID:-$$}.memory"
        fi
    fi
}
phase_wait_finish_all() {
    local file id
    [[ -n "$HOLD_WAIT_DIR" ]] || return 0
    for file in "$HOLD_WAIT_DIR"/phase-wait.* "$HOLD_WAIT_DIR"/phase-close.*; do
        [[ -s "$file" ]] || continue
        read -r id < "$file"
        "$PY" "$PHASE_WAIT_RECORDER" finish-wait --session-dir "$PHASE_WAIT_SESSION" --wait-id "$id" \
            || printf 'verify.sh: wait history NotRecorded: cannot close %s\n' "$id" >&2
    done
}

# Record only blocked admission. An immediate grant creates no wait.
buildlog_memory_event() {
    case "$1" in
        first) phase_wait_start memory "memory admission" ;;
        holder\ *) phase_wait_start memory "memory admission" "${1#holder }" ;;
        done|left) phase_wait_finish memory ;;
    esac
}

collect_routing_wait() {
    phase_wait_finish hold
    if [[ -n "$ROUTING_WAIT_STARTED" ]]; then
        local now=$EPOCHREALTIME elapsed
        elapsed=$(( 10#${now/./} - 10#${ROUTING_WAIT_STARTED/./} ))
        (( elapsed >= 0 )) || elapsed=0
        printf '%d.%06d\n' "$(( elapsed / 1000000 ))" "$(( elapsed % 1000000 ))" >> "$BUILDLOG_HOLD_WAIT_FILE"
        ROUTING_WAIT_STARTED=""
    fi
}

collect_hold_wait() {
    collect_routing_wait
    if [[ -n "${BUILDLOG_HOLD_WAIT_FILE:-}" && -f "$BUILDLOG_HOLD_WAIT_FILE" ]]; then
        BUILDLOG_HOLD_WAIT_S="$(awk '{ total += $1 } END { printf "%.6f", total }' "$BUILDLOG_HOLD_WAIT_FILE")"
        export BUILDLOG_HOLD_WAIT_S
    fi
}

interrupt_hold_wait() {
    if [[ -n "${BUILDLOG_HOLD_WAITER_FILE:-}" && -f "$BUILDLOG_HOLD_WAITER_FILE" ]]; then
        "$PY" "$BUILD_TARGETS_SCRIPT" interrupt-local --call-id "$BUILDLOG_CALL_ID" >/dev/null 2>&1 || true
    fi
}

finish_hold_wait() {
    phase_wait_finish_all
    collect_hold_wait
    if [[ -n "${TOKEN_WAIT_ENTRY:-}" ]]; then rm -f -- "$TOKEN_WAIT_ENTRY"; fi
    if [[ -n "$MAC_IDLE_MARKER" && -f "${MAC_IDLE_SCRIPT:-/nonexistent}" ]]; then
        verify_helper "$MAC_IDLE_SCRIPT" remove "$BUILDLOG_CALL_ID" || true
    fi
    if [[ -n "$HOLD_WAIT_DIR" ]]; then
        rm -rf "$HOLD_WAIT_DIR"
        HOLD_WAIT_DIR=""
    fi
    [[ -z "${VERIFY_HELPER_LOCK:-}" ]] || rm -f -- "$VERIFY_HELPER_LOCK"
}

# Pass records (see the header): on for `test` and `lint` in a
# worker session. --no-cache turns off the lookup; its result is still
# recorded, so a flake hunt that passes clears a recorded lint failure.
CACHE_SESSION_DIR="${UNIT_RUN_DIR:-${UNIT_SESSION_DIR:-}}"
CACHE_DIR=""
if [[ -n "${CACHE_SESSION_DIR}" && ( "$CMD" == test || "$CMD" == lint ) ]]; then
    CACHE_DIR="${CACHE_SESSION_DIR}/verify_cache"
fi
if [[ ${#MAC_ENV_NAMES[@]} -gt 0 || "$MAC_FETCH_GIVEN" == 1 ]]; then CACHE_DIR=""; fi
if [[ "$CMD" == test ]]; then
    for VERIFY_ENV_ENTRY in ${VERIFY_TEST_ENVIRONMENT[@]+"${VERIFY_TEST_ENVIRONMENT[@]}"}; do
        MAC_ENV_NAMES+=("${VERIFY_ENV_ENTRY%%=*}")
        VERIFY_MAC_ENVIRONMENT+=("$VERIFY_ENV_ENTRY")
    done
    export VERIFY_TEST_ENV_NAMES="${MAC_ENV_NAMES[*]}"
fi
RUN_LOG=""
RUN_LOG_PIDS=()
VERDICT_KEY=""
VERDICT_TREE_KEY=""
CLIPPY_FIX_LIMIT_REACHED=0
RUN_STARTED=0
CALL_NOTED=0
TOKEN_WAIT_S=0
TOKEN_WAIT_FILE=""
LOOKUP_KEY=""
LOOKUP_STATUS=0
EXIT_STATUS=0
FILTER_RUN=0
BUILDLOG_MAC=""
BUILDLOG_MAC_HOLDER=""
BUILDLOG_MAC_REASON=""
BUILDLOG_MAC_S=""
# The Mac copy of a natedev run (copy_watch.py): the last outcome word, the
# copies started and the seconds they held the Mac across leave-hook passes,
# and how long natedev's step had run when the call's result was decided.
BUILDLOG_MAC_COPY=""
BUILDLOG_MAC_COPY_S=""
BUILDLOG_MAC_COPY_COUNT=""
BUILDLOG_MAC_COPY_NATEDEV_S=""
export BUILDLOG_TARGET=""
if [[ "$CMD" == test && ( " ${ARGS[*]} " == *" --filter "* || " ${ARGS[*]} " == *" --run-ignored "* ) ]]; then
    FILTER_RUN=1
fi
if [[ "$CMD" == test && "$LOCAL_ONLY" -eq 1 ]]; then
    BUILDLOG_MAC=declined
    BUILDLOG_MAC_REASON=local_flag
fi

tree_key() {
    local words=("$CMD" "${ARGS[@]}") names=() name scope=() key_options=() scripts=()
    local option content=0
    for option in "$@"; do
        if [[ "$option" == --tree-content ]]; then content=1; else key_options+=("$option"); fi
    done
    if [[ "$content" == 0 ]]; then
        scripts=("${BASH_SOURCE[0]}" "$HOME/.claude/scripts/lint/invoke.sh"
            "${LINT_CONFIG_FILE:-$HOME/.claude/config/lint.conf}")
    fi
    if [[ "$CMD" == lint ]]; then
        if [[ ${#ARGS[@]} -eq 1 ]]; then words=(lint); fi
    elif [[ "${FILTER_RUN}" -eq 1 ]]; then
        # The filter names go last, sorted, each once (see the header).
        words=("$CMD")
        set -- "${ARGS[@]}"
        while [[ $# -gt 0 ]]; do
            if [[ "$1" == --filter && $# -gt 1 ]]; then
                names+=("$2")
                shift 2
            else
                words+=("$1")
                shift
            fi
        done
        while IFS= read -r name; do
            if [[ -n "$name" ]]; then words+=(--filter "$name"); fi
        done < <(printf '%s\n' "${names[@]}" | LC_ALL=C sort -u)
    fi
    if [[ "$CMD" == test ]]; then
        for name in ${VERIFY_USER_ENVIRONMENT[@]+"${VERIFY_USER_ENVIRONMENT[@]}"}; do
            words+=(--env "${name%%=*}")
        done
        # Keyed on the package's own build, read from the call's metadata.
        if [[ -n "${ARGS[0]}" ]]; then scope=(--package "${ARGS[0]}"); fi
    fi
    if [[ "$CMD" == lint && -z "${CACHE_DIR:-}" ]]; then
        key_options+=(--without-lint-versions)
    fi
    VERIFY_RESULT_ENVIRONMENT_FD=3 \
    VERIFY_TEST_BUILD_INPUTS_FILE="${HOLD_WAIT_DIR:+$HOLD_WAIT_DIR/test-build-inputs}" \
    verify_helper --stdin "$TREE_WATCH_SCRIPT" --key ${key_options[@]+"${key_options[@]}"} ${scope[@]+"${scope[@]}"} ${scripts[@]+"${scripts[@]}"} -- "${words[@]}" \
        3< <(printf '%s\0' ${VERIFY_USER_ENVIRONMENT[@]+"${VERIFY_USER_ENVIRONMENT[@]}"}) \
        <<< "${GATE_METADATA}" 2>/dev/null || true
}

set_verdict_keys() {
    local keys
    keys="$(tree_key --with-tree-content "$@")"
    VERDICT_KEY="${keys%%$'\n'*}"
    VERDICT_TREE_KEY=""
    if [[ "$keys" == *$'\n'* ]]; then
        VERDICT_TREE_KEY="${keys#*$'\n'}"
    fi
}

TREE_CHANGED_STATUS=77
TREE_WATCH_PID=""
TREE_WATCH_DIR=""

after_tree_change() {
    [[ -n "$TREE_WATCH_DIR" && -s "$TREE_WATCH_DIR/fired" ]]
}

stop_tree_watch() {
    if [[ -n "$TREE_WATCH_PID" ]]; then
        # A group signal can reap the watcher before this cleanup starts.
        if kill "$TREE_WATCH_PID" 2>/dev/null; then
            wait "$TREE_WATCH_PID" 2>/dev/null || true
        fi
        TREE_WATCH_PID=""
    fi
}

find_mac_runner_pid() {
    # Set the caller's runner_pid without a subshell, which would lose job state.
    local latest_job=${!:-}
    runner_pid=$MAC_RUNNER_PID
    if [[ -z "$runner_pid" && "$MAC_RUNNER_LAUNCHING" == 1 && "$latest_job" != "$MAC_RUNNER_PRIOR_JOB" ]]; then
        runner_pid=$latest_job
    fi
}

cancel_tree_run() {
    after_tree_change || return 0
    local runner_pid
    find_mac_runner_pid
    trap '' USR2
    stop_tree_watch
    if [[ -n "$runner_pid" ]]; then
        # offload.py's TERM handler stops the remote run and releases its claim.
        kill -TERM "$runner_pid" 2>/dev/null || true
        wait "$runner_pid" 2>/dev/null || true
        MAC_RUNNER_PID=""
    fi
    # Board release also stops a step's separately owned process group.
    if declare -F release_token >/dev/null; then release_token || true; fi
    if [[ -n "${RUN_STEP_PID:-}" ]]; then
        "${STEP_RECORD_SCRIPT%/*}/../lib/py" "${STEP_RECORD_SCRIPT%/*}/../lib/process_group.py" "$RUN_STEP_PID" KILL 2>/dev/null || { [[ $? == 3 ]] || printf 'verify.sh: process group helper failed for pid %s\n' "$RUN_STEP_PID" >&2; }
        wait "$RUN_STEP_PID" 2>/dev/null || true
        RUN_STEP_PID=""
    fi
    # Reacquisition creates .again in the step's subshell. Both names belong
    # to this call, even when the parent never saw TOKEN_WAIT_ENTRY change.
    if [[ -n "${TOKEN_WAITER_FILE:-}" ]]; then
        rm -f "$TOKEN_WAITER_FILE" "${TOKEN_WAITER_FILE}.again"
    fi
    exec 9>&-
    [[ -z "${MAC_LOG_FILE:-}" ]] || rm -f "$MAC_LOG_FILE"
    [[ -z "${MAC_RESULT_FILE:-}" ]] || rm -f "$MAC_RESULT_FILE"
    local changed_path=""
    if [[ -s "$TREE_WATCH_DIR/fired.path" ]]; then changed_path=" ($(< "$TREE_WATCH_DIR/fired.path"))"; fi
    printf 'verify.sh: cancelled — the tree changed while %s ran%s, so this result would be stale. Resubmit the %s when your edits are done.\n' "$CMD" "$changed_path" "$CMD" >&2
    note_event cancelled "${RUN_STARTED}" "$(( SECONDS - RUN_STARTED ))" "" 0 "$TREE_CHANGED_STATUS"
    rm -rf "$TREE_WATCH_DIR"
    TREE_WATCH_DIR=""
    exit "$TREE_CHANGED_STATUS"
}

start_tree_watch() {
    [[ -z "$TREE_WATCH_PID" ]] || return 0
    TREE_WATCH_DIR="$(verify_helper "$BUILD_FOLDERS_SCRIPT" unique --kind verify-tree --owner "$$")"
    set_verdict_keys --watch-start "$TREE_WATCH_DIR/start"
    [[ -s "$TREE_WATCH_DIR/start" ]] || { rm -rf "$TREE_WATCH_DIR"; TREE_WATCH_DIR=""; return 0; }
    local scope=()
    if [[ "$CMD" == test ]]; then if [[ -n "${ARGS[0]}" ]]; then scope=(--package "${ARGS[0]}"); fi; fi
    trap 'cancel_tree_run' USR2
    "$PY" "$TREE_WATCH_SCRIPT" "$TREE_WATCH_DIR/start" "$TREE_WATCH_DIR/fired" "$$" --signal-parent \
        ${scope[@]+"${scope[@]}"} -- <<< "$GATE_METADATA" >/dev/null 2>&1 &
    TREE_WATCH_PID=$!
}

finish_tree_watch() {
    stop_tree_watch
    cancel_tree_run
    [[ -z "$TREE_WATCH_DIR" ]] || rm -rf "$TREE_WATCH_DIR"
    TREE_WATCH_DIR=""
}

# Read the staged tree through the copy owner's private index.
doc_tree() {
    VERIFY_PY_HELPER=DOC_TREE_PY "$PY" - "$BUILD_FOLDERS_SCRIPT" <<'PYDOC'
from pathlib import Path
import sys
with open(Path(sys.argv[1]).resolve().parents[2] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.copies import copies
print(copies.staged_tree(Path.cwd()))
PYDOC
}

# The copy owner holds the lease until this shell closes descriptor 8.
doc_checkout() {
    local copy_script
    copy_script="$(dirname "$BUILD_FOLDERS_SCRIPT")/../copies/copies.py"
    coproc DOC_COPY { "$PY" "$copy_script" hold --repo "$DOC_REPO_TOP"; }
    exec 8>&"${DOC_COPY[1]}"
    IFS= read -r DOC_TREE <&"${DOC_COPY[0]}" || return 1
    IFS= read -r DOC_CHECKOUT <&"${DOC_COPY[0]}" || return 1
}

doc_record() {
    local current_tree
    current_tree="$(doc_tree)" || return
    if [[ "$current_tree" != "$DOC_TREE" ]]; then
        echo 'verify.sh: the staged tree changed while doc ran; no doc pass record was written.'
        return 0
    fi
    VERIFY_PY_HELPER=DOC_RECORD_PY "$PY" - "$DOC_REPO_TOP" "$DOC_TREE" "$BUILDLOG_CALL_ID" <<'PYDOC'
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time

records = Path.home() / ".local/state/verify/doc_pass"
records.mkdir(parents=True, exist_ok=True)
cutoff = time.time() - 14 * 24 * 60 * 60
for old in records.glob("*.json"):
    try:
        if old.stat().st_mtime < cutoff:
            old.unlink()
    except FileNotFoundError:
        pass
record = dict(repo_top=str(Path(sys.argv[1]).resolve()), tree_id=sys.argv[2],
              time=datetime.now(timezone.utc).isoformat(), call_id=sys.argv[3])
with tempfile.NamedTemporaryFile(mode="w", dir=records, prefix=".doc-", delete=False) as output:
    temporary = Path(output.name)
    try:
        json.dump(record, output)
        output.flush()
        os.fsync(output.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
try:
    temporary.replace(records / (sys.argv[2] + ".json"))
    directory_fd = os.open(records, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)
print("verify.sh: doc pass recorded for tree " + sys.argv[2])
PYDOC
}

# note_event OUTCOME WAIT WALL BUILD SAVED STATUS: this call's one record in
# the build log (see the header), the first time it is called. OUTCOME is ran,
# failed, interrupted, cancelled, reused or replayed; an empty STATUS or BUILD is unknown.
# record.py adds the repo, worktree, branch and commit. A record that cannot be
# written is lost, never the result.
note_event() {
    [[ "${CALL_NOTED}" -eq 0 ]] || return 0
    CALL_NOTED=1
    collect_hold_wait
    [[ -f "${BUILDLOG_RECORD:-}" ]] || return 0
    local outcome=$1 wait=$2 wall=$3 build=$4 saved=$5 status=$6 cached=0
    if [[ -n "${CACHE_DIR}" ]]; then
        cached=1
    fi
    local mem_kills=0
    if [[ -n "${MEM_KILL_FILE:-}" && -f "$MEM_KILL_FILE" ]]; then
        mem_kills=$(wc -c < "$MEM_KILL_FILE")
    fi
    local token_wait_s=${TOKEN_WAIT_S:-0} step_wait
    if [[ -n "${TOKEN_WAIT_FILE:-}" && -f "$TOKEN_WAIT_FILE" ]]; then
        while IFS= read -r step_wait; do
            token_wait_s=$(( token_wait_s + step_wait ))
        done < "$TOKEN_WAIT_FILE"
    fi
    local mem_kill_stopped=0
    if [[ -n "${MEM_KILL_FILE:-}" && -f "${MEM_KILL_FILE}.stopped" ]]; then
        mem_kill_stopped=1
    fi
    BUILDLOG_MEM_KILLS="$mem_kills" BUILDLOG_MEM_KILL_STOPPED="$mem_kill_stopped" BUILDLOG_TOKEN_WAIT_S="$token_wait_s" BUILDLOG_MAC="$BUILDLOG_MAC" BUILDLOG_MAC_HOLDER="$BUILDLOG_MAC_HOLDER" BUILDLOG_MAC_REASON="$BUILDLOG_MAC_REASON" BUILDLOG_MAC_S="$BUILDLOG_MAC_S" \
        BUILDLOG_MAC_COPY="$BUILDLOG_MAC_COPY" BUILDLOG_MAC_COPY_S="$BUILDLOG_MAC_COPY_S" \
        BUILDLOG_MAC_COPY_COUNT="$BUILDLOG_MAC_COPY_COUNT" BUILDLOG_MAC_COPY_NATEDEV_S="$BUILDLOG_MAC_COPY_NATEDEV_S" BUILDLOG_PACKAGE="${VERIFY_PACKAGE:-}" \
        verify_helper "$BUILDLOG_RECORD" call "$outcome" "$status" "$cached" "$wait" "$wall" \
        "$build" "$saved" "${SECONDS}" "$CMD" ${VERIFY_PACKAGE:+"$VERIFY_PACKAGE"} ${COMMAND_ARGS[@]+"${COMMAND_ARGS[@]}"} \
        </dev/null >/dev/null 2>&1 || true
}

MAC_RUNNER="${VERIFY_MAC_RUNNER:-$HOME/.claude/scripts/mac_test/offload.py}"
MAC_TEST_SCRIPT="${VERIFY_MAC_TEST_SCRIPT:-$HOME/.claude/scripts/mac_test/mac_test.py}"
MAC_IDLE_SCRIPT="$(dirname "${BASH_SOURCE[0]}")/../buildlog/mac_idle.py"
MAC_IDLE_MARKER=""
export BUILDLOG_MAC_IDLE_HOOK=""
MAC_COPY_WATCH="${VERIFY_MAC_COPY_WATCH:-$(dirname "$MAC_RUNNER")/copy_watch.py}"
COPY_WATCH_PID=""
COPY_WATCH_DIR=""
VERIFY_STEP_INFO_FILE=""
COPY_WORD="" COPY_MAC="" COPY_REASON="" COPY_SECONDS="" COPY_RUNNER_STATUS="" COPY_STOPPED=""
MAC_RESULT_FILE=""
MAC_LOG_FILE=""
MAC_RUNNER_PID=""
MAC_RUNNER_LAUNCHING=0
MAC_RUNNER_PRIOR_JOB=""
# A waiting local test can leave for this Mac after a temporary decline.
# Busy, blocked, and disk declines use the normal poll interval.
SSH_RETRYABLE_REASON='^(busy|blocked|disk|backoff|unreachable|state|copy|mac_busy|spending_limit|spending_unavailable|state unreadable: .*)$'

step_can_retry() {
    [[ "${STEP_WRITES_TREE:-0}" == 0 && "$LOCAL_ONLY" == 0 ]] || return 1
    case "$CMD" in
        build|final) [[ "$STEP_RUNS_ON" == any_target ]] || return 1 ;;
    esac
    local name eligibility reason settings platform
    while IFS=$'\t' read -r name eligibility reason settings platform; do
        if [[ "$settings" != - && -n "$settings" && ( "$eligibility" == eligible ||
              ( "$eligibility" == excluded && ( "$reason" =~ $SSH_RETRYABLE_REASON ||
                ( -n "${PINNED_TARGET:-}" && "$reason" == starting ) ) ) ) ]]; then
            return 0
        fi
    done <<< "$STEP_TARGET_TABLE"
    return 1
}

MAC_LEAVE_SETTINGS=""
MAC_LEAVE_REASON=""
MAC_RETRY_AFTER=0
MAC_RETRY_NOTICE_SHOWN=0
SSH_RETIRED_SETTINGS=()
SSH_RETRY_SETTINGS=()
SSH_RETRY_DEADLINES=()
SSH_RETRY_AFTER_SETTINGS=""
MAC_VERB=nextest
MAC_SOURCE_ROOT=""
MAC_VERB_ARGS=()
MAC_FINISH_CALL=1
MAC_PASSED=0
STEP_RAN_ON_MAC=0

forward_mac_runner_signal() {
    local signal_name=$1 exit_status=$2 runner_pid
    # TERM stops the child even while Bash ignores INT.
    if [[ "$signal_name" == INT ]]; then signal_name=TERM; fi
    find_mac_runner_pid
    stop_tree_watch
    [[ -z "$TREE_WATCH_DIR" ]] || rm -rf "$TREE_WATCH_DIR"
    TREE_WATCH_DIR=""
    trap - INT TERM HUP
    if [[ -n "$runner_pid" ]]; then
        kill -s "$signal_name" "$runner_pid" 2>/dev/null || true
        wait "$runner_pid" 2>/dev/null || true
    fi
    if [[ -n "$MAC_RESULT_FILE" ]]; then
        if [[ -d "$MAC_RESULT_FILE" ]]; then
            rmdir "$MAC_RESULT_FILE" 2>/dev/null || true
        else
            rm -f "$MAC_RESULT_FILE"
        fi
    fi
    if [[ -n "$MAC_LOG_FILE" ]]; then
        exec 9>&-
        rm -f "$MAC_LOG_FILE"
    fi
    note_event interrupted "$SECONDS" 0 "" 0 "$exit_status"
    exit "$exit_status"
}

remote_step_kind() {
    if [[ "$CMD" != final ]]; then printf '%s\n' "$CMD"; return; fi
    local verb kind
    while IFS=$'\t' read -r verb kind; do
        if [[ "$verb" == "$MAC_VERB" ]]; then printf '%s\n' "$kind"; return; fi
    done < "$HOLD_WAIT_DIR/remote-step-kinds"
}

try_mac_test() {
    local selected_target selected_is_mac=0
    LAST_TARGET_OUTCOME="" LAST_TARGET_REASON=""
    selected_target=$(target_for_settings "$1")
    if [[ "$(target_platform "$selected_target")" == macos ]]; then selected_is_mac=1; fi
    if [[ "$selected_is_mac" == 1 ]]; then step_state waiting_mac; else step_state waiting_ssh; fi
    MAC_PASSED=0
    [[ -z "$MAC_IDLE_MARKER" ]] || rm -f -- "$MAC_IDLE_MARKER"
    local caller_traps
    caller_traps="$(trap -p INT TERM HUP)"
    trap 'forward_mac_runner_signal INT 130' INT
    trap 'forward_mac_runner_signal TERM 143' TERM
    trap 'forward_mac_runner_signal HUP 129' HUP
    REMOTE_BUILD_TARGET_DIR=""
    local runner_status result_text="" result_word="" result_reason="" mac_owns_watch=0
    local result_seconds="" result_holder="" result_fetch=failed mac_words=0 line name
    local runner_args=(run --repo-root "${DOC_REPO_TOP:-$(git rev-parse --show-toplevel)}"
        --package "$PKG" --call-id "$BUILDLOG_CALL_ID" --settings "$1")
    runner_args+=(--target "$selected_target")
    if [[ -s "$HOLD_WAIT_DIR/ssh-reservation" ]]; then
        local reserved_target reserved_settings
        IFS=$'\t' read -r reserved_target reserved_settings < "$HOLD_WAIT_DIR/ssh-reservation"
        if [[ "$reserved_target" != "$selected_target" || "$reserved_settings" != "$1" ]]; then
            return 0
        fi
        local claim_id episode attempt claim_session
        IFS=$'\t' read -r claim_id episode attempt claim_session < "$HOLD_WAIT_DIR/ssh-reservation.identity"
        runner_args+=(--claim-owner-pid $$ --claim-id "$claim_id" --episode "$episode" --attempt "$attempt"
            --claim-session "$claim_session")
    fi
    if declare -F release_token >/dev/null; then release_token || return $?; fi
    if [[ "$MAC_VERB" != nextest ]]; then runner_args+=(--verb "$MAC_VERB"); fi
    local remote_kind
    remote_kind=$(remote_step_kind)
    runner_args+=(--kind "$remote_kind")
    if [[ -n "$MAC_SOURCE_ROOT" ]]; then runner_args+=(--source-root "$MAC_SOURCE_ROOT"); fi
    if [[ "${2:-}" == busy || "${2:-}" == blocked ]]; then
        runner_args+=(--last-decline "$2")
    fi
    local MAC_ENV_NAMES=( ${MAC_ENV_NAMES[@]+"${MAC_ENV_NAMES[@]}"} )
    local VERIFY_MAC_ENVIRONMENT=( ${VERIFY_MAC_ENVIRONMENT[@]+"${VERIFY_MAC_ENVIRONMENT[@]}"} )
    if [[ "$CMD" == final && "$MAC_VERB" == nextest ]]; then
        for name in ${VERIFY_TEST_ENVIRONMENT[@]+"${VERIFY_TEST_ENVIRONMENT[@]}"}; do
            MAC_ENV_NAMES+=("${name%%=*}")
            VERIFY_MAC_ENVIRONMENT+=("$name")
        done
    fi
    for name in ${MAC_ENV_NAMES[@]+"${MAC_ENV_NAMES[@]}"}; do runner_args+=(--env "$name"); done
    if [[ "$MAC_FETCH_GIVEN" == 1 ]]; then runner_args+=(--fetch "$MAC_FETCH"); fi
    MAC_RESULT_FILE="$(mktemp)"
    runner_args+=(--result "$MAC_RESULT_FILE")
    if [[ "$FILTER_RUN" -eq 1 ]]; then
        runner_args+=(--filter-run)
    fi
    if [[ "$MAC_VERB" == nextest ]]; then
        runner_args+=(-- "${NEXTEST_ARGS[@]}")
    else
        runner_args+=(-- "${MAC_VERB_ARGS[@]}")
    fi

    export BUILDLOG_TARGET="$selected_target"
    # A Mac pass is recorded like a natedev one, judged on the tree as it was
    # when the Mac run began, with the output it printed.
    if [[ "$CMD" == test && -z "$TREE_WATCH_PID" ]]; then
        start_tree_watch
        mac_owns_watch=1
    elif [[ -n "${CACHE_DIR}" && -z "$TREE_WATCH_PID" ]]; then
        set_verdict_keys
    fi
    MAC_LOG_FILE="$(mktemp "$HOLD_WAIT_DIR/mac-output.XXXXXX")"
    # Both tee processes inherit this descriptor before a signal removes the file.
    exec 9>>"$MAC_LOG_FILE"
    if [[ "$selected_is_mac" == 1 ]]; then step_state on_mac; else
        step_state on_ssh "$selected_target"
    fi
    # Apply forwarded values in Python. Bash can make these names readonly or arrays.
    # The pipe preserves bytes and keeps values outside argv.
    local VERIFY_MAC_ENV_PY='
import os
import runpy
import sys

with open(3, "rb") as handoff:
    for entry in handoff.read().split(b"\0"):
        if entry:
            name, value = entry.split(b"=", 1)
            os.environb[name] = value
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'
    MAC_RUNNER_PRIOR_JOB=${!:-}
    MAC_RUNNER_LAUNCHING=1
    (
        if [[ -z ${VERIFY_MAC_ENVIRONMENT[0]+set} ]]; then
            exec "$PY" "$MAC_RUNNER" "${runner_args[@]}"
        fi
        VERIFY_PY_HELPER=VERIFY_MAC_ENV_PY exec "$PY" -c "$VERIFY_MAC_ENV_PY" "$MAC_RUNNER" "${runner_args[@]}" \
            3< <(printf '%s\0' "${VERIFY_MAC_ENVIRONMENT[@]}")
    ) > >(tee -a /dev/fd/9) 2> >(tee -a /dev/fd/9 >&2) &
    MAC_RUNNER_PID=$!
    MAC_RUNNER_LAUNCHING=0
    if wait "$MAC_RUNNER_PID"; then
        runner_status=0
    else
        runner_status=$?
    fi
    if [[ "$mac_owns_watch" == 1 ]]; then finish_tree_watch; fi
    trap - INT TERM HUP
    eval "$caller_traps"
    MAC_RUNNER_PID=""
    release_wait_claim

    if [[ -f "$MAC_RESULT_FILE" && -r "$MAC_RESULT_FILE" \
          && -s "$MAC_RESULT_FILE" ]]; then
        result_text="$(< "$MAC_RESULT_FILE")"
        while IFS= read -r line; do
            case "$line" in
                outcome=*)
                    if [[ "$selected_is_mac" == 0 ]]; then
                        ((mac_words += 1)); result_word="${line#outcome=}"
                    fi
                    ;;
                mac=*)
                    ((mac_words += 1))
                    result_word="${line#mac=}"
                    ;;
                reason=*) result_reason="${line#reason=}" ;;
                seconds=*) result_seconds="${line#seconds=}" ;;
                holder=*) result_holder="${line#holder=}" ;;
                fetch=*) result_fetch="${line#fetch=}" ;;
                build_target_directory=*) REMOTE_BUILD_TARGET_DIR="${line#build_target_directory=}" ;;
            esac
        done <<< "$result_text"
    fi
    if [[ -d "$MAC_RESULT_FILE" ]]; then
        rmdir "$MAC_RESULT_FILE" 2>/dev/null || true
    else
        rm -f "$MAC_RESULT_FILE"
    fi
    MAC_RESULT_FILE=""

    if [[ "$mac_words" -ne 1 \
          || ! "$result_word" =~ ^(passed|failed|lost|declined)$ \
          || ( "$result_word" == passed && "$runner_status" -ne 0 ) \
          || ( "$result_word" == failed && "$runner_status" -eq 0 ) ]]; then
        BUILDLOG_MAC=declined
        BUILDLOG_MAC_REASON=runner
        BUILDLOG_MAC_S=""
        BUILDLOG_MAC_HOLDER=""
        exec 9>&-
        rm -f "$MAC_LOG_FILE"
        MAC_LOG_FILE=""
        export BUILDLOG_TARGET=""
        LAST_TARGET_OUTCOME=declined LAST_TARGET_REASON=runner
        if [[ "$selected_is_mac" == 1 ]]; then step_state waiting_mac; else
            BUILDLOG_MAC="" BUILDLOG_MAC_REASON="" BUILDLOG_MAC_S="" BUILDLOG_MAC_HOLDER=""
            step_state waiting_ssh
        fi
        return
    fi

    LAST_TARGET_OUTCOME="$result_word" LAST_TARGET_REASON="$result_reason"
    BUILDLOG_MAC="$result_word"
    BUILDLOG_MAC_REASON="$result_reason"
    BUILDLOG_MAC_S="$result_seconds"
    BUILDLOG_MAC_HOLDER=""
    if [[ "$result_word" == declined && "$result_reason" == busy ]]; then
        BUILDLOG_MAC_HOLDER="$result_holder"
    fi
    if [[ "$selected_is_mac" == 0 ]]; then
        BUILDLOG_MAC="" BUILDLOG_MAC_REASON="" BUILDLOG_MAC_S="" BUILDLOG_MAC_HOLDER=""
    fi
    case "$result_word" in
        declined|lost)
            exec 9>&-
            rm -f "$MAC_LOG_FILE"
            MAC_LOG_FILE=""
            export BUILDLOG_TARGET=""
            if [[ "$selected_is_mac" == 1 ]]; then step_state waiting_mac; else step_state waiting_ssh; fi
            return
            ;;
        failed)
            exec 9>&-
            rm -f "$MAC_LOG_FILE"
            MAC_LOG_FILE=""
            if [[ "$MAC_FINISH_CALL" == 0 ]]; then return "$runner_status"; fi
            note_event failed "${SECONDS}" 0 "" 0 "$runner_status"
            exit "$runner_status"
            ;;
        passed)
            if [[ "$MAC_FETCH_GIVEN" == 1 && "$result_fetch" != fetched ]]; then
                if [[ "$FILTER_RUN" == 1 ]]; then BUILDLOG_MAC=passed_filter; else BUILDLOG_MAC=passed_full; fi
                printf 'verify.sh: FAIL: the tests passed on the Mac, but --fetch %s brought nothing back (%s).\n' "$MAC_FETCH" "$result_fetch" >&2
                exec 9>&-
                rm -f "$MAC_LOG_FILE"
                MAC_LOG_FILE=""
                note_event failed "$SECONDS" 0 "" 0 2
                exit 2
            fi
            MAC_PASSED=1
            if [[ "$MAC_FINISH_CALL" == 0 ]]; then
                exec 9>&-
                rm -f "$MAC_LOG_FILE"
                MAC_LOG_FILE=""
                return 0
            fi
            if [[ "$selected_is_mac" == 0 ]]; then
                record_mac_pass "$MAC_LOG_FILE" "$result_seconds"
                echo "verify.sh: PASS on $selected_target (Linux)."
                note_event ran "${SECONDS}" 0 "" 0 0
                exit 0
            fi
            finish_mac_pass "$MAC_LOG_FILE" "$result_seconds"
            ;;
    esac
}

# Ends the call on a Mac pass, filtered or full. Before CI either machine
# answers a test; CI is the protection (user, 2026-10-16).
finish_mac_pass() {
    if [[ "$FILTER_RUN" -eq 1 ]]; then
        BUILDLOG_MAC=passed_filter
    else
        BUILDLOG_MAC=passed_full
    fi
    record_mac_pass "$1" "$2"
    echo "verify.sh: PASS on the Mac (macOS), so nothing ran on natedev."
    note_event ran "${SECONDS}" 0 "" 0 0
    exit 0
}

# The helper checks the hold on every poll. Show its stderr again only
# when the holder, reason or escalation message changes during this wait.
check_routing_hold() {
    local admission=0
    if build_hold_nothing_held; then return 0; fi
    "$PY" "$BUILD_TARGETS_SCRIPT" register-local --call-id "$BUILDLOG_CALL_ID" --pid $$ \
        2>"$HOLD_WAIT_DIR/routing-diagnostic" || admission=$?
    if ! cmp -s "$HOLD_WAIT_DIR/routing-diagnostic" "$HOLD_WAIT_DIR/last-routing-diagnostic"; then
        cat "$HOLD_WAIT_DIR/routing-diagnostic" >&2
        cp "$HOLD_WAIT_DIR/routing-diagnostic" "$HOLD_WAIT_DIR/last-routing-diagnostic"
    fi
    build_hold_check_result "$admission" register-local
}

initialize_step_record() {
    local answer table line name eligibility reason settings platform extra status=0 decision_reason="no eligible SSH target"
    local eligible_names="" mac_names="" mac_disk_exclusions=""
    local arguments=(eligible --new-step --probe-marks-file "$HOLD_WAIT_DIR/probe-marks" --kind "$CMD" --repo-root "$(git rev-parse --show-toplevel)")
    if [[ "$CMD" == final ]]; then arguments+=(--remote-kinds-file "$HOLD_WAIT_DIR/remote-step-kinds"); fi
    PKG="${ARGS[0]:-}"
    arguments+=(--package "$PKG")
    if [[ "$MAC_ONLY" == 1 ]]; then arguments+=(--mac); fi
    if [[ "$LOCAL_ONLY" == 1 ]]; then arguments+=(--local "$LOCAL_REASON"); fi
    if [[ -n "$PINNED_TARGET" ]]; then arguments+=(--target "$PINNED_TARGET"); fi
    STEP_ELIGIBILITY_ARGUMENTS=("${arguments[@]}")
    STEP_ELIGIBILITY_READY=1
    answer=$("$PY" "$BUILD_TARGETS_SCRIPT" "${arguments[@]}") || status=$?
    if (( status != 0 )); then
        printf 'verify.sh: target eligibility check failed (exit %s); step refused\n' "$status" >&2
        exit "$status"
    fi
    line=${answer%%$'\n'*}
    case "$line" in
        any_target) STEP_RUNS_ON=any_target; table=${answer#*$'\n'} ;;
        natedev_only\ *) decision_reason=${line#natedev_only }; table=${answer#*$'\n'} ;;
        *)
            printf 'verify.sh: target eligibility check returned no decision; step refused\n' >&2
            exit 2 ;;
    esac
    if [[ -z "$table" || "$table" == "$line" && "$line" != *$'\t'* ]]; then
        printf 'verify.sh: target eligibility check returned no targets; step refused\n' >&2
        exit 2
    fi
    while IFS=$'\t' read -r name eligibility reason settings platform extra; do
        if [[ -z "$name" || ( "$eligibility" != eligible && "$eligibility" != excluded ) || -z "$reason" || -z "$settings" || -n "$extra" ]]; then
            printf 'verify.sh: target eligibility check returned invalid targets; step refused\n' >&2
            exit 2
        fi
        if [[ "$eligibility" == excluded && "$reason" == disk && "$platform" == macos ]]; then
            mac_disk_exclusions+="${mac_disk_exclusions:+,}$name"
        fi
        if [[ "$settings" != - && ( "$eligibility" == eligible || "$reason" =~ $SSH_RETRYABLE_REASON || ( -n "$PINNED_TARGET" && "$reason" == starting ) ) ]]; then
            if [[ "$eligibility" == eligible && "$STEP_RUNS_ON" == natedev_only && "$decision_reason" != disk ]]; then
                printf 'verify.sh: target eligibility answer differs from its targets; step refused\n' >&2
                exit 2
            fi
            eligible_names+="${eligible_names:+,}$name"
            if [[ "$platform" == macos ]]; then mac_names+="${mac_names:+,}$name"; fi
        elif [[ "$eligibility" == eligible && "$settings" == - ]]; then
            eligible_names+="${eligible_names:+,}$name"
        fi
    done <<< "$table"
    STEP_TARGET_TABLE=$table
    if step_can_retry; then
        case "$CMD" in
            check|doc|example|build|final) STEP_DEFER_LOCAL=1 ;;
            lint) [[ "${COMMAND_ARGS[0]:-}" != --check ]] || STEP_DEFER_LOCAL=1 ;;
        esac
        while IFS=$'\t' read -r name eligibility reason settings platform; do
            if [[ "$settings" != - && ( "$eligibility" == eligible || "$reason" =~ $SSH_RETRYABLE_REASON ) ]]; then
                MAC_LEAVE_SETTINGS="$settings"
                break
            fi
        done <<< "$table"
    fi
    local record_args=(write --call-id "$BUILDLOG_CALL_ID" --pid "$$"
        --repo "$(git rev-parse --show-toplevel)" --kind "$CMD"
        --runs-on "$STEP_RUNS_ON" --reason "$decision_reason" --state waiting_hold
        --mac-disk-exclusions "$mac_disk_exclusions")
    if [[ "$STEP_RUNS_ON" == any_target ]]; then
        record_args+=(--eligible-targets "$eligible_names" --mac-targets "$mac_names")
    fi
    if [[ -n "$PINNED_TARGET" ]]; then record_args+=(--pinned-target "$PINNED_TARGET"); fi
    record_args+=(--package "$PKG")
    local report
    report=$(unset BUILDLOG_CALL_ID; verify_helper "$STEP_RECORD_SCRIPT" "${record_args[@]}" --report) || exit 2
    {
        IFS= read -r VERIFY_MONOTONIC_OFFSET_US
        IFS= read -r STEP_RECORD_PATH
        IFS= read -r STEP_STATE_PATH
    } <<< "$report"
    STEP_RECORD_TEXT=$(< "$STEP_RECORD_PATH")
    STEP_RECORD_ACTIVE=1
    export BUILDLOG_MEM_WAIT_FIRST_FILE="${BUILDLOG_MEM_WAIT_FIRST_FILE:-$HOLD_WAIT_DIR/first-queued-at}"
    if [[ ! -s "$BUILDLOG_MEM_WAIT_FIRST_FILE" ]]; then
        printf '%s\n' "${BUILDLOG_MEM_WAIT_SINCE:-${EPOCHSECONDS:-$(date +%s)}}" > "$BUILDLOG_MEM_WAIT_FIRST_FILE"
    fi
    export BUILDLOG_MEM_WAIT_SINCE="$(< "$BUILDLOG_MEM_WAIT_FIRST_FILE")"
    export BUILDLOG_CALL_ID
    if [[ "$MAC_ONLY" == 1 ]]; then
        printf 'verify.sh: Mac only (%s): route movable steps to the Mac.\n' "$MAC_REASON" >&2
        local mac_eligible=0 refusal=state
        while IFS=$'\t' read -r name eligibility reason settings platform; do
            [[ "$settings" != - && "$platform" == macos ]] || continue
            if [[ "$eligibility" == eligible || "$reason" == busy ]]; then
                mac_eligible=1
            else
                refusal=$reason
            fi
        done <<< "$STEP_TARGET_TABLE"
        if [[ "$mac_eligible" == 0 ]]; then
            BUILDLOG_MAC=declined BUILDLOG_MAC_REASON=$refusal BUILDLOG_MAC_S=0
            mac_only_refuse "$refusal"
        fi
    fi
    if step_can_retry; then
        while IFS=$'\t' read -r name eligibility reason settings platform; do
            if [[ "$settings" != - && ( "$eligibility" == eligible || "$reason" =~ $SSH_RETRYABLE_REASON )
                  && "$platform" == macos ]]; then
                MAC_LEAVE_SETTINGS=$settings
                register_mac_wait "$settings" 0
                break
            fi
        done <<< "$table"
    fi
}

# Reuse the call's decision. A hold check supplies the only local allow.
route_compiling_step() {
    local admission=0 command=$1
    STEP_RAN_ON_MAC=0
    MAC_PASSED=0
    step_state waiting_hold
    if [[ -z "$PINNED_TARGET" || "$PINNED_TARGET" == natedev || "${STEP_WRITES_TREE:-0}" == 1 ]]; then check_routing_hold || admission=$?; fi
    if [[ "${STEP_WRITES_TREE:-0}" == 1 ]]; then
        case "$admission" in
            0) return 0 ;;
            10) build_hold_wait_local "$@"; return $? ;;
            *) return "$admission" ;;
        esac
    fi
    case "$admission" in
        0)
            if [[ "$STEP_RUNS_ON" != any_target && "$STEP_FORCE_MAC" == 0 ]]; then return 0; fi
            ;;
        10) ;;
        *) return "$admission" ;;
    esac
    step_state waiting_hold
    if [[ "$CMD" != test ]] && step_can_retry; then
        if [[ "$CMD" == lint && "$admission" == 10 ]]; then
            HOLD_LINT=1
            : > "$HOLD_WAIT_DIR/lint-held"
        fi
        shift
        if [[ "$command" == env ]]; then
            while [[ "${1:-}" == *=* || "${1:-}" == -u ]]; do
                if [[ "$1" == -u ]]; then shift 2; else shift; fi
            done
            command=${1:-}
            shift || true
        fi
        if [[ "${1:-}" == +* ]]; then shift; fi
        MAC_VERB=${1:-}
        shift || true
        if [[ "$MAC_VERB" == mend ]]; then MAC_VERB=mend-check; fi
        if [[ "$command" != cargo || ( "$MAC_VERB" != check && "$MAC_VERB" != doc && "$MAC_VERB" != clippy && "$MAC_VERB" != mend-check && "$MAC_VERB" != build && "$MAC_VERB" != nextest ) ]]; then
            build_check_stop 2 'cannot route this compiling step to the Mac'
            return 2
        fi
        MAC_VERB_ARGS=()
        while (( $# )); do
            case "$MAC_VERB:$1" in
                doc:--no-deps|clippy:--all-targets|mend-check:--all-targets|mend-check:--fail-on-warn) shift ;;
                clippy:--allow-dirty|clippy:--allow-staged|clippy:--allow-no-vcs) shift ;;
                doc:--jobs) shift 2 ;;
                *) MAC_VERB_ARGS+=("$1"); shift ;;
            esac
        done
        if [[ "$MAC_VERB" == nextest ]]; then
            if [[ "${MAC_VERB_ARGS[0]:-}" == run ]]; then MAC_VERB_ARGS=("${MAC_VERB_ARGS[@]:1}"); fi
            NEXTEST_ARGS=("${MAC_VERB_ARGS[@]}")
        fi
        MAC_FINISH_CALL=0
        local route_status=0
        if [[ -s "$HOLD_WAIT_DIR/ssh-reservation" ]]; then
            try_reserved_target || route_status=$?
        else
            route_test || route_status=$?
        fi
        STEP_FORCE_MAC=0
        (( route_status == 0 )) || return "$route_status"
        if [[ "$MAC_PASSED" == 1 ]]; then STEP_RAN_ON_MAC=1; return 0; fi
    fi
    step_state waiting_hold
    build_hold_wait_local || return $?
    return 0
}

step_stopped_by_hold() {
    (( status > 128 )) || return 1
    local admission=0 call_id="$BUILDLOG_CALL_ID"
    (unset BUILDLOG_CALL_ID; verify_helper "$STEP_RECORD_SCRIPT" stopped --call-id "$call_id" \
        --consumed "$HOLD_WAIT_DIR/stops") || admission=$?
    case "$admission" in 0) return 0 ;; 1) ;; *) exit "$admission" ;; esac
    admission=0
    check_routing_hold || admission=$?
    case "$admission" in 0) return 1 ;; 10) return 0 ;; *) exit "$admission" ;; esac
}

# Keep one retry rule for routing and local memory or hold waits.
register_mac_wait() {
    [[ "${OSTYPE:-}" == linux* && -f "$MAC_IDLE_SCRIPT" ]] || return 0
    [[ "$(target_platform "$(target_for_settings "$1")")" == macos ]] || return 0
    local board="${UNIT_RUN_DIR:-${UNIT_SESSION_DIR:-}}"
    MAC_IDLE_MARKER="$(verify_helper "$MAC_IDLE_SCRIPT" register "$BUILDLOG_CALL_ID" "$$" \
        "${DOC_REPO_TOP:-$(git rev-parse --show-toplevel)}" "$PKG" "$1" "$2" \
        "${BUILDLOG_HOLD_WAITER_FILE:-}" "${board:+$board/token_waiters}" \
        "${CLAUDE_CODE_SESSION_ID:-}" "${TMPDIR:-}" 1)" || MAC_IDLE_MARKER=""
    if [[ -n "$MAC_IDLE_MARKER" ]]; then
        BUILDLOG_MAC_IDLE_HOOK="$(printf 'test -f %q && ' "$MAC_IDLE_MARKER")$(mac_retry_hook "$1" 0)"
        export BUILDLOG_MAC_IDLE_HOOK
    fi
}

remember_mac_retry() {
    case "${LAST_TARGET_OUTCOME:-$BUILDLOG_MAC}:${LAST_TARGET_REASON:-$BUILDLOG_MAC_REASON}" in
        declined:no_tests|declined:off|declined:repo|declined:linux_only|declined:local_flag)
            SSH_RETIRED_SETTINGS+=("$1")
            if [[ "$MAC_LEAVE_SETTINGS" == "$1" ]]; then
                MAC_LEAVE_SETTINGS="" MAC_LEAVE_REASON="" MAC_RETRY_AFTER=0
                BUILDLOG_MAC_IDLE_HOOK=""
                if [[ -n "$MAC_IDLE_MARKER" ]]; then
                    verify_helper "$MAC_IDLE_SCRIPT" exclude "$BUILDLOG_CALL_ID" "$BUILDLOG_MAC_REASON" || true
                    MAC_IDLE_MARKER=""
                fi
            fi
            return 1 ;;
    esac
    MAC_LEAVE_SETTINGS="$1" MAC_LEAVE_REASON="${LAST_TARGET_REASON:-$BUILDLOG_MAC_REASON}" MAC_RETRY_AFTER=0
    if [[ "$(target_platform "$(target_for_settings "$1")")" != macos ]]; then
        SSH_RETRY_SETTINGS+=("$1") SSH_RETRY_DEADLINES+=(0)
        return 0
    fi
    if [[ "${LAST_TARGET_OUTCOME:-$BUILDLOG_MAC}" != declined || ! "$MAC_LEAVE_REASON" =~ ^(busy|blocked|disk)$ ]]; then
        local now=$EPOCHREALTIME deadline
        deadline=$(( 10#${now/./} - VERIFY_MONOTONIC_OFFSET_US + 180000000 ))
        printf -v MAC_RETRY_AFTER '%d.%06d' "$(( deadline / 1000000 ))" "$(( deadline % 1000000 ))"
        if [[ "$MAC_RETRY_NOTICE_SHOWN" == 0 ]]; then
            echo "verify.sh: Mac attempt ended ($BUILDLOG_MAC${BUILDLOG_MAC_REASON:+: $BUILDLOG_MAC_REASON}); waiting 180 s before each retry while natedev waits." >&2
            MAC_RETRY_NOTICE_SHOWN=1
        fi
    fi
    SSH_RETRY_SETTINGS+=("$1") SSH_RETRY_DEADLINES+=("$MAC_RETRY_AFTER")
    register_mac_wait "$1" "$MAC_RETRY_AFTER"
}

# The same command gates routing retries and both local wait hooks. The
# deadline uses the call's monotonic clock. free-now reads local state only.
target_platform() {
    local name eligibility reason settings platform
    while IFS=$'\t' read -r name eligibility reason settings platform; do
        [[ "$name" == "$1" ]] || continue
        case "$platform" in
            macos|linux) printf '%s\n' "$platform"; return ;;
        esac
    done <<< "$STEP_TARGET_TABLE"
    return 1
}

target_for_settings() {
    local name eligibility reason settings platform
    while IFS=$'\t' read -r name eligibility reason settings platform; do
        if [[ "$settings" == "$1" ]]; then printf '%s' "$name"; return; fi
    done <<< "$STEP_TARGET_TABLE"
    printf '%s' mac
}

release_wait_claim() {
    [[ -n "${HOLD_WAIT_DIR:-}" ]] || return 0
    local claim_id="" episode="" attempt="" claim_session=""
    if [[ -s "$HOLD_WAIT_DIR/ssh-reservation.identity" ]]; then
        IFS=$'\t' read -r claim_id episode attempt claim_session < "$HOLD_WAIT_DIR/ssh-reservation.identity"
        "$PY" "$BUILD_TARGETS_SCRIPT" release-wait --pid $$ --claim-id "$claim_id" \
            --episode "$episode" --attempt "$attempt" --session "$claim_session" >/dev/null 2>&1 || true
    fi
    rm -f "$HOLD_WAIT_DIR/ssh-reservation" "$HOLD_WAIT_DIR/ssh-reservation.identity"
}

ssh_reserve_hook() {
    local name=$1 settings=$2 retired index deadline=0
    for retired in "${SSH_RETIRED_SETTINGS[@]}"; do
        if [[ "$retired" == "$settings" ]]; then printf 'false'; return; fi
    done
    for index in "${!SSH_RETRY_SETTINGS[@]}"; do
        if [[ "${SSH_RETRY_SETTINGS[$index]}" == "$settings" ]]; then
            deadline=${SSH_RETRY_DEADLINES[$index]}
        fi
    done
    if [[ "$MAC_LEAVE_SETTINGS" == "$settings" ]]; then deadline=$MAC_RETRY_AFTER; fi
    if [[ "$deadline" != 0 && "$(target_platform "$name")" != macos ]]; then
        # The retry hook expands the clock when it runs.
        # shellcheck disable=SC2016
        printf 'test "$(( ${EPOCHREALTIME/./} - %q ))" -ge %q' \
            "$VERIFY_MONOTONIC_OFFSET_US" "${deadline/./}"
        printf ' && '
    fi
    local remote_kind
    remote_kind=$(remote_step_kind)
    printf '%q ' "$PY" "$BUILD_TARGETS_SCRIPT" reserve --repo-root "${DOC_REPO_TOP:-$(git rev-parse --show-toplevel)}" \
        --kind "$remote_kind" --package "$PKG" --pid $$ --reservation-file "$HOLD_WAIT_DIR/ssh-reservation" \
        --claim-identity-file "$HOLD_WAIT_DIR/ssh-reservation.identity" --target "$name"
    if [[ "${3:-}" == 0 ]]; then printf '%s ' --new-step; fi
    if [[ "$(target_platform "$name")" == macos ]]; then printf '${SSH_RESERVE_RECOVERY:-}'; fi
}

ssh_retry_hook() {
    step_can_retry || { printf 'false'; return; }
    local name eligibility reason settings platform pivot=-1 index
    local names=() candidates=()
    while IFS=$'\t' read -r name eligibility reason settings platform; do
        [[ "$settings" != - && ( "$eligibility" == eligible || "$reason" =~ $SSH_RETRYABLE_REASON ) ]] || continue
        names+=("$name") candidates+=("$settings")
        if [[ "$settings" == "$SSH_RETRY_AFTER_SETTINGS" ]]; then pivot=$((${#candidates[@]} - 1)); fi
    done <<< "$STEP_TARGET_TABLE"
    # Finish this pass before retrying targets from the previous pass.
    for ((index=pivot+1; index<${#candidates[@]}; index++)); do
        printf '{ '
        if [[ "$(target_platform "${names[$index]}")" == macos ]]; then
            mac_retry_hook "${candidates[$index]}" "$MAC_RETRY_AFTER" reserve
            printf ' && '
        fi
        ssh_reserve_hook "${names[$index]}" "${candidates[$index]}"
        printf '; } || '
    done
    for ((index=0; index<=pivot; index++)); do
        printf '{ '
        if [[ "$(target_platform "${names[$index]}")" == macos ]]; then
            mac_retry_hook "${candidates[$index]}" "$MAC_RETRY_AFTER" reserve
            printf ' && '
        fi
        ssh_reserve_hook "${names[$index]}" "${candidates[$index]}"
        printf '; } || '
    done
    printf 'false'
}

try_reserved_target() {
    local target settings
    if [[ -s "$HOLD_WAIT_DIR/ssh-reservation" ]]; then
        IFS=$'\t' read -r target settings < "$HOLD_WAIT_DIR/ssh-reservation"
    elif [[ -n "$MAC_LEAVE_SETTINGS" && "$(target_platform "$(target_for_settings "$MAC_LEAVE_SETTINGS")")" == macos ]]; then
        settings=$MAC_LEAVE_SETTINGS
    else
        return 0
    fi
    SSH_RETRY_AFTER_SETTINGS=$settings
    try_mac_test "$settings" "$MAC_LEAVE_REASON" || return $?
    if [[ "$MAC_PASSED" != 1 ]]; then check_mac_only_result; fi
    remember_mac_retry "$settings" || true
}


mac_retry_hook() {
    local target
    if [[ "$2" != 0 ]]; then
        printf '{ '
        if [[ -n "$MAC_IDLE_MARKER" ]]; then printf 'test -f %q || ' "$MAC_IDLE_MARKER"; fi
        local deadline=$2
        # The retry hook expands the clock when it runs.
        # shellcheck disable=SC2016
        printf 'test "$(( ${EPOCHREALTIME/./} - %q ))" -ge %q' \
            "$VERIFY_MONOTONIC_OFFSET_US" "${deadline/./}"
        printf '; } && '
    fi
    if [[ "${3:-}" == reserve ]]; then printf '{ if SSH_RETRY_AVAILABILITY="$('; fi
    printf '%q ' "$PY" "$MAC_TEST_SCRIPT" free-now --settings "$1"
    target="$(target_for_settings "$1")"
    if [[ "$target" != mac ]]; then printf '%q ' --target "$target"; fi
    printf '%q ' --repo-root "${DOC_REPO_TOP:-$(git rev-parse --show-toplevel)}"
    if [[ "${3:-}" == reserve ]]; then
        printf ')"; then SSH_RETRY_AVAILABILITY="${SSH_RETRY_AVAILABILITY:-free}"; fi; '
        printf 'case "$SSH_RETRY_AVAILABILITY" in free) SSH_RESERVE_RECOVERY= ;; busy) SSH_RESERVE_RECOVERY=--recover-only ;; *) false ;; esac; }'
    fi
}

mac_only_refuse() {
    local reason=$1 text
    case "$reason" in
        blocked) text="the Mac is blocked (/build:status names the holder and when it lifts)" ;;
        unreachable) text="the Mac cannot be reached (/build:status --probe)" ;;
        backoff) text="the Mac was unreachable a short time ago (retry backoff)" ;;
        disk) text="the Mac is below its free-space floor" ;;
        off) text="Mac offload is off" ;;
        repo) text="this repository has no Mac setting" ;;
        linux_only) text="$PKG is Linux only on the Mac" ;;
        state) text="the Mac claim state cannot be read" ;;
        copy) text="the tree copy to the Mac failed" ;;
        no_tests) text="no test matched on the Mac" ;;
        lost) text="the link to the Mac dropped during the run" ;;
        runner) text="the Mac runner gave no usable result" ;;
        *) text=$reason ;;
    esac
    export BUILDLOG_TARGET=""
    if [[ "$CMD" == lint ]]; then
        printf 'verify.sh: %s --mac refused: %s.\n' "$CMD" "$text" >&2
    else
        printf 'verify.sh: %s --mac refused: %s; nothing ran on natedev.\n' "$CMD" "$text" >&2
    fi
    note_event failed "$SECONDS" 0 "" 0 2
    exit 2
}

# Only a busy claim can wait on a Mac-only call; every other answer ends it.
check_mac_only_result() {
    [[ "$MAC_ONLY" == 1 ]] || return 0
    if [[ "$BUILDLOG_MAC:$BUILDLOG_MAC_REASON" == declined:busy ]]; then return 0; fi
    if [[ "$BUILDLOG_MAC" == lost ]]; then mac_only_refuse lost; fi
    mac_only_refuse "${BUILDLOG_MAC_REASON:-runner}"
}

route_test() {
    local name eligibility reason settings platform admission remote_candidates=0 reservation_status
    local announced_reason="" retry=0 retry_ready
    while IFS=$'\t' read -r name eligibility reason settings platform; do
        if [[ "$eligibility" == excluded && "$settings" != - && "$platform" == macos ]]; then
            BUILDLOG_MAC=declined
            if [[ "$LOCAL_ONLY" == 0 && "$MAC_ONLY" == 0 ]]; then
                BUILDLOG_MAC_REASON=$reason
                export BUILDLOG_LOCAL_REASON="$reason"
            fi
        fi
    done <<< "$STEP_TARGET_TABLE"
    while true; do
        if [[ -n "$PINNED_TARGET" && "$PINNED_TARGET" != natedev ]]; then
            local pinned_answer
            pinned_answer=$("$PY" "$BUILD_TARGETS_SCRIPT" eligible --kind "$CMD" \
                --repo-root "${DOC_REPO_TOP:-$(git rev-parse --show-toplevel)}" --package "$PKG" --target "$PINNED_TARGET") || return $?
            STEP_TARGET_TABLE=${pinned_answer#*$'\n'}
            step_state waiting_ssh
        fi
        if [[ "$MAC_ONLY" == 0 && "$retry" == 1 && -z "$PINNED_TARGET" &&
              "$(target_platform "$(target_for_settings "$MAC_LEAVE_SETTINGS")")" != macos ]]; then
            admission=0
            check_routing_hold || admission=$?
            case "$admission" in
                0) collect_routing_wait; return 0 ;;
                10) ;;
                *) return "$admission" ;;
            esac
        fi
        remote_candidates=0
        while IFS=$'\t' read -r name eligibility reason settings platform; do
            if [[ "$MAC_ONLY" == 1 ]]; then
                [[ "$settings" != - && "$platform" == macos ]] || continue
            fi
            if [[ "$settings" == - ]]; then
                [[ "$retry" == 0 && "$eligibility" == eligible && "$STEP_FORCE_MAC" == 0 ]] || continue
                admission=0
                check_routing_hold || admission=$?
                case "$admission" in
                    0) collect_routing_wait; return 0 ;;
                    10)
                        if [[ "$PINNED_TARGET" == natedev ]]; then
                            build_hold_wait_local
                            return $?
                        fi
                        continue ;;
                    *) return "$admission" ;;
                esac
            fi
            if [[ -n "$PINNED_TARGET" ]] && { [[ "$reason" == starting ]] \
                || { [[ "$reason" == busy ]] && phase_wait_target_busy "$PINNED_TARGET"; }; }; then
                remote_candidates=1
                continue
            fi
            step_can_retry || continue
            if [[ "$eligibility" == excluded && ! "$reason" =~ $SSH_RETRYABLE_REASON ]]; then continue; fi
            [[ -f "$MAC_RUNNER" ]] || continue
            if [[ "$(ssh_reserve_hook "$name" "$settings" "$retry")" == false ]]; then continue; fi
            remote_candidates=1
            if [[ "$MAC_ONLY" == 0 && "$retry" == 1 && -z "$PINNED_TARGET" && "$platform" == macos ]]; then
                retry_ready=0
                eval "$(mac_retry_hook "$settings" "$MAC_RETRY_AFTER" reserve)" >/dev/null 2>&1 || retry_ready=$?
                if [[ "$retry_ready" != 0 ]]; then
                    admission=0
                    check_routing_hold || admission=$?
                    case "$admission" in
                        0) collect_routing_wait; return 0 ;;
                        10) continue ;;
                        *) return "$admission" ;;
                    esac
                fi
            fi
            if [[ -z "$MAC_IDLE_MARKER" ]]; then register_mac_wait "$settings" 0; fi
            reservation_status=0
            eval "$(ssh_reserve_hook "$name" "$settings" "$retry")" || reservation_status=$?
            if [[ -n "$PINNED_TARGET" && "$reservation_status" != 0 ]] \
                && phase_wait_target_busy "$PINNED_TARGET"; then continue; fi
            if [[ -n "$PINNED_TARGET" && "$reservation_status" != 0 && "$eligibility" == eligible ]]; then
                echo "verify.sh: target $PINNED_TARGET refused: admission changed." >&2
                return 2
            fi
            if [[ "$MAC_ONLY" == 0 && "$retry" == 1 && "$reservation_status" == 11 && "$platform" == macos ]]; then
                continue
            fi
            if [[ "$reservation_status" == 0 || "$MAC_ONLY" == 1 || ( "$reservation_status" == 11 && "$platform" == macos ) ]]; then
                phase_wait_finish target
                if [[ "$retry" == 1 ]]; then collect_routing_wait; fi
                MAC_LEAVE_SETTINGS=$settings
                try_reserved_target || return $?
                if [[ "$MAC_PASSED" == 1 ]]; then return 0; fi
                if [[ -n "$PINNED_TARGET" && "${LAST_TARGET_REASON:-}" == busy ]] \
                    && phase_wait_target_busy "$PINNED_TARGET"; then continue; fi
                if [[ -n "$PINNED_TARGET" ]]; then
                    printf 'verify.sh: target %s refused: %s\n' "$PINNED_TARGET" "${LAST_TARGET_REASON:-runner}" >&2
                    return 2
                fi
                if [[ "$retry" == 1 && "$BUILDLOG_MAC" == declined ]]; then
                    printf '%s\n' "${BUILDLOG_MAC_S:-0}" >> "$BUILDLOG_HOLD_WAIT_FILE"
                fi
            fi
        done <<< "$STEP_TARGET_TABLE"
        if [[ "$remote_candidates" == 0 ]]; then
            if [[ "$MAC_ONLY" == 1 ]]; then
                BUILDLOG_MAC=declined BUILDLOG_MAC_REASON=${BUILDLOG_MAC_REASON:-runner}
                mac_only_refuse "$BUILDLOG_MAC_REASON"
            fi
            [[ -z "$PINNED_TARGET" ]] || { echo "verify.sh: target $PINNED_TARGET refused: no capacity" >&2; return 2; }
            return 0
        fi
        if [[ -n "$PINNED_TARGET" ]]; then
            if [[ "$announced_reason" != starting ]]; then
                echo "verify.sh: waiting for target $PINNED_TARGET." >&2
                announced_reason=starting
            fi
            phase_wait_start target "target $PINNED_TARGET" "$PINNED_TARGET"
            sleep "${BUILD_TARGETS_POLL_S:-15}"
            retry=1
            continue
        fi
        if [[ "$MAC_ONLY" == 0 ]]; then
            admission=0
            check_routing_hold || admission=$?
            case "$admission" in
                0) collect_routing_wait; return 0 ;;
                10) ;;
                *) return "$admission" ;;
            esac
        fi
        if [[ "$MAC_ONLY" == 1 ]]; then
            if [[ -z "$announced_reason" ]]; then
                echo 'verify.sh: waiting for the Mac (busy); natedev runs nothing for this call.' >&2
                announced_reason=busy
            fi
        elif [[ -n "$MAC_LEAVE_SETTINGS" && "$(target_platform "$(target_for_settings "$MAC_LEAVE_SETTINGS")")" == macos ]]; then
            if [[ "$announced_reason" != "$BUILDLOG_MAC_REASON" ]]; then
                echo "verify.sh: waiting for the Mac ($BUILDLOG_MAC_REASON) or natedev's release." >&2
                announced_reason=$BUILDLOG_MAC_REASON
            fi
        elif [[ -z "$announced_reason" ]]; then
            echo 'verify.sh: waiting for eligible SSH capacity or the local hold release.' >&2
            announced_reason=ssh
        fi
        if [[ "$MAC_ONLY" == 1 ]]; then phase_wait_start hold "Mac busy"; else phase_wait_start hold "build hold"; fi
        if [[ -z "$ROUTING_WAIT_STARTED" ]]; then
            ROUTING_WAIT_STARTED=$EPOCHREALTIME
        fi
        if [[ "$MAC_ONLY" == 1 ]]; then
            sleep "${BUILD_TARGETS_POLL_S:-15}" &
        else
            "$PY" "$LOCAL_RELEASE_WAIT_SCRIPT" "${BUILD_TARGETS_POLL_S:-15}" "$BUILDLOG_CALL_ID" &
        fi
        HOLD_WAITER_PID=$!
        admission=0
        wait "$HOLD_WAITER_PID" || admission=$?
        HOLD_WAITER_PID=""
        if [[ "$admission" != 0 ]]; then
            printf 'verify.sh: local release check failed (exit %s); step refused\n' "$admission" >&2
            return "$admission"
        fi
        retry=1
    done
}

# Starts copy_watch.py beside the natedev step run() is about to start: while
# the step compiles, it may run a copy of the same test on the Mac, and the
# first result wins (see docs/as-built/mac-test-offload.md). Its folder sits in
# the run's hold folder, so the EXIT trap removes both, and both names carry
# the run's pid for tmp_sweep.py when a kill skips the trap.
start_copy_watch() {
    # BASHPID identifies this shell inside a subshell. Bash 3.2 needs the child shell's PPID.
    local parent_pid="${BASHPID:-$(exec sh -c 'printf "%s\n" "$PPID"')}"
    local copy_parent=()
    [[ -z "${HOLD_WAIT_DIR:-}" ]] || copy_parent=(--parent "$HOLD_WAIT_DIR")
    COPY_WATCH_DIR="$(verify_helper "$BUILD_FOLDERS_SCRIPT" unique --kind verify-copy --owner "$$" "${copy_parent[@]}")"
    VERIFY_STEP_INFO_FILE="$COPY_WATCH_DIR/step"
    local watch_args=(--info "$VERIFY_STEP_INFO_FILE" --result "$COPY_WATCH_DIR/result"
        --runner "$MAC_RUNNER" --settings "$MAC_LEAVE_SETTINGS"
        --repo-root "$(git rev-parse --show-toplevel)" --package "$PKG"
        --call-id "$BUILDLOG_CALL_ID" --call-started "$VERIFY_CALL_STARTED"
        --parent-pid "$parent_pid")
    if [[ "$FILTER_RUN" -eq 1 ]]; then
        watch_args+=(--filter-run)
    fi
    "$PY" "$MAC_COPY_WATCH" "${watch_args[@]}" -- "${NEXTEST_ARGS[@]}" &
    COPY_WATCH_PID=$!
}

# Stops the watcher, which stops a copy still running, and reads its result
# into COPY_* and the call's BUILDLOG_MAC_COPY* words. Safe to call twice.
stop_copy_watch() {
    local line copy_s="" copy_count="" natedev_s=""
    COPY_WORD="" COPY_MAC="" COPY_REASON="" COPY_SECONDS="" COPY_RUNNER_STATUS="" COPY_STOPPED=""
    [[ -n "$COPY_WATCH_PID" ]] || return 0
    kill -TERM "$COPY_WATCH_PID" 2>/dev/null || true
    wait "$COPY_WATCH_PID" 2>/dev/null || true
    COPY_WATCH_PID=""
    if [[ -f "$COPY_WATCH_DIR/result" ]]; then
        while IFS= read -r line; do
            case "$line" in
                copy=*) COPY_WORD="${line#copy=}" ;;
                copy_s=*) copy_s="${line#copy_s=}" ;;
                copy_count=*) copy_count="${line#copy_count=}" ;;
                natedev_s_at_end=*) natedev_s="${line#natedev_s_at_end=}" ;;
                mac=*) COPY_MAC="${line#mac=}" ;;
                reason=*) COPY_REASON="${line#reason=}" ;;
                seconds=*) COPY_SECONDS="${line#seconds=}" ;;
                runner_status=*) COPY_RUNNER_STATUS="${line#runner_status=}" ;;
                stopped_natedev=*) COPY_STOPPED="${line#stopped_natedev=}" ;;
            esac
        done < "$COPY_WATCH_DIR/result"
    fi
    rm -rf "$COPY_WATCH_DIR"
    COPY_WATCH_DIR="" VERIFY_STEP_INFO_FILE=""
    if [[ -n "$COPY_WORD" && "$copy_count" =~ ^[0-9]+$ && "$copy_s" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        BUILDLOG_MAC_COPY="$COPY_WORD"
        BUILDLOG_MAC_COPY_COUNT=$(( ${BUILDLOG_MAC_COPY_COUNT:-0} + copy_count ))
        BUILDLOG_MAC_COPY_S="$(LC_ALL=C awk -v total="${BUILDLOG_MAC_COPY_S:-0}" -v more="$copy_s" \
            'BEGIN { printf "%.3f", total + more }')"
        if [[ -n "$natedev_s" ]]; then
            BUILDLOG_MAC_COPY_NATEDEV_S="$natedev_s"
        fi
    else
        COPY_WORD=""
    fi
}

# The Mac copy finished first and stopped the natedev step: end the call as
# try_mac_test ends it for a Mac result. The run log holds both machines'
# output; a pass keeps it as the Mac's pass record.
finish_copy_win() {
    local log="$RUN_LOG"
    BUILDLOG_MAC="$COPY_MAC"
    BUILDLOG_MAC_REASON="$COPY_REASON"
    BUILDLOG_MAC_S="$COPY_SECONDS"
    BUILDLOG_MAC_HOLDER=""
    export BUILDLOG_TARGET=mac
    RUN_LOG=""
    if [[ "$COPY_MAC" == failed ]]; then
        [[ -z "$log" ]] || rm -f "$log"
        note_event failed "${SECONDS}" 0 "" 0 "$COPY_RUNNER_STATUS"
        exit "$COPY_RUNNER_STATUS"
    fi
    if [[ -z "$log" ]]; then
        log="$(mktemp)"
    fi
    finish_mac_pass "$log" "$COPY_SECONDS"
}

# Runs the tests on natedev. While MAC_LEAVE_SETTINGS names a Mac, a run held
# in the memory wait leaves once mac_test.py free-now says that Mac is free,
# and its Mac result counts as one from route_test: a failure or a pass ends
# the call. Other results keep the hook unless the Mac excludes this test.
# Other temporary declines wait for the retry deadline. Once the step compiles,
# copy_watch.py may run a copy on the Mac; a copy result that finishes first
# ends the call (finish_copy_win).
run_local_test() {
    local status hook copy_watch
    while true; do
        hook=""
        copy_watch=0
        if step_can_retry; then
            hook="$(ssh_retry_hook)"
            if [[ -n "$MAC_LEAVE_SETTINGS" && "$(target_platform "$(target_for_settings "$MAC_LEAVE_SETTINGS")")" == macos && -f "$MAC_COPY_WATCH" && -f "$MAC_RUNNER" ]]; then
                copy_watch=1
            fi
        fi
        export BUILDLOG_TARGET=natedev
        echo "verify.sh: running on natedev"
        status=0
        if [[ "$copy_watch" -eq 1 ]]; then
            start_copy_watch
        fi
        LINT_QUEUE_LEAVE_HOOK="$hook" run_nextest "${NEXTEST_ARGS[@]}" || status=$?
        if [[ "$copy_watch" -eq 1 ]]; then
            stop_copy_watch
            if [[ "$COPY_WORD" == won ]]; then
                if [[ "$COPY_STOPPED" == 1 && "$status" -ne 0 \
                      && ( "$COPY_MAC" == failed || "$COPY_MAC" == passed ) ]]; then
                    finish_copy_win
                fi
                # natedev ended before the stop reached it: its result stands.
                BUILDLOG_MAC_COPY=natedev_won
            fi
        fi
        if [[ -z "$hook" || "$status" -ne "$LINT_LEFT_QUEUE_STATUS" ]]; then
            return "$status"
        fi
        # The step never started and has no memory reservation.
        try_reserved_target
    done
}

# Succeeds, after printing the record, when this call already passed on this
# tree, or (lint) already failed on it; LOOKUP_STATUS is the status to exit
# with. Sets LOOKUP_KEY either way, --no-cache included.
cache_lookup() {
    local record outcome="" word
    [[ -n "${CACHE_DIR}" ]] || return 1
    LOOKUP_KEY="$(tree_key)"
    [[ -n "${LOOKUP_KEY}" && "${NO_CACHE}" -eq 0 ]] || return 1
    record="${CACHE_DIR}/${LOOKUP_KEY}"
    # --local needs a natedev pass; --mac needs a Mac pass.
    if [[ -f "${record}.pass" \
          && ! ( "$LOCAL_ONLY" == 1 && "$(sed -n 's/^host=//p' "${record}.pass")" == mac ) \
          && ! ( "$MAC_ONLY" == 1 && "$(sed -n 's/^host=//p' "${record}.pass")" != mac ) ]]; then
        record+=.pass outcome=reused word=PASS
    elif [[ -f "${record}.fail" ]]; then
        record+=.fail outcome=replayed word=FAIL
    fi
    [[ -n "${outcome}" ]] || return 1
    LOOKUP_STATUS="$(sed -n 's/^status=//p' "${record}")"
    LOOKUP_STATUS="${LOOKUP_STATUS:-0}"
    note_event "${outcome}" "${SECONDS}" 0 0 "$(sed -n 's/^saved_s=//p' "${record}")" \
        "${LOOKUP_STATUS}" || true
    if [[ "${FILTER_RUN}" -eq 1 ]]; then
        echo "verify.sh: ${word} (recorded), skipped — $(head -n 1 "${record}"); the tree and every input are unchanged since, so nothing was built or run (--no-cache runs it anyway)."
        return 0
    fi
    echo "verify.sh: ${word} (recorded) — $(head -n 1 "${record}")"
    if ! head -n 1 "${record}" | grep -qF "\`verify.sh $CMD${VERIFY_PACKAGE:+ $VERIFY_PACKAGE}${COMMAND_ARGS[*]:+ ${COMMAND_ARGS[*]}}\`"; then
        echo "verify.sh: lint covers the workspace and every changed member, so that result answers this package too."
    fi
    if [[ "${outcome}" == reused ]]; then
        echo "verify.sh: the tree and every input are unchanged since, so nothing was rebuilt or re-run."
        echo "verify.sh: that run's output is ${CACHE_DIR}/${LOOKUP_KEY}.log; its last lines:"
        tail -n 5 "${CACHE_DIR}/${LOOKUP_KEY}.log" 2>/dev/null || true
        echo "verify.sh: add --no-cache to run it anyway (a flake hunt)."
        return 0
    fi
    echo "verify.sh: the tree and every input are unchanged since, so lint would fail the same way; nothing was re-run. Its output:"
    cat "${CACHE_DIR}/${LOOKUP_KEY}.log" 2>/dev/null || true
    echo "verify.sh: FAIL (recorded), status ${LOOKUP_STATUS}: fix the error above, wherever it is in the workspace, then lint again. --no-cache runs it anyway."
}

# Cargo prints "in 0.23s" or "in 1m 23s"; the rest of a run is what a repeat on
# the same tree would pay again.
build_seconds() {
    { grep -aoE 'Finished .* in ([0-9]+m )?[0-9.]+s' "${RUN_LOG}" || true; } \
        | awk '{ t = $NF; sub(/s$/, "", t); m = 0
                 if ($(NF - 1) ~ /^[0-9]+m$/) { m = $(NF - 1); sub(/m$/, "", m) }
                 total += m * 60 + t }
               END { printf "%d", total }'
}

# Records a pass, or a lint failure, under the tree its verdict was judged on
# (VERDICT_KEY; lint's rewrites are part of it), before the token is released
# so a peer queued on the same call finds it. A record replaces the other kind
# on the same tree. A third word `mac` marks a pass the Mac ran (`host=mac`),
# which a --local call does not reuse. Fails when there is nothing to record,
# or when the tree has changed since that verdict.
# The time in the user's zone setting, with its label; UTC (labeled) when the setting cannot be read.
user_clock() {
    TZ="$("$PY" -S "$(dirname "${BASH_SOURCE[0]}")/../lib/user_zone.py" 2>/dev/null)" date '+%Y-%m-%d %H:%M:%S %Z'
}

cache_record() {
    local kind=$1 repeat=$2 host=${3:-} key verb=passed other=fail
    # A lost token gives no verdict about the tree.
    (( EXIT_STATUS != 125 )) || return 1
    if [[ "$CMD" == lint ]]; then
        [[ -n "$LINT_LOADED_CODE_KEY" && "$(lint_code_key)" == "$LINT_LOADED_CODE_KEY" ]] || return 1
    fi
    key="$(tree_key)"
    [[ -n "${key}" && -n "${VERDICT_KEY}" ]] || return 1
    if [[ "${key}" != "${VERDICT_KEY}" ]]; then
        if [[ -z "$VERDICT_TREE_KEY" || "$(tree_key --tree-content)" != "$VERDICT_TREE_KEY" ]]; then
            echo "verify.sh: the tree changed while ${CMD} ran, so this result was not recorded; the next ${CMD} runs again."
        fi
        return 1
    fi
    if [[ "${kind}" == fail ]]; then
        verb="failed with status ${EXIT_STATUS}" other=pass
    elif [[ "${host}" == mac ]]; then
        verb="passed on the Mac"
    fi
    mv -f "${RUN_LOG}" "${CACHE_DIR}/${key}.log"
    {
        printf '`verify.sh %s` %s %s, run by %s\nsaved_s=%d\nstatus=%d\n' \
            "$CMD${VERIFY_PACKAGE:+ $VERIFY_PACKAGE}${COMMAND_ARGS[*]:+ ${COMMAND_ARGS[*]}}" "${verb}" "$(user_clock)" \
            "${UNIT_TEAM_ROLE:-the unit director}" "${repeat}" "${EXIT_STATUS}"
        if [[ "${host}" == mac ]]; then
            printf 'host=mac\n'
        fi
    } > "${CACHE_DIR}/${key}.${kind}.tmp"
    mv -f "${CACHE_DIR}/${key}.${kind}.tmp" "${CACHE_DIR}/${key}.${kind}"
    rm -f "${CACHE_DIR}/${key}.${other}"
}

# Records a Mac pass, whose output is in the file $1, as this call's pass
# record. The tree is read before the Mac run (VERDICT_KEY) and again now, so
# an edit made while the Mac ran leaves nothing recorded. $2 is the Mac's
# seconds, what a reuse saves.
record_mac_pass() {
    local log=$1 mac_seconds=${2%%.*}
    exec 9>&-
    if [[ -z "${CACHE_DIR}" ]] || ! mkdir -p "${CACHE_DIR}"; then
        rm -f "$log" "${RUN_LOG}"
        RUN_LOG=""
        return 0
    fi
    # A natedev log from before the Mac took the call has nothing to keep.
    if [[ -n "${RUN_LOG}" && "${RUN_LOG}" != "$log" ]]; then
        rm -f "${RUN_LOG}"
    fi
    RUN_LOG="$log"
    EXIT_STATUS=0
    if ! cache_record pass "${mac_seconds:-0}" mac; then
        rm -f "$log"
    fi
    RUN_LOG=""
}

# A failed lint is the tree's own answer unless the run never reached a
# verdict: a usage or tooling error (2), the sandbox (3), or a kill. A fix pass
# that still changes the tree at the limit can leave more fixes for the next call.
lint_failure_is_the_tree() {
    [[ "$CMD" == lint ]] || return 1
    (( CLIPPY_FIX_LIMIT_REACHED == 0 )) || return 1
    (( EXIT_STATUS != 2 && EXIT_STATUS != 3 && EXIT_STATUS < 128 )) || return 1
    (( ${RUN_LAST_ATTEMPT_KILLED:-0} == 0 ))
}

# Logs the finished run and records a pass or a lint failure. Runs once:
# INT/TERM and then EXIT both reach it.
finish_run() {
    local status=$1 outcome=failed wall build repeat kind="" code="${EXIT_STATUS}"
    [[ -n "${RUN_LOG}" ]] || return 0
    wall=$(( SECONDS - RUN_STARTED ))
    build="$(build_seconds)"
    repeat=$(( wall - ${build:-0} ))
    if (( repeat < 0 )); then
        repeat=0
    fi
    case "${status}" in
        completed) outcome=ran kind=pass ;;
        interrupted) outcome=interrupted code="" ;;
        cancelled) outcome=cancelled ;;
        error) lint_failure_is_the_tree && kind=fail ;;
    esac
    note_event "${outcome}" "${RUN_STARTED}" "${wall}" "${build:-0}" 0 "${code}"
    if [[ -z "${kind}" || -z "${CACHE_DIR}" ]] || ! cache_record "${kind}" "${repeat}"; then
        rm -f "${RUN_LOG}"
    fi
    RUN_LOG=""
}

# Checked before the cache lookup and any build, so a recorded pass never
# stands in for the rule that examples carry no tests.
GATE_METADATA=""
export BUILDLOG_OUTPUT_ROOTS=""
CHECK_UNTESTED_EXAMPLES=0
if [[ "$CMD" == final || ( "$CMD" == test && ( ${#ARGS[@]} -eq 1 \
    || ( ${#ARGS[@]} -eq 3 && "${ARGS[1]}" == --features ) ) ) ]]; then
    CHECK_UNTESTED_EXAMPLES=1
    if [[ "$CMD" == final ]]; then
        EXAMPLE_SCOPE=--workspace
    else
        EXAMPLE_SCOPE="${ARGS[0]:---workspace}"
    fi
    load_metadata
fi
# A test record's key reads the package's dependencies from it (tree_key).
if [[ "$CMD" == test && -z "${GATE_METADATA}" ]]; then
    set_gate_metadata 2>/dev/null || true
fi

HOLD_WAIT_DIR="$(verify_helper "$BUILD_FOLDERS_SCRIPT" unique --kind verify-hold --owner "$$")"
if [[ "$CMD" == lint ]]; then
    # The call owns these files. Cleanup removes them with the wait directory.
    export VERIFY_LINT_VERSIONS_FILE="$HOLD_WAIT_DIR/lint-versions"
    export VERIFY_LINT_RUSTFMT_VERSION_FILE="$HOLD_WAIT_DIR/rustfmt-version"
fi
export BUILDLOG_HOLD_WAIT_FILE="$HOLD_WAIT_DIR/seconds"
export BUILDLOG_HOLD_WAITER_FILE="$HOLD_WAIT_DIR/waiter"
export BUILDLOG_CHECK_FAILED_FILE="$HOLD_WAIT_DIR/check-failed"
trap 'hold_status=$?; interrupt_hold_wait; finish_hold_wait; release_wait_claim; remove_step_record; final_pass_finish "$hold_status"; exit "$hold_status"' EXIT
hold_interrupted() {
    local signal_name=$1 code=$2
    # Ignore repeated signals while cleanup closes the hold record.
    trap '' INT TERM HUP
    if [[ -n "$HOLD_WAITER_PID" ]]; then
        # A background Bash sleep inherits ignored INT. End that waiter with
        # TERM for every caller signal; keep the caller's status in the record.
        if [[ "$MAC_ONLY" == 1 ]]; then signal_name=TERM; fi
        kill -s "$signal_name" "$HOLD_WAITER_PID" 2>/dev/null || true
        wait "$HOLD_WAITER_PID" 2>/dev/null || true
    fi
    interrupt_hold_wait
    note_event interrupted "$SECONDS" 0 "" 0 "$code"
    exit "$code"
}
trap 'hold_interrupted INT 130' INT
trap 'hold_interrupted TERM 143' TERM
trap 'hold_interrupted HUP 129' HUP
initialize_step_record
if cache_lookup; then
    exit "${LOOKUP_STATUS}"
fi
if [[ "$CMD" == test ]]; then
    compose_nextest_args
    route_test || exit $?
    if [[ "$MAC_ONLY" == 1 ]]; then
        BUILDLOG_MAC=declined BUILDLOG_MAC_REASON=runner
        mac_only_refuse runner
    fi
fi
while [[ -z "$PINNED_TARGET" ]] && ! build_hold_nothing_held; do
    step_state waiting_hold
    if [[ "$CMD" != test ]] && step_can_retry; then
        HOLD_WAIT_STATUS=0
        check_routing_hold || HOLD_WAIT_STATUS=$?
        case "$HOLD_WAIT_STATUS" in
            0) break ;;
            10)
                STEP_DEFER_LOCAL=1
                if [[ "$CMD" == lint ]]; then HOLD_LINT=1; fi
                break ;;
            *) exit "$HOLD_WAIT_STATUS" ;;
        esac
    fi
    phase_wait_start hold "build hold"
    HOLD_WAIT_ARGS=(wait-local --call-id "$BUILDLOG_CALL_ID" --pid $$)
    if [[ "$CMD" == test && -n "$MAC_LEAVE_SETTINGS" ]]; then
        HOLD_WAIT_ARGS+=(--leave-hook "$(ssh_retry_hook)")
    fi
    "$PY" "$BUILD_TARGETS_SCRIPT" "${HOLD_WAIT_ARGS[@]}" &
    HOLD_WAITER_PID=$!
    HOLD_WAIT_STATUS=0
    wait "$HOLD_WAITER_PID" || HOLD_WAIT_STATUS=$?
    HOLD_WAITER_PID=""
    phase_wait_finish hold
    if [[ "$HOLD_WAIT_STATUS" == "$LINT_LEFT_QUEUE_STATUS" && -n "$MAC_LEAVE_SETTINGS" ]]; then
        try_reserved_target
        continue
    fi
    if [[ "$HOLD_WAIT_STATUS" != 0 ]]; then
        build_hold_check_result "$HOLD_WAIT_STATUS" wait-local || exit "$HOLD_WAIT_STATUS"
    fi
    break
done
trap - INT TERM HUP

# Open a progress window for the duration of this run when a worker session is
# in scope. This is what the unit director's progress header reports against
# while it runs verification itself: an activity, not a pass, so findings.py
# never counts it toward convergence. The launcher-owns-its-own-pass
# rule applies here too — the thing that runs the work opens the window.
PROGRESS_HISTORY="${HOME}/.claude/scripts/direct/progress_history.py"
ACTIVITY_SESSION_DIR="${UNIT_SESSION_DIR:-}"

# A phase runs two workers against one target/ directory and one Cargo lock,
# so every cargo run below has to be serialized against its peers. Taking the
# token here rather than asking each worker's prompt to take it is deliberate:
# a rule that lives only in a prompt is a rule an agent can drop, and dropping
# this one blocks the whole team behind a lock nobody announced.
#
# UNIT_RUN_DIR is deliberately not UNIT_SESSION_DIR: that
# variable also opens a progress activity window, and concurrent windows
# would collide in a recorder that keeps one.
BOARD_HELPER="${HOME}/.claude/scripts/direct/board.sh"
BOARD_DIR="${UNIT_RUN_DIR:-}"
BOARD_SLOT="${UNIT_TEAM_ROLE:-}"
# The unit director's own run (a session dir, no board, no team role) takes the
# token too, as slot `director` on the session's board, so it queues with the
# workers' detached runs instead of building beside them.
if [[ -z "${BOARD_DIR}" && -z "${BOARD_SLOT}" && -n "${UNIT_SESSION_DIR:-}" ]]; then
    BOARD_DIR="${UNIT_SESSION_DIR}"
    BOARD_SLOT=director
fi
# TOKEN_USED: this run takes the token. TOKEN_HELD: it holds the token now. The
# token is released during holds and remote work.
TOKEN_USED=0
TOKEN_HELD=0
# A step routed to the Mac skips its first token acquisition.
TOKEN_WAITED=0
TOKEN_WAITER_PID=""
# shellcheck source=/dev/null
source "${BASH_SOURCE[0]%/*}/../lib/conf.sh"
TOKEN_HOLD_S="$(conf_value direct VERIFY_TOKEN_HOLD_SECONDS)" || exit 2
TOKEN_RENEW_S="$(conf_value direct VERIFY_TOKEN_RENEW_SECONDS)" || exit 2
TOKEN_RENEW_S=$(awk -v interval="$TOKEN_RENEW_S" -v hold="$TOKEN_HOLD_S" \
    'BEGIN { if (interval > hold / 3) interval = hold / 3; print interval }')
token_wait_clock() {
    # Tests can advance a disposable clock while acquisition still blocks on
    # the real board. Normal calls retain Bash's elapsed-seconds clock.
    if [[ -n "${UNIT_TOKEN_CLOCK_FILE:-}" ]]; then
        read -r TOKEN_CLOCK_NOW < "$UNIT_TOKEN_CLOCK_FILE"
    else
        TOKEN_CLOCK_NOW=$SECONDS
    fi
}
# A run ended during its token wait stops the waiting acquire, gives back a
# token that acquire took in the last instant, then ends by its caller's trap,
# or by the signal itself when the caller has none.
token_wait_interrupted() {
    local signal_name=$1
    trap - INT TERM HUP
    if [[ -n "$TOKEN_WAITER_PID" ]]; then
        kill "$TOKEN_WAITER_PID" 2>/dev/null || true
        wait "$TOKEN_WAITER_PID" 2>/dev/null || true
    fi
    bash "${BOARD_HELPER}" release "${BOARD_DIR}" "${BOARD_SLOT}" cargo --pid $$ >/dev/null 2>&1 || true
    [[ -z "$TOKEN_WAIT_ENTRY" ]] || rm -f "$TOKEN_WAIT_ENTRY"
    eval "$TOKEN_CALLER_TRAPS"
    kill -s "$signal_name" "${BASHPID:-$$}"
}
# A newer call from the same worker, with the same verb and arguments in the
# same worktree, ends this one while it still waits for its first token: the
# newer call checks a newer tree, so this verdict is no longer wanted (one
# worker queued 27 such calls on 2026-10-21, and peers waited 20 h behind
# them). A lint key leaves out its package, since every lint covers the
# workspace. A waiting call keeps a file named <slot>.<key>.<start>.<pid> in
# the board's token_waiters folder; a newer call sends SIGUSR1 to each older one that still
# runs verify.sh. A call that holds the token runs to the end, and a signal
# that lands after the wait does nothing.
#
# The same files make one queue: a call tries for the token only while no
# older call that still runs verify.sh waits. Before, any poller took a freed
# token, and with about 25 calls on one board two calls waited 3 and 3.5 h
# behind newer ones (2026-10-22). A call that releases the token during a
# hold waits again under its first start, in a file with an `.again`
# suffix: it keeps its place, and a newer call does not supersede it, since it
# already took the token once.
VERIFY_SUPERSEDED_STATUS=76
TOKEN_WAITERS_DIR=""
TOKEN_WAITER_PREFIX=""
TOKEN_WAITER_FILE=""
TOKEN_WAITER_OWN_STAMP=0
TOKEN_WAIT_ENTRY=""
TOKEN_FIRST_WAIT_DONE=0
TOKEN_WAITERS_AHEAD=0
TOKEN_SUPERSEDED=0
# Minutes between the "still waiting for the cargo token" lines.
TOKEN_WAIT_NOTE_MIN=5
trap ':' USR1
token_waiters_key() {
    local key_args=(${ARGS[@]+"${ARGS[@]}"})
    if [[ "$CMD" == lint && ${#key_args[@]} -gt 0 && "${key_args[0]}" != --* ]]; then
        key_args=("${key_args[@]:1}")
    fi
    local hash
    hash="$( { printf '%s' "$(pwd -P)"; printf '\0%s' "$CMD" ${key_args[@]+"${key_args[@]}"}; } | hash_stream)"
    printf '%s\n' "${hash:0:16}"
}
supersede_older_waiters() {
    local file name pid stamp own=${TOKEN_WAITER_FILE%.*}
    own=${own##*.}
    for file in "$TOKEN_WAITERS_DIR/$TOKEN_WAITER_PREFIX".*.*; do
        [[ -f "$file" ]] || continue
        name=${file##*/}
        pid=${name##*.}
        stamp=${name%.*}
        stamp=${stamp##*.}
        [[ "$pid" =~ ^[0-9]+$ && "$stamp" =~ ^[0-9]+$ && "$pid" != "$$" ]] || continue
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$file"
        elif (( 10#$stamp < 10#$own )) \
            && ps -p "$pid" -o args= 2>/dev/null | grep -q 'verify\.sh'; then
            kill -USR1 "$pid" 2>/dev/null || true
        fi
    done
}
token_waiter_runs_verify() {
    local pid=$1 args=()
    kill -0 "$pid" 2>/dev/null || return 1
    if [[ -r "/proc/$pid/cmdline" ]]; then
        mapfile -d '' args < "/proc/$pid/cmdline" 2>/dev/null || return 1
        [[ "${args[*]}" == *verify.sh* ]]
    else
        ps -p "$pid" -o args= 2>/dev/null | grep -q 'verify\.sh'
    fi
}
# Sets TOKEN_WAITERS_AHEAD to the waiting calls that started before this one,
# and removes the files of calls that no longer run.
count_token_waiters_ahead() {
    local file name pid stamp
    TOKEN_WAITERS_AHEAD=0
    [[ -n "$TOKEN_WAITERS_DIR" ]] || return 0
    for file in "$TOKEN_WAITERS_DIR"/*; do
        [[ -f "$file" ]] || continue
        name=${file##*/}
        name=${name%.again}
        pid=${name##*.}
        stamp=${name%.*}
        stamp=${stamp##*.}
        [[ "$pid" =~ ^[0-9]+$ && "$stamp" =~ ^[0-9]+$ && "$pid" != "$$" ]] || continue
        if ! token_waiter_runs_verify "$pid"; then
            rm -f "$file"
        elif (( 10#$stamp < 10#$TOKEN_WAITER_OWN_STAMP )); then
            TOKEN_WAITERS_AHEAD=$(( TOKEN_WAITERS_AHEAD + 1 ))
        fi
    done
}
# Prints the token wait to the log: a line when it starts, one every
# TOKEN_WAIT_NOTE_MIN minutes, and one when the token is taken.
token_wait_note() {
    local kind=$1 waited_min=$2 holder="" holder_pid="" who
    if [[ -r "${BOARD_DIR}/locks/cargo.d/holder" ]]; then
        holder=$(< "${BOARD_DIR}/locks/cargo.d/holder")
    fi
    if [[ -r "${BOARD_DIR}/locks/cargo.d/holder_pid" ]]; then
        holder_pid=$(< "${BOARD_DIR}/locks/cargo.d/holder_pid")
    fi
    if [[ "$holder_pid" == "$$" ]]; then
        who="this call holds it (pid $holder_pid)"
    elif [[ -n "$holder" ]]; then
        who="$holder (pid ${holder_pid:-unknown}) holds it"
    elif (( TOKEN_WAITERS_AHEAD > 0 )); then
        who="it is free and goes to an earlier call"
    else
        who="it came free just now"
    fi
    case "$kind" in
        first) printf 'waiting for the cargo token since %s: %s; calls ahead of this one: %d\n' \
            "$(TZ="$("$PY" -S "$(dirname "${BASH_SOURCE[0]}")/../lib/user_zone.py" 2>/dev/null)" date '+%H:%M %Z')" \
            "$who" "$TOKEN_WAITERS_AHEAD" >&2 ;;
        still) printf 'still waiting for the cargo token (%d min): %s; calls ahead of this one: %d\n' \
            "$waited_min" "$who" "$TOKEN_WAITERS_AHEAD" >&2 ;;
        taken) printf 'took the cargo token after %d min\n' "$waited_min" >&2 ;;
    esac
}
# Ends a call that a newer one replaced. Its caller reads this as no verdict,
# never as a failure to repair: the newer call's result answers instead.
# A step whose call is gone has no one to give a result to, and the token
# never comes: board.sh answers a dead --pid with "gone" (exit 4). On
# 2026-10-20 a step a test left behind tried again here for 45 h.
token_wait_orphaned() {
    [[ -z "$TOKEN_WAIT_ENTRY" ]] || rm -f "$TOKEN_WAIT_ENTRY"
    trap - INT TERM HUP
    echo "verify.sh: the call that started this step is gone, so the step stops waiting for the cargo token." >&2
    exit 1
}
token_wait_superseded() {
    bash "${BOARD_HELPER}" release "${BOARD_DIR}" "${BOARD_SLOT}" cargo --pid $$ >/dev/null 2>&1 || true
    rm -f "$TOKEN_WAIT_ENTRY"
    trap - INT TERM HUP
    TOKEN_WAIT_S=$TOKEN_WAITED
    printf 'verify.sh: superseded by a newer %s call; this call ran nothing. Read the newer call'"'"'s result: nothing failed and nothing needs a repair.\n' "$CMD"
    note_event superseded "$SECONDS" 0 "" 0 "$VERIFY_SUPERSEDED_STATUS"
    exit "$VERIFY_SUPERSEDED_STATUS"
}
# A release stays visible to every shell until an acquisition succeeds.
sync_token_state() {
    if [[ -n "${TOKEN_WAIT_FILE:-}" && -e "${TOKEN_WAIT_FILE}.given_back" ]]; then
        TOKEN_HELD=0
    fi
}

token_acquired() {
    TOKEN_HELD=1
    [[ -z "${TOKEN_WAIT_FILE:-}" ]] || rm -f "${TOKEN_WAIT_FILE}.given_back"
}

# Dead holders release the token. The queue removes waiters whose verify.sh ended.
# TOKEN_WAITED records the elapsed wait in seconds.
# The local argument disables remote routing for a local formatter.
record_token_wait() {
    if (( TOKEN_WAITED > 0 )) && [[ -n "${TOKEN_WAIT_FILE:-}" ]]; then
        printf '%s\n' "$TOKEN_WAITED" >> "$TOKEN_WAIT_FILE"
    fi
}

acquire_token() {
    sync_token_state
    TOKEN_WAITED=0
    [[ "${TOKEN_USED}" -eq 1 && "${TOKEN_HELD}" -eq 0 ]] || return 0
    local started noted=-1 waited pause
    # A step's subshell has no traps of its own, though trap -p there prints
    # the parent's: restoring those would run the parent's cleanup in the step.
    TOKEN_CALLER_TRAPS=""
    if [[ "${BASHPID:-$$}" == "$$" ]]; then
        TOKEN_CALLER_TRAPS="$(trap -p INT TERM HUP)"
    fi
    trap 'token_wait_interrupted INT' INT
    trap 'token_wait_interrupted TERM' TERM
    trap 'token_wait_interrupted HUP' HUP
    token_wait_clock
    started=$TOKEN_CLOCK_NOW
    TOKEN_WAIT_ENTRY=""
    if [[ -n "$TOKEN_WAITER_FILE" ]]; then
        TOKEN_WAIT_ENTRY=$TOKEN_WAITER_FILE
        (( TOKEN_FIRST_WAIT_DONE == 0 )) || TOKEN_WAIT_ENTRY="${TOKEN_WAITER_FILE}.again"
        : > "$TOKEN_WAIT_ENTRY"
    fi
    local status
    while :; do
        if (( TOKEN_SUPERSEDED )); then
            token_wait_clock
            TOKEN_WAITED=$(( TOKEN_CLOCK_NOW - started ))
            token_wait_superseded
        fi
        kill -0 "$$" 2>/dev/null || token_wait_orphaned
        count_token_waiters_ahead
        if (( noted >= 0 )); then phase_wait_start token "cargo token"; fi
        # The line goes out before the first try when the token is not free.
        if (( noted < 0 )) && { (( TOKEN_WAITERS_AHEAD > 0 )) || [[ -d "${BOARD_DIR}/locks/cargo.d" ]]; }; then
            phase_wait_start token "cargo token"
            token_wait_note first 0
            noted=0
        fi
        status=1
        pause=3
        if (( TOKEN_WAITERS_AHEAD == 0 )); then
            # Two tries 3 s apart, then the queue is read again.
            pause=1
            bash "${BOARD_HELPER}" acquire "${BOARD_DIR}" "${BOARD_SLOT}" cargo \
                --pid $$ --hold "$TOKEN_HOLD_S" --wait 3 >/dev/null 2>&1 &
            TOKEN_WAITER_PID=$!
            status=0
            wait "$TOKEN_WAITER_PID" || status=$?
            # SIGUSR1 ends the wait builtin early; the acquire may have won first.
            if (( status != 0 && TOKEN_SUPERSEDED )); then
                kill "$TOKEN_WAITER_PID" 2>/dev/null || true
                status=0
                wait "$TOKEN_WAITER_PID" 2>/dev/null || status=$?
            fi
            if (( status == 0 )); then
                TOKEN_SUPERSEDED=0
                break
            fi
            TOKEN_WAITER_PID=""
            (( status != 4 )) || token_wait_orphaned
        fi
        if [[ "${1:-}" != local && "$CMD" == test && -n "$MAC_LEAVE_SETTINGS" ]] \
              && eval "$(ssh_retry_hook)" >/dev/null 2>&1; then
            phase_wait_finish token
            try_reserved_target
        elif [[ "${1:-}" != local && "$CMD" != test ]] && step_can_retry \
              && eval "$(ssh_retry_hook)" >/dev/null 2>&1; then
            STEP_FORCE_MAC=1
            phase_wait_finish token
            return "$LINT_LEFT_QUEUE_STATUS"
        fi
        token_wait_clock
        waited=$(( (TOKEN_CLOCK_NOW - started) / 60 ))
        if (( noted < 0 )); then
            phase_wait_start token "cargo token"
            token_wait_note first 0
            noted=0
        elif (( waited >= noted + TOKEN_WAIT_NOTE_MIN )); then
            token_wait_note still "$waited"
            noted=$waited
        fi
        (( TOKEN_SUPERSEDED )) || sleep "$pause"
    done
    phase_wait_finish token
    TOKEN_WAITER_PID=""
    token_acquired
    TOKEN_FIRST_WAIT_DONE=1
    [[ -z "$TOKEN_WAIT_ENTRY" ]] || rm -f "$TOKEN_WAIT_ENTRY"
    TOKEN_WAIT_ENTRY=""
    trap - INT TERM HUP
    eval "$TOKEN_CALLER_TRAPS"
    token_wait_clock
    TOKEN_WAITED=$(( TOKEN_CLOCK_NOW - started ))
    (( noted < 0 )) || token_wait_note taken $(( TOKEN_WAITED / 60 ))
}
if [[ -n "${BOARD_DIR}" && -n "${BOARD_SLOT}" && -f "${BOARD_HELPER}" ]]; then
    TOKEN_USED=1
    TOKEN_WAITERS_DIR="${BOARD_DIR}/token_waiters"
    TOKEN_WAITER_PREFIX="${BOARD_SLOT}.$(token_waiters_key)"
    mkdir -p "$TOKEN_WAITERS_DIR"
    TOKEN_WAITER_STAMP=${EPOCHREALTIME:-$(date +%s).000000}
    TOKEN_WAITER_OWN_STAMP=${TOKEN_WAITER_STAMP/[.,]/}
    TOKEN_WAITER_FILE="${TOKEN_WAITERS_DIR}/${TOKEN_WAITER_PREFIX}.${TOKEN_WAITER_OWN_STAMP}.$$"
    trap 'TOKEN_SUPERSEDED=1' USR1
    supersede_older_waiters
    if [[ "$STEP_DEFER_LOCAL" == 0 ]]; then
        initial_token_status=0
        acquire_token || initial_token_status=$?
        if [[ "$initial_token_status" == "$LINT_LEFT_QUEUE_STATUS" ]]; then
            STEP_DEFER_LOCAL=1
        elif [[ "$initial_token_status" != 0 ]]; then
            exit "$initial_token_status"
        fi
    fi
    trap ':' USR1
    TOKEN_WAIT_S=$TOKEN_WAITED
fi

# Fork before starting each step. The child creates its own session, records
# its group leader under the token guard, and only then starts the command.
# This also covers the first instant of a step, when a killed verify cannot
# leave a command that a periodic process scan has not found yet.
STEP_OWNER_PY='
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

lock = Path(sys.argv[1])
holder = sys.argv[2]
command = sys.argv[3:]

def shell_status(status: int) -> int:
    return status if status >= 0 else 128 - status

with open(Path(os.environ["VERIFY_PROCESS_GROUP_LIB"]) / "package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.lib.process_start import Started, process_start, process_start_format

def identity(pid):
    result = process_start(pid)
    if not isinstance(result, Started):
        raise RuntimeError("cannot read the step owner start")
    return result.start

child = os.fork()
if child:
    def forward(signum, _frame):
        try:
            if (sent := subprocess.run([str(Path(os.environ["VERIFY_PROCESS_GROUP_LIB"]) / "py"), str(Path(os.environ["VERIFY_PROCESS_GROUP_LIB"]) / "process_group.py"), str(child), str(signum)], capture_output=True, text=True)).returncode: raise ProcessLookupError if sent.returncode == 3 else SystemExit("verify.sh: process group helper failed: " + " ".join(sent.stderr.splitlines()))
        except ProcessLookupError:
            # The child has not created its new group yet, or has exited.
            try:
                os.kill(child, signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    _, status = os.waitpid(child, 0)
    sys.exit(shell_status(os.waitstatus_to_exitcode(status)))

os.setsid()
with (lock.parent / "cargo.guard").open("a+") as guard:
    fcntl.flock(guard, fcntl.LOCK_EX)
    # A token released or reclaimed during the memory wait leaves no holder_pid,
    # or a holder that recorded none: either way this run no longer holds it.
    try:
        recorded = (lock / "holder_pid").read_text()
    except OSError:
        recorded = ""
    if recorded != holder:
        lost_file = os.environ.get("VERIFY_TOKEN_LOST_FILE")
        if lost_file:
            Path(lost_file).write_text("lost")
        print("verify.sh: lost the cargo token before its step started", file=sys.stderr)
        sys.exit(125)
    record = lock / f"owned.{os.getpid()}.tmp"
    record.write_text(json.dumps({"group": os.getpid(), "start": identity(os.getpid()), "start_format": process_start_format()}))
    os.replace(record, lock / "owned")
    fcntl.flock(guard, fcntl.LOCK_UN)

sys.exit(shell_status(subprocess.call(command)))
'

# Preserve the shared invocation policy (including systemd scopes and its
# fallback), changing only the command it launches while this token is held.
eval "$(declare -f buildlog_exec | sed '1s/buildlog_exec/buildlog_exec_unowned/')"
renew_token() {
    local status=0
    bash "${BOARD_HELPER}" renew "${BOARD_DIR}" "${BOARD_SLOT}" cargo \
        --pid $$ --hold "$TOKEN_HOLD_S" >/dev/null 2>&1 || status=$?
    # Status 2 means the guard cannot confirm ownership. Continue renewal.
    (( status == 0 || status == 2 ))
}

# Each span owns one renewer. Nested spans use the same renewer.
# Release stops the renewer before ownership changes.
start_token_renewer() {
    [[ "${TOKEN_HELD:-0}" -eq 1 ]] || return 0
    renew_token || return 1
    local renew_parent=${BASHPID:-$$}
    (
        local renew_sleep_pid="" renew_previous_pid="" renew_sleep_starting=0
        # Bash can retain pipeline jobs that this subshell cannot wait for.
        # Stop and collect only the sleep that this renewer started.
        trap '
            trap "" INT TERM HUP
            # Bash sets $! before it can run this trap after the sleep starts.
            if (( renew_sleep_starting )) && [[ "${!:-}" != "$renew_previous_pid" ]]; then
                renew_sleep_pid=$!
            fi
            if [[ -n "$renew_sleep_pid" ]]; then
                kill "$renew_sleep_pid" 2>/dev/null || true
                wait "$renew_sleep_pid" 2>/dev/null || true
            fi
            exit 0
        ' INT TERM HUP
        while kill -0 "$$" 2>/dev/null && kill -0 "$renew_parent" 2>/dev/null; do
            renew_previous_pid=${!:-}
            renew_sleep_starting=1
            sleep "$TOKEN_RENEW_S" &
            renew_sleep_pid=$!
            renew_sleep_starting=0
            wait "$renew_sleep_pid" || break
            renew_sleep_pid=""
            renew_token || break
        done
    ) &
    TOKEN_RENEWER_PID=$!
}

stop_token_renewer() {
    [[ -n "${TOKEN_RENEWER_PID:-}" ]] || return 0
    kill "$TOKEN_RENEWER_PID" 2>/dev/null || true
    wait "$TOKEN_RENEWER_PID" 2>/dev/null || true
    TOKEN_RENEWER_PID=""
}

with_token_renewal() {
    # A nested call must keep its caller's renewer.
    if [[ -n "${TOKEN_RENEWER_PID:-}" ]] && kill -0 "$TOKEN_RENEWER_PID" 2>/dev/null; then
        "$@"
        return $?
    fi
    local TOKEN_RENEWER_PID="" status=0
    # Admission or the command guard handles confirmed ownership loss.
    start_token_renewer || true
    "$@" || status=$?
    stop_token_renewer
    return "$status"
}

# Routing can retain the token during remote waits. Renew it during those waits.
eval "$(declare -f route_test | sed '1s/route_test/route_test_without_renewal/')"
route_test() {
    with_token_renewal route_test_without_renewal
}

buildlog_exec() {
    if [[ "${TOKEN_HELD}" -eq 1 ]]; then
        local TOKEN_RENEWER_PID="" step_status=0
        if ! start_token_renewer; then
            printf lost > "${HOLD_WAIT_DIR:?}/token-lost"
            printf 'verify.sh: lost the cargo token before its step started\n' >&2
            return 125
        fi
        VERIFY_TOKEN_LOST_FILE="${HOLD_WAIT_DIR:+$HOLD_WAIT_DIR/token-lost}" \
            VERIFY_PY_HELPER=STEP_OWNER_PY buildlog_exec_unowned env VERIFY_PROCESS_GROUP_LIB="$(dirname -- "${STEP_RECORD_SCRIPT:-$BOARD_HELPER}")/../lib" "$PY" -c "$STEP_OWNER_PY" \
                "${BOARD_DIR}/locks/cargo.d" "$$" "$@" || step_status=$?
        stop_token_renewer
        return "$step_status"
    else
        buildlog_exec_unowned "$@"
    fi
}

# Retry only when the ownership guard started no command.
# run_once releases the reservation and sampler before this call.
buildlog_retry_unstarted_step() {
    BUILDLOG_RETRY_STEP=0
    [[ -s "$HOLD_WAIT_DIR/token-lost" ]] || return 0
    rm -f "$HOLD_WAIT_DIR/token-lost"
    # shellcheck disable=SC2034 # invoke.sh reads this callback result.
    BUILDLOG_RETRY_STEP=1
    TOKEN_HELD=0
    if ! buildlog_step_compiles "$@"; then
        acquire_token local || return $?
        record_token_wait
    fi
}

release_token() {
    stop_token_renewer
    # A subshell records its release for the parent shell.
    # A killed acquisition can still hold the token, so release that token too.
    if [[ -n "${TOKEN_WAIT_FILE:-}" && -e "${TOKEN_WAIT_FILE}.given_back" ]]; then
        if [[ "${TOKEN_HELD}" -eq 1 ]]; then
            bash "${BOARD_HELPER}" release "${BOARD_DIR}" "${BOARD_SLOT}" cargo --pid $$ >/dev/null 2>&1 || true
        fi
        TOKEN_HELD=0
    fi
    [[ "${TOKEN_HELD}" -eq 1 ]] || return 0
    local release_status=0
    bash "${BOARD_HELPER}" release "${BOARD_DIR}" "${BOARD_SLOT}" cargo --pid $$ >/dev/null \
        || release_status=$?
    # An ownership identity change permits reclaim. Preserve the original step result.
    (( release_status == 0 || release_status == 3 )) || return "$release_status"
    TOKEN_HELD=0
    if [[ -n "${TOKEN_WAIT_FILE:-}" ]]; then
        : > "${TOKEN_WAIT_FILE}.given_back"
    fi
}

# A local attempt takes its token before it requests memory.
# The periodic admission hook renews the token throughout the memory wait.
eval "$(declare -f buildlog_wait_for_memory | sed '1s/buildlog_wait_for_memory/buildlog_wait_for_memory_only/')"
buildlog_wait_for_memory() {
    local status=0 cleanup_status=0 renewal
    local leave_hook="${LINT_QUEUE_LEAVE_HOOK:-${BUILDLOG_MAC_IDLE_HOOK:-}}"
    if [[ "${LINT_MEND_WAIT:-0}" == 1 ]]; then
        leave_hook="$(STEP_WRITES_TREE=0 ssh_retry_hook)"
    fi
    BUILDLOG_MEM_WAIT_S=0
    step_state waiting_hold
    sync_token_state
    acquire_token || return $?
    if (( TOKEN_WAITED > 0 )) && [[ -n "${TOKEN_WAIT_FILE:-}" ]]; then
        printf '%s\n' "$TOKEN_WAITED" >> "$TOKEN_WAIT_FILE"
    fi
    if [[ "${TOKEN_USED}" == 1 ]]; then
        # Renew only this call's lease. The execution guard still checks ownership.
        printf -v renewal 'bash -c %q verify-token %q %q %q %q' \
            '[[ "$(<"$1/locks/cargo.d/holder_pid")" == "$4" ]] && bash "$2" renew "$1" "$3" cargo --hold 3600 >/dev/null; exit 1' \
            "$BOARD_DIR" "$BOARD_HELPER" "$BOARD_SLOT" "$$"
        leave_hook="$renewal; ${leave_hook:-false}"
    fi
    local LINT_QUEUE_LEAVE_HOOK="$leave_hook"
    export LINT_QUEUE_LEAVE_HOOK
    with_token_renewal buildlog_wait_for_memory_only "$@" || status=$?
    if (( status != 0 )) || [[ "$BUILDLOG_MEM_OUTCOME" != Granted ]]; then
        release_token || cleanup_status=$?
        (( status != 0 )) || status=$cleanup_status
        return "$status"
    fi
    step_state on_natedev
}

# A build hold stops every natedev build, so a step gives the token back while
# it waits out a hold too, and takes it again after the release; a step that
# leaves the hold wait for the Mac (status LINT_LEFT_QUEUE_STATUS) does not. The
# hold time stays in the hold wait record; only the time to take the token
# again counts as token wait. With no hold present, nothing is given back.
refresh_test_build_inputs() {
    # Admission and the recorder must see the inputs after a hold, just as
    # buildlog_begin takes its source key after that wait.
    rm -f "$HOLD_WAIT_DIR/test-build-inputs"
    VERIFY_COMPUTE_TEST_BUILD=1 set_verdict_keys
    export BUILDLOG_TEST_BUILD_KEY="" BUILDLOG_TEST_TARGET_DIR="" BUILDLOG_TEST_BUILD_DIR=""
    if [[ -f "$HOLD_WAIT_DIR/test-build-inputs" ]]; then
        { IFS= read -r BUILDLOG_TEST_BUILD_KEY; IFS= read -r BUILDLOG_TEST_TARGET_DIR;
          IFS= read -r BUILDLOG_TEST_BUILD_DIR; } \
            < "$HOLD_WAIT_DIR/test-build-inputs"
    fi
    # A hold can also arrive during memory admission, after buildlog_begin.
    if [[ -n "${BUILDLOG_START:-}" ]]; then
        buildlog_read_context ${BUILDLOG_STEP_ARGV[@]+"${BUILDLOG_STEP_ARGV[@]}"}
    fi
    TEST_BUILD_INPUTS_READY=1
}
TEST_BUILD_INPUTS_READY=0

eval "$(declare -f build_hold_wait_local | sed '1s/build_hold_wait_local/build_hold_wait_local_only/')"
build_hold_wait_local() {
    local status=0
    if ! build_hold_applies || build_hold_nothing_held; then
        return 0
    fi
    step_state waiting_hold
    phase_wait_start hold "build hold"
    # A step without the token (before the first take, or after a Mac trip) only waits.
    if [[ "${TOKEN_HELD:-0}" -ne 1 ]]; then
        build_hold_wait_local_only "$@" || status=$?
        phase_wait_finish hold
        if [[ "${status:-0}" -ne 0 ]]; then return "$status"; fi
        if [[ "$TEST_BUILD_INPUTS_READY" == 1 ]]; then refresh_test_build_inputs; fi
        return 0
    fi
    release_token || return $?
    build_hold_wait_local_only "$@" || status=$?
    phase_wait_finish hold
    [[ "$status" == 0 ]] || return "$status"
    acquire_token || return $?
    record_token_wait
    if [[ "$TEST_BUILD_INPUTS_READY" == 1 ]]; then refresh_test_build_inputs; fi
    return "$status"
}

# Again with the token held: a peer running this same call on this tree held
# the token until its result was recorded.
if cache_lookup; then
    if ! release_token && (( LOOKUP_STATUS == 0 )); then
        LOOKUP_STATUS=1
    fi
    exit "${LOOKUP_STATUS}"
fi
RUN_STARTED=${SECONDS}
MEM_KILL_FILE="$(mktemp)"
TOKEN_WAIT_FILE="$(mktemp)"
if [[ "$CMD" == test ]]; then
    # The common path calculates once; a later hold refreshes after its wait.
    refresh_test_build_inputs
fi
run_log_tee() {
    # Keep readers alive while verify_interrupted stops the step and records its result.
    # An early reader exit can cause SIGPIPE during cleanup.
    trap '' INT TERM HUP
    exec tee -i -a "$RUN_LOG"
}

start_run_log() {
    exec > >(run_log_tee)
    RUN_LOG_PIDS+=("$!")
    exec 2> >(run_log_tee >&2)
    RUN_LOG_PIDS+=("$!")
}

stop_interrupted_run_log() {
    # Stop blocked readers after cleanup. They can ignore EOF while their output pipe is full.
    [[ ${#RUN_LOG_PIDS[@]} -gt 0 ]] || return 0
    set +x
    exec >/dev/null 2>&1
    local pid
    for pid in "${RUN_LOG_PIDS[@]}"; do kill -KILL "$pid" 2>/dev/null || true; done
    for pid in "${RUN_LOG_PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
    RUN_LOG_PIDS=()
}

if [[ -n "${CACHE_DIR}" ]] && mkdir -p "${CACHE_DIR}"; then
    # Taken now, after the token and hold waits: an edit made while this call
    # waited is part of the tree it runs on.
    if [[ "$CMD" != test ]]; then set_verdict_keys; fi
    RUN_LOG="${CACHE_DIR}/run.$$.log"
    : > "${RUN_LOG}"
    start_run_log
fi

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
                cancelled) status=canceled ;;
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
    local status=$1 outcome=ran code="${EXIT_STATUS}"
    if [[ "$EXIT_STATUS" == "$TREE_CHANGED_STATUS" ]]; then status=cancelled; fi
    stop_tree_watch
    [[ -z "$TREE_WATCH_DIR" ]] || rm -rf "$TREE_WATCH_DIR"
    TREE_WATCH_DIR=""
    if [[ "$STEP_RECORD_ACTIVE" == 1 ]]; then
        publish_step_state waiting_hold || true
    fi
    stop_copy_watch || true
    if [[ "$status" != interrupted ]]; then interrupt_hold_wait; fi
    finish_run "${status}" || true
    # finish_run notes only a run with a record log; every other call is noted
    # here (note_event notes a call once). Its build time is unknown.
    case "${status}" in
        error) outcome=failed ;;
        cancelled) outcome=cancelled ;;
        interrupted) outcome=interrupted code="" ;;
    esac
    note_event "${outcome}" "${RUN_STARTED}" $(( SECONDS - RUN_STARTED )) "" 0 "${code}" || true
    finish_hold_wait
    [[ -z "${MEM_KILL_FILE:-}" ]] || rm -f "$MEM_KILL_FILE"
    [[ -z "${MEM_KILL_FILE:-}" ]] || rm -f "${MEM_KILL_FILE}.stopped"
    if [[ "${ACTIVITY_ACTIVE}" -eq 1 ]]; then
        finish_activity "${status}"
    fi
    if ! release_token && (( EXIT_STATUS == 0 )); then
        EXIT_STATUS=1
    fi
    [[ -z "${TOKEN_WAIT_FILE:-}" ]] || rm -f "$TOKEN_WAIT_FILE" "${TOKEN_WAIT_FILE}.given_back"
    release_wait_claim
    remove_step_record
}
# EXIT alone would report success for a failed cargo run, so branch on the
# status the trap receives; a lint failure record keeps it.
trap 'EXIT_STATUS=$?; [[ ${EXIT_STATUS} -eq 0 ]] && verify_cleanup completed || verify_cleanup error; final_pass_finish "$EXIT_STATUS"; exit "${EXIT_STATUS}"' EXIT
verify_interrupted() {
    trap - EXIT
    # The supervisor can forward a signal that this group already received.
    # Ignore later signals until cleanup removes this call's records.
    trap '' INT TERM HUP
    EXIT_STATUS=$((128 + $1))
    interrupt_hold_wait
    if [[ -n "$RUN_STEP_PID" ]]; then
        "${STEP_RECORD_SCRIPT%/*}/../lib/py" "${STEP_RECORD_SCRIPT%/*}/../lib/process_group.py" "$RUN_STEP_PID" "$1" 2>/dev/null || { [[ $? == 3 ]] || printf 'verify.sh: process group helper failed for pid %s\n' "$RUN_STEP_PID" >&2; }
        release_token || true
        wait "$RUN_STEP_PID" 2>/dev/null || true
        RUN_STEP_PID=""
    fi
    verify_cleanup interrupted
    final_pass_finish "$EXIT_STATUS"
    stop_interrupted_run_log
    exit "$EXIT_STATUS"
}
trap 'verify_interrupted 2' INT
trap 'verify_interrupted 15' TERM
trap 'verify_interrupted 1' HUP

run_lint_judgment() {
    if lint_config_enabled clippy; then
        [[ -n "$PKG" ]] || echo 'style-lint: clippy start'
        start_tree_watch
        # Store diagnostics in the watch directory for cancellation cleanup.
        # Keep admission and the step record in run().
        CLIPPY_LOG="$(mktemp "${TREE_WATCH_DIR:-${TMPDIR:-/tmp}}/clippy.XXXXXX")"
        clippy_status=0
        # Use the existing tee to record diagnostics.
        # Keep wait and cancellation messages visible.
        if invoke_clippy --workspace --all-targets --all-features --keep-going; then
            :
        else
            clippy_status=$?
        fi
        clippy_repair=0
        if [[ -f "${HOLD_WAIT_DIR:-/nonexistent}/lint-held" ]]; then HOLD_LINT=1; fi
        if first_judgment_permits_repair "$clippy_status" "$CLIPPY_LOG"; then
            clippy_repair=1
        fi
        rm -f "$CLIPPY_LOG"
        unset CLIPPY_LOG
        finish_tree_watch
        if (( clippy_repair )); then
            # Read at use so a changed policy applies to this repair round.
            clippy_fix_limit="$(_lint_config_raw clippy_fix_pass_limit)"
            if [[ ! "$clippy_fix_limit" =~ ^[1-9][0-9]{0,3}$ ]]; then
                [[ -z "$clippy_fix_limit" ]] || echo "verify.sh: invalid clippy_fix_pass_limit=$clippy_fix_limit; using 3." >&2
                clippy_fix_limit=3
            fi
            clippy_tree_before="$(tree_key --tree-content)"
            for ((clippy_fix_pass=1; HOLD_LINT == 0 && clippy_fix_pass<=clippy_fix_limit; clippy_fix_pass++)); do
                invoke_clippy_fix --workspace --all-targets --all-features --keep-going || true
                fmt_cargo "${FMT_SCOPE[@]}"
                clippy_tree_after="$(tree_key --tree-content)"
                if [[ -n "$clippy_tree_before" && "$clippy_tree_before" == "$clippy_tree_after" ]]; then
                    break
                fi
                # An empty key cannot prove that the tree stopped changing.
                if (( clippy_fix_pass == clippy_fix_limit )); then
                    CLIPPY_FIX_LIMIT_REACHED=1
                fi
                clippy_tree_before="$clippy_tree_after"
            done
            start_tree_watch
            clippy_status=0
            invoke_clippy --workspace --all-targets --all-features --keep-going || clippy_status=$?
            finish_tree_watch
        fi
        (( clippy_status == 0 )) || exit "$clippy_status"
        [[ -n "$PKG" ]] || echo 'style-lint: clippy passed'
    else
        invoke_clippy --workspace --all-targets --all-features
    fi
}

run_lint_held_sequence() {
    # Format locally; the Mac judges the resulting tree without fixes.
    fmt_cargo --all
    load_metadata
    workspace_taplo
    lint_status=0
    if lint_config_enabled mend; then
        STEP_FORCE_MAC=${1:-0} invoke_mend --workspace --fail-on-warn "${FEATURE_FLAGS[@]}" || lint_status=1
    fi
    if lint_config_enabled clippy; then
        start_tree_watch
        STEP_FORCE_MAC=${1:-0} invoke_clippy --workspace --all-targets --all-features || lint_status=1
        finish_tree_watch
    fi
    return 0
}

run_lint_sequence() {
    if [[ "$HOLD_LINT" == 1 ]]; then
        run_lint_held_sequence
        exit "$lint_status"
    fi
    if (( CHECK_LINT )); then
        # Judge all three tools even when an earlier one reports findings.
        # Report mode never enters the rewrite paths of the package gate.
        lint_status=0
        if lint_config_enabled mend; then
            echo 'style-lint: mend start'
            if invoke_mend --workspace --fail-on-warn "${FEATURE_FLAGS[@]}"; then
                echo 'style-lint: mend passed'
            else
                lint_status=1
            fi
        else
            lint_config_skip_notice mend 'cargo mend --all-targets --workspace --fail-on-warn'
        fi
        if lint_config_enabled fmt; then
            echo 'style-lint: rustfmt start'
            if fmt_cargo --all --check; then
                echo 'style-lint: rustfmt passed'
            else
                lint_status=1
            fi
        else
            lint_config_skip_notice fmt 'cargo +nightly fmt --all --check'
        fi
        load_metadata
        if ! workspace_taplo --check; then
            lint_status=1
        fi
        if lint_config_enabled clippy; then
            echo 'style-lint: clippy start'
            start_tree_watch
            if invoke_clippy --workspace --all-targets --all-features; then
                echo 'style-lint: clippy passed'
            else
                lint_status=1
            fi
            finish_tree_watch
        else
            lint_config_skip_notice clippy 'cargo clippy --workspace --all-targets --all-features'
        fi
        exit "$lint_status"
    fi
    # Cargo stages are gated by lint.conf; the Taplo check always runs.
    # mend passes --all-targets because it needs the test and example
    # targets to see every import and visibility site. It covers the
    # workspace like the other stages, so
    # --fix can also rewrite a member the phase did not edit, when this
    # phase's change left an item there unused or over-visible.
    if lint_config_enabled mend; then
        [[ -n "$PKG" ]] || echo 'style-lint: mend start'
        MEND_LOG="$(mktemp)"
        # run_step() records diagnostics through its tee.
        # Keep this call in the parent so cleanup can stop the step.
        local LINT_MEND_WAIT=1
        while true; do
            if invoke_mend --workspace --fix "${FEATURE_FLAGS[@]}"; then
                break
            else
                mend_status=$?
            fi
            LINT_MEND_WAIT=0
            if (( mend_status == LINT_LEFT_QUEUE_STATUS )); then
                run_lint_held_sequence 1
                if (( lint_status == 0 )); then
                    rm -f "$MEND_LOG"
                    exit 0
                fi
                HOLD_LINT=0
                rm -f "$HOLD_WAIT_DIR/lint-held"
                continue
            fi
            if (( mend_status == 137 || mend_status == 125 )); then
                rm -f "$MEND_LOG"
                exit "$mend_status"
            fi
            if grep -qiE 'rolled back|revert' "$MEND_LOG"; then
                echo "verify.sh: cargo mend --fix rolled its rewrites back; stop and keep the tree for inspection" >&2
            else
                echo "verify.sh: cargo mend --fix failed; see the output above" >&2
            fi
            rm -f "$MEND_LOG"
            exit 1
        done
        LINT_MEND_WAIT=0
        rm -f "$MEND_LOG"
        MEND_LOG=""
        [[ -n "$PKG" ]] || echo 'style-lint: mend passed'
    else
        lint_config_skip_notice mend "cargo mend --all-targets --workspace --fix"
    fi
    # fmt compiles nothing, so it stays on the phase's own work: this
    # package and every member that differs from HEAD once mend has run.
    # Covering them all is what lets one lint record answer for each
    # package (tree_key).
    if [[ -z "$PKG" ]]; then
        FMT_SCOPE=(--all)
    else
        FMT_SCOPE=(-p "$PKG")
        FMT_MEMBERS="$(read_metadata \
        | VERIFY_PY_HELPER=CHANGED_MEMBERS_PY "$PY" -c "$CHANGED_MEMBERS_PY")"
        while IFS= read -r member; do
            if [[ -n "$member" && "$member" != "$PKG" ]]; then
                FMT_SCOPE+=(-p "$member")
            fi
        done <<< "$FMT_MEMBERS"
    fi
    if [[ -z "$PKG" ]] && lint_config_enabled fmt; then
        echo 'style-lint: rustfmt start'
    fi
    fmt_cargo "${FMT_SCOPE[@]}"
    if [[ -z "$PKG" ]] && lint_config_enabled fmt; then
        echo 'style-lint: rustfmt passed'
    fi
    # Record the tree Taplo reads; clippy fix and fmt update the key below.
    if [[ -n "${RUN_LOG}" ]]; then
        set_verdict_keys
    fi
    # Cargo's workspace root can differ from the caller's directory.
    # Run there so Taplo uses the same config and scope as the push gate.
    load_metadata
    TAPLO_ARGS=(--check)
    if [[ "$HOLD_LINT" == 1 ]]; then TAPLO_ARGS=(); fi
    if workspace_taplo ${TAPLO_ARGS[@]+"${TAPLO_ARGS[@]}"}; then
        :
    else
        taplo_status=$?
        if (( taplo_status == 101 )); then
            if [[ "$(uname)" == Darwin ]]; then
                echo "verify.sh: taplo panicked; the macOS sandbox does this. Re-run the same command with the sandbox disabled." >&2
                exit 3
            fi
            echo "verify.sh: taplo panicked; this is a tooling failure." >&2
            exit 2
        fi
        exit "$taplo_status"
    fi
    run_lint_judgment
}

case "$CMD" in
    check)
        PKG="$VERIFY_PACKAGE"
        KEEP_GOING=()
        CHECK_ARGS=()
        for word in "$@"; do
            if [[ "$word" == --keep-going ]]; then
                KEEP_GOING=(--keep-going)
            else
                CHECK_ARGS+=("$word")
            fi
        done
        take_features "${CHECK_ARGS[@]}"
        require_member "$PKG"
        take_check_targets
        run cargo check --workspace "${CHECK_SELECTION[@]}" "${KEEP_GOING[@]}" "${FEATURE_FLAGS[@]}"
        ;;
    build)
        PKG="$VERIFY_PACKAGE"
        require_member "$PKG"
        take_build_args "$@"
        run cargo build --workspace "${BUILD_ARGS[@]}"
        print_built_binaries
        ;;
    test)
        start_tree_watch
        test_status=0
        run_local_test || test_status=$?
        finish_tree_watch
        exit "$test_status"
        ;;
    lint)
        PKG="$VERIFY_PACKAGE"
        CHECK_LINT=0
        if [[ "${1:-}" == --check ]]; then CHECK_LINT=1; shift; fi
        [[ $# -eq 0 ]] || { usage; exit 2; }
        if [[ -n "$PKG" ]]; then require_member "$PKG"; fi
        require_taplo
        run_lint_sequence
        ;;
    doc)
        DOC_FEATURE_FLAGS=()
        DOC_FEATURES=""
        if [[ $# -eq 2 && "$1" == --features && -n "$2" ]]; then
            DOC_FEATURES="$(qualify_features "$VERIFY_PACKAGE" "$2")"
        elif [[ $# -ne 0 ]]; then
            usage
            exit 2
        fi
        DOC_FEATURES="$(qualify_features "$VERIFY_PACKAGE" "$(merge_dev_features "$DOC_FEATURES")")"
        if [[ -n "$VERIFY_PACKAGE" ]]; then require_member "$VERIFY_PACKAGE"; fi
        if [[ -n "$DOC_FEATURES" ]]; then
            DOC_FEATURE_FLAGS=(--features "$DOC_FEATURES")
        fi
        DOC_REPO_TOP="$(git rev-parse --show-toplevel)"
        # Cargo resolves its configured target at the original repo, never at
        # the isolated source checkout. Relative env overrides use that top.
        if [[ -z "${CARGO_TARGET_DIR:-}" ]]; then
            load_metadata
            CARGO_TARGET_DIR="$TARGET_DIRECTORY"
        fi
        CARGO_TARGET_DIR="$(VERIFY_PY_HELPER=DOC_TARGET_PATH_PY "$PY" -c 'from pathlib import Path; import sys; print((Path(sys.argv[1]) / sys.argv[2]).resolve())' "$DOC_REPO_TOP" "$CARGO_TARGET_DIR")"
        export CARGO_TARGET_DIR
        doc_checkout
        if ! lint_config_enabled doc; then
            echo 'verify.sh: the phase rustdoc check cannot be switched off; ignoring doc=off.'
        fi
        DOC_CALLER_DIR=$PWD
        cd "$DOC_CHECKOUT"
        # Keep the signal handlers and the active step PID in the caller shell.
        MAC_SOURCE_ROOT="$DOC_CHECKOUT"
        LINT_CONFIG_FORCE=1 invoke_doc --workspace "${DOC_FEATURE_FLAGS[@]}"
        cd "$DOC_CALLER_DIR"
        doc_record
        ;;
    example)
        PKG="$VERIFY_PACKAGE"
        NAME="$EXAMPLE_NAME"
        require_member "$PKG"
        EXAMPLE_SELECTION=(--examples)
        if [[ -n "$PKG" ]]; then EXAMPLE_SELECTION+=(-p "$PKG"); else EXAMPLE_SELECTION+=(--workspace); fi
        FEATURES=""
        if [[ -n "$NAME" ]]; then
            EXAMPLE_SELECTION=(--workspace --example "$NAME")
            FEATURES="$(example_features "$PKG" "$NAME")"
        fi
        if [[ -n "$FEATURES" ]]; then
            invoke_clippy "${EXAMPLE_SELECTION[@]}" --features "$(qualify_features "$PKG" "$FEATURES")"
        else
            invoke_clippy "${EXAMPLE_SELECTION[@]}"
        fi
        ;;
    final)
        if [[ -n "$PINNED_TARGET" || "$MAC_ONLY" == 1 || "$LOCAL_ONLY" == 1 ]]; then
            VERIFY_PY_HELPER=FINAL_CLEAN_TREE_PY "$PY" -c '
from pathlib import Path
import sys
with open(Path(sys.argv[1]).resolve().parent / "lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.direct.final_pass import clean_tree
clean_tree()
' "${FINAL_PASS_SCRIPT%/*}" || exit 2
        fi
        [[ $# -eq 0 ]] || { command_help final >&2; exit 2; }
        FINAL_FEATURES="$(merge_dev_features "")"
        if [[ -n "$VERIFY_PACKAGE" ]]; then require_member "$VERIFY_PACKAGE"; fi
        if [[ -n "$FINAL_FEATURES" ]]; then
            FEATURE_FLAGS=(--features "$FINAL_FEATURES")
        fi
        run cargo check --workspace --all-targets "${FEATURE_FLAGS[@]}"
        FINAL_TEST_SELECTION=(--workspace --tests)
        if [[ -n "$VERIFY_PACKAGE" ]]; then FINAL_TEST_SELECTION+=(-E "package($VERIFY_PACKAGE)"); fi
        VERIFY_TEST_ENV_NAMES=""
        for VERIFY_ENV_ENTRY in ${VERIFY_TEST_ENVIRONMENT[@]+"${VERIFY_TEST_ENVIRONMENT[@]}"}; do
            export "$VERIFY_ENV_ENTRY"
            VERIFY_TEST_ENV_NAMES+="${VERIFY_TEST_ENV_NAMES:+ }${VERIFY_ENV_ENTRY%%=*}"
        done
        export VERIFY_TEST_ENV_NAMES
        run_nextest "${FINAL_TEST_SELECTION[@]}" "${FEATURE_FLAGS[@]}"
        ;;
    *)
        usage
        exit 2
        ;;
esac
