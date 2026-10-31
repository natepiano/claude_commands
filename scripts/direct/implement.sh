#!/usr/bin/env bash
# implement.sh — Invoke the configured worker agent to implement code changes.
#
# Usage: implement.sh [--to <worker>] [--phase <id>] [--fix-cause <text>] <session_dir> [working_dir] [prompt_file] [task]
#                     [role_description] [pass_kind] [pass_activity] [fix_pass]
#                     <team_role> [mesh_prefix]
#   role_description — 1-2 lines describing this dispatch's responsibility,
#   written as a header block into the shared heartbeat log
#   pass_kind — required; impl, test, fix, or review. Every worker carries one.
#   It is the worker's opening role, stamped on its board register line
#   pass_activity — short user-facing description for the progress header
#   fix_pass — required pass count when pass_kind is fix
#   team_role — required; which member of the phase team this is (impl, test,
#   review). Every dispatch is a team member -- the only two callers are
#   the implementation and fix phases, and both run a team -- so there is one
#   artifact layout rather than a solo one and a team one. Several launchers
#   run at once against one session directory, so every artifact below is
#   written per role, and every member records its own progress pass: the
#   recorder keys passes by worker and closes only that worker's stale pass, so
#   concurrent passes describe their own workers rather than whichever
#   happened to finish last.
#
# Produces:
#   <session_dir>/impl_status_<role>      — "implementing" while running, "implemented" on success, "error" on failure
#   <session_dir>/impl_summary_<role>.txt — the implementation summary, written
#                                     by the worker itself as its last act
#   <session_dir>/impl_reply_<role>.txt   — the worker's chat replies, in order,
#                                     on the codex paths; the summary file is
#                                     filled from the last one only when the
#                                     worker left it empty
#   <session_dir>/impl_agent_<role>.log   — full agent log
#   <session_dir>/impl_agent_<role>       — resolved task, family, agent, and effort
#   <session_dir>/impl_bg_id_<role>       — the background session's short id, on
#                                     the claude path only. The session is left
#                                     alive when its turn ends so a peer's message
#                                     can resume it. Absent on the codex path,
#                                     whose address is a thread id in
#                                     <session_dir>/mesh_roster.json instead.
#   <session_dir>/workers             — every background session this run launched,
#                                     `<id>\t<name>`, appended at launch;
#                                     remove_workers.py removes them at phase end
#   <session_dir>/board.log         — shared coordination board; this launcher
#                                     posts each member's start and end so peers
#                                     learn of them without the unit director,
#                                     which is asleep between progress ticks
#   <session_dir>/heartbeat.log     — shared liveness log for every dispatch in
#                                     this session: a role header block at start,
#                                     [wrapper] beats every 60s while the agent
#                                     pid is alive (each carrying an activity
#                                     digest from the agent log)

set -euo pipefail
UNIT_ENV_SHIM="${BASH_SOURCE[0]%/*}/../lib/unit_env.sh"
[[ ! -r "$UNIT_ENV_SHIM" ]] || source "$UNIT_ENV_SHIM"

FOLLOW_TO=""
# A phase running beside another names itself: start-pass stamps its pass with it, and its
# per-role files go to <session_dir>/phases/<id>/.
PHASE_ID=""
FIX_CAUSE=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --to) FOLLOW_TO="${2:?Usage: implement.sh --to <worker> <session_dir> ...}" ;;
    --phase) PHASE_ID="${2:?--phase requires a phase id}" ;;
    --fix-cause) FIX_CAUSE="${2:?--fix-cause requires a failed gate or defect}" ;;
    *) echo "ERROR: unknown implement option '$1'." >&2; exit 2 ;;
  esac
  shift 2
done

SESSION_DIR="${1:?Usage: implement.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]}"
# Two phases running the same role would otherwise write one status, summary and log.
# Run-wide files -- the board, heartbeat, recorder and ledger state, worker identities -- stay
# in the session.
ARTIFACT_DIR="${SESSION_DIR}"
if [[ -n "${PHASE_ID}" ]]; then
  # The directory keeps only the id's letters, digits, `.`, `_` and `-`: `ad hoc` is `adhoc`.
  PHASE_DIR_NAME="${PHASE_ID//[^[:alnum:]._-]/}"
  if [[ -z "${PHASE_DIR_NAME}" || "${PHASE_DIR_NAME}" == . || "${PHASE_DIR_NAME}" == .. ]]; then
    echo "ERROR: phase id names no directory: ${PHASE_ID}" >&2
    exit 2
  fi
  ARTIFACT_DIR="${SESSION_DIR}/phases/${PHASE_DIR_NAME}"
