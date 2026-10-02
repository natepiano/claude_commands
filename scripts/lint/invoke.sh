#!/usr/bin/env bash
# invoke.sh — sourced library holding every cargo invocation policy: canonical
# flags, config/lint.conf gating, and macOS sandbox-failure detection. This is the single bottom layer. The lint CLI in
# this directory is the public entry point (shell aliases, validate_ci.sh,
# the fix pipeline, pre_release_checks.sh, /clippy); delegate/verify.sh sources this
# file directly and adds only its per-package scope rules on top. Nothing
# outside this directory composes policy flags like -D warnings or the nextest
# profile.
#
# Callers own scope (--workspace vs -p <pkg>, target selection); this file
# owns how each tool always runs once scoped.

[[ -n "${LINT_INVOKE_SOURCED:-}" ]] && return 0
LINT_INVOKE_SOURCED=1

# macOS sandboxes cannot nest. A dependency whose build script shells out to
# Swift Package Manager — apple-cf, apple-metal, screencapturekit, anything
# wrapping a macOS framework — makes SwiftPM call sandbox-exec, which fails
# inside the Claude Code sandbox. The panic names Swift and never names the
# sandbox, so it reads like a broken dependency and costs a round trip to
# diagnose. Name it here instead of leaving it to be rediscovered.
SANDBOX_SIGNATURE='sandbox_apply: Operation not permitted'

# Lint policy. Missing reader means every check runs — a caller must never be
# silently under-verified because a config script moved.
LINT_CONFIG_READER="${LINT_CONFIG_READER:-$HOME/.claude/scripts/lint/lint_config.sh}"
if [[ -f "$LINT_CONFIG_READER" ]]; then
    # shellcheck source=/dev/null
    source "$LINT_CONFIG_READER"
else
    echo "invoke.sh: $LINT_CONFIG_READER not found — running every check" >&2
    lint_config_enabled() { return 0; }
    lint_config_skip_notice() { :; }
fi

# natedev's machine-wide pool of build slots (/etc/nixos
# modules/linux/jobserver.nix). The session environment names it, but a shell
# started before the pool existed does not, so join it here too. The Mac has no
# /dev/steve and skips this.
if [[ -z "${CARGO_MAKEFLAGS:-}" && -c /dev/steve && -r /dev/steve && -w /dev/steve ]]; then
    export CARGO_MAKEFLAGS="--jobserver-auth=fifo:/dev/steve"
fi

# The build log (~/.claude/scripts/buildlog): every step run() runs leaves one
# record — what ran, where, for whom, how long, its exit status, and from the
# tee'd output its build time, diagnostics and test results — in
# ~/.local/state/buildlog, which `buildlog query` answers SQL over.
#
# The hook adds one wait a build can see. buildlog_begin first has record.py
# print the tree key (treekey.py) of the files the step starts on, before the
# clock starts: 57 ms median per step on a hana worktree (natedev, 2026-10-02,
# load average 20 to 30; 15 ms starting python, 21 ms imports, 10 ms git). The
# recorder takes the key again after the step and records it only when the
# two agree, so a step during which the files changed has none; port-lint
# reuses a step by that key. Then buildlog_begin reads the clock
# (EPOCHREALTIME without a fork; perl on the Mac's bash 3.2); buildlog_end
# renames the log out of the way, since run() reuses its name for the next
# step, and starts record.py detached, with no descriptor of ours, to read it.
# The key goes to it in BUILDLOG_TREE_START, not as an argument: a shell that
# sourced an older copy of this file still calls record.py step with today's
# arguments.
# The recorder ignores SIGHUP from birth: a terminal that closes as the last
# step ends (a pty wrapper, a closed pane) killed it before its own setsid.
# On natedev buildlog_exec also runs the step in its own systemd scope, whose
# cgroup memory.peak is the step's peak memory, page cache included. That moves
# the step from the caller's scope (a terminal's, with its own OOM policy) to
# app.slice. Per step, starting the recorder costs about 2 ms and the scope 8
# ms more (natedev, 2026-10-02); BUILDLOG_SCOPE=0 turns the scope off,
# BUILDLOG_OFF=1 the whole hook. Callers run with set -euo pipefail, so none
# of this may fail, print, or change a status: the begin and end calls sit
# behind `|| true`, and the step's status is the step's own.
#
# fd 3 carries the step's stderr past systemd-run, whose own complaints go to
# /dev/null. --expand-environment=no keeps the argv as given: by default
# systemd-run rewrites $VAR, ${VAR} and $$ in it, as ExecStart= would. The
# marker file is written inside the scope just before the step starts, so its
# absence afterwards proves the scope never ran the step (no user manager, a
# sandbox without its socket, a systemd older than 254 that rejects the
# option), and run() runs it plainly.
BUILDLOG_RECORD="$HOME/.claude/scripts/buildlog/record.py"
BUILDLOG_SCOPE_SH='exec 2>&3 3>&-; { : > "$0"; } 2>/dev/null || exit 125; "$@"; s=$?; cat "/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)/memory.peak" > "$0" 2>/dev/null; exit $s'

