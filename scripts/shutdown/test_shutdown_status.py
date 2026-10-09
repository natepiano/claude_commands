"""Regression tests for shutdown status rendering and account resolution."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

import shutdown
from inventory import Inventory


LOGIN = "owner@example.com"


def empty_inventory(machine: str = "natedev") -> Inventory:
    return {
        "machine": machine,
        "login": LOGIN,
        "label": "claude 2",
        "sessions": [],
        "unknown": [],
    }


def complete_session(*, host: object) -> dict[str, object]:
    return {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "pid": 1234,
        "name": "shutdown",
        "cwd": "/tmp/checkout",
        "kind": "top-level",
        "status": "idle",
        "host": host,
        "model": "opus",
        "checkout": {
            "path": "/tmp/checkout",
            "branch": "main",
            "head": "abc123",
            "ahead": 0,
            "dirty": [],
        },
        "run_dirs": [],
        "codex_servers": [],
        "timers": [],
        "owner": None,
    }


@final
class ShutdownStatusTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.notes = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.notes = self.root / "agents"
        self.notes.mkdir()
        environment = {**os.environ, "AGENT_NOTES_DIR": str(self.notes)}
        environment_context: object = cast(
            object,
            self.enterContext(patch.dict(os.environ, environment, clear=True)),
        )
        del environment_context

    def write_note(self, label: str, login: str) -> None:
        _ = (self.notes / f"{label}.md").write_text(
            f"---\nlogin: {login}\nstate: active\n---\n",
            encoding="utf-8",
        )

    def run_status(
        self, status: int, output: str, *, as_json: bool = True
    ) -> tuple[int, str, str]:
        standard_output = io.StringIO()
        standard_error = io.StringIO()
        arguments = ["status", LOGIN]
        if as_json:
            arguments.append("--json")
        with (
            patch.object(shutdown, "inventory", return_value=empty_inventory()),
            patch.object(shutdown, "run_remote", return_value=(status, output)),
            patch.object(shutdown, "other_machine", return_value="Mac"),
            redirect_stdout(standard_output),
            redirect_stderr(standard_error),
        ):
            result = shutdown.main(arguments)
        return result, standard_output.getvalue(), standard_error.getvalue()

    def test_empty_inventory_renders_no_sessions_for_the_account(self) -> None:
        standard_output = io.StringIO()
        with (
            patch.object(shutdown, "inventory", return_value=empty_inventory()),
            redirect_stdout(standard_output),
        ):
            result = shutdown.main(["status", LOGIN, "--here"])

        self.assertEqual(result, 0)
        self.assertIn("natedev:\n  no sessions on claude 2\n", standard_output.getvalue())

    def test_account_label_resolution_accepts_only_claude_notes(self) -> None:
        self.write_note("claude 2", LOGIN)
        self.write_note("codex 2", "codex@example.com")

        def inventory_for(login: str) -> Inventory:
            self.assertEqual(login, LOGIN)
            return empty_inventory()

        with (
            patch.object(shutdown, "inventory", side_effect=inventory_for),
            redirect_stdout(io.StringIO()),
        ):
            result = shutdown.main(["status", "claude 2", "--json", "--here"])
        self.assertEqual(result, 0)

        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch.object(
                shutdown,
                "inventory",
                side_effect=AssertionError("inventory must not run"),
            ),
        ):
            result = shutdown.main(["status", "codex 2", "--here"])
        self.assertEqual(result, 2)
        self.assertIn("unknown account codex 2", errors.getvalue())

    def test_login_argument_is_accepted_without_a_note(self) -> None:
        def inventory_for(login: str) -> Inventory:
            self.assertEqual(login, "person@example.com")
            return empty_inventory()

        with (
            patch.object(shutdown, "inventory", side_effect=inventory_for),
            redirect_stdout(io.StringIO()),
        ):
            result = shutdown.main(
                ["status", "person@example.com", "--json", "--here"]
            )
        self.assertEqual(result, 0)

    def test_unknown_account_name_exits_two_with_guidance(self) -> None:
        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch.object(
                shutdown,
                "inventory",
                side_effect=AssertionError("inventory must not run"),
            ),
        ):
            result = shutdown.main(["status", "claude2", "--here"])

        self.assertEqual(result, 2)
        self.assertEqual(
            errors.getvalue(),
            "shutdown: unknown account claude2: give a note label such as claude 2, or a login\n",
        )

    def test_remote_inventory_missing_unknown_is_unavailable_with_reason(self) -> None:
        response = json.dumps(
            {
                "machine": "Mac",
                "login": LOGIN,
                "label": "claude 2",
                "sessions": [],
            }
        )

        result, output, error = self.run_status(0, response)

        expected = {
            "machine": "Mac",
            "status": "unavailable",
            "rc": 0,
            "reason": "inventory is missing unknown",
        }
        self.assertEqual(result, 0)
        self.assertEqual(
            output,
            json.dumps([empty_inventory(), expected], sort_keys=True) + "\n",
        )
        self.assertEqual(error, "")

    def test_remote_inventory_with_malformed_session_is_unavailable_with_reason(self) -> None:
        response = json.dumps(
            {
                "machine": "Mac",
                "login": LOGIN,
                "label": "claude 2",
                "sessions": [complete_session(host={"kind": 7})],
                "unknown": [],
            }
        )

        result, output, error = self.run_status(0, response)

        expected = {
            "machine": "Mac",
            "status": "unavailable",
            "rc": 0,
            "reason": "inventory.sessions[0].host.kind must be a string",
        }
        self.assertEqual(result, 0)
        self.assertEqual(
            output,
            json.dumps([empty_inventory(), expected], sort_keys=True) + "\n",
        )
        self.assertEqual(error, "")

    def test_reachable_remote_failure_is_unavailable_not_unreachable(self) -> None:
        result, output, error = self.run_status(3, "remote inventory failed")

        expected = {
            "machine": "Mac",
            "status": "unavailable",
            "rc": 3,
            "reason": "remote inventory failed",
        }
        self.assertEqual(result, 0)
        self.assertEqual(
            output,
            json.dumps([empty_inventory(), expected], sort_keys=True) + "\n",
        )
        self.assertEqual(error, "")

        _, plain, _ = self.run_status(3, "remote inventory failed", as_json=False)
        self.assertIn("Mac: unavailable (rc 3)\n", plain)

    def test_transport_failure_is_unreachable(self) -> None:
        result, output, error = self.run_status(255, "")

        self.assertEqual(result, 0)
        self.assertEqual(
            output,
            json.dumps(
                [empty_inventory(), {"machine": "Mac", "unreachable": True}],
                sort_keys=True,
            )
            + "\n",
        )
        self.assertEqual(error, "")

        _, plain, _ = self.run_status(255, "", as_json=False)
        self.assertIn("Mac: unreachable\n", plain)


if __name__ == "__main__":
    _ = unittest.main()
