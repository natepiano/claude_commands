"""hana_shot run as a subprocess against a stdlib fake BRP server: one still camera, a few entities and tools."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import tomllib
from typing import NotRequired, TypedDict, cast, override
import unittest
import zlib


SCRIPT = Path(__file__).with_name("hana_shot.py")
CAMERA_ENTITY = 5
WINDOW_ENTITY = 3
NAME = "bevy_ecs::name::Name"
AABB = "bevy_camera::primitives::Aabb"
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


def camera_components(physical_size: tuple[int, int] = (1280, 720), scale_factor: float = 1.0) -> dict[str, object]:
    angles = {"yaw": 0.0, "pitch": 0.3}
    return {
        "hana_lagrange::orbit_cam::OrbitCam": {
            "orbit": {"current": angles, "target": angles},
            "pan": {"current": [0.0, 1.0, 0.0], "target": [0.0, 1.0, 0.0]},
            "zoom": {"current": 6.0, "target": 6.0},
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
        self.frame: int = 0
        self.screenshot: str = "ok"
        self.remote: Path | None = None
        self.window_physical_size: tuple[int, int] = (1280, 720)
        self.window_scale_factor: float = 1.0
        self.window_scale_factor_override: float | None = None
        self.release: threading.Event = threading.Event()
        self.lock: threading.Lock = threading.Lock()

    def methods(self, method: str) -> list[dict[str, object]]:
        with self.lock:
            return [call["params"] for call in self.calls if call["method"] == method]

    def answer(self, method: str, params: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.calls.append({"method": method, "params": params})
            screenshots = sum(1 for call in self.calls if call["method"] == "brp_extras/screenshot")
            if method == "world.mutate_components" and params.get("component") == WINDOW:
                width, height = self.window_physical_size
                if params.get("path") == ".resolution.physical_width":
                    self.window_physical_size = (cast(int, params["value"]), height)
                elif params.get("path") == ".resolution.physical_height":
                    self.window_physical_size = (width, cast(int, params["value"]))
            if method == "brp_extras/get_diagnostics":
                self.frame += 1
                return {"result": {"frame_time_ms": {"current": 8.0}, "frame_count": float(self.frame)}}
        if method == "world.query":
            return {"result": query(self, params)}
        if method == "world.get_resources":
            return {"result": {"value": {"home": {"animation_duration_milliseconds": 2000}}}}
        if method == "world.get_components":
            return {"result": {"components": camera_components(
                self.window_physical_size,
                self.window_scale_factor_override or self.window_scale_factor,
            ), "errors": {}}}
        if method == "world.list_components":
            entity = params.get("entity")
            if entity is None:
                return {"result": [NAME, AABB, GRAYSCALE, BLUR]}
            if entity == EXTRA_ENTITY:
                return {"result": [NAME]}
            return {"error": {"code": -23402, "message": f"entity {entity} not found"}}
        if method == "brp_extras/screenshot":
            if self.screenshot == "hang":
                _ = self.release.wait(15)
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


def query(fake: Fake, params: dict[str, object]) -> list[dict[str, object]]:
    with_types = cast(dict[str, list[str]], params.get("filter") or {}).get("with", [])
    data = cast(dict[str, list[str]], params.get("data") or {})
    if EDITOR_CAMERA in with_types:
        return [{"entity": CAMERA_ENTITY, "components": camera_components(
            fake.window_physical_size,
            fake.window_scale_factor_override or fake.window_scale_factor,
        )}]
    if PRIMARY_WINDOW in with_types:
        width, height = fake.window_physical_size
        return [{"entity": WINDOW_ENTITY, "components": {WINDOW: {"resolution": {
            "physical_width": width,
            "physical_height": height,
            "scale_factor_override": fake.window_scale_factor_override,
            "scale_factor": fake.window_scale_factor,
        }}}}]
    if SWITCH_SLIDER in with_types:
        return [
            {"entity": entity, "components": {path: {"id": tool_id, "definition": definition}}, "has": {AABB: False}}
            for entity, path, tool_id, definition in TOOLS
        ]
    if NAME in data.get("components", []):
        return [{"entity": entity, "components": {NAME: name}, "has": {AABB: True}} for entity, name in NAMES]
    return []


def handler_for(fake: Fake) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            request = cast(Request, json.loads(self.rfile.read(length)))
            reply = {"jsonrpc": "2.0", "id": 1, **fake.answer(request["method"], request.get("params") or {})}
            body = json.dumps(reply).encode()
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
    scratch: Path = Path()
    fake: Fake = Fake()
    port: int = 0

    @override
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.fake = Fake()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.fake))
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

    def run_script(self, *args: str, session_variables: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.scratch, env=environment, capture_output=True, text=True, timeout=60, check=False,
        )

    def shot(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_script("shot", "--port", str(self.port), "--out", str(self.scratch / "shot.png"), *args)

    def views(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_script("views", *args, "--views-file", str(self.scratch / "views.toml"))


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
                    self.assertIn("must name exactly one entity; found 20, 30", result.stderr)
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(triggers, [{"event": ZOOM_TO_TARGET, "value": {"entity": entity}}])

    def test_ambiguous_and_missing_targets_fail(self) -> None:
        for selector, found in (("name:Twin", "12, 13"), ("tool:Grayscale#5", "nothing"), ("entity:999", "nothing")):
            with self.subTest(selector=selector):
                result = self.shot("--target", selector, "--mode", "hana-frame")
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"found {found}", result.stderr)
        self.assertEqual(self.fake.methods("brp_extras/screenshot"), [])


class ViewsTests(HanaShotTest):
    def test_empty_registry_records_a_refused_invocation(self) -> None:
        _ = (self.scratch / "views.toml").write_text("")
        shot = self.run_script(
            "shot", "--port", str(self.port), "--view", "all", "--views-file", str(self.scratch / "views.toml"),
        )
        check = self.views("check", "--port", str(self.port))
        self.assertEqual((shot.returncode, check.returncode), (2, 2))
        self.assertEqual([line["failure_reason"] for line in self.timing_lines()], ["invalid_request"] * 2)
        self.assertEqual([line["invocation_kind"] for line in self.timing_lines()], ["shot", "views_check"])

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

        _ = fake_magick.write_text("#!/bin/sh\necho 0.5\n")
        self.fake.screenshot = "tiny"
        empty = self.views("check", "--port", str(self.port), "--out", str(self.scratch / "check"))
        self.assertEqual(empty.returncode, 1, empty.stderr)
        self.assertEqual(self.timing_lines()[1]["failure_reason"], "empty_crop")

        self.fake.screenshot = "ok"
        passed = self.views("check", "--port", str(self.port), "--out", str(self.scratch / "check"))
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(self.timing_lines()[2]["status"], "success")
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
        self.assertEqual(line["session"], {"state": "absent"})
        [attempt] = line["attempts"]
        self.assertEqual((attempt["status"], attempt["label"], attempt["view"]), ("success", "home", None))
        self.assertEqual(attempt["image_paths"], [str(self.scratch / "shot.png")])
        self.assertEqual([path.name for path in self.scratch.glob(".shot-*")], [])

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

    def test_missing_target_and_invalid_request_are_recorded(self) -> None:
        missing = self.shot("--target", "name:Absent")
        refused = self.shot("--mode", "pose")
        self.assertEqual((missing.returncode, refused.returncode), (1, 2))
        self.assertEqual([line["failure_reason"] for line in self.timing_lines()], ["no_target", "invalid_request"])

    def test_parser_error_is_recorded(self) -> None:
        result = self.run_script("shot", "--port", str(self.port), "--mode")
        self.assertEqual(result.returncode, 2, result.stderr)
        [line] = self.timing_lines()
        self.assertEqual((line["port"], line["failure_reason"], line["exit_code"]),
                         (self.port, "invalid_request", 2))

    def test_multi_view_failure_keeps_earlier_attempt(self) -> None:
        views = self.scratch / "views.toml"
        _ = views.write_text('[views.first]\nmode = "home"\n[views.second]\ntarget = "name:Absent"\n')
        result = self.run_script("shot", "--port", str(self.port), "--view", "all", "--views-file", str(views))
        self.assertEqual(result.returncode, 1, result.stderr)
        [line] = self.timing_lines()
        self.assertEqual(line["failure_reason"], "no_target")
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
        ])
        calls = self.ssh_calls()
        uname, awake, copy = calls[:3]
        self.assertEqual(uname, ["ssh", REMOTE_HOST, "uname", "-s"])
        self.assertEqual(awake[:2], ["ssh", REMOTE_HOST])
        self.assertTrue(awake[2].startswith("caffeinate -u "), awake)
        self.assertEqual(copy[0], "scp", "the display is awake before the shot")
        self.assertIn(["ended"], calls, "the keep-awake ends before the run exits")

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
