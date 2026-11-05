#!/usr/bin/env bash
# Start a named Claude worker and wait for its turn.
# The session remains available after its turn ends.
# Staff supplies its current identity for later messages and cleanup.

set -euo pipefail
"${BASH_SOURCE[0]%/*}/../lib/py" "${BASH_SOURCE[0]%/*}/../direct/assign_gate.py"
unset STAFF_ID

ATTACH=0
if [[ "${1:-}" == "--attach" ]]; then
  ATTACH=1
  shift
fi

[[ "${1:-}" == --run ]] || { echo "agent_bg.sh: --run <session_dir> is required" >&2; exit 2; }
ADMISSION_RUN="${2:?missing session_dir}"
shift 2
export UNIT_RUN_DIR="$ADMISSION_RUN"

# Use the repository interpreter shim on both machines.
PY="${HOME}/.claude/scripts/lib/py"
NIX_ENV_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"

MESH_NAME="${1:?Usage: agent_bg.sh [--attach] --run <session_dir> <mesh_name> <working_dir> <prompt_file> <summary_file> <log_file> [model] [poll_secs]}"
WORKING_DIR="${2:?missing working_dir}"
PROMPT_FILE="${3:?missing prompt_file}"
SUMMARY_FILE="${4:?missing summary_file}"
LOG_FILE="${5:?missing log_file}"
MODEL="${6:-}"
POLL_SECS="${7:-15}"
EFFORT="${AGENT_BG_EFFORT:-}"
DETACH="${AGENT_BG_DETACH:-0}"

# Use an executable path. Interactive aliases can change stop commands.
CLAUDE_BIN="${CLAUDE_BIN:-}"
if [[ -z "${CLAUDE_BIN}" ]]; then
  if [[ -x "${HOME}/.local/bin/claude" ]]; then
    CLAUDE_BIN="${HOME}/.local/bin/claude"
  else
    CLAUDE_BIN="$(command -v claude || true)"
  fi
fi
if [[ -z "${CLAUDE_BIN}" ]]; then
  echo "agent_bg.sh: no claude binary found." >&2
  exit 127
fi

if [[ ! -f "${PROMPT_FILE}" ]]; then
  echo "agent_bg.sh: prompt file not found: ${PROMPT_FILE}" >&2
  exit 2
fi

if [[ ! "${MESH_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]; then
  echo "agent_bg.sh: invalid mesh name '${MESH_NAME}'." >&2
  exit 2
fi

WORKER_FILL="$(dirname "${BASH_SOURCE[0]}")/../production/worker_fill.py"
close_switch_fds() { exec {SWITCH_READ}<&- {SWITCH_WRITE}>&-; }
switch_signal() {
  printf '%s\n' "$1" >&"$SWITCH_WRITE"
  if [[ "$1" == dispatched ]]; then
    close_switch_fds
    wait "$SWITCH_PID" || return "$?"
    trap - EXIT INT TERM HUP
    SWITCH_PID=""
  else
    local reply
    read -r -u "$SWITCH_READ" reply NATIVE_ID || { wait "$SWITCH_PID"; return 2; }
    [[ "$reply" == published ]] || return 2
    WORKER_PUBLISHED=1
  fi
}

SWITCH_PID=""
WORKER_PUBLISHED=0
if [[ "$ATTACH" == 0 ]]; then
  coproc ADMISSION { exec "$PY" "$WORKER_FILL" coordinate-claude --name "$MESH_NAME" --run "$ADMISSION_RUN" \
    --prompt "$PROMPT_FILE" --staff-id "${WORKER_STAFF_ID:-}"; }
  SWITCH_PID=$ADMISSION_PID
  exec {SWITCH_READ}<&"${ADMISSION[0]}" {SWITCH_WRITE}>&"${ADMISSION[1]}"
  close_switch() {
    local status=$?
    close_switch_fds
    kill "$SWITCH_PID" 2>/dev/null || true
    wait "$SWITCH_PID" 2>/dev/null || true
    if [[ "$WORKER_PUBLISHED" == 0 && -n "${BG_ID:-}" ]]; then
      "$CLAUDE_BIN" stop "$BG_ID" || echo "staff: ${MESH_NAME}: ${BG_ID}: cleanup unproved" >&2
    fi
    exit "$status"
  }
  trap close_switch EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  read -r -u "$SWITCH_READ" action || { wait "$SWITCH_PID"; exit 2; }
  case "$action" in
    unchanged) wait "$SWITCH_PID"; echo unchanged; exit 0 ;;
    attach) ATTACH=1 ;;
    launch) ;;
    *) exit 2 ;;
  esac
  read -r -u "$SWITCH_READ" PROMPT_FILE || exit 2
