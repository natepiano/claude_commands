#!/usr/bin/env bash
# verify.sh — The only build/test/lint commands a worker may run.
#
# Work Orders list exact invocations of this script; the worker composes no
# cargo flags and makes no scope choices. `check` pins lib and bin targets;
# `test` selects the package's test targets; examples are never test targets.
# `build` builds one package's binaries, or one bin or example, so a smoke run
# or live shot can launch it, and prints each binary's path.
# `lint` runs clippy with --all-targets --all-features to match CI, and mend
# also uses --all-targets. It checks TOML formatting with taplo at the workspace
# root. `example` runs clippy on one named example while
# iterating. `final` is the plan-final full gate, run by the unit director,
# never by a phase worker. It checks all targets, including examples, then
# runs nextest with --tests so no example is linked. No tests are lost:
# the scan first refuses examples that hold a test or set test = true.
#
# Package selection is always --workspace. Non-release check, test, final,
# doc and build add workspace.metadata.dev.features to Work Order features.
# `lint` is unchanged; its clippy always uses all features.
# Cargo resolves features per invocation from the selected packages, so `-p <pkg>`
# gives every member its own feature set for bevy and the rest of the
# dependency tree, and each set is a separate compile of that tree in the
# shared target directory: nine to eleven copies of bevy_render across the
# members, which evict one another under the sweep budget and rebuild in
# minutes each time a phase moves to a new package. --workspace resolves one
# set for the whole tree, so commands with the same feature set reuse one
# compiled copy. `lint`'s all-features clippy shares its set with the push gate.
# The package argument still decides what runs and what is reported: nextest
# filters to it with -E 'package(<pkg>)', and --features qualifies its list
# with it. `test` builds the named package's test targets, plus workspace libs
# when the package has a lib.
#
# `doc` documents the whole workspace with warnings denied, as the unit
# director's last command before a Rust checkpoint. Workers may run it too.
# Unlike lint stages, this phase check cannot be switched off in lint.conf.
# A pass is stored in ~/.local/state/verify/doc_pass/<tree>.json, keyed by a
# staged index in a stable, locked checkout under the repo's target directory.
# Unstaged and untracked files are excluded. The real index is unchanged. A staged tree
# changed during the run records nothing.
# Records outlive worker sessions and are pruned after 14 days on each write.
#
# The lint stages — mend, fmt, clippy fix, fmt, clippy judgment — use
# config/lint.conf (edit it with /lint_config). Taplo fmt --check always runs
# at the workspace root. A gated-off check prints a SKIPPED line and exits 0.
# Scope is never configurable. cargo check, cargo nextest and the checkpoint
# rustdoc check are correctness gates and cannot be switched off.
#
# A worker session remembers passes. With UNIT_RUN_DIR or
# UNIT_SESSION_DIR set, a `test` or `lint` that exits 0 is recorded in
# <session>/verify_cache/, keyed by the working tree its verdict was judged on,
# the arguments, rustc -vV, lint.conf, verify.sh, invoke.sh and the
# RUST*/CARGO_*/NEXTEST_* environment. A plain `lint <package>` leaves the package out of the key:
# every lint stage but fmt covers the workspace, and fmt covers every member
# that differs from HEAD, so one lint record answers for each package. A
# `test` key holds only the part of the tree its package is built from
# (TREE_KEY_PY), so a second phase editing another member in the same worktree
# neither drops the pass nor misses it. No key holds the files under the
# session folder (--session-dir, UNIT_RUN_DIR) when it is inside the worktree:
# the call writes its own log and board there while it runs. A `test --filter` key holds its names
# sorted, each once, so one set of names finds one record whatever order a call
# gives them in. The same call on the
# same tree then prints that record and its log and exits 0 without the cargo
# token: workers re-ran unchanged trees 416 times in the week to 2026-10-01. A
# `test --filter` reuse prints one line instead, since iteration feedback needs
# only the verdict. A failed `lint` is recorded the same way
# and replayed with its full output and exit status, since lint on an
# unchanged tree fails the same way (one worker ran 12 in a row on 2026-10-01).
# A failed `test` is never recorded (tests flake), nor a run cut short by a
# usage error, the sandbox or a kill. A lint failure is not recorded when the
# third clippy fix and format pass still changed the tree. The judged tree is
# the tree when the run starts, or for `lint` the tree invoke_clippy_fix and formatting leave
# for clippy judgment. A run that ends on any other tree (for `test`,
# any other part of it the package is built from) records nothing: an edit
# landed while it ran, and its verdict belongs to neither tree.
# A test or lint run outside a worker session is never cached.
# end_session.sh deletes the records when the run ends.
#
# Each record carries what a repeat would cost: the run's wall time minus
# cargo's own "Finished … in" build times, since a repeat on the same tree
# builds nothing. Every call, of every verb, in a worker session or not,
# leaves one call record in the build log (~/.local/state/buildlog; `buildlog
# schema` lists its fields): ran, failed, interrupted, cancelled (exit 77: the
# input tree changed during test or lint judgment), reused (a pass),
# replayed (a lint failure) or superseded (exit 76: a newer call of the same
# verb from the same worker replaced it in the token queue), with the seconds saved and the repo, worktree,
# branch and commit. The steps it ran carry the same call id. The build log
# outlives the records; /verify_saved reports the time saved.
#
# Usage:
#   verify.sh check <package> [--keep-going]
#                                          fast compile feedback (workspace
#                                          lib + bins); --keep-going reads every
#                                          crate's result past the first that fails
#   verify.sh test <package>               the package's unit + integration tests
#                                          (package targets; workspace libs if any);
#                                          refuse examples that hold a test
#                                          or set test = true
#   verify.sh test <package> <int_test>    one named integration test target,
#                                          for re-running it alone
#   verify.sh test <package> --filter <name> [--filter <name> …]
#                                          only the package's tests whose name
#                                          contains any <name>, while iterating;
#                                          never a gate: its pass is recorded
#                                          under its own words, so it never
#                                          answers `test <package>`. Repeat
#                                          --filter to run a change's tests in
#                                          one call; in a worker session the
#                                          same names, in any order, on an
#                                          unchanged tree print one line and
#                                          build nothing
#   verify.sh lint <package>               mend --fix, nightly fmt of the package
#                                          and the members that differ from HEAD,
#                                          workspace taplo fmt --check,
#                                          workspace clippy fix, fmt, then clippy
#                                          judgment on all targets with all features
#                                          (warnings denied)
#                                          — cargo stages gated by config/lint.conf;
#                                          taplo always checks TOML formatting
#   verify.sh lint --workspace [--check]   the same gate with fmt over all members;
#                                          emits style-lint tool boundaries for records
#                                          --check reports mend findings and checks
#                                          formatting without applying fixes
#   verify.sh final-pass [<tree>]          read a matching machine-wide final pass
#   verify.sh doc [--features <list>]      workspace rustdoc, warnings denied;
#                                          mandatory before a Rust checkpoint,
#                                          regardless of lint.conf; workspace
#                                          feature names pass through unchanged
#   … [--features <list>]                  check and test accept one trailing
#                                          `--features a,b` when the Work Order names
#                                          it. Names without a `/` are qualified as
#                                          <package>/<name>. check, test, final,
#                                          doc and non-release build also add
#                                          [workspace.metadata.dev] features,
#                                          without duplicates; lint is unchanged
#   … --no-cache                           test and lint: run even when this
#                                          exact call already passed (or lint
#                                          failed) on this tree, a flake hunt;
#                                          the result replaces the record
#   verify.sh test <package> --local "<why>"   run on natedev; only for a test that cannot pass on macOS.
#                                          Before CI either machine answers a test,
#                                          merge gates included; CI is the protection
#                                          (user, 2026-10-16). A pass the Mac ran
#                                          does not answer --local
#   verify.sh example <package> <name>     clippy one example (only when the
#                                          phase changed that example)
#   verify.sh build <package> [--bin <name> | --example <name>] [--release]
#                                          build the package's binaries, or the
#                                          one named bin or example, for a smoke
#                                          run or a live shot, and print each
#                                          binary's path (`binary: <path>`);
#                                          an example's required features are
#                                          turned on. Accepts --features <list>
#   verify.sh final                        full workspace gate (unit director only);
#                                          check all targets; nextest uses --tests
#                                          so no example is linked; refuse examples
#                                          that hold a test or set test = true
#                                          so no tests are lost
#   verify.sh --session-dir <dir> …        any of the above inside a worker
#                                          session; same as setting
#                                          UNIT_SESSION_DIR=<dir>
#
# `final` always runs the full gate. A clean, unchanged tree that passes gets
# ~/.local/state/verify/final_pass/<tree>.json (VERIFY_STATE_DIR sets its parent).
# `final-pass [<tree>]` reads that proof without Cargo or build admission. It
# requires the current rustc -vV and verify.sh hash. No tree argument means
# the current clean HEAD tree. Writes prune records older than 14 days.
#
# Invocation policy — canonical flags, lint.conf gating, sandbox-failure
# detection — lives in scripts/lint/invoke.sh, the
# single bottom layer, sourced below. This file adds only worker scope rules
# on top. Shell aliases, validate_ci and release checks use the lint CLI in that
# directory instead of this script.
#
# Exit codes: 2 = usage error or missing tooling; 3 = sandbox failure, re-run
# the same command unsandboxed; anything else is the underlying tool status.

set -euo pipefail
# Read-only queries run before environment setup and build admission.
if [[ "${1:-}" == final-pass || ( "${1:-}" == --session-dir && "${3:-}" == final-pass ) ]]; then
    if [[ "$1" == --session-dir ]]; then shift 2; fi
    shift
    VERIFY_QUERY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    export CLAUDE_PYTHON="${CLAUDE_PYTHON:-python3}"
    exec "$VERIFY_QUERY_DIR/../lib/py" "$VERIFY_QUERY_DIR/final_pass.py" query "$VERIFY_QUERY_DIR/verify.sh" "$@"