buildlog_now() {
    if [[ -n "${EPOCHREALTIME:-}" ]]; then
        BUILDLOG_NOW="${EPOCHREALTIME/,/.}"
    elif [[ -x /usr/bin/perl ]]; then
        BUILDLOG_NOW="$(/usr/bin/perl -MTime::HiRes=time -e 'printf "%.6f", time' 2>/dev/null)"
    fi
    [[ -n "${BUILDLOG_NOW:-}" ]] || BUILDLOG_NOW="$(date +%s 2>/dev/null)"
}

# buildlog_begin ARGV...
buildlog_begin() {
    BUILDLOG_START="" BUILDLOG_PEAK="" BUILDLOG_NOW="" BUILDLOG_TREE=""
    [[ "${BUILDLOG_OFF:-0}" != 1 && -f "$BUILDLOG_RECORD" ]] || return 0
    BUILDLOG_TREE="$("$HOME/.claude/scripts/lib/py" "$BUILDLOG_RECORD" key ${1+"$@"} </dev/null 2>/dev/null)" \
        || BUILDLOG_TREE=""
    buildlog_now
    BUILDLOG_START="${BUILDLOG_NOW:-}"
    if [[ "${BUILDLOG_SCOPE:-1}" != 0 && -S "${XDG_RUNTIME_DIR:-/nonexistent}/systemd/private" \
          && -r /proc/self/cgroup ]] && command -v systemd-run >/dev/null 2>&1; then
        BUILDLOG_PEAK="${TMPDIR:-/tmp}/buildlog.$$.$RANDOM.peak"
    fi
}

buildlog_exec() {
    if [[ -z "${BUILDLOG_PEAK:-}" ]]; then
        "$@"
        return
    fi
    local status=0
    systemd-run --user --scope --quiet --collect --expand-environment=no -- \
        /bin/sh -c "$BUILDLOG_SCOPE_SH" "$BUILDLOG_PEAK" "$@" 3>&2 2>/dev/null
    status=$?
    if [[ -e "$BUILDLOG_PEAK" ]]; then
        return $status
    fi
    "$@"
}

# buildlog_end STATUS LOG TTY ARGV...
buildlog_end() {
    [[ -n "${BUILDLOG_START:-}" ]] || return 0
    local status=$1 log=$2 tty=$3 held=""
    shift 3
    BUILDLOG_NOW=""
    buildlog_now
    if [[ -n "$log" && -f "$log" ]]; then
        held="${TMPDIR:-/tmp}/buildlog.$$.$RANDOM$RANDOM.log"
        mv -f "$log" "$held" 2>/dev/null || held=""
    fi
    if [[ "${BUILDLOG_SYNC:-0}" == 1 ]]; then
        BUILDLOG_TREE_START="${BUILDLOG_TREE:-}" "$HOME/.claude/scripts/lib/py" "$BUILDLOG_RECORD" step \
            "$status" "$BUILDLOG_START" "${BUILDLOG_NOW:-}" "$tty" "$held" "${BUILDLOG_PEAK:-}" ${1+"$@"} \
            </dev/null >/dev/null 2>&1
    else
        ( trap '' HUP
          BUILDLOG_TREE_START="${BUILDLOG_TREE:-}" "$HOME/.claude/scripts/lib/py" "$BUILDLOG_RECORD" step \
            "$status" "$BUILDLOG_START" "${BUILDLOG_NOW:-}" "$tty" "$held" "${BUILDLOG_PEAK:-}" ${1+"$@"} \
            </dev/null >/dev/null 2>&1 & )
    fi
    BUILDLOG_START="" BUILDLOG_TREE=""
    return 0
}

run() {
    printf '+ %s\n' "$*"
    buildlog_begin "$@" || true
    # A terminal on the other end means a human is watching, so run straight
    # through and keep the colors. The tee below is what costs them: cargo and
    # mend see a pipe, not a tty, and drop their ANSI. It buys only the sandbox
    # detection underneath, which can fire only inside Claude Code, where
    # stdout is never a tty.
    if [[ -t 1 ]]; then
        local tty_status=0
        set +e
        buildlog_exec "$@"
        tty_status=$?
        set -e
        buildlog_end "$tty_status" "" 1 "$@" || true
        return $tty_status
    fi
    local log="${TMPDIR:-/tmp}/lint_invoke.$$.log"
    local status=0
    # tee keeps output streaming: heartbeat_watch.sh digests the agent log to
    # prove a delegate is alive, so buffering a long build looks like a hang.
    set +e
    buildlog_exec "$@" 2>&1 | tee "$log"
    status=${PIPESTATUS[0]}
    set -e
    if [[ $status -ne 0 ]] && grep -q "$SANDBOX_SIGNATURE" "$log"; then
        buildlog_end "$status" "$log" 0 "$@" || true
        rm -f "$log"
        cat >&2 <<'EOF'

THIS IS A SANDBOX FAILURE, NOT A TEST FAILURE.

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
    buildlog_end "$status" "$log" 0 "$@" || true
    rm -f "$log"
    return $status
}

