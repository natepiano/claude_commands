#!/usr/bin/env python3
"""Record a phase's berth reservation as `Active` once a first touch has acquired it.

A unit director persists `enrolled_awaiting_first_touch` in
`${SESSION_DIR}/delegated_phase_reservation_state.json` before dispatch, and the first edit
that a clear `cargo-berth check` acquires turns it `Active` (`commands/unit/direct.md` →
<DelegatedPhaseReservationContract/>). Codex seats edit through `apply_patch`, so the Codex
pre-edit hook (`scripts/hooks/pre-tool-use-berth.py`) runs that check and writes `Active`
here. `record-active` is the checkpoint-time backstop for a first touch the hook did not see:
it checks every path changed since the phase baseline and writes `Active` from the answer.

Only `enrolled_awaiting_first_touch` is ever replaced. Every other state is left as it is:
`Active` already names its reservation, and the checkpoint's own states belong to
<CheckpointCommit/>.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple, cast

STATE_FILE = "delegated_phase_reservation_state.json"
BASELINE_FILE = "phase_baseline"
AWAITING_FIRST_TOUCH = "enrolled_awaiting_first_touch"
ACTIVE = "active"
CLEAR = "clear"
UNCONFIGURED = "unconfigured"
SESSION_ENVIRONMENT = "CARGO_BERTH_SESSION_ID"
# The engine's single ledger deadline is ten seconds; the rest covers a slow start under load.
CHECK_TIMEOUT_SECONDS = 25
PATCH_PATH_HEADERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")


class Acquisition(NamedTuple):
    """The reservation a clear check acquired or found already held."""

    reservation_id: str
    coordination_run_id: str
    phase_start_head: str


class CheckAnswer(NamedTuple):
    status: str
    message: str
    acquisition: Acquisition | None


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def patch_paths(patch: str) -> list[str]:
    """Every path an `apply_patch` text writes, deletes or moves to, in patch order."""
    found: list[str] = []
    for line in patch.splitlines():
        header = next((header for header in PATCH_PATH_HEADERS if line.startswith(header)), "")
        path = line.removeprefix(header).strip() if header else ""
        if path and path not in found:
            found.append(path)
    return found


def repository_root(directory: Path) -> Path | None:
    result = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def repository_paths(root: Path, directory: Path, paths: list[str]) -> list[str]:
    """The paths as berth scopes: repository-relative, with any outside the repository dropped."""
    scopes: list[str] = []
    for path in paths:
        try:
            scope = (directory / path).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if scope != "." and scope not in scopes:
            scopes.append(scope)
    return scopes


def parse_answer(stdout: str) -> CheckAnswer:
    try:
        envelope = _object(cast(object, json.loads(stdout)))
    except ValueError:
        return CheckAnswer("malformed", f"cargo-berth printed no JSON envelope: {stdout.strip()[:200]}", None)
    data = _object(_object(envelope.get("payload")).get("data"))
    acquired = _object(data.get("acquisition"))
    fields = (_string(acquired.get(name)) for name in Acquisition._fields)
    acquisition = Acquisition(*fields)
    return CheckAnswer(
        _string(envelope.get("status")) or "malformed",
        _string(envelope.get("message")),
        acquisition if all(acquisition) else None,
    )


def check(root: Path, scopes: list[str], session_id: str) -> CheckAnswer:
    """Run `cargo-berth check <scopes> --json` from the repository root as `session_id`."""
    environment = {**os.environ, SESSION_ENVIRONMENT: session_id} if session_id else dict(os.environ)
    try:
        result = subprocess.run(["cargo-berth", "check", *scopes, "--json"], cwd=root, env=environment,
                                capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
                                timeout=CHECK_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return CheckAnswer("not_installed", "the cargo-berth binary is not on PATH", None)
    except subprocess.TimeoutExpired:
        return CheckAnswer("timed_out", f"cargo-berth check did not answer within {CHECK_TIMEOUT_SECONDS} seconds", None)
    return parse_answer(result.stdout)


def _read_state(session_dir: Path) -> dict[str, object]:
    try:
        return _object(cast(object, json.loads((session_dir / STATE_FILE).read_text(encoding="utf-8"))))
    except (OSError, UnicodeError, ValueError):
        return {}


def record_active(session_dir: Path, acquisition: Acquisition) -> bool:
    """Replace `enrolled_awaiting_first_touch` with `Active` for its phase; True when written."""
    state = _read_state(session_dir)
    phase = _string(state.get("phase"))
    if _string(state.get("kind")) != AWAITING_FIRST_TOUCH or not phase:
        return False
    active = {"kind": ACTIVE, "active_phase_reservation": {
        "reservation_id": acquisition.reservation_id,
        "coordination_run_id": acquisition.coordination_run_id,
        "phase": phase,
        "phase_start_head": acquisition.phase_start_head,
    }}
    descriptor, temporary = tempfile.mkstemp(prefix=".reservation-", suffix=".json", dir=session_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(active, output, separators=(",", ":"))
            _ = output.write("\n")
        os.replace(temporary, session_dir / STATE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def changed_since_baseline(root: Path, baseline: str) -> list[str]:
    """Every path changed, deleted or added since the phase baseline, untracked files included."""
    paths: list[str] = []
    for command in (["diff", "--name-only", "--no-renames", baseline], ["ls-files", "--others", "--exclude-standard"]):
        result = subprocess.run(["git", "-C", str(root), *command], capture_output=True, text=True,
                                check=False, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise SystemExit(f"git {' '.join(command)} failed: {result.stderr.strip()}")
        paths.extend(line for line in result.stdout.splitlines() if line and line not in paths)
    return paths


def _record_active_command(args: argparse.Namespace) -> int:
    session_dir = Path(cast(str, args.session_dir))
    state = _read_state(session_dir)
    if _string(state.get("kind")) != AWAITING_FIRST_TOUCH:
        print(f"phase reservation: {_string(state.get('kind')) or 'no state'}; nothing to record")
        return 0
    root = repository_root(Path(cast(str, args.repo)))
    baseline = (session_dir / BASELINE_FILE).read_text(encoding="utf-8").strip() if (session_dir / BASELINE_FILE).is_file() else ""
    if root is None or not baseline:
        raise SystemExit("phase reservation: no repository or no phase baseline to compare against")
    scopes = changed_since_baseline(root, baseline)
    if not scopes:
        print("phase reservation: nothing changed since the phase baseline; still awaiting first touch")
        return 0
    answer = check(root, scopes, os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    if answer.status != CLEAR or answer.acquisition is None:
        print(f"phase reservation: cargo-berth check answered {answer.status}: {answer.message}", file=sys.stderr)
        return 1
    _ = record_active(session_dir, answer.acquisition)
    print(f"phase reservation: Active, reservation {answer.acquisition.reservation_id}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record-active", help="write Active for a first touch the hook did not see")
    _ = record.add_argument("--session-dir", required=True)
    _ = record.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    return _record_active_command(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
