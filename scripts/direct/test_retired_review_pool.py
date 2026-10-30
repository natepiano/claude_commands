"""Reject operational readers of the retained archive, allowing only migration history."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


REMOVED_NAMES = frozenset({
    "review_pool", "review_pool_report", "transfer_reviewers", "_transfer_reviewers",
    "_adopt_unit_reviewers", "IncomingReviewer", "NoIncomingReviewer", "PooledReviewerOwner",
})
REMOVED_PATHS = re.compile(r"scripts[/.]direct[/.]review_pool(?:_report)?|review_pool(?:_report)?\.py|"
                           + r"REVIEW_POOL_STATE|worker-transfers|reviewer_inbox_sources|reviewers_adopted|transfer-reviewers")


def retired_operations(path: str, source: str) -> list[str]:
    """Check executable references; historical record arms and ignored flags remain valid."""
    problems: list[str] = []
    if path.endswith(".py"):
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name.rsplit(".", 1)[-1] for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [item.name for item in node.names] + [(node.module or "").rsplit(".", 1)[-1]]
            elif isinstance(node, (ast.Name, ast.Attribute)):
                names = [node.id if isinstance(node, ast.Name) else node.attr]
            else:
                names = []
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Name, ast.Attribute)) and any(name in REMOVED_NAMES for name in names):
                problems.append(f"{path}:{node.lineno}: retired operation")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if REMOVED_PATHS.search(node.value):
                    problems.append(f"{path}:{node.lineno}: retired path or state reader")
                if "review-pool" in node.value:
                    # The rename sweep classifies this retained on-disk archive as history.
                    if not (path == "scripts/production/unit_rename_sweep.py" and node.value == "review-pool/"):
                        problems.append(f"{path}:{node.lineno}: retired archive")
    else:
        for number, line in enumerate(source.splitlines(), 1):
            if not line.lstrip().startswith("#") and REMOVED_PATHS.search(line):
                problems.append(f"{path}:{number}: retired launcher operation")
    return problems


class RetiredReviewPoolTests(unittest.TestCase):
    def test_guard_rejects_readers_and_late_imports_but_keeps_compatibility_and_history(self) -> None:
        for source in (
            "from scripts.direct import review_pool\nreview_pool.main()\n",
            "from scripts.direct.review_pool import ProductionPool\n",
            "root = Path(os.environ.get('REVIEW_POOL_STATE', '~/.local/state/review-pool'))\n",
            "workers.transfer_reviewers('production', {})\n",
            "entry['reviewer_inbox_sources'] = []\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(retired_operations("scripts/new_reader.py", source))
        self.assertTrue(retired_operations("scripts/launcher.sh", '"$PY" "$DIR/review_pool.py" release\n'))
        self.assertEqual(retired_operations("scripts/production/rename.py",
            "legacy = '_rename_review_pool'\nlocations = ('review_pool',)\n"), [])
        self.assertEqual(retired_operations("scripts/direct/progress_history.py", "arm = 'pool'\n"), [])
        self.assertEqual(retired_operations("scripts/direct/review.sh", 'case "$1" in --pool|--lease) shift;; esac\n'), [])
        self.assertEqual(retired_operations("scripts/production/unit_rename_sweep.py", "history = ('review-pool/',)\n"), [])

    def test_live_scripts_have_no_pool_reader_or_transfer_caller(self) -> None:
        root = Path(__file__).resolve().parents[2]
        problems: list[str] = []
        for path in sorted((root / "scripts").rglob("*")):
            if path.suffix not in (".py", ".sh") or path.name.startswith("test_"):
                continue
            if "test_support" in path.name or "fixture" in path.name:
                continue
            problems.extend(retired_operations(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")))
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    _ = unittest.main()
