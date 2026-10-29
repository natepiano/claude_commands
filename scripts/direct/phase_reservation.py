#!/usr/bin/env python3
"""Record a phase's berth reservation as `Active` once a first touch has acquired it.

Each open phase of a unit has its own directory, `${SESSION_DIR}/phases/<phase>/`, holding its
reservation state, its baselines and its `files` list, so a later phase running beside the
current one keeps its own state. A unit director persists `enrolled_awaiting_first_touch` in
the phase's `delegated_phase_reservation_state.json` before dispatch, and the first edit that
a clear `cargo-berth check` acquires turns it `Active`. Each phase checks as its own berth
session, `<director session id>.phase-<phase>`, so a phase opened beside another holds its own
reservation; a path a sibling phase already holds answers `left_unmapped` with the sibling's
reservation, which is never recorded here (`commands/unit/direct.md` →
<PhaseReservationContract/>). With one open phase there is no sibling: a `left_unmapped`
reservation that cargo-berth's session map names for the director's bare session id, from an edit
the director made itself, is that phase's own. Codex workers edit through `apply_patch`, so the Codex
pre-edit hook (`scripts/hooks/pre-tool-use-berth.py`) runs that check and writes `Active`
here. `record-active` is the checkpoint-time backstop for a first touch the hook did not see:
it checks every path of the phase changed since its baseline and writes `Active` from the answer.

A phase is open while the progress recorder lists it as started and unfinished. Every change to it goes through a verb,
so no session writes it by hand: `enroll` writes the first state from the `/sync board` answer
and keeps that answer in the phase's `board_result`, `repair` restores a lost record from
berth's session map after the recorder confirms the phase is open and its board confirms the reservation is active, `committed` turns
`Active` into the pending release, and `clear` removes an inactive state, or a pending one once its release names the
same checkpoint commit and leaves `reservation_released`. A style ticket's checkpoint passes
`committed --ticket`: the pending state then names the ticket and the unit's worktree, step 7 of
<CheckpointCommit/> releases nothing, and `release-held`, run by `style_ledger.py ticket merge`,
releases the reservation from that worktree and clears the record. Released phases take no part in edit
attribution or lost-record repair while their plan review finishes. A recorder-open phase with no
state file and no berth mapping of its own was never enrolled, as FinalGate's `final` phase, and is
no lost record: its edits check under the bare session id. The hook reads the recorder's
state in process. `progress_history.py finish-phase` clears an inactive state when the
phase completes, and `start-phase` on a finished phase id restores its enrolled state from the
kept board answer and removes the release marker (rca 261023-3). With one
open phase in the edit's worktree every edited path is that phase's. With several, a path is the phase's whose `files`
list covers it: one repository-relative path per line, a trailing `/` covering the tree below.
Before workers open, `claim --repo` writes the repository root in the phase directory's `worktree` file.
A legacy phase with no file still matches every worktree.

`enrolled_awaiting_first_touch` turns `Active`, and an `Active` record follows a new
reservation of its own phase: once cargo-berth releases the recorded one, as a shrink's
checkpoint does, the phase's next edit acquires another, and drift must name that one
(cargo-liner, 2026-10-21). `record-active` on an `Active` record reads the recorded reservation
on berth's board, and a released one takes the phase's live reservation. Every other state is
left as it is: the checkpoint's own states belong to <CheckpointCommit/>.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, cast, overload

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.direct import recorder_state  # noqa: E402

PHASES_DIR = "phases"
REPAIRS_FILE = "reservation_repairs.log"
RELEASED_FILE = "reservation_released"
STATE_FILE = "delegated_phase_reservation_state.json"
BASELINE_FILE = "phase_baseline"
BASELINE_STATUS_FILE = "progress_baseline_status"
FILES_FILE = "files"
WORKTREE_FILE = "worktree"
AWAITING_FIRST_TOUCH = "enrolled_awaiting_first_touch"
ACTIVE = "active"
NOT_ENROLLED = "repository_not_enrolled"
PENDING = "checkpoint_committed_awaiting_release_confirmation"
PENDING_FIELD = "checkpoint_release_confirmation_pending"
# The style ticket whose merge releases a pending reservation, and the worktree that release runs in.
HELD_FOR_TICKET = "held_for_ticket"
WORKTREE = "worktree"
# Lifecycles of a released reservation; `protected_tip` names the commit it was released at.
CHECKPOINT_RELEASED = frozenset({"outstanding", "released_after_checkpoint"})
HOLDS_RESERVATION = frozenset({ACTIVE, PENDING})
# A checkpoint's display form.
SHORT_COMMIT = 7
# The repository a verb resolves commit names in when the caller names none.
WORKING_DIRECTORY = Path(".")
# The phase's kept `/sync board` answer, and the first state each answer writes.
BOARD_RESULT_FILE = "board_result"
BOARD_STATES = {"board_ready": AWAITING_FIRST_TOUCH, "unconfigured": NOT_ENROLLED}
CLEAR = "clear"
UNCONFIGURED = "unconfigured"
LEDGER_UNREADABLE = "ledger_unreadable"
# Answers that let an edit through unchecked, as the engine's own edit hook does: an unenrolled
# repository, and a ledger it cannot read (a scratch clone has none), since ledger loss fails open.
FAILS_OPEN = frozenset({UNCONFIGURED, LEDGER_UNREADABLE})
SESSION_ENVIRONMENT = "CARGO_BERTH_SESSION_ID"
# The engine's answer when a sibling reservation of the run already holds every checked path:
# the reservation id it returns is the sibling's, so it is never this phase's to record.
LEFT_UNMAPPED = "left_unmapped"
# cargo-berth's ledger folder in the repository's common git directory, and its map from a
# harness session id to the reservation that session holds.
LEDGER_DIRECTORY = "cargo-berth"
SESSION_MAP_FILE = "session-identities.json"
# A check over a phase's whole files list took 77 s on 2026-10-29, against a ten-second ledger deadline.
CHECK_TIMEOUT_SECONDS = 90
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
    session_mapping: str = ""


class KnownPhaseWorktree(NamedTuple):
    """The repository root recorded when this phase claimed its files."""

    root: Path


class LegacyPhaseWorktree(NamedTuple):
    """No worktree was recorded; this phase still matches every root."""


class AllPhaseWorktrees(NamedTuple):
    """A caller without an edit root keeps the original attribution across all phases."""


ALL_PHASE_WORKTREES = AllPhaseWorktrees()


class OpenPhase(NamedTuple):
    """A phase open in the recorder, or a legacy run's still-held reservation state."""

    phase: str
    directory: Path
    worktree: KnownPhaseWorktree | LegacyPhaseWorktree = LegacyPhaseWorktree()


