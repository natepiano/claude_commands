#!/usr/bin/env bash
# invoke.sh — sourced library holding every cargo invocation policy: canonical
# flags, config/lint.conf gating, and macOS sandbox-failure detection. This is the single bottom layer. The lint CLI in
# this directory is the public entry point (shell aliases, validate_ci.sh,
# clean-fix, pre_release_checks.sh, /clippy); delegate/verify.sh sources this
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

run() {
    printf '+ %s\n' "$*"
    # A terminal on the other end means a human is watching, so run straight
    # through and keep the colors. The tee below is what costs them: cargo and
    # mend see a pipe, not a tty, and drop their ANSI. It buys only the sandbox
    # detection underneath, which can fire only inside Claude Code, where
    # stdout is never a tty.
    if [[ -t 1 ]]; then
        local tty_status=0
        set +e
        "$@"
        tty_status=$?
        set -e
        return $tty_status
    fi
    local log="${TMPDIR:-/tmp}/lint_invoke.$$.log"
    local status=0
    # tee keeps output streaming: heartbeat_watch.sh digests the agent log to
    # prove a delegate is alive, so buffering a long build looks like a hang.
    set +e
    "$@" 2>&1 | tee "$log"
    status=${PIPESTATUS[0]}
    set -e
    if [[ $status -ne 0 ]] && grep -q "$SANDBOX_SIGNATURE" "$log"; then
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
    # clean-fix passes --manifest-path). Forcing --workspace here silently
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
invoke_doc() {
    if ! lint_config_enabled doc; then
        lint_config_skip_notice doc "cargo doc $*"
        return 0
    fi
    run env RUSTDOCFLAGS="-D warnings" cargo doc --no-deps --all-features "$@"
}
