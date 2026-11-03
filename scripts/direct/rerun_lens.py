#!/usr/bin/env python3
"""Find changed operations that need a review of failure and rerun behavior."""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast


@dataclass(frozen=True)
class DiffLine:
    path: str
    number: int
    text: str
    added: bool


@dataclass(frozen=True)
class StateWriteHit:
    path: str
    line: int
    kind: str


def production_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = PurePosixPath(path).name.lower()
    return not (
        any(part.lower() in {"test", "tests", "__tests__", "fixtures", "fixture", "docs", "doc"} for part in parts)
        or name.startswith(("test_", "test-", "fixture_"))
        or name == "conftest.py" or name.endswith("_test_support.py")
        or PurePosixPath(name).stem in {"test", "tests", "fixture", "fixtures"}
        or re.search(r"(?:_tests?|\.test|\.spec|_fixture)\.", name)
        or PurePosixPath(name).suffix in {".md", ".rst", ".txt", ".adoc"}
    )


def changed_hunks(diff: str) -> list[list[DiffLine]]:
    hunks: list[list[DiffLine]] = []
    path = ""
    number = 0
    active = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            active = False
        elif line.startswith("+++ "):
            path = line[4:].split("\t", 1)[0]
            if path.startswith('"'):
                # Git quotes paths that contain spaces or escaped characters.
                path = cast(str, ast.literal_eval(path))
            path = path.removeprefix("b/")
            active = False
        elif match := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line):
            number = int(match[1])
            active = path != "/dev/null" and production_path(path)
            if active:
                hunks.append([])
        elif active and line.startswith(("+", " ")):
            hunks[-1].append(DiffLine(path, number, line[1:], line.startswith("+")))
            number += 1
    return hunks


# Match executable code. Literal contents are handled only by the file-open
# checks, where a mode or a lock path is an argument to an actual operation.
OPERATIONS: tuple[tuple[str, str], ...] = (
    ("file write or append", r"\.(?:write_text|write_bytes|write_all)\s*\(|\b(?:std::)?fs::write\s*\(|\bFile::create\s*\("),
    ("file write or append", r"\bO_(?:WRONLY|RDWR|CREAT|APPEND)\b"),
    ("file write or append", r"(?<![\w.])(?!(?:sys\.)?(?:stdout|stderr)\b)[A-Za-z_]\w*(?:\.\w+)*\.write\s*\("),
    ("file move or delete", r"\b(?:os\.replace|os\.rename|shutil\.move|shutil\.rmtree)\s*\(|\.(?:rename|unlink|rmdir)\s*\(|\b(?:std::)?fs::(?:rename|remove_file|remove_dir_all)\s*\("),
    ("lock or claim", r"\bfcntl\.flock\s*\(|\b(?:fs2|fd_lock|fd-lock)\b"),
    ("JSON state write", r"\bjson\.dump\s*\(|\b(?:write_json|save_json|append_jsonl)\s*\("),
)


def executable_source(source: str, suffix: str) -> str:
    """Blank literals and comments while keeping offsets and newlines."""
    strings = r'r(?P<hashes>\#+)"[\s\S]*?"(?P=hashes)|"{3}[\s\S]*?"{3}|\'{3}[\s\S]*?\'{3}|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\\n])*\''
    comments = r"|//[^\n]*|/\*[\s\S]*?\*/" if suffix == ".rs" else r"|\#[^\n]*"
    return re.sub(strings + comments, lambda match: re.sub(r"[^\n]", " ", match[0]), source)


def rust_test_lines(lines: list[str]) -> set[int]:
    """Exclude a cfg(test) item through its closing brace, including nested items."""
    excluded: set[int] = set()
    pending = False
    depth = 0
    for index, code in enumerate(lines):
        if re.search(r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]", code):
            pending = True
        if pending or depth:
            excluded.add(index)
            depth += code.count("{") - code.count("}")
            if "{" in code or ";" in code:
                pending = False
    return excluded


def path_replacement(code: str, context: str) -> bool:
    if re.search(r"\b(?:pathlib\.)?Path\s*\([^)]*\)\.replace\s*\(", code):
        return True
    for match in re.finditer(r"\b([\w.]+)\.replace\s*\(", code):
        receiver = re.escape(match[1])
        # A path annotation, constructor, or filesystem method identifies the
        # receiver. A text replace call alone cannot establish a file move.
        if re.search(rf"\b{receiver}\s*:\s*(?:pathlib\.)?Path\b|\b{receiver}\s*=\s*(?:pathlib\.)?Path\s*\(", context):
            return True
        if re.search(rf"\b{receiver}\.(?:with_suffix|with_name|exists|stat|is_file|write_text|write_bytes|unlink)\s*\(", context):
            return True
        if re.search(rf"\b{receiver}\s*=\s*[\w.]+\.(?:with_suffix|with_name|joinpath|resolve|absolute)\s*\(", context):
            return True
    return False