fi
if [[ -n "${TMPDIR:-}" && ! -d "$TMPDIR" ]]; then mkdir -p -- "$TMPDIR"; fi
# Ask the shared helper which envrc applies, before tool setup or record access.
# With no envrc, ensure returns the unchanged env without invoking direnv.
if [[ -z "${IN_NIX_SHELL:-}" ]]; then
    if [[ -n "${VERIFY_NIX_ENV_REEXEC:-}" ]]; then
        printf '%s\n' 'verify.sh: nix_env.py re-exec still has no nix shell (IN_NIX_SHELL unset)' >&2
        exit 1
    fi
    VERIFY_NIX_SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
    [[ "$VERIFY_NIX_SCRIPT_DIR" != "${BASH_SOURCE[0]}" ]] || VERIFY_NIX_SCRIPT_DIR=.
    VERIFY_NIX_SCRIPT_DIR="$(cd -- "$VERIFY_NIX_SCRIPT_DIR" && pwd -P)"
    VERIFY_NIX_ENV_JSON="$("$VERIFY_NIX_SCRIPT_DIR/../lib/py" \
        "$VERIFY_NIX_SCRIPT_DIR/../lib/nix_env.py" ensure "$PWD")"
    if "$VERIFY_NIX_SCRIPT_DIR/../lib/py" -c \
        'import json, sys; sys.exit(0 if json.load(sys.stdin).get("IN_NIX_SHELL") else 1)' \
        <<< "$VERIFY_NIX_ENV_JSON"; then
        printf '%s\n' 'verify.sh: not in the nix shell; re-running under nix_env.py exec' >&2
        export VERIFY_NIX_ENV_REEXEC=1
        exec "$VERIFY_NIX_SCRIPT_DIR/../lib/py" "$VERIFY_NIX_SCRIPT_DIR/../lib/nix_env.py" \
            exec "$VERIFY_NIX_SCRIPT_DIR/${BASH_SOURCE[0]##*/}" "$@"
    fi
    unset VERIFY_NIX_ENV_JSON VERIFY_NIX_SCRIPT_DIR
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
# repo needs >= 3.11.
PY="${HOME}/.claude/scripts/lib/py"
PROCESS_REPOSITORY="$(cd "${BASH_SOURCE[0]%/*}/../.." && pwd -P)"

# The single bottom layer: run(), lint.conf gating, fmt_cargo, run_nextest,
# invoke_clippy_fix, invoke_clippy, and sandbox-failure detection come from here.
# shellcheck source=/dev/null
source "$HOME/.claude/scripts/lint/invoke.sh" || {
    printf 'verify.sh: cannot load admission and hold checks; step refused\n' >&2
    exit 2
}
BUILD_TARGETS_SCRIPT="${BUILD_TARGETS_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../build_targets" && pwd)/build_targets.py}"
LOCAL_RELEASE_WAIT_SCRIPT="${VERIFY_LOCAL_RELEASE_WAIT_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/local_release_wait.py}"
ASSERTION_GUARD_SCRIPT="${VERIFY_ASSERTION_GUARD_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/assertion_guard.py}"
STEP_RECORD_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/step_record.py"
FINAL_PASS_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/final_pass.py"
FINAL_PASS_START=""
final_pass_finish() {
    [[ -n "$FINAL_PASS_START" ]] || return 0
    env -u BUILDLOG_CALL_ID "$PY" "$FINAL_PASS_SCRIPT" finish "${BASH_SOURCE[0]}" "$FINAL_PASS_START" "$1" "${FINAL_FEATURES:-}" || true
    FINAL_PASS_START=""
}
STEP_RECORD_ACTIVE=0
STEP_RUNS_ON=natedev_only
STEP_TARGET_TABLE=""
HOLD_LINT=0
STEP_DEFER_LOCAL=0
STEP_FORCE_MAC=0

step_state() {
    [[ "$STEP_RECORD_ACTIVE" == 1 ]] || return 0
    env -u BUILDLOG_CALL_ID "$PY" "$STEP_RECORD_SCRIPT" state --call-id "$BUILDLOG_CALL_ID" "$1" || exit 2
}

remove_step_record() {
    if [[ "$STEP_RECORD_ACTIVE" == 1 ]]; then
        env -u BUILDLOG_CALL_ID "$PY" "$STEP_RECORD_SCRIPT" remove --call-id "$BUILDLOG_CALL_ID" || true
        STEP_RECORD_ACTIVE=0
    fi
}

# A failed step may be a process killed by earlyoom or the kernel. Keep the
# journal query here so tests can put a journalctl stub first on PATH.
memory_kill_in_journal() {
    local started=$1 ended=$2
    journalctl -u earlyoom --since "$started" --until "$ended" --no-pager 2>/dev/null \
        | grep -E 'sending SIG(TERM|KILL) to process' >/dev/null && return 0
    journalctl -k --since "$started" --until "$ended" --no-pager 2>/dev/null \
        | grep -E 'Memory cgroup out of memory|Out of memory: Killed' >/dev/null
}

step_was_killed_for_memory() {
    local log=$1 started=$2 ended=$3
    grep -qaE 'SIGKILL|SIGTERM|signal: (9|15)' "$log" || return 1
    memory_kill_in_journal "$started" "$ended"
}

run() {
    local attempt status started ended step_log monitor=0
    RUN_LAST_ATTEMPT_KILLED=0
    step_log="$(mktemp)"
    for attempt in 1 2; do
        if [[ -f "${HOLD_WAIT_DIR:-/nonexistent}/lint-held" ]]; then HOLD_LINT=1; fi
        if buildlog_step_compiles "$@"; then
            route_compiling_step "$@" || { status=$?; rm -f "$step_log"; return "$status"; }
            if [[ "$STEP_RAN_ON_MAC" == 1 ]]; then rm -f "$step_log"; return 0; fi
        fi
        if buildlog_step_compiles "$@"; then
            step_state on_natedev
        else
            step_state local_noncompiling
        fi
        export BUILDLOG_TARGET=natedev
        started="$(date '+%Y-%m-%d %H:%M:%S %z')"
        monitor=0
        [[ $- != *m* ]] || monitor=1
        set -m
        (
            set +m
            if run_once "$@" 2>&1 | tee "$step_log"; then
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
            exit "$status"
        fi
        # A Mac copy that finished first stopped this step: no memory retry.
        if [[ -n "${VERIFY_STEP_INFO_FILE:-}" && -e "$VERIFY_STEP_INFO_FILE.stopped" ]]; then
            rm -f "$step_log"
            return "$status"
        fi
        if buildlog_step_compiles "$@" && { [[ "$status" == "$LINT_LEFT_QUEUE_STATUS" && "$CMD" != test && "$STEP_RUNS_ON" == mac_allowed ]] || step_stopped_by_hold; }; then
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

usage() {
    sed -n '/^# Usage:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
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
    sys.exit(0)
except (OSError, ValueError):
    print("verify.sh: workspace.metadata.dev.features must be a list of strings", file=sys.stderr)
    sys.exit(2)
print(",".join(features))
'

MEMBER_PY='
import json
import sys

package_name = sys.argv[1]
meta = json.load(sys.stdin)
if any(package["name"] == package_name for package in meta["packages"]):
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

# The cargo words that select what `build` builds, then the binaries it makes:
# `select <word>` lines, `features <list>` for an example's required features,
# and `binary <path under the profile folder>` lines. Bin and example names
# are unique across the workspace, so --workspace --bin builds the one target
# under the workspace's feature set, the set `test` already compiled.
BUILD_TARGETS_PY='
import json
import sys

package_name, kind, name = sys.argv[1], sys.argv[2], sys.argv[3]
meta = json.load(sys.stdin)
package = next(package for package in meta["packages"] if package["name"] == package_name)
wanted = [target for target in package["targets"] if (kind or "bin") in target["kind"] and name in ("", target["name"])]
if not wanted:
    what = f"{kind} {name}" if name else "binary"
    advice = "" if name else "; name an example with --example <name>"
    print(f"verify.sh: package {package_name} has no {what}{advice}", file=sys.stderr)
    sys.exit(2)
for target in wanted:
    print("select --" + (kind or "bin"))
    print("select " + target["name"])
    if kind == "example" and target.get("required-features"):
        print("features " + ",".join(target["required-features"]))
    print(("binary examples/" if kind == "example" else "binary ") + target["name"])
print("target " + meta["target_directory"])
'

# Select the named package's test targets. Named targets with unavailable
# required features would error instead of being skipped by --tests, and names
# shared across members would select both; either case uses the broad flags.
TEST_TARGETS_PY='
import json
import sys

package_name = sys.argv[1]
meta = json.load(sys.stdin)
package = next(package for package in meta["packages"] if package["name"] == package_name)
features = package["features"]
enabled = set()
pending = features.get("default", []) + [
    name[len(package_name) + 1 :] for name in sys.argv[2].split(",") if name.startswith(package_name + "/")
]
while pending:
    name = pending.pop()
    if name in features and name not in enabled:
        enabled.add(name)
        pending += features[name]
flags = {"bin": "--bin", "test": "--test", "bench": "--bench"}
shared = {
    (target["kind"][0], target["name"])
    for other in meta["packages"]
    if other["name"] != package_name
    for target in other["targets"]
}
has_lib = any(target["kind"][0] in {"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"} for target in package["targets"])
selection = ["--lib"] if has_lib else []
for target in package["targets"]:
    kind = target["kind"][0]
    if kind not in flags or (kind != "bin" and not target["test"]):
        continue
    if not set(target.get("required-features", [])) <= enabled or (kind, target["name"]) in shared:
        selection = (["--lib"] if has_lib else []) + ["--bins", "--tests"]
        break
    selection += [flags[kind], target["name"]]
print("\n".join(selection or ["--bins", "--tests"]))
'

UNTESTED_EXAMPLES_PY='
import json
from pathlib import Path
import re
import sys

meta = json.load(sys.stdin)
members = set(meta["workspace_members"])
packages = [package for package in meta["packages"] if package["id"] in members]
package_name = sys.argv[1]
if package_name != "--workspace":
    packages = [package for package in packages if package["name"] == package_name]
    if not packages:
        print("verify.sh: package " + package_name + " not found in workspace", file=sys.stderr)
        sys.exit(2)

test_marker = re.compile(r"^\s*(#\[(?:cfg\(test\)|(?:\w+::)*test)\])")
offenders = []
for package in packages:
    for target in package["targets"]:
        if "example" not in target["kind"]:
            continue
        example_name = target["name"]
        owner_package = package["name"]
        manifest_path = package["manifest_path"]
        source = Path(target["src_path"])
        if source.name == "main.rs" and source.parent.parent.name == "examples":
            sources = sorted(source.parent.rglob("*.rs"))
        else:
            sources = [source]
        for path in sources:
            for line_number, line in enumerate(path.read_text().splitlines(), 1):
                if match := test_marker.match(line):
                    offenders.append((
                        f"example {example_name} holds a test: {path}:{line_number} has {match.group(1)}, and examples carry no tests.",
                        f"move it into {owner_package}\x27s src/ or tests/, or delete it.",
                    ))
                    break
            else:
                continue
            break
        if target.get("test", False):
            offenders.append((
                f"example {example_name} sets test = true in {manifest_path}, and examples carry no tests.",
                "remove test = true from its [[example]] entry.",
            ))

for index, (reason, fix) in enumerate(offenders):
    if index:
        print(file=sys.stderr)
    print(reason, file=sys.stderr)
    print(fix, file=sys.stderr)
if offenders:
    sys.exit(2)
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

require_member() {
    if ! read_metadata | "$PY" -c "$MEMBER_PY" "$1"; then
        exit 2
    fi
}

require_taplo() {
    if ! command -v taplo >/dev/null 2>&1; then
        echo "taplo is required" >&2
        exit 2
    fi
}

example_features() {
    cargo metadata --no-deps --format-version 1 \
        | "$PY" -c "$EXAMPLE_FEATURES_PY" "$1" "$2"
}

TEST_SELECTION=()
take_test_targets() {
    local words word
    words="$(read_metadata \
        | "$PY" -c "$TEST_TARGETS_PY" "$PKG" "${FEATURE_FLAGS[1]:-}")"
    while IFS= read -r word; do
        if [[ -n "$word" ]]; then
            TEST_SELECTION+=("$word")
        fi
    done <<< "$words"
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
        [[ "$name" != */* ]] && name="$pkg/$name"
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
    words="$(read_metadata | "$PY" -c "$BUILD_TARGETS_PY" "$PKG" "$kind" "$name")" || exit 2
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
        if [[ -x "$path" ]]; then
            echo "binary: $path"
        else
            echo "verify.sh: cargo build passed but made no ${path}" >&2
            exit 1
        fi
    done
}

NEXTEST_ARGS=()
compose_nextest_args() {
    set -- "${ARGS[@]}"
    PKG="${1:?verify.sh test <package> [integration_test]}"
    shift
    TARGET=""
    FILTERS=()
    if [[ $# -gt 0 && "$1" != --* ]]; then
        TARGET="$1"
        shift
    fi
    # --filter, as often as named, and --features in any order; a target
    # takes no filter.
    REST=()
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "--filter" && -z "$TARGET" \
              && "${2:-}" =~ ^[A-Za-z0-9_:]+$ ]]; then
            FILTERS+=("$2")
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
        TEST_FILTER="package($PKG) & $FILTER"
    elif [[ -n "$TARGET" ]]; then
        # Integration test target names are unique across the workspace,
        # so --test builds just that binary, under the workspace's
        # feature resolution.
        NEXTEST_ARGS=(--no-fail-fast --workspace --test "$TARGET" \
            -E "package($PKG)" "${FEATURE_FLAGS[@]}")
        return
    else
        TEST_FILTER="package($PKG)"
    fi
    # Build only the targets this package's tests live in
    # (TEST_TARGETS_PY): --lib, which cannot be narrowed to one member
    # under --workspace and is left out for a package without a lib,
    # and the package's own bins and test-enabled targets. --bins
    # --tests linked every member's test executables, 78 in hana, to
    # run one package's; this links 4 (2026-10-04). The integration
    # tests stay in so a phase runs the package's integration tests
    # alongside its unit tests. The lint half's clippy checks all
    # workspace targets with all features.
    take_test_targets
    NEXTEST_ARGS=(--no-fail-fast --workspace "${TEST_SELECTION[@]}" \
        -E "$TEST_FILTER" "${FEATURE_FLAGS[@]}")
}

VERIFY_ORIGINAL_ARGS=("$@")
# A flag, not an env prefix, so a call stays one plain command that the
# settings.json allow rule matches.
if [[ "${1:-}" == "--session-dir" ]]; then
    export UNIT_SESSION_DIR="${2:?verify.sh --session-dir <dir> <command> ...}"
    shift 2
    # Test temp folders land in the run folder and end with it (tmp_sweep.py judges each entry).
    if mkdir -p "${UNIT_SESSION_DIR}/tmp" 2>/dev/null; then
        export TMPDIR="${UNIT_SESSION_DIR}/tmp"
    fi
