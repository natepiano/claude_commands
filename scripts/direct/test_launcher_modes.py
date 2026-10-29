#!/usr/bin/env python3
"""Every launcher in scripts/direct keeps git mode 100755.

A launcher is a tracked file in scripts/direct whose first line starts with
`#!` and which is not test code. Agents and scripts call these
by path (`~/.claude/scripts/direct/verify.sh ...`), so a 100644 entry makes the
call exit 126. Files with no `#!` line are imported modules and are not checked.
A gate copy with no .git has no index, so there the test reads the modes of the
files on disk instead; a copy keeps them, and a call by path needs them too.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.lib.code_kind import is_test_code

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


def disk_modes(root: Path) -> dict[str, str]:
    """Map each file in scripts/direct on disk to the git mode its executable bit gives."""
    return {
        f"{DIRECT}/{path.name}": EXECUTABLE if path.stat().st_mode & 0o111 else "100644"
        for path in sorted((root / DIRECT).iterdir())
        if path.is_file()
    }


def tracked_modes(root: Path) -> tuple[dict[str, str], str]:
    """The index modes, or the disk modes when the tree has no git index, and where they came from."""
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-s", "--", DIRECT],
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode == 0 and listed.stdout:
        return index_modes(listed.stdout), "in git"
    return disk_modes(root), "on disk"


def is_launcher(path: str, first_line: str) -> bool:
    """A production file with a `#!` first line."""
    return first_line.startswith("#!") and not is_test_code(Path(path))


def failures(modes: dict[str, str], first_lines: dict[str, str], source: str = "in git") -> list[str]:
    """One message per launcher whose mode is not 100755."""
    return [
        f"{path} is {mode} {source}; fix: git update-index --chmod=+x {path} && chmod +x {path}"
        for path, mode in sorted(modes.items())
        if is_launcher(path, first_lines.get(path, "")) and mode != EXECUTABLE
    ]


def first_line(path: Path) -> str:
    with path.open("rb") as handle:
        return handle.readline().decode("utf-8", errors="replace")


class LauncherModeTests(unittest.TestCase):
    def test_every_launcher_is_executable_in_git(self) -> None:
        modes, source = tracked_modes(REPO_ROOT)
        self.assertIn(f"{DIRECT}/verify.sh", modes)
        first_lines = {path: first_line(REPO_ROOT / path) for path in modes}
        found = failures(modes, first_lines, source)
        if found:
            self.fail("launchers without 100755:\n" + "\n".join(found))

    def test_a_100644_launcher_fails_and_names_its_fix(self) -> None:
        output = (
            "100755 aaaa 0\tscripts/direct/board.sh\n"
            + "100644 bbbb 0\tscripts/direct/verify.sh\n"
            + "100644 cccc 0\tscripts/direct/plan_history.py\n"
            + "100644 dddd 0\tscripts/direct/test_findings.py\n"
            + "100644 eeee 0\tscripts/direct/fake_tool.py\n"
            + "100644 ffff 0\tscripts/direct/launcher_test_support.py\n"
        )
        first_lines = {
            "scripts/direct/board.sh": "#!/usr/bin/env bash\n",
            "scripts/direct/verify.sh": "#!/usr/bin/env bash\n",
            "scripts/direct/plan_history.py": '"""A module."""\n',
            "scripts/direct/test_findings.py": "#!/usr/bin/env python3\n",
            "scripts/direct/fake_tool.py": "#!/usr/bin/env python3\n",
            "scripts/direct/launcher_test_support.py": "#!/usr/bin/env python3\n",
        }
        self.assertEqual(
            failures(index_modes(output), first_lines),
            [
                "scripts/direct/verify.sh is 100644 in git; fix: "
                + "git update-index --chmod=+x scripts/direct/verify.sh && chmod +x scripts/direct/verify.sh"
            ],
        )

    def test_a_tree_with_no_git_reads_the_modes_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            direct = root / DIRECT
            direct.mkdir(parents=True)
            for name, mode in (("verify.sh", 0o755), ("board.sh", 0o644), ("plan_history.py", 0o644)):
                launcher = direct / name
                _ = launcher.write_text("#!/usr/bin/env bash\n" if name.endswith(".sh") else '"""A module."""\n')
                launcher.chmod(mode)
            modes, source = tracked_modes(root)
            self.assertEqual(source, "on disk")
            first_lines = {path: first_line(root / path) for path in modes}
            self.assertEqual(
                failures(modes, first_lines, source),
                [
                    "scripts/direct/board.sh is 100644 on disk; fix: "
                    + "git update-index --chmod=+x scripts/direct/board.sh && chmod +x scripts/direct/board.sh"
                ],
            )


if __name__ == "__main__":
    _ = unittest.main()
