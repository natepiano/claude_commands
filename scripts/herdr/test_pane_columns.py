"""Tests for the pane width read through a fake herdr socket."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts.herdr import herdr

LAYOUT = {"result": {"type": "pane_layout", "layout": {"panes": [
    {"pane_id": "w1:p1", "rect": {"x": 0, "y": 0, "width": 88, "height": 40}},
    {"pane_id": "w1:p2", "rect": {"x": 88, "y": 0, "width": 176, "height": 40}}]}}}


class PaneColumnsTests(unittest.TestCase):
    requests: list[object] = []

    def serve(self, reply: bool) -> Path:
        """Answer one pane.layout request on a socket, or hold it without a reply."""
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "herdr.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        server.listen(1)
        requests: list[object] = []
        done = threading.Event()
        self.addCleanup(done.set)

        def answer() -> None:
            connection = server.accept()[0]
            with connection, connection.makefile("r", encoding="utf-8") as stream:
                request: object = json.loads(stream.readline())  # pyright: ignore[reportAny]
                requests.append(request)
                if reply:
                    connection.sendall((json.dumps(LAYOUT) + "\n").encode())
                _ = done.wait(5)

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        self.requests = requests
        return path

    def test_width_comes_from_the_calling_pane(self) -> None:
        path = self.serve(reply=True)
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2", "HERDR_SOCKET_PATH": str(path)}):
            self.assertEqual(herdr.own_pane_columns(1.0), 176)
        self.assertEqual(self.requests, [{"id": "pane-columns", "method": "pane.layout",
                                          "params": {"pane_id": "w1:p2"}}])

    def test_no_width_outside_herdr_or_after_the_time_limit(self) -> None:
        path = self.serve(reply=False)
        for name, environment in (
                ("no pane", {"HERDR_SOCKET_PATH": str(path)}),
                ("no socket", {"HERDR_PANE_ID": "w1:p2", "HERDR_SOCKET_PATH": str(path.with_name("absent.sock"))}),
                ("no reply", {"HERDR_PANE_ID": "w1:p2", "HERDR_SOCKET_PATH": str(path)})):
            with self.subTest(name), mock.patch.dict(os.environ, environment):
                if name == "no pane":
                    _ = os.environ.pop("HERDR_PANE_ID", None)
                self.assertIsNone(herdr.own_pane_columns(0.05))


if __name__ == "__main__":
    _ = unittest.main()