class PhaseMissingRecord(NamedTuple):
    """A phase directory whose reservation record must be restored before its paths can be edited."""

    phase: str
    directory: Path


class NoOpenPhaseHere(NamedTuple):
    """No open phase runs in this edit root; check under the bare session id."""


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
        _string(_object(acquired.get("session_mapping_publication")).get("status")),
    )


def session_reservation(root: Path, session_id: str) -> tuple[str, str] | None:
    """The reservation and run cargo-berth's session map names for `session_id`, if any."""
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    if result.returncode != 0 or not session_id:
        return None
    path = Path(result.stdout.strip()) / LEDGER_DIRECTORY / SESSION_MAP_FILE
    try:
        identities = _object(_object(cast(object, json.loads(path.read_text(encoding="utf-8")))).get("identities"))
    except (OSError, UnicodeError, ValueError):
        return None
    mapped = _object(identities.get(session_id))
    reservation, run = _string(mapped.get("reservation_id")), _string(mapped.get("coordination_run_id"))
    return (reservation, run) if reservation and run else None


def own_acquisition(answer: CheckAnswer, root: Path, phases: list[OpenPhase], session_id: str) -> Acquisition | None:
    """The reservation a clear answer gives this phase: none when a sibling's already held every path.

    The one exception is the director's own reservation. An edit the director makes itself, such as
    to the plan doc, checks under its bare session id, so cargo-berth maps that reservation there
    and a later check under the phase's session answers `left_unmapped` with it. With one open
    phase there is no sibling, and a reservation the map names for the bare session id is the phase's.
    """
    acquisition = answer.acquisition
    if answer.session_mapping != LEFT_UNMAPPED or acquisition is None:
        return acquisition
    director = session_reservation(root, session_id) if len(phases) == 1 else None
    return acquisition if director == (acquisition.reservation_id, acquisition.coordination_run_id) else None


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


