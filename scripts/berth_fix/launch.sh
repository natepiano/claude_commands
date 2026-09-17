#!/usr/bin/env bash

# Open or attach the single cargo-berth fixer session.
#
# The fixer is resident: one conversation in one terminal, kept running so every
# report reaches a session that already holds every earlier engagement. Reporters
# message it by name and never start it, so this script is the only way one
# starts — and a second copy, the failure it exists to prevent, has nowhere to
# come from.
#
# It runs in a terminal rather than in the background because its engagement ends
# in a merge to main, and auto mode escalates that merge to a user a background
# session does not have.

set -u

repository=$HOME/rust/cargo-liner
worktree=$HOME/rust/berth-fix
legacy_worktree=$HOME/rust/cargo-liner-berth-fix
branch=fix/berth
session_name=berth-fix
state=$HOME/.claude/state/berth-fix
record=$state/session.json

usage() {
    cat <<'USAGE'
usage:
  launch.sh             open the fixer in a Ghostty window, or reach the live one
  launch.sh --here      open the fixer in this terminal instead of a window
  launch.sh --status    report paths and liveness, open nothing
USAGE
}

# The fixer is the live session sitting in the fix worktree. The worktree, not
# the name, is the identity: a row in `claude agents --json` outlives the session
# it names, finished sessions leave rows behind with no pid, and a renamed window
# still answers from the same directory. Liveness is a row in $worktree with a pid
# that answers, a resident terminal beating any leftover background row. Prints
# `<pid> <name>`, and refreshes the record while it has the live session id in
# hand — a resumed session is given a new id, so the record goes stale on every
# restart unless something reads it back.
live_fixer() {
    local candidates kind pid session name
    candidates=$(timeout 60 claude agents --json 2>/dev/null | WORKTREE=$worktree python3 -c '
import json, os, sys

wanted = os.environ["WORKTREE"]
try:
    rows = json.load(sys.stdin)
except ValueError:
    raise SystemExit(1)
rows = [row for row in rows if row.get("cwd") == wanted and row.get("pid")]
rows.sort(key=lambda row: row.get("kind") != "interactive")
for row in rows:
    print(row.get("kind", "interactive"), row["pid"], row.get("sessionId", ""), row.get("name", ""))
') || return 1
    [ -n "$candidates" ] || return 1
    while read -r kind pid session name; do
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || continue
        [ -n "$session" ] && remember_session "$session" "$kind"
        printf '%s %s\n' "$pid" "$name"
        return 0
    done <<< "$candidates"
    return 1
}

recorded_session() {
    [ -f "$record" ] || return 1
    python3 -c '
import json, sys
try:
    recorded = json.load(open(sys.argv[1])).get("session_id", "")
except (OSError, ValueError):
    raise SystemExit(1)
if not recorded:
    raise SystemExit(1)
print(recorded)
' "$record" 2>/dev/null
}

remember_session() {
    RECORD=$record SESSION_ID=$1 KIND=$2 python3 -c '
import json, os, subprocess

with open(os.environ["RECORD"], "w") as handle:
    json.dump({
        "session_id": os.environ["SESSION_ID"],
        "kind": os.environ["KIND"],
        "recorded_at": subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                                      capture_output=True, text=True).stdout.strip(),
    }, handle, indent=2)
    handle.write("\n")
'
}

# A terminal session takes the id it is given, so the conversation is pinned
# before it starts; only `--bg` assigns its own and forces discovery afterwards.
resume_flags=()
set_resume_flags() {
    local remembered
    if remembered=$(recorded_session); then
        resume_flags=(-r "$remembered")
        return 0
    fi
    remembered=$(python3 -c 'import uuid; print(uuid.uuid4())') || return 1
    resume_flags=(--session-id "$remembered")
    remember_session "$remembered" "$1"
}

# The fixer used to live at $legacy_worktree. Relocation is the only way onto the
# new path: that checkout still holds $branch, so `worktree add` would refuse, and
# the recorded conversation is filed under the old directory, so its project
# history moves with it or the next resume finds nothing to continue.
relocate_legacy_worktree() {
    local projects=$HOME/.claude/projects
    git -C "$repository" worktree move "$legacy_worktree" "$worktree" || return 1
    if [ -d "$projects/${legacy_worktree//\//-}" ] && [ ! -d "$projects/${worktree//\//-}" ]; then
        mv "$projects/${legacy_worktree//\//-}" "$projects/${worktree//\//-}"
    fi
}

