#!/usr/bin/env bash
# implement.sh — Invoke the configured delegate agent to implement code changes.
#
# Usage: implement.sh <session_dir> [working_dir] [prompt_file] [task]
#                     [role_description] [pass_kind] [pass_activity] [fix_pass]
#                     <team_role> [mesh_prefix]
#   role_description — 1-2 lines describing this dispatch's responsibility,
#   written as a header block into the shared heartbeat log
#   pass_kind — required; impl, test, fix, or review. Every seat carries one.
#   It is the seat's opening role, stamped on its board register line
#   pass_activity — short user-facing description for the progress header
#   fix_pass — required pass count when pass_kind is fix
#   team_role — required; which member of the phase team this is (impl, test,
#   review). Every dispatch is a team member -- the only two callers are
#   the implementation and fix phases, and both run a team -- so there is one
#   artifact layout rather than a solo one and a team one. Several launchers
#   run at once against one session directory, so every artifact below is
#   written per role, and every member records its own progress pass: the
#   recorder keys passes by seat and closes only that seat's stale pass, so
#   concurrent passes describe their own seats rather than whichever
#   happened to finish last.
#
# Produces:
#   <session_dir>/impl_status_<role>      — "implementing" while running, "implemented" on success, "error" on failure
#   <session_dir>/impl_summary_<role>.txt — the implementation summary, written
#                                     by the delegate itself as its last act
#   <session_dir>/impl_reply_<role>.txt   — the delegate's chat replies, in order,
#                                     on the codex paths; the summary file is
#                                     filled from the last one only when the
#                                     delegate left it empty
#   <session_dir>/impl_agent_<role>.log   — full agent log
#   <session_dir>/impl_agent_<role>       — resolved task, family, agent, and effort
#   <session_dir>/impl_bg_id_<role>       — the background session's short id, on
#                                     the claude path only. The session is left
#                                     alive when its turn ends so a peer's message
#                                     can resume it. Absent on the codex path,
#                                     whose address is a thread id in
#                                     <session_dir>/mesh_roster.json instead.
#   <session_dir>/seats             — every background session this run launched,
#                                     `<id>\t<name>`, appended at launch;
#                                     remove_seats.py removes them at phase end
#   <session_dir>/board.log         — shared coordination board; this launcher
#                                     posts each member's start and end so peers
#                                     learn of them without the unit director,
#                                     which is asleep between progress ticks
#   <session_dir>/heartbeat.log     — shared liveness log for every dispatch in
#                                     this session: a role header block at start,
#                                     [wrapper] beats every 60s while the agent
#                                     pid is alive (each carrying an activity
#                                     digest from the agent log), and [agent]
#                                     narration lines (prompt-instructed)
#   <session_dir>/impl_awake_<role>   — seconds the beat loop has counted for
#                                     this dispatch, which excludes time the
#                                     machine was suspended; stamped on the pass

set -euo pipefail

FOLLOW_TO=""
if [[ "${1:-}" == "--to" ]]; then
  FOLLOW_TO="${2:?Usage: implement.sh --to <seat> <session_dir> ...}"
  shift 2
fi

SESSION_DIR="${1:?Usage: implement.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]}"
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${SESSION_DIR}/implementation_prompt.md}"
SUBTASK="${4:-impl}"
ROLE_DESC="${5:-work order at ${PROMPT_FILE}}"
PASS_KIND="${6:-}"
PASS_ACTIVITY="${7:-${ROLE_DESC%%$'\n'*}}"
FIX_PASS="${8:-0}"
TEAM_ROLE="${9:?Usage: implement.sh needs a team_role (impl, test, review) as its 9th argument}"