fi

# One list feeds both the guard invocation and its cache inputs. The identity
# check also deduplicates two spellings of the same existing folder.
assertion_rulings_args() {
    ASSERTION_RULINGS_ARGS=()
    local folder previous=""
    for folder in "${UNIT_SESSION_DIR:-}" "${UNIT_RUN_DIR:-}"; do
        [[ -n "$folder" ]] || continue
        if [[ "$folder" != "$previous" && ( -z "$previous" || ! "$folder" -ef "$previous" ) ]]; then
            ASSERTION_RULINGS_ARGS+=(--rulings "$folder")
            previous="$folder"
        fi
    done
}
assertion_rulings_args

# A Codex mesh worker's command dies with its turn, and a worker that waits on
# one spends a model call per wait. So a worker's call detaches: mesh_runs.py
# runs this same call under a supervisor outside the worker's process group,
# returns at once, and sends the result to the worker as its next message.
# Exit 3 means the caller is not a mesh worker; this call then runs attached.
# The detached run sets UNIT_DETACHED_RUN, so it does not detach again.
MESH_RUNS_SCRIPT="${VERIFY_MESH_RUNS_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../agents" && pwd)/mesh_runs.py}"
if [[ -z "${UNIT_DETACHED_RUN:-}" && -n "${CODEX_THREAD_ID:-}" ]]; then
    VERIFY_SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
    DETACH_STATUS=0
    "$PY" "$MESH_RUNS_SCRIPT" start --label "verify.sh ${VERIFY_ORIGINAL_ARGS[*]-}" -- \
        bash "$VERIFY_SELF" ${VERIFY_ORIGINAL_ARGS[@]+"${VERIFY_ORIGINAL_ARGS[@]}"} || DETACH_STATUS=$?
    case "$DETACH_STATUS" in
        0) exit 0 ;;
        3) ;;
        *) echo "verify.sh: mesh_runs.py could not detach this run (exit $DETACH_STATUS); running attached" >&2 ;;
    esac
fi

CMD="${1:-}"
if [[ -z "$CMD" ]]; then
    usage
    exit 2
fi
shift
if [[ "$CMD" == final ]]; then
    FINAL_PASS_START="$("$PY" "$FINAL_PASS_SCRIPT" start "${BASH_SOURCE[0]}")"
    trap 'final_status=$?; final_pass_finish "$final_status"; exit "$final_status"' EXIT
fi

if [[ "$CMD" == example-test ]]; then
    echo 'verify.sh: example-test is removed: examples carry no tests (user, 2026-10-04).' >&2
    exit 2
fi

# --no-cache and --local may sit anywhere after the subcommand; nothing below
# sees them.
NO_CACHE=0
LOCAL_ONLY=0
LOCAL_REASON=""
ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-cache)
            NO_CACHE=1
            shift
            ;;
        --local)
            if [[ $# -lt 2 || "${2:-}" =~ ^[[:space:]]*$ || "${2:-}" == --* ]]; then
                echo 'verify.sh: --local needs a reason: --local "<why this test cannot pass on macOS>". Leave it off to run on the Mac.' >&2
                exit 2
            fi
            # A reservation passed as a --local reason is recorded nowhere and
            # never ends; the Mac hold is the one way to keep tests off the Mac
            # (user, 2026-10-18).
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
set -- "${ARGS[@]}"
DEV_FEATURES="$("$PY" -c "$DEV_FEATURES_PY" "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/Cargo.toml")" || exit 2
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
PHASE_WAIT_RETRY_ROOT=""
PHASE_WAIT_PUBLISHED=0
# Publish the folder only after its owner is recorded. A missing process or
# a changed start time makes it recoverable, even if cleanup never ran.
PHASE_WAIT_OWNER_PY='
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, sys.argv[-1])
from scripts.lib import process_start

proc_root = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
owner = Path(sys.argv[2]) / "owner.json"
if sys.argv[1] == "record":
    pid = int(sys.argv[3])
    start = process_start.process_start(pid, proc_root)
    if isinstance(start, process_start.ProcessGone):
        sys.exit("verify.sh: wait owner has ended")
    identity = {"pid": pid, "start_time": start.text}
    with tempfile.NamedTemporaryFile(mode="w", dir=owner.parent, prefix=".owner-", delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(identity, output)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(owner)
    finally:
        temporary.unlink(missing_ok=True)
else:
    try:
        record = json.loads(owner.read_text())
        pid = record["pid"]
        saved_start = record["start_time"]
        if not isinstance(pid, int) or pid <= 0 or not isinstance(saved_start, str) or not saved_start:
            raise ValueError("invalid wait owner identity")
        recorded = process_start.parse_start(saved_start)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        # An unpublished folder without an owner can still be under construction.
        if sys.argv[1] == "staging-live":
            sys.exit(2)
        # Legacy folders have only a pid. A read error cannot prove it is gone.
        try:
            pid = int(owner.parent.name.split(".")[1])
            if pid <= 0:
                raise ValueError("invalid wait owner pid")
        except (ValueError, IndexError) as error:
            print(f"verify.sh: wait history NotRecorded: cannot identify {owner}: {error}", file=sys.stderr)
            sys.exit(2)
        try:
            state = process_start.process_state(pid, proc_root)
        except process_start.ProcessReadError:
            sys.exit(2)
        sys.exit(1 if state is process_start.ProcessRunState.GONE else 0)
    check = process_start.check_owner(pid, recorded, proc_root)
    if check in (process_start.OwnerCheck.GONE, process_start.OwnerCheck.REPLACED):
        sys.exit(1)
    sys.exit(0 if check is process_start.OwnerCheck.ALIVE else 2)

'
phase_wait_start() {
    local key=$1 reason=$2 file
    [[ "${CLAUDE_PROBE_SESSION:-}" != 1 && -n "$PHASE_WAIT_SESSION" \
        && -f "$PHASE_WAIT_SESSION/progress_history_state.json" && "$PHASE_WAIT_PUBLISHED" -eq 1 ]] || return 0
    if ! phase_wait_retry_pending; then
        printf 'verify.sh: wait history NotRecorded: an earlier wait still needs to close\n' >&2
        return 0
    fi
    file="$HOLD_WAIT_DIR/phase-wait.${BASHPID:-$$}.$key"
    [[ ! -s "$file" ]] || return 0
    local args=(start-wait --session-dir "$PHASE_WAIT_SESSION" --wait-file "$file"
        --kind WaitingForMemoryOrSlot --reason "$reason")
    local owner="${CODEX_THREAD_ID:-}"
    if [[ -n "$owner" ]]; then
        args+=(--worker-thread-id "$owner" --owner-session "$owner")
    else
        args+=(--director --owner-session "${CLAUDE_CODE_SESSION_ID:-}")
    fi
    if ! "$PY" "$PHASE_WAIT_RECORDER" "${args[@]}" >/dev/null; then
        printf 'verify.sh: wait history NotRecorded: %s\n' "$reason" >&2
    fi
}
phase_wait_finish() {
    phase_wait_finish_file "$HOLD_WAIT_DIR/phase-wait.${BASHPID:-$$}.$1" || true
}
phase_wait_finish_file() {
    local file=$1 id="" status=0
    [[ -s "$file" ]] || return 0
    if ! { read -r id < "$file"; } 2>/dev/null; then
        # Another scanner may have finished and removed it after the size check.
        [[ ! -e "$file" ]] && return 0
        printf 'verify.sh: wait history NotRecorded: cannot read %s\n' "$file" >&2
        return 1
    fi
    "$PY" "$PHASE_WAIT_RECORDER" finish-wait --session-dir "$PHASE_WAIT_SESSION" --wait-id "$id" || status=$?
    if [[ "$status" -eq 0 || "$status" -eq 4 ]]; then
        rm -f "$file"
    else
        printf 'verify.sh: wait history NotRecorded: cannot close %s\n' "$id" >&2
        return 1
    fi
}
phase_wait_recover_folder() {
    if ! "$PY" "$PHASE_WAIT_RECORDER" recover-wait-folder \
        --session-dir "$PHASE_WAIT_SESSION" --directory "$1"; then
        printf 'verify.sh: wait history NotRecorded: cannot close waits in %s\n' "$1" >&2
        return 1
    fi
}
phase_wait_retry_pending() {
    local directory owner_status failed=0
    [[ -n "$PHASE_WAIT_RETRY_ROOT" ]] || return 0
    # These folders were closed and renamed under the recorder lock. They are
    # only pending deletion; partial deletion may have removed their owner.
    for directory in "$PHASE_WAIT_RETRY_ROOT"/.verify-hold-removing.*; do
        [[ -d "$directory" ]] || continue
        if ! rm -rf -- "$directory"; then
            printf 'verify.sh: cannot remove retired wait folder %s\n' "$directory" >&2
            failed=1
        fi
    done
    for directory in "$PHASE_WAIT_RETRY_ROOT"/.verify-hold-new.*; do
        [[ -d "$directory" ]] || continue
        if "$PY" -c "$PHASE_WAIT_OWNER_PY" staging-live "$directory" "$PROCESS_REPOSITORY"; then
            continue
        else
            owner_status=$?
            if [[ "$owner_status" -eq 1 ]]; then
                rm -rf "$directory"
            fi
        fi
    done
    for directory in "$PHASE_WAIT_RETRY_ROOT"/verify-hold.*; do
        [[ -d "$directory" && "$directory" != "$HOLD_WAIT_DIR" ]] || continue
        if "$PY" -c "$PHASE_WAIT_OWNER_PY" live "$directory" "$PROCESS_REPOSITORY"; then
            continue
        else
            owner_status=$?
            if [[ "$owner_status" -ne 1 ]]; then
                failed=1
                continue
            fi
        fi
        phase_wait_recover_folder "$directory" || failed=1
    done
    return "$failed"
}

phase_wait_create_directory() {
    local staging directory
    if [[ -n "$PHASE_WAIT_RETRY_ROOT" ]]; then
        if staging="$(mktemp -d "$PHASE_WAIT_RETRY_ROOT/.verify-hold-new.$$.XXXXXX")" \
            && "$PY" -c "$PHASE_WAIT_OWNER_PY" record "$staging" "$$" "$PROCESS_REPOSITORY"; then
            directory="$PHASE_WAIT_RETRY_ROOT/verify-hold.${staging##*/.verify-hold-new.}"
            if mv "$staging" "$directory"; then
                HOLD_WAIT_DIR="$directory"
                PHASE_WAIT_PUBLISHED=1
                return 0
            fi
        fi
        printf 'verify.sh: wait history NotRecorded: cannot publish wait owner\n' >&2
    fi
    # Build admission still needs these files when wait history is unavailable.
    HOLD_WAIT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/verify-hold.$$.XXXXXX")"
}

# The memory gate announces only blocked admission. A granted first check
# emits no line and therefore creates no zero-length wait.
buildlog_memory_line() {
    case "$1" in
        first) phase_wait_start memory "memory admission" ;;
        done|left) phase_wait_finish memory ;;
    esac
    buildlog_memory_line_inner "$@"
}

collect_routing_wait() {
    phase_wait_finish hold
    if [[ -n "$ROUTING_WAIT_STARTED" ]]; then
        "$PY" -c 'import sys, time
with open(sys.argv[2], "a") as output:
    output.write("%.6f\n" % max(0, time.monotonic() - float(sys.argv[1])))' \
            "$ROUTING_WAIT_STARTED" "$BUILDLOG_HOLD_WAIT_FILE"
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
        python3 "$BUILD_TARGETS_SCRIPT" interrupt-local --call-id "$BUILDLOG_CALL_ID" >/dev/null 2>&1 || true
    fi
}

finish_hold_wait() {
    collect_hold_wait
    if [[ -n "${TOKEN_WAIT_ENTRY:-}" ]]; then rm -f -- "$TOKEN_WAIT_ENTRY"; fi
    if [[ -n "$MAC_IDLE_MARKER" && -f "${MAC_IDLE_SCRIPT:-/nonexistent}" ]]; then
        "$PY" "$MAC_IDLE_SCRIPT" remove "$BUILDLOG_CALL_ID" || true
    fi
    if [[ -n "$HOLD_WAIT_DIR" ]]; then
        if [[ "$PHASE_WAIT_PUBLISHED" -eq 1 ]]; then
            phase_wait_recover_folder "$HOLD_WAIT_DIR" || true
        else
            # This admission-only folder has no recorded owner or wait ids.
            rm -rf -- "$HOLD_WAIT_DIR"
        fi
        HOLD_WAIT_DIR=""
    fi
}

