#!/usr/bin/env python3
"""Frame Hana's camera on a known view over BRP and save a timed screenshot."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from difflib import get_close_matches
from enum import StrEnum
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from types import FrameType
from typing import IO, Literal, NamedTuple, NoReturn, NotRequired, TypedDict, cast, override


REFUSED_PORT = 15702
HOST = "127.0.0.1"
CALL_TIMEOUT = 10.0
# Above extras' 25 s capture deadline. The environment override exists for the tests only.
CAPTURE_TIMEOUT = float(os.environ.get("HANA_SHOT_CAPTURE_TIMEOUT", "30"))
# A full Mac window over a slow link.
COPY_TIMEOUT = 60.0
SETTLE_LIMIT = 5.0
UNCHANGED_FRAMES = 3
RESEND_LIMIT = 2.0
SETUP_LIMIT = 5.0
READY_LIMIT = 180.0
WARM_FRAME_MS = 50.0
WARM_LIMIT = 30.0
BLACK_MAXIMUM = 0.02
MAX_MARGIN = 0.45
SHOT_DIRECTORY_LIMIT = 1_073_741_824
MAX_AGE_SECONDS = 604_800
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Wakes the remote display and holds it until the run closes this shell's stdin; -t ends it if that never comes.
REMOTE_KEEP_AWAKE = "caffeinate -u -t 3600 & echo awake; cat > /dev/null; kill $!"

ORBIT_CAM = "hana_lagrange::orbit_cam::OrbitCam"
EDITOR_CAMERA = "hana::camera::editor_camera::EditorCamera"
GLOBAL_TRANSFORM = "bevy_transform::components::global_transform::GlobalTransform"
CAMERA = "bevy_camera::camera::Camera"
AABB = "bevy_camera::primitives::Aabb"
CHILD_OF = "bevy_ecs::hierarchy::ChildOf"
NAME = "bevy_ecs::name::Name"
VIEW_VISIBILITY = "bevy_camera::visibility::ViewVisibility"
INHERITED_VISIBILITY = "bevy_camera::visibility::InheritedVisibility"
OUTSIDE_FIT_BOUNDS = "hana_lagrange::fit::target::OutsideFitBounds"
SWITCH_SLIDER = "hana_catalyst::switch::SwitchSlider"
TOOL_PREFIX = "hana_catalyst::identity::Tool<"
WINDOW = "bevy_window::window::Window"
PRIMARY_WINDOW = "bevy_window::window::PrimaryWindow"
ANIMATE_TO_FIT = "hana_lagrange::fit::triggers::animate::AnimateToFit"
ZOOM_TO_TARGET = "hana::camera::zoom_to_target::ZoomToTarget"
RESET_HOME_COMMAND = "camera::reset_home"
CAMERA_SETTINGS = "hana::camera::settings::CameraSettings"
HOME_DURATION = ".home.animation_duration_milliseconds"
READY_LOG_LINE = "scene revealed"

MODES = ("fit", "pose", "hana-frame", "home")
FACES = ("+x", "-x", "+y", "-y", "+z", "-z")
CROPS = ("entity", "none")
SELECTOR = re.compile(r"(name|marker|tool|entity):(.+)")
TOOL_INDEX = re.compile(r"(.+)#([1-9][0-9]*)")
WINDOW_SIZE = re.compile(r"([1-9][0-9]{1,4})x([1-9][0-9]{1,4})")
VIEW_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
VIEW_KEYS = (
    "target", "mode", "face", "yaw", "pitch", "margin", "focus", "radius", "crop",
    "padding", "window", "setup", "settle", "note", "verified",
)
VIEWS_HEADER = """\
# Known Hana camera views for hana_shot (~/.claude/scripts/hana_shot/hana_shot.py).
# `hana_shot.py views add` writes a view; `views check` stamps `verified` on a pass.
# Prefer fit views: they follow their target. Selectors: name:, marker:, tool:<Kind>[#n].
# face = "+z" faces the target's local +z side; replaces world yaw/pitch in fit or pose.
"""


class Refused(Exception):
    """A request the script will not run; exit 2."""


class Failure(Exception):
    """A request that ran and could not finish; exit 1."""


def next_step(action: str) -> str:
    return f"; next step: {action}"


class CaptureTimeout(Exception):
    """A screenshot that never answered; exit 3, and never resend it."""


class ExitSignal(BaseException):
    """A catchable process signal that should unwind the active shot."""

    def __init__(self, number: int) -> None:
        super().__init__(number)
        self.number: int = number


@dataclass(frozen=True)
class ExitSignalsEnabled:
    """The first exit signal may unwind the active operation."""


@dataclass(frozen=True)
class ExitSignalsDeferred:
    """Cleanup must finish before an exit signal unwinds the operation."""


@dataclass(frozen=True)
class DeferredExitSignal:
    """The first exit signal received while cleanup was protected."""

    number: int


@dataclass(frozen=True)
class ExitSignalsLatched:
    """An unwind or final cleanup has begun; later exit signals do nothing."""


class ExitSignalLatch:
    """Make the first exit signal unwind once, after any cleanup in progress."""

    def __init__(self) -> None:
        self.state: ExitSignalsEnabled | ExitSignalsDeferred | DeferredExitSignal | ExitSignalsLatched
        self.state = ExitSignalsEnabled()

    def reset(self) -> None:
        self.state = ExitSignalsEnabled()

    def latch(self) -> None:
        self.state = ExitSignalsLatched()

    def interrupt(self, number: int) -> None:
        if isinstance(self.state, ExitSignalsEnabled):
            self.state = ExitSignalsLatched()
            raise ExitSignal(number)
        if isinstance(self.state, ExitSignalsDeferred):
            self.state = DeferredExitSignal(number)

    @contextmanager
    def defer_during_cleanup(self) -> Generator[None, None, None]:
        deferring = isinstance(self.state, ExitSignalsEnabled)
        if deferring:
            self.state = ExitSignalsDeferred()
        try:
            yield
        finally:
            if deferring and isinstance(self.state, ExitSignalsDeferred):
                self.state = ExitSignalsEnabled()
            elif deferring and isinstance(self.state, DeferredExitSignal):
                deferred = self.state
                self.state = ExitSignalsLatched()
                raise ExitSignal(deferred.number)


EXIT_SIGNAL_LATCH = ExitSignalLatch()


class BrpCallError(Failure):
    """A JSON-RPC error answer from Hana."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.code: int = code
        self.message: str = message


class RpcError(TypedDict):
    code: int
    message: str


class RpcReply(TypedDict, total=False):
    result: object
    error: RpcError


class FrameTimeValue(TypedDict):
    current: float | None


class Diagnostics(TypedDict):
    frame_time_ms: FrameTimeValue
    frame_count: float | None


class OrbitAnglesValue(TypedDict):
    yaw: float
    pitch: float


class OrbitValue(TypedDict):
    current: OrbitAnglesValue
    target: OrbitAnglesValue


class PanValue(TypedDict):
    current: list[float]
    target: list[float]


class ZoomValue(TypedDict):
    current: float
    target: float


class OrbitCamValue(TypedDict):
    orbit: OrbitValue
    pan: PanValue
    zoom: ZoomValue
    update_request: str


class TargetInfoValue(TypedDict):
    physical_size: list[int]
    scale_factor: float


class CameraComputedValue(TypedDict):
    clip_from_view: list[float]
    target_info: TargetInfoValue | None


class CameraValue(TypedDict):
    viewport: object
    computed: CameraComputedValue


class QueryRow(TypedDict):
    entity: int
    components: dict[str, object]
    has: NotRequired[dict[str, bool]]


class ComponentsReply(TypedDict):
    components: dict[str, object]
    errors: dict[str, object]


class HomeSettingsValue(TypedDict):
    animation_duration_milliseconds: int


class CameraSettingsValue(TypedDict):
    home: HomeSettingsValue


class CameraSettingsReply(TypedDict):
    value: CameraSettingsValue


class AabbValue(TypedDict):
    center: list[float]
    half_extents: list[float]


class ToolValue(TypedDict):
    id: int
    definition: str


class WindowResolutionValue(TypedDict):
    physical_width: int
    physical_height: int
    scale_factor_override: float | None
    scale_factor: float


class WindowValue(TypedDict):
    resolution: WindowResolutionValue
    window_level: str


@dataclass(frozen=True)
class WindowLevelPending:
    """The session has not yet prepared the macOS window for capture."""


@dataclass(frozen=True)
class WindowLeftAlone:
    """The session found no unique primary window to raise."""


@dataclass(frozen=True)
class WindowRaised:
    """A window this session must restore when it closes."""

    entity: int
    restore_level: str


class RectValue(TypedDict):
    x: int
    y: int
    width: int
    height: int


class ShotReply(TypedDict, total=False):
    path: str
    status: str
    capture_kind: str
    bounds_kind: str
    rect: RectValue


class VerifiedValue(TypedDict):
    sha: str
    date: str


class ViewValue(TypedDict, total=False):
    target: str
    mode: str
    face: str
    yaw: float
    pitch: float
    margin: float
    focus: list[float]
    radius: float
    crop: str
    padding: int
    window: str
    setup: str
    settle: int
    note: str
    verified: VerifiedValue


class TimingRecord(TypedDict):
    time: str
    host: str
    sha: str
    port: int
    label: str
    view: str | None
    target: str | None
    mode: str
    face: NotRequired[str]
    crop: str
    window: str
    frame_ms: float | None
    resolve_ms: float
    move_ms: float
    settle_ms: float
    settle_frames: int
    capture_ms: float
    crop_ms: float
    total_ms: float
    width: int
    height: int
    bytes: int


class LegacySuccess(TimingRecord):
    """A successful shot line written before invocation records existed."""


class FailureReason(StrEnum):
    TIMEOUT = "timeout"
    ALREADY_IN_PROGRESS = "already_in_progress"
    BLACK_CAPTURE = "black_capture"
    EMPTY_CROP = "empty_crop"
    NO_APP = "no_app"
    INVALID_REQUEST = "invalid_request"
    NO_TARGET = "no_target"
    INVALID_PNG = "invalid_png"
    COPY_FAILED = "copy_failed"
    BRP_ERROR = "brp_error"
    SHOT_FAILED = "shot_failed"


class PresentSession(TypedDict):
    state: Literal["present"]
    value: str


class AbsentSession(TypedDict):
    state: Literal["absent"]


class SuccessfulAttempt(TimingRecord):
    status: Literal["success"]
    image_paths: list[str]


class FailedAttempt(TypedDict):
    status: Literal["failure"]
    label: str
    view: str | None
    image_paths: list[str]
    failure_reason: FailureReason
    error: str
    face: NotRequired[str]


class SuccessfulInvocation(TimingRecord):
    status: Literal["success"]
    invocation_kind: Literal["shot", "views_check"]
    exit_code: Literal[0]
    session: PresentSession | AbsentSession
    attempts: list[SuccessfulAttempt | FailedAttempt]


class FailedInvocation(TypedDict):
    time: str
    host: str
    port: int
    status: Literal["failure"]
    invocation_kind: Literal["shot", "views_check"]
    exit_code: int
    failure_reason: FailureReason
    error: str
    view: NotRequired[str | None]
    target: NotRequired[str | None]
    mode: NotRequired[str]
    face: NotRequired[str]
    session: PresentSession | AbsentSession
    attempts: list[SuccessfulAttempt | FailedAttempt]


class CaptureFailure(NamedTuple):
    reason: FailureReason
    error: str


@dataclass(frozen=True)
class UnparsedShotRequest:
    """No shot has been parsed from this invocation's request yet."""


@dataclass
class InProgressCaptureInvocation:
    kind: Literal["shot", "views_check"] = "shot"
    attempts: list[SuccessfulAttempt | FailedAttempt] = field(default_factory=list)
    successful_timings: list[TimingRecord] = field(default_factory=list)
    failures: list[CaptureFailure] = field(default_factory=list)
    shot: Shot | UnparsedShotRequest = field(default_factory=UnparsedShotRequest)
    orientation: DefaultAngles | ExplicitAngles | TargetFace | ResolvedTargetFace = field(default_factory=lambda: DefaultAngles())


class LaunchState(TypedDict):
    pid: int
    port: int
    scratch: str
    log: str
    binary: str
    started: str
    uncommitted_paths: NotRequired[list[str]]


@dataclass(frozen=True)
class AbsentLaunchRecord:
    """No launch metadata exists at the requested path."""


@dataclass(frozen=True)
class UnreadableLaunchRecord:
    reason: str


@dataclass(frozen=True)
class WholeLaunchRecord:
    state: LaunchState


LaunchRecord = AbsentLaunchRecord | UnreadableLaunchRecord | WholeLaunchRecord


Vec3 = tuple[float, float, float]