# Every seat carries a kind, so an empty one is a dropped argument rather than a
# choice. It used to be tolerated: the launcher skipped start-pass, ran the agent
# normally, and left the seat's previous pass standing. A repair round dispatched
# with the kind on `impl` alone then produced a phase whose ledger held one live
# window and two records closed `error` an hour earlier -- and when that one
# window closed, the recorder refused every progress call for having none open
# while two agents were still posting to the board. Nothing in that sequence
# looked like a launch fault, which is why it costs the run its ledger. Refuse
# it at the argument instead. Whether a pass is *recorded* stays a property of
# the session having recorder state, tested where start-pass is called.
case "${PASS_KIND}" in
  impl|test|fix|review) ;;
  *)
    echo "ERROR: pass_kind must be impl, test, fix, or review; got '${PASS_KIND}'." >&2
    echo "It is the 6th argument and the seat's opening role. A team dispatch gives one to every seat." >&2
    exit 2
    ;;
esac

# Resolving a repair round is an explicit assignment, never a property of the
# pass kind. Keying it off `fix` is what forced seats to misreport their work:
# every repairing seat should record `fix`, but a second one doing so
# would mark one round landed several times over and hand the next review defects
# pre-labelled as repaired. So the unit director names exactly one resolver, and
# the kind goes back to being only a name for the work.
if [[ -n "${PLAN_DELEGATE_RESOLVES_ROUND+set}" ]]; then
  RESOLVES_ROUND="${PLAN_DELEGATE_RESOLVES_ROUND}"
elif [[ "${PASS_KIND}" == "fix" && "${TEAM_ROLE}" == "impl" ]]; then
  # A session that loaded the prompt text before this change sets no signal, and
  # under that text only the impl seat ever carried `fix`. This keeps a run that
  # is already in flight resolving its rounds instead of stalling silently.
  # Remove once no session predating the change can still be running.
  RESOLVES_ROUND=1
else
  RESOLVES_ROUND=0
fi
TASK="delegate.${SUBTASK}"

# The role indexes file paths and board fields, so hold it to a character set
# that can escape neither.
if [[ ! "${TEAM_ROLE}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]]; then
  echo "ERROR: team_role must be alphanumeric/_- and 1-32 chars; got '${TEAM_ROLE}'." >&2
  exit 2
fi
SLOT="_${TEAM_ROLE}"
BEAT_TAG="${SUBTASK}:${TEAM_ROLE}"
BOARD_AGENT="${TEAM_ROLE}"
# Exported here rather than beside the other launcher exports below, because
# start-pass runs well before that point and the recorder reads this to label
# which team slot owns the one pass a phase records. Set it late and the label
# is silently empty rather than wrong, which is worse.
export PLAN_DELEGATE_TEAM_ROLE="${TEAM_ROLE}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The address peers type to reach this member: `<project>-<slot>`, or
# `<project>-fix` for a repair seat. seat_name.sh owns the rule, because the
# unit director writes the same name into each peer's prompt before this runs.
# A run whose unit director loaded the earlier rule tells its peers
# `<session dir basename>-<slot>`, and its board already registers a seat under
# that prefix, so it keeps the prefix for the rest of the run. Remove once no
# run predating seat_name.sh can still be running.
MESH_PREFIX="${10:-}"
LEGACY_PREFIX="$(basename "${SESSION_DIR}")"
if [[ -z "${MESH_PREFIX}" ]] && grep -qF "mesh=${LEGACY_PREFIX}-" "${SESSION_DIR}/board.log" 2>/dev/null; then
  MESH_PREFIX="${LEGACY_PREFIX}"
fi
if [[ -n "${FOLLOW_TO}" ]]; then
  MESH_NAME="${FOLLOW_TO}"
else
  MESH_NAME="$(bash "${SCRIPT_DIR}/seat_name.sh" "${WORKING_DIR}" "${TEAM_ROLE}" "${PASS_KIND}" "${MESH_PREFIX}")"
fi

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.10.
PY="${SCRIPT_DIR}/../lib/py"
SUMMARY_FILE="${SESSION_DIR}/impl_summary${SLOT}.txt"
# Truncate at launch: the "done" post below names this path unconditionally, so a
# seat that reports on the board without writing a summary would otherwise leave
# the previous round's file sitting at exactly the path the board line points to.
REPLY_FILE="${SESSION_DIR}/impl_reply${SLOT}.txt"
STATUS_FILE="${SESSION_DIR}/impl_status${SLOT}"
LOG_FILE="${SESSION_DIR}/impl_agent${SLOT}.log"
AGENT_FILE="${SESSION_DIR}/impl_agent${SLOT}"
BG_ID_FILE="${SESSION_DIR}/impl_bg_id${SLOT}"
BOARD_HELPER="${SCRIPT_DIR}/board.sh"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
AWAKE_FILE="${SESSION_DIR}/impl_awake${SLOT}"
PROGRESS_HELPER="${SCRIPT_DIR}/progress_history.py"
PROGRESS_STATE="${SESSION_DIR}/progress_history_state.json"
FINDINGS_HELPER="${SCRIPT_DIR}/findings.py"
FINDINGS_STATE="${SESSION_DIR}/findings_state.json"
HEARTBEAT_INTERVAL_SECS=60

# -1 says the beat loop counted nothing, which a dispatch shorter than one
# interval always does. The previous dispatch's file is removed before this one
# launches, so a leftover total can never be read as this pass's.
awake_seconds() {
  local counted
  counted="$(cat "${AWAKE_FILE}" 2>/dev/null || true)"
  case "${counted}" in
    ''|*[!0-9]*) echo "-1" ;;
    *) echo "${counted}" ;;
  esac
}

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  exit 1
fi
CODEX_MESH="${PLAN_DELEGATE_CODEX_MESH:-$(_agents_registry_get delegate.options codex_mesh)}"
if [[ "${AGENT_FAMILY}" == "codex" && "${CODEX_MESH}" == "1" ]]; then
  USE_CODEX_MESH=1
