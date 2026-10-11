#!/usr/bin/env bash
# review.sh — Invoke the configured delegate agent for a review.
#
# Usage: review.sh [--to <seat> | --slot <new-slot>] <session_dir>
#                  [working_dir] [prompt_file] [task]
#                  [role_description] [pass_activity] [pass_index] [lens]
#                  [early_ready_file]
#   --to reuses an open Codex seat. --slot opens a role-named Codex seat for
#   this review and leaves its thread open for later `--to` dispatches.
#   role_description — 1-2 lines describing this review's responsibility,
#   written as a header block into the shared heartbeat log
#   pass_activity — short user-facing description for the progress header
#   pass_index — 1 for a phase's broad review, incrementing for each closure
#   review after it. Artifacts are written per index and never overwritten:
#   a run that failed to converge can be read back round by round.
#   lens — which reading this reviewer is doing, when a phase's broad review
#   runs its three lenses at once: adversary, contract, or craft (code quality
#   by the style guide). It suffixes every artifact below, so concurrent
#   reviewers never overwrite each other, and it selects the seat the pass
#   records under. The seat assignment is fixed, not meaningful.
#   Empty is the single-reviewer layout: unsuffixed names, no seat, which is
#   what a closure review and a solo broad review still run.
#   early_ready_file — early-launch mode: the reviewer starts while the
#   implementer is still running, so its start-pass is deferred until this
#   sentinel appears (the recorder closes any active pass as interrupted when
#   a new one starts, and the implementation pass is still open at launch).
#   The unit director creates the sentinel after the implementation pass has
#   finished and the final diff is written.
#
# Produces (every name below takes a `_<lens>` suffix when a lens is given):
#   <session_dir>/review_status            — "reviewing" while running, "reviewed" on success, "error" on failure
#   <session_dir>/review_pid               — this wrapper's pid, so a progress
#                                            report can tell an early-launched
#                                            reviewer that is still working from
#                                            one that was killed
#   <session_dir>/review_findings_<N>.txt  — review findings for pass N
#   <session_dir>/review_agent_<N>.log     — full agent log for pass N
#   <session_dir>/review_findings.txt      — symlink to the current pass's findings
#   <session_dir>/review_agent.log         — symlink to the current pass's log
#   <session_dir>/review_agent         — resolved task, family, agent, and effort
#   <session_dir>/heartbeat.log        — shared with implement.sh: role header at
#                                        start + [wrapper] beats every 60s, each
#                                        carrying an activity digest decoded from
#                                        the reviewer's streamed log. Fresh
#                                        reviewers are read-only; seated reviewers
#                                        are checked for an unchanged tree after
#                                        their turn.
#   <session_dir>/review_awake         — seconds the beat loop has counted for
#                                        this dispatch, which excludes time the
#                                        machine was suspended; stamped on the
#                                        pass. An early launch starts the loop
#                                        before its pass opens, so the total can
#                                        exceed the pass it is recorded on.

set -euo pipefail

FOLLOW_TO=""
NEW_SLOT=""
case "${1:-}" in
  --to)
    FOLLOW_TO="${2:?Usage: review.sh --to <seat> <session_dir> ...}"
    shift 2
    ;;
  --slot)
    NEW_SLOT="${2:?Usage: review.sh --slot <new-slot> <session_dir> ...}"
    shift 2
    ;;
esac

SESSION_DIR="${1:?Usage: review.sh <session_dir> [working_dir] [prompt_file] [task] [role_description]}"
WORKING_DIR="${2:-$(pwd)}"
PROMPT_FILE="${3:-${SESSION_DIR}/review_prompt.md}"
SUBTASK="${4:-review}"
ROLE_DESC="${5:-blind review of the current diff against its spec}"
PASS_ACTIVITY="${6:-reviewing the current diff against its work order}"
PASS_INDEX="${7:-1}"
LENS="${8:-}"
LENS_ARGS=()
if [[ -n "${LENS}" ]]; then
  LENS_ARGS=(--lens "${LENS}")
fi
EARLY_READY_FILE="${9:-}"
TASK="delegate.${SUBTASK}"

case "${PASS_INDEX}" in
  ''|*[!0-9]*|0) PASS_INDEX=1 ;;
