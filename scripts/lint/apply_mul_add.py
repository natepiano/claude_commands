#!/usr/bin/env python3
"""Apply only machine-applicable suboptimal_flops spans from Cargo JSON.

Exit 0 after applying changes, 3 (NOTHING_APPLIED) when no changes apply,
or 1 when the diagnostic stream is incomplete or a read/write fails.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict, cast


NOTHING_APPLIED = 3


class SuggestionSpan(TypedDict):
    file_name: str
    byte_start: int
    byte_end: int
    suggested_replacement: str | None
    suggestion_applicability: str | None


class LintCode(TypedDict):
    code: str


class LintDiagnostic(TypedDict):
    code: NotRequired[LintCode | None]
    spans: list[SuggestionSpan]
    children: NotRequired[list[LintDiagnostic]]


class CargoDiagnosticRecord(TypedDict):
    reason: str
    message: NotRequired[LintDiagnostic]


@dataclass(frozen=True)
class MulAddReplacement:
    path: Path
    start: int
    end: int
    replacement: bytes


@dataclass(frozen=True)
class RewriteTotals:
    replacements: int
    files: int
    overlapping: int

    def report(self) -> str:
        line = (f'verify.sh: rewrote {self.replacements} multiply-add(s) '
                f'in {self.files} file(s)')
        if self.overlapping:
            line += f' ({self.overlapping} skipped: overlapping)'
        return line


def read_replacements(root: Path, diagnostics: Path) -> list[MulAddReplacement]:
    replacements: list[MulAddReplacement] = []
    seen: set[MulAddReplacement] = set()
    build_finished = False
    with diagnostics.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            record = cast(CargoDiagnosticRecord, json.loads(line))
            if record['reason'] == 'build-finished':
                build_finished = True
            if record['reason'] != 'compiler-message':
                continue
            message = record.get('message')
            if message is None:
                raise ValueError('compiler-message record has no diagnostic')
            code = message.get('code')
            if code is None or code['code'] != 'clippy::suboptimal_flops':
                continue
            for section in [message, *message.get('children', [])]:
                for span in section['spans']:
                    text = span.get('suggested_replacement')
                    if span.get('suggestion_applicability') != 'MachineApplicable' or text is None:
                        continue
                    path = (root / span['file_name']).resolve()
                    if not path.is_relative_to(root):
                        continue
                    start, end = span['byte_start'], span['byte_end']
                    if not 0 <= start <= end:
                        raise ValueError(f'invalid byte range in {path}: {start}..{end}')
                    replacement = MulAddReplacement(path, start, end, text.encode('utf-8'))
                    if replacement not in seen:
                        seen.add(replacement)
                        replacements.append(replacement)
    if not build_finished:
        raise ValueError('diagnostic stream has no build-finished record')
    return replacements


def overlaps(left: MulAddReplacement, right: MulAddReplacement) -> bool:
    if left.start == right.start:
        return True
    return left.start < right.end and right.start < left.end


def replace_file(path: Path, content: bytes) -> None:
    # A failed temporary write leaves the original file intact, including mode.
    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.mul-add-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            _ = stream.write(content)
            stream.flush()
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def apply_replacements(replacements: list[MulAddReplacement]) -> RewriteTotals:
    by_file: dict[Path, list[MulAddReplacement]] = {}
    skipped = 0
    for replacement in replacements:
        accepted = by_file.setdefault(replacement.path, [])
        if any(overlaps(replacement, prior) for prior in accepted):
            skipped += 1
        else:
            accepted.append(replacement)
    # Read and validate every file before writing any, so bad ranges cannot
    # leave part of the stream applied.
    updates: dict[Path, bytes] = {}
    changed = 0
    for path, spans in by_file.items():
        original = path.read_bytes()
        content = original
        file_changes = 0
        for span in sorted(spans, key=lambda item: item.start, reverse=True):
            if span.end > len(original):
                raise ValueError(f'byte range exceeds {path}: {span.start}..{span.end}')
            if original[span.start:span.end] != span.replacement:
                content = content[:span.start] + span.replacement + content[span.end:]
                file_changes += 1
        if content != original:
            updates[path] = content
            changed += file_changes
    for path, content in updates.items():
        replace_file(path, content)
    return RewriteTotals(changed, len(updates), skipped)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--workspace-root', required=True, type=Path)
    _ = parser.add_argument('diagnostics', type=Path)
    args = parser.parse_args()
    root = cast(Path, args.workspace_root).resolve()
    diagnostics = cast(Path, args.diagnostics)
    try:
        totals = apply_replacements(read_replacements(root, diagnostics))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f'verify.sh: multiply-add suggestions could not be applied: {error}', file=sys.stderr)
        return 1
    print(totals.report())
    return 0 if totals.replacements else NOTHING_APPLIED


if __name__ == '__main__':
    raise SystemExit(main())