def _held_acquisition(root: Path, phase: OpenPhase, phase_session: str) -> Acquisition | None:
    """The live reservation berth's session map already names for this phase, with the phase baseline."""
    mapped = session_reservation(root, phase_session)
    if mapped is None or lifecycle(root, mapped[0]) != ACTIVE:
        return None
    try:
        baseline = (phase.directory / BASELINE_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return Acquisition(*mapped, baseline) if baseline else None


def phase_check(root: Path, scopes: list[str], session_id: str, phase: OpenPhase | None) -> CheckAnswer:
    """`check` for an edit or claim in `phase`, as its berth session; clear at once when the phase holds a live reservation.

    A held phase's reservation was claimed over its whole files list, and attribution keeps every
    scope inside that list, so cargo-berth has nothing to add.
    """
    berth_session = phase_session_id(session_id, phase.phase) if phase is not None else session_id
    held = _held_acquisition(root, phase, berth_session) if phase is not None else None
    if held is not None:
        return CheckAnswer(CLEAR, "the phase already holds its reservation", held)
    return check(root, scopes, berth_session)


def read_state(phase_dir: Path) -> dict[str, object]:
    try:
        return _object(cast(object, json.loads((phase_dir / STATE_FILE).read_text(encoding="utf-8"))))
    except (OSError, UnicodeError, ValueError):
        return {}


def _state_phase(state: dict[str, object]) -> str:
    """The phase a state of any kind names."""
    nested = (_object(value) for value in state.values())
    return _string(state.get("phase")) or next((_string(fields.get("phase")) for fields in nested
                                                if _string(fields.get("phase"))), "")


def phase_directory_name(phase: str) -> str:
    """The phase id's letters, digits, `.`, `_` and `-`: `ad hoc` is `adhoc`, as the launchers name it."""
    name = "".join(character for character in phase if character.isalnum() or character in "._-")
    return "" if name in (".", "..") else name


def phase_directory(session_dir: Path, phase: str) -> Path:
    return session_dir / PHASES_DIR / phase_directory_name(phase)


def phase_session_id(session_id: str, phase: str) -> str:
    """The berth session a phase checks under, so two open phases each hold a reservation of their own."""
    return f"{session_id}.phase-{phase_directory_name(phase)}" if session_id else ""


def _adopt_session_level_files(session_dir: Path) -> None:
    """Move a session's pre-phase-directory files into the directory of the phase they name.

    A unit mid-phase when per-phase directories arrived kept one state file and one pair of
    baselines at the top of its session directory; they move once, before anything reads them.
    """
    phase = _state_phase(read_state(session_dir))
    if not phase_directory_name(phase):
        return
    target = phase_directory(session_dir, phase)
    if (target / STATE_FILE).exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for name in (BASELINE_FILE, BASELINE_STATUS_FILE, STATE_FILE):
        if (session_dir / name).exists():
            os.replace(session_dir / name, target / name)


def _phase_worktree(directory: Path) -> KnownPhaseWorktree | LegacyPhaseWorktree:
    """Read the repository root kept by claim, or keep legacy global matching."""
    try:
        root = (directory / WORKTREE_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return LegacyPhaseWorktree()
    if not root:
        raise ValueError(f"Empty phase worktree record: {directory / WORKTREE_FILE}")
    return KnownPhaseWorktree(Path(root))


def _record_worktree(directory: Path, root: Path) -> None:
    """Keep one live worktree per phase, before claim checks its reservation state."""
    previous = _phase_worktree(directory)
    if isinstance(previous, KnownPhaseWorktree):
        if previous.root.resolve() == root:
            return
        if previous.root.is_dir():
            raise StateRefused(f"phase worktree is {previous.root}; cannot claim it in {root}")
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".worktree-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            _ = output.write(str(root) + "\n")
        os.replace(temporary, directory / WORKTREE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def open_phases(session_dir: Path) -> list[OpenPhase]:
    """Recorder-open phases still awaiting a checkpoint release, including lost records.

    Each phase is named by its directory, as before the recorder listed them: `ad hoc` is `adhoc`.
    An unreadable recorder state raises ValueError."""
    _adopt_session_level_files(session_dir)
    if not (session_dir / recorder_state.STATE_FILENAME).is_file():
        return held_phases(session_dir)
    names = {phase_directory_name(phase) for phase in recorder_state.read_open_phase_ids(session_dir)}
    return [OpenPhase(name, session_dir / PHASES_DIR / name,
                      _phase_worktree(session_dir / PHASES_DIR / name))
            for name in sorted(names - {""}) if not (session_dir / PHASES_DIR / name / RELEASED_FILE).is_file()]


def held_phases(session_dir: Path) -> list[OpenPhase]:
    """Every phase whose directory still holds a state file, in phase-name order: a run with no recorder list."""
    phases = session_dir / PHASES_DIR
    found = sorted(phases.iterdir()) if phases.is_dir() else []
    return [OpenPhase(path.name, path, _phase_worktree(path)) for path in found if (path / STATE_FILE).is_file()]


def enrolled_phases(phases: list[OpenPhase], root: Path, session_id: str) -> list[OpenPhase]:
    """The open phases that hold a state file or a berth mapping of their own.

    A phase with neither was never enrolled, as FinalGate's `final` phase: its edits check under
    the bare session id, as they did before the recorder listed open phases. A phase with a
    mapping but no state file is a lost record, which `repair` restores."""
    return [phase for phase in phases if (phase.directory / STATE_FILE).is_file()
            or session_reservation(root, phase_session_id(session_id, phase.phase)) is not None]


def _phase_files(phase: OpenPhase | PhaseMissingRecord) -> list[str]:
    try:
        lines = (phase.directory / FILES_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    return [line.strip().removeprefix("./") for line in lines if line.strip()]


def _covers(entry: str, scope: str) -> bool:
    return scope.startswith(entry) if entry.endswith("/") else scope == entry


@overload
def attribute(phases: list[OpenPhase], scopes: list[str],
              root: AllPhaseWorktrees = ALL_PHASE_WORKTREES) -> Attribution: ...


@overload
def attribute(phases: list[OpenPhase], scopes: list[str], root: Path) -> Attribution | NoOpenPhaseHere: ...


def attribute(phases: list[OpenPhase], scopes: list[str],
              root: Path | AllPhaseWorktrees = ALL_PHASE_WORKTREES) -> Attribution | NoOpenPhaseHere:
    """Group scopes in the edit root; callers without one keep the original attribution."""
    if isinstance(root, Path):
        phases = [phase for phase in phases if isinstance(phase.worktree, LegacyPhaseWorktree)
                  or (phase.worktree.root.is_dir() and phase.worktree.root.resolve() == root.resolve())]
        if not phases:
            return NoOpenPhaseHere()
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


def _recorded(state: dict[str, object]) -> str:
    """The reservation an `Active` record names; empty for every other state."""
    held = _object(state.get("active_phase_reservation"))
    return _string(held.get("reservation_id")) if _string(state.get("kind")) == ACTIVE else ""


def _write_state(phase_dir: Path, state: dict[str, object]) -> None:
    """Replace the phase's state file atomically."""
    phase_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".reservation-", suffix=".json", dir=phase_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(state, output, separators=(",", ":"))
            _ = output.write("\n")
        os.replace(temporary, phase_dir / STATE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StateRefused(ValueError):
    """A state change the phase's current state does not allow."""


def enroll(phase_dir: Path, phase: str, board: str) -> str:
    """Write the phase's first state from the `/sync board` answer, and keep the answer.

    The kept answer lets `restore` enroll the phase again when a finished phase reopens. A
    reservation the phase holds, `Active` or pending release, is never replaced."""
    kind = BOARD_STATES.get(board)
    if kind is None:
        raise StateRefused(f"unknown /sync board answer {board!r}; expected one of {', '.join(BOARD_STATES)}")
    current = _string(read_state(phase_dir).get("kind"))
    if current in HOLDS_RESERVATION:
        raise StateRefused(f"phase {phase} holds a reservation ({current}); enroll does not replace it")
    _write_state(phase_dir, {"kind": kind, "phase": phase})
    _ = (phase_dir / BOARD_RESULT_FILE).write_text(board + "\n", encoding="utf-8")
    (phase_dir / RELEASED_FILE).unlink(missing_ok=True)
    return kind


def restore(phase_dir: Path, phase: str) -> str:
    """Enroll a reopened phase again from its kept `/sync board` answer; the state written, or ''."""
    try:
        board = (phase_dir / BOARD_RESULT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if (phase_dir / STATE_FILE).exists() or board not in BOARD_STATES:
        return ""
    return enroll(phase_dir, phase, board)


def commit_id(repo: Path, name: str) -> str:
    """The full object id of the commit that `name` names in `repo`.

    berth's `protected_tip` and `git rev-parse HEAD` give full ids, so a checkpoint is recorded and
    compared in that form whatever name the caller gave."""
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "--end-of-options",
                             f"{name}^{{commit}}"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise StateRefused(f"{name!r} names no single commit in {repo}")
    return result.stdout.strip()


def committed(phase_dir: Path, commit: str, ticket: str = "", worktree: Path | None = None) -> None:
    """Replace `Active` with the pending release state that names the checkpoint commit, a full object id.

    With a style ticket, the state also names the ticket and the worktree: the reservation stays held
    until `release-held` runs at that ticket's merge."""
    state = read_state(phase_dir)
    if _string(state.get("kind")) != ACTIVE:
        raise StateRefused(f"the phase is {_string(state.get('kind')) or 'not open'}, not active")
    pending = {**_object(state.get("active_phase_reservation")), "checkpoint_commit": commit}
    if ticket:
        if worktree is None:
            raise StateRefused(f"ticket {ticket} needs the worktree its merge releases from")
        pending |= {HELD_FOR_TICKET: ticket, WORKTREE: str(worktree)}
    _write_state(phase_dir, {"kind": PENDING, PENDING_FIELD: pending})


def clear(phase_dir: Path, released: str = "", repo: Path = WORKING_DIRECTORY) -> bool:
    """Delete the phase's state; True when deleted, False when there was none.

    A state that owns no reservation clears at once. The pending release state clears only with
    `released`, the checkpoint commit whose release was confirmed: the recorded string itself, or any
    name `repo` resolves to the recorded commit. The recorded commit remains in the release marker
    until enrollment restores the phase. `Active` never clears here."""
    state = read_state(phase_dir)
    kind = _string(state.get("kind"))
    if not kind:
        return False
    if kind == PENDING:
        commit = _string(_object(state.get(PENDING_FIELD)).get("checkpoint_commit"))
        if not released or (released != commit and commit_id(repo, released) != commit):
            raise StateRefused(f"the pending release names checkpoint {commit}; pass --released {commit} after its release")
        released = commit
    elif kind not in BOARD_STATES.values():
        raise StateRefused(f"the phase is {kind}; commit its checkpoint and confirm the release first")
    if released:
        # Publish proof before unlinking the state, so a concurrent edit never sees a lost record.
        _ = (phase_dir / RELEASED_FILE).write_text(released + "\n", encoding="utf-8")
    (phase_dir / STATE_FILE).unlink()
    return True


def record_active(phase_dir: Path, acquisition: Acquisition) -> bool:
    """Write `Active` for the phase from its own reservation; True when written.

    `enrolled_awaiting_first_touch` turns `Active`, and an `Active` record that names another
    reservation takes this one: a phase holds one live reservation at a time."""
    state = read_state(phase_dir)
    kind = _string(state.get("kind"))
    if kind == AWAITING_FIRST_TOUCH:
        phase = _string(state.get("phase"))
    elif kind == ACTIVE and _recorded(state) != acquisition.reservation_id:
        phase = _string(_object(state.get("active_phase_reservation")).get("phase"))
    else:
        phase = ""
    if not phase:
        return False
    _write_active(phase_dir, phase, acquisition)
    return True


def _write_active(phase_dir: Path, phase: str, acquisition: Acquisition) -> None:
    """Publish the same Active shape for a check acquisition or a ledger-validated repair."""
    active: dict[str, object] = {"kind": ACTIVE, "active_phase_reservation": {
        "reservation_id": acquisition.reservation_id,
        "coordination_run_id": acquisition.coordination_run_id,
        "phase": phase,
        "phase_start_head": acquisition.phase_start_head,
    }}
    _write_state(phase_dir, active)


def _berth(root: Path, *arguments: str) -> dict[str, object]:
    """cargo-berth's JSON answer to the command; empty when it cannot run or answers no JSON."""
    try:
        result = subprocess.run(["cargo-berth", *arguments, "--json"], cwd=root, capture_output=True, text=True,
                                check=False, stdin=subprocess.DEVNULL, timeout=CHECK_TIMEOUT_SECONDS)
        return _object(cast(object, json.loads(result.stdout)))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}


def _lifecycle(root: Path, reservation_id: str) -> dict[str, object]:
    return _object(_object(_object(_berth(root, "board", "--reservation", reservation_id).get("payload")).get("data"))
                   .get("lifecycle"))


def lifecycle(root: Path, reservation_id: str) -> str:
    """The reservation's lifecycle status on berth's live board, such as `active`; empty when the
    board cannot be read."""
    return _string(_lifecycle(root, reservation_id).get("status"))


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """Whether `ancestor` is `descendant` or a commit it contains; False for a name `repo` does not know."""
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", "--end-of-options", ancestor, descendant],
                          capture_output=True, check=False).returncode == 0


def _released_at(repo: Path, found: dict[str, object], checkpoint: str, merged: str) -> bool:
    """Whether a lifecycle from berth's board shows the reservation released at this checkpoint commit, or at a
    later commit of the unit's branch that the merged commit contains.

    A unit that merges style/main after its checkpoint releases at that later merge, so berth's
    `protected_tip` is the merge, not the checkpoint (cargo-liner ticket 90)."""
    tip = _string(found.get("protected_tip"))
    return _string(found.get("status")) in CHECKPOINT_RELEASED and (
        tip == checkpoint or bool(tip) and _is_ancestor(repo, checkpoint, tip) and _is_ancestor(repo, tip, merged))


def _held_commit(worktree: Path, role: str, name: str, reservation: str) -> str:
    """The full id of a commit `release_held` compares; a name that is no commit keeps the reservation held."""
    try:
        return commit_id(worktree, name)
    except StateRefused as refused:
        raise StateRefused(f"{role} {refused}; reservation {reservation} stays held") from None


def release_held(phase_dir: Path, ticket: str, merged: str) -> str:
    """Release the reservation the phase's checkpoint kept for this ticket's merge, and clear the record.

    `merged` names the commit the ticket's merge brings in. Returns the released reservation id, or ''
    when the phase holds none for the ticket, so a repeated merge changes nothing. A release an earlier
    run made is confirmed on the board, not made again: berth's `protected_tip` is the checkpoint, or a
    later commit that `merged` contains. Only an `active` reservation is released. berth's
    release snapshots its worktree's HEAD, so a worktree that moved off the checkpoint keeps the
    reservation held, as does a board that shows another lifecycle or does not confirm the release."""
    state = read_state(phase_dir)
    pending = _object(state.get(PENDING_FIELD))
    if _string(state.get("kind")) != PENDING or _string(pending.get(HELD_FOR_TICKET)) != ticket:
        return ""
    reservation = _string(pending.get("reservation_id"))
    commit = _string(pending.get("checkpoint_commit"))
    worktree = Path(_string(pending.get(WORKTREE)))
    # A record may name its checkpoint in short form (cargo-liner ticket 59); berth and git answer in full.
    checkpoint = _held_commit(worktree, "checkpoint", commit, reservation)
    merged_commit = _held_commit(worktree, "merged commit", merged, reservation)
    found = _lifecycle(worktree, reservation)
    if not _released_at(worktree, found, checkpoint, merged_commit):
        if _string(found.get("status")) != ACTIVE:
            raise StateRefused(f"berth shows reservation {reservation} {_string(found.get('status')) or 'unreadable'}, "
                               + f"not active or released at checkpoint {checkpoint[:SHORT_COMMIT]} or at a later commit "
                               + f"that {merged_commit[:SHORT_COMMIT]} contains; it stays held")
        head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=False).stdout.strip()
        if head != checkpoint:
            raise StateRefused(f"{worktree} is at {head[:SHORT_COMMIT] or 'no commit'}, not checkpoint "
                               + f"{checkpoint[:SHORT_COMMIT]}; reservation {reservation} stays held")
        answer = _berth(worktree, "release", reservation)
        if not _released_at(worktree, _lifecycle(worktree, reservation), checkpoint, merged_commit):
            raise StateRefused(f"berth did not release reservation {reservation} at checkpoint {checkpoint[:SHORT_COMMIT]}: "
                               + (_string(answer.get("message")) or "no answer") + "; it stays held")
    _ = clear(phase_dir, commit, worktree)
    return reservation


def repair_command(session_dir: Path, phase: str, root: Path) -> str:
    """The command that restores this phase's lost record from berth, without caller-supplied ids."""
    return shlex.join(["python3", str(Path(__file__).resolve()), "repair", "--session-dir", str(session_dir),
                       "--phase", phase, "--repo", str(root)])


def missing_record_phases(phases: list[OpenPhase], scopes: list[str], root: Path) -> list[PhaseMissingRecord]:
    """Open phases owning edited paths whose reservation records have been lost."""
    attribution = attribute(phases, scopes, root)
    if isinstance(attribution, NoOpenPhaseHere):
        return []
    owners = attribution.by_phase
    return [PhaseMissingRecord(phase.phase, phase.directory) for phase in owners
            if not (phase.directory / STATE_FILE).is_file()]


def _repair_refused(session_dir: Path, phase: str, root: Path, reason: str) -> int:
    print(f"phase reservation: {reason}; open the phase again:", file=sys.stderr)
    script = str(Path(__file__).resolve())
    arguments = ["--session-dir", str(session_dir), "--phase", phase]
    print(shlex.join(["python3", script, "enroll", *arguments, "--board", "board_ready"]), file=sys.stderr)
    print(shlex.join(["python3", script, "claim", *arguments, "--repo", str(root)]), file=sys.stderr)
    return 2


def _repair_command(args: argparse.Namespace) -> int:
    session_dir = Path(cast(str, args.session_dir))
    phase = cast(str, args.phase)
    directory = phase_directory(session_dir, phase)
    requested_root = Path(cast(str, args.repo))
    root = repository_root(requested_root)
    if root is None or not phase_directory_name(phase):
        return _repair_refused(session_dir, phase, requested_root, "no repository or invalid phase")
    if (directory / RELEASED_FILE).is_file():
        print(f"phase reservation: phase {phase} checkpoint released its reservation; record unchanged", file=sys.stderr)
        return 2
    recorded_open = {phase_directory_name(each) for each in recorder_state.read_open_phase_ids(session_dir)}
    if phase_directory_name(phase) not in recorded_open:
        print(f"phase reservation: phase {phase} is finished or was never started; reopen it with:", file=sys.stderr)
        print(shlex.join(["python3", str(Path(__file__).with_name("progress_history.py")), "start-phase",
                          "--session-dir", str(session_dir), "--phase-id", phase, "--phase-title", phase]),
              file=sys.stderr)
        return 2
    state = read_state(directory)
    recorded = _recorded(state)
    if recorded and lifecycle(root, recorded) == ACTIVE:
        print(f"phase reservation: Active, reservation {recorded} is active on the board; record unchanged")
        return 0
    if _string(state.get("kind")) == PENDING:
        print("phase reservation: checkpoint release confirmation is pending; record unchanged", file=sys.stderr)
        return 2
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    source = phase_session_id(session_id, phase)
    mapped = session_reservation(root, source)
    if mapped is None and session_id and all(each.directory == directory for each in open_phases(session_dir)):
        source = session_id
        mapped = session_reservation(root, source)
    if mapped is None or lifecycle(root, mapped[0]) != ACTIVE:
        return _repair_refused(session_dir, phase, root, "no live mapped reservation")
    try:
        baseline = (directory / BASELINE_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        baseline = ""
    if not baseline:
        print("phase reservation: no phase baseline; record unchanged", file=sys.stderr)
        return 2
    reservation, run = mapped
    _write_active(directory, phase, Acquisition(reservation, run, baseline))
    line = (f"{datetime.now(timezone.utc).isoformat()} phase={phase} reservation_id={reservation} "
            + f"run_id={run} mapping_session={source}")
    with (directory / REPAIRS_FILE).open("a", encoding="utf-8") as log:
        _ = log.write(line + "\n")
    print(f"phase reservation: Active, reservation {reservation}; mapping session {source}")
    return _post_repair(session_dir, line)


def _post_repair(session_dir: Path, line: str) -> int:
    try:
        result = subprocess.run(["bash", str(Path(__file__).with_name("board.sh")), "post", str(session_dir),
                                 "director", "status", line], check=False, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=CHECK_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        detail = str(error)
    else:
        if result.returncode == 0:
            return 0
        detail = result.stderr.strip() or f"exit {result.returncode}"
    print(f"phase reservation: record repaired and logged, but board post failed: {detail}", file=sys.stderr)
    return 1


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
    directory = phase_directory(session_dir, cast(str, args.phase))
    phase = next((open_phase for open_phase in phases if open_phase.directory == directory), None)
    state = read_state(phase.directory) if phase is not None else {}
    recorded = _recorded(state)
    if phase is None or (_string(state.get("kind")) != AWAITING_FIRST_TOUCH and not recorded):
        print(f"phase reservation: {_string(state.get('kind')) or 'no state'}; nothing to record")
        return 0
    root = repository_root(Path(cast(str, args.repo)))
    if root is not None and isinstance(attribute([phase], [], root), NoOpenPhaseHere):
        print("phase reservation: no open phase in this worktree; nothing to record")
        return 0
    status = lifecycle(root, recorded) if recorded and root is not None else ""
    if recorded and status in ("active", ""):
        # A board that cannot be read leaves the record to drift, which names the failure.
        print(f"phase reservation: Active, reservation {recorded} is {status or 'unread'} on the board")
        return 0
    baseline_file = phase.directory / BASELINE_FILE
    baseline = baseline_file.read_text(encoding="utf-8").strip() if baseline_file.is_file() else ""
    if root is None or not baseline:
        raise SystemExit("phase reservation: no repository or no phase baseline to compare against")
    # Paths a sibling phase owns, or no phase owns, are that phase's or the director's to answer.
    attribution = attribute(phases, changed_since_baseline(root, baseline), root)
    if isinstance(attribution, NoOpenPhaseHere):
        print("phase reservation: no open phase in this worktree; nothing to record")
        return 0
    scopes = attribution.by_phase.get(phase, [])
    stale = f"reservation {recorded} is {status} on the board"
    if not scopes:
        if recorded:
            print(f"phase reservation: {stale}, and nothing of this phase changed since its baseline",
                  file=sys.stderr)
            return 1
        print("phase reservation: nothing of this phase changed since its baseline; still awaiting first touch")
        return 0
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    answer = check(root, scopes, phase_session_id(session_id, phase.phase))
    if answer.status != CLEAR or answer.acquisition is None:
        print(f"phase reservation: cargo-berth check answered {answer.status}: {answer.message}", file=sys.stderr)
        return 1
    acquisition = own_acquisition(answer, root, phases, session_id)
    if acquisition is None or acquisition.reservation_id == recorded:
        if recorded:
            print(f"phase reservation: {stale}, and no live reservation of this phase replaces it", file=sys.stderr)
            return 1
        print("phase reservation: a sibling phase's reservation holds every changed path; still awaiting first touch")
        return 0
    _ = record_active(phase.directory, acquisition)
    print(f"phase reservation: Active, reservation {acquisition.reservation_id}"
          + (f"; {stale}" if recorded else ""))
    return 0


def _claim_command(args: argparse.Namespace) -> int:
    """Acquire a phase's reservation over its whole files list before any worker edits.

    A worker's first edit is claimed only when the worker runs the berth check itself: a Codex worker
    whose hook never fires, on either machine, edits with no reservation, and drift charges those
    paths to whichever reservation is held. Claiming at launch leaves no window. A directory entry
    (trailing `/`) is claimed at its first touch.
    """
    session_dir = Path(cast(str, args.session_dir))
    directory = phase_directory(session_dir, cast(str, args.phase))
    root = repository_root(Path(cast(str, args.repo)))
    if root is None:
        print("phase reservation: no repository or no files to claim; still awaiting first touch")
        return 0
    root = root.resolve()
    try:
        _record_worktree(directory, root)
    except StateRefused as refused:
        print(f"phase_reservation: {refused}", file=sys.stderr)
        return 2
    phases = open_phases(session_dir)
    phase = next((open_phase for open_phase in phases if open_phase.directory == directory), None)
    state = read_state(phase.directory) if phase is not None else {}
    if phase is None or _string(state.get("kind")) != AWAITING_FIRST_TOUCH:
        print(f"phase reservation: {_string(state.get('kind')) or 'no state'}; nothing to claim")
        return 0
    scopes = [entry for entry in _phase_files(phase) if not entry.endswith("/")]
    if not scopes:
        print("phase reservation: no repository or no files to claim; still awaiting first touch")
        return 0
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    answer = phase_check(root, scopes, session_id, phase)
    if answer.status in FAILS_OPEN:
        print(f"phase reservation: cargo-berth check answered {answer.status}; still awaiting first touch")
        return 0
    if answer.status != CLEAR:
        print(f"phase reservation: cargo-berth check answered {answer.status}: {answer.message}", file=sys.stderr)
        return 1
    acquisition = own_acquisition(answer, root, phases, session_id)
    if acquisition is None:
        print("phase reservation: a sibling phase's reservation holds every file; still awaiting first touch")
        return 0
    _ = record_active(phase.directory, acquisition)
    print(f"phase reservation: Active, reservation {acquisition.reservation_id}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record-active", help="write Active for a first touch the hook did not see")
    _ = record.add_argument("--session-dir", required=True)
    _ = record.add_argument("--phase", required=True)
    _ = record.add_argument("--repo", default=".")
    claim = commands.add_parser("claim", help="acquire a phase's reservation over its files before its workers open")
    _ = claim.add_argument("--session-dir", required=True)
    _ = claim.add_argument("--phase", required=True)
    _ = claim.add_argument("--repo", default=".")
    repair = commands.add_parser("repair", help="restore a lost record from berth's live reservation")
    _ = repair.add_argument("--session-dir", required=True)
    _ = repair.add_argument("--phase", required=True)
    _ = repair.add_argument("--repo", required=True)
    enrolled = commands.add_parser("enroll", help="write a phase's first state from the /sync board answer")
    _ = enrolled.add_argument("--board", required=True, choices=tuple(BOARD_STATES))
    done = commands.add_parser("committed", help="replace Active with the pending release of a checkpoint commit")
    _ = done.add_argument("--commit", required=True, help="any name of the checkpoint commit; its full object id is recorded")
    _ = done.add_argument("--ticket", default="", help="a style ticket whose merge releases the reservation")
    _ = done.add_argument("--repo", default=".")
    cleared = commands.add_parser("clear", help="delete a phase's state that owns no reservation, or a confirmed release")
    _ = cleared.add_argument("--released", default="", help="any name of the released checkpoint commit")
    _ = cleared.add_argument("--repo", default=".")
    held = commands.add_parser("release-held", help="release the reservation a style ticket's checkpoint kept, at its merge")
    _ = held.add_argument("--ticket", required=True)
    _ = held.add_argument("--merged", required=True, help="any name of the commit the ticket's merge brings in")
    for each in (enrolled, done, cleared, held):
        _ = each.add_argument("--session-dir", required=True)
        _ = each.add_argument("--phase", required=True)
    args = parser.parse_args(argv)
    command = cast(str, args.command)
    if command in ("enroll", "committed", "clear", "release-held"):
        return _state_command(command, args)
    if command == "repair":
        return _repair_command(args)
    return _claim_command(args) if command == "claim" else _record_active_command(args)


def _state_command(command: str, args: argparse.Namespace) -> int:
    phase = cast(str, args.phase)
    phase_dir = phase_directory(Path(cast(str, args.session_dir)), phase)
    try:
        if command == "enroll":
            print(f"phase {phase}: {enroll(phase_dir, phase, cast(str, args.board))}")
        elif command == "committed":
            ticket = cast(str, args.ticket)
            repo = Path(cast(str, args.repo))
            committed(phase_dir, commit_id(repo, cast(str, args.commit)), ticket, repository_root(repo) if ticket else None)
            print(f"phase {phase}: {PENDING}" + (f", held for ticket {ticket}" if ticket else ""))
        elif command == "release-held":
            ticket = cast(str, args.ticket)
            reservation = release_held(phase_dir, ticket, cast(str, args.merged))
            print(f"phase {phase}: released reservation {reservation}" if reservation
                  else f"phase {phase}: no reservation held for ticket {ticket}")
        else:
            cleared = clear(phase_dir, cast(str, args.released), Path(cast(str, args.repo)))
            print(f"phase {phase}: {'cleared' if cleared else 'no state to clear'}")
    except StateRefused as refused:
        print(f"phase_reservation: {refused}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