else
  USE_CODEX_MESH=0
fi

# One lock covers identity, live-state validation, and the claim. The identity
# written at the original launch is independent of the short-id seats ledger
# and of impl_agent_<slot>, which this launcher rewrites for every pass.
seat_record() {
  "$PY" - "${1}" "${SESSION_DIR}" "${TEAM_ROLE}" "${MESH_NAME}" \
  "${AGENT_FAMILY}" "${AGENT_MODEL}" "${BG_ID_FILE}" \
  "${USE_CODEX_MESH}" "${SCRIPT_DIR}/../agents/codex_mesh.py" <<'PY'
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

action, root_text, slot, name, family, model, bg_file, mesh, mesh_script = sys.argv[1:]
root = Path(root_text)
identity_file = root / f"impl_seat_{slot}.json"
claim_file = root / f"impl_claim_{slot}.json"
lock_file = root / "impl_seat_claim.lock"

def fail(message):
    print(f"implement.sh: {message}", file=sys.stderr)
    raise SystemExit(2)

def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}

def claude_session(short_id):
    directory = Path(os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions")))
    for path in directory.glob("*.json"):
        data = read(path)
        if str(data.get("id", path.stem)) == short_id or data.get("sessionId", "").startswith(short_id):
            if data.get("name") == name:
                return data.get("sessionId", "")
    return ""

with lock_file.open("a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    roster = read(root / "mesh_roster.json")
    entry = roster.get(name, {}) if isinstance(roster, dict) else {}
    ledger = (root / "seats").read_text().splitlines() if (root / "seats").exists() else []
    short_id = next((line.split("\t", 1)[0] for line in ledger if line.split("\t", 1)[-1] == name), "")
    if family == "codex" and isinstance(entry, dict) and entry.get("thread_id"):
        seat_id = entry["thread_id"]
    elif family == "claude" and short_id:
        seat_id = claude_session(short_id)
    else:
        seat_id = ""
    if action == "record":
        if not seat_id:
            raise SystemExit(1)
        identity = {"name": name, "slot": slot, "family": family, "model": model, "seat_id": seat_id}
        identity_file.write_text(json.dumps(identity))
    elif action == "claim":
        identity = read(identity_file)
        if not identity or identity != {"name": name, "slot": slot, "family": family, "model": model, "seat_id": seat_id}:
            fail(f"{name} has no matching durable seat identity for slot {slot}")
        status_path = root / f"impl_status_{slot}"
        claim = read(claim_file)
        pid = claim.get("pid", 0)
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                status_path.write_text("error\n")
                claim_file.unlink(missing_ok=True)
            else:
                fail(f"{name} is claimed by launcher {pid}")
        if status_path.exists() and status_path.read_text().strip() == "implementing":
            fail(f"{name} is already implementing")
        if family == "codex":
            if mesh != "1":
                fail(f"{name} has mesh=none")
            check = subprocess.run(
                [sys.executable, mesh_script, "can-follow", "--session-dir", root_text,
                 "--to", name, "--claim-pid", os.environ["FOLLOW_LAUNCHER_PID"]],
                capture_output=True, text=True,
            )
            if check.returncode != 0:
                fail(check.stderr.strip() or f"{name} is busy or not followable")
        else:
            claude = os.environ.get("CLAUDE_BIN") or str(Path.home() / ".local/bin/claude")
            try:
                result = subprocess.run([claude, "agents", "--json"], capture_output=True, text=True)
            except OSError:
                fail("cannot read claude agents")
            try:
                rows = json.loads(result.stdout)
            except ValueError:
                fail("cannot read claude agents")
            row = next((row for row in rows if row.get("id") == short_id), None)
            if row is None or row.get("status") != "idle":
                fail(f"{name} is busy or gone")
        claim_file.write_text(json.dumps({"pid": int(os.environ["FOLLOW_LAUNCHER_PID"]), "seat": name, "turn_id": ""}))
    elif action == "release":
        claim = read(claim_file)
        if claim.get("pid") == int(os.environ.get("FOLLOW_LAUNCHER_PID", "0")):
            if family == "codex" and mesh == "1":
                subprocess.run(
                    [sys.executable, mesh_script, "release-follow", "--session-dir", root_text,
                     "--to", name, "--claim-pid", os.environ["FOLLOW_LAUNCHER_PID"]],
                    capture_output=True, text=True,
                )
            claim_file.unlink(missing_ok=True)
    elif action == "turn":
        claim = read(claim_file)
        if claim.get("pid") == int(os.environ.get("FOLLOW_LAUNCHER_PID", "0")):
            turn_id = (
                entry.get("turn_id", "") if entry.get("status") == "running" else ""
            ) if family == "codex" else claim.get("turn_id", "")
            if not turn_id:
                raise SystemExit
            claim["turn_id"] = turn_id
            claim_file.write_text(json.dumps(claim))
            print(turn_id)
PY
}

if [[ -n "${FOLLOW_TO}" ]]; then
  export FOLLOW_LAUNCHER_PID="$$"
  seat_record claim
  trap 'seat_record release' EXIT
fi

: > "${SUMMARY_FILE}"
rm -f "${REPLY_FILE}" "${AWAKE_FILE}"
echo "implementing" > "${STATUS_FILE}"

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

if [[ -f "${PROGRESS_STATE}" ]]; then
  if ! PLAN_DELEGATE_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" start-pass \
    --session-dir "${SESSION_DIR}" \
    --pass-kind "${PASS_KIND}" \
    --fix-pass "${FIX_PASS}" \
    --activity "${PASS_ACTIVITY}" \
    --called-task "${TASK}" \
    --called-family "${AGENT_FAMILY}" \
    --called-model "${AGENT_MODEL}" \
    --called-effort "${AGENT_EFFORT:-unset}"; then
    echo "ERROR: unable to record the ${PASS_KIND} pass start." >&2
    echo "error" > "${STATUS_FILE}"
    bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
      "launcher: unable to record the ${PASS_KIND} pass start" || true
    exit 1
  fi
fi

bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" header "${BEAT_TAG} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset})" "${ROLE_DESC}" || true

