#!/usr/bin/env bash

# Address the single cargo-berth fixer session: attach, resume, or start it.
#
# One fixer exists at a time, in one worktree, under one recorded session id, so
# every reporter can reach it by name and every restart continues the same
# conversation. Start and resume both run through here because a second copy of
# the fixer is the one failure this script exists to prevent: `claude --bg -r`
# on an already-running session starts a copy instead of refusing.
#
# The id is recorded rather than pinned: `--bg` assigns its own session id and
# ignores `--session-id`, so only a session started in a terminal can be given
# one up front.

set -u

repository=$HOME/rust/cargo-liner
worktree=$HOME/rust/berth-fix
legacy_worktree=$HOME/rust/cargo-liner-berth-fix
branch=fix/berth
session_name=berth-fix
state=$HOME/.claude/state/berth-fix
record=$state/session.json
lock=$state/launch.lock

usage() {
    cat <<'USAGE'
usage:
  launch.sh                                    attach to the live fixer, or start it in this terminal
  launch.sh --status                           report paths and liveness, launch nothing
  launch.sh --report <dir> --reply-to <name>   ensure a background fixer exists, addressed to <name>
USAGE
}

# A row in `claude agents --json` outlives the session it names: rows for dead
# background sessions persist with state "blocked". Liveness is the row plus a
# live process, so every caller goes through this.
live_fixer() {
    local rows candidate kind identifier
    rows=$(timeout 60 claude agents --json 2>/dev/null) || return 1
    candidate=$(printf '%s' "$rows" | SESSION_NAME=$session_name python3 -c '
import json, os, sys

wanted = os.environ["SESSION_NAME"]
try:
    rows = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
for row in rows:
    if row.get("name") != wanted:
        continue
    if row.get("kind") == "background":
        print("background", row.get("id", ""))
    else:
        print("interactive", row.get("pid", ""))
') || return 1
    [ -n "$candidate" ] || return 1

    kind=${candidate%% *}
    identifier=${candidate##* }
    [ -n "$identifier" ] || return 1
    if [ "$kind" = interactive ]; then
        kill -0 "$identifier" 2>/dev/null || return 1
    else
        timeout 60 claude logs "$identifier" >/dev/null 2>&1 || return 1
    fi
    printf '%s %s\n' "$kind" "$identifier"
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

# A background session is assigned its id at launch, so it can only be read back
# afterwards. It takes a few seconds to register.
discover_session_id() {
    local attempt=0
    while [ "$attempt" -lt 15 ]; do
        local found
        found=$(timeout 60 claude agents --json 2>/dev/null | SESSION_NAME=$session_name python3 -c '
import json, os, sys

wanted = os.environ["SESSION_NAME"]
try:
    rows = json.load(sys.stdin)
except ValueError:
    raise SystemExit(1)
for row in rows:
    if row.get("name") == wanted and row.get("kind") == "background" and row.get("sessionId"):
        print(row["sessionId"])
        break
' 2>/dev/null)
        if [ -n "$found" ]; then
            printf '%s\n' "$found"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
}

# The fixer used to live at $legacy_worktree. Relocation is the only way onto the
# new path: that checkout still holds $branch, so `worktree add` would refuse, and
# the recorded conversation is filed under the old directory, so its project
# history moves with it or the next resume finds nothing to continue. Reached only
# with no fixer live, since every caller checks that before preparing a worktree.
relocate_legacy_worktree() {
    local projects=$HOME/.claude/projects
    git -C "$repository" worktree move "$legacy_worktree" "$worktree" || return 1
    if [ -d "$projects/${legacy_worktree//\//-}" ] && [ ! -d "$projects/${worktree//\//-}" ]; then
        mv "$projects/${legacy_worktree//\//-}" "$projects/${worktree//\//-}"
    fi
}

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

take_lock() {
    mkdir "$lock" 2>/dev/null || {
        printf 'another launch is in progress (%s); nothing started\n' "$lock" >&2
        exit 3
    }
    trap 'rmdir "$lock" 2>/dev/null' EXIT HUP INT TERM
}

# CLAUDE_CODE_MESSAGING_SOCKET, its token, and the bridge session id name the
# caller's session; a child inheriting them comes up wearing the caller's
# identity. CLAUDE_CODE_ENTRYPOINT is left alone deliberately — stripping it
# reads as hiding the caller and auto mode denies the launch.
claude_child() {
    env -u CLAUDE_CODE_MESSAGING_SOCKET \
        -u CLAUDE_CODE_MESSAGING_TOKEN \
        -u CLAUDE_CODE_BRIDGE_SESSION_ID \
        claude "$@"
}

mode=interactive
report=
reply_to=
while [ $# -gt 0 ]; do
    case $1 in
        --status) mode=status ;;
        --report) shift; report=${1-} ;;
        --reply-to) shift; reply_to=${1-} ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
if [ -n "$report" ] || [ -n "$reply_to" ]; then
    mode=report
    if [ -z "$report" ] || [ -z "$reply_to" ]; then
        printf -- '--report and --reply-to are used together\n' >&2
        exit 2
    fi
fi

if [ "$mode" = status ]; then
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
    if found=$(live_fixer); then
        printf 'fixer: live %s\n' "$found"
        [ "${found%% *}" = background ] && printf 'attach: claude attach %s\n' "${found##* }"
    else
        printf 'fixer: not running\n'
    fi
    exit 0
fi

mkdir -p "$state/inbox"

if [ "$mode" = report ]; then
    take_lock
    if found=$(live_fixer); then
        printf 'already-live %s %s\n' "$session_name" "$found"
        [ "${found%% *}" = background ] && printf 'attach: claude attach %s\n' "${found##* }"
        exit 0
    fi
    ensure_worktree || { printf 'worktree preparation failed\n' >&2; exit 1; }
    resume_flags=()
    if remembered=$(recorded_session); then
        resume_flags=(-r "$remembered")
    fi
    (
        cd "$worktree" || exit 1
        claude_child --bg -n "$session_name" "${resume_flags[@]+"${resume_flags[@]}"}" \
            --permission-mode auto \
            "/berth_fix --fixer --report $report --reply-to \"$reply_to\""
    ) || { printf 'background launch failed\n' >&2; exit 1; }
    printf 'launched %s\n' "$session_name"
    if [ ${#resume_flags[@]} -eq 0 ]; then
        if started=$(discover_session_id); then
            remember_session "$started" background
        else
            printf 'session id not recorded; the next launch starts a fresh conversation\n' >&2
        fi
    fi
    exit 0
fi

if found=$(live_fixer); then
    if [ "${found%% *}" = background ]; then
        exec claude attach "${found##* }"
    fi
    printf '%s is already running interactively (pid %s) in another terminal\n' \
        "$session_name" "${found##* }"
    exit 0
fi
take_lock
ensure_worktree || { printf 'worktree preparation failed\n' >&2; exit 1; }
if remembered=$(recorded_session); then
    resume_flags=(-r "$remembered")
else
    remembered=$(python3 -c 'import uuid; print(uuid.uuid4())') || exit 1
    resume_flags=(--session-id "$remembered")
    remember_session "$remembered" interactive
fi
cd "$worktree" || exit 1
rmdir "$lock" 2>/dev/null
trap - EXIT HUP INT TERM
exec env -u CLAUDE_CODE_MESSAGING_SOCKET \
    -u CLAUDE_CODE_MESSAGING_TOKEN \
    -u CLAUDE_CODE_BRIDGE_SESSION_ID \
    claude -n "$session_name" "${resume_flags[@]}" "/berth_fix --fixer"
