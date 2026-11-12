"""Require shared discovery for saved transcripts that retention can compress."""
from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from time import perf_counter
from typing import NamedTuple
import unittest


ALLOW_LIST: dict[tuple[str, str], str] = {
    ("scripts/direct/progress_history.py", "_main_identity_from_codex"):
        "Reads the current CODEX_THREAD_ID session.",
    ("scripts/direct/progress_history.py", "_main_identity_from_claude"):
        "Reads the current CLAUDE_CODE_SESSION_ID session.",
    ("scripts/herdr/herdr.py", "TranscriptCursor.skip_existing_turns"):
        "Records byte offsets for a running worker.",
    ("scripts/herdr/herdr.py", "TranscriptCursor.read_turns"):
        "Reads new turns from a running worker by byte offset.",
    ("scripts/accounts/refusal_watch.py", "session_refusals"):
        "Lists project folders; transcript_paths finds the transcript files.",
}
ADVICE = ("Use scripts.lib.transcripts (transcript_paths and open_transcript), "
          "or add an allow-list entry with the reason.")
ENUMERATIONS = {"glob", "rglob", "walk", "scandir", "iterdir", "listdir"}


class DirectTranscriptEnumeration(NamedTuple):
    path: str
    scope: str
    line: int


def function_scopes(node: ast.AST, prefix: str = "") -> Iterator[tuple[str, list[ast.stmt]]]:
    for child in ast.iter_child_nodes(node):
        name = prefix
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{prefix}.{child.name}" if prefix else child.name
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield name, child.body
        yield from function_scopes(child, name)


def scope_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def names_transcript_folder(text: str) -> bool:
    return (text in {"projects", "sessions", "archived_sessions"}
            or ".claude/projects" in text or ".codex/sessions" in text
            or text.startswith(("projects/", "sessions/")))


def enumerates_transcripts(call: ast.Call) -> bool:
    name = (call.func.attr if isinstance(call.func, ast.Attribute) else
            call.func.id if isinstance(call.func, ast.Name) else "")
    if name not in ENUMERATIONS:
        return False
    if name in {"glob", "rglob"} and call.args:
        pattern = call.args[0]
        if isinstance(pattern, ast.Constant) and isinstance(pattern.value, str):
            # Registry files and account folders are not transcript files.
            if pattern.value.endswith(".json") or pattern.value in {".claude*", ".codex*"}:
                return False
    return True


def direct_enumerations(path: str, source: str) -> list[DirectTranscriptEnumeration]:
    if ".jsonl" not in source:
        return []
    tree = ast.parse(source, filename=path)
    found: list[DirectTranscriptEnumeration] = []
    for name, body in (("<module>", tree.body), *function_scopes(tree)):
        nodes = list(scope_nodes(body))
        strings = [node.value for node in nodes if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        if not any(names_transcript_folder(text) for text in strings) or not any(".jsonl" in text for text in strings):
            continue
        lines = [node.lineno for node in nodes if isinstance(node, ast.Call) and enumerates_transcripts(node)]
        if lines:
            found.append(DirectTranscriptEnumeration(path, name, min(lines)))
    return found


def guard_failures(found: list[DirectTranscriptEnumeration], allowed: dict[tuple[str, str], str]) -> list[str]:
    failures = [f"{item.path}:{item.line}: {item.scope}: {ADVICE}"
                for item in found if (item.path, item.scope) not in allowed]
    present = {(item.path, item.scope) for item in found}
    failures.extend(f"{path}: {scope}: stale allow-list entry."
                    for path, scope in sorted(allowed.keys() - present))
    return failures


class TranscriptReaderGuardTests(unittest.TestCase):
    def test_direct_discovery_shared_discovery_and_allow_list(self) -> None:
        direct = ('def read():\n'
                  '    root = Path("projects")\n'
                  '    return root.glob("*/<id>.jsonl")\n')
        shared = ('def read():\n'
                  '    root = Path("projects")\n'
                  '    return transcript_paths(root, "*/<id>.jsonl")\n')
        cases: list[tuple[str, str, dict[tuple[str, str], str], list[str]]] = [
            ("direct", direct, {}, [f"example.py:3: read: {ADVICE}"]),
            ("shared", shared, {}, []),
            ("allowed", direct, {("example.py", "read"): "Reads the current session."}, []),
            ("stale", shared, {("example.py", "read"): "Reads the current session."},
             ["example.py: read: stale allow-list entry."]),
            ("separate functions", 'def folders():\n    return "projects"\n' +
             'def files():\n    return root.glob("*.jsonl")\n', {}, []),
            ("method", 'class Reader:\n    def read(self):\n' +
             '        return (Path("sessions")).glob("*.jsonl")\n', {},
             [f"example.py:3: Reader.read: {ADVICE}"]),
        ]
        for name, source, allowed, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(guard_failures(direct_enumerations("example.py", source), allowed), expected)

    def test_repository_readers_use_shared_transcript_discovery(self) -> None:
        root = Path(__file__).resolve().parents[2]
        start = perf_counter()
        found: list[DirectTranscriptEnumeration] = []
        parsed = 0
        for path in sorted((root / "scripts").rglob("*.py")):
            if path.name.startswith("test_") or path.stem.endswith("_test_support"):
                continue
            relative = path.relative_to(root).as_posix()
            if relative == "scripts/lib/transcripts.py":
                continue
            source = path.read_text(encoding="utf-8")
            if ".jsonl" not in source:
                continue
            parsed += 1
            found.extend(direct_enumerations(relative, source))
        elapsed = perf_counter() - start
        print(f"Transcript reader scan: {parsed} files parsed; {len(found)} scopes flagged; {elapsed:.3f} seconds.")
        failures = guard_failures(found, ALLOW_LIST)
        self.assertEqual(failures, [], "\n" + "\n".join(failures))
