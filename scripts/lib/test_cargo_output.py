"""Test Cargo output discovery without a Cargo process or shared output."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable, cargo_metadata_output_roots, read_cargo_output_roots


class CargoOutputTests(unittest.TestCase):
    def test_metadata_decoder_resolves_paths_and_defaults_the_build_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            target = workspace / "target"
            for build in (None, str(workspace / "build/../intermediates")):
                with self.subTest(build=build):
                    payload = {"workspace_root": str(workspace), "target_directory": str(target)}
                    if build is not None:
                        payload["build_directory"] = build
                    self.assertEqual(cargo_metadata_output_roots(json.dumps(payload)), CargoOutputRoots(
                        workspace, target, workspace / "intermediates" if build else target))

    def test_metadata_command_uses_its_environment(self) -> None:
        checkout = Path("/workspace")
        target = checkout / "selected-output"
        environment = {"CARGO_TARGET_DIR": str(target)}
        payload = {"workspace_root": str(checkout), "target_directory": str(target)}
        with mock.patch("scripts.lib.cargo_output.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(payload), "",
        )) as run:
            self.assertEqual(read_cargo_output_roots(checkout, cargo=("/tools/cargo", "+nightly"),
                                                     options=("--config", 'build.target-dir="selected-output"'),
                                                     environment=environment), CargoOutputRoots(checkout, target, target))
        run.assert_called_once_with(
            ["/tools/cargo", "+nightly", "metadata", "--format-version", "1", "--no-deps",
             "--config", 'build.target-dir="selected-output"'],
            cwd=checkout, capture_output=True, text=True, check=False, env=environment,
        )

    def test_metadata_selects_arbitrary_intermediate_root_and_keeps_missing_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory).resolve()
            target, build = checkout / "artifacts", checkout / "separate" / "intermediates"
            payload = {"workspace_root": str(checkout), "target_directory": str(target), "build_directory": str(build)}
            with mock.patch("scripts.lib.cargo_output.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(payload), "",
            )) as run:
                self.assertEqual(read_cargo_output_roots(checkout), CargoOutputRoots(checkout, target, build))
            self.assertEqual(run.call_args.kwargs["cwd"], checkout)
            self.assertEqual(run.call_args.args[0], ["cargo", "metadata", "--format-version", "1", "--no-deps"])
            self.assertFalse(build.exists())

    def test_older_metadata_uses_target_for_both_roots(self) -> None:
        checkout = Path("/workspace")
        payload = {"workspace_root": str(checkout), "target_directory": str(checkout / "target")}
        with mock.patch("scripts.lib.cargo_output.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(payload), "",
        )):
            self.assertEqual(read_cargo_output_roots(checkout), CargoOutputRoots(
                checkout, checkout / "target", checkout / "target",
            ))

    def test_bad_metadata_returns_named_failure(self) -> None:
        for payload in ("bad JSON", "[]", "{}", '{"workspace_root":null}',
                        '{"workspace_root":"/w","target_directory":"relative"}',
                        '{"workspace_root":"/w","target_directory":"/t","build_directory":null}'):
            with self.subTest(payload=payload), mock.patch(
                "scripts.lib.cargo_output.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, payload, ""),
            ):
                self.assertIsInstance(read_cargo_output_roots(Path("/w")), CargoOutputUnavailable)

    def test_command_failure_returns_named_failure(self) -> None:
        with mock.patch("scripts.lib.cargo_output.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 1, "", "no workspace",
        )):
            self.assertEqual(read_cargo_output_roots(Path("/w")), CargoOutputUnavailable("no workspace"))
        with mock.patch("scripts.lib.cargo_output.subprocess.run", side_effect=OSError("no cargo")):
            self.assertEqual(read_cargo_output_roots(Path("/w")), CargoOutputUnavailable("no cargo"))


if __name__ == "__main__":
    _ = unittest.main()
