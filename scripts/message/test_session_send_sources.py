"""Check the repository's own scripts for sender guards."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest


class SessionSendTests(unittest.TestCase):
    def test_sender_callers_install_the_guard_in_set_up_module(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        targets = {"session_send_argv", "send_holder_message", "send_floor_alert", "deliver_reading_alert"}
        missing: list[str] = []
        for path in sorted(scripts.rglob("*.py")):
            if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
                continue
            if path == scripts / "message/test_send.py":
                continue
            module = ast.parse(path.read_text(), filename=str(path))
            if not any((isinstance(node, ast.Name) and node.id in targets)
                       or (isinstance(node, ast.Attribute) and node.attr in targets)
                       for node in ast.walk(module)):
                continue
            guarded = any(
                isinstance(node, ast.FunctionDef) and node.name == "setUpModule"
                and any(isinstance(call, ast.Call)
                        and ((isinstance(call.func, ast.Name) and call.func.id == "forbid_real_sender")
                             or (isinstance(call.func, ast.Attribute) and call.func.attr == "forbid_real_sender"))
                        for call in ast.walk(node))
                for node in module.body
            )
            if not guarded:
                missing.append(str(path.relative_to(scripts)))
        self.assertEqual(missing, [], "Sender tests must install forbid_real_sender in setUpModule")


if __name__ == "__main__":
    _ = unittest.main()
