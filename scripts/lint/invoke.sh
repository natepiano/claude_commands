#!/usr/bin/env bash
# invoke.sh — sourced library holding every cargo invocation policy: canonical
# flags, config/lint.conf gating, and macOS sandbox-failure detection.
# The lint CLI in this directory serves shell aliases, validate_ci.sh and
# pre_release_checks.sh. direct/verify.sh sources this file directly and adds
# only its per-package scope rules on top. Nothing
# outside this directory composes policy flags like -D warnings or the nextest
# profile.
#
# Callers own scope (--workspace vs -p <pkg>, target selection); this file
# owns how each tool always runs once scoped.
# invoke_clippy_fix applies fixes; fmt_cargo formats them before invoke_clippy
# judges the result. Worker verdict keys include this policy file.

if [[ -n "${TMPDIR:-}" && ! -d "$TMPDIR" ]]; then mkdir -p -- "$TMPDIR" || return; fi

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
# cgroup memory.peak and memory.pressure measure the step's peak memory and
# memory stalls. Peak memory includes page cache. The scope moves
# the step from the caller's scope (a terminal's, with its own OOM policy) to
# builds.slice. Per step, starting the recorder costs about 2 ms and the scope 8
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
# The scope raises oom_score_adj to 500, so earlyoom kills build steps before
# CI (100) and sessions (200), the largest first.
BUILDLOG_RECORD="$HOME/.claude/scripts/buildlog/record.py"
BUILDLOG_SCOPE_SH='exec 2>&3 3>&-; { : > "$0"; } 2>/dev/null || exit 125; cgroup="/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"; printf "%s\n" "$cgroup" > "$0.cgroup"; { echo 500 > /proc/self/oom_score_adj; } 2>/dev/null || true; "$@"; s=$?; cat "$cgroup/memory.peak" "$cgroup/memory.pressure" > "$0" 2>/dev/null; exit $s'
source "$(dirname "${BASH_SOURCE[0]}")/memory_gate.sh" || {
    printf 'invoke.sh: cannot load admission and hold checks; step refused\n' >&2
    return 2
}

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
    BUILDLOG_START="" BUILDLOG_PEAK="" BUILDLOG_NOW="" BUILDLOG_TREE="" BUILDLOG_MEM_WAIT_S=0
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

buildlog_step_compiles() {
    local arg
    for arg in "$@"; do
        [[ "$arg" == */sweep.py ]] && return 1
    done
    [[ "${1:-}" == taplo ]] && return 1
    [[ "${1:-}" == cargo ]] || return 0
    shift
    if [[ "${1:-}" == +* ]]; then
        shift
    fi
    [[ "${1:-}" == fmt ]] && return 1
    return 0
}

buildlog_exec() {
    if [[ -z "${BUILDLOG_PEAK:-}" ]]; then
        "$@"
        return
    fi
    local status=0
    systemd-run --user --scope --quiet --collect --expand-environment=no --slice=builds.slice -- \
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
        BUILDLOG_TREE_START="${BUILDLOG_TREE:-}" BUILDLOG_MEM_WAIT_S="${BUILDLOG_MEM_WAIT_S:-0}" "$HOME/.claude/scripts/lib/py" "$BUILDLOG_RECORD" step \
            "$status" "$BUILDLOG_START" "${BUILDLOG_NOW:-}" "$tty" "$held" "${BUILDLOG_PEAK:-}" ${1+"$@"} \
            </dev/null >/dev/null 2>&1
    else
        ( trap '' HUP
          BUILDLOG_TREE_START="${BUILDLOG_TREE:-}" BUILDLOG_MEM_WAIT_S="${BUILDLOG_MEM_WAIT_S:-0}" "$HOME/.claude/scripts/lib/py" "$BUILDLOG_RECORD" step \
            "$status" "$BUILDLOG_START" "${BUILDLOG_NOW:-}" "$tty" "$held" "${BUILDLOG_PEAK:-}" ${1+"$@"} \
            </dev/null >/dev/null 2>&1 & )
    fi
    BUILDLOG_START="" BUILDLOG_TREE=""
    return 0
}

