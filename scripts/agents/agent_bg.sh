#!/usr/bin/env bash
# agent_bg.sh — Launch a delegate as a NAMED BACKGROUND Claude session and block
# until it finishes, so callers can treat it like any foreground worker.
#
# Usage: agent_bg.sh <mesh_name> <working_dir> <prompt_file> <summary_file>
#                    <log_file> <id_file> [model] [poll_secs]
#   env AGENT_BG_EFFORT=<effort>  adds --effort to the launch
#   env AGENT_BG_DETACH=1         return once the session is registered, with
#                                 its id in <id_file>, instead of waiting for
#                                 its turn to end -- for a caller that talks to
#                                 the session itself (ask_a_friend) rather than
#                                 waiting on a summary
#
# Why this exists rather than agent_exec.sh: a delegate launched with
# `claude --print` is INVISIBLE. It holds the ListAgents/SendMessage tools but
# registers nowhere, so it sees no peers, no peer sees it, and the orchestrator
# cannot reach it -- verified by running two concurrent print sessions in one
# directory, each of which reported "no reachable agents" four times while the
# other was live. `claude --bg` registers the session instead: it appears in
# ListAgents as a peer, `claude agents --json` lists it, and messages flow in
# every direction (delegate to delegate, delegate to the main session, and back).
# That mesh is the whole point -- a delegate that finds a blocker can tell the
# user's session directly instead of burying it in a summary nobody reads until
# the phase ends.
#
# The cost of --bg is that it returns immediately, which would break every
# caller that waits on a pid. So this script keeps the process shape: it stays
# in the foreground for the life of the agent and exits with its outcome, and
# the caller's `wait`, heartbeat watch, and pass recording work unchanged.
#
# The session is deliberately LEFT ALIVE when its turn ends. A message resumes a
# stopped-turn session from its transcript, so a finished implementer can still
# answer the tester's question. Whoever runs the phase stops them at the end --
# the id is in <id_file> for exactly that.

set -euo pipefail

MESH_NAME="${1:?Usage: agent_bg.sh <mesh_name> <working_dir> <prompt_file> <summary_file> <log_file> <id_file> [model] [poll_secs]}"
WORKING_DIR="${2:?missing working_dir}"
PROMPT_FILE="${3:?missing prompt_file}"
SUMMARY_FILE="${4:?missing summary_file}"
LOG_FILE="${5:?missing log_file}"
ID_FILE="${6:?missing id_file}"
MODEL="${7:-}"
POLL_SECS="${8:-15}"
EFFORT="${AGENT_BG_EFFORT:-}"
DETACH="${AGENT_BG_DETACH:-0}"

# Never the bare name: the user's interactive shell aliases `claude` to inject
# `--remote-control "<dir> <date>"`, which turns `claude stop <id>` into a new
# session prompted with the words "stop <id>". Scripts do not load that alias,
# but a caller that sources a profile would, and the failure is silent.
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

# A mesh name is an address other agents type. Keep it to something a peer can
# reproduce from the role alone, and reject anything that would need quoting.
if [[ ! "${MESH_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]; then
  echo "agent_bg.sh: invalid mesh name '${MESH_NAME}'." >&2
  exit 2
fi

launch_args=(--bg --name "${MESH_NAME}"
             --dangerously-skip-permissions
             --settings '{"sandbox":{"enabled":false}}')
if [[ -n "${MODEL}" ]]; then
  launch_args+=(--model "${MODEL}")
fi
if [[ -n "${EFFORT}" ]]; then
  launch_args+=(--effort "${EFFORT}")
fi

banner="$(cd "${WORKING_DIR}" && "${CLAUDE_BIN}" "${launch_args[@]}" \
            -- "$(cat "${PROMPT_FILE}")" 2>&1 || true)"

# The banner reads `backgrounded · <id> · <name>`; the separators are multibyte,
# so match the id itself rather than splitting on them.
BG_ID="$(printf '%s\n' "${banner}" | grep -oE '\b[0-9a-f]{8}\b' | head -1 || true)"
if [[ -z "${BG_ID}" ]]; then
  printf '%s\n' "${banner}" > "${LOG_FILE}"
  echo "agent_bg.sh: could not start a background session; banner follows." >&2
  printf '%s\n' "${banner}" >&2
  exit 1
fi
printf '%s\n' "${BG_ID}" > "${ID_FILE}"
if [[ "${DETACH}" == "1" ]]; then
  echo "launched ${MESH_NAME} (${BG_ID})"
  exit 0
fi

agent_status() {
  "${CLAUDE_BIN}" agents --json 2>/dev/null | BG_ID="${BG_ID}" python3 -c '
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
        # A background row now carries BOTH fields, and they disagree: `status`
        # speaks the busy/idle vocabulary the poll loop below matches on, while
        # `state` speaks working/blocked/done and goes stale -- a seat whose turn
        # ended half an hour ago has been observed still reading `working`.
        # Prefer `status`; fall back to `state` only when the row omits it, and
        # translate it, because an untranslated `done` matches no case arm and
        # the loop then spins until the session is stopped by hand.
        status = row.get("status")
        if status:
            print(status)
        else:
            state = row.get("state")
            print({"working": "busy", "done": "idle", "blocked": "idle"}.get(
                state, state or "unknown"))
        break
else:
    print("gone")
' 2>/dev/null || true
}

# `claude logs` prints a rolling tail, so rewrite the log rather than appending:
# the heartbeat digest wants the newest line, and appending a rolling buffer
# every poll would repeat whatever the agent lingered on.
refresh_log() {
  "${CLAUDE_BIN}" logs "${BG_ID}" > "${LOG_FILE}.tmp" 2>/dev/null || return 0
  mv -f "${LOG_FILE}.tmp" "${LOG_FILE}" 2>/dev/null || true
}

# An agent is done when its turn ends. `idle` is also where a finished session
# waits to be resumed by a peer's message, so idle is terminal here and the
# session stays alive on purpose -- see the header.
seen_busy=0
while true; do
  status="$(agent_status)"
  refresh_log
  case "${status}" in
    busy|shell|prompting)
      seen_busy=1
      ;;
    idle)
      # Trust a single idle only after the agent has actually started working;
      # a session polled in the instant between dispatch and first token would
      # otherwise look finished before it began.
      if [[ "${seen_busy}" -eq 1 ]]; then
        break
      fi
      ;;
    gone)
      break
      ;;
  esac
  sleep "${POLL_SECS}"
done

refresh_log
# The delegate writes its own summary (its prompt says so) because --bg has no
# output redirect to capture. Fall back to the visible log so a caller that
# reads the summary never finds nothing at all.
if [[ ! -s "${SUMMARY_FILE}" ]]; then
  if [[ -s "${LOG_FILE}" ]]; then
    tail -c 4000 "${LOG_FILE}" > "${SUMMARY_FILE}" 2>/dev/null || true
  else
    printf 'The background agent %s produced no summary.\n' "${MESH_NAME}" > "${SUMMARY_FILE}"
  fi
fi

if [[ "$(agent_status)" == "gone" ]]; then
  # A session that vanished without writing a summary did not finish its work.
  [[ -s "${SUMMARY_FILE}" ]] || exit 1
fi
exit 0