# Announce the member on the shared board. Peers read the board to learn who is
# running in which role; the unit director sleeps between progress ticks, so
# nothing else would tell them.
# codex delegates run as threads on one app-server instead of as `codex exec`
# processes, which is what makes them addressable. On by default; set the key to
# 0 for the plain launcher. The registry holds the standing choice; the env var
# overrides it for one run. A missing key reads as empty, which fails the test
# below and leaves the plain launcher in place.
# The register line carries the mesh address as well as the role, so a peer that
# joined late can learn who to message without being told at launch. A family
# with no address says so, and that is what stops a peer waiting on a reply that
# can never arrive.
# The two families are reached by different calls, so the address alone is not
# enough: a peer that picks the wrong call sends into nothing and then waits on a
# reply that was never queued.
if [[ "${AGENT_FAMILY}" == "claude" ]]; then
  MESH_FIELD="mesh=${MESH_NAME}; reach=SendMessage"
elif [[ "${USE_CODEX_MESH}" == "1" ]]; then
  MESH_FIELD="mesh=${MESH_NAME}; reach=codex_mesh.py"
else
  MESH_FIELD="mesh=none"
fi
# The opening role is stamped here so the progress table has a real answer from
# second zero, instead of a dash until some agent remembers to call `board.sh
# role`. The opening role is the kind: the unit director picks each seat's kind
# from the Work Order's Seats field, so a `review` seat launched to write opens
# as `impl`, and a `test` seat in a crate with no test lane opens as whatever
# it was given. Nothing here reads the seat name -- the seat name is an
# identity and says nothing about the work.
#
# The stamp is the opening only. Every later `board.sh role` handoff overrides
# it and the recorder reads the whole board history, so stamping the kind here
# freezes nothing. What must stay out is the reverse: reading PASS_KIND *later*
# as the role, which would report every seat in its launch role for the whole
# phase and lose the one movement the table exists to show.
# The kind is one of the four words by the time this runs, so the field is
# always present -- which is what makes a dispatch that lost it legible from
# the board alone: a register line with no `role=` predates this check.
ROLE_FIELD="role=${PASS_KIND}; "
if [[ -n "${FOLLOW_TO}" ]]; then
  LAUNCH_NOTE="follow-up to ${FOLLOW_TO}"