buildlog_sample_anon() {
    local sidecar=$1 maximum=$2 parent=${3:-$$} scope="" line anon peak=0
    while :; do
        kill -0 "$parent" 2>/dev/null || return
        if [[ -r "$sidecar" ]]; then
            IFS= read -r scope < "$sidecar" || true
            if [[ -n "$scope" && -r "$scope/memory.stat" ]]; then
                while IFS= read -r line; do
                    if [[ "$line" == 'anon '* ]]; then
                        anon=${line#anon }
                        if [[ "$anon" =~ ^[0-9]+$ ]] && (( anon > peak )); then
                            peak=$anon
                            printf '%s\n' "$peak" > "$maximum"
                        fi
                        break
                    fi
                done < "$scope/memory.stat"
            fi
        fi
        sleep 1
    done
}

buildlog_finish_memory() {
    local status=$1 sampler_pid=$2
    if [[ -n "$sampler_pid" ]]; then
        kill "$sampler_pid" 2>/dev/null || true
        wait "$sampler_pid" 2>/dev/null || true
    fi
    buildlog_release_memory "$status"
    if [[ -n "${BUILDLOG_PEAK:-}" ]]; then
        rm -f "$BUILDLOG_PEAK.cgroup" || true
    fi
}

# run() returns this when LINT_QUEUE_LEAVE_HOOK (memory_gate.sh) ended the
# memory wait or natedev's hold wait: the step never started, holds no reservation, leaves no build
# log row, and BUILDLOG_MEM_WAIT_S
# keeps the time it waited. run_nextest passes it through. 75 is EX_TEMPFAIL;
# neither cargo nor nextest exits with it.
LINT_LEFT_QUEUE_STATUS=75

run_once() {
    if [[ -n "${BUILDLOG_CHECK_FAILED_FILE:-}" && -f "$BUILDLOG_CHECK_FAILED_FILE" ]]; then
        build_check_stop 2 'a prior admission or hold check failed'
        return 2
    fi
    printf '+ %s\n' "$*"
    if buildlog_step_compiles "$@"; then
        build_hold_wait_local || return $?
    fi
    buildlog_begin "$@" || true
    local sampler_pid="" parent_pid=$BASHPID status=0 memory_wait=0
    if buildlog_step_compiles "$@"; then
        while :; do
            buildlog_wait_for_memory "$@" || {
                status=$?
                buildlog_release_memory "$status"
                return "$status"
            }
            if [[ -n "${BUILDLOG_MEM_WAIT_SINCE:-}" ]]; then
                memory_wait=$BUILDLOG_MEM_WAIT_S
            else
                memory_wait=$(( memory_wait + BUILDLOG_MEM_WAIT_S ))
            fi
            if [[ "$BUILDLOG_MEM_OUTCOME" == LeftQueue ]]; then
                buildlog_release_memory 0
                BUILDLOG_MEM_WAIT_S=$memory_wait BUILDLOG_START="" BUILDLOG_TREE=""
                return "$LINT_LEFT_QUEUE_STATUS"
            fi
            if [[ "$BUILDLOG_MEM_OUTCOME" != Granted ]]; then
                buildlog_release_memory 2
                build_check_stop 2 'memory admission check did not allow the step'
                return 2
            fi
            if build_hold_check_local; then
                break
            else
                status=$?
            fi
            buildlog_release_memory 0
            if [[ "$status" != 10 ]]; then
                BUILDLOG_MEM_WAIT_S=$memory_wait
                return "$status"
            fi
            build_hold_wait_local || {
                status=$?
                BUILDLOG_MEM_WAIT_S=$memory_wait
                if [[ "$status" == "$LINT_LEFT_QUEUE_STATUS" ]]; then
                    BUILDLOG_START="" BUILDLOG_TREE=""
                fi
                return "$status"
            }
        done
        BUILDLOG_MEM_WAIT_S=$memory_wait
    fi
    if [[ -n "${LINT_NEXTEST_SHARE:-}" ]]; then
        nextest_share_claim "$@"
        set -- "${LINT_NEXTEST_SHARED_ARGV[@]}"
    fi
    if [[ -n "${BUILDLOG_MEM_RESERVATION:-}" && -n "${BUILDLOG_PEAK:-}" ]]; then
        buildlog_sample_anon "$BUILDLOG_PEAK.cgroup" "${BUILDLOG_MEM_RESERVATION%.reservation}.max" "$parent_pid" &
        sampler_pid=$!
    fi
    if [[ -n "${BUILDLOG_START:-}" ]]; then
        buildlog_now
        BUILDLOG_START="${BUILDLOG_NOW:-}"
    fi
    # A terminal on the other end means a human is watching, so run straight
    # through and keep the colors. The tee below is what costs them: cargo and
    # mend see a pipe, not a tty, and drop their ANSI. It buys only the sandbox
    # detection underneath, which can fire only inside Claude Code, where
    # stdout is never a tty.
    if [[ -t 1 ]]; then
        local tty_status=0
        set +e
        buildlog_exec "$@" 9>&-
        tty_status=$?
        set -e
        nextest_share_end
        buildlog_finish_memory "$tty_status" "$sampler_pid"
        buildlog_end "$tty_status" "" 1 "$@" || true
        sweep_after_step "$@" || true
        return $tty_status
    fi
    # $RANDOM too: a background sweep's run() shares this shell's $$.
    local log="${TMPDIR:-/tmp}/lint_invoke.$$.$RANDOM.log"
    # tee keeps output streaming: heartbeat_watch.sh digests the agent log to
    # prove a worker is alive, so buffering a long build looks like a hang.
    set +e
    buildlog_exec "$@" 9>&- 2>&1 | tee "$log"
    status=${PIPESTATUS[0]}
    set -e
    nextest_share_end
    buildlog_finish_memory "$status" "$sampler_pid"
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
    sweep_after_step "$@" || true
    return $status
}

run() {
    run_once "$@"
}

# Every step, failed or not, leaves its workspace's target swept (sweep.py,
# which also holds natedev's disk floor). The sweep runs in the background, so
# no step waits for it, at most once every SWEEP_EVERY_S per working directory:
# it takes cargo's locks while it scans, a few seconds, and a build starting
# then waits for it.
SWEEP_EVERY_S=300

sweep_after_step() {
    [[ "${LINT_SKIP_SWEEP:-}" == 1 ]] && return 0
    local arg now last=0 dir stamp
    for arg in "$@"; do
        [[ "$arg" == */sweep.py ]] && return 0
    done
    dir="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/lint-sweep"
    stamp="$dir/${PWD//\//%}"
    now="${EPOCHSECONDS:-$(date +%s)}"
    [[ -r "$stamp" ]] && read -r last < "$stamp"
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
    (( now - last >= SWEEP_EVERY_S )) || return 0
    mkdir -p "$dir" && printf '%s\n' "$now" > "$stamp" || return 0
    # Detached work and its descendants do not own verify.sh's foreground step.
    ( trap '' HUP; TOKEN_HELD=0; invoke_sweep </dev/null >/dev/null 2>&1 & )
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

# The nextest width mode: trial alternates physical and logical by block, and
# physical or logical fixes the width. The trial block length must stay in sync
# with BLOCK_S in scripts/buildlog/width_trial.py.
LINT_TEST_WIDTH=logical

nextest_physical_cores() {
    [[ "${LINT_PHYSICAL_CORES+x}" == x ]] && return 0
    LINT_PHYSICAL_CORES=""

    local sysfs_root="${1:-/sys/devices/system/cpu}"
    local topology package core pair seen="" count=0 value=""
    case "$OSTYPE" in
        linux*)
            for topology in "$sysfs_root"/cpu[0-9]*/topology; do
                [[ -r "$topology/physical_package_id" && -r "$topology/core_id" ]] || continue
                IFS= read -r package < "$topology/physical_package_id" 2>/dev/null || continue
                IFS= read -r core < "$topology/core_id" 2>/dev/null || continue
                pair=":$package:$core:"
                case "$seen" in
                    *"$pair"*) continue ;;
                esac
                seen="$seen$pair"
                (( count += 1 ))
            done
            value="$count"
            ;;
        darwin*)
            value="$(sysctl -n hw.physicalcpu 2>/dev/null)" || value=""
            ;;
    esac
    [[ "$value" =~ ^[1-9][0-9]*$ ]] && LINT_PHYSICAL_CORES="$value"
    return 0
}

