"""Find every file under the state roots that still holds a renamed unit's old names, less history.

rename_unit verify checks each live place it rewrote; this sweep catches the places it does not
know. Every file that holds the old id, branch or worktree path as a whole token must belong to a
history class below, or the sweep names it: a place added later fails verify until it is filed here.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

# Under the state root: logs, reports and archives that keep a unit's old names as history.
STATE_HISTORY = (
    "shutdown/", "nightly-review/", "message/log.jsonl", "message/relay/", "message/keys.json",
    "message/queue/", "message/postmaster/replies/", "notify/", "accounts/moves.jsonl", "showrunner/outstanding/",
    "unit-history/runs/", "unit-history/review_turns/", "plan-delegate/runs/", "plan-delegate/review_turns/", "model-study/", "claude-model-study/",
    "screenshot-analysis/", "plan-backlog/", "usage-model/", "style-backups/",
    # Build logs, hook trial records, the feature log, review prompts, verify events and shell history.
    "buildlog/", "mul-add-hook/", "fn-length-hook/", "claude/feature-log/", "review-pool/", "verify/", "zsh/",
    # Rebuilt from its sources on the next board refresh.
    "unit-board/",
)
# Under a production's own state folder.
PRODUCTION_HISTORY = ("log-backups/", "dailies_last.json", "rename_unit.json", "rename.json")
# Under each Claude config dir: Claude's transcripts, history and caches, and the trust entries,
# which keep the old worktree's entry on purpose.
CONFIG_HISTORY = (
    "projects/", "file-history/", "history.jsonl", "todos/", "shell-snapshots/", "debug/", "plans/",
    "paste-cache/", "sessions/", "statsig/", "backups/", ".claude.json", ".git/", ".claude/worktrees/",
    "data/review_regime.jsonl", "data/review_regime_marks_", "teams/", "jobs/", "feedback/", "state/berth-fix/",
)


class Roots(NamedTuple):
    state: Path
    production_state: Path
    configs: tuple[Path, ...]
    # Checkouts, swept as git sees them: tracked files are history or a place verify reads itself.
    # A config dir that is a repository has its tracked files classified the same way.
    checkouts: tuple[Path, ...]
    # Files with no history in them: every old name they hold is stale.
    live_files: tuple[Path, ...]
    # Path prefixes that are history wherever they are, such as the production log and its backups.
    history_prefixes: tuple[str, ...] = ()


def token_pattern(values: Iterable[str]) -> re.Pattern[str]:
    """Each value as a whole token: `demo-alpha` does not match inside `claude-demo-alphabet`."""
    return re.compile("|".join(rf"(?<![A-Za-z0-9_-]){re.escape(value)}(?![A-Za-z0-9_-])" for value in values))


def _under(path: Path, root: Path, prefixes: Iterable[str]) -> bool:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return False
    return any(relative == prefix.rstrip("/") or relative.startswith(prefix) for prefix in prefixes)


def _tracked(checkout: Path) -> set[Path]:
    result = subprocess.run(["git", "-C", str(checkout), "ls-files", "-z"], capture_output=True, text=True,
                            check=False, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    return {checkout / name for name in result.stdout.split("\0") if name} if result.returncode == 0 else set()


def _history(path: Path, roots: Roots, tracked: set[Path]) -> bool:
    return (path in tracked or any(_under(path, checkout, (".git/",)) for checkout in roots.checkouts)
            or str(path).startswith(roots.history_prefixes)
            or _under(path, roots.state, STATE_HISTORY)
            or _under(path, roots.production_state, PRODUCTION_HISTORY)
            or any(_under(path, config, CONFIG_HISTORY) for config in roots.configs))


def _hits(values: tuple[str, ...], paths: list[Path], *, ignored: bool) -> list[Path]:
    """Files that hold any value as text; ignored also reads files git ignores."""
    existing = [str(path) for path in paths if path.exists()]
    if not existing:
        return []
    flags = ["-uuu"] if ignored else ["--hidden"]
    # Bytecode caches are rebuilt from their sources, so they are never a live place.
    result = subprocess.run(["rg", "-l", "-F", *flags, "--glob", "!__pycache__/", "--glob", "!*.pyc", *(argument for value in values for argument in ("-e", value)),
                             "--", *existing], capture_output=True, text=True, check=False)
    # rg exits 1 when nothing matches, 2 when some file could not be read.
    if result.returncode not in (0, 1, 2):
        raise OSError(f"rg failed: {result.stderr.strip()}")
    return [Path(line) for line in result.stdout.splitlines() if line]


def shown(path: Path, state: Path) -> str:
    """A path to print: shutdown records are named by account, so they stay masked."""
    return "(masked)" if _under(path, state, ("shutdown/",)) else str(path)


def sweep(values: tuple[str, ...], roots: Roots) -> list[str]:
    """The unclassified files that still hold one of the old names, each as a failure line."""
    pattern = token_pattern(values)
    repositories = {*roots.checkouts, *(config for config in roots.configs if (config / ".git").exists())}
    tracked = {path for checkout in repositories for path in _tracked(checkout)}
    found = {*_hits(values, [roots.state, *roots.configs, *roots.live_files], ignored=True),
             *_hits(values, list(roots.checkouts), ignored=False)}
    failures: list[str] = []
    for path in sorted(found):
        if path not in roots.live_files and _history(path, roots, tracked):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            failures.append(f"unclassified: {shown(path, roots.state)} still holds an old name")
    return failures
