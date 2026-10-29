"""The fake showrunner owns a short socket path independent of its record directory."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from typing import cast

from scripts.production.fake_showrunner import write_session


class FakeShowrunnerTests(unittest.TestCase):
    def test_long_session_directory_has_a_live_socket_removed_on_close(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            sessions = Path(temporary) / ("long-session-directory-" * 6)
            self.assertGreater(len(os.fsencode(sessions)), 108)
            held = write_session(sessions, "showrunner", "demo-session")
            self.addCleanup(held.close)
            record = cast(dict[str, object], json.loads(
                (sessions / f"{os.getpid()}.json").read_text(encoding="utf-8")))
            path = Path(cast(str, record["messagingSocketPath"]))
            self.assertLess(len(os.fsencode(path)), 108)
            self.assertTrue(path.is_socket())
            self.assertEqual(path.parent.parent, Path("/tmp"))
            held.listen(1)
            held.settimeout(2)
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(2)
                client.connect(str(path))
                connection = held.accept()[0]
                connection.close()
            held.close()
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
            held.close()


if __name__ == "__main__":
    _ = unittest.main()
