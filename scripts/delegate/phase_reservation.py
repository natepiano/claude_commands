#!/usr/bin/env python3
"""Record a phase's berth reservation as `Active` once a first touch has acquired it.

Each open phase of a unit has its own directory, `${SESSION_DIR}/phases/<phase>/`, holding its
reservation state, its baselines and its `files` list, so a later phase running beside the
current one keeps its own state. A unit director persists `enrolled_awaiting_first_touch` in
the phase's `delegated_phase_reservation_state.json` before dispatch, and the first edit that
a clear `cargo-berth check` acquires turns it `Active` (`commands/unit/direct.md` →
<DelegatedPhaseReservationContract/>). Codex seats edit through `apply_patch`, so the Codex
pre-edit hook (`scripts/hooks/pre-tool-use-berth.py`) runs that check and writes `Active`
here. `record-active` is the checkpoint-time backstop for a first touch the hook did not see:
it checks every path of the phase changed since its baseline and writes `Active` from the answer.

A phase is open while its directory holds a state file; the checkpoint deletes it. With one
open phase every edited path is that phase's. With several, a path is the phase's whose `files`
list covers it: one repository-relative path per line, a trailing `/` covering the tree below.

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

PHASES_DIR = "phases"
STATE_FILE = "delegated_phase_reservation_state.json"
BASELINE_FILE = "phase_baseline"
BASELINE_STATUS_FILE = "progress_baseline_status"
FILES_FILE = "files"
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


class OpenPhase(NamedTuple):
    """A phase whose directory still holds its reservation state."""

    phase: str
    directory: Path


class Attribution(NamedTuple):
    """Edited scopes grouped by the open phase that owns them, and the scopes no single phase owns."""

    by_phase: dict[OpenPhase, list[str]]
    refused: list[str]


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


def _read_state(phase_dir: Path) -> dict[str, object]:
    try:
        return _object(cast(object, json.loads((phase_dir / STATE_FILE).read_text(encoding="utf-8"))))
    except (OSError, UnicodeError, ValueError):
        return {}


def _state_phase(state: dict[str, object]) -> str:
    """The phase a state of any kind names."""
    nested = (_object(value) for value in state.values())
    return _string(state.get("phase")) or next((_string(fields.get("phase")) for fields in nested
                                                if _string(fields.get("phase"))), "")


def phase_directory(session_dir: Path, phase: str) -> Path:
    return session_dir / PHASES_DIR / phase


def _adopt_session_level_files(session_dir: Path) -> None:
    """Move a session's pre-phase-directory files into the directory of the phase they name.

    A unit mid-phase when per-phase directories arrived kept one state file and one pair of
    baselines at the top of its session directory; they move once, before anything reads them.
    """
    phase = _state_phase(_read_state(session_dir))
    if not phase or "/" in phase or phase in (".", ".."):
        return
    target = phase_directory(session_dir, phase)
    if (target / STATE_FILE).exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for name in (BASELINE_FILE, BASELINE_STATUS_FILE, STATE_FILE):
        if (session_dir / name).exists():
            os.replace(session_dir / name, target / name)


def open_phases(session_dir: Path) -> list[OpenPhase]:
    """Every phase whose directory still holds a state file, in phase-name order."""
    _adopt_session_level_files(session_dir)
    phases = session_dir / PHASES_DIR
    found = sorted(phases.iterdir()) if phases.is_dir() else []
    return [OpenPhase(path.name, path) for path in found if (path / STATE_FILE).is_file()]


def _phase_files(phase: OpenPhase) -> list[str]:
    try:
        lines = (phase.directory / FILES_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    return [line.strip().removeprefix("./") for line in lines if line.strip()]


def _covers(entry: str, scope: str) -> bool:
    return scope.startswith(entry) if entry.endswith("/") else scope == entry


def attribute(phases: list[OpenPhase], scopes: list[str]) -> Attribution:
    """Group the scopes by owning phase: the only open phase, else the one whose files cover each."""
    if len(phases) == 1:
        return Attribution({phases[0]: list(scopes)}, [])
    by_phase: dict[OpenPhase, list[str]] = {}
    refused: list[str] = []
    files = {phase: _phase_files(phase) for phase in phases}
    for scope in scopes:
        owners = [phase for phase in phases if any(_covers(entry, scope) for entry in files[phase])]
        if len(owners) == 1:
            by_phase.setdefault(owners[0], []).append(scope)
        else:
            refused.append(scope)
    return Attribution(by_phase, refused)


def refusal_reason(phases: list[OpenPhase], refused: list[str]) -> str:
    names = ", ".join(phase.phase for phase in phases)
    return (f"{', '.join(refused)} is in no single open phase's files list (open phases: {names}); "
            + f"add each path to exactly one {PHASES_DIR}/<phase>/{FILES_FILE}.")


def record_active(phase_dir: Path, acquisition: Acquisition) -> bool:
    """Replace `enrolled_awaiting_first_touch` with `Active` for its phase; True when written."""
    state = _read_state(phase_dir)
    phase = _string(state.get("phase"))
    if _string(state.get("kind")) != AWAITING_FIRST_TOUCH or not phase:
        return False
    active = {"kind": ACTIVE, "active_phase_reservation": {
        "reservation_id": acquisition.reservation_id,
        "coordination_run_id": acquisition.coordination_run_id,
        "phase": phase,
        "phase_start_head": acquisition.phase_start_head,
    }}
    descriptor, temporary = tempfile.mkstemp(prefix=".reservation-", suffix=".json", dir=phase_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(active, output, separators=(",", ":"))
            _ = output.write("\n")
        os.replace(temporary, phase_dir / STATE_FILE)
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
    phases = open_phases(session_dir)
    phase = next((open_phase for open_phase in phases if open_phase.phase == cast(str, args.phase)), None)
    state = _read_state(phase.directory) if phase is not None else {}
    if phase is None or _string(state.get("kind")) != AWAITING_FIRST_TOUCH:
        print(f"phase reservation: {_string(state.get('kind')) or 'no state'}; nothing to record")
        return 0
    root = repository_root(Path(cast(str, args.repo)))
    baseline_file = phase.directory / BASELINE_FILE
    baseline = baseline_file.read_text(encoding="utf-8").strip() if baseline_file.is_file() else ""
    if root is None or not baseline:
        raise SystemExit("phase reservation: no repository or no phase baseline to compare against")
    # Paths a sibling phase owns, or no phase owns, are that phase's or the director's to answer.
    scopes = attribute(phases, changed_since_baseline(root, baseline)).by_phase.get(phase, [])
    if not scopes:
        print("phase reservation: nothing of this phase changed since its baseline; still awaiting first touch")
        return 0
    answer = check(root, scopes, os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    if answer.status != CLEAR or answer.acquisition is None:
        print(f"phase reservation: cargo-berth check answered {answer.status}: {answer.message}", file=sys.stderr)
        return 1
    _ = record_active(phase.directory, answer.acquisition)
    print(f"phase reservation: Active, reservation {answer.acquisition.reservation_id}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record-active", help="write Active for a first touch the hook did not see")
    _ = record.add_argument("--session-dir", required=True)
    _ = record.add_argument("--phase", required=True)
    _ = record.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    return _record_active_command(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