nextest_logical_cores() {
    [[ "${LINT_LOGICAL_CORES+x}" == x ]] && return 0
    LINT_LOGICAL_CORES=""

    local value=""
    value="$(getconf _NPROCESSORS_ONLN 2>/dev/null)" || value=""
    [[ "$value" =~ ^[1-9][0-9]*$ ]] && LINT_LOGICAL_CORES="$value"
    return 0
}

nextest_width_arm() {
    local epoch="$1"
    if (( (epoch / 6000) % 2 == 0 )); then
        LINT_TEST_WIDTH_ARM=physical
    else
        LINT_TEST_WIDTH_ARM=logical
    fi
}

# Local nextest runs share the machine's test threads: a run takes the mode's
# width divided by the runs in flight, itself included, never below
# nextest_budget.FLOOR (4). A run claims its share only after the memory gate
# admits it (run_once calls nextest_share_claim), so a run still waiting for
# memory never counts and never narrows the runs already testing. Each run holds
# an flock on its own marker through fd 9 for as long as it runs, so a killed run
# stops counting with its shell. The test processes do not inherit the fd. The
# trial mode stays unshared, since width_trial.py names its arm by width. Any
# failure runs at the full width.
LINT_TEST_BUDGET_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nextest_budget.py"

run_nextest_shared() {
    local width=$1 status=0 dir name
    shift
    dir="${BUILDLOG_DIR:-$HOME/.local/state/buildlog}/admission/nextest"
    name="$$.$RANDOM$RANDOM"
    if ! mkdir -p "$dir" 2>/dev/null; then
        run cargo nextest run --test-threads "$width" "$@"
        return
    fi
    # The block puts fd 9 back as it was, so the marker nextest_share_claim
    # opens on it closes when the run ends.
    {
        LINT_NEXTEST_SHARE="$dir/$name" run cargo nextest run --test-threads "$width" "$@" || status=$?
    } 9>&-
    rm -f "$dir/$name.run" "$dir/$name.new"
    return "$status"
}

