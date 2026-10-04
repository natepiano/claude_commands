#!/usr/bin/env bash
# mend_rebuild.sh: rebuilds the clippy-linked cargo-mend for the stable rustc
# once a toolchain update leaves the installed one stale (built for another
# rustc, or unable to load the librustc_driver it links). invoke.sh's
# mend_clippy_rebuild starts it detached on each stale probe outside the
# Claude Code sandbox; run by hand, it works in the foreground.
#
# Each run, in order:
#   1. exits unless cargo-mend is stale for the stable rustc, asked from $HOME:
#      a project that pins another toolchain probes stale where a rebuild
#      cannot help
#   2. alerts `no-install` when the cargo-liner checkout has no install.sh
#   3. alerts `cold` when clippy's source for this rustc is not cached, so
#      install.sh must fetch it over ssh (git rewrites github.com urls to ssh),
#      and github-warm-status says gpg-agent is cold
#   4. exits when a build started less than an hour ago
#   5. runs install.sh with its output in rebuild.log, and alerts `failed`
#      unless cargo-mend then reports active
# Steps 2 and 3 run no build and do not start the hour, so after a pull or a
# `github-warmup` the next stale probe rebuilds. An alert goes out once per
# reason and stable rustc (a stamp in the state directory); a rebuild that
# succeeds removes the stamps. One run at a time: the lock is a symlink naming
# the holder's pid, and a holder that died leaves one the next run replaces.
#
# The build gets the environment a nate.jobs job gets: a fixed PATH, and none
# of the calling worktree's devshell, CARGO_TARGET_DIR or RUSTUP_TOOLCHAIN.
# Tests replace that PATH with MEND_REBUILD_PATH, install.sh with
# MEND_REBUILD_INSTALL, pushover.py with LINT_ALERT_SENDER, and the state
# directory through XDG_STATE_HOME. Bash 3.2 compatible (the Mac's /bin/bash).
set -euo pipefail

# The sandbox can refuse the ssh fetch and the writes to ~/.cache for its own
# reasons; a failure there says nothing about the rebuild.
[[ "${SANDBOX_RUNTIME:-}" != 1 ]] || exit 0

if [[ -z "${MEND_REBUILD_CLEAN:-}" ]]; then
    user=${USER:-$(id -un)}
    if [[ "$(uname -s)" == Darwin ]]; then
        path="/etc/profiles/per-user/$user/bin:/run/current-system/sw/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:$HOME/.cargo/bin"
    else
        path="/run/wrappers/bin:/etc/profiles/per-user/$user/bin:/run/current-system/sw/bin:$HOME/.local/bin:$HOME/.cargo/bin"
    fi
    keep=("HOME=$HOME" "USER=$user")
    for name in LOGNAME LANG SSH_AUTH_SOCK GNUPGHOME XDG_RUNTIME_DIR XDG_STATE_HOME XDG_CACHE_HOME \
        CARGO_HOME RUSTUP_HOME CARGO_MAKEFLAGS LINT_ALERT_SENDER MEND_REBUILD_INSTALL; do
        [[ -z "${!name:-}" ]] || keep+=("$name=${!name}")
    done
    exec env -i "${keep[@]}" PATH="${MEND_REBUILD_PATH:-$path}" MEND_REBUILD_CLEAN=1 "$BASH" "$0" "$@"
fi

dir="${XDG_STATE_HOME:-$HOME/.local/state}/mend-clippy"
log="$dir/rebuild.log"
built="$dir/last-build"
lock="$dir/rebuild.lock"
install="${MEND_REBUILD_INSTALL:-$HOME/rust/cargo-liner/crates/cargo-mend-clippy/install.sh}"
pushover="$HOME/.claude/scripts/notify/pushover.py"
sender="${LINT_ALERT_SENDER:-$pushover}"
machine=$(uname -n)
machine=${machine%%.*}
mkdir -p "$dir"

