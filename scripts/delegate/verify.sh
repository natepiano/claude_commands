#!/usr/bin/env bash
# verify.sh — The only build/test/lint commands a delegate may run.
#
# Work Orders list exact invocations of this script; the delegate composes no
# cargo flags and makes no scope choices. Cargo's default target selection
# compiles examples, so every dev-loop subcommand pins explicit targets
# (--lib/--bins/--tests). `test` includes examples with test = true; other
# examples are left out. Nothing below `final` uses --all-targets (mend
# excepted, see `lint`); `final` is the plan-final full
# gate, run by the unit director, never by a phase delegate.
#
# Package selection is always --workspace, with default features. Cargo
# resolves features per invocation from the selected packages, so `-p <pkg>`
# gives every member its own feature set for bevy and the rest of the
# dependency tree, and each set is a separate compile of that tree in the
# shared target directory: nine to eleven copies of bevy_render across the
# members, which evict one another under the sweep budget and rebuild in
# minutes each time a phase moves to a new package. --workspace resolves one
# set for the whole tree, so every command below reuses one compiled copy.
# The package argument still decides what runs and what is reported: nextest
# filters to it with -E 'package(<pkg>)', and --features qualifies its list
# with it. `test` builds the named package's test targets, plus workspace libs
# when the package has a lib.
#
# rustdoc is the exception: `lint` documents only the members that differ from
# HEAD, found here rather than named by the delegate (CHANGED_MEMBERS_PY). A
# workspace rustdoc starts one process per member at 1 to 5 GB each, and
# overlapping runs ran natedev out of memory on 2026-09-30. Each distinct member
# set pays its own dependency compile for that; the push gate still documents
# the whole workspace.
#
# The lint stages — mend, fmt, clippy, doc — are gated by config/lint.conf (edit
# it with /lint_config), so one switch silences a check across /clippy, the fix
# pipeline, and every delegate phase. A phase runs all four; only the
# branch-wide style review is left to the plan-final gate. A gated-off check
# prints a SKIPPED line and the command still exits 0. Scope is never
# configurable: the target pinning and the single package selection above are
# correctness and cache constraints, not preferences. cargo check and cargo
# nextest are never gated — a phase that compiles nothing has verified nothing.
#
# A delegate session remembers passes. With PLAN_DELEGATE_BOARD_DIR or
# PLAN_DELEGATE_SESSION_DIR set, a `test` or `lint` that exits 0 is recorded in
# <session>/verify_cache/, keyed by the working tree as the run left it, the
# arguments, rustc -vV, lint.conf, these scripts and the RUST*/CARGO_*/NEXTEST_*
# environment. A plain `lint <package>` leaves the package out of the key:
# every lint stage but fmt covers the workspace, and fmt covers every member
# that differs from HEAD, so one lint record answers for each package. A
# `test --filter` key holds its names sorted, each once, so one set of names
# finds one record whatever order a call gives them in. The same call on the
# same tree then prints that record and its log and exits 0 without the cargo
# token: seats re-ran unchanged trees 416 times in the week to 2026-10-01. A
# `test --filter` reuse prints one line instead, since iteration feedback needs
# only the verdict. A failed `lint` is recorded the same way
# and replayed with its full output and exit status, since lint on an
# unchanged tree fails the same way (one seat ran 12 in a row on 2026-10-01).
# A failed `test` is never recorded (tests flake), nor a run cut short by a
# usage error, the sandbox or a kill. A `test` that changed the tree records
# nothing, and a run outside a delegate session is never cached.
# end_session.sh deletes the records when the run ends.
#
# Each record carries what a repeat would cost: the run's wall time minus
# cargo's own "Finished … in" build times, since a repeat on the same tree
# builds nothing. Every call, of every verb, in a delegate session or not,
# leaves one call record in the build log (~/.local/state/buildlog; `buildlog
# schema` lists its fields): ran, failed, interrupted, reused (a pass) or
# replayed (a lint failure), with the seconds saved and the repo, worktree,
# branch and commit. The steps it ran carry the same call id. The build log
# outlives the records; /verify_saved reports the time saved.
#
# Usage:
#   verify.sh check <package>              fast compile feedback (workspace
#                                          lib + bins)
#   verify.sh test <package>               the package's unit + integration tests
#                                          (package targets; workspace libs if any);
#                                          refuse examples that hold a test
#   verify.sh test <package> <int_test>    one named integration test target,
#                                          for re-running it alone
#   verify.sh test <package> --filter <name> [--filter <name> …]
#                                          only the package's tests whose name
#                                          contains any <name>, while iterating;
#                                          never a gate: its pass is recorded
#                                          under its own words, so it never
#                                          answers `test <package>`. Repeat
#                                          --filter to run a change's tests in
#                                          one call; in a delegate session the
#                                          same names, in any order, on an
#                                          unchanged tree print one line and
#                                          build nothing
#   verify.sh lint <package>               mend --fix, nightly fmt of the package
#                                          and the members that differ from HEAD,
#                                          workspace clippy (warnings denied), then
#                                          rustdoc on the members that differ
#                                          — every stage gated by config/lint.conf
#   … [--features <list>]                  check, test, and lint accept one trailing
#                                          `--features a,b` when the Work Order names
#                                          it: code behind a non-default feature has
#                                          no other route to a gate. Names without a
#                                          `/` are qualified as <package>/<name>
#   … --no-cache                           test and lint: run even when this
#                                          exact call already passed (or lint
#                                          failed) on this tree, a flake hunt;
#                                          the result replaces the record
#   verify.sh fmt <package>               format only (checkpoint-commit backstop)
#                                          — gated by config/lint.conf
#   verify.sh example <package> <name>     clippy one example (only when the
#                                          phase changed that example)
#   verify.sh final                        full workspace gate; refuse examples
#                                          that hold a test (unit director only)
#   verify.sh --session-dir <dir> …        any of the above inside a delegate
#                                          session; same as setting
#                                          PLAN_DELEGATE_SESSION_DIR=<dir>
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
    local attempt status started ended step_log
    RUN_LAST_ATTEMPT_KILLED=0
    step_log="$(mktemp)"
    for attempt in 1 2; do
        started="$(date '+%Y-%m-%d %H:%M:%S %z')"
        if run_once "$@" 2>&1 | tee "$step_log"; then
            RUN_LAST_ATTEMPT_KILLED=0
            rm -f "$step_log"
            return 0
        else
            status=${PIPESTATUS[0]}
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
flags = {"bin": "--bin", "test": "--test", "example": "--example", "bench": "--bench"}
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
        source = Path(target["src_path"])
        if source.name == "main.rs" and source.parent.parent.name == "examples":
            sources = sorted(source.parent.rglob("*.rs"))
        else:
            sources = [source]
        for path in sources:
            for line_number, line in enumerate(path.read_text().splitlines(), 1):
                if match := test_marker.match(line):
                    offenders.append((target["name"], path, line_number, match.group(1), package["name"]))
                    break
            else:
                continue
            break