else
  LAUNCH_NOTE="launcher up"
fi
bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" register \
  "${SUBTASK} ${LAUNCH_NOTE} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset}); ${MESH_FIELD}; ${ROLE_FIELD}status in impl_status${SLOT}" || true

# The delegate's own verify.sh runs inherit these and take the cargo token with
# them, so serialization does not depend on the agent remembering a prompt rule.
export PLAN_DELEGATE_BOARD_DIR="${SESSION_DIR}"

# A claude-family delegate launches as a NAMED BACKGROUND session so it joins the
# machine's session mesh: it can message its peers and the unit director, and both
# can message it, mid-run. A --print delegate carries the same ListAgents and
# SendMessage tools but registers nowhere -- two concurrent print sessions in one
# directory each report "no reachable agents" while the other is live -- so the
# tools would be there and reach nobody. A codex delegate reaches the same place
# by a different road: `codex exec` really is unreachable from outside its own
# process, but a thread on a shared `codex app-server` is addressable, so
# codex_mesh.py launches it there instead. With codex_mesh=0 it falls back to
# the plain launcher and coordinates through the board alone.
if [[ -n "${FOLLOW_TO}" ]]; then
  FOLLOW_MESSAGE="${SESSION_DIR}/impl_follow_message${SLOT}.$$.txt"
  cat "${PROMPT_FILE}" > "${FOLLOW_MESSAGE}"
  printf '\n\nAs your last act, write impl_summary%s.txt, then post done on the board.\n' "${SLOT}" >> "${FOLLOW_MESSAGE}"
  if [[ "${AGENT_FAMILY}" == "claude" ]]; then
    bash "${SCRIPT_DIR}/../agents/agent_bg.sh" --attach \
      "${MESH_NAME}" "${WORKING_DIR}" "${FOLLOW_MESSAGE}" "${SUMMARY_FILE}" \
      "${LOG_FILE}" "${BG_ID_FILE}" "${AGENT_MODEL}" &
  elif [[ "${USE_CODEX_MESH}" == "1" ]]; then
    "$PY" "${SCRIPT_DIR}/../agents/codex_mesh.py" follow \
      --session-dir "${SESSION_DIR}" --to "${MESH_NAME}" --claim-pid "${FOLLOW_LAUNCHER_PID}" \
      --message-file "${FOLLOW_MESSAGE}" --summary-file "${SUMMARY_FILE}" \
      --reply-file "${REPLY_FILE}" --log-file "${LOG_FILE}" \
      --model "${AGENT_MODEL}" --effort "${AGENT_EFFORT:-}" \
      --service-tier "${AGENT_SERVICE_TIER:-}" &
  else
    echo "ERROR: ${MESH_NAME} has mesh=none" >&2
    exit 2
  fi