have_nextest() {
    cargo nextest --version >/dev/null 2>&1
}

require_nextest() {
    if ! have_nextest; then
        echo "cargo nextest is required" >&2
        exit 2
    fi
}

run_nextest() {
    require_nextest
    run cargo nextest run "$@"
}

# The rustfmt configuration in these workspaces uses nightly-only options.
# Formatting with stable could accept output that nightly rejects, so it is an
# error here.
fmt_cargo() {
    if ! lint_config_enabled fmt; then
        lint_config_skip_notice fmt "cargo +nightly fmt $*"
        return 0
    fi
    if ! cargo +nightly fmt --version >/dev/null 2>&1; then
        echo "cargo +nightly fmt is required" >&2
        exit 2
    fi
    run cargo +nightly fmt "$@"
}

# Clippy with warnings denied. Callers pass full cargo scope args (--workspace
# --all-targets, -p <pkg> --lib --bins --tests, --target <triple>, ...) and,
# after `--`, extra clippy lints appended after the always-on -D warnings.
# bash 3.2 with set -u rejects "${arr[@]}" on an empty array, hence the
# ${arr[@]+...} expansions.
invoke_clippy() {
    if ! lint_config_enabled clippy; then
        lint_config_skip_notice clippy "cargo clippy $*"
        return 0
    fi
    local -a cargo_args=() clippy_args=()
    local seen_sep=0 arg
    for arg in "$@"; do
        if [[ "$arg" == "--" && $seen_sep -eq 0 ]]; then
            seen_sep=1
            continue
        fi
        if [[ $seen_sep -eq 0 ]]; then
            cargo_args+=("$arg")
        else
            clippy_args+=("$arg")
        fi
    done
    run cargo clippy ${cargo_args[@]+"${cargo_args[@]}"} -- -D warnings \
        ${clippy_args[@]+"${clippy_args[@]}"}
}

invoke_mend() {
    if ! lint_config_enabled mend; then
        lint_config_skip_notice mend "cargo mend --workspace"
        return 0
    fi
    # RUSTC_WRAPPER cleared: a compiler cache replays cached output instead of
    # running rustc, which suppresses the diagnostics mend analyzes
    # Scope comes from the caller (the lint CLI resolves it, or a caller like
    # the fix pipeline passes --manifest-path). Forcing --workspace here silently
    # overrode both: a per-project run linted the whole workspace instead.
    run env RUSTC_WRAPPER= cargo mend --all-targets "$@"
}

# Scope comes from the caller, like mend and clippy. --workspace used to be
# hardcoded here, which made every dev-time doc lint a full-workspace rustdoc
# run no matter how little changed. Narrowing is safe: rustdoc resolves
# intra-doc links per crate against that crate's own dependency metadata, so
# which other members share the invocation never changes a given crate's
# warnings. The workspace-wide sweep it used to provide incidentally now runs
# once at the push gate (validate_ci.sh), where it belongs.
#
# Features come from the caller too. A hardcoded --all-features made every
# dev-loop doc run resolve a feature set that no check, clippy or test run
# shares, and so compile its own copy of the dependency tree. The push gate
# passes --all-features itself, where full coverage is the point.
#
# At most LINT_DOC_JOBS (default 8) processes at once, for every caller. cargo
# defaults to one per core and starts one rustdoc per member at 1 to 5 GB each:
# overlapping workspace runs had 32 going (34 GB) when natedev ran out of memory
# on 2026-09-30. The cap also paces any dependency compile inside the same run.
# cargo ignores --jobs once it joins a shared pool (natedev's, above), and the
# pool alone would allow 32 again, so the doc run leaves it for a private 8.
# LINT_-prefixed like the sweep knobs below.
invoke_doc() {
    if ! lint_config_enabled doc; then
        lint_config_skip_notice doc "cargo doc $*"
        return 0
    fi
    run env -u CARGO_MAKEFLAGS -u MAKEFLAGS -u MFLAGS RUSTDOCFLAGS="-D warnings" \
        cargo doc --no-deps --jobs "${LINT_DOC_JOBS:-8}" "$@"
}

# Keep a project's target directory under a size budget after cargo-port's
# per-save lint run, so the cache stays warm without growing without limit.
# Runs from the project root (cargo-port's cwd) and never resolves package
# scope: size is a property of the target directory, not of which members
# changed. sweep.py holds the policy (least recently used build units and
# incremental dirs go first, cargo's build locks taken without waiting) and
# the measurements behind it. The knobs are LINT_SWEEP_BUDGET_GIB (default 96)
# and LINT_SWEEP_DOC_INDEX_MIB (default 250, the doc/ cross-crate index past
# which the whole doc tree goes, because rustdoc's peak memory tracks that
# index rather than the crate it documents). Both are LINT_-prefixed on
# purpose: sccache hashes every CARGO_* variable into its cache key.
# --dry-run reports what would go and removes nothing.
invoke_sweep() {
    if ! lint_config_enabled sweep; then
        lint_config_skip_notice sweep "lint sweep"
        return 0
    fi
    run "$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lint/sweep.py" "$@"
}