class Pose(NamedTuple):
    """Where the orbit camera looks from: focus, angles in radians, and distance."""

    focus: Vec3
    yaw: float
    pitch: float
    radius: float


class CameraState(NamedTuple):
    """One read of the editor camera: its orbit pose and what projection needs."""

    pose: Pose
    update_request: str
    transform: list[float]
    clip_from_view: list[float]
    size: tuple[int, int]
    viewport_set: bool
    orbit_cam: OrbitCamValue


class Sample(NamedTuple):
    state: CameraState
    frame: int


class Settled(NamedTuple):
    state: CameraState
    moved: bool
    frames: int


class Target(NamedTuple):
    entity: int
    label: str
    has_aabb: bool


class Rect(NamedTuple):
    x: int
    y: int
    width: int
    height: int


class RemoteFile(NamedTuple):
    """A shot Hana writes on another machine, before the copy back."""

    host: str
    path: str


class Bounds(NamedTuple):
    """The hierarchy data a crop needs: parents by child, and visible boxes by entity."""

    parents: dict[int, int]
    boxes: dict[int, tuple[list[float], AabbValue]]


@dataclass(frozen=True)
class DefaultAngles:
    """No angles supplied: use world yaw and pitch zero."""


class ExplicitAngles(NamedTuple):
    yaw: float
    pitch: float


class TargetFace(NamedTuple):
    """A target-local axis awaiting this run's world transform."""

    axis: str


class ResolvedTargetFace(NamedTuple):
    axis: str
    yaw: float
    pitch: float


@dataclass(frozen=True)
class Shot:
    """One normalized shot request, from a stored view or from the command line."""

    label: str
    view: str | None
    target: str | None
    mode: str
    orientation: DefaultAngles | ExplicitAngles | TargetFace | ResolvedTargetFace = DefaultAngles()
    margin: float = 0.1
    focus: Vec3 | None = None
    radius: float | None = None
    crop: str = "none"
    padding: int = 0
    window: str | None = None
    setup: str | None = None
    settle: int = 0

    @property
    def yaw(self) -> float:
        if isinstance(self.orientation, TargetFace):
            raise RuntimeError("target face must be resolved before reading yaw")
        return 0.0 if isinstance(self.orientation, DefaultAngles) else self.orientation.yaw

    @property
    def pitch(self) -> float:
        if isinstance(self.orientation, TargetFace):
            raise RuntimeError("target face must be resolved before reading pitch")
        return 0.0 if isinstance(self.orientation, DefaultAngles) else self.orientation.pitch


@dataclass
class Result:
    """What one shot produced, for the caller and the timing log."""

    shot: Shot
    path: Path
    width: int
    height: int
    size_bytes: int
    crop_kind: str
    phases: dict[str, float]
    settle_frames: int


POOL = ThreadPoolExecutor(max_workers=6)


# ---------------------------------------------------------------------------
# BRP


@dataclass
class TargetEvidence:
    """Names and geometry already read this run, for hints without another BRP call."""

    names: dict[int, str] = field(default_factory=dict)
    parents: dict[int, int] = field(default_factory=dict)
    aabb_entities: set[int] = field(default_factory=set)
    selector_candidates: list[str] = field(default_factory=list)

    def observe(self, rows: list[QueryRow]) -> None:
        for row in rows:
            entity, components = row["entity"], row["components"]
            name = components.get(NAME)
            if isinstance(name, str):
                self.names[entity] = name
            parent = components.get(CHILD_OF)
            if isinstance(parent, int):
                self.parents[entity] = parent
            if AABB in components or row.get("has", {}).get(AABB, False):
                self.aabb_entities.add(entity)
            elif row.get("has", {}).get(AABB) is False:
                self.aabb_entities.discard(entity)

    def parent_fit_hint(self, entity: int) -> str:
        visited = {entity}
        while entity in self.parents:
            entity = self.parents[entity]
            if entity in visited:
                break
            visited.add(entity)
            if entity in self.aabb_entities:
                name = f" ({self.names[entity]})" if entity in self.names else ""
                return f"; nearest ancestor with an AABB: --target entity:{entity}{name}"
        return ""


class Brp:
    """JSON-RPC over HTTP to one Hana instance; each call uses its own connection."""

    def __init__(self, port: int) -> None:
        self.port: int = port
        self.target_evidence: TargetEvidence = TargetEvidence()

    def call(self, method: str, params: object = None, timeout: float = CALL_TIMEOUT) -> object:
        body: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        connection = http.client.HTTPConnection(HOST, self.port, timeout=timeout)
        try:
            connection.request("POST", "/", json.dumps(body), {"content-type": "application/json"})
            raw = connection.getresponse().read()
        except TimeoutError as exc:
            if method == "brp_extras/screenshot":
                raise CaptureTimeout(
                    f"screenshot gave no answer in {timeout:.0f} s; stopped without resending "
                    + "(a late capture can crash Hana)"
                ) from exc
            raise Failure(f"{method} gave no answer in {timeout:.0f} s on port {self.port}") from exc
        except OSError as exc:
            raise Failure(f"no Hana answers on port {self.port}: {exc}") from exc
        finally:
            connection.close()
        reply = cast(RpcReply, json.loads(raw))
        error = reply.get("error")
        if error is not None:
            raise BrpCallError(method, error["code"], error["message"])
        return reply.get("result")

    def query(self, params: dict[str, object]) -> list[QueryRow]:
        rows = cast(list[QueryRow], self.call("world.query", params))
        self.target_evidence.observe(rows)
        return rows

    def components(self, entity: int, paths: list[str]) -> dict[str, object]:
        reply = cast(ComponentsReply, self.call("world.get_components", {"entity": entity, "components": paths}))
        return reply["components"]

    def diagnostics(self) -> Diagnostics:
        return cast(Diagnostics, self.call("brp_extras/get_diagnostics"))

    def trigger(self, event: str, value: dict[str, object]) -> None:
        _ = self.call("world.trigger_event", {"event": event, "value": value})

    def invoke(self, command: str) -> None:
        _ = self.call("hana/invoke_command", {"command": command})

    def mutate(self, entity: int, component: str, path: str, value: object) -> None:
        _ = self.call(
            "world.mutate_components",
            {"entity": entity, "component": component, "path": path, "value": value},
        )


def frame_number(diagnostics: Diagnostics) -> int:
    count = diagnostics["frame_count"]
    if count is None:
        raise Failure("Hana reports no frame count yet; wait for startup to finish")
    return int(count)


def check_port(port: int) -> None:
    if port == REFUSED_PORT:
        raise Refused(f"port {REFUSED_PORT} is the user's own Hana; launch a test instance on another port")
    if not 1024 <= port <= 65535:
        raise Refused(f"--port must be 1024 to 65535, got {port}")


def elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


# ---------------------------------------------------------------------------
# Camera reads and moves


def camera_state(components: dict[str, object]) -> CameraState:
    orbit_cam = cast(OrbitCamValue, components[ORBIT_CAM])
    camera = cast(CameraValue, components[CAMERA])
    transform = cast(list[float], components[GLOBAL_TRANSFORM])
    target_info = camera["computed"]["target_info"]
    if target_info is None:
        raise Failure("the camera has no render target yet; wait for startup to finish")
    focus = orbit_cam["pan"]["current"]
    angles = orbit_cam["orbit"]["current"]
    pose = Pose(
        (focus[0], focus[1], focus[2]),
        angles["yaw"],
        angles["pitch"],
        orbit_cam["zoom"]["current"],
    )
    width, height = target_info["physical_size"]
    return CameraState(
        pose,
        orbit_cam["update_request"],
        transform,
        camera["computed"]["clip_from_view"],
        (width, height),
        camera["viewport"] is not None,
        orbit_cam,
    )