fi

if [[ "${ATTACH}" == "1" ]]; then
  if [[ -n "$SWITCH_PID" ]]; then
    switch_signal attached
  else
    read -r FAMILY NATIVE_ID < <("$PY" "$WORKER_FILL" member --run "$ADMISSION_RUN" --name "$MESH_NAME" --staff-id "${WORKER_STAFF_ID:-}")
    [[ "$FAMILY" == claude ]] || exit 2
  fi
  BG_ID="${NATIVE_ID:0:8}"
  transcript_count() {
    NATIVE_ID="${NATIVE_ID}" "$PY" - <<'PY'
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(os.environ["HOME"]) / ".claude"))
from scripts.message import sessions
matches = sessions.transcript_locations(os.environ["NATIVE_ID"])
count = 0
if matches:
    try:
        for line in matches[0].read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "assistant" and row.get("message", {}).get("stop_reason") in ("end_turn", "stop_sequence"):
                count += 1
    except OSError:
        pass
print(count)
PY
  }
  before="$(transcript_count)"
else

source "$(dirname "${BASH_SOURCE[0]}")/claude_account.sh"
if [[ "${AGENT_BG_WORKERS:-0}" == "1" ]]; then
  claude_launch_environment workers || exit 1
else
  claude_launch_environment || exit 1
fi

# The daemon can retain an earlier worktree environment.
# Pass the current worker values through its session settings.
WORKER_ENV_NAMES=(UNIT_RUN_DIR UNIT_TEAM_ROLE MODELS_CONFIG_FILE)
worker_settings="$("$PY" - "$WORKING_DIR" "$NIX_ENV_LIB" "${WORKER_ENV_NAMES[@]}" <<'PY'
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
import nix_env

loaded = nix_env.ensure(Path(sys.argv[1]))
if isinstance(loaded, nix_env.Refused):
    print(loaded.reason, file=sys.stderr)
    raise SystemExit(1)
env = {name: value for name, value in loaded.env.items()
       if not name.startswith(("HERDR_", "CODEX_", "CLAUDE_CODE_MESSAGING_"))
       and name not in {"CLAUDE_CODE_BRIDGE_SESSION_ID", "TMUX", "TMUX_PANE"}}
env.update({name: "" for name in os.environ if name not in loaded.env})
env.update({name: os.environ.get(name, "") for name in sys.argv[3:]})
env.update({name: os.environ[name] for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_REMOTE_MEMORY_DIR") if name in os.environ})
env.pop("CLAUDE_CODE_CHILD_SESSION", None)
print(json.dumps({"sandbox": {"enabled": False}, "env": env}))
PY
)" || exit 1

launch_args=(--bg --name "${MESH_NAME}"
             --dangerously-skip-permissions
             --settings "${worker_settings}")
if [[ -n "${MODEL}" ]]; then
  launch_args+=(--model "${MODEL}")
fi
if [[ -n "${EFFORT}" ]]; then
  launch_args+=(--effort "${EFFORT}")
fi

banner="$(close_switch_fds
          cd "${WORKING_DIR}" \
            && env -u UNIT_RUN_DIR -u UNIT_TEAM_ROLE -u MODELS_CONFIG_FILE \
                   -u UNIT_RESOLVES_ROUND -u AGENT_BG_WORKERS -u CLAUDE_CODE_CHILD_SESSION \
                   "${PY}" "$NIX_ENV_LIB/nix_env.py" exec "${CLAUDE_BIN}" "${launch_args[@]}" \
            2>&1 || true)"

# Read the id only from the launch banner.
# A diagnostic path can also contain eight hexadecimal characters.
BG_ID="$(printf '%s\n' "${banner}" | grep 'backgrounded' | grep -oE '\b[0-9a-f]{8}\b' | head -1 || true)"
if [[ -z "${BG_ID}" ]]; then
  printf '%s\n' "${banner}" > "${LOG_FILE}"
  echo "agent_bg.sh: could not start a background session; banner follows." >&2
  printf '%s\n' "${banner}" >&2
  exit 1
fi
switch_signal "$BG_ID" || exit 2
fi

SEND_SCRIPT="${AGENT_BG_SEND_SCRIPT:-$(dirname "${BASH_SOURCE[0]}")/../message/send.py}"
delivery="$("$PY" "${SEND_SCRIPT}" --to "session:${NATIVE_ID}" --from "launcher" --file "${PROMPT_FILE}")" || exit 2
if [[ "$delivery" != SENT:* && "$delivery" != SKIPPED:* ]]; then
  echo "agent_bg.sh: opening prompt was not delivered: ${delivery}" >&2
  exit 2