# Pass records (see the header): on for `test` and `lint` in a
# worker session. --no-cache turns off the lookup; its result is still
# recorded, so a flake hunt that passes clears a recorded lint failure.
CACHE_SESSION_DIR="${UNIT_RUN_DIR:-${UNIT_SESSION_DIR:-}}"
CACHE_DIR=""
if [[ -n "${CACHE_SESSION_DIR}" && ( "$CMD" == test || "$CMD" == lint ) ]]; then
    CACHE_DIR="${CACHE_SESSION_DIR}/verify_cache"
fi
RUN_LOG=""
VERDICT_KEY=""
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
if [[ "$CMD" == test && " ${ARGS[*]} " == *" --filter "* ]]; then
    FILTER_RUN=1
fi
if [[ "$CMD" == test && "$LOCAL_ONLY" -eq 1 ]]; then
    BUILDLOG_MAC=declined
    BUILDLOG_MAC_REASON=local_flag
fi

tree_key() {
    local words=("$CMD" "${ARGS[@]}") names=() name scope=() key_options=("$@")
    if [[ "$CMD" == lint ]]; then
        scope=(--assertion-guard "$ASSERTION_GUARD_SCRIPT" ${ASSERTION_RULINGS_ARGS[@]+"${ASSERTION_RULINGS_ARGS[@]}"})
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
            words+=(--filter "$name")
        done < <(printf '%s\n' "${names[@]}" | LC_ALL=C sort -u)
    fi
    if [[ "$CMD" == test ]]; then
        # Keyed on the package's own build, read from the call's metadata.
        scope=(--package "${ARGS[0]}")
    fi
    VERIFY_TEST_BUILD_INPUTS_FILE="${HOLD_WAIT_DIR:+$HOLD_WAIT_DIR/test-build-inputs}" \
    "$PY" "$TREE_WATCH_SCRIPT" --key ${key_options[@]+"${key_options[@]}"} ${scope[@]+"${scope[@]}"} "${BASH_SOURCE[0]}" "$HOME/.claude/scripts/lint/invoke.sh" \
        "${LINT_CONFIG_FILE:-$HOME/.claude/config/lint.conf}" -- "${words[@]}" \
        <<< "${GATE_METADATA}" 2>/dev/null || true
}

TREE_CHANGED_STATUS=77
TREE_WATCH_PID=""
TREE_WATCH_DIR=""

after_tree_change() {
    [[ -n "$TREE_WATCH_DIR" && -s "$TREE_WATCH_DIR/fired" ]]
}

stop_tree_watch() {
    if [[ -n "$TREE_WATCH_PID" ]]; then
        kill "$TREE_WATCH_PID" 2>/dev/null || true
        wait "$TREE_WATCH_PID" 2>/dev/null || true
        TREE_WATCH_PID=""
    fi
}

cancel_tree_run() {
    after_tree_change || return 0
    trap '' USR2
    stop_tree_watch
    if [[ -n "${MAC_RUNNER_PID:-}" ]]; then
        # offload.py's TERM handler stops the remote run and releases its claim.
        kill -TERM "$MAC_RUNNER_PID" 2>/dev/null || true
        wait "$MAC_RUNNER_PID" 2>/dev/null || true
        MAC_RUNNER_PID=""
    fi
    # Board release also stops a step's separately owned process group.
    if declare -F release_token >/dev/null; then release_token || true; fi
    if [[ -n "${RUN_STEP_PID:-}" ]]; then
        kill -KILL -- "-$RUN_STEP_PID" 2>/dev/null || true
        wait "$RUN_STEP_PID" 2>/dev/null || true
        RUN_STEP_PID=""
    fi
    # Reacquisition creates .again in the step's subshell. Both names belong
    # to this call, even when the parent never saw TOKEN_WAIT_ENTRY change.
    if [[ -n "${TOKEN_WAITER_FILE:-}" ]]; then
        rm -f "$TOKEN_WAITER_FILE" "${TOKEN_WAITER_FILE}.again"
    fi
    # run_once owns admission in a pipeline subshell. Its exact ledger paths
    # are shared with us before cargo starts, so killing it cannot leak them.
    local memory_path
    if [[ -f "$TREE_WATCH_DIR/reservations" ]]; then
        while IFS= read -r memory_path; do
            BUILDLOG_MEM_RESERVATION="$memory_path" buildlog_release_memory "$TREE_CHANGED_STATUS"
        done < "$TREE_WATCH_DIR/reservations"
    fi
    if [[ -f "$TREE_WATCH_DIR/waiters" ]]; then
        while IFS= read -r memory_path; do rm -f "$memory_path"; done < "$TREE_WATCH_DIR/waiters"
    fi
    [[ -z "${MAC_LOG_FILE:-}" ]] || rm -f "$MAC_LOG_FILE"
    [[ -z "${MAC_RESULT_FILE:-}" ]] || rm -f "$MAC_RESULT_FILE"
    printf 'verify.sh: cancelled — the tree changed while %s ran, so this result would be stale. Resubmit the %s when your edits are done.\n' "$CMD" "$CMD" >&2
    note_event cancelled "${RUN_STARTED}" "$(( SECONDS - RUN_STARTED ))" "" 0 "$TREE_CHANGED_STATUS"
    rm -rf "$TREE_WATCH_DIR"
    TREE_WATCH_DIR=""
    exit "$TREE_CHANGED_STATUS"
}

start_tree_watch() {
    [[ -z "$TREE_WATCH_PID" ]] || return 0
    TREE_WATCH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/verify-tree.$$.XXXXXX")"
    VERDICT_KEY="$(tree_key --watch-start "$TREE_WATCH_DIR/start")"
    [[ -s "$TREE_WATCH_DIR/start" ]] || { rm -rf "$TREE_WATCH_DIR"; TREE_WATCH_DIR=""; return 0; }
    local scope=()
    if [[ "$CMD" == test ]]; then scope=(--package "${ARGS[0]}"); fi
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

# Git's staged tree identity. A private copy avoids updating the real index's
# cache-tree extension when write-tree runs.
doc_tree() {
    "$PY" - <<'PYDOC'
import os
from pathlib import Path
import subprocess
import tempfile
import shutil

with tempfile.TemporaryDirectory(prefix="verify-doc-", dir="/tmp") as temporary:
    top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    original = Path(subprocess.check_output(["git", "rev-parse", "--git-path", "index"], text=True).strip())
    private_index = Path(temporary) / "index"
    if original.exists():
        shutil.copyfile(original, private_index)
    environment = {**os.environ, "GIT_INDEX_FILE": str(private_index)}
    def git(*arguments: str) -> str:
        return subprocess.check_output(["git", "-C", top, *arguments], env=environment, text=True).strip()
    print(git("write-tree"))
PYDOC
}

# The caller holds DOC_CHECKOUT/lock until rustdoc and its record are complete.
# The private index and persistent path preserve unchanged source mtimes.
doc_checkout() {
    "$PY" - "$DOC_REPO_TOP" "$DOC_TREE" "$DOC_CHECKOUT" <<'PYDOC'
import os
from pathlib import Path
import shutil
import subprocess
import sys

top, judged, checkout = sys.argv[1], sys.argv[2], Path(sys.argv[3])
tree, index, previous = checkout / "tree", checkout / "index", checkout / "tree-id"
git_dir = subprocess.check_output(["git", "-C", top, "rev-parse", "--absolute-git-dir"], text=True).strip()
environment = {**os.environ, "GIT_DIR": git_dir, "GIT_WORK_TREE": str(tree), "GIT_INDEX_FILE": str(index)}

def git(*arguments):
    subprocess.run(["git", "-C", str(tree), *arguments], env=environment, check=True)

try:
    old = previous.read_text().strip()
    if not index.is_file() or not tree.is_dir() or not old:
        raise ValueError("checkout metadata missing")
    # A timestamp-only change is clean after rehashing; a content change
    # instead triggers the one full recovery below.
    git("update-index", "--refresh")
    git("clean", "-ffdqx")
    git("read-tree", "-m", "-u", old, judged)
except (OSError, ValueError, subprocess.CalledProcessError):
    # A failed update or missing private index gets exactly one full recovery.
    if tree.exists():
        shutil.rmtree(tree)
    index.unlink(missing_ok=True)
    index.with_name(index.name + ".lock").unlink(missing_ok=True)
    tree.mkdir()
    git("read-tree", judged)
    git("checkout-index", "--all", "--index")
previous.write_text(judged + "\n")
PYDOC
}

doc_record() {
    local current_tree
    current_tree="$(doc_tree)" || return
    if [[ "$current_tree" != "$DOC_TREE" ]]; then
        echo 'verify.sh: the staged tree changed while doc ran; no doc pass record was written.'
        return 0
    fi
    "$PY" - "$DOC_REPO_TOP" "$DOC_TREE" "$BUILDLOG_CALL_ID" <<'PYDOC'
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
        BUILDLOG_MAC_COPY_COUNT="$BUILDLOG_MAC_COPY_COUNT" BUILDLOG_MAC_COPY_NATEDEV_S="$BUILDLOG_MAC_COPY_NATEDEV_S" \
        "$PY" "$BUILDLOG_RECORD" call "$outcome" "$status" "$cached" "$wait" "$wall" \
        "$build" "$saved" "${SECONDS}" "$CMD" ${ARGS[@]+"${ARGS[@]}"} \
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
# A waiting local test can still leave for this eligible Mac. Non-busy
# declines set a retry deadline; busy and blocked use the normal poll.
MAC_LEAVE_SETTINGS=""
MAC_LEAVE_REASON=""
MAC_RETRY_AFTER=0
MAC_RETRY_NOTICE_SHOWN=0
MAC_VERB=nextest
MAC_SOURCE_ROOT=""
MAC_VERB_ARGS=()
MAC_FINISH_CALL=1
MAC_PASSED=0
STEP_RAN_ON_MAC=0

forward_mac_runner_signal() {
    local signal_name=$1 exit_status=$2
    stop_tree_watch
    [[ -z "$TREE_WATCH_DIR" ]] || rm -rf "$TREE_WATCH_DIR"
    TREE_WATCH_DIR=""
    trap - INT TERM HUP
    if [[ -n "$MAC_RUNNER_PID" ]]; then
        kill -s "$signal_name" "$MAC_RUNNER_PID" 2>/dev/null || true
        wait "$MAC_RUNNER_PID" 2>/dev/null || true
    fi
    if [[ -n "$MAC_RESULT_FILE" ]]; then
        if [[ -d "$MAC_RESULT_FILE" ]]; then
            rmdir "$MAC_RESULT_FILE" 2>/dev/null || true
        else
            rm -f "$MAC_RESULT_FILE"
        fi
    fi
    if [[ -n "$MAC_LOG_FILE" ]]; then
        rm -f "$MAC_LOG_FILE"
    fi
    note_event interrupted "$SECONDS" 0 "" 0 "$exit_status"
    exit "$exit_status"
}

try_mac_test() {
    step_state waiting_mac
    MAC_PASSED=0
    [[ -z "$MAC_IDLE_MARKER" ]] || rm -f -- "$MAC_IDLE_MARKER"
    local caller_traps
    caller_traps="$(trap -p INT TERM HUP)"
    trap 'forward_mac_runner_signal INT 130' INT
    trap 'forward_mac_runner_signal TERM 143' TERM
    trap 'forward_mac_runner_signal HUP 129' HUP
    local runner_status result_text="" result_word="" result_reason="" mac_owns_watch=0
    local result_seconds="" result_holder="" mac_words=0 line
    local runner_args=(run --repo-root "${DOC_REPO_TOP:-$(git rev-parse --show-toplevel)}"
        --package "$PKG" --call-id "$BUILDLOG_CALL_ID" --settings "$1")
    if [[ "$MAC_VERB" != nextest ]]; then runner_args+=(--verb "$MAC_VERB"); fi
    if [[ -n "$MAC_SOURCE_ROOT" ]]; then runner_args+=(--source-root "$MAC_SOURCE_ROOT"); fi
    if [[ "${2:-}" == busy || "${2:-}" == blocked ]]; then
        runner_args+=(--last-decline "$2")
    fi
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

    export BUILDLOG_TARGET=mac
    # A Mac pass is recorded like a natedev one, judged on the tree as it was
    # when the Mac run began, with the output it printed.
    if [[ "$CMD" == test && -z "$TREE_WATCH_PID" ]]; then
        start_tree_watch
        mac_owns_watch=1
    elif [[ -n "${CACHE_DIR}" && -z "$TREE_WATCH_PID" ]]; then
        VERDICT_KEY="$(tree_key)"
    fi
    MAC_LOG_FILE="$(mktemp)"
    step_state on_mac
    "$PY" "$MAC_RUNNER" "${runner_args[@]}" > >(tee "$MAC_LOG_FILE") 2> >(tee -a "$MAC_LOG_FILE" >&2) &
    MAC_RUNNER_PID=$!
    if wait "$MAC_RUNNER_PID"; then
        runner_status=0
    else
        runner_status=$?
    fi
    if [[ "$mac_owns_watch" == 1 ]]; then finish_tree_watch; fi
    trap - INT TERM HUP
    eval "$caller_traps"
    MAC_RUNNER_PID=""

    if [[ -f "$MAC_RESULT_FILE" && -r "$MAC_RESULT_FILE" \
          && -s "$MAC_RESULT_FILE" ]]; then
        result_text="$(< "$MAC_RESULT_FILE")"
        while IFS= read -r line; do
            case "$line" in
                mac=*)
                    ((mac_words += 1))
                    result_word="${line#mac=}"
                    ;;
                reason=*) result_reason="${line#reason=}" ;;
                seconds=*) result_seconds="${line#seconds=}" ;;
                holder=*) result_holder="${line#holder=}" ;;
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
        rm -f "$MAC_LOG_FILE"
        MAC_LOG_FILE=""
        export BUILDLOG_TARGET=""
        step_state waiting_mac
        return
    fi

    BUILDLOG_MAC="$result_word"
    BUILDLOG_MAC_REASON="$result_reason"
    BUILDLOG_MAC_S="$result_seconds"
    BUILDLOG_MAC_HOLDER=""
    if [[ "$result_word" == declined && "$result_reason" == busy ]]; then
        BUILDLOG_MAC_HOLDER="$result_holder"
    fi
    case "$result_word" in
        declined|lost)
            rm -f "$MAC_LOG_FILE"
            MAC_LOG_FILE=""
            export BUILDLOG_TARGET=""
            step_state waiting_mac
            return
            ;;
        failed)
            rm -f "$MAC_LOG_FILE"
            MAC_LOG_FILE=""
            if [[ "$MAC_FINISH_CALL" == 0 ]]; then return "$runner_status"; fi
            note_event failed "${SECONDS}" 0 "" 0 "$runner_status"
            exit "$runner_status"
            ;;
        passed)
            MAC_PASSED=1
            if [[ "$MAC_FINISH_CALL" == 0 ]]; then
                rm -f "$MAC_LOG_FILE"
                MAC_LOG_FILE=""
                return 0
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
    python3 "$BUILD_TARGETS_SCRIPT" register-local --call-id "$BUILDLOG_CALL_ID" --pid $$ \
        2>"$HOLD_WAIT_DIR/routing-diagnostic" || admission=$?
    if ! cmp -s "$HOLD_WAIT_DIR/routing-diagnostic" "$HOLD_WAIT_DIR/last-routing-diagnostic"; then
        cat "$HOLD_WAIT_DIR/routing-diagnostic" >&2
        cp "$HOLD_WAIT_DIR/routing-diagnostic" "$HOLD_WAIT_DIR/last-routing-diagnostic"
    fi
    build_hold_check_result "$admission" register-local
}