esac

# A closed set, like the pass kinds: an unrecognized lens would suffix artifacts
# nobody reads and record a pass under a seat no column renders, both silently.
case "${LENS}" in
  '') LENS_SLOT='' ;;
  adversary) LENS_SLOT=test ;;
  contract) LENS_SLOT=impl ;;
  # No writer sits in `review` since the three-seat team was folded into two,
  # so the third lens takes it and its pass never closes a writer's window.
  craft) LENS_SLOT=review ;;
  *)
    echo "ERROR: unknown review lens '${LENS}' (adversary, contract, craft)" >&2
    exit 2
    ;;
esac

# A seated pass belongs to the chair that actually ran it, not to the lens's
# historical default chair. Seat names end in their semantic slot (`impl`,
# `test`, `review`, `fix2`); solo reviewers retain the lens mapping above.
if [[ -n "${FOLLOW_TO}" ]]; then
  TEAM_SLOT="${FOLLOW_TO##*-}"
  if [[ ! "${TEAM_SLOT}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]]; then
    echo "ERROR: cannot derive a valid team slot from review seat '${FOLLOW_TO}'." >&2
    exit 2
  fi
elif [[ -n "${NEW_SLOT}" ]]; then
  TEAM_SLOT="${NEW_SLOT}"
  if [[ ! "${TEAM_SLOT}" =~ ^[A-Za-z][A-Za-z0-9_]{0,31}$ ]]; then
    echo "ERROR: new review slot must be role-named and use 1-32 alphanumeric/_ characters; got '${NEW_SLOT}'." >&2
    exit 2
  fi
else
  TEAM_SLOT="${LENS_SLOT}"
fi

SUFFIX="${LENS:+_${LENS}}"
# What the shared heartbeat log tags this reviewer's beats with. Three lenses
# run at once against one log, and `review` on every line cannot be read.
BEAT_TAG="${SUBTASK}${LENS:+:${LENS}}"
# The seat this pass records under, so concurrent reviewers key separate pass
# records instead of each closing the last as interrupted. Exported even when the
# lens is absent and the seat with it: the lens decides this launcher's seat, and
# leaving the variable unset instead would let an inherited one decide it, which
# is how a lone reviewer ends up closing a live seat's pass.
export PLAN_DELEGATE_TEAM_ROLE="${TEAM_SLOT}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${FOLLOW_TO}" ]]; then
  REVIEW_SEAT="${FOLLOW_TO}"
elif [[ -n "${NEW_SLOT}" ]]; then
  REVIEW_SEAT="$(bash "${SCRIPT_DIR}/seat_name.sh" "${WORKING_DIR}" "${NEW_SLOT}" review)"
else
  REVIEW_SEAT=""
fi

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.10.
PY="${SCRIPT_DIR}/../lib/py"
FINDINGS_FILE="${SESSION_DIR}/review_findings_${PASS_INDEX}${SUFFIX}.txt"
STATUS_FILE="${SESSION_DIR}/review_status${SUFFIX}"
LOG_FILE="${SESSION_DIR}/review_agent_${PASS_INDEX}${SUFFIX}.log"
REPLY_FILE="${SESSION_DIR}/review_reply_${PASS_INDEX}${SUFFIX}.$$.txt"

AGENT_FILE="${SESSION_DIR}/review_agent${SUFFIX}"
HEARTBEAT_HELPER="${SCRIPT_DIR}/../agents/heartbeat.sh"
BOARD_HELPER="${SCRIPT_DIR}/board.sh"
HEARTBEAT_FILE="${SESSION_DIR}/heartbeat.log"
AWAKE_FILE="${SESSION_DIR}/review_awake${SUFFIX}"
PROGRESS_HELPER="${SCRIPT_DIR}/progress_history.py"
PROGRESS_STATE="${SESSION_DIR}/progress_history_state.json"
HEARTBEAT_INTERVAL_SECS=60

