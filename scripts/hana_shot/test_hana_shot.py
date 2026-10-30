"""hana_shot run as a subprocess against a stdlib fake BRP server: one still camera, a few entities and tools."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
import re
import shlex
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import NotRequired, TypedDict, cast, override
import unittest
from unittest import mock
import zlib

from scripts.hana_shot import hana_shot
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


SCRIPT = Path(__file__).with_name("hana_shot.py")
CAMERA_ENTITY = 5
WINDOW_ENTITY = 3
NAME = "bevy_ecs::name::Name"
AABB = "bevy_camera::primitives::Aabb"
GLOBAL_TRANSFORM = "bevy_transform::components::global_transform::GlobalTransform"
ORBIT_CAM = "hana_lagrange::orbit_cam::OrbitCam"
ANIMATE_TO_FIT = "hana_lagrange::fit::triggers::animate::AnimateToFit"
CHILD_OF = "bevy_ecs::hierarchy::ChildOf"
EDITOR_CAMERA = "hana::camera::editor_camera::EditorCamera"
SWITCH_SLIDER = "hana_catalyst::switch::SwitchSlider"
WINDOW = "bevy_window::window::Window"
PRIMARY_WINDOW = "bevy_window::window::PrimaryWindow"
ZOOM_TO_TARGET = "hana::camera::zoom_to_target::ZoomToTarget"
GRAYSCALE = "hana_catalyst::identity::Tool<hana_catalyst::effects::Grayscale>"
BLUR = "hana_catalyst::identity::Tool<hana_catalyst::effects::Blur>"
NAMES = ((11, "Log"), (12, "Twin"), (13, "Twin"))
TOOLS = ((30, GRAYSCALE, 3, "hana.effect.grayscale"), (20, GRAYSCALE, 1, "hana.effect.grayscale"), (40, BLUR, 2, "hana.effect.blur"))
EXTRA_ENTITY = 77
SHOT_SIZE = (4, 3)
REMOTE_HOST = "natemccoy@mac"
# Fake scp and ssh: the remote host is a local folder, and every call is logged as one JSON line.
# The keep-awake call answers "awake", then logs "ended" once the script closes its stdin.
FAKE_REMOTE = """\
#!{python}
import json, shutil, sys
from pathlib import Path
remote = Path({remote!r})
name = Path(sys.argv[0]).name
def log(entry):
    with open({log!r}, "a") as handle:
        handle.write(json.dumps(entry) + "\\n")
log([name, *sys.argv[1:]])
if name == "scp":
    if {copies!r}:
        shutil.copyfile(remote / Path(sys.argv[2]).name, sys.argv[3])
elif sys.argv[2:] == ["uname", "-s"]:
    print({system!r})
elif sys.argv[2] == "rm":
    (remote / Path(sys.argv[-1]).name).unlink(missing_ok=True)
else:
    print("awake", flush=True)
    sys.stdin.read()
    log(["ended"])