fi
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${ARTIFACT_DIR}/implementation_prompt.md}"
SUBTASK="${4:-impl}"
ROLE_DESC="${5:-work order at ${PROMPT_FILE}}"
PASS_KIND="${6:-}"
PASS_ACTIVITY="${7:-${ROLE_DESC%%$'\n'*}}"
FIX_PASS="${8:-0}"
TEAM_ROLE="${9:?Usage: implement.sh needs a team_role (impl, test, review) as its 9th argument}"

# Every worker carries a kind, so an empty one is a dropped argument rather than a
# choice. It used to be tolerated: the launcher skipped start-pass, ran the agent
# normally, and left the worker's previous pass standing. A repair round dispatched
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
    echo "It is the 6th argument and the worker's opening role. A team dispatch gives one to every worker." >&2
    exit 2
    ;;
esac

# Resolving a repair round is an explicit assignment, never a property of the
# pass kind. Keying it off `fix` is what forced workers to misreport their work:
# every repairing worker should record `fix`, but a second one doing so
# would mark one round landed several times over and hand the next review defects
# pre-labelled as repaired. So the unit director names exactly one resolver, and
# the kind goes back to being only a name for the work.
if [[ -n "${UNIT_RESOLVES_ROUND+set}" ]]; then
  RESOLVES_ROUND="${UNIT_RESOLVES_ROUND}"
elif [[ "${PASS_KIND}" == "fix" && "${TEAM_ROLE}" == "impl" ]]; then
  # A session that loaded the prompt text before this change sets no signal, and
  # under that text only the impl worker ever carried `fix`. This keeps a run that
  # is already in flight resolving its rounds instead of stalling silently.
  # Remove once no session predating the change can still be running.
  RESOLVES_ROUND=1
else
  RESOLVES_ROUND=0
fi
TASK="direct.${SUBTASK}"

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
export UNIT_TEAM_ROLE="${TEAM_ROLE}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The address peers type to reach this member: `<project>-<slot>`, or
# `<project>-fix` for a repair worker. worker_name.sh owns the rule, because the
# unit director writes the same name into each peer's prompt before this runs.
# A run whose unit director loaded the earlier rule tells its peers
# `<session dir basename>-<slot>`, and its board already registers a worker under
# that prefix, so it keeps the prefix for the rest of the run. Remove once no
# run predating worker_name.sh can still be running.
MESH_PREFIX="${10:-}"
LEGACY_PREFIX="$(basename "${SESSION_DIR}")"
if [[ -z "${MESH_PREFIX}" ]] && grep -qF "mesh=${LEGACY_PREFIX}-" "${SESSION_DIR}/board.log" 2>/dev/null; then
  MESH_PREFIX="${LEGACY_PREFIX}"
fi
if [[ -n "${FOLLOW_TO}" ]]; then
  MESH_NAME="${FOLLOW_TO}"
else
  MESH_NAME="$(bash "${SCRIPT_DIR}/worker_name.sh" "${WORKING_DIR}" "${TEAM_ROLE}" "${PASS_KIND}" "${MESH_PREFIX}")"