if ! ln -s "$$" "$lock" 2>/dev/null; then
    holder=$(readlink "$lock" 2>/dev/null) || holder=""
    if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
        exit 0
    fi
    rm -f "$lock"
    ln -s "$$" "$lock" 2>/dev/null || exit 0
fi
trap 'rm -f "$lock"' EXIT
trap 'exit 1' INT TERM

# The same test as invoke.sh's mend_clippy_probe, with the stable rustc.
mend_state() {
    local out status=0
    out=$(cd "$HOME" && RUSTUP_TOOLCHAIN=stable cargo mend --clippy-status 2>&1 </dev/null) || status=$?
    if [[ $status -eq 0 && "$out" == *'"clippy":"active"'* ]]; then
        echo active
    elif [[ "$out" == *'"clippy":"rustc_mismatch"'* || "$out" == *"error while loading shared libraries"* ||
        "$out" == *"Library not loaded"* ]]; then
        echo stale
    else
        echo absent
    fi
}

[[ "$(mend_state)" == stale ]] || exit 0

stable=$(rustc +stable -vV)
host=$(head -n 1 <<<"$stable")
commit=$(sed -n 's/^commit-hash: //p' <<<"$stable")

# alert REASON MESSAGE: the run that creates the stamp sends; a failed send
# removes it for a later run. The Pushover keys exist only on natedev.
alert() {
    local stamp
    stamp="$dir/alerted-$1-$(printf '%s' "$host" | tr -c 'A-Za-z0-9.' '-')"
    (set -C && : >"$stamp") 2>/dev/null || return 0
    local -a send=("$sender" --priority 1 "lint: cargo-mend did not rebuild" "$2")
    if [[ "$sender" == "$pushover" && "$(uname -s)" == Darwin ]]; then
        # shellcheck disable=SC2088 # the tilde is for natedev's shell
        send=(ssh -o BatchMode=yes -o ConnectTimeout=10 natedev
            "~/.claude/scripts/notify/pushover.py $(printf '%q ' "${send[@]:1}")")
    fi
    "${send[@]}" </dev/null >/dev/null 2>&1 || rm -f "$stamp"
}

if [[ ! -f "$install" ]]; then
    alert no-install "cargo-mend on $machine needs a rebuild for $host, and $install does not exist. Pull ~/rust/cargo-liner on $machine; the next lint rebuilds it."
    exit 0
fi

cold_alert() {
    alert cold "cargo-mend on $machine needs a rebuild for $host, which fetches clippy's source over ssh, and gpg-agent is cold. Run github-warmup on $machine; the next lint rebuilds it."
}

cache="${XDG_CACHE_HOME:-$HOME/.cache}/mend-clippy-src/$commit/src/tools/clippy"
if [[ ! -d "$cache" ]] && ! github-warm-status >/dev/null 2>&1; then
    cold_alert
    exit 0
fi

now=$(date +%s)
last=$(cat "$built" 2>/dev/null) || last=0
[[ "$last" =~ ^[0-9]+$ ]] || last=0
((now - last >= 3600)) || exit 0
echo "$now" >"$built"

echo "mend_rebuild.sh: $(date) on $machine, for $host" >"$log"
if "$BASH" "$install" >>"$log" 2>&1 </dev/null && [[ "$(mend_state)" == active ]]; then
    echo "mend_rebuild.sh: cargo-mend is active for $host" >>"$log"
    rm -f "$dir"/alerted-*
    exit 0
fi

why=$(grep '^install.sh: ' "$log" | tail -n 1) || why=""
if [[ "$why" == *"fetching clippy's source"* ]] && ! github-warm-status >/dev/null 2>&1; then
    rm -f "$built"
    cold_alert
    exit 0
fi
why=${why#install.sh: }
why=${why:-install.sh finished, and cargo-mend still reports stale}
echo "mend_rebuild.sh: failed: $why" >>"$log"
alert failed "Rebuilding cargo-mend on $machine for $host failed: $why. Log: $log. Stock clippy runs meanwhile; the next try is in an hour."
