#!/usr/bin/env bash
# verify.sh — The only build/test/lint commands a delegate may run.
#
# Work Orders list exact invocations of this script; the delegate composes no
# cargo flags and makes no scope choices. Cargo's default target selection
# compiles examples, so every dev-loop subcommand pins explicit targets
# (--lib/--bins/--tests). Nothing below `final` compiles examples or uses
# --all-targets (mend excepted, see `lint`); `final` is the plan-final full
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
# with it. Only the compile covers the workspace.
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
# that differs from HEAD, so one lint record answers for each package. The same
# call on the same tree then prints that record and its log and exits 0
# without the cargo token: seats re-ran unchanged trees 416
# times in the week to 2026-10-01. A failed `lint` is recorded the same way
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
#                                          (workspace lib + bins + tests built)
#   verify.sh test <package> <int_test>    one named integration test target,
#                                          for re-running it alone
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
#   verify.sh example-test <package> <name>
#                                          test one example (only when the
#                                          example contains unit tests)
#   verify.sh final                        full workspace gate (unit director only)
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
require_member() {
    if ! cargo metadata --no-deps --format-version 1 | "$PY" -c "$MEMBER_PY" "$1"; then
        exit 2
    fi
}

example_features() {
    cargo metadata --no-deps --format-version 1 \
        | "$PY" -c "$EXAMPLE_FEATURES_PY" "$1" "$2"
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

CMD="${1:-}"
if [[ -z "$CMD" ]]; then
    usage
    exit 2
fi
shift

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
LOOKUP_KEY=""
LOOKUP_STATUS=0
EXIT_STATUS=0

tree_key() {
    local words=("$CMD" "${ARGS[@]}")
    if [[ "$CMD" == lint && ${#ARGS[@]} -eq 1 ]]; then
        words=(lint)
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
    "$PY" "$BUILDLOG_RECORD" call "$outcome" "$status" "$cached" "$wait" "$wall" \
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
# verdict: a usage or tooling error (2), the sandbox (3), or a kill (a status
# past 128, or a compiler SIGKILL in the log, as earlyoom deals out).
lint_failure_is_the_tree() {
    [[ "$CMD" == lint ]] || return 1
    (( EXIT_STATUS != 2 && EXIT_STATUS != 3 && EXIT_STATUS < 128 )) || return 1
    ! grep -qaE 'SIGKILL|signal: 9' "${RUN_LOG}"
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

# Again with the token held: a peer running this same call on this tree held
# the token until its result was recorded.
if cache_lookup; then
    release_token
    exit "${LOOKUP_STATUS}"
fi
RUN_STARTED=${SECONDS}
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
    if [[ "${ACTIVITY_ACTIVE}" -eq 1 ]]; then
        finish_activity "${status}"
    fi
    release_token
}
# EXIT alone would report success for a failed cargo run, so branch on the
# status the trap receives; a lint failure record keeps it.
trap 'EXIT_STATUS=$?; [[ ${EXIT_STATUS} -eq 0 ]] && verify_cleanup completed || verify_cleanup error' EXIT
trap 'verify_cleanup interrupted' INT TERM

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
        if [[ $# -gt 0 && "$1" != "--features" ]]; then
            TARGET="$1"
            shift
        fi
        take_features "$@"
        require_member "$PKG"
        # --no-fail-fast: nextest cancels every remaining test after the first
        # failure, so one broken test silently hides the rest of the suite. A
        # phase gate has to report the whole result, not the first stop.
        # The build covers the workspace (see the header); -E runs only this
        # package's tests.
        if [[ -n "$TARGET" ]]; then
            # Integration test target names are unique across the workspace,
            # so --test builds just that binary, under the workspace's
            # feature resolution.
            run_nextest --no-fail-fast --workspace --test "$TARGET" \
                -E "package($PKG)" "${FEATURE_FLAGS[@]}"
        else
            # --tests adds the integration targets, matching what the lint
            # half already compiles under clippy. Without it a phase could
            # lint an integration test, pass its gate, and checkpoint without
            # ever running it. The measured runtime cost is seconds, because
            # the expensive compile-fail suites are #[ignore]d and
            # .config/nextest.toml keeps tool_id_boundary's downstream cases
            # out of the default profile.
            run_nextest --no-fail-fast --workspace --lib --bins --tests \
                -E "package($PKG)" "${FEATURE_FLAGS[@]}"
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
        #
        # A clippy-linked cargo-mend (invoke.sh, mend_clippy_probe) runs
        # clippy's lints in its own compile, and then stands in for the clippy
        # step, so the workspace compiles once. fmt moves first, so those lints
        # read the formatted tree, as stock clippy did after fmt. A --fix that
        # wrote prints a report from before its last rewrite: fmt runs again
        # and a read-only mend (cargo replays its cached diagnostics) reports
        # on the settled tree. Lint then fails on any warning left, all targets
        # included, where clippy failed on any in --lib --bins --tests; a report
        # that shows no warning count falls back to stock clippy.
        MEND_COVERS_CLIPPY=0
        if lint_config_enabled mend && lint_config_enabled clippy; then
            mend_clippy_probe
            if [[ "$MEND_CLIPPY_STATE" == active ]]; then
                MEND_COVERS_CLIPPY=1
            fi
        fi
        # lint_mend [--fix]: one mend run. Sets MEND_APPLIED (1 when --fix
        # wrote) and MEND_WARNINGS (mend_clippy_warnings, empty when the report
        # shows no count). The log lives only as long as the run.
        lint_mend() {
            local log
            log="$(mktemp)"
            # pipefail is set, so a failing mend fails the pipeline; the log
            # only decides which message names the failure.
            if ! invoke_mend --workspace "$@" "${FEATURE_FLAGS[@]}" 2>&1 | tee "$log"; then
                if grep -qiE 'rolled back|revert' "$log"; then
                    echo "verify.sh: cargo mend --fix rolled its rewrites back; the tree reproduces it — run /mend_fix" >&2
                else
                    echo "verify.sh: cargo mend${*:+ $*} failed; see the output above" >&2
                fi
                rm -f "$log"
                exit 1
            fi
            MEND_APPLIED=0
            if grep -q '^mend: applied' "$log"; then
                MEND_APPLIED=1
            fi
            MEND_WARNINGS="$(mend_clippy_warnings "$log")" || MEND_WARNINGS=""
            rm -f "$log"
        }
        # fmt compiles nothing, so it stays on the phase's own work: this
        # package and every member that differs from HEAD once mend has run.
        # Covering them all is what lets one lint record answer for each
        # package (tree_key).
        lint_fmt() {
            local member
            FMT_SCOPE=(-p "$PKG")
            FMT_MEMBERS="$(cargo metadata --no-deps --format-version 1 \
                | "$PY" -c "$CHANGED_MEMBERS_PY" "")"
            while IFS= read -r member; do
                if [[ -n "$member" && "$member" != "$PKG" ]]; then
                    FMT_SCOPE+=(-p "$member")
                fi
            done <<< "$FMT_MEMBERS"
            fmt_cargo "${FMT_SCOPE[@]}"
        }
        if [[ $MEND_COVERS_CLIPPY -eq 1 ]]; then
            lint_fmt
        fi
        if lint_config_enabled mend; then
            lint_mend --fix
            if [[ $MEND_COVERS_CLIPPY -eq 1 && $MEND_APPLIED -eq 1 ]]; then
                lint_fmt
                lint_mend
            fi
        else
            lint_config_skip_notice mend "cargo mend --all-targets --workspace --fix"
        fi
        if [[ $MEND_COVERS_CLIPPY -eq 0 ]]; then
            lint_fmt
            invoke_clippy --workspace --lib --bins --tests "${FEATURE_FLAGS[@]}"
        elif [[ -z "$MEND_WARNINGS" ]]; then
            echo "verify.sh: cargo mend's report shows no warning count; running stock clippy"
            invoke_clippy --workspace --lib --bins --tests "${FEATURE_FLAGS[@]}"
        elif [[ "$MEND_WARNINGS" -gt 0 ]]; then
            echo "verify.sh: $MEND_WARNINGS compiler warnings remain, clippy's included (it ran inside cargo mend); lint denies warnings: fix the ones above" >&2
            exit 1
        else
            echo "verify.sh: clippy ran inside cargo mend's compile and left no warnings; the stock clippy step is skipped"
        fi
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
    example-test)
        PKG="${1:?verify.sh example-test <package> <name>}"
        NAME="${2:?verify.sh example-test <package> <name>}"
        FEATURES="$(example_features "$PKG" "$NAME")"
        if [[ -n "$FEATURES" ]]; then
            run_nextest --workspace --example "$NAME" -E "package($PKG)" \
                --features "$(qualify_features "$PKG" "$FEATURES")"
        else
            run_nextest --workspace --example "$NAME" -E "package($PKG)"
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