initialize_step_record() {
    local answer table line name eligibility reason settings status=0 decision_reason="no eligible Mac" mac_targets=0
    local arguments=(eligible --kind "$CMD" --repo-root "$(git rev-parse --show-toplevel)")
    PKG="${ARGS[0]:-}"
    if [[ "$CMD" == doc || "$CMD" == final ]]; then PKG=""; fi
    if [[ "$CMD" == test ]]; then arguments+=(--package "$PKG"); fi
    if [[ "$LOCAL_ONLY" == 1 ]]; then arguments+=(--local "$LOCAL_REASON"); fi
    answer=$(python3 "$BUILD_TARGETS_SCRIPT" "${arguments[@]}") || status=$?
    if (( status != 0 )); then
        printf 'verify.sh: target eligibility check failed (exit %s); step refused\n' "$status" >&2
        exit "$status"
    fi
    line=${answer%%$'\n'*}
    case "$line" in
        mac_allowed) STEP_RUNS_ON=mac_allowed; table=${answer#*$'\n'} ;;
        natedev_only\ *) decision_reason=${line#natedev_only }; table=${answer#*$'\n'} ;;
        *)
            printf 'verify.sh: target eligibility check returned no decision; step refused\n' >&2
            exit 2 ;;
    esac
    if [[ -z "$table" || "$table" == "$line" && "$line" != *$'\t'* ]]; then
        printf 'verify.sh: target eligibility check returned no targets; step refused\n' >&2
        exit 2
    fi
    while IFS=$'\t' read -r name eligibility reason settings; do
        if [[ -z "$name" || ( "$eligibility" != eligible && "$eligibility" != excluded ) || -z "$reason" || -z "$settings" || "$settings" == *$'\t'* ]]; then
            printf 'verify.sh: target eligibility check returned invalid targets; step refused\n' >&2
            exit 2
        fi
        if [[ "$eligibility" == eligible && "$settings" != - ]]; then
            if [[ "$STEP_RUNS_ON" != mac_allowed ]]; then
                printf 'verify.sh: target eligibility answer differs from its targets; step refused\n' >&2
                exit 2
            fi
            (( mac_targets += 1 ))
        elif [[ "$eligibility" == excluded && "$settings" != - ]]; then
            decision_reason=$reason
        fi
    done <<< "$table"
    if [[ "$STEP_RUNS_ON" == mac_allowed && "$mac_targets" == 0 ]]; then
        printf 'verify.sh: target eligibility check allowed no Mac target; step refused\n' >&2
        exit 2
    fi
    STEP_TARGET_TABLE=$table
    local record_args=(write --call-id "$BUILDLOG_CALL_ID" --pid "$$"
        --repo "$(git rev-parse --show-toplevel)" --kind "$CMD"
        --runs-on "$STEP_RUNS_ON" --reason "$decision_reason" --state waiting_hold)
    if [[ "$CMD" == test ]]; then record_args+=(--package "$PKG"); fi
    env -u BUILDLOG_CALL_ID "$PY" "$STEP_RECORD_SCRIPT" "${record_args[@]}" || exit 2
    STEP_RECORD_ACTIVE=1
    export BUILDLOG_CALL_ID
    if [[ "$CMD" != test && "$STEP_RUNS_ON" == mac_allowed ]]; then
        while IFS=$'\t' read -r name eligibility reason settings; do
            if [[ "$eligibility" == eligible && "$settings" != - ]]; then
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
    step_state waiting_hold
    check_routing_hold || admission=$?
    case "$admission" in
        0) if [[ "$STEP_FORCE_MAC" == 0 ]]; then return 0; fi ;;
        10) ;;
        *) return "$admission" ;;
    esac
    step_state waiting_hold
    if [[ "$STEP_RUNS_ON" == mac_allowed && "$CMD" != test ]]; then
        if [[ "$CMD" == lint ]]; then
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
        if [[ "$command" != cargo || ( "$MAC_VERB" != check && "$MAC_VERB" != doc && "$MAC_VERB" != clippy && "$MAC_VERB" != mend-check ) ]]; then
            build_check_stop 2 'cannot route this compiling step to the Mac'
            return 2
        fi
        MAC_VERB_ARGS=()
        while (( $# )); do
            case "$MAC_VERB:$1" in
                doc:--no-deps|clippy:--all-targets|mend-check:--all-targets|mend-check:--fail-on-warn) shift ;;
                clippy:--fix|clippy:--allow-dirty|clippy:--allow-staged|clippy:--allow-no-vcs|mend-check:--fix) shift ;;
                doc:--jobs) shift 2 ;;
                *) MAC_VERB_ARGS+=("$1"); shift ;;
            esac
        done
        MAC_FINISH_CALL=0
        local route_status=0
        route_test || route_status=$?
        STEP_FORCE_MAC=0
        (( route_status == 0 )) || return "$route_status"
        if [[ "$MAC_PASSED" == 1 ]]; then STEP_RAN_ON_MAC=1; return 0; fi
    fi
    step_state waiting_hold
    build_hold_wait_local || return $?
    return 0
}

step_stopped_by_hold() {
    local admission=0
    env -u BUILDLOG_CALL_ID "$PY" "$STEP_RECORD_SCRIPT" stopped --call-id "$BUILDLOG_CALL_ID" \
        --consumed "$HOLD_WAIT_DIR/stops" || admission=$?
    case "$admission" in 0) return 0 ;; 1) ;; *) exit "$admission" ;; esac
    admission=0
    check_routing_hold || admission=$?
    case "$admission" in 0) return 1 ;; 10) return 0 ;; *) exit "$admission" ;; esac
}

# Keep one retry rule for routing and local memory or hold waits.
register_mac_wait() {
    [[ "${OSTYPE:-}" == linux* && -f "$MAC_IDLE_SCRIPT" ]] || return 0
    local board="${UNIT_RUN_DIR:-${UNIT_SESSION_DIR:-}}"
    MAC_IDLE_MARKER="$("$PY" "$MAC_IDLE_SCRIPT" register "$BUILDLOG_CALL_ID" "$$" \
        "${DOC_REPO_TOP:-$(git rev-parse --show-toplevel)}" "$PKG" "$1" "$2" \
        "${BUILDLOG_HOLD_WAITER_FILE:-}" "${board:+$board/token_waiters}" \
        "${CLAUDE_CODE_SESSION_ID:-}" "${TMPDIR:-}" 1)" || MAC_IDLE_MARKER=""
    if [[ -n "$MAC_IDLE_MARKER" ]]; then
        BUILDLOG_MAC_IDLE_HOOK="$(printf 'test -f %q && ' "$MAC_IDLE_MARKER")$(mac_retry_hook "$1" 0)"
        export BUILDLOG_MAC_IDLE_HOOK
    fi
}

remember_mac_retry() {
    case "$BUILDLOG_MAC:$BUILDLOG_MAC_REASON" in
        declined:no_tests|declined:off|declined:repo|declined:linux_only|declined:local_flag)
            if [[ "$MAC_LEAVE_SETTINGS" == "$1" ]]; then
                MAC_LEAVE_SETTINGS="" MAC_LEAVE_REASON="" MAC_RETRY_AFTER=0
                BUILDLOG_MAC_IDLE_HOOK=""
                if [[ -n "$MAC_IDLE_MARKER" ]]; then
                    "$PY" "$MAC_IDLE_SCRIPT" exclude "$BUILDLOG_CALL_ID" "$BUILDLOG_MAC_REASON" || true
                    MAC_IDLE_MARKER=""
                fi
            fi
            return 1 ;;
    esac
    MAC_LEAVE_SETTINGS="$1" MAC_LEAVE_REASON="$BUILDLOG_MAC_REASON" MAC_RETRY_AFTER=0
    if [[ "$BUILDLOG_MAC" != declined || ( "$BUILDLOG_MAC_REASON" != busy && "$BUILDLOG_MAC_REASON" != blocked ) ]]; then
        MAC_RETRY_AFTER="$("$PY" -c 'import time; print(time.monotonic() + 180)')"
        if [[ "$MAC_RETRY_NOTICE_SHOWN" == 0 ]]; then
            echo "verify.sh: Mac attempt ended ($BUILDLOG_MAC${BUILDLOG_MAC_REASON:+: $BUILDLOG_MAC_REASON}); waiting 180 s before each retry while natedev waits." >&2
            MAC_RETRY_NOTICE_SHOWN=1
        fi
    fi
    register_mac_wait "$1" "$MAC_RETRY_AFTER"
}

# The same command gates routing retries and both local wait hooks. The
# deadline uses the call's monotonic clock. free-now reads local state only.
mac_retry_hook() {
    if [[ "$2" != 0 ]]; then
        printf '{ '
        if [[ -n "$MAC_IDLE_MARKER" ]]; then printf 'test -f %q || ' "$MAC_IDLE_MARKER"; fi
        printf '%q ' "$PY" -c 'import sys, time; sys.exit(time.monotonic() < float(sys.argv[1]))' "$2"
        printf '; } && '
    fi
    printf '%q ' "$PY" "$MAC_TEST_SCRIPT" free-now --settings "$1" \
        --repo-root "${DOC_REPO_TOP:-$(git rev-parse --show-toplevel)}"
}