fi

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.11.
PY="${SCRIPT_DIR}/../lib/py"
"$PY" "${SCRIPT_DIR}/worker_state.py" "${SESSION_DIR}"
mkdir -p "${ARTIFACT_DIR}"
# Every dispatch names the run's director in the run folder (unit_runs.py), which the
# production tree reads: a marker names one run per director, and a director runs phases side by side.
"$PY" "${SCRIPT_DIR}/../lib/unit_runs.py" record-director "${SESSION_DIR}" || true
SUMMARY_FILE="${ARTIFACT_DIR}/impl_summary${SLOT}.txt"
# Truncate at launch: the "done" post below names this path unconditionally, so a
# worker that reports on the board without writing a summary would otherwise leave
# the previous round's file sitting at exactly the path the board line points to.
REPLY_FILE="${ARTIFACT_DIR}/impl_reply${SLOT}.txt"
STATUS_FILE="${ARTIFACT_DIR}/impl_status${SLOT}"
LOG_FILE="${ARTIFACT_DIR}/impl_agent${SLOT}.log"
AGENT_FILE="${ARTIFACT_DIR}/impl_agent${SLOT}"
BG_ID_FILE="${ARTIFACT_DIR}/impl_bg_id${SLOT}"
BOARD_HELPER="${SCRIPT_DIR}/board.sh"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
PROGRESS_HELPER="${SCRIPT_DIR}/progress_history.py"
PROGRESS_STATE="${SESSION_DIR}/progress_history_state.json"
FINDINGS_HELPER="${SCRIPT_DIR}/findings.py"
FINDINGS_STATE="${SESSION_DIR}/findings_state.json"
HEARTBEAT_INTERVAL_SECS=60
DIRECT_CONFIG="${UNIT_CONFIG:-${HOME}/.claude/config/direct.conf}"
COMPACT_ABOVE_TOKENS=""
if [[ -r "${DIRECT_CONFIG}" ]]; then
  COMPACT_ABOVE_TOKENS="$(sed -n \
    's/^UNIT_COMPACT_ABOVE_TOKENS=\([0-9][0-9]*\).*$/\1/p' \
    "${DIRECT_CONFIG}" | tail -n 1)"
fi
if [[ ! "${COMPACT_ABOVE_TOKENS}" =~ ^[0-9]+$ ]] || (( COMPACT_ABOVE_TOKENS <= 0 )); then
  echo "ERROR: UNIT_COMPACT_ABOVE_TOKENS in ${DIRECT_CONFIG} must be a positive integer." >&2
  exit 2
fi

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  exit 1
fi
CODEX_MESH="${UNIT_CODEX_MESH:-$(_agents_registry_get direct.options codex_mesh)}"
if [[ "${AGENT_FAMILY}" == "codex" && "${CODEX_MESH}" == "1" ]]; then
  USE_CODEX_MESH=1
else
  USE_CODEX_MESH=0
fi