fi

[[ -z "$SWITCH_PID" ]] || switch_signal dispatched || exit 2

if [[ "$ATTACH" == 1 ]]; then
  if [[ -n "${UNIT_RUN_DIR:-}" && -n "${UNIT_TEAM_ROLE:-}" ]]; then
    FOLLOW_TURN_INDEX="$((before + 1))" "$PY" - "$(dirname "${BASH_SOURCE[0]}")/../direct" "$ADMISSION_RUN" <<'PY'
import json
import os
from pathlib import Path

import sys
sys.path.insert(0, sys.argv[1])
from worker_state import worker_claim_lock

root = Path(sys.argv[2])
slot = os.environ["UNIT_TEAM_ROLE"]
with worker_claim_lock(root):
    path = root / f"impl_claim_{slot}.json"
    try:
        claim = json.loads(path.read_text())
    except (OSError, ValueError):
        raise SystemExit
    if claim.get("pid") == int(os.environ.get("FOLLOW_LAUNCHER_PID", "0")):
        claim["turn_id"] = f"claude-turn-{os.environ['FOLLOW_TURN_INDEX']}"
        path.write_text(json.dumps(claim))
PY
  fi
else

"$PY" "$(dirname "${BASH_SOURCE[0]}")/../usage/agents_log.py" append --id "${BG_ID}" --role worker || true
if [[ "${DETACH}" == "1" ]]; then
  echo "launched ${MESH_NAME} (${BG_ID})"
  exit 0
fi
fi

agent_status() {
  "${CLAUDE_BIN}" agents --json 2>/dev/null | BG_ID="${BG_ID}" "$PY" -c '
import json
import os
import sys

wanted = os.environ["BG_ID"]
try:
    rows = json.loads(sys.stdin.read())
except (json.JSONDecodeError, ValueError):
    sys.exit(0)
for row in rows:
    if row.get("id") == wanted:
        state = row.get("state")
        print(row.get("status") or {"working": "busy", "done": "idle", "blocked": "idle"}.get(
            state, state or "unknown"))
        break
else:
    print("gone")
' 2>/dev/null || true
}

# Replace the rolling log tail to avoid repeated lines.
refresh_log() {
  "${CLAUDE_BIN}" logs "${BG_ID}" > "${LOG_FILE}.tmp" 2>/dev/null || return 0
  mv -f "${LOG_FILE}.tmp" "${LOG_FILE}" 2>/dev/null || true
}

# Require evidence that work started before a new session becomes idle.
seen_busy=0
while true; do
  status="$(agent_status)"
  refresh_log
  case "${status}" in
    busy|shell|prompting)
      seen_busy=1
      ;;
    idle)
      if [[ "$ATTACH" == 1 ]]; then
        after="$(transcript_count)"
        [[ "$after" =~ ^[0-9]+$ && "$after" -gt "$before" ]] && break
      elif [[ "$seen_busy" -eq 1 ]]; then
        break
      fi
      ;;
    gone)
      [[ "$ATTACH" == 0 ]] || { echo "agent_bg.sh: ${MESH_NAME} vanished after delivery" >&2; exit 1; }
      break
      ;;
  esac
  sleep "${POLL_SECS}"
done

refresh_log
# Use the visible reply when the worker leaves no summary.
if [[ ! -s "${SUMMARY_FILE}" ]]; then
  if [[ -s "${LOG_FILE}" ]]; then
    if ! "$PY" - "${LOG_FILE}" "${SUMMARY_FILE}" <<'PY'
import re
import sys
from pathlib import Path

log = Path(sys.argv[1]).read_bytes().decode("utf-8", errors="replace")
terminal_sequences = (
    r"\x1b\].*?(?:\x07|\x1b\\|\Z)"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[0-~]"
)
plain = re.sub(terminal_sequences, "", log, flags=re.DOTALL)
plain = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", plain)
tail = plain[-4000:]
if tail.strip():
    Path(sys.argv[2]).write_text(tail, encoding="utf-8")
PY
    then
      : > "${SUMMARY_FILE}"
    fi
  fi
  if [[ ! -s "${SUMMARY_FILE}" ]]; then
    printf 'The background agent %s produced no summary.\n' "${MESH_NAME}" > "${SUMMARY_FILE}"
  fi
fi

if [[ "$(agent_status)" == "gone" ]]; then
  [[ -s "${SUMMARY_FILE}" ]] || exit 1
fi
exit 0