"""


class Request(TypedDict):
    method: str
    params: NotRequired[dict[str, object] | None]


class Call(TypedDict):
    method: str
    params: dict[str, object]


class SceneEntityFixture(TypedDict):
    entity: int
    components: dict[str, object]
    has: NotRequired[dict[str, bool]]


class TimingLine(TypedDict):
    status: str
    exit_code: int
    session: dict[str, str]
    attempts: list[dict[str, object]]
    port: int
    mode: str
    crop: str
    window: str
    width: int
    height: int
    resolve_ms: float
    settle_ms: float
    capture_ms: float
    total_ms: float


def png(width: int, height: int, brightness: int = 128) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixel = bytes([brightness]) * 3
    rows = b"".join(b"\x00" + pixel * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def camera_components(
    physical_size: tuple[int, int] = (1280, 720), scale_factor: float = 1.0,
    yaw: float = 0.0, pitch: float = 0.3, radius: float = 6.0,
) -> dict[str, object]:
    angles = {"yaw": yaw, "pitch": pitch}
    return {
        "hana_lagrange::orbit_cam::OrbitCam": {
            "orbit": {"current": angles, "target": angles},
            "pan": {"current": [0.0, 1.0, 0.0], "target": [0.0, 1.0, 0.0]},
            "zoom": {"current": radius, "target": radius},
            "update_request": "None",
        },
        "bevy_transform::components::global_transform::GlobalTransform": [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, 0, 2.0, 6.0],
        "bevy_camera::camera::Camera": {
            "viewport": None,
            "computed": {
                "clip_from_view": [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 0, -1.0, 0, 0, 0.1, 0],
                "target_info": {"physical_size": list(physical_size), "scale_factor": scale_factor},
            },
        },
    }


class Fake:
    """Answers the BRP calls hana_shot makes; the camera never moves, so every settle ends after three frames."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.named_entities: list[SceneEntityFixture] = [
            {"entity": entity, "components": {NAME: name}, "has": {AABB: True}}
            for entity, name in NAMES
        ]
        self.named_entity_snapshots: list[list[SceneEntityFixture]] = []
        self.tool_entities: list[tuple[int, str, int, str]] = list(TOOLS)
        self.fit_moves_camera: bool = False
        self.camera_yaw: float = 0.0
        self.camera_pitch: float = 0.3
        self.camera_radius: float = 6.0
        self.frame: int = 0
        self.screenshot: str = "ok"
        self.remote: Path | None = None
        self.window_physical_size: tuple[int, int] = (1280, 720)
        self.window_scale_factor: float = 1.0
        self.window_scale_factor_override: float | None = None
        self.window_level: str = "Normal"
        self.refuse_window_level_raise_once: bool = False
        self.refuse_window_level_restore: bool = False
        self.stop_on_shutdown: bool = False
        self.screenshot_started: threading.Event = threading.Event()
        self.release: threading.Event = threading.Event()
        self.lock: threading.Lock = threading.Lock()

    def methods(self, method: str) -> list[dict[str, object]]:
        with self.lock:
            return [call["params"] for call in self.calls if call["method"] == method]

    def answer(self, method: str, params: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.calls.append({"method": method, "params": params})
            screenshots = sum(1 for call in self.calls if call["method"] == "brp_extras/screenshot")
            if self.fit_moves_camera and method == "world.trigger_event" and params.get("event") == ANIMATE_TO_FIT:
                fit = cast(dict[str, object], params["value"])
                self.camera_yaw = cast(float, fit["yaw"])
                self.camera_pitch = cast(float, fit["pitch"])
                self.camera_radius = 6.0 * (1.0 + cast(float, fit["margin"]))
            if method == "world.mutate_components" and params.get("component") == WINDOW:
                width, height = self.window_physical_size
                if params.get("path") == ".resolution.physical_width":
                    self.window_physical_size = (cast(int, params["value"]), height)
                elif params.get("path") == ".resolution.physical_height":
                    self.window_physical_size = (width, cast(int, params["value"]))
                elif params.get("path") == ".window_level":
                    level = cast(str, params["value"])
                    if self.refuse_window_level_raise_once and level == "AlwaysOnTop":
                        self.refuse_window_level_raise_once = False
                        return {"error": {"code": -32603, "message": "raise refused once"}}
                    if self.refuse_window_level_restore and level != "AlwaysOnTop":
                        return {"error": {"code": -32603, "message": "restore refused"}}
                    self.window_level = level
            if method == "brp_extras/get_diagnostics":
                self.frame += 1
                return {"result": {"frame_time_ms": {"current": 8.0}, "frame_count": float(self.frame)}}
        if method == "world.query":
            return {"result": query(self, params)}
        if method == "world.get_resources":
            return {"result": {"value": {"home": {"animation_duration_milliseconds": 2000}}}}
        if method == "world.get_components":
            entity = params["entity"]
            components = camera_components(
                self.window_physical_size, self.window_scale_factor_override or self.window_scale_factor,
                self.camera_yaw, self.camera_pitch, self.camera_radius,
            ) if entity == CAMERA_ENTITY else next(
                (row["components"] for row in self.named_entities if row["entity"] == entity), dict[str, object](),
            )
            requested = cast(list[str], params["components"])
            return {"result": {
                "components": {path: components[path] for path in requested if path in components}, "errors": {},
            }}
        if method == "world.list_components":
            entity = params.get("entity")
            if entity is None:
                return {"result": [NAME, AABB, GRAYSCALE, BLUR]}
            if entity == EXTRA_ENTITY:
                return {"result": [NAME]}
            return {"error": {"code": -23402, "message": f"entity {entity} not found"}}
        if method == "brp_extras/screenshot":
            self.screenshot_started.set()
            if self.screenshot in ("hang", "hold"):
                _ = self.release.wait(TEST_HANG_GUARD_SECS)
                return {"result": None}
            if self.screenshot == "busy-always" or (self.screenshot == "busy-once" and screenshots == 1):
                return {"error": {"code": -32603, "message": "Screenshot already in progress"}}
            if self.screenshot == "error":
                return {"error": {"code": -32603, "message": "camera rejected capture"}}
            path = Path(cast(str, params["path"]))
            if self.remote is not None:
                path = self.remote / path.name
            if self.screenshot == "invalid":
                _ = path.write_bytes(b"not png")
            elif self.screenshot == "black":
                _ = path.write_bytes(png(*SHOT_SIZE, brightness=0))
            elif self.screenshot == "tiny":
                _ = path.write_bytes(png(1, 1))
            else:
                _ = path.write_bytes(png(*SHOT_SIZE))
            return {"result": {"path": params["path"], "status": "ok"}}
        return {"result": None}


def query(fake: Fake, params: dict[str, object]) -> list[SceneEntityFixture]:
    with_types = cast(dict[str, list[str]], params.get("filter") or {}).get("with", [])
    data = cast(dict[str, list[str]], params.get("data") or {})
    rows: list[SceneEntityFixture]
    if EDITOR_CAMERA in with_types:
        rows = [{"entity": CAMERA_ENTITY, "components": camera_components(
            fake.window_physical_size,
            fake.window_scale_factor_override or fake.window_scale_factor,
            fake.camera_yaw, fake.camera_pitch, fake.camera_radius,
        )}]
    elif PRIMARY_WINDOW in with_types:
        width, height = fake.window_physical_size
        rows = [{"entity": WINDOW_ENTITY, "components": {WINDOW: {"resolution": {
            "physical_width": width,
            "physical_height": height,
            "scale_factor_override": fake.window_scale_factor_override,
            "scale_factor": fake.window_scale_factor,
        }, "window_level": fake.window_level}}}]
    elif SWITCH_SLIDER in with_types:
        rows = [
            {"entity": entity, "components": {path: {"id": tool_id, "definition": definition}}, "has": {AABB: False}}
            for entity, path, tool_id, definition in fake.tool_entities
        ]
    elif NAME in data.get("components", []) or GLOBAL_TRANSFORM in data.get("components", []):
        if fake.named_entity_snapshots:
            fake.named_entities = fake.named_entity_snapshots.pop(0)
        rows = fake.named_entities
    else:
        rows = []
    required = data.get("components", [])
    requested = [*required, *data.get("option", [])]
    result: list[SceneEntityFixture] = []
    for row in rows:
        components = row["components"]
        if not all(path in components for path in required):
            continue
        projected: SceneEntityFixture = {
            "entity": row["entity"],
            "components": {path: components[path] for path in requested if path in components},
        }
        if data.get("has"):
            projected["has"] = {
                path: row.get("has", {}).get(path, path in components) for path in data["has"]
            }
        result.append(projected)
    return result


def handler_for(fake: Fake) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            request = cast(Request, json.loads(self.rfile.read(length)))
            reply = {"jsonrpc": "2.0", "id": 1, **fake.answer(request["method"], request.get("params") or {})}
            body = json.dumps(reply).encode()
            if request["method"] == "brp_extras/shutdown" and fake.stop_on_shutdown:
                self.server.shutdown()
                self.server.server_close()
            try:
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                _ = self.wfile.write(body)
            except OSError:
                pass

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


class HanaShotTest(unittest.TestCase):
    fake_listeners: list[ThreadingHTTPServer] = []
    scratch: Path = Path()
    fake: Fake = Fake()
    port: int = 0

    @override
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.fake = Fake()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.fake))
        self.fake_listeners = [server]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(self.fake.release.set)
        self.port = server.server_port

    @property
    def timings(self) -> Path:
        return self.scratch / "cache" / "hana-shot" / "timings.jsonl"

    def timing_lines(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in self.timings.read_text().splitlines()]

    def script_environment(self, session_variables: dict[str, str] | None = None) -> dict[str, str]:
        environment = {
            **os.environ,
            "XDG_CACHE_HOME": str(self.scratch / "cache"),
            "HANA_SHOT_CAPTURE_TIMEOUT": "1",
            # RemoteTests puts its fake scp and ssh here.
            "PATH": f"{self.scratch / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        for name in ("CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            _ = environment.pop(name, None)
        if session_variables is not None:
            environment.update(session_variables)
        return environment

    def run_python(
        self, program: Path, *args: str, session_variables: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(program), *args],
            cwd=self.scratch, env=self.script_environment(session_variables), capture_output=True, text=True,
            timeout=TEST_HANG_GUARD_SECS, check=False,
        )

    def run_script(self, *args: str, session_variables: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return self.run_python(SCRIPT, *args, session_variables=session_variables)

    def start_python(self, program: Path, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, str(program), *args],
            cwd=self.scratch, env=self.script_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def start_script(self, *args: str) -> subprocess.Popen[str]:
        return self.start_python(SCRIPT, *args)

    def stop_script(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
        _ = process.communicate()

    def shot(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_script("shot", "--port", str(self.port), "--out", str(self.scratch / "shot.png"), *args)

    def views(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_script("views", *args, "--views-file", str(self.scratch / "views.toml"))


def face_transforms() -> dict[str, list[float]]:
    half = math.sqrt(0.5)
    sixth = 1.0 / math.sqrt(6.0)
    third = 1.0 / math.sqrt(3.0)
    return {
        "identity": [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, 13, -7, 21],
        "yawed": [0, 0, -1.0, 0, 1.0, 0, 1.0, 0, 0, 13, -7, 21],
        "pitched": [1.0, 0, 0, 0, half, half, 0, -half, half, 13, -7, 21],
        "tilted_scaled": [
            2 * half, 2 * half, 0,
            -3 * sixth, 3 * sixth, 6 * sixth,
            4 * third, -4 * third, 4 * third,
            13, -7, 21,
        ],
    }


class FaceAngleTests(unittest.TestCase):
    def test_all_local_axes_round_trip_through_hana_camera_offset(self) -> None:
        for label, matrix in face_transforms().items():
            for column, axis in enumerate(("x", "y", "z")):
                for sign in ("+", "-"):
                    face = sign + axis
                    with self.subTest(transform=label, face=face):
                        if label in ("identity", "yawed") and axis == "y":
                            with self.assertRaises(hana_shot.Refused):
                                _ = hana_shot.face_angles(matrix, face)
                            continue
                        direction = matrix[3 * column:3 * column + 3]
                        length = math.sqrt(sum(value * value for value in direction))
                        expected = [value / length * (1 if sign == "+" else -1) for value in direction]
                        yaw, pitch = hana_shot.face_angles(matrix, face)
                        offset = (
                            math.sin(yaw) * math.cos(pitch),
                            math.sin(pitch),
                            math.cos(yaw) * math.cos(pitch),
                        )
                        for actual, wanted in zip(offset, expected, strict=True):
                            self.assertAlmostEqual(actual, wanted, places=10)

    def test_near_vertical_axis_refuses_inside_one_degree_and_accepts_outside(self) -> None:
        for degrees in (0.5, 1.5):
            angle = math.radians(degrees)
            matrix = [1.0, 0, 0, 0, math.sin(angle), -math.cos(angle),
                      0, math.cos(angle), math.sin(angle), 0, 0, 0]
            for face in ("+z", "-z"):
                with self.subTest(degrees=degrees, face=face):
                    if degrees < 1:
                        with self.assertRaises(hana_shot.Refused) as refused:
                            _ = hana_shot.face_angles(matrix, face)
                        text = str(refused.exception)
                        self.assertIn(face, text)
                        self.assertIn("--yaw 0", text)
                        self.assertIn("--pitch " + ("1.55" if face == "+z" else "-1.55"), text)
                    else:
                        _, pitch = hana_shot.face_angles(matrix, face)
                        self.assertAlmostEqual(abs(pitch), math.radians(88.5), places=10)


class FaceShotTests(HanaShotTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.fake.named_entities = [self.panel(face_transforms()["tilted_scaled"])]

    def panel(self, matrix: list[float]) -> SceneEntityFixture:
        return {"entity": 101, "components": {NAME: "Panel", GLOBAL_TRANSFORM: matrix}, "has": {AABB: False}}

    def fit_events(self) -> list[dict[str, object]]:
        return [
            cast(dict[str, object], request["value"])
            for request in self.fake.methods("world.trigger_event") if request.get("event") == ANIMATE_TO_FIT
        ]

    def test_face_fit_event_uses_target_axis_and_preserves_margin_and_record(self) -> None:
        self.fake.fit_moves_camera = True
        result = self.shot("--target", "name:Panel", "--face", "+z", "--mode", "fit", "--margin", "0.2")

        self.assertEqual(result.returncode, 0, result.stderr)
        [event] = self.fit_events()
        self.assertEqual(event["target"], 101)
        self.assertAlmostEqual(cast(float, event["yaw"]), math.pi / 4)
        self.assertAlmostEqual(cast(float, event["pitch"]), -math.asin(1 / math.sqrt(3)))
        self.assertEqual(event["margin"], 0.2)
        [line] = self.timing_lines()
        self.assertEqual(line["face"], "+z")
        [attempt] = cast(list[dict[str, object]], line["attempts"])
        self.assertEqual(attempt["face"], "+z")

    def test_face_pose_defaults_focus_to_translation_and_keeps_radius(self) -> None:
        result = self.shot("--target", "name:Panel", "--face", "-z", "--mode", "pose", "--radius", "9")

        self.assertEqual(result.returncode, 0, result.stderr)
        [mutation] = [request for request in self.fake.methods("world.mutate_components")
                      if request.get("component") == ORBIT_CAM]
        pose = cast(dict[str, dict[str, object]], mutation["value"])
        self.assertEqual(pose["pan"]["current"], [13, -7, 21])
        self.assertEqual(pose["pan"]["target"], [13, -7, 21])
        self.assertEqual(pose["zoom"]["current"], 9.0)
        angles = cast(dict[str, float], pose["orbit"]["current"])
        self.assertAlmostEqual(angles["yaw"], -3 * math.pi / 4)
        self.assertAlmostEqual(angles["pitch"], math.asin(1 / math.sqrt(3)))
        [line] = self.timing_lines()
        self.assertEqual(line["face"], "-z")

    def test_face_pose_explicit_focus_wins_over_target_translation(self) -> None:
        result = self.shot("--target", "name:Panel", "--face", "+z", "--mode", "pose",
                           "--radius", "9", "--focus", "1,2,3")

        self.assertEqual(result.returncode, 0, result.stderr)
        [mutation] = [request for request in self.fake.methods("world.mutate_components")
                      if request.get("component") == ORBIT_CAM]
        pose = cast(dict[str, dict[str, object]], mutation["value"])
        self.assertEqual(pose["pan"]["current"], [1, 2, 3])

    def test_face_conflicts_with_explicit_zero_yaw_or_pitch(self) -> None:
        for flag in ("--yaw", "--pitch"):
            with self.subTest(flag=flag):
                result = self.shot("--target", "name:Panel", "--face", "+z", flag, "0")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--face", result.stderr)
                self.assertIn(flag, result.stderr)
                self.assertRegex(result.stderr.lower(), r"drop|remove|omit")
        self.assertEqual(self.fake.calls, [])

    def test_face_refuses_modes_without_angles_and_names_supported_modes(self) -> None:
        for mode in ("home", "hana-frame"):
            with self.subTest(mode=mode):
                result = self.shot("--target", "name:Panel", "--face", "+z", "--mode", mode)
                self.assertEqual(result.returncode, 2, result.stderr)
                for detail in ("face", mode, "fit", "pose"):
                    self.assertIn(detail, result.stderr)
        self.assertEqual(self.fake.calls, [])

    def test_face_pose_requires_target_and_radius(self) -> None:
        for flags, missing in (
            (("--radius", "9"), "--target"),
            (("--target", "name:Panel"), "--radius"),
        ):
            with self.subTest(missing=missing):
                result = self.shot("--mode", "pose", "--face", "+z", *flags)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(missing, result.stderr)
        self.assertEqual(self.fake.calls, [])

    def test_vertical_face_refusal_explains_orbit_limit_and_nearby_pose(self) -> None:
        self.fake.named_entities = [self.panel(face_transforms()["identity"])]
        for face, pitch in (("+y", "1.55"), ("-y", "-1.55")):
            with self.subTest(face=face):
                result = self.shot("--target", "name:Panel", "--face=" + face,
                                   "--mode", "pose", "--radius", "9")
                self.assertEqual(result.returncode, 2, result.stderr)
                for detail in (face, "yaw", "pitch", "--yaw 0", "--pitch " + pitch):
                    self.assertIn(detail, result.stderr)
                self.assertRegex(result.stderr.lower(), r"undefined|defin")
                self.assertRegex(result.stderr.lower(), r"clamp|limit")
                line = self.timing_lines()[-1]
                self.assertEqual(line["face"], face)
                [attempt] = cast(list[dict[str, object]], line["attempts"])
                self.assertEqual(attempt["face"], face)
        self.assertEqual(self.fake.methods("brp_extras/screenshot"), [])

    def test_views_add_and_show_preserve_face_without_storing_angles(self) -> None:
        for mode in ("fit", "pose"):
            with self.subTest(mode=mode):
                label = "panel-" + mode
                flags = ("--radius", "9") if mode == "pose" else ()
                result = self.views("add", label, "--target", "name:Panel", "--face", "+z", "--mode", mode, *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                document = tomllib.loads((self.scratch / "views.toml").read_text())
                view = cast(dict[str, object], document["views"][label])
                self.assertEqual((view["face"], view["mode"], view["target"]), ("+z", mode, "name:Panel"))
                self.assertNotIn("yaw", view)
                self.assertNotIn("pitch", view)
                if mode == "pose":
                    self.assertEqual(view["radius"], 9)
                    self.assertNotIn("focus", view)
                shown = self.views("show", label)
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertRegex(shown.stdout, r'(?m)^face\s*=\s*"\+z"$')
        self.assertEqual(self.fake.calls, [])

    def test_stored_face_pose_loads_and_defaults_focus_to_target(self) -> None:
        views_path = self.scratch / "views.toml"
        _ = views_path.write_text('[views.panel]\ntarget = "name:Panel"\nmode = "pose"\nface = "+z"\nradius = 9\n')
        result = self.shot("--view", "panel", "--views-file", str(views_path))

        self.assertEqual(result.returncode, 0, result.stderr)
        [mutation] = [request for request in self.fake.methods("world.mutate_components")
                      if request.get("component") == ORBIT_CAM]
        pose = cast(dict[str, dict[str, object]], mutation["value"])
        self.assertEqual(pose["pan"]["current"], [13, -7, 21])
        self.assertEqual(pose["zoom"]["current"], 9)
        [line] = self.timing_lines()
        self.assertEqual(line["face"], "+z")

    def test_explicit_focus_overrides_stored_face_pose_default(self) -> None:
        views_path = self.scratch / "views.toml"
        _ = views_path.write_text('[views.panel]\ntarget = "name:Panel"\nmode = "pose"\nface = "+z"\nradius = 9\n')
        result = self.shot("--view", "panel", "--views-file", str(views_path), "--focus", "1,2,3")

        self.assertEqual(result.returncode, 0, result.stderr)
        [mutation] = [request for request in self.fake.methods("world.mutate_components")
                      if request.get("component") == ORBIT_CAM]
        pose = cast(dict[str, dict[str, object]], mutation["value"])
        self.assertEqual(pose["pan"]["current"], [1, 2, 3])

    def test_face_conflicts_are_checked_across_stored_views_and_cli_flags(self) -> None:
        for keys, flags, detail in (
            ('face = "+z"', ("--yaw", "0"), "--yaw"),
            ('face = "+z"', ("--pitch", "0"), "--pitch"),
            ('yaw = 0', ("--face", "+z"), "yaw"),
        ):
            with self.subTest(keys=keys, flags=flags):
                views_path = self.scratch / "views.toml"
                _ = views_path.write_text('[views.panel]\ntarget = "name:Panel"\nmode = "fit"\n' + keys + "\n")
                result = self.shot("--view", "panel", "--views-file", str(views_path), *flags)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--face", result.stderr)
                self.assertIn(detail, result.stderr)
                self.assertRegex(result.stderr.lower(), r"drop|remove|omit")
        self.assertEqual(self.fake.calls, [])

    def test_failed_face_fit_retains_axis_on_invocation_and_attempt(self) -> None:
        result = self.shot("--target", "name:Panel", "--face", "+z", "--mode", "fit")

        self.assertEqual(result.returncode, 1, result.stderr)
        [line] = self.timing_lines()
        self.assertEqual(line["face"], "+z")
        [attempt] = cast(list[dict[str, object]], line["attempts"])
        self.assertEqual(attempt["face"], "+z")
        self.assertIn("name:Panel", cast(str, line["error"]))

    def test_stored_face_rejects_present_zero_angles_and_unsupported_modes(self) -> None:
        cases = (
            ('yaw = 0', "yaw"), ('pitch = 0', "pitch"),
            ('mode = "home"', "home"), ('mode = "hana-frame"', "hana-frame"),
            ('face = "front"', "front"),
        )
        for extra, detail in cases:
            with self.subTest(extra=extra):
                base = '[views.panel]\ntarget = "name:Panel"\n'
                if not extra.startswith("face"):
                    base += 'face = "+z"\n'
                _ = (self.scratch / "views.toml").write_text(base + extra + "\n")
                result = self.views("show", "panel")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("face", result.stderr)
                self.assertIn(detail, result.stderr)
                if detail in ("home", "hana-frame"):
                    self.assertIn("fit", result.stderr)
                    self.assertIn("pose", result.stderr)
        self.assertEqual(self.fake.calls, [])

    def test_views_check_runs_and_stamps_a_face_fit(self) -> None:
        self.fake.fit_moves_camera = True
        _ = (self.scratch / "views.toml").write_text(
            '[views.panel]\ntarget = "name:Panel"\nmode = "fit"\nface = "+z"\n',
        )
        fake_magick = self.scratch / "bin" / "magick"
        fake_magick.parent.mkdir()
        _ = fake_magick.write_text("#!/bin/sh\necho 0.5\n")
        fake_magick.chmod(0o755)
        result = self.views("check", "--port", str(self.port), "--out", str(self.scratch / "check"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS panel", result.stdout)
        document = tomllib.loads((self.scratch / "views.toml").read_text())
        view = cast(dict[str, object], document["views"]["panel"])
        self.assertIn("verified", view)
        self.assertEqual(view["face"], "+z")
        self.assertNotIn("yaw", view)
        self.assertNotIn("pitch", view)
        [event] = self.fit_events()
        self.assertAlmostEqual(cast(float, event["pitch"]), -math.asin(1 / math.sqrt(3)))

    def test_views_check_continues_after_face_refusal_and_stamps_passing_views(self) -> None:
        self.fake.fit_moves_camera = True
        self.fake.named_entities = [self.panel(face_transforms()["identity"])]
        views_path = self.scratch / "views.toml"
        _ = views_path.write_text("".join(
            f'[views.{name}]\ntarget = "name:Panel"\nmode = "fit"\nface = "{face}"\n'
            for name, face in (("first", "+z"), ("vertical", "+y"), ("last", "-z"))
        ))
        fake_magick = self.scratch / "bin" / "magick"
        fake_magick.parent.mkdir()
        _ = fake_magick.write_text("#!/bin/sh\necho 0.5\n")
        fake_magick.chmod(0o755)

        result = self.views("check", "--port", str(self.port), "--out", str(self.scratch / "check"))

        self.assertEqual(result.returncode, 1, result.stderr)
        document = tomllib.loads(views_path.read_text())
        views = cast(dict[str, dict[str, object]], document["views"])
        for name in ("first", "last"):
            self.assertIn("verified", views[name])
            self.assertIn("PASS " + name, result.stdout)
        self.assertNotIn("verified", views["vertical"])
        self.assertIn("FAIL vertical:", result.stderr)
        [line] = self.timing_lines()
        self.assertEqual((line["status"], line["failure_reason"]), ("failure", "invalid_request"))
        attempts = cast(list[dict[str, object]], line["attempts"])
        self.assertEqual([attempt["view"] for attempt in attempts], ["first", "vertical", "last"])
        self.assertEqual([attempt["status"] for attempt in attempts], ["success", "failure", "success"])
        failed = attempts[1]
        self.assertEqual((failed["face"], failed["failure_reason"]), ("+y", "invalid_request"))
        error = cast(str, failed["error"])
        for detail in ("+y", "vertical", "--yaw 0", "--pitch 1.55"):
            self.assertIn(detail, error)
        self.assertIn(error, result.stderr)
        self.assertIn(error, cast(str, line["error"]))
        self.assertEqual(len(self.fake.methods("brp_extras/screenshot")), 2)

    def test_repeated_face_view_moves_again_when_target_turns(self) -> None:
        self.fake.fit_moves_camera = True
        self.fake.named_entity_snapshots = [
            [self.panel(face_transforms()["yawed"])],
            [self.panel(face_transforms()["tilted_scaled"])],
        ]
        views_path = self.scratch / "views.toml"
        _ = views_path.write_text('[views.panel]\ntarget = "name:Panel"\nmode = "fit"\nface = "+z"\n')
        result = self.run_script(
            "shot", "--port", str(self.port), "--out", str(self.scratch / "shots"),
            "--view", "panel", "--view", "panel", "--views-file", str(views_path),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        first, second = self.fit_events()
        self.assertAlmostEqual(cast(float, first["yaw"]), math.pi / 2)
        self.assertAlmostEqual(cast(float, first["pitch"]), 0.0)
        self.assertAlmostEqual(cast(float, second["yaw"]), math.pi / 4)
        self.assertAlmostEqual(cast(float, second["pitch"]), -math.asin(1 / math.sqrt(3)))
        self.assertEqual(len(self.fake.methods("brp_extras/screenshot")), 2)


class LaunchBuildWarningTests(unittest.TestCase):
    root: Path = Path()
    executable: Path = Path()
    source: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.source = self.root / "crates" / "hana" / "main.rs"
        self.source.parent.mkdir(parents=True)
        _ = self.source.write_text("committed source\n")
        _ = (self.root / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/hana"]\n')
        _ = (self.root / "Cargo.lock").write_text("version = 4\n")
        self.git("init", "--quiet")
        self.git("add", "crates/", "Cargo.toml", "Cargo.lock")
        self.git("-c", "user.name=Hana Shot Test", "-c", "user.email=hana-shot@example.invalid", "commit", "--quiet", "-m", "fixture")
        self.executable = self.root / "target" / "debug" / "hana"
        self.executable.parent.mkdir(parents=True)
        _ = self.executable.write_bytes(b"fake binary; never execute")
        os.utime(self.executable, ns=(2_000_000_000, 2_000_000_000))

    def git(self, *args: str) -> None:
        _ = subprocess.run(
            ["git", "-C", str(self.root), *args], stdin=subprocess.DEVNULL,
            capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS,
        )

    def modify_source(self, mtime_ns: int) -> None:
        _ = self.source.write_text("uncommitted source\n")
        os.utime(self.source, ns=(mtime_ns, mtime_ns))

    def test_clean_crates_do_not_warn_about_binary(self) -> None:
        self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), [])

    def test_modified_source_is_included_only_when_no_newer_than_binary(self) -> None:
        for mtime_ns, expected in (
            (1_000_000_000, ["crates/hana/main.rs"]),
            (2_000_000_000, ["crates/hana/main.rs"]),
            (3_000_000_000, []),
        ):
            with self.subTest(mtime_ns=mtime_ns):
                self.modify_source(mtime_ns)
                self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), expected)

    def test_untracked_source_older_than_binary_is_named_without_path_quoting(self) -> None:
        path = self.source.with_name('new "panel"\nsource.rs')
        _ = path.write_text("new source\n")
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), [path.relative_to(self.root).as_posix()])

    def test_modified_root_lockfile_older_than_binary_is_named(self) -> None:
        path = self.root / "Cargo.lock"
        _ = path.write_text("version = 4\n# dependency changed\n")
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), ["Cargo.lock"])

    def test_modified_root_manifest_newer_than_binary_is_excluded(self) -> None:
        path = self.root / "Cargo.toml"
        _ = path.write_text('[workspace]\nmembers = []\n')
        os.utime(path, ns=(3_000_000_000, 3_000_000_000))
        self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), [])

    def test_manifest_outside_crates_and_repository_root_is_excluded(self) -> None:
        path = self.root / "other" / "Cargo.toml"
        path.parent.mkdir()
        _ = path.write_text('[package]\nname = "unrelated"\n')
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), [])

    def test_deleted_root_cargo_files_are_named_without_file_mtimes(self) -> None:
        (self.root / "Cargo.toml").unlink()
        (self.root / "Cargo.lock").unlink()
        self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), ["Cargo.lock", "Cargo.toml"])

    def test_deleted_source_is_included_without_a_file_mtime(self) -> None:
        self.source.unlink()
        self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), ["crates/hana/main.rs"])

    def test_staged_added_source_is_included_and_other_directories_are_excluded(self) -> None:
        added = self.source.with_name("added.rs")
        _ = added.write_text("added source\n")
        os.utime(added, ns=(1_000_000_000, 1_000_000_000))
        self.git("add", str(added))
        _ = (self.root / "outside.rs").write_text("not crate source\n")
        self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), ["crates/hana/added.rs"])

    def test_rename_tracks_destination_and_undatable_removal(self) -> None:
        renamed = self.source.with_name("renamed panel.rs")
        self.git("mv", str(self.source), str(renamed))
        os.utime(renamed, ns=(1_000_000_000, 1_000_000_000))
        self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), [
            "crates/hana/main.rs", "crates/hana/renamed panel.rs",
        ])

    def test_missing_binary_includes_dirty_source_newer_than_previous_build(self) -> None:
        self.modify_source(3_000_000_000)
        untracked = self.source.with_name("additional.rs")
        _ = untracked.write_text("additional source\n")
        self.executable.unlink()
        self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), [
            "crates/hana/additional.rs", "crates/hana/main.rs",
        ])

    def test_non_git_root_does_not_block_launch_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(hana_shot.uncommitted_build_paths(Path(directory), self.executable), [])

    def test_git_timeout_or_unavailable_git_does_not_block_launch_check(self) -> None:
        for error in (subprocess.TimeoutExpired("git", 3), FileNotFoundError("git unavailable")):
            with self.subTest(error=type(error).__name__), mock.patch.object(subprocess, "run", side_effect=error):
                self.assertEqual(hana_shot.uncommitted_build_paths(self.root, self.executable), [])

    def test_missing_binary_refusal_names_edits_a_build_would_include(self) -> None:
        self.modify_source(3_000_000_000)
        self.executable.unlink()
        with mock.patch.object(hana_shot, "port_answers", return_value=False), self.assertRaises(hana_shot.Refused) as refused:
            _ = hana_shot.launch(15797, str(self.root), str(self.executable))
        self.assertIn(f"no Hana binary at {self.executable}; build it first (cargo build -p hana)", str(refused.exception))
        self.assertIn("uncommitted edits in crates/ or the root Cargo files will be included by a build: crates/hana/main.rs", str(refused.exception))

    def test_launch_warns_before_start_and_saves_all_uncommitted_paths_with_capped_text(self) -> None:
        paths: list[str] = []
        for index in range(12):
            path = self.source.with_name(f"new-{index:02}.rs")
            _ = path.write_text("uncommitted source\n")
            os.utime(path, ns=(1_000_000_000, 1_000_000_000))
            paths.append(path.relative_to(self.root).as_posix())
        diagnostics = io.StringIO()
        process = mock.Mock(pid=12345)

        def start_stub(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
            self.assertEqual(command[-1], str(self.executable))
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIn("so this Hana runs that code:", diagnostics.getvalue())
            return cast(subprocess.Popen[bytes], process)

        def make_scratch(*, prefix: str) -> str:
            self.assertEqual(prefix, "hana-shot-15797-")
            # Git status has already run; replace only the subsequent Hana start.
            _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=start_stub))
            scratch = self.root / "scratch"
            scratch.mkdir()
            return str(scratch)

        with (
            redirect_stderr(diagnostics),
            mock.patch.object(hana_shot, "port_answers", return_value=False),
            mock.patch.object(hana_shot, "wait_ready"),
            mock.patch.object(hana_shot, "launch_state_path", return_value=self.root / "launch.json"),
            mock.patch.object(tempfile, "mkdtemp", side_effect=make_scratch),
        ):
            state, _ = hana_shot.launch(15797, str(self.root), str(self.executable))
        warning = next(line for line in diagnostics.getvalue().splitlines() if line.startswith("hana_shot: warning:"))
        self.assertIn(f"{self.executable} was built after uncommitted edits in crates/ or the root Cargo files,", warning)
        for path in paths[:10]:
            self.assertIn(path, warning)
        for path in paths[10:]:
            self.assertNotIn(path, warning)
        self.assertTrue(warning.endswith("and 2 more"), warning)
        self.assertEqual(state.get("uncommitted_paths"), paths)
        saved = cast(dict[str, object], json.loads((self.root / "launch.json").read_text()))
        self.assertEqual(saved["uncommitted_paths"], paths)