class Session:
    """One script run against one Hana: the camera, caches, and run-wide settings."""

    def __init__(self, brp: Brp, remote: str | None = None) -> None:
        self.brp: Brp = brp
        self.remote: str | None = remote
        self.remote_shots: int = 0
        # True when Hana runs on macOS; start() asks a remote host.
        self.macos: bool = remote is None and sys.platform == "darwin"
        self.keep_awake: subprocess.Popen[bytes] | None = None
        self.camera: int = 0
        self.frame_ms: float | None = None
        self.tool_types: list[str] | None = None
        self.rect_support: bool | None = None
        self.last_move: tuple[object, ...] | None = None
        self.last_state: CameraState | None = None
        self.window_level: WindowLevelPending | WindowLeftAlone | WindowRaised = WindowLevelPending()

    def start(self) -> CameraState:
        system = None if self.remote is None else POOL.submit(remote_system, self.remote)
        camera_rows = POOL.submit(
            self.brp.query,
            {"data": {"components": [ORBIT_CAM, GLOBAL_TRANSFORM, CAMERA]}, "filter": {"with": [EDITOR_CAMERA]}},
        )
        diagnostics = POOL.submit(self.brp.diagnostics)
        rows = camera_rows.result()
        if len(rows) != 1:
            raise Failure(f"expected one editor camera, found {len(rows)}")
        self.camera = rows[0]["entity"]
        self.frame_ms = diagnostics.result()["frame_time_ms"]["current"]
        if system is not None:
            self.macos = system.result() == "Darwin"
        return camera_state(rows[0]["components"])

    def close(self) -> None:
        """Latch exit signals, restore the window, then end the keep-awake."""
        EXIT_SIGNAL_LATCH.latch()
        self.restore_window_and_end_keep_awake()

    def restore_window_and_end_keep_awake(self) -> None:
        """Undo window preparation without changing the active outcome."""
        window_level = self.window_level
        if isinstance(window_level, WindowRaised):
            try:
                self.brp.mutate(window_level.entity, WINDOW, ".window_level", window_level.restore_level)
            except Failure as exc:
                print(
                    f"hana_shot: could not put the window level back to {window_level.restore_level}: {exc}",
                    file=sys.stderr,
                )
        self.window_level = WindowLevelPending()
        if self.keep_awake is None:
            return
        keep_awake_process = self.keep_awake
        try:
            if self.remote is None:
                keep_awake_process.terminate()
            _ = keep_awake_process.communicate(timeout=CALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            keep_awake_process.kill()
            _ = keep_awake_process.wait()
        self.keep_awake = None

    def read_camera(self) -> CameraState:
        return camera_state(self.brp.components(self.camera, [ORBIT_CAM, GLOBAL_TRANSFORM, CAMERA]))

    def sample(self) -> Sample:
        diagnostics = POOL.submit(self.brp.diagnostics)
        state = self.read_camera()
        return Sample(state, frame_number(diagnostics.result()))

    def frame_seconds(self) -> float:
        return max(0.005, (self.frame_ms or 16.0) / 1000)

    def settle(self, before: CameraState, extra_frames: int, limit: float = SETTLE_LIMIT) -> Settled:
        """Wait until the camera has moved and held still for a frame, or never moved.

        A BRP call can land in the frame that answered the previous call, so frames are
        counted from the frame counter, never from the number of calls.
        """
        deadline = time.monotonic() + limit
        first: int | None = None
        moved_at: int | None = None
        previous: Sample | None = None
        while True:
            current = self.sample()
            if first is None:
                first = current.frame
            if moved_at is None and not same_view(current.state, before):
                moved_at = current.frame
            held = (
                previous is not None
                and current.frame > previous.frame
                and same_view(current.state, previous.state)
                and current.state.update_request == "None"
            )
            if moved_at is not None and held and current.frame >= moved_at + 1 + extra_frames:
                return Settled(current.state, True, current.frame - first)
            if moved_at is None and current.frame >= first + UNCHANGED_FRAMES + extra_frames:
                return Settled(current.state, False, current.frame - first)
            if time.monotonic() > deadline:
                raise Failure(
                    f"the camera did not settle within {limit:.0f} s"
                    + next_step(
                        "rerun the same command once; if it fails again, read the pose with `hana_shot.py pose` "
                        + "and shoot with `--mode pose --focus x,y,z --radius N` using that focus and radius "
                        + "(no fit move)"
                    )
                )
            previous = current

    def wait_frames(self, frames: int) -> None:
        if frames <= 0:
            return
        start = frame_number(self.brp.diagnostics())
        while frame_number(self.brp.diagnostics()) < start + frames:
            pass

    def fit(self, target: int, shot: Shot, margin: float) -> None:
        self.brp.trigger(ANIMATE_TO_FIT, {
            "camera": self.camera,
            "target": target,
            "yaw": shot.yaw,
            "pitch": shot.pitch,
            "margin": margin,
            "anchor": "Center",
            "offset_px": [0.0, 0.0],
            "viewport_insets": {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0},
            "duration": {"secs": 0, "nanos": 0},
            "easing": "CubicOut",
        })

    def set_pose(self, before: CameraState, pose: Pose) -> None:
        value = cast(OrbitCamValue, json.loads(json.dumps(before.orbit_cam)))
        focus = [pose.focus[0], pose.focus[1], pose.focus[2]]
        angles: OrbitAnglesValue = {"yaw": pose.yaw, "pitch": pose.pitch}
        value["pan"]["current"] = focus
        value["pan"]["target"] = list(focus)
        value["orbit"]["current"] = angles
        value["orbit"]["target"] = {"yaw": pose.yaw, "pitch": pose.pitch}
        value["zoom"]["current"] = pose.radius
        value["zoom"]["target"] = pose.radius
        value["update_request"] = "ForceUpdate"
        self.brp.mutate(self.camera, ORBIT_CAM, "", value)


def same_view(first: CameraState, second: CameraState) -> bool:
    return first.transform == second.transform and first.pose == second.pose and first.size == second.size


def move(session: Session, shot: Shot, target: Target | None, before: CameraState) -> Settled:
    """Send the shot's camera move and wait for it to settle."""
    extra = shot.settle
    if shot.mode == "fit":
        assert target is not None
        session.fit(target.entity, shot, shot.margin)
        settled = session.settle(before, extra)
        if settled.moved:
            return settled
        # An unchanged camera means the fit was rejected, or the camera already held this
        # fit. A fit with another margin tells the two apart.
        probe = shot.margin + 0.05 if shot.margin + 0.05 <= MAX_MARGIN else shot.margin - 0.05
        session.fit(target.entity, shot, probe)
        probed = session.settle(settled.state, 0)
        if not probed.moved:
            raise Failure(
                f"fit on {target.label} was rejected: the camera did not move "
                + "(no visible geometry under the target, or an animation owns the camera)"
                + f"; retry without --view: shot --target {shlex.quote(target.label)} --mode pose "
                + "--focus 0,0,0 --radius 6 (set the focus and radius for your scene), "
                + "or fit a parent target that has geometry"
                + session.brp.target_evidence.parent_fit_hint(target.entity)
            )
        session.fit(target.entity, shot, shot.margin)
        settled = session.settle(probed.state, extra)
        if not settled.moved:
            raise Failure(f"fit on {target.label} did not return to margin {shot.margin}")
        return settled
    if shot.mode == "pose":
        assert shot.focus is not None and shot.radius is not None
        session.set_pose(before, Pose(shot.focus, shot.yaw, shot.pitch, shot.radius))
        return session.settle(before, extra)
    if shot.mode == "hana-frame":
        assert target is not None
        session.brp.trigger(ZOOM_TO_TARGET, {"entity": target.entity})
        return session.settle(before, extra)
    # Home animates over two seconds; a zero duration lands on the same pose in one frame.
    duration = home_duration(session)
    if duration:
        set_home_duration(session, 0)
    try:
        session.brp.invoke(RESET_HOME_COMMAND)
        return session.settle(before, extra)
    finally:
        if duration:
            set_home_duration(session, duration)


def home_duration(session: Session) -> int | None:
    try:
        reply = cast(CameraSettingsReply, session.brp.call("world.get_resources", {"resource": CAMERA_SETTINGS}))
    except BrpCallError:
        return None
    return reply["value"]["home"]["animation_duration_milliseconds"]


def set_home_duration(session: Session, milliseconds: int) -> None:
    _ = session.brp.call(
        "world.mutate_resources", {"resource": CAMERA_SETTINGS, "path": HOME_DURATION, "value": milliseconds},
    )


# ---------------------------------------------------------------------------
# Targets


class Selector(NamedTuple):
    kind: str
    value: str
    ordinal: int | None


def parse_selector(text: str) -> Selector:
    match = SELECTOR.fullmatch(text)
    if match is None:
        raise Refused(f"selector {text!r} must be name:<Name>, marker:<type path>, tool:<Kind>[#n] or entity:<id>")
    kind, value = match.group(1), match.group(2)
    ordinal: int | None = None
    if kind == "tool":
        indexed = TOOL_INDEX.fullmatch(value)
        if indexed is not None:
            value, ordinal = indexed.group(1), int(indexed.group(2))
    if kind == "entity" and not value.isdigit():
        raise Refused(f"entity:<id> takes a number, got {value!r}")
    return Selector(kind, value, ordinal)


def tool_kind(type_path: str) -> str:
    inner = type_path[len(TOOL_PREFIX):-1]
    return inner.rsplit("::", 1)[-1]


class ToolRow(NamedTuple):
    entity: int
    kind: str
    tool_id: int
    definition: str
    has_aabb: bool


def tool_rows(session: Session) -> list[ToolRow]:
    if session.tool_types is None:
        registered = cast(list[str], session.brp.call("world.list_components"))
        session.tool_types = [path for path in registered if path.startswith(TOOL_PREFIX)]
    rows = session.brp.query({
        "data": {"option": [*session.tool_types, NAME, CHILD_OF], "has": [AABB]},
        "filter": {"with": [SWITCH_SLIDER]},
    })
    tools: list[ToolRow] = []
    for row in rows:
        for path, value in row["components"].items():
            if path.startswith(TOOL_PREFIX):
                tool = cast(ToolValue, value)
                tools.append(ToolRow(row["entity"], tool_kind(path), tool["id"], tool["definition"], row.get("has", {}).get(AABB, False)))
                _ = session.brp.target_evidence.names.setdefault(row["entity"], f"{tool_kind(path)} ({tool['definition']})")
    tools.sort(key=lambda tool: tool.tool_id)
    return tools


def resolve(session: Session, text: str) -> list[Target]:
    """Every entity the selector names right now; ids change on every launch."""
    selector = parse_selector(text)
    evidence = session.brp.target_evidence
    evidence.selector_candidates = []
    if selector.kind == "entity":
        entity = int(selector.value)
        try:
            present = cast(list[str], session.brp.call("world.list_components", {"entity": entity}))
        except BrpCallError:
            return []
        return [Target(entity, text, AABB in present)]
    if selector.kind == "name":
        rows = session.brp.query({"data": {"components": [NAME], "option": [CHILD_OF], "has": [AABB]}})
        evidence.selector_candidates = [cast(str, row["components"][NAME]) for row in rows]
        return [
            Target(row["entity"], text, row.get("has", {}).get(AABB, False))
            for row in rows
            if row["components"].get(NAME) == selector.value
        ]
    if selector.kind == "marker":
        try:
            rows = session.brp.query({
                "data": {"option": [NAME, CHILD_OF], "has": [AABB]},
                "filter": {"with": [selector.value]},
            })
        except BrpCallError as exc:
            raise Failure(f"marker {selector.value} is not a registered component: {exc.message}") from exc
        return [Target(row["entity"], text, row.get("has", {}).get(AABB, False)) for row in rows]
    wanted = selector.value.lower()
    tools = tool_rows(session)
    evidence.selector_candidates = sorted({tool.kind for tool in tools})
    matches = [
        tool for tool in tools
        if tool.kind.lower() == wanted or tool.definition.lower() == wanted
    ]
    if selector.ordinal is not None:
        if selector.ordinal > len(matches):
            return []
        matches = [matches[selector.ordinal - 1]]
    return [Target(tool.entity, text, tool.has_aabb) for tool in matches]


def closest_target_hint(evidence: TargetEvidence, text: str) -> str:
    selector = parse_selector(text)
    if selector.kind in ("name", "tool"):
        candidates = {candidate.lower(): candidate for candidate in evidence.selector_candidates}
        closest = get_close_matches(selector.value.lower(), sorted(candidates), n=5)
        if closest:
            return "; closest: " + ", ".join(f"{selector.kind}:{candidates[name]}" for name in closest)
    return ""


def resolve_one(session: Session, shot: Shot) -> Target | None:
    if shot.target is None:
        return None
    targets = resolve(session, shot.target)
    if not targets and shot.setup is not None:
        targets = run_setup(session, shot)
    if len(targets) != 1:
        evidence = session.brp.target_evidence
        found = ", ".join(
            f"entity:{target.entity} ({evidence.names.get(target.entity, 'unnamed')})" for target in targets
        ) or "nothing"
        hint = ""
        if not targets:
            selector = parse_selector(shot.target)
            if selector.kind == "entity":
                hint = "; entity ids change when Hana restarts; select by name:, tool: or marker: instead"
            else:
                hint = closest_target_hint(evidence, shot.target)
        raise Failure(f"{shot.target} must name exactly one entity; found {found}{hint}")
    return targets[0]


def run_setup(session: Session, shot: Shot) -> list[Target]:
    """Run the view's Hana command, then wait for its target and a still camera."""
    assert shot.setup is not None and shot.target is not None
    session.brp.invoke(shot.setup)
    deadline = time.monotonic() + SETUP_LIMIT
    targets: list[Target] = []
    while not targets:
        if time.monotonic() > deadline:
            raise Failure(
                f"setup {shot.setup} ran but {shot.target} did not appear within {SETUP_LIMIT:.0f} s"
                + closest_target_hint(session.brp.target_evidence, shot.target)
                + next_step("run `hana_shot.py targets` to list the selectors that resolve now")
            )
        session.wait_frames(1)
        targets = resolve(session, shot.target)
    before = session.read_camera()
    _ = session.settle(before, 0)
    return targets


# ---------------------------------------------------------------------------
# Hierarchy crop


def start_bounds(session: Session) -> tuple[Future[list[QueryRow]], Future[list[QueryRow]]]:
    parents = POOL.submit(session.brp.query, {"data": {"components": [CHILD_OF]}})
    boxes = POOL.submit(session.brp.query, {
        "data": {
            "components": [AABB, GLOBAL_TRANSFORM],
            "option": [VIEW_VISIBILITY, INHERITED_VISIBILITY],
            "has": [OUTSIDE_FIT_BOUNDS],
        },
    })
    return parents, boxes


def finish_bounds(futures: tuple[Future[list[QueryRow]], Future[list[QueryRow]]]) -> Bounds:
    parents: dict[int, int] = {}
    for row in futures[0].result():
        parents[row["entity"]] = cast(int, row["components"][CHILD_OF])
    boxes: dict[int, tuple[list[float], AabbValue]] = {}
    for row in futures[1].result():
        components = row["components"]
        if row.get("has", {}).get(OUTSIDE_FIT_BOUNDS, False):
            continue
        if not visible(components.get(VIEW_VISIBILITY), components.get(INHERITED_VISIBILITY)):
            continue
        boxes[row["entity"]] = (cast(list[float], components[GLOBAL_TRANSFORM]), cast(AabbValue, components[AABB]))
    return Bounds(parents, boxes)


def visible(view: object, inherited: object) -> bool:
    if inherited is False:
        return False
    if isinstance(view, bool):
        return view
    if isinstance(view, int):
        return view & 1 == 1
    return True


def descendants(parents: dict[int, int], root: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    found = {root}
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def affine_point(matrix: list[float], point: Vec3) -> Vec3:
    """Apply a Bevy GlobalTransform, serialized as x, y, z axes then translation."""
    x, y, z = point
    return (
        matrix[0] * x + matrix[3] * y + matrix[6] * z + matrix[9],
        matrix[1] * x + matrix[4] * y + matrix[7] * z + matrix[10],
        matrix[2] * x + matrix[5] * y + matrix[8] * z + matrix[11],
    )


def face_angles(matrix: list[float], axis: str) -> tuple[float, float]:
    """Orbit yaw/pitch for the camera offset along a target-local axis."""
    if axis not in FACES:
        raise Refused(f"--face {axis!r} is invalid; choose one of {', '.join(FACES)}")
    if len(matrix) != 12 or not all(math.isfinite(value) for value in matrix):
        raise Refused("--face needs a finite GlobalTransform; choose another --target or drop --face and use --yaw/--pitch")
    offset = "xyz".index(axis[1]) * 3
    sign = -1.0 if axis[0] == "-" else 1.0
    direction = [sign * value for value in matrix[offset:offset + 3]]
    length = math.hypot(*direction)
    if length == 0.0:
        raise Refused(f"--face {axis} has a zero-length axis; choose another --face or use --yaw/--pitch")
    x, y, z = (value / length for value in direction)
    if abs(y) >= math.cos(math.radians(1.0)):
        pitch = "1.55" if y > 0 else "-1.55"
        raise Refused(
            f"--face {axis} is within 1 degree of vertical: yaw is undefined and Hana's orbit pitch clamp prevents reaching it; "
            + f"drop --face and use --mode pose --yaw 0 --pitch {pitch} with --focus x,y,z --radius R to come close"
        )
    return math.atan2(x, z), math.asin(max(-1.0, min(1.0, y)))


def resolve_face(session: Session, shot: Shot, target: Target | None) -> Shot:
    if not isinstance(shot.orientation, (TargetFace, ResolvedTargetFace)):
        return shot
    assert target is not None
    components = session.brp.components(target.entity, [GLOBAL_TRANSFORM])
    matrix = cast(list[float], components[GLOBAL_TRANSFORM])
    yaw, pitch = face_angles(matrix, shot.orientation.axis)
    focus = shot.focus
    if shot.mode == "pose" and focus is None:
        focus = affine_point(matrix, (0.0, 0.0, 0.0))
    return replace(shot, orientation=ResolvedTargetFace(shot.orientation.axis, yaw, pitch), focus=focus)


def inverse_affine_point(matrix: list[float], point: Vec3) -> Vec3:
    a, b, c = matrix[0], matrix[3], matrix[6]
    d, e, f = matrix[1], matrix[4], matrix[7]
    g, h, i = matrix[2], matrix[5], matrix[8]
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(determinant) < 1e-12:
        raise Failure("the camera transform cannot be inverted")
    x = point[0] - matrix[9]
    y = point[1] - matrix[10]
    z = point[2] - matrix[11]
    return (
        ((e * i - f * h) * x - (b * i - c * h) * y + (b * f - c * e) * z) / determinant,
        (-(d * i - f * g) * x + (a * i - c * g) * y - (a * f - c * d) * z) / determinant,
        ((d * h - e * g) * x - (a * h - b * g) * y + (a * e - b * d) * z) / determinant,
    )


def project(clip_from_view: list[float], view: Vec3) -> tuple[float, float] | None:
    """Normalized device x and y of a view-space point, or None behind the camera."""
    x, y, z = view
    m = clip_from_view
    clip_x = m[0] * x + m[4] * y + m[8] * z + m[12]
    clip_y = m[1] * x + m[5] * y + m[9] * z + m[13]
    clip_w = m[3] * x + m[7] * y + m[11] * z + m[15]
    if clip_w <= 1e-6:
        return None
    return clip_x / clip_w, clip_y / clip_w


def hierarchy_rect(bounds: Bounds, root: int, state: CameraState, padding: int, evidence: TargetEvidence) -> Rect:
    """Pixel rectangle around the visible boxes under root, as the fit measures them."""
    if state.viewport_set:
        raise Failure("the camera has a viewport; the hierarchy crop covers full-window cameras only")
    width, height = state.size
    left, top, right, bottom = math.inf, math.inf, -math.inf, -math.inf
    any_box = False
    for entity in descendants(bounds.parents, root):
        entry = bounds.boxes.get(entity)
        if entry is None:
            continue
        any_box = True
        transform, aabb = entry
        center, half = aabb["center"], aabb["half_extents"]
        for sx in (-1.0, 1.0):
            for sy in (-1.0, 1.0):
                for sz in (-1.0, 1.0):
                    local = (center[0] + sx * half[0], center[1] + sy * half[1], center[2] + sz * half[2])
                    ndc = project(state.clip_from_view, inverse_affine_point(state.transform, affine_point(transform, local)))
                    if ndc is None:
                        left, top, right, bottom = 0.0, 0.0, float(width), float(height)
                        continue
                    px = (ndc[0] + 1) * 0.5 * width
                    py = (1 - ndc[1]) * 0.5 * height
                    left, right = min(left, px), max(right, px)
                    top, bottom = min(top, py), max(bottom, py)
    if not any_box:
        raise Failure(
            "the target has no visible mesh bounds to crop to"
            + evidence.parent_fit_hint(root)
            + next_step("rerun with --crop none to shoot without a crop")
        )
    x0 = max(0, math.floor(left) - padding)
    y0 = max(0, math.floor(top) - padding)
    x1 = min(width, math.ceil(right) + padding)
    y1 = min(height, math.ceil(bottom) + padding)
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise Failure("the crop is empty: the target is off screen")
    return Rect(x0, y0, x1 - x0, y1 - y0)


# ---------------------------------------------------------------------------
# Capture


def capture(session: Session, params: dict[str, object]) -> ShotReply:
    """Take one screenshot, resending only when extras refuses because one is in flight."""
    deadline = time.monotonic() + RESEND_LIMIT
    while True:
        try:
            return cast(ShotReply, session.brp.call("brp_extras/screenshot", params, timeout=CAPTURE_TIMEOUT))
        except BrpCallError as exc:
            if "already in progress" not in exc.message or time.monotonic() > deadline:
                raise
            time.sleep(session.frame_seconds())


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != PNG_SIGNATURE:
        raise Failure(f"{path} is not a PNG")
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def is_png(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE
    except OSError:
        return False


def remote_file(session: Session) -> RemoteFile | None:
    """A unique name on the remote host, for the same reason staging_path gives one."""
    if session.remote is None:
        return None
    session.remote_shots += 1
    return RemoteFile(session.remote, f"/tmp/hana_shot_{os.getpid()}_{session.remote_shots}.png")


def fetch(remote: RemoteFile, staging: Path) -> None:
    """Copy a shot from the remote host to staging, then remove it there in the background.

    Tailscale SSH can report exit 0 after a failure, so the copy counts only when a PNG arrived.
    """
    source = f"{remote.host}:{remote.path}"
    try:
        copied = subprocess.run(
            ["scp", "-q", source, str(staging)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=COPY_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f"could not copy {source}: {exc}") from exc
    if copied.returncode != 0 or not is_png(staging):
        detail = copied.stderr.strip() or f"scp exited {copied.returncode}"
        raise Failure(f"no PNG came back from {source} ({detail}); the shot is left there")
    _ = POOL.submit(remove_remote, remote)


def remove_remote(remote: RemoteFile) -> None:
    _ = subprocess.run(
        ["ssh", remote.host, "rm", "-f", remote.path],
        stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=COPY_TIMEOUT,
    )


def remote_system(host: str) -> str:
    """The remote kernel name; an empty answer fails, since Tailscale SSH can report exit 0 after a failure."""
    try:
        answer = subprocess.run(
            ["ssh", host, "uname", "-s"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=CALL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f"could not ask {host} for its system over ssh: {exc}") from exc
    system = answer.stdout.strip()
    if answer.returncode != 0 or not system:
        detail = answer.stderr.strip() or f"ssh exited {answer.returncode}"
        raise Failure(f"ssh {host} uname -s gave no answer ({detail})")
    return system


def keep_awake(host: str) -> subprocess.Popen[bytes]:
    """Hold the remote display awake until Session.close; returns once caffeinate runs."""
    process = subprocess.Popen(
        ["ssh", host, REMOTE_KEEP_AWAKE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    started = POOL.submit(cast(IO[bytes], process.stdout).readline)
    try:
        awake = started.result(timeout=CALL_TIMEOUT).strip() == b"awake"
    except TimeoutError:
        awake = False
    if not awake:
        process.kill()
        _ = process.wait()
        raise Failure(f"could not keep the display awake on {host}: caffeinate did not start over ssh")
    return process


def magick() -> str:
    found = shutil.which("magick")
    if found is None:
        raise Failure("this extras has no rect crop and ImageMagick (magick) is not on PATH")
    return found


def magick_crop(source: Path, rect: Rect, destination: Path) -> None:
    geometry = f"{rect.width}x{rect.height}+{rect.x}+{rect.y}"
    result = subprocess.run(
        [magick(), str(source), "-crop", geometry, "+repage", str(destination)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise Failure(f"magick crop failed: {result.stderr.strip()}")


def brightest(path: Path) -> float:
    result = subprocess.run(
        [magick(), str(path), "-scale", "64x64", "-format", "%[fx:maxima]", "info:"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise Failure(f"magick could not read {path}: {result.stderr.strip()}")
    return float(result.stdout.strip())


def staging_path(final: Path) -> Path:
    """A unique sibling name: extras hangs on two back-to-back requests for one path."""
    return final.with_name(f".{final.stem}-{os.getpid()}-{time.monotonic_ns()}.png")


def take(session: Session, shot: Shot, final: Path) -> Result:
    total_start = time.perf_counter()
    phases: dict[str, float] = {}

    start = time.perf_counter()
    if shot.window is not None:
        ensure_window(session, shot.window)
    if session.macos:
        keep_visible(session)
    target_future = POOL.submit(resolve_one, session, shot)
    before = session.read_camera()
    target = target_future.result()
    shot = resolve_face(session, shot, target)
    phases["resolve_ms"] = elapsed_ms(start)

    move_key = (shot.mode, target.entity if target else None, shot.yaw, shot.pitch, shot.margin, shot.focus, shot.radius)
    start = time.perf_counter()
    if move_key == session.last_move and session.last_state is not None and same_view(before, session.last_state):
        settled = Settled(before, False, 0)
        phases["move_ms"] = 0.0
        phases["settle_ms"] = 0.0
    else:
        settled = move(session, shot, target, before)
        phases["move_ms"] = 0.0
        phases["settle_ms"] = elapsed_ms(start)
    session.last_move = move_key
    session.last_state = settled.state

    staging = staging_path(final)
    remote = remote_file(session)
    params: dict[str, object] = {"path": str(staging) if remote is None else remote.path, "camera": session.camera}
    crop_kind = "none"
    rect: Rect | None = None
    if shot.crop == "entity" and target is not None:
        if target.has_aabb:
            params["entity"] = target.entity
            params["padding"] = shot.padding
            crop_kind = "entity"
        else:
            # Visibility is read after the move: parts culled from the old pose show in the new one.
            rect = hierarchy_rect(
                finish_bounds(start_bounds(session)), target.entity, settled.state, shot.padding,
                session.brp.target_evidence,
            )
            crop_kind = "rect"

    start = time.perf_counter()
    try:
        cropped = False
        if rect is not None and session.rect_support is not False:
            try:
                _ = capture(session, {**params, "rect": rect._asdict()})
                session.rect_support = True
                cropped = True
            except BrpCallError as exc:
                if "unknown field `rect`" not in exc.message:
                    raise
                session.rect_support = False
        if not cropped:
            _ = capture(session, params)
        if remote is not None:
            fetch(remote, staging)
        phases["capture_ms"] = elapsed_ms(start)
        if rect is not None and not cropped:
            start = time.perf_counter()
            crop_kind = "magick"
            magick_crop(staging, rect, final)
            staging.unlink()
            phases["crop_ms"] = elapsed_ms(start)
        else:
            phases["crop_ms"] = 0.0
            _ = staging.replace(final)
    finally:
        staging.unlink(missing_ok=True)

    width, height = png_size(final)
    phases["total_ms"] = elapsed_ms(total_start)
    return Result(shot, final, width, height, final.stat().st_size, crop_kind, phases, settled.frames)


def ensure_window(session: Session, size: str) -> None:
    """Resize the primary window to a logical size and wait until the camera sees it."""
    match = WINDOW_SIZE.fullmatch(size)
    if match is None:
        raise Refused(f"window must look like 1440x900, got {size!r}")
    rows = session.brp.query({"data": {"components": [WINDOW]}, "filter": {"with": [PRIMARY_WINDOW]}})
    if len(rows) != 1:
        raise Failure(f"expected one primary window, found {len(rows)}")
    window = cast(WindowValue, rows[0].get("components", {})[WINDOW])
    resolution = window["resolution"]
    scale = resolution["scale_factor_override"]
    if scale is None:
        scale = resolution["scale_factor"]
    width = round(int(match.group(1)) * scale)
    height = round(int(match.group(2)) * scale)
    if session.read_camera().size == (width, height):
        return
    entity = rows[0]["entity"]
    first = POOL.submit(session.brp.mutate, entity, WINDOW, ".resolution.physical_width", width)
    session.brp.mutate(entity, WINDOW, ".resolution.physical_height", height)
    first.result()
    deadline = time.monotonic() + SETTLE_LIMIT
    while session.read_camera().size != (width, height):
        if time.monotonic() > deadline:
            raise Failure(f"the window did not reach {width}x{height} physical pixels; the window manager may refuse resizes")
    session.wait_frames(2)


def keep_visible(session: Session) -> None:
    """macOS draws nothing for a hidden window: raise it above others and wake the display."""
    if not isinstance(session.window_level, WindowLevelPending):
        return
    rows = session.brp.query({"data": {"components": [WINDOW]}, "filter": {"with": [PRIMARY_WINDOW]}})
    prepared_window: WindowLeftAlone | WindowRaised = WindowLeftAlone()
    if len(rows) == 1:
        raw_window = rows[0].get("components", {}).get(WINDOW)
        window = cast(WindowValue, cast(object, raw_window)) if isinstance(raw_window, dict) else None
        level = window.get("window_level") if window is not None else None
        restore_level = level if isinstance(level, str) and level != "AlwaysOnTop" else "Normal"
        entity = rows[0]["entity"]
        prepared_window = WindowRaised(entity, restore_level)
        session.window_level = prepared_window
    try:
        if isinstance(prepared_window, WindowRaised):
            session.brp.mutate(prepared_window.entity, WINDOW, ".window_level", "AlwaysOnTop")
        if session.remote is not None:
            session.keep_awake = keep_awake(session.remote)
        else:
            caffeinate = shutil.which("caffeinate")
            if caffeinate is not None:
                session.keep_awake = subprocess.Popen(
                    [caffeinate, "-u", "-t", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        session.wait_frames(2)
        session.window_level = prepared_window
    except BaseException:
        with EXIT_SIGNAL_LATCH.defer_during_cleanup():
            session.restore_window_and_end_keep_awake()
        raise


# ---------------------------------------------------------------------------
# Output paths, timing log, git


def cache_directory() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "hana-shot"


def prune_shots(directory: Path) -> None:
    now = time.time()
    kept: list[tuple[float, int, Path]] = []
    for path in directory.glob("*.png"):
        try:
            details = path.stat()
        except OSError:
            continue
        if now - details.st_mtime > MAX_AGE_SECONDS:
            path.unlink(missing_ok=True)
        else:
            kept.append((details.st_mtime, details.st_size, path))
    kept.sort()
    total = sum(size for _, size, _ in kept)
    for _, size, path in kept:
        if total < SHOT_DIRECTORY_LIMIT:
            break
        path.unlink(missing_ok=True)
        total -= size


def output_paths(out: str | None, labels: list[str]) -> list[Path]:
    if out is not None and out.endswith(".png"):
        if len(labels) != 1:
            raise Refused("--out names one .png file; give a directory for several shots")
        path = Path(out).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return [path]
    if out is None:
        directory = cache_directory() / "shots"
        directory.mkdir(parents=True, exist_ok=True)
        prune_shots(directory)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        names = [f"{label}-{stamp}" for label in labels]
    else:
        directory = Path(out).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        names = list(labels)
    paths: list[Path] = []
    seen: set[str] = set()
    for name in names:
        unique = name
        suffix = 2
        while unique in seen:
            unique = f"{name}-{suffix}"
            suffix += 1
        seen.add(unique)
        paths.append(directory / f"{unique}.png")
    return paths


def git_sha(directory: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--short=9", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def git_toplevel() -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def timing_record(
    result: Result, port: int, sha: str, frame_ms: float | None, window: tuple[int, int], remote: str | None,
) -> TimingRecord:
    phases = result.phases
    record: TimingRecord = {
        "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        # The machine Hana ran on: its frame time and capture are what the line measures.
        "host": socket.gethostname() if remote is None else remote.rsplit("@", 1)[-1],
        "sha": sha,
        "port": port,
        "label": result.shot.label,
        "view": result.shot.view,
        "target": result.shot.target,
        "mode": result.shot.mode,
        "crop": result.crop_kind,
        "window": f"{window[0]}x{window[1]}",
        "frame_ms": None if frame_ms is None else round(frame_ms, 1),
        "resolve_ms": phases["resolve_ms"],
        "move_ms": phases["move_ms"],
        "settle_ms": phases["settle_ms"],
        "settle_frames": result.settle_frames,
        "capture_ms": phases["capture_ms"],
        "crop_ms": phases["crop_ms"],
        "total_ms": phases["total_ms"],
        "width": result.width,
        "height": result.height,
        "bytes": result.size_bytes,
    }
    if isinstance(result.shot.orientation, (TargetFace, ResolvedTargetFace)):
        record["face"] = result.shot.orientation.axis
    return record


def append_timing(record: SuccessfulInvocation | FailedInvocation) -> None:
    path = cache_directory() / "timings.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        _ = handle.write(json.dumps(record) + "\n")


def session_evidence() -> PresentSession | AbsentSession:
    for name in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
        value = os.environ.get(name)
        if value:
            return {"state": "present", "value": value}
    return {"state": "absent"}


def failure_reason(exc: Refused | Failure | CaptureTimeout) -> FailureReason:
    if isinstance(exc, CaptureTimeout) or "gave no answer" in str(exc):
        return FailureReason.TIMEOUT
    if isinstance(exc, Refused):
        return FailureReason.INVALID_REQUEST
    if isinstance(exc, BrpCallError):
        if "already in progress" in exc.message.lower():
            return FailureReason.ALREADY_IN_PROGRESS
        return FailureReason.BRP_ERROR
    message = str(exc)
    if "no Hana answers" in message or "Hana exited" in message:
        return FailureReason.NO_APP
    if "must name exactly one entity" in message:
        return FailureReason.NO_TARGET
    if "not a PNG" in message:
        return FailureReason.INVALID_PNG
    if "could not copy" in message or "no PNG came back" in message:
        return FailureReason.COPY_FAILED
    if "crop is empty" in message:
        return FailureReason.EMPTY_CROP
    return FailureReason.SHOT_FAILED


def failed_attempt(shot: Shot, path: Path, reason: FailureReason, started_ns: int, error: str) -> FailedAttempt:
    try:
        image_paths = [str(path)] if path.is_file() and path.stat().st_mtime_ns >= started_ns else []
    except OSError:
        image_paths = []
    attempt: FailedAttempt = {
        "status": "failure", "label": shot.label, "view": shot.view,
        "image_paths": image_paths, "failure_reason": reason, "error": error,
    }
    if isinstance(shot.orientation, (TargetFace, ResolvedTargetFace)):
        attempt["face"] = shot.orientation.axis
    return attempt


def append_invocation(invocation: InProgressCaptureInvocation, port: int, remote: str | None, exit_code: int) -> None:
    if exit_code == 0 and invocation.successful_timings:
        success: SuccessfulInvocation = {
            **invocation.successful_timings[-1], "status": "success", "exit_code": 0,
            "invocation_kind": invocation.kind,
            "session": session_evidence(), "attempts": invocation.attempts,
        }
        append_timing(success)
        return
    failure: FailedInvocation = {
        "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "host": socket.gethostname() if remote is None else remote.rsplit("@", 1)[-1],
        "port": port, "status": "failure", "exit_code": exit_code,
        "invocation_kind": invocation.kind,
        "failure_reason": invocation.failures[0].reason if invocation.failures else FailureReason.SHOT_FAILED,
        "error": "\n".join(failure.error for failure in invocation.failures) or f"shot failed with exit code {exit_code}",
        "session": session_evidence(), "attempts": invocation.attempts,
    }
    if isinstance(invocation.orientation, (TargetFace, ResolvedTargetFace)):
        failure["face"] = invocation.orientation.axis
    if isinstance(invocation.shot, Shot):
        failure["view"] = invocation.shot.view
        failure["target"] = invocation.shot.target
        failure["mode"] = invocation.shot.mode
    append_timing(failure)


def write_invocation(invocation: InProgressCaptureInvocation, port: int, remote: str | None, exit_code: int) -> None:
    try:
        append_invocation(invocation, port, remote, exit_code)
    except OSError as exc:
        print(f"hana_shot: could not write timing record: {exc}", file=sys.stderr)


def report(result: Result) -> None:
    phases = result.phases
    print(result.path)
    print(
        f"hana_shot: {result.shot.label} {result.width}x{result.height} {phases['total_ms']:.0f} ms "
        + f"(resolve {phases['resolve_ms']:.0f}, settle {phases['settle_ms']:.0f} over {result.settle_frames} frames, "
        + f"capture {phases['capture_ms']:.0f}, crop {phases['crop_ms']:.0f} {result.crop_kind})",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Views registry


def default_views_file() -> Path:
    top = git_toplevel()
    if top is None:
        raise Refused("not inside a git worktree; pass --views-file")
    return top / "crates" / "hana" / "brp_views.toml"


def views_path(given: str | None) -> Path:
    return Path(given).expanduser().resolve() if given else default_views_file()


def load_views(path: Path) -> dict[str, ViewValue]:
    if not path.exists():
        raise Refused(f"no views file at {path}; pass --views-file")
    try:
        document: dict[str, object] = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f"{path} is not valid TOML: {exc}") from exc
    unknown_tables = [key for key in document if key != "views"]
    if unknown_tables:
        raise Refused(f"{path} may hold only [views.*] tables; found {', '.join(unknown_tables)}")
    raw = document.get("views", {})
    if not isinstance(raw, dict):
        raise Refused(f"{path}: views must be a table")
    views: dict[str, ViewValue] = {}
    for name, value in cast(dict[str, object], raw).items():
        views[name] = validate_view(name, value)
    return views


def view_name_refusal(name: str) -> Refused:
    example = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:64] or "view"
    return Refused(f"view name {name!r} must be lowercase letters, digits and hyphens; use {example!r}, for example")


def validate_view(name: str, value: object) -> ViewValue:
    if VIEW_NAME.fullmatch(name) is None:
        raise view_name_refusal(name)
    if not isinstance(value, dict):
        raise Refused(f"view {name} must be a table")
    table = cast(dict[str, object], value)
    unknown = [key for key in table if key not in VIEW_KEYS]
    if unknown:
        raise Refused(f"view {name} has unknown keys: {', '.join(unknown)}")

    def expect(key: str, kind: type | tuple[type, ...]) -> None:
        if key in table and (not isinstance(table[key], kind) or isinstance(table[key], bool)):
            raise Refused(f"view {name}: {key} has the wrong type")

    for key in ("target", "mode", "face", "crop", "window", "setup", "note"):
        expect(key, str)
    for key in ("yaw", "pitch", "margin", "radius"):
        expect(key, (int, float))
    for key in ("padding", "settle"):
        expect(key, int)
    focus = table.get("focus")
    if focus is not None and not (
        isinstance(focus, list)
        and len(cast(list[object], focus)) == 3
        and all(isinstance(item, (int, float)) for item in cast(list[object], focus))
    ):
        raise Refused(f"view {name}: focus must be three numbers")
    verified = table.get("verified")
    if verified is not None and not (
        isinstance(verified, dict)
        and all(isinstance(cast(dict[str, object], verified).get(key), str) for key in ("sha", "date"))
    ):
        raise Refused(f"view {name}: verified must be {{ sha = \"...\", date = \"...\" }}")
    view = cast(ViewValue, cast(object, table))
    _ = shot_from_view(name, view)
    return view


def number(value: float | int | None, fallback: float) -> float:
    return fallback if value is None else float(value)


def request_orientation(face: str | None, yaw: float | None, pitch: float | None) -> DefaultAngles | ExplicitAngles | TargetFace:
    """Convert optional parser/TOML values into the requested orientation's meaning."""
    if face is not None:
        if face not in FACES:
            raise Refused(f"--face {face!r} is invalid; choose one of {', '.join(FACES)}")
        if yaw is not None or pitch is not None:
            raise Refused("--face conflicts with explicit --yaw or --pitch (view keys yaw/pitch); drop --face or drop both angle options")
        return TargetFace(face)
    if yaw is not None or pitch is not None:
        return ExplicitAngles(number(yaw, 0.0), number(pitch, 0.0))
    return DefaultAngles()


def shot_from_view(name: str, view: ViewValue) -> Shot:
    focus_values = view.get("focus")
    shot = Shot(
        label=name,
        view=name,
        target=view.get("target"),
        mode=view.get("mode", "fit"),
        orientation=request_orientation(view.get("face"), view.get("yaw"), view.get("pitch")),
        margin=number(view.get("margin"), 0.1),
        focus=None if focus_values is None else (float(focus_values[0]), float(focus_values[1]), float(focus_values[2])),
        radius=view.get("radius"),
        crop=view.get("crop", "none"),
        padding=view.get("padding", 0),
        window=view.get("window"),
        setup=view.get("setup"),
        settle=view.get("settle", 0),
    )
    validate_shot(shot)
    return shot


def validate_shot(shot: Shot) -> None:
    where = f"view {shot.view}" if shot.view else "shot"
    if shot.mode not in MODES:
        raise Refused(f"{where}: mode must be one of {', '.join(MODES)}, got {shot.mode!r}")
    if shot.crop not in CROPS:
        raise Refused(f"{where}: crop must be entity or none, got {shot.crop!r}")
    face = isinstance(shot.orientation, (TargetFace, ResolvedTargetFace))
    if face and shot.target is None:
        raise Refused(f"{where}: --face needs a target; add --target name:<Name> or --target tool:<Kind>")
    if face and shot.mode not in ("fit", "pose"):
        raise Refused(f"{where}: --face requires angles; use --mode fit or --mode pose instead of {shot.mode}")
    if shot.mode in ("fit", "hana-frame") and shot.target is None:
        raise Refused(f"{where}: {shot.mode} needs a target")
    if shot.mode == "pose" and ((shot.focus is None and not face) or shot.radius is None):
        missing = (["--focus"] if shot.focus is None and not face else []) + (["--radius"] if shot.radius is None else [])
        raise Refused(f"{where}: pose needs {' and '.join(missing)}; example: --mode pose --focus 0,0,0 --radius 6")
    if shot.crop == "entity" and shot.target is None:
        raise Refused(f"{where}: crop entity needs a target")
    if not 0.0 <= shot.margin <= MAX_MARGIN:
        raise Refused(
            f"{where}: --margin must be 0 to {MAX_MARGIN}; "
            + "fraction of the viewport kept clear on each side (0.1 = 10%)"
        )
    if shot.padding < 0 or shot.settle < 0:
        raise Refused(f"{where}: padding and settle cannot be negative")
    if shot.target is not None:
        _ = parse_selector(shot.target)
    if shot.window is not None and WINDOW_SIZE.fullmatch(shot.window) is None:
        raise Refused(f"{where}: window must look like 1440x900")


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Refused("views hold finite numbers only")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in cast(list[object], value)) + "]"
    if isinstance(value, dict):
        items = cast(dict[str, object], value)
        return "{ " + ", ".join(f"{key} = {toml_value(item)}" for key, item in items.items()) + " }"
    raise Refused(f"cannot write {value!r} to TOML")


def format_view(name: str, view: ViewValue) -> str:
    table = cast(dict[str, object], cast(object, view))
    lines = [f"[views.{name}]"]
    width = max((len(key) for key in table), default=0)
    # Sorted, as taplo's reorder_keys leaves them, so a push's format pass never rewrites the file.
    for key in sorted(table):
        lines.append(f"{key.ljust(width)} = {toml_value(table[key])}")
    return "\n".join(lines) + "\n"


def write_views(path: Path, views: dict[str, ViewValue]) -> None:
    text = VIEWS_HEADER + "".join("\n" + format_view(name, view) for name, view in views.items())
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    _ = temporary.write_text(text)
    _ = temporary.replace(path)


# ---------------------------------------------------------------------------
# Launch and shutdown


def launch_state_path(port: int) -> Path:
    return cache_directory() / "launches" / f"{port}.json"


def _write_launch_state(state_path: Path, state: LaunchState) -> None:
    temporary = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=state_path.parent,
        prefix=f".{state_path.name}.", suffix=".tmp", delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            json.dump(state, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, state_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _read_launch_state(state_path: Path) -> LaunchRecord:
    """Distinguish absent, unreadable and whole shutdown metadata."""
    try:
        value = cast(object, json.loads(state_path.read_text()))
    except FileNotFoundError:
        return AbsentLaunchRecord()
    except json.JSONDecodeError as error:
        return UnreadableLaunchRecord(f"invalid JSON: {error}")
    except (OSError, ValueError) as error:
        return UnreadableLaunchRecord(str(error))
    if not isinstance(value, dict):
        return UnreadableLaunchRecord("expected a JSON object")
    fields = cast(dict[str, object], value)
    missing = [key for key in ("pid", "binary", "log", "scratch") if key not in fields]
    if missing:
        return UnreadableLaunchRecord(f"missing {', '.join(missing)}")
    pid = fields["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return UnreadableLaunchRecord("pid must be a positive integer")
    for key in ("binary", "log", "scratch"):
        if not isinstance(fields[key], str) or not fields[key]:
            return UnreadableLaunchRecord(f"{key} must be a nonempty string")
    return WholeLaunchRecord(cast(LaunchState, cast(object, fields)))


def port_answers(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


def panic_lines(log: Path) -> list[str]:
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return []
    found: list[str] = []
    for index, line in enumerate(lines):
        if "panic" in line.lower():
            found.extend(lines[index:index + 3])
    return found[:40]


def uncommitted_build_paths(root: Path, executable: Path) -> list[str]:
    """Dirty build inputs that predate the binary, or every dirty input before a build."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "crates/", "Cargo.toml", "Cargo.lock"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    dirty: dict[str, bool] = {}
    records = iter(result.stdout.split(b"\0"))
    for record in records:
        if len(record) < 4:
            continue
        status = record[:2]
        path = os.fsdecode(record[3:])
        if path.startswith("crates/") or path in ("Cargo.toml", "Cargo.lock"):
            dirty[path] = b"D" in status
        if b"R" in status or b"C" in status:
            # In -z format the destination comes first, followed by the original path.
            original = os.fsdecode(next(records, b""))
            if b"R" in status and (original.startswith("crates/") or original in ("Cargo.toml", "Cargo.lock")):
                dirty[original] = True
    try:
        if not executable.is_file():
            return sorted(dirty)
        built_ns = executable.stat().st_mtime_ns
    except OSError:
        return []
    included: list[str] = []
    for path, deleted in dirty.items():
        if deleted:
            included.append(path)
            continue
        try:
            if (root / path).stat().st_mtime_ns <= built_ns:
                included.append(path)
        except OSError:
            continue
    return sorted(included)


def build_paths_label(paths: list[str]) -> str:
    label = ", ".join(paths[:10])
    return label + (f", and {len(paths) - 10} more" if len(paths) > 10 else "")


def launch(port: int, worktree: str | None, binary: str | None) -> tuple[LaunchState, subprocess.Popen[bytes]]:
    """Start a Hana build on a test port with scratch config and wait until the scene shows."""
    check_port(port)
    if port_answers(port):
        raise Refused(f"something already listens on port {port}; shut it down or pick another port")
    root = Path(worktree).expanduser().resolve() if worktree else git_toplevel()
    if root is None or not (root / "crates" / "hana").is_dir():
        raise Refused("run inside a Hana worktree or pass --worktree")
    executable = Path(binary).expanduser().resolve() if binary else root / "target" / "debug" / "hana"
    uncommitted_paths = uncommitted_build_paths(root, executable)
    if not executable.is_file():
        hint = f"; uncommitted edits in crates/ or the root Cargo files will be included by a build: {build_paths_label(uncommitted_paths)}" if uncommitted_paths else ""
        raise Refused(f"no Hana binary at {executable}; build it first (cargo build -p hana){hint}")
    if uncommitted_paths:
        print(
            f"hana_shot: warning: {executable} was built after uncommitted edits in crates/ or the root Cargo files, "
            + f"so this Hana runs that code: {build_paths_label(uncommitted_paths)}",
            file=sys.stderr,
        )
    scratch = Path(tempfile.mkdtemp(prefix=f"hana-shot-{port}-"))
    config = scratch / "config"
    config.mkdir()
    log = scratch / "hana.log"
    command = [
        "env", f"XDG_CONFIG_HOME={config}", f"BRP_EXTRAS_PORT={port}",
        f"BEVY_ASSET_ROOT={root / 'crates' / 'hana'}", str(executable),
    ]
    direnv = shutil.which("direnv")
    if direnv is not None and (root / ".envrc").exists():
        command = [direnv, "exec", str(root), *command]
    if sys.platform == "darwin":
        print("hana_shot: macOS ignores XDG_CONFIG_HOME; this Hana reads your real config", file=sys.stderr)
    with log.open("wb") as handle:
        process = subprocess.Popen(
            command, cwd=root, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    state: LaunchState = {
        "pid": process.pid,
        "port": port,
        "scratch": str(scratch),
        "log": str(log),
        "binary": str(executable),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if uncommitted_paths:
        state["uncommitted_paths"] = uncommitted_paths
    state_path = launch_state_path(port)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _write_launch_state(state_path, state)
    start = time.monotonic()
    try:
        recorder_path = Path(__file__).resolve().parents[1] / "buildlog" / "record.py"
        recorder = sys.modules.get("record")
        if recorder is None or getattr(recorder, "__file__", "") != str(recorder_path):
            module_name = "_hana_shot_buildlog_record"
            recorder = sys.modules.get(module_name)
            if recorder is None:
                spec = importlib.util.spec_from_file_location(
                    module_name, recorder_path, submodule_search_locations=[str(recorder_path.parent)],
                )
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot load launch recorder at {recorder_path}")
                recorder = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = recorder
                try:
                    spec.loader.exec_module(recorder)
                except BaseException:
                    del sys.modules[module_name]
                    raise
        record_launch = cast(Callable[[Path, Path], bool], recorder.launch)
        _ = record_launch(state_path, root)
        wait_ready(Brp(port), process, log)
    except BaseException:
        shutdown(port, process)
        raise
    print(f"hana_shot: Hana pid {process.pid} ready on port {port} in {time.monotonic() - start:.1f} s (log {log})", file=sys.stderr)
    return state, process


def wait_ready(brp: Brp, process: subprocess.Popen[bytes], log: Path) -> None:
    deadline = time.monotonic() + READY_LIMIT
    while True:
        if process.poll() is not None:
            lines = panic_lines(log)
            detail = ("; " + " | ".join(lines)) if lines else ""
            raise Failure(f"Hana exited with {process.returncode} during startup{detail}")
        if READY_LOG_LINE in log.read_text(errors="replace"):
            break
        if time.monotonic() > deadline:
            raise Failure(f"Hana did not log '{READY_LOG_LINE}' within {READY_LIMIT:.0f} s")
        time.sleep(0.1)
    warm_deadline = time.monotonic() + WARM_LIMIT
    while time.monotonic() < warm_deadline:
        try:
            frame_ms = brp.diagnostics()["frame_time_ms"]["current"]
        except Failure:
            frame_ms = None
        if frame_ms is not None and frame_ms < WARM_FRAME_MS:
            return
        time.sleep(0.1)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def same_binary(pid: int, binary: str) -> bool:
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists():
        return sys.platform == "darwin"
    return binary.encode() in cmdline.read_bytes()


def shutdown(port: int, process: subprocess.Popen[bytes] | None = None) -> None:
    """Stop a launched Hana, show any panic lines from its log, and remove its scratch."""
    check_port(port)
    state_path = launch_state_path(port)
    record = _read_launch_state(state_path)
    launch_record = record
    if not isinstance(launch_record, WholeLaunchRecord):
        temporary_records: list[tuple[int, Path]] = []
        for temporary_path in state_path.parent.glob(f".{state_path.name}.*.tmp"):
            try:
                temporary_records.append((temporary_path.stat().st_mtime_ns, temporary_path))
            except FileNotFoundError:
                continue
        for entry in sorted(temporary_records, key=lambda entry: entry[0], reverse=True):
            candidate = _read_launch_state(entry[1])
            if isinstance(candidate, WholeLaunchRecord):
                launch_record = candidate
                break
    shutdown_answered = False
    try:
        _ = Brp(port).call("brp_extras/shutdown", timeout=5)
        shutdown_answered = True
    except Failure:
        pass
    if isinstance(launch_record, WholeLaunchRecord):
        state = launch_record.state
        pid = state["pid"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process is not None:
                if process.poll() is not None:
                    break
            elif not process_alive(pid):
                break
            time.sleep(0.1)
        else:
            if same_binary(pid, state["binary"]):
                for signal_number in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(pid, signal_number)
                    except ProcessLookupError:
                        break
                    time.sleep(2)
        lines = panic_lines(Path(state["log"]))
        if lines:
            print(f"hana_shot: Hana on port {port} logged a panic:", file=sys.stderr)
            for line in lines:
                print(f"  {line}", file=sys.stderr)
        shutil.rmtree(state["scratch"], ignore_errors=True)
    else:
        deadline = time.monotonic() + 20
        while port_answers(port):
            if time.monotonic() >= deadline:
                raise Failure(
                    f"Hana on port {port} still answers after 20 s without a whole launch record"
                    + next_step(f"run shutdown again on port {port} after the launch finishes starting"),
                )
            time.sleep(0.1)
    state_path.unlink(missing_ok=True)
    for temporary_path in state_path.parent.glob(f".{state_path.name}.*.tmp"):
        temporary_path.unlink(missing_ok=True)
    if isinstance(record, UnreadableLaunchRecord):
        print(
            f"hana_shot: launch record {state_path} is unreadable ({record.reason}); "
            + f"asked Hana on port {port} to stop and removed the record; "
            + f"check scratch parent {tempfile.gettempdir()} for hana-shot-{port}-* folders",
            file=sys.stderr,
        )
    if shutdown_answered or isinstance(launch_record, WholeLaunchRecord):
        print(f"hana_shot: Hana on port {port} shut down", file=sys.stderr)
    else:
        print(
            f"hana_shot: no Hana answered on port {port}; if a launch on this port is still starting, "
            + "run shutdown again when the next launch reports the port in use",
            file=sys.stderr,
        )


@contextmanager
def instance(port: int, launch_first: bool, shutdown_after: bool, worktree: str | None, binary: str | None) -> Generator[Brp]:
    check_port(port)
    process: subprocess.Popen[bytes] | None = None
    if launch_first:
        _, process = launch(port, worktree, binary)
    try:
        yield Brp(port)
    finally:
        if shutdown_after:
            shutdown(port, process)


# ---------------------------------------------------------------------------
# Commands


class Arguments(argparse.Namespace):
    command: str = ""
    action: str = ""
    port: int = 0
    view: list[str] | None = None
    target: str | None = None
    mode: str | None = None
    face: str | None = None
    yaw: float | None = None
    pitch: float | None = None
    margin: float | None = None
    focus: str | None = None
    radius: float | None = None
    crop: str | None = None
    padding: int | None = None
    window: str | None = None
    setup: str | None = None
    settle: int | None = None
    note: str | None = None
    name: str | None = None
    out: str | None = None
    views_file: str | None = None
    launch: bool = False
    shutdown: bool = False
    remote: str | None = None
    worktree: str | None = None
    binary: str | None = None
    from_current: bool = False
    replace: bool = False
    since: str | None = None


def parse_focus(text: str) -> Vec3:
    parts = text.split(",")
    try:
        values = [float(part) for part in parts]
    except ValueError as exc:
        raise Refused(f"--focus takes x,y,z, got {text!r}") from exc
    if len(values) != 3:
        raise Refused(f"--focus takes x,y,z, got {text!r}")
    return (values[0], values[1], values[2])


def shot_from_arguments(args: Arguments) -> Shot:
    mode = args.mode or ("fit" if args.target else "home")
    target = args.target
    label = f"{mode}-{LABEL_UNSAFE.sub('_', target.rsplit(':', 1)[-1].rsplit('::', 1)[-1])}" if target else mode
    shot = Shot(
        label=label,
        view=None,
        target=target,
        mode=mode,
        orientation=request_orientation(args.face, args.yaw, args.pitch),
        margin=args.margin if args.margin is not None else 0.1,
        focus=parse_focus(args.focus) if args.focus else None,
        radius=args.radius,
        crop=args.crop or "none",
        padding=args.padding or 0,
        window=args.window,
        setup=args.setup,
        settle=args.settle or 0,
    )
    validate_shot(shot)
    return shot


def with_overrides(shot: Shot, args: Arguments) -> Shot:
    """Apply run-wide flags that also make sense on a stored view."""
    if args.face is not None:
        if isinstance(shot.orientation, ExplicitAngles):
            raise Refused("--face conflicts with explicit view yaw/pitch; drop --face or remove the view's yaw/pitch keys")
        shot = replace(shot, orientation=request_orientation(args.face, args.yaw, args.pitch))
    elif isinstance(shot.orientation, (TargetFace, ResolvedTargetFace)) and (args.yaw is not None or args.pitch is not None):
        _ = request_orientation(shot.orientation.axis, args.yaw, args.pitch)
    if isinstance(shot.orientation, (TargetFace, ResolvedTargetFace)) and shot.mode == "pose" and args.focus is not None:
        shot = replace(shot, focus=parse_focus(args.focus))
    if args.window is not None:
        shot = replace(shot, window=args.window)
    if args.settle is not None:
        shot = replace(shot, settle=args.settle)
    validate_shot(shot)
    return shot


def run_shots(
    brp: Brp, shots: list[Shot], out: str | None, remote: str | None, on_result: Callable[[Result], None],
    invocation: InProgressCaptureInvocation,
) -> list[Result]:
    if shots:
        invocation.shot = shots[0]
    with closing(Session(brp, remote)) as session:
        _ = session.start()
        sha = git_sha(Path.cwd())
        results: list[Result] = []
        for shot, path in zip(shots, output_paths(out, [shot.label for shot in shots]), strict=True):
            invocation.shot = shot
            invocation.orientation = shot.orientation
            started_ns = time.time_ns()
            try:
                result = take(session, shot, path)
            except (Refused, Failure, CaptureTimeout) as exc:
                reason = failure_reason(exc)
                invocation.attempts.append(failed_attempt(shot, path, reason, started_ns, str(exc)))
                invocation.failures.append(CaptureFailure(reason, str(exc)))
                raise
            window = session.last_state.size if session.last_state else (0, 0)
            timing = timing_record(result, brp.port, sha, session.frame_ms, window, remote)
            invocation.successful_timings.append(timing)
            invocation.attempts.append({
                **timing, "status": "success", "image_paths": [str(result.path)],
            })
            on_result(result)
            results.append(result)
        return results


def command_shot(args: Arguments, invocation: InProgressCaptureInvocation) -> int:
    if args.view and args.target:
        raise Refused("give --view or --target, not both")
    if args.view:
        path = views_path(args.views_file)
        views = load_views(path)
        names = list(views) if args.view == ["all"] else args.view
        missing = [name for name in names if name not in views]
        if missing:
            raise Refused(f"no view named {', '.join(missing)} in {path}; known: {', '.join(views)}")
        shots = [with_overrides(shot_from_view(name, views[name]), args) for name in names]
        if not shots:
            raise Refused(f"no views to shoot in {path}")
    else:
        shots = [shot_from_arguments(args)]
    invocation.orientation = shots[0].orientation
    invocation.shot = shots[0]
    with instance(args.port, args.launch, args.shutdown, args.worktree, args.binary) as brp:
        _ = run_shots(brp, shots, args.out, args.remote, report, invocation)
    return 0


def command_pose(args: Arguments) -> int:
    with instance(args.port, False, False, None, None) as brp:
        session = Session(brp)
        state = session.start()
        if args.action == "set":
            if args.focus is None and args.yaw is None and args.pitch is None and args.radius is None:
                raise Refused("pose set needs --focus x,y,z, --yaw, --pitch or --radius; the rest keep their values")
            current = state.pose
            pose = Pose(
                parse_focus(args.focus) if args.focus else current.focus,
                current.yaw if args.yaw is None else args.yaw,
                current.pitch if args.pitch is None else args.pitch,
                current.radius if args.radius is None else args.radius,
            )
            session.set_pose(state, pose)
            state = session.settle(state, 0).state
        pose = state.pose
        print(json.dumps({
            "focus": [round(value, 6) for value in pose.focus],
            "yaw": round(pose.yaw, 6),
            "pitch": round(pose.pitch, 6),
            "radius": round(pose.radius, 6),
            "window": f"{state.size[0]}x{state.size[1]}",
        }))
    return 0


def command_targets(args: Arguments) -> int:
    with instance(args.port, False, False, None, None) as brp:
        session = Session(brp)
        _ = session.start()
        print(f"camera  entity:{session.camera}")
        for tool in tool_rows(session):
            print(f"tool:{tool.kind}  {tool.definition}  id {tool.tool_id}  entity {tool.entity}")
        registered = cast(list[str], brp.call("world.list_components"))
        for path in sorted(item for item in registered if item.endswith("Shield") and "<" not in item):
            if len(brp.query({"data": {}, "filter": {"with": [path]}})) == 1:
                print(f"marker:{path}")
        names = sorted({
            cast(str, row["components"].get(NAME))
            for row in brp.query({"data": {"components": [NAME]}})
        })
        for name in names:
            if "::" not in name:
                print(f"name:{name}")
    return 0


def usable_stats_shot(item: dict[str, object], since: str | None, phases: tuple[str, ...]) -> bool:
    stamp = item.get("time")
    return (
        isinstance(stamp, str)
        and (since is None or stamp >= since)
        and isinstance(item.get("mode"), str)
        and isinstance(item.get("crop"), str)
        and all(isinstance(item.get(key), (int, float)) for key in phases)
    )


def command_stats(args: Arguments) -> int:
    path = cache_directory() / "timings.jsonl"
    if not path.exists():
        print(f"no timings yet at {path}")
        return 0
    records: list[LegacySuccess | SuccessfulAttempt] = []
    failures: dict[str, int] = {}
    phases = ("resolve_ms", "move_ms", "settle_ms", "capture_ms", "crop_ms", "total_ms")
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            raw = cast(object, json.loads(line))
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, object], raw)
        if item.get("status") == "failure":
            cause = item.get("failure_reason")
            stamp = item.get("time")
            if isinstance(cause, str) and isinstance(stamp, str) and (not args.since or stamp >= args.since):
                failures[cause] = failures.get(cause, 0) + 1
        if "status" not in item:
            if usable_stats_shot(item, args.since, phases):
                records.append(cast(LegacySuccess, cast(object, item)))
        else:
            attempts = item.get("attempts")
            if not isinstance(attempts, list):
                continue
            for raw_attempt in cast(list[object], attempts):
                if not isinstance(raw_attempt, dict):
                    continue
                attempt = cast(dict[str, object], raw_attempt)
                if attempt.get("status") == "success" and usable_stats_shot(attempt, args.since, phases):
                    records.append(cast(SuccessfulAttempt, cast(object, attempt)))
    if not records:
        print("no shots in that span")
    else:
        print(f"{len(records)} shots since {records[0]['time'][:10]} ({path})")
        groups: dict[str, list[LegacySuccess | SuccessfulAttempt]] = {"all": records}
        for record in records:
            groups.setdefault(f"{record['mode']}/{record['crop']}", []).append(record)
        print(f"{'group':<18}{'shots':>6}" + "".join(f"{phase[:-3]:>10}" for phase in phases) + "   (median ms)")
        for group, items in groups.items():
            medians = "".join(
                f"{statistics.median(item[phase] for item in items):>10.1f}"
                for phase in phases
            )
            print(f"{group:<18}{len(items):>6}{medians}")
    for cause, count in sorted(failures.items()):
        print(f"failure {cause}: {count}")
    return 0


def command_views(args: Arguments, invocation: InProgressCaptureInvocation | None = None) -> int:
    path = views_path(args.views_file)
    if args.action == "list":
        for name, view in load_views(path).items():
            verified = view.get("verified")
            stamp = f"{verified['sha']} {verified['date']}" if verified else "never verified"
            print(f"{name:<24}{view.get('mode', 'fit'):<11}{view.get('target', '-'):<52}{stamp}  {view.get('note', '')}")
        return 0
    if args.action == "show":
        views = load_views(path)
        if args.name not in views:
            raise Refused(f"no view named {args.name}")
        print(format_view(args.name, views[args.name]), end="")
        return 0
    if args.action == "add":
        return views_add(args, path)
    if invocation is None:
        raise RuntimeError("views check needs an invocation")
    return views_check(args, path, invocation)


def views_add(args: Arguments, path: Path) -> int:
    if args.from_current and args.face is not None:
        raise Refused("--face conflicts with --from-current camera angles; drop --from-current to store --face or drop --face")
    name = args.name or ""
    if VIEW_NAME.fullmatch(name) is None:
        raise view_name_refusal(name)
    views = load_views(path) if path.exists() else {}
    if name in views and not args.replace:
        raise Refused(f"view {name} exists; pass --replace to overwrite it")
    shot = shot_from_arguments(args) if not args.from_current else None
    view: ViewValue = {}
    if args.target:
        view["target"] = args.target
    mode = args.mode or ("fit" if args.target else "home")
    view["mode"] = mode
    if args.from_current:
        with instance(args.port, False, False, None, None) as brp:
            view.update(from_current(Session(brp), args, mode))
    else:
        assert shot is not None
        if mode in ("fit", "pose"):
            if isinstance(shot.orientation, TargetFace):
                view["face"] = shot.orientation.axis
            else:
                view["yaw"] = shot.yaw
                view["pitch"] = shot.pitch
        if mode == "fit":
            view["margin"] = shot.margin
        if mode == "pose":
            assert shot.radius is not None
            if shot.focus is not None:
                view["focus"] = list(shot.focus)
            view["radius"] = shot.radius
    view["crop"] = args.crop or "none"
    if args.padding:
        view["padding"] = args.padding
    if args.window:
        view["window"] = args.window
    if args.setup:
        view["setup"] = args.setup
    if args.settle:
        view["settle"] = args.settle
    if args.note:
        view["note"] = args.note
    _ = shot_from_view(name, view)
    views[name] = view
    write_views(path, views)
    print(format_view(name, view), end="")
    return 0


def from_current(session: Session, args: Arguments, mode: str) -> ViewValue:
    """Read the live camera into a view: an absolute pose, or the fit margin that matches it."""
    state = session.start()
    pose = state.pose
    view: ViewValue = {"yaw": round(pose.yaw, 6), "pitch": round(pose.pitch, 6)}
    if mode == "pose":
        view["focus"] = [round(value, 6) for value in pose.focus]
        view["radius"] = round(pose.radius, 6)
        return view
    if mode != "fit" or not args.target:
        raise Refused("--from-current takes --mode pose, or a fit view with --target")
    target = resolve_one(session, Shot(label="add", view=None, target=args.target, mode="fit"))
    assert target is not None
    shot = Shot(label="add", view=None, target=args.target, mode="fit", orientation=ExplicitAngles(pose.yaw, pose.pitch))

    def radius_at(margin: float, before: CameraState) -> tuple[float, CameraState]:
        session.fit(target.entity, shot, margin)
        settled = session.settle(before, 0)
        return settled.state.pose.radius, settled.state

    low, high = 0.0, MAX_MARGIN
    low_radius, current = radius_at(low, state)
    high_radius, current = radius_at(high, current)
    if pose.radius <= low_radius:
        margin = low
    elif pose.radius >= high_radius:
        margin = high
    else:
        for _ in range(12):
            middle = (low + high) / 2
            radius, current = radius_at(middle, current)
            if radius < pose.radius:
                low = middle
            else:
                high = middle
        margin = (low + high) / 2
    _ = radius_at(margin, current)
    view["margin"] = round(margin, 4)
    if margin in (0.0, MAX_MARGIN):
        print(f"hana_shot: the current distance is outside what margins 0 to {MAX_MARGIN} reach; stored {margin}", file=sys.stderr)
    return view


def views_check(args: Arguments, path: Path, invocation: InProgressCaptureInvocation) -> int:
    """Shoot every view and fail any that resolves wrong, is rejected, is black, or crops empty."""
    views = load_views(path)
    names = args.view if args.view and args.view != ["all"] else list(views)
    if not names:
        raise Refused(f"no views to check in {path}")
    missing = [name for name in names if name not in views]
    if missing:
        raise Refused(f"no view named {', '.join(missing)} in {path}")
    invocation.shot = with_overrides(shot_from_view(names[0], views[names[0]]), args)
    invocation.orientation = invocation.shot.orientation
    out = args.out or tempfile.mkdtemp(prefix="hana-shot-check-")
    failures: list[str] = []
    passed: list[str] = []
    with (
        instance(args.port, args.launch, args.shutdown, args.worktree, args.binary) as brp,
        closing(Session(brp, args.remote)) as session,
    ):
        _ = session.start()
        sha = git_sha(path.parent)
        today = datetime.now().strftime("%Y-%m-%d")
        for name, final in zip(names, output_paths(out, names), strict=True):
            shot = with_overrides(shot_from_view(name, views[name]), args)
            invocation.shot = shot
            invocation.orientation = shot.orientation
            started_ns = time.time_ns()
            try:
                result = take(session, shot, final)
            except (Refused, Failure, CaptureTimeout) as exc:
                reason = failure_reason(exc)
                invocation.attempts.append(failed_attempt(shot, final, reason, started_ns, str(exc)))
                invocation.failures.append(CaptureFailure(reason, f"{name}: {exc}"))
                failures.append(f"FAIL {name}: {exc}")
                print(failures[-1], file=sys.stderr)
                continue
            window = session.last_state.size if session.last_state else (0, 0)
            try:
                peak = brightest(result.path)
            except Failure as exc:
                reason = failure_reason(exc)
                invocation.attempts.append(failed_attempt(shot, result.path, reason, started_ns, str(exc)))
                invocation.failures.append(CaptureFailure(reason, f"{name}: {exc}"))
                failures.append(f"FAIL {name}: {exc}")
                print(failures[-1], file=sys.stderr)
                continue
            if peak < BLACK_MAXIMUM:
                error = f"the shot is black (brightest {peak:.3f}) at {result.path}"
                invocation.attempts.append(failed_attempt(shot, result.path, FailureReason.BLACK_CAPTURE, started_ns, error))
                invocation.failures.append(CaptureFailure(FailureReason.BLACK_CAPTURE, f"{name}: {error}"))
                failures.append(f"FAIL {name}: {error}")
                print(failures[-1], file=sys.stderr)
                continue
            if result.width < 2 or result.height < 2:
                error = f"the crop is empty ({result.width}x{result.height})"
                invocation.attempts.append(failed_attempt(shot, result.path, FailureReason.EMPTY_CROP, started_ns, error))
                invocation.failures.append(CaptureFailure(FailureReason.EMPTY_CROP, f"{name}: {error}"))
                failures.append(f"FAIL {name}: {error}")
                print(failures[-1], file=sys.stderr)
                continue
            timing = timing_record(result, brp.port, sha, session.frame_ms, window, args.remote)
            invocation.successful_timings.append(timing)
            invocation.attempts.append({
                **timing, "status": "success", "image_paths": [str(result.path)],
            })
            views[name]["verified"] = {"sha": sha, "date": today}
            passed.append(name)
            print(f"PASS {name} {result.width}x{result.height} {result.phases['total_ms']:.0f} ms {result.path}")
    if passed:
        write_views(path, views)
    if args.out is None:
        shutil.rmtree(out, ignore_errors=True)
    print(f"hana_shot: {len(passed)} passed, {len(failures)} failed; stamped {len(passed)} in {path}", file=sys.stderr)
    return 1 if failures else 0


def command_launch(args: Arguments) -> int:
    state, _ = launch(args.port, args.worktree, args.binary)
    print(json.dumps(state))
    return 0


def command_shutdown(args: Arguments) -> int:
    shutdown(args.port)
    return 0


def add_shot_flags(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument("--target", help="selector: name:<Name>, marker:<type path>, tool:<Kind>[#n], entity:<id>")
    _ = parser.add_argument("--mode", choices=MODES)
    _ = parser.add_argument("--face", choices=FACES, help="target-local side to face in fit or pose; replaces --yaw/--pitch")
    _ = parser.add_argument("--yaw", type=float)
    _ = parser.add_argument("--pitch", type=float)
    _ = parser.add_argument("--margin", type=float, help="0 to 0.45; fraction of the viewport reserved on each side of a fit (0.1 = 10%%)")
    _ = parser.add_argument("--focus", help="x,y,z (pose mode)")
    _ = parser.add_argument("--radius", type=float)
    _ = parser.add_argument("--crop", choices=CROPS)
    _ = parser.add_argument("--padding", type=int)
    _ = parser.add_argument("--setup", help="Hana command to run when the target is missing")
    _ = parser.add_argument("--note")


def add_run_flags(parser: argparse.ArgumentParser, port_required: bool = True) -> None:
    _ = parser.add_argument("--port", type=int, required=port_required, default=0)
    _ = parser.add_argument(
        "--window",
        help="logical window size, such as 1280x720 (the PNG is 2560x1440 at Hana's 2x scale)",
    )
    _ = parser.add_argument("--settle", type=int, help="extra frames to wait after the camera stops")
    _ = parser.add_argument("--out", help="directory, or one .png path")
    _ = parser.add_argument("--views-file")
    _ = parser.add_argument("--launch", action="store_true", help="start a Hana on --port first")
    _ = parser.add_argument("--shutdown", action="store_true", help="stop that Hana afterwards")
    _ = parser.add_argument("--remote", help="ssh host of a Hana whose --port is tunnelled here, such as natemccoy@mac")
    _ = parser.add_argument("--worktree")
    _ = parser.add_argument("--binary")


class ArgumentExit(SystemExit):
    """A parser exit retaining its diagnostic for the invocation record."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(status)
        self.error: str = error


class RequestArgumentParser(argparse.ArgumentParser):
    @override
    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            _ = sys.stderr.write(message)
        raise ArgumentExit(status, message.strip() if message else "")


def arguments(argv: list[str]) -> Arguments:
    parser = RequestArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    shot = commands.add_parser("shot", help="frame and capture one or more views")
    add_run_flags(shot)
    _ = shot.add_argument("--view", action="append", help="stored view name (repeat, or 'all')")
    add_shot_flags(shot)

    pose = commands.add_parser("pose", help="read or set the orbit camera pose")
    _ = pose.add_argument("action", choices=("get", "set"))
    _ = pose.add_argument("--port", type=int, required=True)
    _ = pose.add_argument("--focus")
    _ = pose.add_argument("--yaw", type=float)
    _ = pose.add_argument("--pitch", type=float)
    _ = pose.add_argument("--radius", type=float)

    targets = commands.add_parser("targets", help="list selectors that resolve right now")
    _ = targets.add_argument("--port", type=int, required=True)

    views = commands.add_parser("views", help="list, show, add or check stored views")
    _ = views.add_argument("action", choices=("list", "show", "add", "check"))
    _ = views.add_argument("name", nargs="?")
    add_run_flags(views, port_required=False)
    _ = views.add_argument("--view", action="append", help="check only these views")
    add_shot_flags(views)
    _ = views.add_argument("--from-current", action="store_true")
    _ = views.add_argument("--replace", action="store_true")

    stats = commands.add_parser("stats", help="median time per phase from the timing log")
    _ = stats.add_argument("--since", help="ISO date, such as 2026-10-01")

    launch_parser = commands.add_parser("launch", help="start a Hana on a test port")
    _ = launch_parser.add_argument("--port", type=int, required=True)
    _ = launch_parser.add_argument("--worktree")
    _ = launch_parser.add_argument("--binary")

    shutdown_parser = commands.add_parser("shutdown", help="stop a launched Hana and remove its scratch")
    _ = shutdown_parser.add_argument("--port", type=int, required=True)

    # argparse otherwise mistakes the -x/-y/-z values for option names.
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        if argv[index] == "--face" and index + 1 < len(argv) and argv[index + 1] in FACES:
            normalized.append(f"--face={argv[index + 1]}")
            index += 2
        else:
            normalized.append(argv[index])
            index += 1
    return parser.parse_args(normalized, namespace=Arguments())


def exit_for_signal(number: int, _frame: FrameType | None) -> None:
    EXIT_SIGNAL_LATCH.interrupt(number)


def main(argv: list[str]) -> int:
    try:
        args = arguments(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        if code != 0 and (argv[:1] == ["shot"] or argv[:2] == ["views", "check"]):
            port = 0
            if "--port" in argv:
                index = argv.index("--port") + 1
                if index < len(argv):
                    try:
                        port = int(argv[index])
                    except ValueError:
                        pass
            kind: Literal["shot", "views_check"] = "shot" if argv[:1] == ["shot"] else "views_check"
            error = exc.error if isinstance(exc, ArgumentExit) else "arguments refused"
            write_invocation(InProgressCaptureInvocation(
                kind=kind, failures=[CaptureFailure(FailureReason.INVALID_REQUEST, error)],
            ), port, None, code)
        return code
    handlers: dict[str, Callable[[Arguments], int]] = {
        "pose": command_pose,
        "targets": command_targets,
        "stats": command_stats,
        "launch": command_launch,
        "shutdown": command_shutdown,
    }
    invocation = (InProgressCaptureInvocation(kind="shot" if args.command == "shot" else "views_check")
                  if args.command == "shot" or args.command == "views" and args.action == "check" else None)
    if invocation is not None and args.face is not None:
        invocation.orientation = TargetFace(args.face)
    EXIT_SIGNAL_LATCH.reset()
    _ = signal.signal(signal.SIGINT, exit_for_signal)
    _ = signal.signal(signal.SIGTERM, exit_for_signal)
    _ = signal.signal(signal.SIGHUP, exit_for_signal)
    exit_code = 1
    try:
        if args.command == "views" and args.action in ("check", "add") and args.from_current or (
            args.command == "views" and args.action == "check"
        ):
            check_port(args.port)
        if args.remote is not None and (args.launch or args.shutdown):
            raise Refused(f"--launch and --shutdown run a Hana on this machine only; start and stop the one on {args.remote} there")
        if args.command == "shot" and invocation is not None:
            exit_code = command_shot(args, invocation)
        elif args.command == "views":
            exit_code = command_views(args, invocation)
        else:
            exit_code = handlers[args.command](args)
    except Refused as exc:
        print(f"hana_shot: refused: {exc}", file=sys.stderr)
        exit_code = 2
        if invocation is not None and not invocation.failures:
            invocation.failures.append(CaptureFailure(failure_reason(exc), str(exc)))
    except CaptureTimeout as exc:
        print(f"hana_shot: {exc}", file=sys.stderr)
        exit_code = 3
        if invocation is not None and not invocation.failures:
            invocation.failures.append(CaptureFailure(failure_reason(exc), str(exc)))
    except Failure as exc:
        print(f"hana_shot: {exc}", file=sys.stderr)
        exit_code = 1
        if invocation is not None and not invocation.failures:
            invocation.failures.append(CaptureFailure(failure_reason(exc), str(exc)))
    except ExitSignal as exc:
        exit_code = 128 + exc.number
        error = f"terminated by {signal.Signals(exc.number).name}"
        print(f"hana_shot: {error}", file=sys.stderr)
        if invocation is not None:
            invocation.failures.append(CaptureFailure(FailureReason.SHOT_FAILED, error))
    except Exception as exc:
        if invocation is not None:
            invocation.failures.append(CaptureFailure(FailureReason.SHOT_FAILED, f"{type(exc).__name__}: {exc}"))
        raise
    finally:
        if invocation is not None:
            write_invocation(invocation, args.port, args.remote, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
