from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import showrunner_footer


HOOK = Path(__file__).with_name("stop-showrunner-footer.py")


class RetiredHookTests(unittest.TestCase):
    def test_a_showrunner_reply_with_an_unregistered_ask_passes(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            instance = Path(scratch) / "showrunner-demo"
            instance.mkdir()
            _ = (instance / "conf").write_text("TARGET=session:showrunner-session\n")
            payload = {"session_id": "showrunner-session",
                       "last_assistant_message": "Run now (recommended), or wait?"}
            result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
                                    capture_output=True, check=False, timeout=30,
                                    env={"NOTIFIER_STATE_DIR": scratch})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


class QuestionLibraryTests(unittest.TestCase):
    def test_read_production_skips_wrapped_doc(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            instance = root / "showrunner-demo"
            instance.mkdir()
            doc = root / "production.md"
            _ = doc.write_text("> **Status: PRODUCTION — wrapped.** demo\n- **User zone:** America/Los_Angeles — demo\n")
            _ = (instance / "conf").write_text(f"CHECK=zsh /x/production_check.sh {doc}\n")
            self.assertIsNone(showrunner_footer.read_production(instance))


if __name__ == "__main__":
    _ = unittest.main()
