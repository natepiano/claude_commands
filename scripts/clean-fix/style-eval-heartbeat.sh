#!/bin/bash
# Update the per-project style-eval pending JSON with liveness information.
#
# Records are written under:
#   ~/rust/nate_style/.history/.pending/<project>.json
#
# Usage:
#   style-eval-heartbeat.sh --project <name> --record heartbeat|agent [options]
#
# `heartbeat` is the wrapper record written by style-eval-all.sh.
# `agent` is the evaluator-agent record written from the style_eval prompt.

set -euo pipefail

PROJECT=""
RECORD=""
PID=""
PROJECT_ROOT=""
LOG_FILE=""
EVAL_PATH=""
RESULTS_FILE=""
MESSAGE=""

usage() {
    sed -n '2,11p' "$0" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)
            PROJECT="${2:-}"
            shift 2
            ;;
        --record)
            RECORD="${2:-}"
            shift 2
            ;;
        --pid)
            PID="${2:-}"
            shift 2
            ;;
        --project-root)
            PROJECT_ROOT="${2:-}"
            shift 2
            ;;
        --log-file)
            LOG_FILE="${2:-}"
            shift 2
            ;;
        --eval-path)
            EVAL_PATH="${2:-}"
            shift 2
            ;;
        --results-file)
            RESULTS_FILE="${2:-}"
            shift 2
            ;;
        --message)
            MESSAGE="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "style-eval-heartbeat: unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "$PROJECT" || -z "$RECORD" ]]; then
    echo "style-eval-heartbeat: --project and --record are required" >&2
    usage
    exit 2
fi

case "$RECORD" in
    heartbeat|agent) ;;
    *)
        echo "style-eval-heartbeat: --record must be heartbeat or agent" >&2
        exit 2
        ;;
esac

NATE_STYLE_DIR="${STYLE_HISTORY_NATE_STYLE_DIR:-$HOME/rust/nate_style}"
PENDING_FILE="$NATE_STYLE_DIR/.history/.pending/$PROJECT.json"

python3 - "$PENDING_FILE" "$RECORD" "$PID" "$PROJECT_ROOT" "$LOG_FILE" "$EVAL_PATH" "$RESULTS_FILE" "$MESSAGE" <<'PY'
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

pending_file = Path(sys.argv[1])
record = sys.argv[2]
pid = sys.argv[3]
project_root = sys.argv[4]
log_file = sys.argv[5]
eval_path = sys.argv[6]
results_file = sys.argv[7]
message = sys.argv[8]


def utc_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def optional_path(value: str) -> Path | None:
    return Path(value) if value else None


def latest_activity(paths: list[Path | None]) -> tuple[str, float | None]:
    latest_path = ""
    latest_mtime: float | None = None
    for path in paths:
        if path is None or not path.exists():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if latest_mtime is None or mtime > latest_mtime:
            latest_mtime = mtime
            latest_path = str(path)
    return latest_path, latest_mtime


if not pending_file.exists():
    raise SystemExit(0)

lock_path = pending_file.with_suffix(pending_file.suffix + ".lock")
lock_path.parent.mkdir(parents=True, exist_ok=True)

with lock_path.open("w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if not pending_file.exists():
        raise SystemExit(0)

    try:
        data: dict[str, Any] = json.loads(pending_file.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"style-eval-heartbeat: invalid JSON in {pending_file}: {exc}") from exc

    now_epoch = time.time()
    now = utc_from_epoch(now_epoch)
    activity_path, activity_mtime = latest_activity(
        [
            optional_path(log_file),
            optional_path(eval_path),
            optional_path(results_file),
        ]
    )

    records = data.setdefault("style_eval_heartbeat", {})
    previous = records.get(record, {})
    count = 0
    if isinstance(previous, dict):
        raw_count = previous.get("count", 0)
        if isinstance(raw_count, int):
            count = raw_count

    entry: dict[str, Any] = {
        "record": record,
        "updated_at": now,
        "updated_at_epoch": int(now_epoch),
        "count": count + 1,
    }
    if isinstance(previous, dict) and previous.get("first_seen_at"):
        entry["first_seen_at"] = previous["first_seen_at"]
        entry["first_seen_epoch"] = previous.get("first_seen_epoch")
    else:
        entry["first_seen_at"] = now
        entry["first_seen_epoch"] = int(now_epoch)

    if pid:
        entry["pid"] = int(pid) if pid.isdigit() else pid
    if project_root:
        entry["project_root"] = project_root
    if log_file:
        entry["log_file"] = log_file
    if eval_path:
        entry["eval_path"] = eval_path
    if results_file:
        entry["results_file"] = results_file
    if message:
        entry["message"] = message

    if activity_mtime is not None:
        entry["last_observed_activity_at"] = utc_from_epoch(activity_mtime)
        entry["last_observed_activity_epoch"] = int(activity_mtime)
        entry["last_observed_activity_path"] = activity_path
        entry["stale_seconds"] = max(0, int(now_epoch - activity_mtime))

    records[record] = entry

    tmp_path = pending_file.with_suffix(pending_file.suffix + f".{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_path, pending_file)
PY