def python_open_lines(source: str, code: str) -> set[int]:
    writes: set[int] = set()
    for call in re.finditer(r"\b(?:open|fdopen)\s*\([^)]*\)", code):
        arguments = source[call.start():call.end()]
        if re.search(r"[\"'](?:[wax][bt+]*|r[bt]?\+)[\"']", arguments):
            first = code.count("\n", 0, call.start())
            last = code.count("\n", 0, call.end())
            writes.update(range(first, last + 1))
    return writes


def fail_fast_lines(lines: list[DiffLine], code: list[str]) -> set[int]:
    errexit = [index for index, text in enumerate(code)
               if re.search(r"\bset\s+(?:-[a-zA-Z]*e[a-zA-Z]*\b|-o\s+errexit\b)", text)]
    if not errexit:
        return set()
    changed_steps: set[int] = set()
    depth = 0
    has_loop = False
    for index, text in enumerate(code):
        if re.search(r"\b(?:for\s+\w+\s+in|while\b)", text):
            depth += 1
            has_loop = True
        if depth and lines[index].added and text.strip():
            changed_steps.add(index)
        if re.search(r"\bdone\b", text):
            depth = max(0, depth - 1)
    if has_loop and (changed_steps or any(lines[index].added for index in errexit)):
        changed_steps.add(errexit[0])
    return changed_steps


def arm(diff: str) -> tuple[StateWriteHit, ...]:
    hits: list[StateWriteHit] = []
    files: dict[str, list[DiffLine]] = {}
    for hunk in changed_hunks(diff):
        if hunk:
            files.setdefault(hunk[0].path, []).extend(hunk)
    for path, lines in files.items():
        suffix = PurePosixPath(path).suffix
        source = "\n".join(line.text for line in lines)
        code = executable_source(source, suffix)
        code_lines = code.split("\n")
        excluded = rust_test_lines(code_lines) if suffix == ".rs" else set[int]()
        context = "\n".join(text for index, text in enumerate(code_lines) if index not in excluded)
        open_writes = python_open_lines(source, code) if suffix == ".py" else set[int]()
        shell = suffix in {".sh", ".bash", ".zsh"} or bool(re.search(r"^#!.*\b(?:ba|z)?sh\b", source, re.MULTILINE))
        fail_fast = fail_fast_lines(lines, code_lines) if shell else set[int]()
        for index, line in enumerate(lines):
            if index in fail_fast:
                hits.append(StateWriteHit(path, line.number, "fail-fast list"))
            text = code_lines[index]
            if not line.added or index in excluded or not text.strip():
                continue
            kinds = {kind for kind, pattern in OPERATIONS if re.search(pattern, text)}
            if suffix == ".py":
                if index in open_writes:
                    kinds.add("file write or append")
                if path_replacement(text, context):
                    kinds.add("file move or delete")
            if (re.search(r"\b(?:open|create)\s*\(|\.(?:write_text|write_bytes)\s*\(", text)
                    and re.search(r"[\"'][^\"'\n]*\.(?:lock|claim)[\"']", line.text)):
                kinds.add("lock or claim")
            if suffix == ".rs":
                nearby = "\n".join(code_lines[max(0, index - 8):index + 9])
                if ("OpenOptions" in nearby and re.search(r"\.(?:append|write)\s*\(\s*true\s*\)", nearby)
                        and re.search(r"OpenOptions|\.(?:append|write)\s*\(", text)):
                    kinds.add("file write or append")
                if re.search(r"\.(?:try_)?lock_(?:exclusive|shared)\s*\(", text):
                    kinds.add("lock or claim")
                if (re.search(r"\b(?:use|extern\s+crate)\s+fd_lock\b", context)
                        and re.search(r"\.(?:try_)?(?:write|read)\s*\(\s*\)", text)):
                    kinds.add("lock or claim")
            if shell:
                if re.search(r"(?:^|[;&|]\s*|\s)(?:mv|rm)\s+", text):
                    kinds.add("file move or delete")
                if re.search(r">", text) and re.search(r"(?<![<>&])>{1,2}\s*(?!&|/dev/null\b|/dev/stderr\b|/dev/stdout\b)[\w\"'$./{]", line.text):
                    kinds.add("file write or append")
                if (re.search(r"\bmkdir\b", text) and re.search(r"lock|claim", line.text)) or re.search(r"\bflock\b", text):
                    kinds.add("lock or claim")
            hits.extend(StateWriteHit(path, line.number, kind) for kind in sorted(kinds))
    return tuple(hits)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scanner = commands.add_parser("arm")
    _ = scanner.add_argument("--diff", type=Path, required=True)
    args = parser.parse_args()
    try:
        hits = arm(cast(Path, args.diff).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, SyntaxError) as exc:
        print(f"rerun_lens: {exc}", file=sys.stderr)
        return 2
    print("armed" if hits else "not armed")
    for hit in hits:
        print(f"{hit.path}:{hit.line} — {hit.kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
