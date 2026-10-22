#!/usr/bin/env python3
"""Every launcher in scripts/direct keeps git mode 100755.

A launcher is a tracked file in scripts/direct whose first line starts with
`#!` and whose name does not start with `test_`. Agents and scripts call these
by path (`~/.claude/scripts/direct/verify.sh ...`), so a 100644 entry makes the
call exit 126. Files with no `#!` line are imported modules and are not checked.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DIRECT = "scripts/direct"
EXECUTABLE = "100755"


def index_modes(ls_files_output: str) -> dict[str, str]:
    """Map each path in `git ls-files -s` output to its mode."""
    modes: dict[str, str] = {}
    for line in ls_files_output.splitlines():
        meta, _, path = line.partition("\t")
        if path:
            modes[path] = meta.split(" ", 1)[0]
    return modes


def is_launcher(path: str, first_line: str) -> bool:
    """A file with a `#!` first line and no `test_` name prefix."""
    return first_line.startswith("#!") and not Path(path).name.startswith("test_")


def failures(modes: dict[str, str], first_lines: dict[str, str]) -> list[str]:
    """One message per launcher whose index mode is not 100755."""
    return [
        f"{path} is {mode} in git; fix: git update-index --chmod=+x {path} && chmod +x {path}"
        for path, mode in sorted(modes.items())
        if is_launcher(path, first_lines.get(path, "")) and mode != EXECUTABLE
    ]


def first_line(path: Path) -> str:
    with path.open("rb") as handle:
        return handle.readline().decode("utf-8", errors="replace")


class LauncherModeTests(unittest.TestCase):
    def test_every_launcher_is_executable_in_git(self) -> None:
        output = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-s", "--", DIRECT],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        modes = index_modes(output)
        self.assertIn(f"{DIRECT}/verify.sh", modes)
        first_lines = {path: first_line(REPO_ROOT / path) for path in modes}
        found = failures(modes, first_lines)
        if found:
            self.fail("launchers without 100755:\n" + "\n".join(found))

    def test_a_100644_launcher_fails_and_names_its_fix(self) -> None:
        output = (
            "100755 aaaa 0\tscripts/direct/board.sh\n"
            + "100644 bbbb 0\tscripts/direct/verify.sh\n"
            + "100644 cccc 0\tscripts/direct/plan_history.py\n"
            + "100644 dddd 0\tscripts/direct/test_findings.py\n"
        )
        first_lines = {
            "scripts/direct/board.sh": "#!/usr/bin/env bash\n",
            "scripts/direct/verify.sh": "#!/usr/bin/env bash\n",
            "scripts/direct/plan_history.py": '"""A module."""\n',
            "scripts/direct/test_findings.py": "#!/usr/bin/env python3\n",
        }
        self.assertEqual(
            failures(index_modes(output), first_lines),
            [
                "scripts/direct/verify.sh is 100644 in git; fix: "
                + "git update-index --chmod=+x scripts/direct/verify.sh && chmod +x scripts/direct/verify.sh"
            ],
        )


if __name__ == "__main__":
    _ = unittest.main()
