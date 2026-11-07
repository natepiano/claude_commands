#!/usr/bin/env python3
"""Find orphaned scripts and config files in a .claude directory."""

import json
import re
import sys
from pathlib import Path
from typing import TypedDict

# Directories that can name a script or config file in a way that keeps it
# alive: a slash command, a skill, another script, a config file, a doc, a
# template, plus the two top-level files. Everything else under .claude is
# session residue -- transcripts, caches, backups -- where a stale mention
# proves the file was used once, not that anything still reaches it.
REFERENCE_DIRECTORIES = (
    "commands",
    "skills",
    "scripts",
    "config",
    "docs",
    "templates",
    "tests",
    "shared",
)
REFERENCE_FILES = ("CLAUDE.md", "settings.json")
SKIPPED_DIRECTORY_NAMES = frozenset({"__pycache__", ".git", ".venv", "node_modules"})
MAXIMUM_CORPUS_FILE_BYTES = 2_000_000


class OrphanInfo(TypedDict):
    """Information about an orphaned file."""
    path: str
    category: str  # "script" or "config"


class ExpectedOrphans(TypedDict):
    """Expected orphan files that should be ignored."""
    scripts: list[str]
    config: list[str]


def find_scripts(scripts_dir: Path) -> list[Path]:
    """Find all bash and python scripts in the scripts directory.

    Test modules are left out. Unittest reaches them by discovery -- `python3 -m
    package.tests.test_thing` -- so no file names them, and every one of them
    would be reported forever. A stranded test goes when the code it covers
    goes, not on its own.
    """
    scripts: list[Path] = []
    if scripts_dir.exists():
        scripts.extend(scripts_dir.rglob("*.sh"))
        scripts.extend(scripts_dir.rglob("*.py"))
    return [
        s
        for s in scripts
        if not _is_skipped(s) and not s.name.startswith("test_")
    ]


def find_config_files(config_dir: Path) -> list[Path]:
    """Find all files in the config directory."""
    if config_dir.exists():
        return [f for f in config_dir.rglob("*") if f.is_file() and not _is_skipped(f)]
    return []


def _is_skipped(path: Path) -> bool:
    """Whether any directory on the path is one this scan never reads."""
    return any(part in SKIPPED_DIRECTORY_NAMES for part in path.parts)


def load_expected_orphans(claude_dir: Path) -> ExpectedOrphans:
    """Load the list of expected orphans from config."""
    config_file = claude_dir / "config" / "orphans_expected.json"

    if not config_file.exists():
        return {"scripts": [], "config": []}

    try:
        with open(config_file) as f:
            data: ExpectedOrphans = json.load(f)  # pyright: ignore[reportAny]
            return data
    except (OSError, json.JSONDecodeError):
        return {"scripts": [], "config": []}


def read_reference_corpus(claude_dir: Path) -> dict[Path, str]:
    """Read every file that could name a script or config file, once.

    One pass beats grepping per candidate: the corpus is read a single time and
    every later question is a substring test against text already in memory.
    """
    corpus: dict[Path, str] = {}

    candidates: list[Path] = []
    for directory_name in REFERENCE_DIRECTORIES:
        directory = claude_dir / directory_name
        if directory.is_dir():
            candidates.extend(p for p in directory.rglob("*") if p.is_file())
    for file_name in REFERENCE_FILES:
        top_level_file = claude_dir / file_name
        if top_level_file.is_file():
            candidates.append(top_level_file)

    for candidate in candidates:
        if _is_skipped(candidate):
            continue
        try:
            if candidate.stat().st_size > MAXIMUM_CORPUS_FILE_BYTES:
                continue
            corpus[candidate] = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

    return corpus


def reference_pattern(file_path: Path) -> re.Pattern[str]:
    """The text that counts as naming this file.

    A shell script is only ever reached by its full name. A Python file is
    reached by its module stem too -- `from berth.tests.installed_front_end
    import ...` never writes the extension -- so matching the file name alone
    reports every imported module and every shared helper as dead code.
    """
    if file_path.suffix == ".py":
        return re.compile(rf"\b{re.escape(file_path.stem)}\b")
    return re.compile(re.escape(file_path.name))


def is_referenced(file_path: Path, corpus: dict[Path, str]) -> bool:
    """Whether any file other than this one names it."""
    pattern = reference_pattern(file_path)
    return any(
        pattern.search(text) for path, text in corpus.items() if path != file_path
    )


def resolve_claude_directory(arguments: list[str]) -> Path | None:
    """Pick the .claude directory to scan.

    With no argument this scans the user-level `~/.claude`, which is where
    scripts and config accumulate across every project. Pass a path -- either a
    `.claude` directory or the project holding one -- to scan that instead.
    """
    if not arguments:
        return Path.home() / ".claude"

    given = Path(arguments[0]).expanduser()
    if given.name == ".claude":
        return given if given.is_dir() else None
    nested = given / ".claude"
    return nested if nested.is_dir() else None


def main() -> int:
    """Main entry point."""
    claude_dir = resolve_claude_directory(sys.argv[1:])

    if claude_dir is None or not claude_dir.exists():
        print("Error: .claude directory not found", file=sys.stderr)
        return 1

    scripts_dir = claude_dir / "scripts"
    config_dir = claude_dir / "config"

    expected = load_expected_orphans(claude_dir)
    corpus = read_reference_corpus(claude_dir)

    scripts = find_scripts(scripts_dir)
    config_files = find_config_files(config_dir)

    orphans: list[OrphanInfo] = []

    for script in scripts:
        if not is_referenced(script, corpus):
            rel_path = str(script.relative_to(claude_dir))
            if rel_path not in expected["scripts"]:
                orphans.append({"path": rel_path, "category": "script"})

    for config_file in config_files:
        if not is_referenced(config_file, corpus):
            rel_path = str(config_file.relative_to(claude_dir))
            if rel_path != "config/orphans_expected.json" and rel_path not in expected["config"]:
                orphans.append({"path": rel_path, "category": "config"})

    print(f"Scanned {claude_dir} ({len(scripts)} scripts, {len(config_files)} config files)\n")

    if not orphans:
        print("✓ No orphaned files found")
        return 0

    print(f"Found {len(orphans)} orphaned file(s):\n")

    scripts_orphaned = [o for o in orphans if o["category"] == "script"]
    configs_orphaned = [o for o in orphans if o["category"] == "config"]

    if scripts_orphaned:
        print("Orphaned scripts:")
        for orphan in sorted(scripts_orphaned, key=lambda o: o["path"]):
            print(f"  - {orphan['path']}")
        print()

    if configs_orphaned:
        print("Orphaned config files:")
        for orphan in sorted(configs_orphaned, key=lambda o: o["path"]):
            print(f"  - {orphan['path']}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
