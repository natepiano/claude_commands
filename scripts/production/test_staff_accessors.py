"""Keep the documented staff accessors complete."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
DOCUMENT = "docs/as-built/staff-accessors.md"
ACCESS = re.compile(
    r"\bfrom\s+(?:scripts\.production\.|\.+production\.)?staff\s+import\b"
    + r"|\bfrom\s+(?:scripts\.production|\.+production)\s+import\s+"
    + r"(?:\([^)]*\bstaff\b[^)]*\)|[^\n;#]*\bstaff\b)"
    + r"|(?<!from )\bimport[ \t]+(?:[\w.]+(?:[ \t]+as[ \t]+\w+)?[ \t]*,[ \t]*)*"
    + r"(?:scripts\.production\.)?staff\b"
    + r"|\bimport_module\s*\(\s*[\"'](?:scripts\.production\.)?staff[\"']\s*\)"
    + r"|(?<![\w-])staff/staff\.json\b"
    + r"|[\"']staff[\"']\s*/\s*[\"']staff\.json[\"']"
    + r"|(?<![\w-])staff\.py(?=[\s\"'`]|$)"
)
BACKTICKS = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
REPO_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)+")


def command_text(source: str) -> str:
    """Keep Markdown commands and their original line numbers."""
    lines: list[str] = []
    fence = ""
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = "" if fence == marker else marker
            lines.append("")
        elif fence or re.match(r"(?:[-*] |\d+\. )?(?:Read|Run)\s", stripped):
            lines.append(line)
        else:
            # A bare file name in an inventory is not a command.
            lines.append(" ".join(match.group(1) for match in BACKTICKS.finditer(line)
                                  if re.search(r"\s", match.group(1))))
    return "\n".join(lines)


def accessor_gaps(root: Path, tracked: Iterable[str], document: str) -> list[str]:
    """Report missing accessors and absent files from the accessor table."""
    listed: set[str] = set()
    file_column = -1
    for line in document.splitlines():
        if not line.startswith("|"):
            file_column = -1
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if "File" in cells:
            file_column = cells.index("File")
        elif 0 <= file_column < len(cells):
            for match in BACKTICKS.finditer(cells[file_column]):
                if REPO_PATH.fullmatch(match.group(1)):
                    listed.add(match.group(1))
    gaps = [f"{path}: listed path does not exist" for path in sorted(listed)
            if not (root / path).is_file()]
    for relative in tracked:
        if relative in listed or not relative.startswith(("scripts/", "commands/", "docs/")):
            continue
        path = root / relative
        if relative.startswith("docs/plans/") or (path.name.startswith("test_") and path.suffix == ".py"):
            continue
        if path.suffix not in {".py", ".sh", ".md"} or not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        if "staff" not in source:
            continue
        scanned = command_text(source) if path.suffix == ".md" else source
        match = ACCESS.search(scanned)
        if match is not None:
            number = scanned.count("\n", 0, match.start()) + 1
            evidence = source.splitlines()[number - 1].strip()
            gaps.append(f"{relative}:{number}: missing accessor: {evidence}")
    return gaps


class StaffAccessorInventoryTests(unittest.TestCase):
    def test_document_lists_every_tracked_accessor(self) -> None:
        document = REPOSITORY / DOCUMENT
        if not document.is_file():
            self.fail(f"{DOCUMENT}: required document is missing")
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z", "--", "scripts/", "commands/", "docs/"],
            cwd=REPOSITORY, text=True,
        ).split("\0")
        gaps = accessor_gaps(REPOSITORY, filter(None, tracked), document.read_text(encoding="utf-8"))
        if gaps:
            self.fail("\n".join(gaps))

    def test_omitted_accessors_and_removed_files_have_evidence(self) -> None:
        sources = {
            "scripts/reader.py": "# A reader.\nimport staff\n",
            "scripts/types.py": "from staff import StaffRow\n",
            "scripts/qualified.py": "from scripts.production.staff import StaffRow\n",
            "scripts/dynamic.py": 'resolve = import_module("scripts.production.staff").current_session_target\n',
            "scripts/dynamic_short.py": "module = import_module('staff')\n",
            "scripts/reader_test_support.py": "from scripts.production import (\n    staff,\n)\n",
            "scripts/paths.sh": 'cat "$HOME/.local/state/staff/staff.json"\n',
            "scripts/path_parts.py": 'record = state / "staff" / "staff.json"\n',
            "commands/read.md": "Read `~/.local/state/staff/staff.json`.\n",
            "docs/run.md": "The module is `scripts/production/staff.py`.\n"
                           + "Run `python3 scripts/production/staff.py show`.\n",
            "docs/fenced.md": "```python\nfrom scripts.production import session_staff, staff\n```\n",
        }
        excluded = {
            "scripts/test_reader.py": "import staff\n",
            "docs/plans/history.md": "```sh\npython3 staff.py show\n```\n",
            "docs/description.md": "The module is `scripts/production/staff.py`.\n",
            "scripts/untracked.py": "import staff\n",
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for relative, source in (sources | excluded).items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text(source, encoding="utf-8")
            tracked = [path for path in sources | excluded if path != "scripts/untracked.py"]
            document = "| File | Access |\n|---|---|\n" + "\n".join(
                f"| `{path}` | read |" for path in sources
            ) + ("\n\n## Approved copies\n\n| Copy | Writer |\n|---|---|\n"
                 + "| `~/.local/state/staff/staff.json` | `scripts/other-repo.py` |\n")
            self.assertEqual(accessor_gaps(root, tracked, document), [])
            missing = document.replace("| `scripts/reader.py` | read |\n", "")
            self.assertEqual(accessor_gaps(root, tracked, missing),
                             ["scripts/reader.py:2: missing accessor: import staff"])
            (root / "scripts/reader.py").unlink()
            self.assertEqual(accessor_gaps(root, tracked, document),
                             ["scripts/reader.py: listed path does not exist"])
            gaps = accessor_gaps(root, tracked, "| File |\n|---|\n")
            for relative in sources.keys() - {"scripts/reader.py"}:
                self.assertTrue(any(gap.startswith(relative + ":") for gap in gaps), relative)
            self.assertEqual(len(gaps), len(sources) - 1)


if __name__ == "__main__":
    _ = unittest.main()