initialize_review_artifacts() {
  # Every reader of the unnumbered paths — the unit director's mid-run
  # preemption read, the heartbeat digest — keeps working while the per-pass
  # history is kept.
  ln -sfn "review_findings_${PASS_INDEX}${SUFFIX}.txt" "${SESSION_DIR}/review_findings${SUFFIX}.txt"
  ln -sfn "review_agent_${PASS_INDEX}${SUFFIX}.log" "${SESSION_DIR}/review_agent${SUFFIX}.log"
  rm -f "${AWAKE_FILE}" "${REPLY_FILE}"
  if [[ -n "${REVIEW_SEAT}" ]]; then
    : > "${FINDINGS_FILE}"
  fi
  echo "reviewing" > "${STATUS_FILE}"
  # Only meaningful while an early launch is showing in the progress report,
  # and read there only if written after the review was armed.
  echo "$$" > "${SESSION_DIR}/review_pid${SUFFIX}"
}

# -1 says the beat loop counted nothing, which a review shorter than one
# interval always does. The previous review's file is removed before this one
# launches, so a leftover total can never be read as this pass's.
awake_seconds() {
  local counted
  counted="$(cat "${AWAKE_FILE}" 2>/dev/null || true)"
  case "${counted}" in
    ''|*[!0-9]*) echo "-1" ;;
    *) echo "${counted}" ;;
  esac
}

# A seated reviewer opens a new occupancy of a chair a writer has just left.
# Without a line of its own the note under the progress table keeps showing the
# words of the delegate that sat there while the code was being written, aged by
# however long the review has run -- a line describing the wrong agent, which
# reads as a stalled one. A lone reviewer holds no seat and posts nothing.
# A fresh blind reviewer has no peers and no address, so its line says
# `mesh=none`. A seated review names the reused mesh address instead.
board_post() {
  [[ -n "${TEAM_SLOT}" ]] || return 0
  bash "${BOARD_HELPER}" post "${SESSION_DIR}" "${TEAM_SLOT}" "$1" "$2" || true
}

# Preserve the old launcher's observable failure path: a solo review creates
# its artifacts before agent resolution. A seated review claims first, so a
# busy-seat refusal cannot overwrite another review's status or findings.
if [[ -z "${REVIEW_SEAT}" ]]; then
  initialize_review_artifacts
fi

source "${SCRIPT_DIR}/../agents/agents_config.sh"
if ! agents_resolve "${TASK}" 2>"${LOG_FILE}"; then
  if [[ -z "${REVIEW_SEAT}" ]]; then
    echo "error" > "${STATUS_FILE}"
  fi
  exit 1
fi

# implement.sh anchors a durable Codex identity to the implementation model
# configured when that seat first opens. A seat opened by review.sh must use
# the same anchor even though this turn runs the review model; otherwise the
# first role switch back to implementation rejects its own retained thread.
DURABLE_SEAT_MODEL="${AGENT_MODEL}"
if [[ -n "${NEW_SLOT}" ]]; then
  IMPLEMENTATION_PAIR="$(_agents_registry_get "delegate.${AGENT_FAMILY}" implementation)"
  if [[ -z "${IMPLEMENTATION_PAIR}" ]]; then
    IMPLEMENTATION_PAIR="$(_agents_registry_get "delegate.${AGENT_FAMILY}" impl)"
  fi
  if [[ -z "${IMPLEMENTATION_PAIR}" ]]; then
    echo "ERROR: cannot record review seat ${REVIEW_SEAT}: no implementation model is configured." >&2
    exit 2
  fi
  DURABLE_SEAT_MODEL="${IMPLEMENTATION_PAIR%%:*}"
fi