elif [[ "${AGENT_FAMILY}" == "claude" ]]; then
  AGENT_BG_EFFORT="${AGENT_EFFORT:-}" AGENT_BG_LEDGER="${SESSION_DIR}/seats" bash "${SCRIPT_DIR}/../agents/agent_bg.sh" \
    "${MESH_NAME}" "${WORKING_DIR}" "${PROMPT_FILE}" "${SUMMARY_FILE}" \
    "${LOG_FILE}" "${BG_ID_FILE}" "${AGENT_MODEL}" &
elif [[ "${USE_CODEX_MESH}" == "1" ]]; then
  # Same foreground behavior as the plain launcher: codex_mesh.py blocks until the
  # delegate's turn ends, so the wait, heartbeat, and pass recording below are
  # unchanged. What it adds is an address other delegates can send to.
  rm -f "${BG_ID_FILE}"
  "$PY" "${SCRIPT_DIR}/../agents/codex_mesh.py" start \
    --session-dir "${SESSION_DIR}" \
    --name "${MESH_NAME}" \
    --cwd "${WORKING_DIR}" \
    --prompt-file "${PROMPT_FILE}" \
    --summary-file "${SUMMARY_FILE}" \
    --reply-file "${REPLY_FILE}" \
    --log-file "${LOG_FILE}" \
    --model "${AGENT_MODEL}" \
    --effort "${AGENT_EFFORT:-}" \
    --service-tier "${AGENT_SERVICE_TIER:-}" &
else
  rm -f "${BG_ID_FILE}"
  # The plain launcher writes the delegate's last message to its output file,
  # so that file is the reply file, never the summary: see the settle step
  # after the wait below.
  bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
    "${TASK}" write "${WORKING_DIR}" "${PROMPT_FILE}" "${REPLY_FILE}" "${LOG_FILE}" &
fi
AGENT_PID=$!
if [[ -n "${FOLLOW_TO}" ]]; then
  if [[ "${AGENT_FAMILY}" == "codex" ]]; then
    for ((turn_wait=0; turn_wait<30; turn_wait++)); do
      if [[ -n "$(seat_record turn 2>/dev/null || true)" ]]; then
        break
      fi
      kill -0 "${AGENT_PID}" 2>/dev/null || break
      [[ "$(ps -o stat= -p "${AGENT_PID}" 2>/dev/null || true)" == Z* ]] && break
      sleep 1
    done
  fi
elif [[ "${AGENT_FAMILY}" == "claude" || "${USE_CODEX_MESH}" == "1" ]]; then
  for ((seat_wait=0; seat_wait<30; seat_wait++)); do
    if seat_record record 2>/dev/null; then
      break
    fi
    kill -0 "${AGENT_PID}" 2>/dev/null || break
    [[ "$(ps -o stat= -p "${AGENT_PID}" 2>/dev/null || true)" == Z* ]] && break
    sleep 1
  done
fi

# Wrapper beats with an activity digest from the agent log: proves the process
# is alive and names what it is doing even while blocked in a long tool call.
# [agent] lines still come from the delegate itself, per its prompt.
bash "${SCRIPT_DIR}/../agents/heartbeat_watch.sh" \
  "${HEARTBEAT_FILE}" "${BEAT_TAG}" "${AGENT_PID}" "${LOG_FILE}" "${HEARTBEAT_INTERVAL_SECS}" \
  "${AWAKE_FILE}" &
HEARTBEAT_LOOP_PID=$!

AGENT_CODE=0
wait "${AGENT_PID}" || AGENT_CODE=$?
if [[ -n "${FOLLOW_TO}" ]]; then
  if [[ "${AGENT_FAMILY}" == "codex" ]]; then
    seat_record turn >/dev/null || true
  fi
  rm -f "${FOLLOW_MESSAGE}"