# The worktree is permanent: it outlives every engagement, and the fixer catches
# it up from main when a new report arrives.
ensure_worktree() {
    [ -d "$worktree" ] && return 0
    [ -d "$legacy_worktree" ] && { relocate_legacy_worktree; return; }
    if git -C "$repository" show-ref --verify --quiet "refs/heads/$branch"; then
        git -C "$repository" worktree add "$worktree" "$branch" || return 1
    else
        git -C "$repository" worktree add "$worktree" -b "$branch" || return 1
    fi
    bash "$HOME/.claude/scripts/make_a_worktree/copy_settings_local.sh" "$worktree" >/dev/null 2>&1 || true
    bash "$HOME/.claude/scripts/make_a_worktree/direnv_allow.sh" "$worktree" >/dev/null 2>&1 || true
}

# CLAUDE_CODE_MESSAGING_SOCKET, its token, and the bridge session id name the
# caller's session; a child inheriting them comes up wearing the caller's
# identity. CLAUDE_CODE_ENTRYPOINT is left alone deliberately — stripping it
# reads as hiding the caller and auto mode denies the launch.
without_caller_identity() {
    env -u CLAUDE_CODE_MESSAGING_SOCKET \
        -u CLAUDE_CODE_MESSAGING_TOKEN \
        -u CLAUDE_CODE_BRIDGE_SESSION_ID \
        "$@"
}

# `+new-window` hands the command to the running Ghostty instance and returns at
# once, so the window outlives the shell that asked for it. With no instance to
# hand it to, a detached Ghostty starts one.
open_window() {
    without_caller_identity ghostty +new-window --working-directory="$worktree" -e "$@" && return 0
    without_caller_identity setsid ghostty --working-directory="$worktree" -e "$@" >/dev/null 2>&1 &
    disown 2>/dev/null
    return 0
}

report_live() {
    local pid=${1%% *} name=${1#* }
    printf 'fixer live: %s (pid %s) — reach it by name with SendMessage\n' "${name:-$session_name}" "$pid"
}

mode=window
while [ $# -gt 0 ]; do
    case $1 in
        --status) mode=status ;;
        --here) mode=here ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "$mode" = status ]; then
    # First: it refreshes the record, and the conversation line below reads it.
    found=$(live_fixer) || found=
    if [ -d "$worktree" ]; then
        printf 'worktree: %s\n' "$worktree"
    elif [ -d "$legacy_worktree" ]; then
        printf 'worktree: %s (moves to %s on the next launch)\n' "$legacy_worktree" "$worktree"
    else
        printf 'worktree: %s (absent)\n' "$worktree"
    fi
    printf 'branch: %s\n' "$branch"
    printf 'session: %s\n' "$session_name"
    if remembered=$(recorded_session); then
        printf 'conversation: resumable (%s)\n' "$remembered"
    else
        printf 'conversation: none yet\n'
    fi
    if [ -n "$found" ]; then
        report_live "$found"
    else
        printf 'fixer: not running (launch.sh opens it)\n'
    fi
    exit 0
fi

if found=$(live_fixer); then
    report_live "$found"
    exit 0
fi

mkdir -p "$state/inbox"
ensure_worktree || { printf 'worktree preparation failed\n' >&2; exit 1; }
set_resume_flags "$mode" || exit 1

if [ "$mode" = here ]; then
    cd "$worktree" || exit 1
    exec env -u CLAUDE_CODE_MESSAGING_SOCKET \
        -u CLAUDE_CODE_MESSAGING_TOKEN \
        -u CLAUDE_CODE_BRIDGE_SESSION_ID \
        claude -n "$session_name" "${resume_flags[@]}" --permission-mode auto "/berth_fix --fixer"
fi

open_window claude -n "$session_name" "${resume_flags[@]}" --permission-mode auto "/berth_fix --fixer" \
    || { printf 'window launch failed\n' >&2; exit 1; }
printf 'opened %s in a Ghostty window\n' "$session_name"