# One lock covers identity, live-state validation, and the claim. The identity
# written at the original launch is independent of the short-id workers ledger
# and of impl_agent_<slot>, which this launcher rewrites for every pass.
worker_record() {
  "$PY" - "${1}" "${SESSION_DIR}" "${TEAM_ROLE}" "${MESH_NAME}" \
  "${AGENT_FAMILY}" "${AGENT_MODEL}" "${BG_ID_FILE}" \
  "${USE_CODEX_MESH}" "${SCRIPT_DIR}/../agents/codex_mesh.py" \
  "${BOARD_HELPER}" "${COMPACT_ABOVE_TOKENS}" "${PASS_KIND}" <<'PY'
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

(
    action, root_text, slot, name, family, model, bg_file, mesh, mesh_script,
    board_helper, compact_above_text, pass_kind,
) = sys.argv[1:]
root = Path(root_text)
compact_above = int(compact_above_text)
claim_file = root / f"impl_claim_{slot}.json"
sys.path.insert(0, str(Path(mesh_script).resolve().parents[2]))
from scripts.astra import design_roles
sys.path.insert(0, str(Path(mesh_script).parent.parent / "direct"))
from worker_state import worker_claim_lock, worker_ledger, worker_record_path

def fail(message):
    print(f"implement.sh: {message}", file=sys.stderr)
    raise SystemExit(2)

def post(message):
    subprocess.run(
        ["bash", board_helper, "post", root_text, slot, "status", message],
        capture_output=True, text=True,
    )

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

with worker_claim_lock(root):
    identity_file = worker_record_path(root, name, slot, role=pass_kind)
    roster = read(root / "mesh_roster.json")
    entry = roster.get(name, {}) if isinstance(roster, dict) else {}
    ledger_path = worker_ledger(root)
    ledger = ledger_path.read_text().splitlines() if ledger_path.exists() else []
    short_id = next((line.split("\t", 1)[0] for line in ledger if line.split("\t", 1)[-1] == name), "")
    if family == "codex" and isinstance(entry, dict) and entry.get("thread_id"):
        worker_id = entry["thread_id"]
    elif family == "claude" and short_id:
        worker_id = claude_session(short_id)
    else:
        worker_id = ""
    if action == "record":
        if not worker_id:
            raise SystemExit(1)
        identity = {"name": name, "slot": slot, "family": family, "model": model, "worker_id": worker_id}
        identity_file.write_text(json.dumps(identity))
    elif action == "claim":
        identity = read(identity_file)
        expected = {"name": name, "slot": slot, "family": family, "model": model, "worker_id": worker_id}
        # A worker with no identity record (an old run, or a reopened worker that keeps only its
        # roster thread) records one from its roster thread.
        if not identity and worker_id:
            identity_file.write_text(json.dumps(expected))
            identity = expected
        # A resumed worker keeps the model it was recorded with: the registry row may have
        # changed since, and the worker is still the same thread (rca of 2026-10-23).
        stored_model = identity.get("model")
        if isinstance(stored_model, str) and stored_model:
            expected["model"] = stored_model
        # Role is not durable identity. Older records may hold it, so drop it before the compare.
        identity.pop("role", None)
        # A worker recorded under another slot stays refused by this comparison.
        if identity != expected:
            fail(f"{name} has no matching durable worker identity for slot {slot}")
        # The identity check keeps the stored model; each turn runs at the current default.
        print(model)
        status_path = root / f"impl_status_{slot}"
        claim = read(claim_file)
        pid = claim.get("pid", 0)
        stale_claim = False
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                stale_claim = True
                claim_file.unlink(missing_ok=True)
            else:
                fail(f"{name} is claimed by launcher {pid}")
        if stale_claim or (status_path.exists() and status_path.read_text().strip() == "implementing"):
            launcher = entry.get("launcher_pid", 0) if isinstance(entry, dict) else 0
            if isinstance(launcher, int) and launcher > 0:
                try:
                    os.kill(launcher, 0)
                except ProcessLookupError:
                    pass
                else:
                    fail(f"{name} is already implementing")
            status_path.write_text("error\n")
        if family == "codex":
            if mesh != "1":
                fail(f"{name} has mesh=none")
            if entry.get("transferring_role") or entry.get("role_redirect") or design_roles.transfer_route(
                    str(entry.get("thread_id", "")), str(entry.get("held_design_id", ""))):
                fail(f"{name}: TransferringRole; use its destination holder")
            if entry.get("held_design_id"):
                fail(f"{name}: held roles resume through design_roles.py")
            context_tokens = entry.get("context_tokens")
            if isinstance(context_tokens, int) and context_tokens > compact_above:
                compact = subprocess.run(
                    [sys.executable, mesh_script, "compact", "--session-dir", root_text,
                     "--to", name],
                    capture_output=True, text=True,
                )
                if compact.returncode == 0:
                    line = compact.stdout.strip() or f"compacted {name}"
                    post(f"launcher: {line}")
                else:
                    detail = (compact.stderr.strip() or compact.stdout.strip()
                              or f"exit {compact.returncode}").splitlines()[-1]
                    # Exit 3: a turn is still live on the worker's thread, so a follow-up would be refused.
                    if compact.returncode == 3:
                        post(f"launcher: {name} still has a live turn after compaction: {detail}; "
                             "follow-up not sent")
                        fail(f"{name} still has a live turn after compaction; end the worker "
                             f"(codex_mesh.py end --to {name}) and start a fresh one")
                    if compact.returncode == 4:
                        post(f"launcher: {detail}; follow-up not sent")
                        fail(f"{name}: roster changed during compaction; follow-up not sent; "
                             f"inspect the worker and use codex_mesh.py reopen --to {name} if idle")
                    post(f"launcher: compaction failed for {name}: {detail}; continuing follow-up")
            check = subprocess.run(
                [sys.executable, mesh_script, "can-follow", "--session-dir", root_text,
                 "--to", name, "--claim-pid", os.environ["FOLLOW_LAUNCHER_PID"],
                 "--role", pass_kind],
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
        claim_file.write_text(json.dumps({"pid": int(os.environ["FOLLOW_LAUNCHER_PID"]), "worker": name, "turn_id": ""}))
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
  AGENT_MODEL="$(worker_record claim)"
  trap 'worker_record release' EXIT
fi

# A phase claims its files before any worker edits them, wherever the worker runs
# (phase_reservation.py claim): an unclaimed edit drifts to another reservation.
if [[ -n "${PHASE_ID}" ]] && ! "$PY" "${SCRIPT_DIR}/phase_reservation.py" claim \
  --session-dir "${SESSION_DIR}" --phase "${PHASE_ID}" --repo "${WORKING_DIR}" >>"${LOG_FILE}" 2>&1; then
  echo "ERROR: phase ${PHASE_ID} could not claim its files before its worker opens; see ${LOG_FILE}." >&2
  echo "error" > "${STATUS_FILE}"
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
    "launcher: phase ${PHASE_ID} could not claim its files; its worker did not open" || true
  exit 1
fi

: > "${SUMMARY_FILE}"
rm -f "${REPLY_FILE}"
echo "implementing" > "${STATUS_FILE}"

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

if [[ -f "${PROGRESS_STATE}" ]]; then
  if ! UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" start-pass \
    --session-dir "${SESSION_DIR}" \
    --pass-kind "${PASS_KIND}" \
    --prompt-file "${PROMPT_FILE}" \
    --fix-cause "${FIX_CAUSE}" \
    --fix-pass "${FIX_PASS}" \
    --activity "${PASS_ACTIVITY}" \
    --called-task "${TASK}" \
    --worker-name "${MESH_NAME}" \
    --called-family "${AGENT_FAMILY}" \
    --called-model "${AGENT_MODEL}" \
    --called-effort "${AGENT_EFFORT:-unset}" \
    ${PHASE_ID:+--phase-id "${PHASE_ID}"}; then
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
# codex workers run as threads on one app-server instead of as `codex exec`
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
# role`. The opening role is the kind: the unit director picks each worker's kind
# from the Work Order's Workers field, so a `review` worker launched to write opens
# as `impl`, and a `test` worker in a crate with no test lane opens as whatever
# it was given. Nothing here reads the worker name -- the worker name is an
# identity and says nothing about the work.
#
# The stamp is the opening only. Every later `board.sh role` handoff overrides
# it and the recorder reads the whole board history, so stamping the kind here
# freezes nothing. What must stay out is the reverse: reading PASS_KIND *later*
# as the role, which would report every worker in its launch role for the whole
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

# The worker's own verify.sh runs inherit these and take the cargo token with
# them, so serialization does not depend on the agent remembering a prompt rule.
export UNIT_RUN_DIR="${SESSION_DIR}"
# Test temp folders (mktemp, tempfile, Rust .tmpXXXXXX) land in the run folder and end with it;
# the hourly tmp sweep judges each entry there with its 4 h rule.
mkdir -p "${SESSION_DIR}/tmp"
export TMPDIR="${SESSION_DIR}/tmp"

# A claude-family worker launches as a NAMED BACKGROUND session so it joins the
# machine's session mesh: it can message its peers and the unit director, and both
# can message it, mid-run. A --print worker carries the same ListAgents and
# SendMessage tools but registers nowhere -- two concurrent print sessions in one
# directory each report "no reachable agents" while the other is live -- so the
# tools would be there and reach nobody. A codex worker reaches the same place
# by a different road: `codex exec` really is unreachable from outside its own
# process, but a thread on a shared `codex app-server` is addressable, so
# codex_mesh.py launches it there instead. With codex_mesh=0 it falls back to
# the plain launcher and coordinates through the board alone.
if [[ -n "${FOLLOW_TO}" ]]; then
  FOLLOW_MESSAGE="${ARTIFACT_DIR}/impl_follow_message${SLOT}.$$.txt"
  cat "${PROMPT_FILE}" > "${FOLLOW_MESSAGE}"
  printf '\n\nAs your last act, write impl_summary%s.txt, then post done on the board.\n' "${SLOT}" >> "${FOLLOW_MESSAGE}"
  if [[ "${AGENT_FAMILY}" == "claude" ]]; then
    bash "${SCRIPT_DIR}/../agents/agent_bg.sh" --attach \
      "${MESH_NAME}" "${WORKING_DIR}" "${FOLLOW_MESSAGE}" "${SUMMARY_FILE}" \
      "${LOG_FILE}" "${BG_ID_FILE}" "${AGENT_MODEL}" &
  elif [[ "${USE_CODEX_MESH}" == "1" ]]; then
    "$PY" "${SCRIPT_DIR}/../agents/codex_mesh.py" follow \
      --session-dir "${SESSION_DIR}" --to "${MESH_NAME}" --claim-pid "${FOLLOW_LAUNCHER_PID}" \
      --role "${PASS_KIND}" --cwd "${WORKING_DIR}" \
      --message-file "${FOLLOW_MESSAGE}" --summary-file "${SUMMARY_FILE}" \
      --reply-file "${REPLY_FILE}" --log-file "${LOG_FILE}" \
      --task "${TASK}" --service-tier "${AGENT_SERVICE_TIER:-}" &
  else
    echo "ERROR: ${MESH_NAME} has mesh=none" >&2
    exit 2
  fi
elif [[ "${AGENT_FAMILY}" == "claude" ]]; then
  AGENT_BG_WORKERS=1 AGENT_BG_EFFORT="${AGENT_EFFORT:-}" AGENT_BG_LEDGER="${SESSION_DIR}/workers" bash "${SCRIPT_DIR}/../agents/agent_bg.sh" \
    "${MESH_NAME}" "${WORKING_DIR}" "${PROMPT_FILE}" "${SUMMARY_FILE}" \
    "${LOG_FILE}" "${BG_ID_FILE}" "${AGENT_MODEL}" &
elif [[ "${USE_CODEX_MESH}" == "1" ]]; then
  # Same foreground behavior as the plain launcher: codex_mesh.py blocks until the
  # worker's turn ends, so the wait, heartbeat, and pass recording below are
  # unchanged. What it adds is an address other workers can send to.
  rm -f "${BG_ID_FILE}"
  "$PY" "${SCRIPT_DIR}/../agents/codex_mesh.py" start \
    --session-dir "${SESSION_DIR}" \
    --name "${MESH_NAME}" \
    --role "${PASS_KIND}" \
    --cwd "${WORKING_DIR}" \
    --prompt-file "${PROMPT_FILE}" \
    --summary-file "${SUMMARY_FILE}" \
    --reply-file "${REPLY_FILE}" \
    --log-file "${LOG_FILE}" \
    --task "${TASK}" --service-tier "${AGENT_SERVICE_TIER:-}" &
else
  rm -f "${BG_ID_FILE}"
  # The plain launcher writes the worker's last message to its output file,
  # so that file is the reply file, never the summary: see the settle step
  # after the wait below.
  bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
    "${TASK}" write "${WORKING_DIR}" "${PROMPT_FILE}" "${REPLY_FILE}" "${LOG_FILE}" &
fi
AGENT_PID=$!
# Defaults remain thirty probes one second apart; fixtures probe on a short
# interval with enough attempts to bound hangs generously under load.
if [[ -n "${FOLLOW_TO}" ]]; then
  if [[ "${AGENT_FAMILY}" == "codex" ]]; then
    for ((turn_wait=0; turn_wait<${UNIT_WORKER_WAIT_ATTEMPTS:-30}; turn_wait++)); do
      if [[ -n "$(worker_record turn 2>/dev/null || true)" ]]; then
        break
      fi
      kill -0 "${AGENT_PID}" 2>/dev/null || break
      [[ "$(ps -o stat= -p "${AGENT_PID}" 2>/dev/null || true)" == Z* ]] && break
      sleep "${UNIT_WORKER_POLL_SECS:-1}"
    done
  fi
elif [[ "${AGENT_FAMILY}" == "claude" || "${USE_CODEX_MESH}" == "1" ]]; then
  for ((worker_wait=0; worker_wait<${UNIT_WORKER_WAIT_ATTEMPTS:-30}; worker_wait++)); do
    if worker_record record 2>/dev/null; then
      break
    fi
    kill -0 "${AGENT_PID}" 2>/dev/null || break
    [[ "$(ps -o stat= -p "${AGENT_PID}" 2>/dev/null || true)" == Z* ]] && break
    sleep "${UNIT_WORKER_POLL_SECS:-1}"
  done
fi

# Wrapper beats with an activity digest from the agent log: proves the process
# is alive and names what it is doing even while blocked in a long tool call.
bash "${SCRIPT_DIR}/../agents/heartbeat_watch.sh" \
  "${HEARTBEAT_FILE}" "${BEAT_TAG}" "${AGENT_PID}" "${LOG_FILE}" "${HEARTBEAT_INTERVAL_SECS}" &
HEARTBEAT_LOOP_PID=$!

AGENT_CODE=0
wait "${AGENT_PID}" || AGENT_CODE=$?
if [[ -n "${FOLLOW_TO}" ]]; then
  if [[ "${AGENT_FAMILY}" == "codex" ]]; then
    worker_record turn >/dev/null || true
  fi
  rm -f "${FOLLOW_MESSAGE}"
elif [[ "${AGENT_FAMILY}" == "claude" || "${USE_CODEX_MESH}" == "1" ]]; then
  worker_record record 2>/dev/null || true
fi

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

# The summary file is the worker's own, written as its last act, and its last
# chat reply lands in REPLY_FILE instead -- a one-line acknowledgement must not
# replace the summary the reviewers read. A worker that skipped that act
# leaves the summary empty, and then the reply is all there is. The mesh path
# settles this itself; here it is repeated for the plain launcher and is a no-op
# once the summary has content.
if [[ ! -s "${SUMMARY_FILE}" && -s "${REPLY_FILE}" ]]; then
  cp "${REPLY_FILE}" "${SUMMARY_FILE}"
fi

if [[ "${AGENT_CODE}" -eq 0 ]]; then
  if [[ -f "${PROGRESS_STATE}" ]]; then
    if ! UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed; then
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
    # The phase is named so a repair in an older open phase lands on its own
    # ledger, not on the newest phase's.
    if ! "$PY" "${FINDINGS_HELPER}" landed --session-dir "${SESSION_DIR}" \
        ${PHASE_ID:+--phase "${PHASE_ID}"}; then
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
  # line as the launcher's: it lands after the worker's own last narration, and
  # the progress report keeps showing those words rather than this boilerplate.
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" done \
    "launcher: ${SUBTASK} finished; summary at impl_summary${SLOT}.txt" || true
  bash "${BOARD_HELPER}" release "${SESSION_DIR}" "${BOARD_AGENT}" cargo >/dev/null 2>&1 || true
else
  LEDGER_CODE=0
  if [[ -f "${PROGRESS_STATE}" ]]; then
    UNIT_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      || { echo "ERROR: unable to record the ${PASS_KIND} pass error." >&2; LEDGER_CODE=1; }
  fi
  # The attempt stands rather than being refunded: a worker that ran and then
  # failed may have left partial edits behind, and the launcher cannot tell.
  if [[ "${RESOLVES_ROUND}" == "1" && -f "${FINDINGS_STATE}" ]]; then
    "$PY" "${FINDINGS_HELPER}" abandon --session-dir "${SESSION_DIR}" --edits-landed \
      ${PHASE_ID:+--phase "${PHASE_ID}"} --reason "the ${SUBTASK} worker exited with code ${AGENT_CODE}" \
      || { echo "ERROR: unable to record the repair round as abandoned." >&2; LEDGER_CODE=1; }
  fi
  echo "error" > "${STATUS_FILE}"
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent exited with code ${AGENT_CODE}" || true
  # Same reason as the success path, plus the token: a member killed mid-hold
  # would otherwise hold the cargo token until its hold expired, stalling every
  # peer behind a lock whose owner is already gone.
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${BOARD_AGENT}" blocked \
    "launcher: ${SUBTASK} exited with code ${AGENT_CODE}; this worker is down; ledger_error=${LEDGER_CODE}" || true
  bash "${BOARD_HELPER}" release "${SESSION_DIR}" "${BOARD_AGENT}" cargo >/dev/null 2>&1 || true
  exit "${AGENT_CODE}"
fi