route_test() {
    local table name eligibility reason settings admission=0 announced_reason="" retry_index
    local retry_settings=() retry_reasons=() retry_after=()
    table=$STEP_TARGET_TABLE
    if [[ -z "$table" ]]; then
        printf 'verify.sh: target eligibility check returned no targets; step refused\n' >&2
        exit 2
    fi
    while IFS=$'\t' read -r name eligibility reason settings; do
        if [[ -z "$name" || ( "$eligibility" != eligible && "$eligibility" != excluded ) || -z "$reason" || -z "$settings" || "$settings" == *$'\t'* ]]; then
            printf 'verify.sh: target eligibility check returned invalid targets; step refused\n' >&2
            exit 2
        fi
    done <<< "$table"

    # Read exclusions before selecting a target so local-first routing also
    # records why a Mac cannot run this package.
    while IFS=$'\t' read -r name eligibility reason settings; do
        if [[ "$eligibility" == eligible && "$settings" != - && -f "$MAC_RUNNER" ]]; then
            MAC_LEAVE_SETTINGS="$settings"
            register_mac_wait "$settings" 0
        fi
        if [[ "$eligibility" == excluded && "$settings" != - ]]; then
            BUILDLOG_MAC=declined
            if [[ "$LOCAL_ONLY" -eq 0 ]]; then
                BUILDLOG_MAC_REASON="$reason"
                export BUILDLOG_LOCAL_REASON="$reason"
            fi
        fi
    done <<< "$table"

    while IFS=$'\t' read -r name eligibility reason settings; do
        [[ "$eligibility" == eligible ]] || continue
        if [[ "$settings" == - ]]; then
            if [[ "$STEP_FORCE_MAC" == 1 ]]; then continue; fi
            admission=0
            check_routing_hold || admission=$?
            case "$admission" in
                0) return 0 ;;
                10) continue ;;
                *) exit "$admission" ;;
            esac
        fi
        [[ -f "$MAC_RUNNER" ]] || continue
        try_mac_test "$settings" || return $?
        if [[ "$MAC_PASSED" == 1 ]]; then return 0; fi
        if remember_mac_retry "$settings"; then
            retry_settings+=("$settings")
            retry_reasons+=("$MAC_LEAVE_REASON")
            retry_after+=("$MAC_RETRY_AFTER")
        fi
    done <<< "$table"

    [[ ${#retry_settings[@]} -gt 0 ]] || return 0
    while true; do
        admission=0
        check_routing_hold || admission=$?
        case "$admission" in
            0) collect_routing_wait; return 0 ;;
            10) ;;
            *) exit "$admission" ;;
        esac
        if [[ "$announced_reason" != "$BUILDLOG_MAC_REASON" ]]; then
            echo "verify.sh: waiting for the Mac ($BUILDLOG_MAC_REASON) or natedev's release." >&2
            announced_reason="$BUILDLOG_MAC_REASON"
        fi
        phase_wait_start hold "build hold"
        if [[ -z "$ROUTING_WAIT_STARTED" ]]; then
            ROUTING_WAIT_STARTED="$("$PY" -c 'import time; print(time.monotonic())')"
        fi
        "$PY" "$LOCAL_RELEASE_WAIT_SCRIPT" "${BUILD_TARGETS_POLL_S:-15}" "$BUILDLOG_CALL_ID" &
        HOLD_WAITER_PID=$!
        admission=0
        wait "$HOLD_WAITER_PID" || admission=$?
        HOLD_WAITER_PID=""
        if [[ "$admission" != 0 ]]; then
            printf 'verify.sh: local release check failed (exit %s); step refused\n' "$admission" >&2
            exit "$admission"
        fi

        # Try a free Mac before checking natedev again.
        for ((retry_index=0; retry_index<${#retry_settings[@]}; retry_index++)); do
            [[ -n "${retry_settings[$retry_index]}" ]] || continue
            eval "$(mac_retry_hook "${retry_settings[$retry_index]}" "${retry_after[$retry_index]}")" >/dev/null 2>&1 || continue
            collect_routing_wait
            try_mac_test "${retry_settings[$retry_index]}" "${retry_reasons[$retry_index]}" || return $?
            if [[ "$MAC_PASSED" == 1 ]]; then return 0; fi
            if ! remember_mac_retry "${retry_settings[$retry_index]}"; then
                retry_settings[$retry_index]=""
                continue
            fi
            # Count declined claims as hold wait. A lost run may have run
            # tests, so its runtime does not count as hold wait.
            if [[ "$BUILDLOG_MAC" == declined ]]; then
                printf '%s\n' "${BUILDLOG_MAC_S:-0}" >> "$BUILDLOG_HOLD_WAIT_FILE"
            fi
            retry_reasons[$retry_index]="$MAC_LEAVE_REASON"
            retry_after[$retry_index]="$MAC_RETRY_AFTER"
            if [[ "$announced_reason" != "$BUILDLOG_MAC_REASON" ]]; then
                echo "verify.sh: waiting for the Mac ($BUILDLOG_MAC_REASON) or natedev's release." >&2
                announced_reason="$BUILDLOG_MAC_REASON"
            fi
        done
        [[ -n "$(printf '%s' "${retry_settings[@]}")" ]] || return 0
    done
}

# Starts copy_watch.py beside the natedev step run() is about to start: while
# the step compiles, it may run a copy of the same test on the Mac, and the
# first result wins (see docs/as-built/mac-test-offload.md). Its folder sits in
# the run's hold folder, so the EXIT trap removes both, and both names carry
# the run's pid for tmp_sweep.py when a kill skips the trap.
start_copy_watch() {
    COPY_WATCH_DIR="$(mktemp -d "${HOLD_WAIT_DIR:-${TMPDIR:-/tmp}}/verify-copy.$$.XXXXXX")"
    VERIFY_STEP_INFO_FILE="$COPY_WATCH_DIR/step"
    local watch_args=(--info "$VERIFY_STEP_INFO_FILE" --result "$COPY_WATCH_DIR/result"
        --runner "$MAC_RUNNER" --settings "$MAC_LEAVE_SETTINGS"
        --repo-root "$(git rev-parse --show-toplevel)" --package "$PKG"
        --call-id "$BUILDLOG_CALL_ID" --call-started "$VERIFY_CALL_STARTED")
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
# Non-busy declines wait for the retry deadline. Once the step compiles,
# copy_watch.py may run a copy on the Mac; a copy result that finishes first
# ends the call (finish_copy_win).
run_local_test() {
    local status hook copy_watch
    while true; do
        hook=""
        copy_watch=0
        if [[ -n "$MAC_LEAVE_SETTINGS" ]]; then
            hook="$(mac_retry_hook "$MAC_LEAVE_SETTINGS" "$MAC_RETRY_AFTER")"
            if [[ -f "$MAC_COPY_WATCH" && -f "$MAC_RUNNER" ]]; then
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
        try_mac_test "$MAC_LEAVE_SETTINGS" "$MAC_LEAVE_REASON"
        remember_mac_retry "$MAC_LEAVE_SETTINGS" || true
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
    # --local asks for Linux: a pass the Mac ran does not answer it.
    if [[ -f "${record}.pass" && ! ( "${LOCAL_ONLY}" -eq 1 && "$(sed -n 's/^host=//p' "${record}.pass")" == mac ) ]]; then
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
    if ! head -n 1 "${record}" | grep -qF "\`verify.sh $CMD${ARGS[*]:+ ${ARGS[*]}}\`"; then
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
    TZ="$(python3 "$(dirname "${BASH_SOURCE[0]}")/../lib/user_zone.py" 2>/dev/null)" date '+%Y-%m-%d %H:%M:%S %Z'
}

cache_record() {
    local kind=$1 repeat=$2 host=${3:-} key verb=passed other=fail
    key="$(tree_key)"
    [[ -n "${key}" && -n "${VERDICT_KEY}" ]] || return 1
    if [[ "${key}" != "${VERDICT_KEY}" ]]; then
        echo "verify.sh: the tree changed while ${CMD} ran, so this result was not recorded; the next ${CMD} runs again."
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
            "$CMD${ARGS[*]:+ ${ARGS[*]}}" "${verb}" "$(user_clock)" \
            "${UNIT_TEAM_ROLE:-the unit director}" "${repeat}" "${EXIT_STATUS}"
        if [[ "${host}" == mac ]]; then
            printf 'host=mac\n'
        fi
    } > "${CACHE_DIR}/${key}.${kind}.tmp"
    # Remove the old verdict first: a stop before publication must make the
    # next call run again, not reuse an answer this run has disproved.
    rm -f "${CACHE_DIR}/${key}.${other}"
    mv -f "${CACHE_DIR}/${key}.${kind}.tmp" "${CACHE_DIR}/${key}.${kind}"
}

# Records a Mac pass, whose output is in the file $1, as this call's pass
# record. The tree is read before the Mac run (VERDICT_KEY) and again now, so
# an edit made while the Mac ran leaves nothing recorded. $2 is the Mac's
# seconds, what a reuse saves.
record_mac_pass() {
    local log=$1 mac_seconds=${2%%.*}
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
if [[ "$CMD" == final || ( "$CMD" == test && ( ${#ARGS[@]} -eq 1 \
    || ( ${#ARGS[@]} -eq 3 && "${ARGS[1]}" == --features ) ) ) ]]; then
    GATE_METADATA="$(cargo metadata --no-deps --format-version 1)"
    if [[ "$CMD" == final ]]; then
        EXAMPLE_SCOPE=--workspace
    else
        EXAMPLE_SCOPE="${ARGS[0]}"
    fi
    if ! "$PY" -c "$UNTESTED_EXAMPLES_PY" "$EXAMPLE_SCOPE" <<< "$GATE_METADATA"; then
        exit 2
    fi
fi
# A test record's key reads the package's dependencies from it (tree_key).
if [[ "$CMD" == test && -z "${GATE_METADATA}" ]]; then
    GATE_METADATA="$(cargo metadata --no-deps --format-version 1 2>/dev/null)" || GATE_METADATA=""
fi

if [[ "${CLAUDE_PROBE_SESSION:-}" != 1 && -n "$PHASE_WAIT_SESSION" \
    && -f "$PHASE_WAIT_SESSION/progress_history_state.json" ]]; then
    # Scope retries to this run and physical working tree. Owner identity
    # lets a later call recover a stopped call without taking a live wait.
    PHASE_WAIT_RETRY_ROOT="$PHASE_WAIT_SESSION/verify_waits/$("$PY" -c \
        'import hashlib, os; print(hashlib.sha256(os.fsencode(os.getcwd())).hexdigest())')"
    if ! mkdir -p "$PHASE_WAIT_RETRY_ROOT"; then
        printf 'verify.sh: wait history NotRecorded: cannot create wait folder\n' >&2
        PHASE_WAIT_RETRY_ROOT=""
    fi
fi
phase_wait_create_directory
export BUILDLOG_HOLD_WAIT_FILE="$HOLD_WAIT_DIR/seconds"
export BUILDLOG_HOLD_WAITER_FILE="$HOLD_WAIT_DIR/waiter"
export BUILDLOG_CHECK_FAILED_FILE="$HOLD_WAIT_DIR/check-failed"
trap 'hold_status=$?; interrupt_hold_wait; finish_hold_wait; remove_step_record; final_pass_finish "$hold_status"; exit "$hold_status"' EXIT
hold_interrupted() {
    local signal_name=$1 code=$2
    trap - INT TERM HUP
    if [[ -n "$HOLD_WAITER_PID" ]]; then
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
phase_wait_retry_pending || true
initialize_step_record
if cache_lookup; then
    exit "${LOOKUP_STATUS}"
fi
if [[ "$CMD" == test ]]; then
    compose_nextest_args
    route_test
fi
while ! build_hold_nothing_held; do
    step_state waiting_hold
    if [[ "$CMD" != test && "$STEP_RUNS_ON" == mac_allowed ]]; then
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
        HOLD_WAIT_ARGS+=(--leave-hook "$(mac_retry_hook "$MAC_LEAVE_SETTINGS" "$MAC_RETRY_AFTER")")
    fi
    python3 "$BUILD_TARGETS_SCRIPT" "${HOLD_WAIT_ARGS[@]}" &
    HOLD_WAITER_PID=$!
    HOLD_WAIT_STATUS=0
    wait "$HOLD_WAITER_PID" || HOLD_WAIT_STATUS=$?
    HOLD_WAITER_PID=""
    phase_wait_finish hold
    if [[ "$HOLD_WAIT_STATUS" == "$LINT_LEFT_QUEUE_STATUS" && -n "$MAC_LEAVE_SETTINGS" ]]; then
        try_mac_test "$MAC_LEAVE_SETTINGS" "$MAC_LEAVE_REASON"
        remember_mac_retry "$MAC_LEAVE_SETTINGS" || true
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
# token is given back for each memory wait, so the two differ between steps.
TOKEN_USED=0
TOKEN_HELD=0
# A step routed to the Mac skips its first token acquisition.
TOKEN_WAITED=0
TOKEN_WAITER_PID=""
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
# behind newer ones (2026-10-22). A call that gives the token back for a memory
# or hold wait waits again under its first start, in a file with an `.again`
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
    "$PY" -c 'import hashlib, sys; print(hashlib.sha256("\0".join(sys.argv[1:]).encode()).hexdigest()[:16])' \
        "$(pwd -P)" "$CMD" ${key_args[@]+"${key_args[@]}"}
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
# TOKEN_WAIT_NOTE_MIN minutes, and one when the token is taken. Before, a step
# that gave the token back for memory waited again with no line, and its log
# ended on "starting anyway" for hours (2026-10-22).
token_wait_note() {
    local kind=$1 waited_min=$2 holder="" who
    read -r holder < "${BOARD_DIR}/locks/cargo.d/holder" 2>/dev/null || holder=""
    if [[ -n "$holder" ]]; then
        who="$holder holds it"
    elif (( TOKEN_WAITERS_AHEAD > 0 )); then
        who="it is free and goes to an earlier call"
    else
        who="it came free just now"
    fi
    case "$kind" in
        first) printf 'waiting for the cargo token since %s: %s; calls ahead of this one: %d\n' \
            "$(TZ="$(buildlog_memory_python "$USER_ZONE_SCRIPT" 2>/dev/null)" date '+%H:%M %Z')" \
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
# Waits until this run holds the token, with no time limit: the pid reclaim and
# the 3600 s hold already free a dead or stuck holder, and the queue above
# drops a waiter whose verify.sh ended. Sets TOKEN_WAITED to the seconds it
# waited.
acquire_token() {
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
                --pid $$ --hold 3600 --wait 3 >/dev/null 2>&1 &
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
        if [[ "$CMD" == test && -n "$MAC_LEAVE_SETTINGS" ]] \
              && eval "$(mac_retry_hook "$MAC_LEAVE_SETTINGS" "$MAC_RETRY_AFTER")" >/dev/null 2>&1; then
            try_mac_test "$MAC_LEAVE_SETTINGS" "$MAC_LEAVE_REASON"
            remember_mac_retry "$MAC_LEAVE_SETTINGS" || true
        elif [[ "$CMD" != test && "$STEP_RUNS_ON" == mac_allowed && -n "$MAC_IDLE_MARKER" && -f "$MAC_IDLE_MARKER" ]] \
              && eval "$(mac_retry_hook "$MAC_LEAVE_SETTINGS" "$MAC_RETRY_AFTER")" >/dev/null 2>&1; then
            STEP_FORCE_MAC=1
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
    TOKEN_HELD=1
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

sys.path.insert(0, sys.argv[1])
from scripts.lib import process_start

proc_root = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
lock = Path(sys.argv[2])
holder = sys.argv[3]
command = sys.argv[4:]

def shell_status(status: int) -> int:
    return status if status >= 0 else 128 - status


child = os.fork()
if child:
    def forward(signum, _frame):
        try:
            os.killpg(child, signum)
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
        print("verify.sh: this run lost the cargo token before its step started; run it again.",
              file=sys.stderr)
        sys.exit(125)
    start = process_start.process_start(os.getpid(), proc_root)
    if isinstance(start, process_start.ProcessGone):
        sys.exit("verify.sh: step owner has ended; step refused")
    record = lock / f"owned.{os.getpid()}.tmp"
    record.write_text(json.dumps({"group": os.getpid(), "start": start.text}))
    os.replace(record, lock / "owned")
    fcntl.flock(guard, fcntl.LOCK_UN)

sys.exit(shell_status(subprocess.call(command)))
'

# Preserve the shared invocation policy (including systemd scopes and its
# fallback), changing only the command it launches while this token is held.
eval "$(declare -f buildlog_exec | sed '1s/buildlog_exec/buildlog_exec_unowned/')"
buildlog_exec() {
    if [[ "${TOKEN_HELD}" -eq 1 ]]; then
        buildlog_exec_unowned "$PY" -c "$STEP_OWNER_PY" \
            "$PROCESS_REPOSITORY" "${BOARD_DIR}/locks/cargo.d" "$$" "$@"
    else
        buildlog_exec_unowned "$@"
    fi
}

release_token() {
    # A step that gave the token back and left for the Mac did so in run()'s
    # subshell; its mark tells this shell the token is no longer held. A step
    # killed as its acquire returned may hold it all the same, so free that.
    if [[ -n "${TOKEN_WAIT_FILE:-}" && -e "${TOKEN_WAIT_FILE}.given_back" ]]; then
        rm -f "${TOKEN_WAIT_FILE}.given_back"
        if [[ "${TOKEN_HELD}" -eq 1 ]]; then
            bash "${BOARD_HELPER}" release "${BOARD_DIR}" "${BOARD_SLOT}" cargo --pid $$ >/dev/null 2>&1 || true
        fi
        TOKEN_HELD=0
    fi
    [[ "${TOKEN_HELD}" -eq 1 ]] || return 0
    local release_status=0
    bash "${BOARD_HELPER}" release "${BOARD_DIR}" "${BOARD_SLOT}" cargo --pid $$ >/dev/null \
        || release_status=$?
    # An expired hold may be reclaimed while this step is still running. The
    # new holder owns the token; the old step's result remains authoritative.
    (( release_status == 0 || release_status == 3 )) || return "$release_status"
    TOKEN_HELD=0
}

# One try for the token, with no wait. Status 0 when it is held. A call that
# waits ahead of this one in the queue makes it a wait.
try_token_now() {
    [[ "${TOKEN_USED}" -eq 1 && "${TOKEN_HELD}" -eq 0 ]] || return 0
    count_token_waiters_ahead
    (( TOKEN_WAITERS_AHEAD == 0 )) || return 1
    bash "${BOARD_HELPER}" acquire "${BOARD_DIR}" "${BOARD_SLOT}" cargo \
        --pid $$ --hold 3600 --wait 0 >/dev/null 2>&1 || return 1
    TOKEN_HELD=1
}

# The token is for natedev cargo only. A step gives it back while it waits for
# memory, and takes it again once the gate admits it; a step that leaves the
# wait for the Mac does not. A token that is not free at once means a wait,
# and a reservation kept through it promises memory that no build uses: the
# gate then holds every other step back (257 GiB promised on 2026-10-20). So
# the step gives the reservation back, waits for the token, and passes the
# gate again with the token held. The step runs in run()'s subshell, so its
# token wait reaches the call record through TOKEN_WAIT_FILE.
eval "$(declare -f buildlog_wait_for_memory | sed '1s/buildlog_wait_for_memory/buildlog_wait_for_memory_only/')"
# Share exact admission paths across run_once's subshell boundary. Releasing
# a reservation twice is safe; keep earlier paths if the step re-enters admission.
remember_tree_reservation() {
    if [[ -n "${TREE_WATCH_DIR:-}" && -n "${BUILDLOG_MEM_RESERVATION:-}" ]]; then
        printf '%s\n' "$BUILDLOG_MEM_RESERVATION" >> "$TREE_WATCH_DIR/reservations"
    fi
}

buildlog_wait_for_memory() {
    local memory_wait admission_start
    if [[ -n "${TREE_WATCH_DIR:-}" ]] && admission_start="$("$PY" -c "
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from scripts.lib import process_start
try:
    start = process_start.process_start(int(sys.argv[2]), Path(os.environ.get(\"VERIFY_PROC_ROOT\", \"/proc\")))
except process_start.ProcessReadError:
    sys.exit(2)
if not isinstance(start, process_start.ProcessStartTime):
    sys.exit(1)
print(start.text)
" "$PROCESS_REPOSITORY" "${BASHPID:-$$}")"; then
        printf '%s/admission/%s-%s.waiting\n' "${BUILDLOG_DIR:-$HOME/.local/state/buildlog}" \
            "${BASHPID:-$$}" "$admission_start" >> "$TREE_WATCH_DIR/waiters"
    fi
    step_state waiting_hold
    if release_token && [[ "${TOKEN_USED}" -eq 1 && -n "${TOKEN_WAIT_FILE:-}" ]]; then
        : > "${TOKEN_WAIT_FILE}.given_back"
    fi
    buildlog_wait_for_memory_only "$@" || return $?
    remember_tree_reservation
    [[ "$BUILDLOG_MEM_OUTCOME" != LeftQueue ]] || return 0
    TOKEN_WAITED=0
    if ! try_token_now; then
        buildlog_release_memory 0
        acquire_token || return $?
        memory_wait=$BUILDLOG_MEM_WAIT_S
        # The second pass keeps the step's place in the memory queue from its first wait.
        BUILDLOG_MEM_WAIT_SINCE=$BUILDLOG_MEM_WAIT_FROM buildlog_wait_for_memory_only "$@" || return $?
        BUILDLOG_MEM_WAIT_S=$(( memory_wait + BUILDLOG_MEM_WAIT_S ))
        remember_tree_reservation
        if [[ "$BUILDLOG_MEM_OUTCOME" == LeftQueue ]]; then
            if release_token && [[ "${TOKEN_USED}" -eq 1 && -n "${TOKEN_WAIT_FILE:-}" ]]; then
                : > "${TOKEN_WAIT_FILE}.given_back"
            fi
            return 0
        fi
    fi
    [[ -z "${TOKEN_WAIT_FILE:-}" ]] || rm -f "${TOKEN_WAIT_FILE}.given_back"
    if (( TOKEN_WAITED > 0 )) && [[ -n "${TOKEN_WAIT_FILE:-}" ]]; then
        printf '%s\n' "$TOKEN_WAITED" >> "$TOKEN_WAIT_FILE"
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
    VERDICT_KEY="$(VERIFY_COMPUTE_TEST_BUILD=1 tree_key)"
    export BUILDLOG_TEST_BUILD_KEY="" BUILDLOG_TEST_TARGET_DIR=""
    if [[ -f "$HOLD_WAIT_DIR/test-build-inputs" ]]; then
        { IFS= read -r BUILDLOG_TEST_BUILD_KEY; IFS= read -r BUILDLOG_TEST_TARGET_DIR; } \
            < "$HOLD_WAIT_DIR/test-build-inputs"
    fi
    # A hold can also arrive during memory admission, after buildlog_begin.
    if [[ -n "${BUILDLOG_START:-}" ]]; then
        BUILDLOG_TREE="$("$PY" "$BUILDLOG_RECORD" key 2>/dev/null)" || BUILDLOG_TREE=""
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
    if release_token && [[ "${TOKEN_USED}" -eq 1 && -n "${TOKEN_WAIT_FILE:-}" ]]; then
        : > "${TOKEN_WAIT_FILE}.given_back"
    fi
    build_hold_wait_local_only "$@" || status=$?
    phase_wait_finish hold
    [[ "$status" == 0 ]] || return "$status"
    acquire_token || return $?
    [[ -z "${TOKEN_WAIT_FILE:-}" ]] || rm -f "${TOKEN_WAIT_FILE}.given_back"
    if (( TOKEN_WAITED > 0 )) && [[ -n "${TOKEN_WAIT_FILE:-}" ]]; then
        printf '%s\n' "$TOKEN_WAITED" >> "$TOKEN_WAIT_FILE"
    fi
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
if [[ -n "${CACHE_DIR}" ]] && mkdir -p "${CACHE_DIR}"; then
    # Taken now, after the token and hold waits: an edit made while this call
    # waited is part of the tree it runs on.
    if [[ "$CMD" != test ]]; then VERDICT_KEY="$(tree_key)"; fi
    RUN_LOG="${CACHE_DIR}/run.$$.log"
    : > "${RUN_LOG}"
    exec > >(tee -a "${RUN_LOG}") 2> >(tee -a "${RUN_LOG}" >&2)
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
        env -u BUILDLOG_CALL_ID "$PY" "$STEP_RECORD_SCRIPT" state --call-id "$BUILDLOG_CALL_ID" waiting_hold || true
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
    remove_step_record
}
# EXIT alone would report success for a failed cargo run, so branch on the
# status the trap receives; a lint failure record keeps it.
trap 'EXIT_STATUS=$?; [[ ${EXIT_STATUS} -eq 0 ]] && verify_cleanup completed || verify_cleanup error; final_pass_finish "$EXIT_STATUS"; exit "${EXIT_STATUS}"' EXIT
verify_interrupted() {
    trap - EXIT INT TERM HUP
    EXIT_STATUS=$((128 + $1))
    interrupt_hold_wait
    if [[ -n "$RUN_STEP_PID" ]]; then
        kill -s "$1" -- "-$RUN_STEP_PID" 2>/dev/null || true
        release_token || true
        wait "$RUN_STEP_PID" 2>/dev/null || true
        RUN_STEP_PID=""
    fi
    verify_cleanup interrupted
    final_pass_finish "$EXIT_STATUS"
    exit "$EXIT_STATUS"
}
trap 'verify_interrupted 2' INT
trap 'verify_interrupted 15' TERM
trap 'verify_interrupted 1' HUP

case "$CMD" in
    check)
        PKG="${1:?verify.sh check <package>}"
        shift
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
        run cargo check --workspace --lib --bins "${KEEP_GOING[@]}" "${FEATURE_FLAGS[@]}"
        ;;
    build)
        PKG="${1:?verify.sh build <package> [--bin <name> | --example <name>] [--release] [--features <list>]}"
        shift
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
        PKG="${1:?verify.sh lint <package>}"
        shift
        CHECK_LINT=0
        if [[ "${1:-}" == --check ]]; then
            if [[ "$PKG" != --workspace ]]; then
                echo 'verify.sh: lint --check requires --workspace' >&2
                exit 2
            fi
            CHECK_LINT=1
            shift
        fi
        [[ $# -eq 0 ]] || { usage; exit 2; }
        ASSERTION_REPO="${WORKSPACE_ROOT:-$(git rev-parse --show-toplevel)}"
        ASSERTION_ARGS=(--repo "$ASSERTION_REPO" ${ASSERTION_RULINGS_ARGS[@]+"${ASSERTION_RULINGS_ARGS[@]}"})
        if [[ -n "${VERIFY_ASSERTION_BASE:-}" ]]; then
            ASSERTION_ARGS+=(--base "$VERIFY_ASSERTION_BASE")
        fi
        [[ "$PKG" != --workspace ]] || echo 'style-lint: assertion guard start'
        "$PY" "$ASSERTION_GUARD_SCRIPT" "${ASSERTION_ARGS[@]}"
        [[ "$PKG" != --workspace ]] || echo 'style-lint: assertion guard passed'
        if [[ "$PKG" != --workspace ]]; then
            require_member "$PKG"
        fi
        require_taplo
        if [[ "$HOLD_LINT" == 1 ]]; then
            # Format locally; the Mac judges the resulting tree without fixes.
            fmt_cargo --all
            GATE_METADATA="$(cargo metadata --no-deps --format-version 1)"
            WORKSPACE_ROOT="$("$PY" -c 'import json, sys; print(json.load(sys.stdin)["workspace_root"])' <<< "$GATE_METADATA")"
            (cd "$WORKSPACE_ROOT" && run taplo fmt)
            lint_status=0
            if lint_config_enabled mend; then
                invoke_mend --workspace --fail-on-warn "${FEATURE_FLAGS[@]}" || lint_status=1
            fi
            if lint_config_enabled clippy; then
                start_tree_watch
                invoke_clippy --workspace --all-targets --all-features || lint_status=1
                finish_tree_watch
            fi
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
            GATE_METADATA="$(cargo metadata --no-deps --format-version 1)"
            WORKSPACE_ROOT="$("$PY" -c 'import json, sys; print(json.load(sys.stdin)["workspace_root"])' <<< "$GATE_METADATA")"
            if ! (cd "$WORKSPACE_ROOT" && run taplo fmt --check); then
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
        # Order: mend, fmt, taplo, clippy fix, fmt, clippy judgment.
        # Cargo stages are gated by lint.conf; the Taplo check always runs.
        # mend passes --all-targets because it needs the test and example
        # targets to see every import and visibility site. It covers the
        # workspace like the other stages, so
        # --fix can also rewrite a member the phase did not edit, when this
        # phase's change left an item there unused or over-visible.
        if lint_config_enabled mend; then
            [[ "$PKG" != --workspace ]] || echo 'style-lint: mend start'
            MEND_LOG="$(mktemp)"
            # pipefail is set, so a failing mend fails the pipeline; the log
            # only decides which message names the failure.
            if invoke_mend --workspace --fix "${FEATURE_FLAGS[@]}" 2>&1 | tee "$MEND_LOG"; then
                :
            else
                mend_status=${PIPESTATUS[0]}
                if (( mend_status == 137 )); then
                    rm -f "$MEND_LOG"
                    exit 137
                fi
                if grep -qiE 'rolled back|revert' "$MEND_LOG"; then
                    echo "verify.sh: cargo mend --fix rolled its rewrites back; the tree reproduces it — run /mend_fix" >&2
                else
                    echo "verify.sh: cargo mend --fix failed; see the output above" >&2
                fi
                rm -f "$MEND_LOG"
                exit 1
            fi
            rm -f "$MEND_LOG"
            [[ "$PKG" != --workspace ]] || echo 'style-lint: mend passed'
        else
            lint_config_skip_notice mend "cargo mend --all-targets --workspace --fix"
        fi
        # fmt compiles nothing, so it stays on the phase's own work: this
        # package and every member that differs from HEAD once mend has run.
        # Covering them all is what lets one lint record answer for each
        # package (tree_key).
        if [[ "$PKG" == --workspace ]]; then
            FMT_SCOPE=(--all)
        else
            FMT_SCOPE=(-p "$PKG")
            FMT_MEMBERS="$(cargo metadata --no-deps --format-version 1 \
            | "$PY" -c "$CHANGED_MEMBERS_PY")"
            while IFS= read -r member; do
                if [[ -n "$member" && "$member" != "$PKG" ]]; then
                    FMT_SCOPE+=(-p "$member")
                fi
            done <<< "$FMT_MEMBERS"
        fi
        if [[ "$PKG" == --workspace ]] && lint_config_enabled fmt; then
            echo 'style-lint: rustfmt start'
        fi
        fmt_cargo "${FMT_SCOPE[@]}"
        if [[ "$PKG" == --workspace ]] && lint_config_enabled fmt; then
            echo 'style-lint: rustfmt passed'
        fi
        # Record the tree Taplo reads; clippy fix and fmt update the key below.
        if [[ -n "${RUN_LOG}" ]]; then
            VERDICT_KEY="$(tree_key)"
        fi
        # Cargo's workspace root can differ from the caller's directory.
        # Run there so Taplo uses the same config and scope as the push gate.
        GATE_METADATA="$(cargo metadata --no-deps --format-version 1)"
        WORKSPACE_ROOT="$("$PY" -c 'import json, sys; print(json.load(sys.stdin)["workspace_root"])' <<< "$GATE_METADATA")"
        TAPLO_ARGS=(fmt --check)
        if [[ "$HOLD_LINT" == 1 ]]; then TAPLO_ARGS=(fmt); fi
        if (cd "$WORKSPACE_ROOT" && run taplo "${TAPLO_ARGS[@]}"); then
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
        if lint_config_enabled clippy; then
            [[ "$PKG" != --workspace ]] || echo 'style-lint: clippy start'
            clippy_tree_before="$(tree_key)"
            for ((clippy_fix_pass=1; HOLD_LINT == 0 && clippy_fix_pass<=3; clippy_fix_pass++)); do
                invoke_clippy_fix --workspace --all-targets --all-features --keep-going || true
                fmt_cargo "${FMT_SCOPE[@]}"
                clippy_tree_after="$(tree_key)"
                if [[ -n "$clippy_tree_before" && "$clippy_tree_before" == "$clippy_tree_after" ]]; then
                    break
                fi
                # An empty key cannot prove that the tree stopped changing.
                if (( clippy_fix_pass == 3 )); then
                    CLIPPY_FIX_LIMIT_REACHED=1
                fi
                clippy_tree_before="$clippy_tree_after"
            done
            start_tree_watch
            clippy_status=0
            invoke_clippy --workspace --all-targets --all-features --keep-going || clippy_status=$?
            finish_tree_watch
            (( clippy_status == 0 )) || exit "$clippy_status"
            [[ "$PKG" != --workspace ]] || echo 'style-lint: clippy passed'
        else
            invoke_clippy --workspace --all-targets --all-features
        fi
        ;;
    doc)
        DOC_FEATURE_FLAGS=()
        DOC_FEATURES=""
        if [[ $# -eq 2 && "$1" == --features && -n "$2" ]]; then
            DOC_FEATURES=$2
        elif [[ $# -ne 0 ]]; then
            usage
            exit 2
        fi
        DOC_FEATURES="$(merge_dev_features "$DOC_FEATURES")"
        if [[ -n "$DOC_FEATURES" ]]; then
            DOC_FEATURE_FLAGS=(--features "$DOC_FEATURES")
        fi
        DOC_REPO_TOP="$(git rev-parse --show-toplevel)"
        # Cargo resolves its configured target at the original repo, never at
        # the isolated source checkout. Relative env overrides use that top.
        if [[ -z "${CARGO_TARGET_DIR:-}" ]]; then
            CARGO_TARGET_DIR="$(cd "$DOC_REPO_TOP" && cargo metadata --no-deps --format-version 1 \
                | "$PY" -c 'import json, sys; print(json.load(sys.stdin)["target_directory"])')"
        fi
        CARGO_TARGET_DIR="$("$PY" -c 'from pathlib import Path; import sys; print((Path(sys.argv[1]) / sys.argv[2]).resolve())' "$DOC_REPO_TOP" "$CARGO_TARGET_DIR")"
        export CARGO_TARGET_DIR
        DOC_CHECKOUT="$CARGO_TARGET_DIR/verify-doc/$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:24])' "$DOC_REPO_TOP")"
        mkdir -p "$DOC_CHECKOUT"
        # fd 8 holds the doc checkout lock; fd 9 belongs to nextest sharing.
        exec 8>"$DOC_CHECKOUT/lock"
        flock 8
        DOC_TREE="$(doc_tree)"
        doc_checkout
        if ! lint_config_enabled doc; then
            echo 'verify.sh: the phase rustdoc check cannot be switched off; ignoring doc=off.'
        fi
        (
            cd "$DOC_CHECKOUT/tree"
            # Keep repository identity at the caller and copy only the judged tree.
            MAC_SOURCE_ROOT="$DOC_CHECKOUT/tree"
            LINT_CONFIG_FORCE=1 invoke_doc --workspace "${DOC_FEATURE_FLAGS[@]}"
        )
        doc_record
        ;;
    example)
        PKG="${1:?verify.sh example <package> <name>}"
        NAME="${2:?verify.sh example <package> <name>}"
        FEATURES="$(example_features "$PKG" "$NAME")"
        # Clippy gives focused lint feedback on one changed example while
        # iterating; the full `lint` command checks every workspace target.
        # Example names are unique across the workspace, so --workspace
        # --example builds this one example under the workspace's feature
        # resolution. A required feature that resolution leaves off still
        # makes its own variant; qualify_features names it on this package.
        if [[ -n "$FEATURES" ]]; then
            invoke_clippy --workspace --example "$NAME" \
                --features "$(qualify_features "$PKG" "$FEATURES")"
        else
            invoke_clippy --workspace --example "$NAME"
        fi
        ;;
    final)
        FINAL_FEATURES="$(merge_dev_features "")"
        if [[ -n "$FINAL_FEATURES" ]]; then
            FEATURE_FLAGS=(--features "$FINAL_FEATURES")
        fi
        run cargo check --workspace --all-targets "${FEATURE_FLAGS[@]}"
        run_nextest --workspace --tests "${FEATURE_FLAGS[@]}"
        ;;
    *)
        usage
        exit 2
        ;;
esac