elif [[ "${AGENT_FAMILY}" == "claude" || "${USE_CODEX_MESH}" == "1" ]]; then
  seat_record record 2>/dev/null || true
fi

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

# The summary file is the delegate's own, written as its last act, and its last
# chat reply lands in REPLY_FILE instead -- a one-line acknowledgement must not
# replace the summary the reviewers read. A delegate that skipped that act
# leaves the summary empty, and then the reply is all there is. The mesh path
# settles this itself; here it is repeated for the plain launcher and is a no-op
# once the summary has content.
if [[ ! -s "${SUMMARY_FILE}" && -s "${REPLY_FILE}" ]]; then
  cp "${REPLY_FILE}" "${SUMMARY_FILE}"
fi

if [[ "${AGENT_CODE}" -eq 0 ]]; then
  if [[ -f "${PROGRESS_STATE}" ]]; then
    if ! PLAN_DELEGATE_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed \
      --agent-awake-seconds "$(awake_seconds)"; then
      echo "ERROR: unable to record the ${PASS_KIND} pass completion." >&2
      echo "error" > "${STATUS_FILE}"
      bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
        "launcher: unable to record the ${PASS_KIND} pass completion" || true
      exit 1
    fi
  fi
  # Only the launcher watches the worker exit, so only the launcher can say a
  # repair landed. Asking the unit director to record it later leaves a gap it can
  # be killed or compacted inside, and that gap used to resolve as "fixed" --
  # handing the next review a defect pre-labelled as repaired.
  if [[ "${RESOLVES_ROUND}" == "1" && -f "${FINDINGS_STATE}" ]]; then
    if ! "$PY" "${FINDINGS_HELPER}" landed --session-dir "${SESSION_DIR}"; then
      echo "ERROR: unable to record the repair round as landed." >&2
      echo "error" > "${STATUS_FILE}"
      bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
        "launcher: unable to record the repair round as landed" || true
      exit 1
    fi
  fi
  echo "implemented" > "${STATUS_FILE}"
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent finished" || true
  # A member that ends without saying so on the board leaves its peers waiting
  # on work already finished, so the launcher posts it rather than trusting the
  # agent to have posted before it exited. The `launcher:` prefix marks the
  # line as the launcher's: it lands after the seat's own last narration, and
  # the progress report keeps showing those words rather than this boilerplate.
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" done \
    "launcher: ${SUBTASK} finished; summary at impl_summary${SLOT}.txt" || true
  bash "${BOARD_HELPER}" release "${SESSION_DIR}" "${BOARD_AGENT}" cargo >/dev/null 2>&1 || true
else
  LEDGER_CODE=0
  if [[ -f "${PROGRESS_STATE}" ]]; then
    PLAN_DELEGATE_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      --agent-awake-seconds "$(awake_seconds)" \
      || { echo "ERROR: unable to record the ${PASS_KIND} pass error." >&2; LEDGER_CODE=1; }
  fi
  # The attempt stands rather than being refunded: a worker that ran and then
  # failed may have left partial edits behind, and the launcher cannot tell.
  if [[ "${RESOLVES_ROUND}" == "1" && -f "${FINDINGS_STATE}" ]]; then
    "$PY" "${FINDINGS_HELPER}" abandon --session-dir "${SESSION_DIR}" --edits-landed \
      --reason "the ${SUBTASK} worker exited with code ${AGENT_CODE}" \
      || { echo "ERROR: unable to record the repair round as abandoned." >&2; LEDGER_CODE=1; }
  fi
  echo "error" > "${STATUS_FILE}"
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent exited with code ${AGENT_CODE}" || true
  # Same reason as the success path, plus the token: a member killed mid-hold
  # would otherwise hold the cargo token until its hold expired, stalling every
  # peer behind a lock whose owner is already gone.
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
    "launcher: ${SUBTASK} exited with code ${AGENT_CODE}; this seat is down; ledger_error=${LEDGER_CODE}" || true
  bash "${BOARD_HELPER}" release "${SESSION_DIR}" "${BOARD_AGENT}" cargo >/dev/null 2>&1 || true
  exit "${AGENT_CODE}"
fi