for index, (name, path, line_number, attribute, package_name) in enumerate(offenders):
    if index:
        print(file=sys.stderr)
    print(
        f"example {name} holds a test: {path}:{line_number} has {attribute}, and examples carry no tests.",
        file=sys.stderr,
    )
    print(f"move it into {package_name}\x27s src/ or tests/, or delete it.", file=sys.stderr)
if offenders:
    sys.exit(2)
'

# Workspace members with a file that differs from HEAD: staged, unstaged,
# deleted, or new and not ignored. A phase commits nothing before its
# checkpoint, so HEAD is where the phase started and this is the whole phase's
# work, not its last edit. A file belongs to the member whose folder holds it,
# the deepest one when members nest; a file outside every member (the root
# manifest, Cargo.lock, docs/) adds none. A non-empty argv[1] is always
# included: --features names that package, so cargo needs it selected.
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
changed = {sys.argv[1]} if sys.argv[1] else set()
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
TREE_KEY_PY='
import hashlib
import os
import subprocess
import sys


def run(*args: str | bytes) -> bytes:
    result = subprocess.run(list(args), capture_output=True)
    if result.returncode != 0:
        sys.exit(1)
    return result.stdout


def content(path: bytes) -> bytes:
    if os.path.islink(path):
        return b"link " + os.readlink(path)
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).digest()
    return b"absent"


