#!/usr/bin/env python3
"""Shared source-evidence hash for Hanadocs prioritization reviews."""

from __future__ import annotations

import hashlib
from pathlib import Path

import renumber  # pyright: ignore[reportImplicitRelativeImport]


def _bytes_without_fields(
    source: renumber.SourceFile,
    excluded_fields: tuple[str, ...],
) -> bytes:
    frontmatter = renumber.parse_frontmatter(source)
    top_level_indices = sorted(
        occurrence.index
        for occurrences in frontmatter.fields.values()
        for occurrence in occurrences
    )
    excluded_indices = {
        occurrence.index
        for key in excluded_fields
        for occurrence in frontmatter.fields.get(key, [])
    }
    for key in excluded_fields:
        for occurrence in frontmatter.fields.get(key, []):
            next_index = next(
                (
                    candidate
                    for candidate in top_level_indices
                    if candidate > occurrence.index
                ),
                frontmatter.closing_index,
            )
            excluded_indices.update(
                index
                for index in range(occurrence.index + 1, next_index)
                if frontmatter.lines[index].strip()
                and not frontmatter.lines[index].lstrip().startswith("#")
            )
    return "".join(
        line
        for index, line in enumerate(frontmatter.lines)
        if index not in excluded_indices
    ).encode("utf-8")


def canonical_review_bytes(source: renumber.SourceFile) -> bytes:
    """Return source bytes without mechanically derived score/rank properties."""

    return _bytes_without_fields(source, renumber.GENERATED_FIELDS)


def canonical_evidence_bytes(source: renumber.SourceFile) -> bytes:
    """Return issue evidence without judgment or derived ranking metadata."""

    return _bytes_without_fields(
        source,
        ("strategic_goal", *renumber.RUBRIC_FIELDS, *renumber.GENERATED_FIELDS),
    )


def review_hash_for_source(source: renumber.SourceFile) -> str:
    """Hash all review evidence while ignoring watcher-owned output fields."""

    return hashlib.sha256(canonical_review_bytes(source)).hexdigest()


def review_hash_for_content(path: Path, content: bytes) -> str:
    """Return the review hash for prepared bytes that are not yet on disk."""

    source = renumber.SourceFile(
        path=path,
        content=content,
        mode=0,
        signature=(0, 0, 0, 0, 0),
        digest=hashlib.sha256(content).hexdigest(),
    )
    return review_hash_for_source(source)


def evidence_hash_for_source(source: renumber.SourceFile) -> str:
    """Hash issue evidence while ignoring review- and watcher-owned metadata."""

    return hashlib.sha256(canonical_evidence_bytes(source)).hexdigest()


def review_hash_for_path(path: Path) -> str:
    """Read a regular, non-symlink note stably and return its review hash."""

    return review_hash_for_source(renumber._read_source(path))