class LaunchRecordRecoveryTests(HanaShotTest):
    state_path: Path = Path()
    root: Path = Path()
    executable: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.fake.stop_on_shutdown = True
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.script_environment(), clear=True)))
        self.state_path = hana_shot.launch_state_path(self.port)
        self.state_path.parent.mkdir(parents=True)
        self.root = self.scratch / "worktree"
        (self.root / "crates" / "hana").mkdir(parents=True)
        self.executable = self.root / "hana"
        _ = self.executable.write_bytes(b"fixture binary; never execute")

    def launch_without_starting_hana(self) -> hana_shot.LaunchState:
        launch_scratch = self.scratch / "new-launch-scratch"
        launch_scratch.mkdir()
        process = cast(subprocess.Popen[bytes], mock.Mock(pid=424242))
        with (
            mock.patch.object(hana_shot, "uncommitted_build_paths", return_value=[]),
            mock.patch.object(tempfile, "mkdtemp", return_value=str(launch_scratch)),
            mock.patch.object(subprocess, "Popen", return_value=process),
            mock.patch.object(hana_shot, "wait_ready"),
            redirect_stderr(io.StringIO()),
        ):
            state, _ = hana_shot.launch(self.port, str(self.root), str(self.executable))
        return state

    def stop_fake_listener(self) -> None:
        server = self.fake_listeners[-1]
        server.shutdown()
        server.server_close()

    def restart_fake_listener(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", self.port), handler_for(self.fake))
        self.fake_listeners.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def shutdown_command(self) -> tuple[int, str]:
        diagnostics = io.StringIO()
        handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            with redirect_stderr(diagnostics):
                exit_code = hana_shot.main(["shutdown", "--port", str(self.port)])
        finally:
            for number, handler in handlers.items():
                _ = signal.signal(number, handler)
        return exit_code, diagnostics.getvalue()

    def assert_unreadable_shutdown_then_relaunch(self, reason: str) -> None:
        with (
            mock.patch.object(hana_shot, "process_alive", side_effect=AssertionError("unidentified pid wait")),
            mock.patch.object(hana_shot, "same_binary", side_effect=AssertionError("unidentified binary")),
            mock.patch.object(os, "killpg", side_effect=AssertionError("unidentified process group")),
        ):
            exit_code, diagnostics = self.shutdown_command()

        self.assertEqual(exit_code, 0, diagnostics)
        self.assertEqual(self.fake.methods("brp_extras/shutdown"), [{}])
        self.assertFalse(self.state_path.exists())
        unreadable = [line for line in diagnostics.splitlines() if "is unreadable (" in line]
        self.assertEqual(len(unreadable), 1, diagnostics)
        line = unreadable[0]
        self.assertIn(f"hana_shot: launch record {self.state_path} is unreadable (", line)
        self.assertIn(reason, line.lower())
        self.assertIn(f"asked Hana on port {self.port} to stop and removed the record", line)
        self.assertIn(str(Path(tempfile.gettempdir())), line)
        self.assertRegex(line.lower(), r"check.*scratch|scratch.*check")
        state = self.launch_without_starting_hana()
        self.assertEqual(json.loads(self.state_path.read_text()), state)

    def test_partial_launch_record_shuts_down_and_allows_a_new_launch(self) -> None:
        _ = self.state_path.write_text('{"pid": 1, "po')

        self.assert_unreadable_shutdown_then_relaunch("json")

    def test_launch_record_without_binary_shuts_down_and_allows_a_new_launch(self) -> None:
        unknown_scratch = self.scratch / "unidentified-scratch"
        unknown_scratch.mkdir()
        _ = self.state_path.write_text(json.dumps({
            "pid": 1, "port": self.port, "scratch": str(unknown_scratch),
            "log": str(unknown_scratch / "hana.log"),
        }))

        self.assert_unreadable_shutdown_then_relaunch("binary")
        self.assertTrue(unknown_scratch.is_dir(), "a broken record cannot identify scratch to remove")

    def test_failed_atomic_replace_leaves_no_record_or_temporary_file_then_shutdown_works(self) -> None:
        self.stop_fake_listener()
        with mock.patch.object(os, "fsync", wraps=os.fsync) as sync:
            def interrupt_replace(source: str | Path, destination: str | Path) -> None:
                temporary = Path(source)
                self.assertEqual(temporary.parent, self.state_path.parent)
                self.assertEqual(Path(destination), self.state_path)
                self.assertFalse(self.state_path.exists())
                written = cast(dict[str, object], json.loads(temporary.read_text()))
                self.assertEqual(written["binary"], str(self.executable))
                self.assertEqual(written["port"], self.port)
                sync.assert_called()
                raise OSError("stopped before record replacement")

            with mock.patch.object(os, "replace", side_effect=interrupt_replace), self.assertRaisesRegex(
                OSError, "stopped before record replacement",
            ):
                _ = self.launch_without_starting_hana()

        self.assertEqual(list(self.state_path.parent.iterdir()), [])
        self.restart_fake_listener()
        exit_code, diagnostics = self.shutdown_command()
        self.assertEqual(exit_code, 0, diagnostics)
        self.assertEqual(self.fake.methods("brp_extras/shutdown"), [{}])

    def test_shutdown_removes_abandoned_temporary_records_only_for_its_port(self) -> None:
        stale = self.state_path.with_name(f".{self.state_path.name}.abandoned.tmp")
        other_port = self.state_path.with_name(".1024.json.abandoned.tmp")
        unrelated = self.state_path.with_name("notes.txt")
        for path in (stale, other_port, unrelated):
            _ = path.write_text('{"pid": 1, "po')

        exit_code, diagnostics = self.shutdown_command()

        self.assertEqual(exit_code, 0, diagnostics)
        self.assertEqual(self.fake.methods("brp_extras/shutdown"), [{}])
        self.assertFalse(stale.exists())
        self.assertFalse(self.state_path.exists())
        self.assertTrue(other_port.exists())
        self.assertTrue(unrelated.exists())

    def test_whole_launch_record_still_waits_for_pid_and_removes_scratch(self) -> None:
        self.stop_fake_listener()
        state = self.launch_without_starting_hana()
        self.restart_fake_listener()
        scratch = Path(state["scratch"])
        _ = Path(state["log"]).write_text("fixture panic: preserved diagnostic\n")
        pid_checks: list[int] = []

        def process_alive(pid: int) -> bool:
            self.assertEqual(self.fake.methods("brp_extras/shutdown"), [{}])
            self.assertTrue(scratch.exists(), "scratch must survive until the process exits")
            pid_checks.append(pid)
            return len(pid_checks) == 1

        with (
            mock.patch.object(hana_shot, "process_alive", side_effect=process_alive),
            mock.patch.object(time, "sleep"),
            mock.patch.object(os, "killpg", side_effect=AssertionError("exited process must not be killed")),
        ):
            exit_code, diagnostics = self.shutdown_command()

        self.assertEqual(exit_code, 0, diagnostics)
        self.assertEqual(pid_checks, [state["pid"], state["pid"]])
        self.assertFalse(scratch.exists())
        self.assertFalse(self.state_path.exists())
        self.assertIn("fixture panic: preserved diagnostic", diagnostics)
        self.assertNotIn("unreadable", diagnostics)

    def test_shutdown_recovers_newest_whole_temporary_record_and_checks_binary_before_killing(self) -> None:
        scratch = self.scratch / "interrupted-launch"
        scratch.mkdir()
        log = scratch / "hana.log"
        _ = log.write_text("temporary launch panic: kept diagnostic\n")
        state: hana_shot.LaunchState = {
            "pid": 424242, "binary": str(self.executable), "log": str(log),
            "scratch": str(scratch), "port": self.port, "started": "2026-10-29T00:00:00+00:00",
        }
        older = self.state_path.with_name(f".{self.state_path.name}.older.tmp")
        newest_whole = self.state_path.with_name(f".{self.state_path.name}.whole.tmp")
        newest_broken = self.state_path.with_name(f".{self.state_path.name}.broken.tmp")
        _ = older.write_text(json.dumps({**state, "pid": 111111}))
        _ = newest_whole.write_text(json.dumps(state))
        _ = newest_broken.write_text('{"pid": 1, "po')
        for stamp, path in enumerate((older, newest_whole, newest_broken), start=1):
            os.utime(path, ns=(stamp * 1_000_000_000, stamp * 1_000_000_000))
        identity_checks: list[tuple[int, str]] = []
        signals: list[tuple[int, int]] = []

        def identify(pid: int, binary: str) -> bool:
            self.assertTrue(scratch.exists())
            identity_checks.append((pid, binary))
            return True

        def kill_group(pid: int, number: int) -> None:
            self.assertEqual(identity_checks, [(state["pid"], state["binary"])])
            signals.append((pid, number))

        with (
            mock.patch.object(time, "monotonic", side_effect=[0.0, 0.0, 21.0]),
            mock.patch.object(time, "sleep"),
            mock.patch.object(hana_shot, "process_alive", return_value=True) as alive,
            mock.patch.object(hana_shot, "same_binary", side_effect=identify),
            mock.patch.object(os, "killpg", side_effect=kill_group),
        ):
            exit_code, diagnostics = self.shutdown_command()

        self.assertEqual(exit_code, 0, diagnostics)
        self.assertEqual(self.fake.methods("brp_extras/shutdown"), [{}])
        alive.assert_called_once_with(state["pid"])
        self.assertEqual(signals, [(state["pid"], signal.SIGTERM), (state["pid"], signal.SIGKILL)])
        self.assertFalse(scratch.exists())
        self.assertEqual(list(self.state_path.parent.iterdir()), [])
        self.assertIn("temporary launch panic: kept diagnostic", diagnostics)

    def test_shutdown_never_kills_recovered_pid_when_binary_identity_changed(self) -> None:
        self.stop_fake_listener()
        scratch = self.scratch / "reused-pid-scratch"
        scratch.mkdir()
        temporary = self.state_path.with_name(f".{self.state_path.name}.whole.tmp")
        _ = temporary.write_text(json.dumps({
            "pid": 424242, "binary": str(self.executable),
            "log": str(scratch / "hana.log"), "scratch": str(scratch),
        }))
        with (
            mock.patch.object(time, "monotonic", side_effect=[0.0, 0.0, 21.0]),
            mock.patch.object(time, "sleep"),
            mock.patch.object(hana_shot, "process_alive", return_value=True),
            mock.patch.object(hana_shot, "same_binary", return_value=False) as identity,
            mock.patch.object(os, "killpg", side_effect=AssertionError("reused pid must not be killed")),
        ):
            exit_code, diagnostics = self.shutdown_command()

        self.assertEqual(exit_code, 0, diagnostics)
        identity.assert_called_once_with(424242, str(self.executable))
        self.assertFalse(temporary.exists())
        self.assertFalse(scratch.exists())
        self.assertIn(f"Hana on port {self.port} shut down", diagnostics)

    def test_shutdown_without_launch_or_listener_reports_no_answer_and_allows_relaunch(self) -> None:
        self.stop_fake_listener()

        exit_code, diagnostics = self.shutdown_command()

        self.assertEqual(exit_code, 0, diagnostics)
        self.assertIn(f"hana_shot: no Hana answered on port {self.port};", diagnostics)
        self.assertIn("run shutdown again when the next launch reports the port in use", diagnostics)
        self.assertNotIn("shut down", diagnostics)
        state = self.launch_without_starting_hana()
        self.assertEqual(json.loads(self.state_path.read_text()), state)

    def test_shutdown_keeps_broken_records_when_unidentified_listener_outlives_deadline(self) -> None:
        self.fake.stop_on_shutdown = False
        partial = '{"pid": 1, "po'
        _ = self.state_path.write_text(partial)
        temporary = self.state_path.with_name(f".{self.state_path.name}.broken.tmp")
        _ = temporary.write_text(partial)
        with (
            mock.patch.object(time, "monotonic", side_effect=[0.0, 21.0]),
            mock.patch.object(time, "sleep"),
            mock.patch.object(hana_shot, "process_alive", side_effect=AssertionError("unidentified pid wait")),
            mock.patch.object(hana_shot, "same_binary", side_effect=AssertionError("unidentified binary")),
            mock.patch.object(os, "killpg", side_effect=AssertionError("unidentified process group")),
        ):
            exit_code, diagnostics = self.shutdown_command()

        self.assertNotEqual(exit_code, 0, diagnostics)
        self.assertEqual(self.fake.methods("brp_extras/shutdown"), [{}])
        self.assertIn(str(self.port), diagnostics)
        self.assertIn("next step", diagnostics.lower())
        self.assertIn("shutdown", diagnostics.lower())
        self.assertNotIn("shut down", diagnostics)
        self.assertEqual(self.state_path.read_text(), partial)
        self.assertEqual(temporary.read_text(), partial)
        self.assertTrue(hana_shot.port_answers(self.port))


class PortGuardTests(HanaShotTest):
    def test_user_port_is_refused_by_every_command(self) -> None:
        for argv in (
            ["shot", "--port", "15702", "--mode", "home"],
            ["pose", "get", "--port", "15702"],
            ["targets", "--port", "15702"],
            ["launch", "--port", "15702"],
            ["shutdown", "--port", "15702"],
            ["views", "check", "--port", "15702", "--views-file", str(self.scratch / "views.toml")],
        ):
            with self.subTest(argv=argv[0]):
                result = self.run_script(*argv)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("15702 is the user's own Hana", result.stderr)

    def test_port_outside_the_range_is_refused(self) -> None:
        result = self.run_script("shot", "--port", "80", "--mode", "home")
        self.assertEqual(result.returncode, 2)
        self.assertIn("1024 to 65535", result.stderr)


class SelectorTests(HanaShotTest):
    def target_queries(self) -> list[dict[str, object]]:
        return [
            request for request in self.fake.methods("world.query")
            if EDITOR_CAMERA not in cast(dict[str, list[str]], request.get("filter") or {}).get("with", [])
            and PRIMARY_WINDOW not in cast(dict[str, list[str]], request.get("filter") or {}).get("with", [])
        ]

    def test_malformed_selectors_are_refused_before_any_call(self) -> None:
        for selector in ("bogus", "entity:abc", "name:", "color:red"):
            with self.subTest(selector=selector):
                result = self.shot("--target", selector, "--mode", "hana-frame")
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.fake.calls, [])

    def test_each_selector_kind_frames_the_entity_it_names(self) -> None:
        cases = (
            ("tool:Grayscale#2", 30),
            ("tool:grayscale", None),
            ("tool:hana.effect.blur", 40),
            ("name:Log", 11),
            (f"entity:{EXTRA_ENTITY}", EXTRA_ENTITY),
        )
        for selector, entity in cases:
            with self.subTest(selector=selector):
                before = len(self.fake.methods("world.trigger_event"))
                result = self.shot("--target", selector, "--mode", "hana-frame")
                triggers = self.fake.methods("world.trigger_event")[before:]
                if entity is None:
                    self.assertEqual(result.returncode, 1)
                    for identity in ("20", "30", "Grayscale"):
                        self.assertIn(identity, result.stderr)
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(triggers, [{"event": ZOOM_TO_TARGET, "value": {"entity": entity}}])

    def test_ambiguous_and_missing_targets_fail(self) -> None:
        for selector, found in (("name:Twin", "Twin"), ("tool:Grayscale#5", "nothing"), ("entity:999", "nothing")):
            with self.subTest(selector=selector):
                result = self.shot("--target", selector, "--mode", "hana-frame")
                self.assertEqual(result.returncode, 1)
                self.assertIn(found, result.stderr)
        self.assertEqual(self.fake.methods("brp_extras/screenshot"), [])

    def test_stale_entity_explains_restart_and_stable_selectors(self) -> None:
        result = self.shot("--target", "entity:999", "--mode", "hana-frame")

        self.assertEqual(result.returncode, 1, result.stderr)
        for detail in ("entity:999", "restart", "name:", "tool:", "marker:"):
            self.assertIn(detail, result.stderr.lower())
        self.assertEqual(len(self.fake.methods("world.list_components")), 1)
        self.assertEqual(self.target_queries(), [])

    def test_missing_name_suggests_at_most_five_nearest_names_without_another_query(self) -> None:
        candidates = ["Panel", "PanelLeft", "PanelRight", "PanelTop", "PanelBase", "PanelRear", "Unrelated"]
        self.fake.named_entities = [
            {"entity": 100 + index, "components": {NAME: name}, "has": {AABB: True}}
            for index, name in enumerate(candidates)
        ]
        result = self.shot("--target", "name:Panle", "--mode", "hana-frame")

        self.assertEqual(result.returncode, 1, result.stderr)
        suggested = [name for name in candidates if re.search(rf"\b{re.escape(name)}\b", result.stderr)]
        self.assertIn("Panel", suggested)
        self.assertGreater(len(suggested), 1)
        self.assertLessEqual(len(suggested), 5)
        self.assertNotIn("Unrelated", suggested)
        self.assertEqual(len(self.target_queries()), 1)
        self.assertTrue(all("entity" not in request for request in self.fake.methods("world.list_components")))

    def test_missing_tool_suggests_kind_from_entities_already_resolved(self) -> None:
        result = self.shot("--target", "tool:Grayscal", "--mode", "hana-frame")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Grayscale", result.stderr)
        self.assertEqual(len(self.target_queries()), 1)
        self.assertTrue(all("entity" not in request for request in self.fake.methods("world.list_components")))

    def test_ambiguous_name_lists_each_match_with_its_name(self) -> None:
        result = self.shot("--target", "name:Twin", "--mode", "hana-frame")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertGreaterEqual(result.stderr.count("Twin"), 2)
        for entity in (12, 13):
            self.assertRegex(result.stderr, rf"(?:{entity}[^\n,;]*Twin|Twin[^\n,;]*{entity})")

    def test_rejected_fit_names_pose_flags_and_parent_geometry_fallback(self) -> None:
        result = self.shot("--target", "tool:Blur", "--mode", "fit")

        self.assertEqual(result.returncode, 1, result.stderr)
        for detail in ("--mode pose", "--focus", "--radius", "parent"):
            self.assertIn(detail, result.stderr)
        self.assertEqual(len(self.target_queries()), 1)
        self.assertEqual(self.fake.methods("brp_extras/screenshot"), [])

    def test_rejected_fit_suggests_nearest_cached_ancestor_with_geometry(self) -> None:
        self.fake.named_entities = [
            {"entity": 101, "components": {NAME: "Leaf", CHILD_OF: 102}, "has": {AABB: False}},
            {"entity": 102, "components": {NAME: "Branch", CHILD_OF: 103}, "has": {AABB: False}},
            {"entity": 103, "components": {NAME: "Inner", CHILD_OF: 104}, "has": {AABB: True}},
            {"entity": 104, "components": {NAME: "Outer"}, "has": {AABB: True}},
        ]
        result = self.shot("--target", "name:Leaf", "--mode", "fit")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("--target entity:103", result.stderr)
        self.assertNotIn("--target entity:102", result.stderr)
        self.assertNotIn("--target entity:104", result.stderr)
        self.assertEqual(len(self.target_queries()), 1)
        self.assertTrue(all(request.get("entity") == CAMERA_ENTITY for request in self.fake.methods("world.get_components")))
        self.assertTrue(all("entity" not in request for request in self.fake.methods("world.list_components")))

    def test_rejected_stored_fit_gives_a_runnable_ad_hoc_pose_for_its_target(self) -> None:
        views_path = self.scratch / "views.toml"
        _ = views_path.write_text('[views.front]\ntarget = "tool:Blur"\nmode = "fit"\n')
        result = self.shot("--view", "front", "--views-file", str(views_path))

        self.assertEqual(result.returncode, 1, result.stderr)
        example = re.search(r"shot --target \S+ --mode pose --focus \S+ --radius [0-9.]+", result.stderr)
        self.assertIsNotNone(example, result.stderr)
        if example is not None:
            command = shlex.split(example.group())
            self.assertEqual(command[command.index("--target") + 1], "tool:Blur")
            repaired = self.run_script(
                *command, "--port", str(self.port), "--out", str(self.scratch / "repaired.png"),
            )
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertTrue((self.scratch / "repaired.png").is_file())
        self.assertRegex(result.stderr.lower(), r"set.*focus.*radius.*scene")

    def test_rejected_fit_does_not_suggest_an_ancestor_whose_aabb_was_removed(self) -> None:
        self.fake.named_entity_snapshots = [
            [
                {"entity": 101, "components": {NAME: "Leaf", CHILD_OF: 102}, "has": {AABB: False}},
                {"entity": 102, "components": {NAME: "Branch"}, "has": {AABB: True}},
            ],
            [
                {"entity": 101, "components": {NAME: "Leaf", CHILD_OF: 102}, "has": {AABB: False}},
                {"entity": 102, "components": {NAME: "Branch"}, "has": {AABB: False}},
            ],
        ]
        views_path = self.scratch / "views.toml"
        _ = views_path.write_text(
            '[views.before]\ntarget = "name:Branch"\nmode = "hana-frame"\n'
            + '[views.after]\ntarget = "name:Leaf"\nmode = "fit"\n',
        )
        result = self.run_script(
            "shot", "--port", str(self.port), "--out", str(self.scratch / "shots"),
            "--view", "all", "--views-file", str(views_path),
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("name:Leaf", result.stderr)
        self.assertIn("parent", result.stderr)
        self.assertNotIn("--target entity:102", result.stderr)
        self.assertEqual(len(self.target_queries()), 2)
        [line] = self.timing_lines()
        attempts = cast(list[dict[str, object]], line["attempts"])
        self.assertEqual([attempt["status"] for attempt in attempts], ["success", "failure"])


class FailureGuidanceTests(HanaShotTest):
    def in_process_shot(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = ["shot", "--port", str(self.port), "--out", str(self.scratch / "shot.png"), *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            with (
                mock.patch.dict(os.environ, self.script_environment(), clear=True),
                redirect_stdout(stdout), redirect_stderr(stderr),
            ):
                returncode = hana_shot.main(argv)
        finally:
            for number, handler in handlers.items():
                _ = signal.signal(number, handler)
        return subprocess.CompletedProcess(argv, returncode, stdout.getvalue(), stderr.getvalue())

    def assert_recorded_error(self, result: subprocess.CompletedProcess[str]) -> str:
        [line] = self.timing_lines()
        self.assertEqual(line["status"], "failure")
        error = cast(str, line["error"])
        self.assertIn(error, result.stderr)
        [attempt] = cast(list[dict[str, object]], line["attempts"])
        self.assertEqual(attempt["error"], error)
        return error

    def test_settle_timeout_names_retry_then_pose_recovery(self) -> None:
        settle = hana_shot.Session.settle

        def settle_with_short_limit(
            session: hana_shot.Session, before: hana_shot.CameraState, extra_frames: int,
        ) -> hana_shot.Settled:
            return settle(session, before, extra_frames, limit=0.01)

        with mock.patch.object(hana_shot.Session, "settle", settle_with_short_limit):
            result = self.in_process_shot("--target", "name:Log", "--mode", "fit", "--settle", "1000000")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        error = self.assert_recorded_error(result)
        self.assertIn("the camera did not settle within", error)
        self.assertRegex(error.lower(), r"(?:rerun|retry).*same command.*once")
        self.assertRegex(error.lower(), r"if.*(?:fails|times out).*again")
        for detail in ("hana_shot.py pose", "--mode pose", "--focus", "--radius"):
            self.assertIn(detail, error)
        self.assertRegex(error.lower(), r"(?:no|without|needs no).*fit move")
        self.assertEqual(self.fake.methods("brp_extras/screenshot"), [])

    def test_setup_missing_target_names_nearby_selectors_and_targets_command(self) -> None:
        candidates = ["Panel", "PanelLeft", "PanelRight", "PanelTop", "PanelBase", "PanelRear", "Unrelated"]
        self.fake.named_entities = [
            {"entity": 100 + index, "components": {NAME: name}, "has": {AABB: True}}
            for index, name in enumerate(candidates)
        ]
        for selector, expected in (("name:Panle", "Panel"), ("tool:Grayscal", "Grayscale")):
            with self.subTest(selector=selector):
                with mock.patch.object(hana_shot, "SETUP_LIMIT", 0.01):
                    result = self.in_process_shot("--target", selector, "--mode", "hana-frame",
                                                  "--setup", "scene::spawn")

                self.assertEqual(result.returncode, 1, result.stderr)
                line = self.timing_lines()[-1]
                error = cast(str, line["error"])
                self.assertIn(error, result.stderr)
                [attempt] = cast(list[dict[str, object]], line["attempts"])
                self.assertEqual(attempt["error"], error)
                self.assertRegex(error, r"setup.*scene::spawn.*ran but")
                self.assertIn(selector, error)
                self.assertIn("did not appear within", error)
                self.assertIn(expected, error)
                self.assertIn("hana_shot.py targets", error)
                self.assertRegex(error.lower(), r"list.*selectors.*resolve now")
                if selector.startswith("name:"):
                    suggested = [name for name in candidates if re.search(rf"\b{re.escape(name)}\b", error)]
                    self.assertGreater(len(suggested), 1)
                    self.assertLessEqual(len(suggested), 5)
                    self.assertNotIn("Unrelated", suggested)
        self.assertEqual(self.fake.methods("hana/invoke_command"), [{"command": "scene::spawn"}] * 2)
        self.assertEqual(self.fake.methods("brp_extras/screenshot"), [])

    def test_missing_crop_bounds_names_nearest_cached_parent_and_uncropped_flag(self) -> None:
        transform = face_transforms()["identity"]
        for has_ancestor in (True, False):
            with self.subTest(has_ancestor=has_ancestor):
                self.fake.named_entities = [
                    {"entity": 101, "components": {NAME: "Leaf", CHILD_OF: 102, GLOBAL_TRANSFORM: transform},
                     "has": {AABB: False}},
                    {"entity": 102, "components": {NAME: "Branch", CHILD_OF: 103, GLOBAL_TRANSFORM: transform},
                     "has": {AABB: False}},
                    {"entity": 103, "components": {NAME: "Inner", CHILD_OF: 104, GLOBAL_TRANSFORM: transform},
                     "has": {AABB: has_ancestor}},
                    {"entity": 104, "components": {NAME: "Outer", GLOBAL_TRANSFORM: transform},
                     "has": {AABB: has_ancestor}},
                ]
                result = self.shot("--target", "name:Leaf", "--mode", "hana-frame", "--crop", "entity")

                self.assertEqual(result.returncode, 1, result.stderr)
                line = self.timing_lines()[-1]
                error = cast(str, line["error"])
                self.assertIn(error, result.stderr)
                [attempt] = cast(list[dict[str, object]], line["attempts"])
                self.assertEqual(attempt["error"], error)
                self.assertIn("the target has no visible mesh bounds to crop to", error)
                self.assertIn("--crop none", error)
                if has_ancestor:
                    self.assertIn("--target entity:103", error)
                else:
                    self.assertNotIn("--target entity:103", error)
                self.assertNotIn("--target entity:102", error)
                self.assertNotIn("--target entity:104", error)
        self.assertEqual(self.fake.methods("brp_extras/screenshot"), [])


class ViewsTests(HanaShotTest):
    def test_signal_after_a_failed_view_preserves_its_reason_and_appends_signal_text(self) -> None:
        views_path = self.scratch / "views.toml"
        _ = views_path.write_text(
            '[views.missing]\ntarget = "name:Absent"\nmode = "hana-frame"\n'
            + '[views.held]\nmode = "home"\n',
        )
        self.fake.screenshot = "hold"
        process = self.start_script(
            "views", "check", "--port", str(self.port), "--views-file", str(views_path),
            "--out", str(self.scratch / "check"),
        )
        self.addCleanup(self.stop_script, process)
        self.assertTrue(self.fake.screenshot_started.wait(TEST_HANG_GUARD_SECS), "the second view never reached capture")
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)

        self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)
        [line] = self.timing_lines()
        self.assertEqual(line["exit_code"], 128 + signal.SIGTERM)
        self.assertEqual(line["failure_reason"], "no_target")
        error = cast(str, line["error"])
        self.assertIn("name:Absent", error)
        self.assertRegex(error.splitlines()[-1], r"SIGTERM$")
        [attempt] = cast(list[dict[str, object]], line["attempts"])
        self.assertEqual(attempt["failure_reason"], "no_target")

    def test_empty_registry_records_a_refused_invocation(self) -> None:
        _ = (self.scratch / "views.toml").write_text("")
        shot = self.run_script(
            "shot", "--port", str(self.port), "--view", "all", "--views-file", str(self.scratch / "views.toml"),
        )
        check = self.views("check", "--port", str(self.port))
        self.assertEqual((shot.returncode, check.returncode), (2, 2))
        self.assertEqual([line["failure_reason"] for line in self.timing_lines()], ["invalid_request"] * 2)
        self.assertEqual([line["invocation_kind"] for line in self.timing_lines()], ["shot", "views_check"])
        for line, result in zip(self.timing_lines(), (shot, check), strict=True):
            error = line["error"]
            self.assertIsInstance(error, str)
            self.assertTrue(error)
            self.assertIn(cast(str, error), result.stderr)

    def test_check_records_black_and_empty_crops_after_validation(self) -> None:
        _ = (self.scratch / "views.toml").write_text('[views.front]\nmode = "home"\n')
        fake_magick = self.scratch / "bin" / "magick"
        fake_magick.parent.mkdir()
        _ = fake_magick.write_text("#!/bin/sh\necho 0\n")
        fake_magick.chmod(0o755)
        self.fake.screenshot = "black"
        black = self.views("check", "--port", str(self.port), "--out", str(self.scratch / "check"))
        self.assertEqual(black.returncode, 1, black.stderr)
        self.assertEqual(self.timing_lines()[0]["failure_reason"], "black_capture")
        self.assertIn("black", cast(str, self.timing_lines()[0]["error"]).lower())

        _ = fake_magick.write_text("#!/bin/sh\necho 0.5\n")
        self.fake.screenshot = "tiny"
        empty = self.views("check", "--port", str(self.port), "--out", str(self.scratch / "check"))
        self.assertEqual(empty.returncode, 1, empty.stderr)
        self.assertEqual(self.timing_lines()[1]["failure_reason"], "empty_crop")
        self.assertIn(cast(str, self.timing_lines()[1]["error"]), empty.stderr)

        self.fake.screenshot = "ok"
        passed = self.views("check", "--port", str(self.port), "--out", str(self.scratch / "check"))
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(self.timing_lines()[2]["status"], "success")
        self.assertNotIn("error", self.timing_lines()[2])
        self.assertEqual([line["invocation_kind"] for line in self.timing_lines()], ["views_check"] * 3)

    def test_add_list_show_and_replace(self) -> None:
        added = self.views(
            "add", "log-back", "--target", "name:Log", "--yaw", "3.14159", "--margin", "0.2",
            "--crop", "entity", "--padding", "8", "--note", "log panel from behind",
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        document = tomllib.loads((self.scratch / "views.toml").read_text())
        self.assertEqual(document["views"], {"log-back": {
            "target": "name:Log", "mode": "fit", "yaw": 3.14159, "pitch": 0.0, "margin": 0.2,
            "crop": "entity", "padding": 8, "note": "log panel from behind",
        }})
        _ = self.views("add", "home", "--mode", "home")
        listed = self.views("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([line.split()[0] for line in listed.stdout.splitlines()], ["log-back", "home"])
        self.assertIn("never verified", listed.stdout)
        shown = self.views("show", "log-back")
        self.assertTrue(shown.stdout.startswith("[views.log-back]\n"))
        self.assertRegex(shown.stdout, r'(?m)^target += "name:Log"$')
        again = self.views("add", "log-back", "--target", "name:Log")
        self.assertEqual(again.returncode, 2)
        self.assertIn("pass --replace", again.stderr)
        replaced = self.views("add", "log-back", "--target", "name:Log", "--margin", "0.3", "--replace")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertRegex(self.views("show", "log-back").stdout, r"(?m)^margin += 0\.3$")
        self.assertEqual(self.fake.calls, [])

    def test_refused_view_name_suggests_a_valid_name_derived_from_input(self) -> None:
        for refused in ("Log Back", "42 View", "!!!"):
            with self.subTest(refused=refused):
                result = self.views("add", refused, "--mode", "home")
                self.assertEqual(result.returncode, 2, result.stderr)
                examples = [match.group(1) for match in re.finditer(r"'([a-z0-9][a-z0-9-]*)'", result.stderr)]
                self.assertTrue(examples, result.stderr)
                example = examples[0]
                if refused == "Log Back":
                    self.assertIn("log", example)
                    self.assertIn("back", example)
                elif refused == "42 View":
                    self.assertIn("42", example)
                    self.assertIn("view", example)
                repaired = self.views("add", example, "--mode", "home")
                self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(self.fake.calls, [])

    def test_invalid_registry_view_name_has_a_derived_example(self) -> None:
        _ = (self.scratch / "views.toml").write_text('[views."Log Back"]\nmode = "home"\n')
        result = self.views("list")

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertRegex(result.stderr, r"log[-_]back")
        self.assertEqual(self.fake.calls, [])

    def test_bad_views_are_refused(self) -> None:
        for argv in (
            ["add", "Log-Back", "--target", "name:Log"],
            ["add", "pose-only", "--mode", "pose"],
            ["add", "wide", "--target", "name:Log", "--margin", "0.9"],
            ["add", "fit-alone", "--mode", "fit"],
            ["show", "absent"],
        ):
            with self.subTest(argv=argv[1]):
                self.assertEqual(self.views(*argv).returncode, 2)
        _ = (self.scratch / "views.toml").write_text('[views.front]\ntarget = "name:Log"\nzoom = 2\n')
        unknown = self.views("list")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("unknown keys: zoom", unknown.stderr)


class ShotTests(HanaShotTest):
    def test_home_shot_writes_the_png_and_one_timing_line(self) -> None:
        result = self.shot("--mode", "home")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.scratch / "shot.png"))
        self.assertTrue((self.scratch / "shot.png").read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(self.fake.methods("hana/invoke_command"), [{"command": "camera::reset_home"}])
        durations = [params["value"] for params in self.fake.methods("world.mutate_resources")]
        self.assertEqual(durations, [0, 2000], "home runs at duration 0, then restores Hana's own")
        lines = self.timings.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        line = cast(TimingLine, json.loads(lines[0]))
        self.assertEqual(
            (line["port"], line["mode"], line["crop"], line["window"], line["width"], line["height"]),
            (self.port, "home", "none", "1280x720", *SHOT_SIZE),
        )
        self.assertGreaterEqual(line["total_ms"], line["capture_ms"])
        self.assertEqual((line["status"], line["exit_code"]), ("success", 0))
        self.assertNotIn("error", line)
        self.assertNotIn("face", line)
        self.assertEqual(line["session"], {"state": "absent"})
        [attempt] = line["attempts"]
        self.assertEqual((attempt["status"], attempt["label"], attempt["view"]), ("success", "home", None))
        self.assertNotIn("face", attempt)
        self.assertEqual(attempt["image_paths"], [str(self.scratch / "shot.png")])
        self.assertEqual([path.name for path in self.scratch.glob(".shot-*")], [])

    def test_margin_help_explains_range_and_fraction(self) -> None:
        result = self.run_script("shot", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = " ".join(result.stdout.split())
        margin_help = help_text.split("--margin MARGIN", 2)[-1].split("--", 1)[0]
        self.assertIn("0 to 0.45", margin_help)
        self.assertRegex(margin_help.lower(), r"fraction|proportion|percent")
        self.assertRegex(margin_help.lower(), r"frame|image|viewport|screen")

    def test_margin_refusal_explains_range_and_viewport_fraction_before_any_brp_call(self) -> None:
        for margin in ("-0.1", "0.46"):
            with self.subTest(margin=margin):
                result = self.shot("--target", "name:Log", "--margin", margin)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--margin", result.stderr)
                self.assertIn("0", result.stderr)
                self.assertIn("0.45", result.stderr)
                self.assertRegex(result.stderr.lower(), r"fraction|proportion|percent")
                self.assertIn("viewport", result.stderr.lower())
                self.assertIn("each side", result.stderr.lower())
                self.assertRegex(result.stderr, r"0\.1\s*=\s*10%")
        self.assertEqual(self.fake.calls, [])

    def test_unhandled_filesystem_failure_records_its_exception_type_and_diagnostic(self) -> None:
        result = self.shot("--view", "front", "--views-file", str(self.scratch))

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Traceback", result.stderr)
        diagnostic = next(line for line in result.stderr.splitlines() if line.startswith("IsADirectoryError:"))
        [line] = self.timing_lines()
        self.assertEqual((line["status"], line["exit_code"], line["failure_reason"]),
                         ("failure", 1, "shot_failed"))
        self.assertIn(diagnostic, cast(str, line["error"]))
        self.assertEqual(self.fake.calls, [])

    def test_pose_refusal_names_missing_flags_and_has_a_runnable_example(self) -> None:
        result = self.shot("--mode", "pose")

        self.assertEqual(result.returncode, 2, result.stderr)
        for flag in ("--focus", "--radius"):
            self.assertIn(flag, result.stderr)
        example = re.search(r"--mode pose --focus (\S+) --radius ([0-9.]+)", result.stderr)
        self.assertIsNotNone(example, result.stderr)
        if example is not None:
            repaired = self.shot(*shlex.split(example.group()))
            self.assertEqual(repaired.returncode, 0, repaired.stderr)

    def test_window_uses_override_scale_when_it_is_higher_than_base_scale(self) -> None:
        self.fake.window_scale_factor = 1.0
        self.fake.window_scale_factor_override = 2.0

        result = self.shot("--mode", "home", "--window", "1280x720")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(self.fake.methods("world.mutate_components"), [
            {"entity": WINDOW_ENTITY, "component": WINDOW,
             "path": ".resolution.physical_width", "value": 2560},
            {"entity": WINDOW_ENTITY, "component": WINDOW,
             "path": ".resolution.physical_height", "value": 1440},
        ])

    def test_window_uses_override_scale_when_it_is_lower_than_base_scale(self) -> None:
        self.fake.window_scale_factor = 2.0
        self.fake.window_scale_factor_override = 1.0

        result = self.shot("--mode", "home", "--window", "1280x720")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fake.methods("world.mutate_components"), [])

    def test_window_uses_base_scale_when_override_is_absent(self) -> None:
        self.fake.window_scale_factor = 2.0

        result = self.shot("--mode", "home", "--window", "1280x720")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(self.fake.methods("world.mutate_components"), [
            {"entity": WINDOW_ENTITY, "component": WINDOW,
             "path": ".resolution.physical_width", "value": 2560},
            {"entity": WINDOW_ENTITY, "component": WINDOW,
             "path": ".resolution.physical_height", "value": 1440},
        ])

    def test_session_evidence_uses_codex_thread_before_claude_code_session(self) -> None:
        cases: tuple[tuple[dict[str, str], dict[str, str]], ...] = (
            ({"CLAUDE_CODE_SESSION_ID": "claude"}, {"state": "present", "value": "claude"}),
            ({"CODEX_THREAD_ID": "codex"}, {"state": "present", "value": "codex"}),
            ({"CLAUDE_CODE_SESSION_ID": "claude", "CODEX_THREAD_ID": "codex"},
             {"state": "present", "value": "codex"}),
            ({}, {"state": "absent"}),
            ({"CLAUDE_SESSION_ID": "old-claude", "CODEX_SESSION_ID": "old-codex"}, {"state": "absent"}),
        )
        for variables, expected in cases:
            with self.subTest(variables=variables):
                result = self.run_script(
                    "shot", "--port", str(self.port), "--out", str(self.scratch / "shot.png"),
                    "--mode", "home", session_variables=variables,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.timing_lines()[-1]["session"], expected)

    def test_timed_out_screenshot_is_never_resent(self) -> None:
        self.fake.screenshot = "hang"
        result = self.shot("--mode", "home")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("without resending", result.stderr)
        self.assertEqual(len(self.fake.methods("brp_extras/screenshot")), 1)
        self.assertFalse((self.scratch / "shot.png").exists())
        [line] = self.timing_lines()
        self.assertEqual((line["exit_code"], line["failure_reason"]), (3, "timeout"))
        self.assertIn("without resending", cast(str, line["error"]))
        self.assertIn(cast(str, line["error"]), result.stderr)
        self.assertEqual(cast(list[dict[str, object]], line["attempts"])[0]["image_paths"], [])

    def test_busy_screenshot_is_resent_once(self) -> None:
        self.fake.screenshot = "busy-once"
        result = self.shot("--mode", "home")
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = self.fake.methods("brp_extras/screenshot")
        self.assertEqual(len(sent), 2)
        self.assertTrue((self.scratch / "shot.png").exists())

    def test_screenshot_busy_past_the_resend_limit_fails(self) -> None:
        self.fake.screenshot = "busy-always"
        result = self.shot("--mode", "home")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already in progress", result.stderr)
        self.assertEqual(self.timing_lines()[0]["failure_reason"], "already_in_progress")

    def test_failed_capture_causes_have_one_record_each(self) -> None:
        for mode, reason in (("error", "brp_error"), ("invalid", "invalid_png")):
            with self.subTest(mode=mode):
                self.fake.screenshot = mode
                result = self.shot("--mode", "home")
                self.assertEqual(result.returncode, 1, result.stderr)
                line = self.timing_lines()[-1]
                self.assertEqual((line["status"], line["failure_reason"]), ("failure", reason))
                error = line["error"]
                self.assertIsInstance(error, str)
                self.assertTrue(error)
                self.assertIn(cast(str, error), result.stderr)

    def test_missing_target_and_invalid_request_are_recorded(self) -> None:
        missing = self.shot("--target", "name:Absent")
        refused = self.shot("--mode", "pose")
        self.assertEqual((missing.returncode, refused.returncode), (1, 2))
        self.assertEqual([line["failure_reason"] for line in self.timing_lines()], ["no_target", "invalid_request"])
        missing_line, refused_line = self.timing_lines()
        self.assertIn("name:Absent", cast(str, missing_line["error"]))
        self.assertIn(cast(str, missing_line["error"]), missing.stderr)
        self.assertIn("--focus", cast(str, refused_line["error"]))
        self.assertIn(cast(str, refused_line["error"]), refused.stderr)

    def test_parser_error_is_recorded(self) -> None:
        result = self.run_script("shot", "--port", str(self.port), "--mode")
        self.assertEqual(result.returncode, 2, result.stderr)
        [line] = self.timing_lines()
        self.assertEqual((line["port"], line["failure_reason"], line["exit_code"]),
                         (self.port, "invalid_request", 2))
        self.assertIn("--mode", cast(str, line["error"]))
        self.assertIn(cast(str, line["error"]), result.stderr)
        for key in ("view", "target", "mode"):
            self.assertNotIn(key, line)

    def test_failed_invocation_records_the_parsed_shot_fields(self) -> None:
        views = self.scratch / "views.toml"
        _ = views.write_text('[views.missing]\ntarget = "name:Absent"\nmode = "hana-frame"\n')
        self.fake.screenshot = "error"
        for flags, expected in (
            (("--view", "missing", "--views-file", str(views)), ("missing", "name:Absent", "hana-frame")),
            (("--target", "name:Absent", "--mode", "fit"), (None, "name:Absent", "fit")),
            (("--mode", "home"), (None, None, "home")),
        ):
            with self.subTest(flags=flags):
                result = self.shot(*flags)

                self.assertEqual(result.returncode, 1, result.stderr)
                line = self.timing_lines()[-1]
                self.assertEqual(line["status"], "failure")
                self.assertEqual((line["view"], line["target"], line["mode"]), expected)

    def test_multi_view_failure_keeps_earlier_attempt(self) -> None:
        views = self.scratch / "views.toml"
        _ = views.write_text('[views.first]\nmode = "home"\n[views.second]\ntarget = "name:Absent"\n')
        result = self.run_script("shot", "--port", str(self.port), "--view", "all", "--views-file", str(views))
        self.assertEqual(result.returncode, 1, result.stderr)
        [line] = self.timing_lines()
        self.assertEqual(line["failure_reason"], "no_target")
        self.assertIn("name:Absent", cast(str, line["error"]))
        self.assertIn(cast(str, line["error"]), result.stderr)
        attempts = cast(list[dict[str, object]], line["attempts"])
        self.assertEqual([attempt["status"] for attempt in attempts], ["success", "failure"])
        self.assertEqual(len(cast(list[str], attempts[0]["image_paths"])), 1)
        self.assertEqual(attempts[0]["mode"], "home")
        stats = self.run_script("stats")
        self.assertIn("1 shots since", stats.stdout)
        self.assertIn("failure no_target: 1", stats.stdout)

    def test_stats_counts_each_view_and_legacy_shot_once(self) -> None:
        views = self.scratch / "views.toml"
        _ = views.write_text('[views.first]\nmode = "home"\n[views.second]\ntarget = "name:Log"\nmode = "hana-frame"\n')
        shot = self.run_script("shot", "--port", str(self.port), "--view", "all", "--views-file", str(views))
        self.assertEqual(shot.returncode, 0, shot.stderr)
        [line] = self.timing_lines()
        attempts = cast(list[dict[str, object]], line["attempts"])
        self.assertEqual([attempt["mode"] for attempt in attempts], ["home", "hana-frame"])
        self.assertTrue(all("total_ms" in attempt for attempt in attempts))
        legacy = {key: value for key, value in line.items() if key not in ("status", "exit_code", "session", "attempts")}
        with self.timings.open("a") as handle:
            _ = handle.write(json.dumps(legacy) + "\n")
        stats = self.run_script("stats")
        self.assertEqual(stats.returncode, 0, stats.stderr)
        rows = {row.split()[0]: row.split() for row in stats.stdout.splitlines()[2:]}
        self.assertEqual(rows["all"][1], "3")
        self.assertEqual(rows["home/none"][1], "1")
        self.assertEqual(rows["hana-frame/none"][1], "2")

    def test_failed_attempt_paths_require_a_new_file(self) -> None:
        output = self.scratch / "shot.png"
        _ = output.write_bytes(png(*SHOT_SIZE))
        old = 1_600_000_000_000_000_000
        os.utime(output, ns=(old, old))
        self.fake.screenshot = "error"
        failed = self.shot("--mode", "home")
        self.assertEqual(failed.returncode, 1, failed.stderr)
        first_attempts = cast(list[dict[str, object]], self.timing_lines()[0]["attempts"])
        self.assertEqual(first_attempts[0]["image_paths"], [])

        _ = (self.scratch / "views.toml").write_text('[views.front]\nmode = "home"\n')
        fake_magick = self.scratch / "bin" / "magick"
        fake_magick.parent.mkdir()
        _ = fake_magick.write_text("#!/bin/sh\necho 0\n")
        fake_magick.chmod(0o755)
        self.fake.screenshot = "black"
        black = self.views("check", "--port", str(self.port), "--out", str(self.scratch / "check"))
        self.assertEqual(black.returncode, 1, black.stderr)
        second_attempts = cast(list[dict[str, object]], self.timing_lines()[1]["attempts"])
        self.assertEqual(second_attempts[0]["image_paths"], [str(self.scratch / "check" / "front.png")])

    def test_timing_write_failure_keeps_call_outcome(self) -> None:
        _ = (self.scratch / "cache").write_text("occupied")
        success = self.shot("--mode", "home")
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(success.stdout.strip(), str(self.scratch / "shot.png"))
        self.assertEqual(sum("could not write timing record" in line for line in success.stderr.splitlines()), 1)
        self.assertIn("Not a directory", success.stderr)
        self.assertNotIn("Traceback", success.stderr)

        self.fake.screenshot = "error"
        failure = self.shot("--mode", "home")
        self.assertEqual(failure.returncode, 1, failure.stderr)
        self.assertEqual(failure.stdout, "")
        self.assertIn("camera rejected capture", failure.stderr)
        self.assertEqual(sum("could not write timing record" in line for line in failure.stderr.splitlines()), 1)
        self.assertIn("Not a directory", failure.stderr)
        self.assertNotIn("Traceback", failure.stderr)

    def test_no_app_is_recorded(self) -> None:
        result = self.run_script("shot", "--port", "65001", "--mode", "home")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.timing_lines()[0]["failure_reason"], "no_app")


class RemoteTests(HanaShotTest):
    def fake_remote(self, copies: bool, system: str = "Linux") -> Path:
        """Point the fake Hana's writes at a folder standing in for the remote host, and fake scp and ssh."""
        remote = self.scratch / "remote"
        remote.mkdir()
        self.fake.remote = remote
        bin_directory = self.scratch / "bin"
        bin_directory.mkdir()
        source = FAKE_REMOTE.format(
            python=sys.executable, remote=str(remote), log=str(self.ssh_log), copies=copies, system=system,
        )
        for name in ("scp", "ssh"):
            fake = bin_directory / name
            _ = fake.write_text(source)
            fake.chmod(0o755)
        return remote

    @property
    def ssh_log(self) -> Path:
        return self.scratch / "ssh.jsonl"

    def ssh_calls(self) -> list[list[str]]:
        if not self.ssh_log.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.ssh_log.read_text().splitlines()]

    def hana_shot_driver(self, name: str, setup: str) -> Path:
        driver = self.scratch / name
        _ = driver.write_text("\n".join([
            "import os",
            "import signal",
            "import sys",
            f"sys.path.insert(0, {str(SCRIPT.parent.resolve())!r})",
            "import hana_shot",
            "",
            setup,
            "",
            "raise SystemExit(hana_shot.main(sys.argv[1:]))",
            "",
        ]))
        return driver

    def interrupt_held_remote_mac_shot(self, signal_number: int, program: Path = SCRIPT) -> tuple[int, str]:
        _ = self.fake_remote(copies=True, system="Darwin")
        self.fake.window_level = "AlwaysOnBottom"
        self.fake.screenshot = "hold"
        process = self.start_python(
            program,
            "shot", "--port", str(self.port), "--out", str(self.scratch / "shot.png"),
            "--mode", "home", "--remote", REMOTE_HOST,
        )
        self.addCleanup(self.stop_script, process)
        self.assertTrue(self.fake.screenshot_started.wait(TEST_HANG_GUARD_SECS), "the screenshot call never reached the fake")
        process.send_signal(signal_number)
        _, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        return cast(int, process.returncode), stderr

    def test_remote_shot_copies_the_png_back_and_removes_it_there(self) -> None:
        remote = self.fake_remote(copies=True)
        result = self.shot("--mode", "home", "--remote", REMOTE_HOST)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.scratch / "shot.png").read_bytes().startswith(b"\x89PNG"))
        [screenshot] = self.fake.methods("brp_extras/screenshot")
        remote_path = cast(str, screenshot["path"])
        self.assertRegex(remote_path, r"^/tmp/hana_shot_[0-9]+_1\.png$")
        [uname, copy, removal] = self.ssh_calls()
        self.assertEqual(uname, ["ssh", REMOTE_HOST, "uname", "-s"])
        self.assertEqual(copy[:3], ["scp", "-q", f"{REMOTE_HOST}:{remote_path}"])
        self.assertEqual(Path(copy[3]).parent, self.scratch)
        self.assertEqual(self.fake.methods("world.mutate_components"), [], "a Linux host keeps its window level")
        self.assertEqual(removal, ["ssh", REMOTE_HOST, "rm", "-f", remote_path])
        self.assertEqual(list(remote.iterdir()), [])
        self.assertEqual([path.name for path in self.scratch.glob(".shot-*")], [])
        line = cast(dict[str, object], json.loads(self.timings.read_text()))
        self.assertEqual(line["host"], "mac")

    def test_remote_copy_that_leaves_no_file_fails(self) -> None:
        remote = self.fake_remote(copies=False)
        result = self.shot("--mode", "home", "--remote", REMOTE_HOST)
        self.assertEqual(result.returncode, 1, result.stderr)
        [screenshot] = self.fake.methods("brp_extras/screenshot")
        self.assertIn(f"no PNG came back from {REMOTE_HOST}:{screenshot['path']}", result.stderr)
        self.assertEqual(
            [call[:3] for call in self.ssh_calls()],
            [["ssh", REMOTE_HOST, "uname"], ["scp", "-q", f"{REMOTE_HOST}:{screenshot['path']}"]],
            "the remote shot is kept",
        )
        self.assertEqual(len(list(remote.iterdir())), 1)
        self.assertFalse((self.scratch / "shot.png").exists())
        self.assertEqual(self.timing_lines()[0]["failure_reason"], "copy_failed")

    def test_remote_mac_shot_raises_the_window_and_holds_the_display_awake_for_the_run(self) -> None:
        _ = self.fake_remote(copies=True, system="Darwin")
        result = self.shot("--mode", "home", "--remote", REMOTE_HOST)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fake.methods("world.mutate_components"), [
            {"entity": WINDOW_ENTITY, "component": WINDOW, "path": ".window_level", "value": "AlwaysOnTop"},
            {"entity": WINDOW_ENTITY, "component": WINDOW, "path": ".window_level", "value": "Normal"},
        ])
        self.assertEqual(self.fake.window_level, "Normal")
        calls = self.ssh_calls()
        uname, awake, copy = calls[:3]
        self.assertEqual(uname, ["ssh", REMOTE_HOST, "uname", "-s"])
        self.assertEqual(awake[:2], ["ssh", REMOTE_HOST])
        self.assertTrue(awake[2].startswith("caffeinate -u "), awake)
        self.assertEqual(copy[0], "scp", "the display is awake before the shot")
        self.assertIn(["ended"], calls, "the keep-awake ends before the run exits")

    def test_remote_mac_shot_restores_an_always_on_bottom_window(self) -> None:
        _ = self.fake_remote(copies=True, system="Darwin")
        self.fake.window_level = "AlwaysOnBottom"

        result = self.shot("--mode", "home", "--remote", REMOTE_HOST)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["value"] for call in self.fake.methods("world.mutate_components")], [
            "AlwaysOnTop", "AlwaysOnBottom",
        ])
        self.assertEqual(self.fake.window_level, "AlwaysOnBottom")
        self.assertIn(["ended"], self.ssh_calls())

    def test_remote_mac_shot_repairs_an_already_always_on_top_window_to_normal(self) -> None:
        _ = self.fake_remote(copies=True, system="Darwin")
        self.fake.window_level = "AlwaysOnTop"

        result = self.shot("--mode", "home", "--remote", REMOTE_HOST)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["value"] for call in self.fake.methods("world.mutate_components")], [
            "AlwaysOnTop", "Normal",
        ])
        self.assertEqual(self.fake.window_level, "Normal")
        self.assertIn(["ended"], self.ssh_calls())

    def test_failed_remote_mac_shot_restores_the_window_and_ends_keep_awake(self) -> None:
        _ = self.fake_remote(copies=False, system="Darwin")
        self.fake.window_level = "AlwaysOnBottom"

        result = self.shot("--mode", "home", "--remote", REMOTE_HOST)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([call["value"] for call in self.fake.methods("world.mutate_components")], [
            "AlwaysOnTop", "AlwaysOnBottom",
        ])
        self.assertEqual(self.fake.window_level, "AlwaysOnBottom")
        self.assertIn(["ended"], self.ssh_calls())

    def test_signal_during_close_does_not_interrupt_window_restore(self) -> None:
        _ = self.fake_remote(copies=True, system="Darwin")
        self.fake.window_level = "AlwaysOnBottom"
        driver = self.hana_shot_driver("signal_during_restore.py", """\
real_mutate = hana_shot.Brp.mutate
def signal_during_restore(brp, entity, component, path, value, *args, **kwargs):
    if path == ".window_level" and value != "AlwaysOnTop":
        os.kill(os.getpid(), signal.SIGTERM)
    return real_mutate(brp, entity, component, path, value, *args, **kwargs)
hana_shot.Brp.mutate = signal_during_restore
""")

        result = self.run_python(
            driver,
            "shot", "--port", str(self.port), "--out", str(self.scratch / "shot.png"),
            "--mode", "home", "--remote", REMOTE_HOST,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["value"] for call in self.fake.methods("world.mutate_components")], [
            "AlwaysOnTop", "AlwaysOnBottom",
        ])
        self.assertEqual(self.fake.window_level, "AlwaysOnBottom")
        self.assertIn(["ended"], self.ssh_calls())
        [line] = self.timing_lines()
        self.assertEqual(line["exit_code"], 0)

    def test_second_signal_during_unwinding_keeps_the_first_exit_and_closes_the_session(self) -> None:
        driver = self.hana_shot_driver("second_signal_during_close.py", """\
real_close = hana_shot.Session.close
def signal_before_close(session):
    os.kill(os.getpid(), signal.SIGHUP)
    return real_close(session)
hana_shot.Session.close = signal_before_close
""")

        return_code, stderr = self.interrupt_held_remote_mac_shot(signal.SIGTERM, driver)

        self.assertEqual(return_code, 128 + signal.SIGTERM, stderr)
        self.assertEqual([call["value"] for call in self.fake.methods("world.mutate_components")], [
            "AlwaysOnTop", "AlwaysOnBottom",
        ])
        self.assertEqual(self.fake.window_level, "AlwaysOnBottom")
        self.assertIn(["ended"], self.ssh_calls())
        [line] = self.timing_lines()
        self.assertEqual(line["exit_code"], 128 + signal.SIGTERM)

    def test_sigint_restores_the_remote_mac_window_and_records_the_signal_exit(self) -> None:
        return_code, stderr = self.interrupt_held_remote_mac_shot(signal.SIGINT)

        self.assertEqual(return_code, 128 + signal.SIGINT, stderr)
        self.assertNotIn("KeyboardInterrupt", stderr)
        self.assertEqual(self.fake.window_level, "AlwaysOnBottom")
        self.assertIn(["ended"], self.ssh_calls())
        [line] = self.timing_lines()
        self.assertEqual((line["invocation_kind"], line["exit_code"]), ("shot", 128 + signal.SIGINT))
        self.assertIn("SIGINT", cast(str, line["error"]))

    def test_sigterm_restores_the_remote_mac_window_and_records_the_signal_exit(self) -> None:
        return_code, stderr = self.interrupt_held_remote_mac_shot(signal.SIGTERM)

        self.assertEqual(return_code, 128 + signal.SIGTERM, stderr)
        self.assertEqual(self.fake.window_level, "AlwaysOnBottom")
        self.assertIn(["ended"], self.ssh_calls())
        [line] = self.timing_lines()
        self.assertEqual((line["invocation_kind"], line["exit_code"]), ("shot", 128 + signal.SIGTERM))
        self.assertIn("SIGTERM", cast(str, line["error"]))

    def test_sighup_restores_the_remote_mac_window_and_records_the_signal_exit(self) -> None:
        return_code, stderr = self.interrupt_held_remote_mac_shot(signal.SIGHUP)

        self.assertEqual(return_code, 128 + signal.SIGHUP, stderr)
        self.assertEqual(self.fake.window_level, "AlwaysOnBottom")
        self.assertIn(["ended"], self.ssh_calls())
        [line] = self.timing_lines()
        self.assertEqual((line["invocation_kind"], line["exit_code"]), ("shot", 128 + signal.SIGHUP))
        self.assertIn("SIGHUP", cast(str, line["error"]))

    def test_views_check_retries_window_preparation_after_a_refused_raise(self) -> None:
        _ = self.fake_remote(copies=True, system="Darwin")
        self.fake.window_level = "AlwaysOnBottom"
        self.fake.refuse_window_level_raise_once = True
        _ = (self.scratch / "views.toml").write_text(
            '[views.first]\nmode = "home"\n[views.second]\nmode = "home"\n',
        )
        fake_magick = self.scratch / "bin" / "magick"
        _ = fake_magick.write_text("#!/bin/sh\necho 0.5\n")
        fake_magick.chmod(0o755)

        result = self.views(
            "check", "--port", str(self.port), "--out", str(self.scratch / "check"),
            "--remote", REMOTE_HOST,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("FAIL first:", result.stderr)
        self.assertIn("raise refused once", result.stderr)
        self.assertIn("PASS second", result.stdout)
        self.assertEqual([call["value"] for call in self.fake.methods("world.mutate_components")], [
            "AlwaysOnTop", "AlwaysOnBottom", "AlwaysOnTop", "AlwaysOnBottom",
        ])
        self.assertEqual(len(self.fake.methods("brp_extras/screenshot")), 1)
        self.assertEqual(self.fake.window_level, "AlwaysOnBottom")
        calls = self.ssh_calls()
        awake = next(index for index, call in enumerate(calls) if call[:2] == ["ssh", REMOTE_HOST]
                     and len(call) > 2 and call[2].startswith("caffeinate -u "))
        copy = next(index for index, call in enumerate(calls) if call[:1] == ["scp"])
        self.assertLess(awake, copy, "the second shot starts only after keep-awake is running")
        self.assertIn(["ended"], calls)

    def test_refused_window_restore_reports_once_without_changing_a_success_exit(self) -> None:
        _ = self.fake_remote(copies=True, system="Darwin")
        self.fake.refuse_window_level_restore = True

        result = self.shot("--mode", "home", "--remote", REMOTE_HOST)

        self.assertEqual(result.returncode, 0)
        restore_errors = [
            line for line in result.stderr.splitlines()
            if line.startswith("hana_shot: could not put the window level back")
        ]
        self.assertEqual(restore_errors, [
            "hana_shot: could not put the window level back to Normal: "
            + "world.mutate_components failed (-32603): restore refused",
        ])
        self.assertEqual(self.fake.window_level, "AlwaysOnTop")
        self.assertIn(["ended"], self.ssh_calls())

    def test_remote_host_that_gives_no_system_fails_before_any_shot(self) -> None:
        _ = self.fake_remote(copies=True, system="")
        result = self.shot("--mode", "home", "--remote", REMOTE_HOST)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"ssh {REMOTE_HOST} uname -s gave no answer", result.stderr)
        self.assertEqual(self.fake.methods("brp_extras/screenshot"), [])

    def test_launch_and_shutdown_are_refused_with_remote(self) -> None:
        for flag in ("--launch", "--shutdown"):
            with self.subTest(flag=flag):
                result = self.shot("--mode", "home", "--remote", REMOTE_HOST, flag)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("this machine only", result.stderr)
        self.assertEqual(self.fake.calls, [])