# nextest_share_claim ARGV... (`cargo nextest run --test-threads WIDTH ...`)
# Called by run_once once the memory gate has admitted the step. It opens the
# run's marker on fd 9, claims a share of WIDTH, and leaves the argv to run in
# LINT_NEXTEST_SHARED_ARGV. A narrower share is written into the step's memory
# reservation, so its peak is recorded against the width it ran at.
nextest_share_claim() {
    local shared="" width=$5
    LINT_NEXTEST_SHARED_ARGV=("$@")
    [[ "${4:-}" == --test-threads && "$width" =~ ^[1-9][0-9]*$ ]] || return 0
    { exec 9>>"$LINT_NEXTEST_SHARE.new"; } 2>/dev/null || return 0
    shared="$(buildlog_memory_python "$LINT_TEST_BUDGET_SCRIPT" claim "${LINT_NEXTEST_SHARE%/*}" \
        "${LINT_NEXTEST_SHARE##*/}" "$width" 9 2>/dev/null)" || shared=""
    [[ "$shared" =~ ^[1-9][0-9]*$ && "$shared" != "$width" ]] || return 0
    LINT_NEXTEST_SHARED_ARGV=("${@:1:4}" "$shared" "${@:6}")
    printf 'nextest: %s of %s test threads, shared with the runs in flight\n' "$shared" "$width"
    if [[ -n "${BUILDLOG_MEM_RESERVATION:-}" ]]; then
        buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" width "$BUILDLOG_MEM_RESERVATION" "$shared" \
            >/dev/null 2>&1 || true
    fi
}

# The test run is over: drop the share before the build log write and the sweep
# start in the background, so neither keeps the marker locked.
nextest_share_end() {
    [[ -z "${LINT_NEXTEST_SHARE:-}" ]] || exec 9>&-
}

run_nextest() {
    require_nextest
    build_hold_wait_local || return $?

    local arg arm="" width=""
    if [[ "${NEXTEST_TEST_THREADS+x}" != x ]]; then
        for arg in "$@"; do
            case "$arg" in
                --)
                    break
                    ;;
                --test-threads|--test-threads=*|-j|-j*)
                    run cargo nextest run "$@"
                    return
                    ;;
            esac
        done

        case "$LINT_TEST_WIDTH" in
            trial)
                nextest_width_arm "${EPOCHSECONDS:-$(date +%s)}"
                arm="$LINT_TEST_WIDTH_ARM"
                ;;
            physical|logical)
                arm="$LINT_TEST_WIDTH"
                ;;
        esac
        case "$arm" in
            physical)
                nextest_physical_cores
                width="${LINT_PHYSICAL_CORES:-}"
                ;;
            logical)
                nextest_logical_cores
                width="${LINT_LOGICAL_CORES:-}"
                ;;
        esac
        if [[ -n "$width" ]]; then
            if [[ "$LINT_TEST_WIDTH" == trial ]]; then
                run cargo nextest run --test-threads "$width" "$@"
            else
                run_nextest_shared "$width" "$@"
            fi
            return
        fi
    fi
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

# Use the same scope, lint arguments, gating, and run wrapper as judgment.
invoke_clippy_fix() {
    invoke_clippy --fix --allow-dirty --allow-staged --allow-no-vcs "$@"
}

invoke_mend() {
    if ! lint_config_enabled mend; then
        lint_config_skip_notice mend "cargo mend --workspace"
        return 0
    fi
    # RUSTC_WRAPPER cleared: a compiler cache replays cached output instead of
    # running rustc, which suppresses the diagnostics mend analyzes
    # The caller sets the scope. The lint CLI resolves its scope before this
    # call. Keep the caller's flags so targeted runs stay within that scope.
    run env RUSTC_WRAPPER= cargo mend --all-targets "$@"
}

# Scope and features come from the caller, like mend and clippy. Both used to
# be fixed here. Standalone lint doc can select members and honors doc=off.
# verify.sh doc checks the workspace before each Cargo checkpoint and ignores
# that switch. The push gate (validate_ci.sh) also checks the workspace and
# passes --all-features for full feature coverage.
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
# the measurements behind it. The budget is sweep_budget_gib.<repo> or
# sweep_budget_gib in config/lint.conf (24 GiB, hana 96), which
# LINT_SWEEP_BUDGET_GIB overrides; the other knob is LINT_SWEEP_DOC_INDEX_MIB (default 250, the doc/ cross-crate index past
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
