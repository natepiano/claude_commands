"""Exercise the usage collector against invented source files."""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import unittest
from typing import cast, final

from scripts.usage.collect_test_support import MacCollectorFixture, REPOSITORY, TEST_HANG_GUARD_SECS


@final
class MacCollectTests(MacCollectorFixture):
    def test_running_pass_keeps_its_lock_through_the_mac_read_until_exit(self) -> None:
        report = self.root / "ssh-report.json"
        waiting = report.with_suffix(".waiting")
        release = report.with_suffix(".release")
        configuration = {"home": str(self.mac_home), "execution_directory": str(self.execution_directory),
                         "report": str(report), "behavior": "wait_for_release", "database": str(self.db_path),
                         "previous_role_read_at": ""}
        _ = (self.root / "ssh-configuration.json").write_text(json.dumps(configuration), encoding="utf-8")
        command, environment = self.collector_invocation(db=self.db_path, mac_pass="fixture")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(TEST_HANG_GUARD_SECS)
            environment["USAGE_TEST_WAIT_PORT"] = str(cast(tuple[str, int], listener.getsockname())[1])
            with subprocess.Popen(command, env=environment, cwd=REPOSITORY, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as process:
                try:
                    event = listener.accept()[0]
                    self.addCleanup(event.close)
                    event.settimeout(TEST_HANG_GUARD_SECS)
                    with event.makefile("rb") as ready:
                        self.assertEqual(ready.read(7), b"waiting", "The fixture must signal its Mac read")
                    self.assertTrue(waiting.exists(), "The fixture pass must reach its Mac read")
                    before = {cast(str, row["name"]): self.all_values(self.rows('SELECT * FROM "' + cast(str, row["name"]) + '"'))
                              for row in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
                    for attempt in range(2):
                        with self.subTest(attempt=attempt):
                            result = self.cli(db=self.db_path)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(result.stdout, "usage collect: another pass is running\n")
                            self.assertEqual(result.stderr, "")
                            self.assertIsNone(process.poll())
                            for table, rows in before.items():
                                self.assertEqual(self.all_values(self.rows('SELECT * FROM "' + table + '"')), rows, table)
                    _ = release.write_bytes(b"")
                    event.sendall(b"1")
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, 0, output + errors)
                finally:
                    _ = release.touch()
                    if process.poll() is None:
                        process.terminate()
                        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertTrue(report.is_file())
        _ = cast(object, json.loads(report.read_text(encoding="utf-8")))
        report.unlink()
        waiting.unlink()
        release.unlink()
        _ = self.mac_collect()

    def test_cut_stream_drains_remote_writer_before_reaping_shell(self) -> None:
        report = self.root / "ssh-report.json"
        finished = self.root / "remote-writer-finished"
        configuration = {"home": str(self.mac_home), "execution_directory": str(self.execution_directory),
                         "report": str(report), "behavior": "cut_inside_record", "database": str(self.db_path),
                         "previous_role_read_at": ""}
        config = self.root / "ssh-configuration.json"
        _ = config.write_text(json.dumps(configuration), encoding="utf-8")
        records = ('{"type":"file","kind":"claude"}\n{"type":"cursor"}\n'
                   '{"type":"file","kind":"claude"}\n{"type":"turn"}\n')
        writer = (
            "import sys; from pathlib import Path; "
            f"sys.stdout.write({records!r}); sys.stdout.flush(); "
            "sys.stdout.write('{}\\n' * 100000); sys.stdout.flush(); "
            "Path(sys.argv[1]).write_text('finished')"
        )
        remote_command = shlex.join((sys.executable, "-c", writer, str(finished))) + '; printf "\\nrc=%s\\n" "$?"'
        result = subprocess.run(
            [str(self.root / "bin/ssh"), remote_command],
            input=b"fixture request", capture_output=True, timeout=TEST_HANG_GUARD_SECS, check=False,
            env={**os.environ, "USAGE_TEST_SSH_CONFIG": str(config)},
        )
        self.assertEqual(result.stderr, b"", result.stderr.decode(errors="replace"))
        self.assertEqual(result.returncode, 0)
        self.assertTrue(finished.is_file(), "A cut fixture must reap the writer after it finishes")
        lines = records.splitlines(keepends=True)
        expected = "".join(lines[:-1]) + lines[-1][:len(lines[-1]) // 2]
        self.assertEqual(result.stdout, expected.encode())



if __name__ == "__main__":
    _ = unittest.main()
