"""SessionStart diagnosis with an injected detached launcher."""
from __future__ import annotations

from collections.abc import Callable
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType
from typing import cast
from unittest.mock import patch
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import nix_env
from scripts.lib.nix_env_test_support import NixFixture, ROOT
from scripts.nix_env import heal


def hook_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("nix_shell_start_fixture", ROOT / "scripts/hooks/session-start-nix-shell.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("SessionStart hook is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SessionStartTests(NixFixture):
    def test_broken_session_explains_cause_and_starts_detached_bounded_sweep(self) -> None:
        hook = hook_module()
        session = heal.HealSession("only-this", 88, "start", "worker", self.work, "busy", self.home / ".claude")
        output = io.StringIO()
        with (patch.object(heal, "find_session", return_value=session), patch.object(heal, "broken", return_value=True),
              patch.object(subprocess, "Popen") as launch,
              patch.object(sys, "stdin", io.StringIO('{"session_id":"only-this"}')),
              redirect_stdout(output)):
            self.assertEqual(cast(Callable[[], int], hook.main)(), 0)
        value = cast(dict[str, dict[str, str]], json.loads(output.getvalue()))
        context = value["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "SessionStart")
        self.assertIn("nix shell", context["additionalContext"])
        self.assertIn("will fail", context["additionalContext"])
        self.assertIn("restart when it is idle", context["additionalContext"])
        command = cast(list[str], launch.call_args.args[0])
        self.assertEqual(command[2:], ["sweep", "--only", "only-this", "--wait-idle", "1800"])
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(launch.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_healthy_or_no_envrc_is_silent_fast_and_never_loads_direnv(self) -> None:
        hook = hook_module()
        session = heal.HealSession("healthy", 88, "start", "worker", self.work, "idle", self.home / ".claude")
        for has_envrc in (False, True):
            with self.subTest(envrc=has_envrc):
                if has_envrc:
                    _ = self.envrc(self.work)
                output = io.StringIO()
                with (patch.object(heal, "find_session", return_value=session),
                      patch.object(heal, "inspect", return_value=heal.Healthy()),
                      patch.object(nix_env, "ensure", side_effect=AssertionError("healthy hook must not load direnv")),
                      patch.object(subprocess, "Popen") as launch,
                      patch.object(sys, "stdin", io.StringIO('{"session_id":"healthy"}')),
                      redirect_stdout(output)):
                    started = time.monotonic()
                    self.assertEqual(cast(Callable[[], int], hook.main)(), 0)
                    self.assertLess(time.monotonic() - started, 0.2)
                self.assertEqual(output.getvalue(), "")
                launch.assert_not_called()
                self.assertEqual(self.direnv_calls(), [])

    def test_absent_session_does_not_start_a_sweep(self) -> None:
        hook = hook_module()
        with (patch.object(heal, "find_session", return_value=heal.Gone()),
              patch.object(subprocess, "Popen") as launch,
              patch.object(sys, "stdin", io.StringIO('{"session_id":"gone"}')),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(cast(Callable[[], int], hook.main)(), 0)
        self.assertEqual(output.getvalue(), "")
        launch.assert_not_called()