class PoseTests(HanaShotTest):
    def test_pose_set_keeps_every_value_it_is_not_given(self) -> None:
        result = self.run_script("pose", "set", "--port", str(self.port), "--yaw", "0.5")
        self.assertEqual(result.returncode, 0, result.stderr)
        [mutation] = self.fake.methods("world.mutate_components")
        orbit_cam = cast(dict[str, dict[str, object]], mutation["value"])
        self.assertEqual(orbit_cam["orbit"]["current"], {"yaw": 0.5, "pitch": 0.3})
        self.assertEqual((orbit_cam["pan"]["current"], orbit_cam["zoom"]["current"]), ([0.0, 1.0, 0.0], 6.0))
        self.assertEqual(self.run_script("pose", "set", "--port", str(self.port)).returncode, 2)


class StatsTests(HanaShotTest):
    def test_failures_by_reason_and_bad_lines_are_skipped(self) -> None:
        self.timings.parent.mkdir(parents=True)
        _ = self.timings.write_text(
            '\n'.join([
                '{"time":"2026-10-03T10:00:00+00:00","status":"failure","exit_code":3,"failure_reason":"timeout"}',
                '{"time":"2026-10-03T11:00:00+00:00","status":"failure","exit_code":1,"failure_reason":"no_app"}',
                '{"time":"2026-10-03T12:00:00+00:00","status":"failure","exit_code":3,"failure_reason":"timeout"}',
                '{bad}',
            ]) + '\n'
        )
        result = self.run_script("stats")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("failure no_app: 1", result.stdout)
        self.assertIn("failure timeout: 2", result.stdout)

    def write_timings(self, *rows: tuple[str, str, str, float]) -> None:
        self.timings.parent.mkdir(parents=True)
        records = [
            {
                "time": time, "host": "test", "sha": "abc", "port": 15791, "label": mode, "view": None,
                "target": None, "mode": mode, "crop": crop, "window": "1280x720", "frame_ms": 8.0,
                "resolve_ms": 1.0, "move_ms": 0.0, "settle_ms": total / 2, "settle_frames": 3,
                "capture_ms": total / 2, "crop_ms": 0.0, "total_ms": total, "width": 4, "height": 3, "bytes": 90,
            }
            for time, mode, crop, total in rows
        ]
        _ = self.timings.write_text("".join(json.dumps(record) + "\n" for record in records))

    def test_medians_per_mode_and_crop(self) -> None:
        self.write_timings(
            ("2026-09-30T10:00:00.000+00:00", "home", "none", 900.0),
            ("2026-10-02T10:00:00.000+00:00", "home", "none", 100.0),
            ("2026-10-02T11:00:00.000+00:00", "home", "none", 300.0),
            ("2026-10-03T10:00:00.000+00:00", "fit", "entity", 150.0),
        )
        everything = self.run_script("stats")
        self.assertEqual(everything.returncode, 0, everything.stderr)
        rows = {line.split()[0]: line.split() for line in everything.stdout.splitlines()[2:]}
        self.assertEqual(rows["all"][1], "4")
        self.assertEqual(rows["home/none"][1:3], ["3", "1.0"])
        self.assertEqual(rows["home/none"][-1], "300.0")
        recent = self.run_script("stats", "--since", "2026-10-01")
        self.assertTrue(recent.stdout.startswith("3 shots since 2026-10-02"))
        rows = {line.split()[0]: line.split() for line in recent.stdout.splitlines()[2:]}
        self.assertEqual(rows["home/none"][-1], "200.0")
        self.assertEqual(rows["fit/entity"][-1], "150.0")
        self.assertIn("no shots in that span", self.run_script("stats", "--since", "2027-01-01").stdout)

    def test_no_timing_log_yet(self) -> None:
        result = self.run_script("stats")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no timings yet", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