claim_follow() {
  "$PY" - "${SESSION_DIR}" "${FOLLOW_TO}" "${SCRIPT_DIR}/../agents/codex_mesh.py" \
    "${BOARD_HELPER}" "${COMPACT_ABOVE_TOKENS}" "${LENS}" <<'PY'
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

root_text, name, mesh_script, board_helper, compact_above_text, lens = sys.argv[1:]
root = Path(root_text)
compact_above = int(compact_above_text)

def read_roster():
    try:
        value = json.loads((root / "mesh_roster.json").read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}

def post(message):
    subprocess.run(
        ["bash", board_helper, "post", root_text, name.rsplit("-", 1)[-1],
         "status", message],
        capture_output=True,
        text=True,
    )

with (root / "impl_seat_claim.lock").open("a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    entry = read_roster().get(name, {})
    context_tokens = entry.get("context_tokens", 0) if isinstance(entry, dict) else 0
    if isinstance(context_tokens, int) and context_tokens > compact_above:
        compact = subprocess.run(
            [sys.executable, mesh_script, "compact", "--session-dir", root_text,
             "--to", name],
            capture_output=True,
            text=True,
        )
        if compact.returncode == 0:
            post(f"launcher: {compact.stdout.strip() or f'compacted {name}'}")
        else:
            detail = (compact.stderr.strip() or compact.stdout.strip()
                      or f"exit {compact.returncode}").splitlines()[-1]
            post(f"launcher: compaction failed for {name}: {detail}; continuing follow-up")
    checked = subprocess.run(
        [sys.executable, mesh_script, "can-follow", "--session-dir", root_text,
         "--to", name, "--claim-pid", os.environ["FOLLOW_LAUNCHER_PID"],
         "--role", "review", *(["--lens", lens] if lens else [])],
        capture_output=True,
        text=True,
    )
    if checked.returncode != 0:
        print(checked.stderr.strip() or f"{name} is busy or not followable", file=sys.stderr)
        raise SystemExit(checked.returncode)
PY
}

cleanup_mesh_review() {
  if [[ -n "${FOLLOW_TO}" ]]; then
    "$PY" "${SCRIPT_DIR}/../agents/codex_mesh.py" release-follow \
      --session-dir "${SESSION_DIR}" --to "${FOLLOW_TO}" \
      --claim-pid "${FOLLOW_LAUNCHER_PID}" >/dev/null 2>&1 || true
  fi
  rm -f "${REPLY_FILE}"
}

tree_status() {
  git -C "${WORKING_DIR}" status --porcelain
}

tree_diff_hash() {
  git -C "${WORKING_DIR}" diff HEAD |
    "$PY" -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

tree_head() {
  git -C "${WORKING_DIR}" rev-parse HEAD
}

tree_untracked_hash() {
  "$PY" -c '
import hashlib
import os
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
names = subprocess.check_output(
    ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"]
).split(b"\0")
digest = hashlib.sha256()
for raw_name in names:
    if not raw_name:
        continue
    path = root / os.fsdecode(raw_name)
    digest.update(len(raw_name).to_bytes(8, "big"))
    digest.update(raw_name)
    if path.is_symlink():
        content = os.fsencode(os.readlink(path))
        digest.update(b"symlink\0")
        digest.update(content)
    else:
        digest.update(b"file\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
print(digest.hexdigest())
' "${WORKING_DIR}"
}

tree_paths() {
  {
    git -C "${WORKING_DIR}" diff HEAD --name-only
    git -C "${WORKING_DIR}" ls-files --others --exclude-standard
  } | LC_ALL=C sort -u
}

capture_tree_baseline() {
  TREE_HEAD_BEFORE="$(tree_head)"
  TREE_STATUS_BEFORE="$(tree_status)"
  TREE_DIFF_HASH_BEFORE="$(tree_diff_hash)"
  TREE_UNTRACKED_HASH_BEFORE="$(tree_untracked_hash)"
  TREE_PATHS_BEFORE="$(tree_paths)"
  TREE_BASELINE_READY=1
}

record_new_seat_identity() {
  "$PY" - "${SESSION_DIR}" "${TEAM_SLOT}" "${REVIEW_SEAT}" \
    "${AGENT_FAMILY}" "${DURABLE_SEAT_MODEL}" <<'PY'
import fcntl
import json
import sys
from pathlib import Path

root_text, slot, name, family, model = sys.argv[1:]
root = Path(root_text)
with (root / "impl_seat_claim.lock").open("a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        roster = json.loads((root / "mesh_roster.json").read_text())
    except (OSError, ValueError):
        roster = {}
    entry = roster.get(name, {}) if isinstance(roster, dict) else {}
    seat_id = entry.get("thread_id", "") if isinstance(entry, dict) else ""
    if not isinstance(seat_id, str) or not seat_id:
        raise SystemExit(1)
    identity = {
        "name": name,
        "slot": slot,
        "family": family,
        "model": model,
        "seat_id": seat_id,
    }
    (root / f"impl_seat_{slot}.json").write_text(json.dumps(identity))
PY
}

TREE_BASELINE_READY=0
TREE_HEAD_BEFORE=""
TREE_STATUS_BEFORE=""
TREE_DIFF_HASH_BEFORE=""
TREE_UNTRACKED_HASH_BEFORE=""
TREE_PATHS_BEFORE=""

if [[ -n "${REVIEW_SEAT}" ]]; then
  CODEX_MESH="${PLAN_DELEGATE_CODEX_MESH:-$(_agents_registry_get delegate.options codex_mesh)}"
  if [[ "${AGENT_FAMILY}" != "codex" || "${CODEX_MESH}" != "1" ]]; then
    echo "ERROR: review seat ${REVIEW_SEAT} is not available through the Codex mesh." >&2
    exit 2
  fi
  if [[ -n "${FOLLOW_TO}" ]]; then
    DELEGATE_CONFIG="${PLAN_DELEGATE_CONFIG:-${HOME}/.claude/config/delegate.conf}"
    COMPACT_ABOVE_TOKENS=""
    if [[ -r "${DELEGATE_CONFIG}" ]]; then
      COMPACT_ABOVE_TOKENS="$(sed -n \
        's/^PLAN_DELEGATE_COMPACT_ABOVE_TOKENS=\([0-9][0-9]*\).*$/\1/p' \
        "${DELEGATE_CONFIG}" | tail -n 1)"
    fi
    if [[ ! "${COMPACT_ABOVE_TOKENS}" =~ ^[0-9]+$ ]] || (( COMPACT_ABOVE_TOKENS <= 0 )); then
      echo "ERROR: PLAN_DELEGATE_COMPACT_ABOVE_TOKENS in ${DELEGATE_CONFIG} must be a positive integer." >&2
      exit 2
    fi
    export FOLLOW_LAUNCHER_PID="$$"
    claim_follow
  fi
  trap cleanup_mesh_review EXIT
  if [[ -z "${EARLY_READY_FILE}" || -e "${EARLY_READY_FILE}" ]]; then
    capture_tree_baseline
  fi
  initialize_review_artifacts
fi

printf 'task=%s\nfamily=%s\nagent=%s\neffort=%s\n' \
  "${TASK}" "${AGENT_FAMILY}" "${AGENT_MODEL}" "${AGENT_EFFORT}" > "${AGENT_FILE}"

if [[ -n "${REVIEW_SEAT}" ]]; then
  MESH_FIELD="mesh=${REVIEW_SEAT}; reach=codex_mesh.py"
  if [[ -n "${FOLLOW_TO}" ]]; then
    LAUNCH_NOTE="follow-up to ${REVIEW_SEAT}"
  else
    LAUNCH_NOTE="new seat ${REVIEW_SEAT}"
  fi
else
  MESH_FIELD="mesh=none"
  LAUNCH_NOTE="up"
fi
board_post register \
  "${LENS} review ${LAUNCH_NOTE} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset}); ${MESH_FIELD}; role=review; status in review_status${SUFFIX}"

PASS_STARTED=0
record_pass_start() {
  [[ -f "${PROGRESS_STATE}" ]] || return 0
  if ! PLAN_DELEGATE_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" start-pass \
    --session-dir "${SESSION_DIR}" \
    --pass-kind review \
    --activity "${PASS_ACTIVITY}" \
    --called-task "${TASK}" \
    --called-family "${AGENT_FAMILY}" \
    --called-model "${AGENT_MODEL}" \
    --called-effort "${AGENT_EFFORT:-unset}"; then
    return 1
  fi
  PASS_STARTED=1
}

if [[ -z "${EARLY_READY_FILE}" ]]; then
  if ! record_pass_start; then
    echo "ERROR: unable to record the review pass start." >&2
    echo "error" > "${STATUS_FILE}"
    exit 1
  fi
fi

bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" header "${BEAT_TAG} (${AGENT_FAMILY}/${AGENT_MODEL}:${AGENT_EFFORT:-unset})" "${ROLE_DESC}" || true

if [[ -n "${FOLLOW_TO}" ]]; then
  "$PY" "${SCRIPT_DIR}/../agents/codex_mesh.py" follow \
    --session-dir "${SESSION_DIR}" --to "${FOLLOW_TO}" \
    --claim-pid "${FOLLOW_LAUNCHER_PID}" --role review "${LENS_ARGS[@]}" \
    --message-file "${PROMPT_FILE}" --summary-file "${FINDINGS_FILE}" \
    --reply-file "${REPLY_FILE}" --log-file "${LOG_FILE}" \
    --model "${AGENT_MODEL}" --effort "${AGENT_EFFORT:-}" \
    --service-tier "${AGENT_SERVICE_TIER:-}" &
elif [[ -n "${NEW_SLOT}" ]]; then
  "$PY" "${SCRIPT_DIR}/../agents/codex_mesh.py" start \
    --session-dir "${SESSION_DIR}" --name "${REVIEW_SEAT}" --role review "${LENS_ARGS[@]}" \
    --cwd "${WORKING_DIR}" --prompt-file "${PROMPT_FILE}" \
    --summary-file "${FINDINGS_FILE}" --reply-file "${REPLY_FILE}" \
    --log-file "${LOG_FILE}" --model "${AGENT_MODEL}" \
    --effort "${AGENT_EFFORT:-}" --service-tier "${AGENT_SERVICE_TIER:-}" &
else
  bash "${SCRIPT_DIR}/../agents/agent_exec.sh" \
    "${TASK}" readonly "${WORKING_DIR}" "${PROMPT_FILE}" "${FINDINGS_FILE}" "${LOG_FILE}" &
fi
AGENT_PID=$!

# The wrapper beat carries an activity digest decoded from the reviewer's own
# streamed log (the tool it is running, the file it is reading, and its
# prompt-instructed narration lines).
bash "${SCRIPT_DIR}/../agents/heartbeat_watch.sh" \
  "${HEARTBEAT_FILE}" "${BEAT_TAG}" "${AGENT_PID}" "${LOG_FILE}" "${HEARTBEAT_INTERVAL_SECS}" \
  "${AWAKE_FILE}" &
HEARTBEAT_LOOP_PID=$!

if [[ -n "${EARLY_READY_FILE}" ]]; then
  # The implementation pass is open until the sentinel appears; starting the
  # review pass before then would close it as interrupted.
  while kill -0 "${AGENT_PID}" 2>/dev/null && [[ ! -e "${EARLY_READY_FILE}" ]]; do
    sleep 5
  done
  if [[ -e "${EARLY_READY_FILE}" && "${TREE_BASELINE_READY}" -eq 0 ]]; then
    capture_tree_baseline
  fi
  if [[ -e "${EARLY_READY_FILE}" ]] && kill -0 "${AGENT_PID}" 2>/dev/null; then
    if ! record_pass_start; then
      echo "ERROR: unable to record the early review pass start." >&2
      kill "${AGENT_PID}" 2>/dev/null || true
      wait "${AGENT_PID}" 2>/dev/null || true
      kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
      wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
      echo "error" > "${STATUS_FILE}"
      exit 1
    fi
  fi
fi

AGENT_CODE=0
wait "${AGENT_PID}" || AGENT_CODE=$?

if [[ -n "${NEW_SLOT}" ]] && ! record_new_seat_identity; then
  echo "ERROR: review seat ${REVIEW_SEAT} has no durable seat identity." >&2
  AGENT_CODE=1
fi

kill "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true
wait "${HEARTBEAT_LOOP_PID}" 2>/dev/null || true

TREE_CHANGED=0
CHANGED_PATHS=""
if [[ -n "${REVIEW_SEAT}" && "${TREE_BASELINE_READY}" -eq 1 ]]; then
  TREE_HEAD_AFTER="$(tree_head)"
  TREE_STATUS_AFTER="$(tree_status)"
  TREE_DIFF_HASH_AFTER="$(tree_diff_hash)"
  TREE_UNTRACKED_HASH_AFTER="$(tree_untracked_hash)"
  TREE_PATHS_AFTER="$(tree_paths)"
  if [[ "${TREE_HEAD_BEFORE}" != "${TREE_HEAD_AFTER}" || \
        "${TREE_STATUS_BEFORE}" != "${TREE_STATUS_AFTER}" || \
        "${TREE_DIFF_HASH_BEFORE}" != "${TREE_DIFF_HASH_AFTER}" || \
        "${TREE_UNTRACKED_HASH_BEFORE}" != "${TREE_UNTRACKED_HASH_AFTER}" ]]; then
    TREE_CHANGED=1
    HEAD_CHANGED_PATHS=""
    if [[ "${TREE_HEAD_BEFORE}" != "${TREE_HEAD_AFTER}" ]]; then
      HEAD_CHANGED_PATHS="$(git -C "${WORKING_DIR}" diff --name-only \
        "${TREE_HEAD_BEFORE}" "${TREE_HEAD_AFTER}" 2>/dev/null || true)"
    fi
    CHANGED_PATHS="$(printf '%s\n%s\n%s\n' \
      "${TREE_PATHS_BEFORE}" "${TREE_PATHS_AFTER}" "${HEAD_CHANGED_PATHS}" |
      sed '/^$/d' | LC_ALL=C sort -u | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
    [[ -n "${CHANGED_PATHS}" ]] || CHANGED_PATHS="working tree state changed"
    echo "ERROR: review seat ${REVIEW_SEAT} changed the working tree: ${CHANGED_PATHS}" >&2
  fi
fi

if [[ "${AGENT_CODE}" -eq 0 && "${TREE_CHANGED}" -eq 0 ]]; then
  echo "reviewed" > "${STATUS_FILE}"
  # A completed early review is a real pass only once the sentinel exists: the
  # worker can beat the 5s poll to the finish, leaving the start unrecorded
  # above. Before the sentinel there is nothing to record. The implementation
  # pass is still open, so start-pass would close it as interrupted and every
  # later progress call would be refused for having no active window; and the
  # verdict is void regardless, because the final diff does not exist yet.
  if [[ "${PASS_STARTED}" -eq 0 && -e "${EARLY_READY_FILE}" ]]; then
    record_pass_start || echo "ERROR: unable to record the review pass start." >&2
  fi
  if [[ -f "${PROGRESS_STATE}" && "${PASS_STARTED}" -eq 1 ]]; then
    if ! PLAN_DELEGATE_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status completed \
      --agent-awake-seconds "$(awake_seconds)"; then
      echo "ERROR: unable to record the review pass completion." >&2
      echo "error" > "${STATUS_FILE}"
      exit 1
    fi
  fi
  if [[ -n "${EARLY_READY_FILE}" && ! -e "${EARLY_READY_FILE}" ]]; then
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper \
      "${BEAT_TAG} agent finished before delivery — no pass recorded, verdict void" || true
  else
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent finished" || true
  fi
  board_post done \
    "launcher: ${LENS} review finished; findings in review_findings_${PASS_INDEX}${SUFFIX}.txt"
else
  echo "error" > "${STATUS_FILE}"
  # An early reviewer that failed before its pass started recorded nothing;
  # there is no pass to close.
  if [[ -f "${PROGRESS_STATE}" && "${PASS_STARTED}" -eq 1 ]]; then
    PLAN_DELEGATE_PASS_OWNER=launcher "$PY" "${PROGRESS_HELPER}" finish-pass \
      --session-dir "${SESSION_DIR}" --status error \
      --agent-awake-seconds "$(awake_seconds)" \
      || echo "ERROR: unable to record the review pass error." >&2
  fi
  if [[ "${TREE_CHANGED}" -eq 1 ]]; then
    bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper \
      "${BEAT_TAG} review seat ${REVIEW_SEAT} changed the working tree: ${CHANGED_PATHS}" || true
    board_post blocked \
      "launcher: review seat ${REVIEW_SEAT} changed the working tree: ${CHANGED_PATHS}; findings kept in review_findings_${PASS_INDEX}${SUFFIX}.txt"
    exit 1
  fi
  bash "${HEARTBEAT_HELPER}" "${HEARTBEAT_FILE}" wrapper "${BEAT_TAG} agent exited with code ${AGENT_CODE}" || true
  board_post blocked \
    "launcher: ${LENS} review exited with code ${AGENT_CODE}; this seat is down"
  exit "${AGENT_CODE}"
fi