split = sys.argv.index("--")
# --no-optional-locks and git status, not git diff: diff rewrites a stat-dirty
# index even with the flag (git 2.54), and the index of this worktree belongs to the
# session working in it. Same paths: staged, unstaged, deleted, untracked.
top = run("git", "--no-optional-locks", "rev-parse", "--show-toplevel").strip()
key = hashlib.sha256(top + b"\0" + run("git", "--no-optional-locks", "-C", top, "rev-parse", "HEAD^{tree}"))
status = run("git", "--no-optional-locks", "-C", top, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
for path in sorted({entry[3:] for entry in status.split(b"\0") if len(entry) > 3}):
    key.update(path + b"\0" + content(os.path.join(top, path)))
key.update(run("rustc", "-vV"))
for name in sys.argv[1:split]:
    key.update(content(name.encode()))
for word in sys.argv[split + 1 :]:
    key.update(b"\0" + word.encode())
for name, value in sorted(os.environ.items()):
    if name.startswith(("RUST", "CARGO_", "NEXTEST_")) and name != "CARGO_MAKEFLAGS":
        key.update(("\0" + name + "=" + value).encode())
print(key.hexdigest())
'

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
        out="${out:+$out,}$name"
    done
    printf '%s' "$out"
}

# Optional trailing `--features <list>` on check, test, and lint. The Work Order
# names the exact list; the delegate still composes no flags of its own. Any
# other leftover argument is a usage error rather than something to pass through.
# The caller sets PKG before calling this.
FEATURE_FLAGS=()
take_features() {
    if [[ $# -gt 0 && "$1" == "--features" && -n "${2:-}" ]]; then
        FEATURE_FLAGS=(--features "$(qualify_features "$PKG" "$2")")
        shift 2
    fi
    if [[ $# -ne 0 ]]; then
        usage
        exit 2
    fi
}

# A flag, not an env prefix, so a call stays one plain command that the
# settings.json allow rule matches.
if [[ "${1:-}" == "--session-dir" ]]; then
    export PLAN_DELEGATE_SESSION_DIR="${2:?verify.sh --session-dir <dir> <command> ...}"
    shift 2
fi

CMD="${1:-}"
if [[ -z "$CMD" ]]; then
    usage
    exit 2
fi
shift

if [[ "$CMD" == example-test ]]; then
    echo 'verify.sh: example-test is removed: examples carry no tests (user, 2026-10-04).' >&2
    exit 2
fi

# --no-cache may sit anywhere after the subcommand; nothing below sees it.
NO_CACHE=0
ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--no-cache" ]]; then
        NO_CACHE=1
    else
        ARGS+=("$arg")
    fi
done
set -- "${ARGS[@]}"

# Every step run() runs below carries this call's id, which buildlog joins to
# the call record note_event writes.
export BUILDLOG_CALLER=verify
export BUILDLOG_CALL_ID="v$(date -u +%Y%m%dT%H%M%S)-$$-$RANDOM"

# Pass records (see the header): on for `test` and `lint` in a
# delegate session. --no-cache turns off the lookup; its result is still
# recorded, so a flake hunt that passes clears a recorded lint failure.
CACHE_SESSION_DIR="${PLAN_DELEGATE_BOARD_DIR:-${PLAN_DELEGATE_SESSION_DIR:-}}"
CACHE_DIR=""
if [[ -n "${CACHE_SESSION_DIR}" && ( "$CMD" == test || "$CMD" == lint ) ]]; then
    CACHE_DIR="${CACHE_SESSION_DIR}/verify_cache"
fi
RUN_LOG=""
RUN_KEY=""
RUN_STARTED=0
CALL_NOTED=0
TOKEN_WAIT_S=0
LOOKUP_KEY=""
LOOKUP_STATUS=0
EXIT_STATUS=0
FILTER_RUN=0
if [[ "$CMD" == test && " ${ARGS[*]} " == *" --filter "* ]]; then
    FILTER_RUN=1
fi

tree_key() {
    local words=("$CMD" "${ARGS[@]}") names=() name
    if [[ "$CMD" == lint && ${#ARGS[@]} -eq 1 ]]; then
        words=(lint)
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
    "$PY" -c "$TREE_KEY_PY" "${BASH_SOURCE[0]}" "$HOME/.claude/scripts/lint/invoke.sh" \
        "${LINT_CONFIG_FILE:-$HOME/.claude/config/lint.conf}" -- "${words[@]}" \
        2>/dev/null || true
}

# note_event OUTCOME WAIT WALL BUILD SAVED STATUS: this call's one record in
# the build log (see the header), the first time it is called. OUTCOME is ran,
# failed, interrupted, reused or replayed; an empty STATUS or BUILD is unknown.
# record.py adds the repo, worktree, branch and commit. A record that cannot be
# written is lost, never the result.
note_event() {
    [[ "${CALL_NOTED}" -eq 0 ]] || return 0
    CALL_NOTED=1
    [[ -f "${BUILDLOG_RECORD:-}" ]] || return 0
    local outcome=$1 wait=$2 wall=$3 build=$4 saved=$5 status=$6 cached=0
    if [[ -n "${CACHE_DIR}" ]]; then
        cached=1
    fi
    local mem_kills=0
    if [[ -n "${MEM_KILL_FILE:-}" && -f "$MEM_KILL_FILE" ]]; then
        mem_kills=$(wc -c < "$MEM_KILL_FILE")
    fi
    local mem_kill_stopped=0
    if [[ -n "${MEM_KILL_FILE:-}" && -f "${MEM_KILL_FILE}.stopped" ]]; then
        mem_kill_stopped=1
    fi
    BUILDLOG_MEM_KILLS="$mem_kills" BUILDLOG_MEM_KILL_STOPPED="$mem_kill_stopped" BUILDLOG_TOKEN_WAIT_S="${TOKEN_WAIT_S:-0}" "$PY" "$BUILDLOG_RECORD" call "$outcome" "$status" "$cached" "$wait" "$wall" \
        "$build" "$saved" "${SECONDS}" "$CMD" ${ARGS[@]+"${ARGS[@]}"} \
        </dev/null >/dev/null 2>&1 || true
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
    if [[ -f "${record}.pass" ]]; then
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

# Records a pass, or a lint failure, under the tree as the run left it (lint's
# rewrites are part of the result), before the token is released so a peer
# queued on the same call finds it. A record replaces the other kind on the
# same tree. Fails when there is nothing to record.
cache_record() {
    local kind=$1 repeat=$2 key verb=passed other=fail
    key="$(tree_key)"
    if [[ -z "${key}" || ( "$CMD" == test && "${key}" != "${RUN_KEY}" ) ]]; then
        return 1
    fi
    if [[ "${kind}" == fail ]]; then
        verb="failed with status ${EXIT_STATUS}" other=pass
    fi
    mv -f "${RUN_LOG}" "${CACHE_DIR}/${key}.log"
    printf '`verify.sh %s` %s %s, run by %s\nsaved_s=%d\nstatus=%d\n' \
        "$CMD${ARGS[*]:+ ${ARGS[*]}}" "${verb}" "$(date '+%Y-%m-%d %H:%M:%S')" \
        "${PLAN_DELEGATE_TEAM_ROLE:-the unit director}" "${repeat}" "${EXIT_STATUS}" \
        > "${CACHE_DIR}/${key}.${kind}.tmp"
    mv -f "${CACHE_DIR}/${key}.${kind}.tmp" "${CACHE_DIR}/${key}.${kind}"
    rm -f "${CACHE_DIR}/${key}.${other}"
}

# A failed lint is the tree's own answer unless the run never reached a
# verdict: a usage or tooling error (2), the sandbox (3), or a kill.
lint_failure_is_the_tree() {
    [[ "$CMD" == lint ]] || return 1
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

if cache_lookup; then
    exit "${LOOKUP_STATUS}"
fi

# Open a progress window for the duration of this run when a delegate session is
# in scope. This is what the unit director's progress header reports against
# while it runs verification itself: an activity, not a pass, so findings.py
# never counts it toward convergence. The launcher-owns-its-own-pass
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
    TOKEN_WAIT_STARTED=$SECONDS
    if bash "${BOARD_HELPER}" acquire "${BOARD_DIR}" "${BOARD_SLOT}" cargo \
        --pid $$ --hold 3600 --wait 1800 >/dev/null 2>&1; then
        TOKEN_HELD=1
    else
        echo "verify.sh: waited for the cargo token and did not get it." >&2
        exit 1
    fi
    TOKEN_WAIT_S=$(( SECONDS - TOKEN_WAIT_STARTED ))
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

def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        return subprocess.check_output(["ps", "-p", str(pid), "-o", "lstart="],
                                       text=True, stderr=subprocess.DEVNULL).strip()

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
    sys.exit(os.waitstatus_to_exitcode(status))

os.setsid()
with (lock.parent / "cargo.guard").open("a+") as guard:
    fcntl.flock(guard, fcntl.LOCK_EX)
    if (lock / "holder_pid").read_text() != holder:
        sys.exit(125)
    record = lock / f"owned.{os.getpid()}.tmp"
    record.write_text(json.dumps({"group": os.getpid(), "start": identity(os.getpid())}))
    os.replace(record, lock / "owned")
    fcntl.flock(guard, fcntl.LOCK_UN)

sys.exit(subprocess.call(command))
'

# Preserve the shared invocation policy (including systemd scopes and its
# fallback), changing only the command it launches while this token is held.
eval "$(declare -f buildlog_exec | sed '1s/buildlog_exec/buildlog_exec_unowned/')"
buildlog_exec() {
    if [[ "${TOKEN_HELD}" -eq 1 ]]; then
        buildlog_exec_unowned python3 -c "$STEP_OWNER_PY" \
            "${BOARD_DIR}/locks/cargo.d" "$$" "$@"
    else
        buildlog_exec_unowned "$@"
    fi
}

release_token() {
    [[ "${TOKEN_HELD}" -eq 1 ]] || return 0
    local release_status=0
    bash "${BOARD_HELPER}" release "${BOARD_DIR}" "${BOARD_SLOT}" cargo --pid $$ >/dev/null \
        || release_status=$?
    # An expired hold may be reclaimed while this step is still running. The
    # new holder owns the token; the old step's result remains authoritative.
    (( release_status == 0 || release_status == 3 )) || return "$release_status"
    TOKEN_HELD=0
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
if [[ -n "${CACHE_DIR}" ]] && mkdir -p "${CACHE_DIR}"; then
    RUN_KEY="${LOOKUP_KEY}"
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
    finish_run "${status}" || true
    # finish_run notes only a run with a record log; every other call is noted
    # here (note_event notes a call once). Its build time is unknown.
    case "${status}" in
        error) outcome=failed ;;
        interrupted) outcome=interrupted code="" ;;
    esac
    note_event "${outcome}" "${RUN_STARTED}" $(( SECONDS - RUN_STARTED )) "" 0 "${code}" || true
    [[ -z "${MEM_KILL_FILE:-}" ]] || rm -f "$MEM_KILL_FILE"
    [[ -z "${MEM_KILL_FILE:-}" ]] || rm -f "${MEM_KILL_FILE}.stopped"
    if [[ "${ACTIVITY_ACTIVE}" -eq 1 ]]; then
        finish_activity "${status}"
    fi
    if ! release_token && (( EXIT_STATUS == 0 )); then
        EXIT_STATUS=1
    fi
}
# EXIT alone would report success for a failed cargo run, so branch on the
# status the trap receives; a lint failure record keeps it.
trap 'EXIT_STATUS=$?; [[ ${EXIT_STATUS} -eq 0 ]] && verify_cleanup completed || verify_cleanup error; exit "${EXIT_STATUS}"' EXIT
verify_interrupted() {
    trap - EXIT INT TERM HUP
    EXIT_STATUS=$((128 + $1))
    verify_cleanup interrupted
    exit "$EXIT_STATUS"
}
trap 'verify_interrupted 2' INT
trap 'verify_interrupted 15' TERM
trap 'verify_interrupted 1' HUP

case "$CMD" in
    check)
        PKG="${1:?verify.sh check <package>}"
        shift
        take_features "$@"
        require_member "$PKG"
        run cargo check --workspace --lib --bins "${FEATURE_FLAGS[@]}"
        ;;
    test)
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
            run_nextest --no-fail-fast --workspace --test "$TARGET" \
                -E "package($PKG)" "${FEATURE_FLAGS[@]}"
        else
            TEST_FILTER="package($PKG)"
        fi
        if [[ -z "$TARGET" ]]; then
            # Build only the targets this package's tests live in
            # (TEST_TARGETS_PY): --lib, which cannot be narrowed to one member
            # under --workspace and is left out for a package without a lib,
            # and the package's own bins and test-enabled targets. --bins
            # --tests linked every member's test executables, 78 in hana, to
            # run one package's; this links 4 (2026-10-04). The integration
            # tests stay in, matching what the lint half compiles under
            # clippy: without them a phase could lint an integration test,
            # pass its gate, and checkpoint without ever running it.
            take_test_targets
            run_nextest --no-fail-fast --workspace "${TEST_SELECTION[@]}" \
                -E "$TEST_FILTER" "${FEATURE_FLAGS[@]}"
        fi
        ;;
    lint)
        PKG="${1:?verify.sh lint <package>}"
        shift
        take_features "$@"
        require_member "$PKG"
        # Order: mend rewrites first, fmt formats what mend wrote, then clippy
        # and rustdoc read the settled tree. Every stage is gated by lint.conf.
        # mend is the one stage below `final` that passes --all-targets: it
        # needs the test and example targets to see every import and
        # visibility site. It covers the workspace like the other stages, so
        # --fix can also rewrite a member the phase did not edit, when this
        # phase's change left an item there unused or over-visible.
        if lint_config_enabled mend; then
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
        else
            lint_config_skip_notice mend "cargo mend --all-targets --workspace --fix"
        fi
        # fmt compiles nothing, so it stays on the phase's own work: this
        # package and every member that differs from HEAD once mend has run.
        # Covering them all is what lets one lint record answer for each
        # package (tree_key).
        FMT_SCOPE=(-p "$PKG")
        FMT_MEMBERS="$(cargo metadata --no-deps --format-version 1 \
            | "$PY" -c "$CHANGED_MEMBERS_PY" "")"
        while IFS= read -r member; do
            if [[ -n "$member" && "$member" != "$PKG" ]]; then
                FMT_SCOPE+=(-p "$member")
            fi
        done <<< "$FMT_MEMBERS"
        fmt_cargo "${FMT_SCOPE[@]}"
        invoke_clippy --workspace --lib --bins --tests "${FEATURE_FLAGS[@]}"
        # Found after mend, so a member its --fix rewrote is documented too.
        FEATURE_MEMBER=""
        if [[ ${#FEATURE_FLAGS[@]} -gt 0 ]]; then
            FEATURE_MEMBER="$PKG"
        fi
        DOC_MEMBERS="$(cargo metadata --no-deps --format-version 1 \
            | "$PY" -c "$CHANGED_MEMBERS_PY" "$FEATURE_MEMBER")"
        DOC_SCOPE=()
        while IFS= read -r member; do
            if [[ -n "$member" ]]; then
                DOC_SCOPE+=(-p "$member")
            fi
        done <<< "$DOC_MEMBERS"
        if [[ ${#DOC_SCOPE[@]} -gt 0 ]]; then
            invoke_doc "${DOC_SCOPE[@]}" "${FEATURE_FLAGS[@]}"
        else
            echo "verify.sh: no workspace member differs from HEAD; rustdoc skipped"
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
        # clippy, not check: `lint` never sees an example, so a check here let
        # lint errors in a changed example reach the merge branch twice.
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
        fmt_cargo --check
        run cargo check --workspace --all-targets
        run_nextest --workspace
        ;;
    *)
        usage
        exit 2
        ;;
esac
