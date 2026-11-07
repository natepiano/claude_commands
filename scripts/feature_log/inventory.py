#!/usr/bin/env python3
"""Freeze backfill evidence and track each source record's review outcome."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections import Counter
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple, NoReturn, TypedDict, cast

from feature_log import PROJECTS, locked, state_dir, vault_dir


RecordKind = Literal["phase", "asbuilt", "changelog", "tag", "merge", "commit"]
Pool = Literal["structured", "commits"]
ProposalCategory = Literal[
    "phase", "asbuilt", "changelog", "tag", "merge", "commit", "documents", "files", "total"
]
ProposalCounts = dict[Pool, dict[ProposalCategory, int]]
HistoryKind = Literal["commit", "merge"]
Disposition = Literal["represented", "combined", "internal", "not-current", "unresolved"]
KINDS: tuple[RecordKind, ...] = ("phase", "asbuilt", "changelog", "tag", "merge", "commit")
POOLS: tuple[Pool, ...] = ("structured", "commits")
DISPOSITIONS: tuple[Disposition, ...] = (
    "represented", "combined", "internal", "not-current", "unresolved"
)


class Unclaimed(TypedDict):
    state: Literal["unclaimed"]


class ClaimedBy(TypedDict):
    state: Literal["claimed"]
    worker: str


ClaimState = Unclaimed | ClaimedBy


class Undisposed(TypedDict):
    state: Literal["undisposed"]


class NoteOutcome(TypedDict):
    state: Literal["represented", "combined"]
    note: str


class NoNoteOutcome(TypedDict):
    state: Literal["internal", "not-current", "unresolved"]
    reason: str


ReviewOutcome = Undisposed | NoteOutcome | NoNoteOutcome


class BackfillSourceRecord(TypedDict):
    id: str
    project: str
    repo: str
    ref: str
    kind: RecordKind
    pool: Pool
    title: str
    date: str
    source: str
    claim: ClaimState
    outcome: ReviewOutcome


class EntireHistory(NamedTuple):
    """Include every ancestor of the frozen branch tip."""


class AfterRef(NamedTuple):
    """Exclude ancestors of this ref from the branch's history pool."""

    excluded_ref: str


class DocumentSource(NamedTuple):
    """Phase blocks and standalone as-built documents in the structured pool."""

    repo: str
    branch: str


class ChangelogSource(NamedTuple):
    """Versioned top-level bullets in the structured pool."""

    repo: str
    branch: str
    globs: tuple[str, ...]


class TagSource(NamedTuple):
    """Tags reachable from a frozen tip, in the named pool."""

    repo: str
    branch: str
    pool: Pool


class HistorySource(NamedTuple):
    """Commit and first-parent merge records in the commits pool."""

    repo: str
    branch: str
    history: EntireHistory | AfterRef
    kinds: tuple[HistoryKind, ...]


RepositorySource = DocumentSource | ChangelogSource | TagSource | HistorySource


class ProjectSources(NamedTuple):
    sources: tuple[RepositorySource, ...]
    proposal_counts: ProposalCounts


SourceTable = dict[str, ProjectSources]
_RUST = Path.home() / "rust"
SOURCES: SourceTable = {
    "hana": ProjectSources(
        (
            DocumentSource(str(_RUST / "hana_catalyst"), "init/catalyst"),
            HistorySource(str(_RUST / "hana"), "main", EntireHistory(), ("merge",)),
            HistorySource(str(_RUST / "hana_catalyst"), "init/catalyst", AfterRef("origin/main"), ("merge",)),
        ),
        {"structured": {"phase": 200, "asbuilt": 58}, "commits": {"merge": 409}},
    ),
    "natedev": ProjectSources(
        (HistorySource("/etc/nixos", "main", EntireHistory(), ("commit", "merge")),
         TagSource("/etc/nixos", "main", "commits")),
        {"structured": {"total": 0}, "commits": {"commit": 973, "merge": 1, "tag": 1}},
    ),
    "cargo-liner": ProjectSources(
        (DocumentSource(str(_RUST / "cargo-liner"), "main"),
         ChangelogSource(str(_RUST / "cargo-liner"), "main", ("crates/*/CHANGELOG.md",)),
         TagSource(str(_RUST / "cargo-liner"), "main", "structured"),
         HistorySource(str(_RUST / "cargo-liner"), "main", EntireHistory(), ("commit", "merge"))),
        {"structured": {"phase": 11, "asbuilt": 15, "changelog": 755, "tag": 30},
         "commits": {"commit": 2156, "merge": 76}},
    ),
    "bevy_brp": ProjectSources(
        (DocumentSource(str(_RUST / "bevy_brp"), "main"),
         ChangelogSource(str(_RUST / "bevy_brp"), "main", ("mcp/CHANGELOG.md", "mcp_macros/CHANGELOG.md", "extras/CHANGELOG.md")),
         TagSource(str(_RUST / "bevy_brp"), "main", "structured"),
         HistorySource(str(_RUST / "bevy_brp"), "main", EntireHistory(), ("commit", "merge"))),
        {"structured": {"asbuilt": 4, "changelog": 205, "tag": 25},
         "commits": {"commit": 2558, "merge": 12}},
    ),
    "obsidian_knife": ProjectSources(
        (HistorySource(str(_RUST / "obsidian_knife"), "main", EntireHistory(), ("commit", "merge")),),
        {"structured": {"total": 0}, "commits": {"commit": 562, "merge": 2}},
    ),
    "nateroids": ProjectSources(
        (DocumentSource(str(_RUST / "nateroids"), "main"),
         HistorySource(str(_RUST / "nateroids"), "main", EntireHistory(), ("commit", "merge"))),
        {"structured": {"asbuilt": 2}, "commits": {"commit": 553, "merge": 2}},
    ),
}


class InventoryError(ValueError):
    """A refused command or invalid frozen ledger."""


def _git(repo: str, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *arguments], capture_output=True, text=True, check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode:
        reason = " ".join(result.stderr.split()) or f"git exited {result.returncode}"
        raise InventoryError(f"{repo}: {reason}")
    return result.stdout


def _freeze(repo: str, branch: str) -> str:
    ref = _git(repo, "rev-parse", "--verify", f"{branch}^{{commit}}").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise InventoryError(f"{repo}: {branch} did not resolve to a 40-hex commit")
    return ref


def _record(project: str, repo: str, ref: str, kind: RecordKind, pool: Pool,
            key: str, title: str, completed: str, source: str) -> BackfillSourceRecord:
    return {
        "id": f"{project}:{kind}:{key}", "project": project, "repo": repo,
        "ref": ref, "kind": kind, "pool": pool, "title": title, "date": completed,
        "source": source, "claim": {"state": "unclaimed"}, "outcome": {"state": "undisposed"},
    }


class PhaseBlock(NamedTuple):
    heading: str
    line: int
    text: str


class ChangelogBullet(NamedTuple):
    version: str
    heading: str
    number: int
    line: int
    title: str
    text: str


def _markdown_lines(text: str) -> list[tuple[int, str]]:
    """Expose real Markdown lines, excluding fenced examples."""
    result: list[tuple[int, str]] = []
    fence = ""
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            delimiter = marker[1]
            if not fence:
                fence = delimiter
            elif delimiter[0] == fence[0] and len(delimiter) >= len(fence):
                fence = ""
            continue
        if not fence:
            result.append((number, line))
    return result


def _phase_blocks(text: str) -> list[PhaseBlock]:
    lines = text.splitlines()
    markdown = _markdown_lines(text)
    blocks: list[PhaseBlock] = []
    phase = ""
    for index, (number, line) in enumerate(markdown):
        heading = re.match(r"^###\s+(Phase\b.*?)(?:\s+#+)?\s*$", line)
        if heading:
            phase = heading[1].strip()
        if not re.fullmatch(r"####\s+As-built(?:\s+#+)?\s*", line):
            continue
        if not phase:
            raise InventoryError(f"As-built block at line {number} has no preceding ### Phase heading")
        end = next((n - 1 for n, following in markdown[index + 1:]
                    if re.match(r"^#{1,4}\s+", following)), len(lines))
        blocks.append(PhaseBlock(phase, number, "\n".join(lines[number - 1:end])))
    return blocks


def _changelog_bullets(text: str) -> list[ChangelogBullet]:
    lines = text.splitlines()
    markdown = _markdown_lines(text)
    bullets: list[ChangelogBullet] = []
    version = ""
    heading = ""
    count = 0
    for index, (number, line) in enumerate(markdown):
        matched = re.match(r"^##\s+\[([^\]]+)\]", line)
        if matched:
            version, heading, count = matched[1], line, 0
        elif re.match(r"^#{1,2}\s+", line):
            version = ""
        if not version or not line.startswith("- "):
            continue
        count += 1
        end = next((n - 1 for n, following in markdown[index + 1:]
                    if following.startswith("- ") or re.match(r"^#{1,6}\s+", following)), len(lines))
        bullets.append(ChangelogBullet(version, heading, count, number, line[2:].strip(),
                                       "\n".join(lines[number - 1:end]).rstrip()))
    return bullets


def _file_date(repo: str, ref: str, path: str) -> str:
    completed = _git(repo, "log", "-1", "--format=%cs", ref, "--", path).strip()
    if not completed:
        raise InventoryError(f"{repo}: no commit date for {path} at {ref}")
    return completed


def _read_frozen_files(repo: str, ref: str, paths: list[str]) -> dict[str, str]:
    """Read the selected blobs in one process, preserving their byte framing."""
    if not paths:
        return {}
    requests = b"".join(f"{ref}:{path}".encode("utf-8") + b"\0" for path in paths)
    result = subprocess.run(
        ["git", "-C", repo, "cat-file", "--batch", "-z"], input=requests,
        capture_output=True, check=False, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode:
        reason = " ".join(result.stderr.decode("utf-8", errors="replace").split())
        raise InventoryError(f"{repo}: {reason or f'git exited {result.returncode}'}")
    replies = BytesIO(result.stdout)
    files: dict[str, str] = {}
    for path in paths:
        header = replies.readline().removesuffix(b"\n")
        if header.endswith(b" missing"):
            raise InventoryError(f"{repo}: missing frozen source {path} at {ref}")
        fields = header.split(b" ")
        if len(fields) != 3 or fields[1] != b"blob" or not fields[2].isdigit():
            raise InventoryError(f"{repo}: invalid blob reply for {path} at {ref}")
        size = int(fields[2])
        contents = replies.read(size)
        if len(contents) != size or replies.read(1) != b"\n":
            raise InventoryError(f"{repo}: truncated blob reply for {path} at {ref}")
        try:
            files[path] = contents.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InventoryError(f"{repo}: {path} at {ref} is not UTF-8") from error
    if replies.read(1):
        raise InventoryError(f"{repo}: unexpected data after frozen blob replies")
    return files


def _matches_path(path: str, pattern: str) -> bool:
    components = path.split("/")
    patterns = pattern.split("/")
    return len(components) == len(patterns) and all(
        fnmatch.fnmatchcase(component, glob) for component, glob in zip(components, patterns)
    )


def _collect_documents(project: str, source: DocumentSource, ref: str) -> list[BackfillSourceRecord]:
    records: list[BackfillSourceRecord] = []
    paths = [path for path in _git(source.repo, "ls-tree", "-r", "-z", "--name-only",
                                   ref, "--", "docs").split("\0")
             if path.startswith("docs/") and path.endswith(".md")]
    for path, text in _read_frozen_files(source.repo, ref, paths).items():
        blocks = _phase_blocks(text)
        if not blocks and PurePosixPath(path).parent.name != "as-built":
            continue
        completed = _file_date(source.repo, ref, path)
        if blocks:
            for block in blocks:
                records.append(_record(project, source.repo, ref, "phase", "structured",
                                       f"{path}#{block.heading}", block.heading, completed, f"{path}:{block.line}"))
        else:
            headings = [(number, re.match(r"^#{1,6}\s+(.+?)(?:\s+#+)?\s*$", line))
                        for number, line in _markdown_lines(text)]
            first = next(((number, match[1].strip()) for number, match in headings if match), (1, path))
            records.append(_record(project, source.repo, ref, "asbuilt", "structured",
                                   path, first[1], completed, f"{path}:{first[0]}"))
    return records


def _collect_changelog(project: str, source: ChangelogSource, ref: str) -> list[BackfillSourceRecord]:
    records: list[BackfillSourceRecord] = []
    paths = [path for path in _git(source.repo, "ls-tree", "-r", "-z", "--name-only", ref).split("\0")
             if path and any(_matches_path(path, pattern) for pattern in source.globs)]
    for path, text in _read_frozen_files(source.repo, ref, paths).items():
        bullets = _changelog_bullets(text)
        if not bullets:
            continue
        completed = _file_date(source.repo, ref, path)
        for bullet in bullets:
            records.append(_record(project, source.repo, ref, "changelog", "structured",
                                   f"{path}@{bullet.version}#{bullet.number}", bullet.title, completed,
                                   f"{path}:{bullet.line}"))
    return records


def _collect_tags(project: str, source: TagSource, ref: str) -> list[BackfillSourceRecord]:
    output = _git(source.repo, "for-each-ref", f"--merged={ref}",
                  "--format=%(refname:strip=2)%00%(*objectname)%00%(objectname)%00%(creatordate:short)", "refs/tags")
    records: list[BackfillSourceRecord] = []
    for line in output.splitlines():
        tag, peeled, object_sha, completed = line.split("\0", 3)
        sha = peeled or object_sha
        records.append(_record(project, source.repo, ref, "tag", source.pool, tag, tag, completed, sha))
    return records


def _collect_history(project: str, source: HistorySource, ref: str) -> list[BackfillSourceRecord]:
    history_range = ref
    if isinstance(source.history, AfterRef):
        history_range = f"{_freeze(source.repo, source.history.excluded_ref)}..{ref}"
    records: list[BackfillSourceRecord] = []
    for kind in source.kinds:
        flags = ("--first-parent", "--merges") if kind == "merge" else ("--no-merges",)
        output = _git(source.repo, "log", *flags, "--format=%H%x00%cs%x00%s", history_range, "--")
        for line in output.splitlines():
            sha, completed, title = line.split("\0", 2)
            records.append(_record(project, source.repo, ref, kind, "commits", sha, title, completed, sha))
    return records


def _collect(project: str, configuration: ProjectSources) -> list[BackfillSourceRecord]:
    records: dict[str, BackfillSourceRecord] = {}
    frozen: dict[tuple[str, str], str] = {}
    for configured in configuration.sources:
        source = configured._replace(repo=str(Path(configured.repo).expanduser().resolve()))
        ref_key = (source.repo, source.branch)
        if ref_key not in frozen:
            frozen[ref_key] = _freeze(*ref_key)
        ref = frozen[ref_key]
        if isinstance(source, DocumentSource):
            collected = _collect_documents(project, source, ref)
        elif isinstance(source, ChangelogSource):
            collected = _collect_changelog(project, source, ref)
        elif isinstance(source, TagSource):
            collected = _collect_tags(project, source, ref)
        else:
            collected = _collect_history(project, source, ref)
        for record in collected:
            previous = records.get(record["id"])
            if previous and record["kind"] not in ("merge", "commit"):
                raise InventoryError(f"duplicate source record {record['id']}")
            _ = records.setdefault(record["id"], record)
    return sorted(records.values(), key=lambda record: (record["date"], record["id"]))


def _ledger_path(project: str) -> Path:
    if project not in PROJECTS:
        raise InventoryError(f"unknown project {project}")
    return state_dir() / "backfill" / f"{project}.jsonl"


def _validate_record(value: object, project: str) -> BackfillSourceRecord:
    if not isinstance(value, dict):
        raise InventoryError("ledger record must be an object")
    fields = cast(dict[str, object], value)
    strings = ("id", "project", "repo", "ref", "kind", "pool", "title", "date", "source")
    if set(fields) != {*strings, "claim", "outcome"} or any(not isinstance(fields[key], str) for key in strings):
        raise InventoryError("ledger record has invalid fields")
    if (fields["project"] != project or fields["kind"] not in KINDS or fields["pool"] not in POOLS
            or not cast(str, fields["id"]).startswith(f"{project}:{fields['kind']}:")
            or not re.fullmatch(r"[0-9a-f]{40}", cast(str, fields["ref"]))
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cast(str, fields["date"]))
            or not Path(cast(str, fields["repo"])).is_absolute()):
        raise InventoryError("ledger record has invalid project, kind, pool, ref, date or repository")
    for key in ("claim", "outcome"):
        raw = fields[key]
        if not isinstance(raw, dict):
            raise InventoryError(f"ledger record has invalid {key}")
        tagged = cast(dict[str, object], raw)
        state = tagged.get("state")
        if key == "claim":
            required = {"unclaimed": {"state"}, "claimed": {"state", "worker"}}
        else:
            required = {"undisposed": {"state"}, "represented": {"state", "note"},
                        "combined": {"state", "note"}, "internal": {"state", "reason"},
                        "not-current": {"state", "reason"}, "unresolved": {"state", "reason"}}
        if not isinstance(state, str) or state not in required or set(tagged) != required[state]:
            raise InventoryError(f"ledger record has invalid {key} state")
        if any(not isinstance(item, str) or not item.strip() for item in tagged.values()):
            raise InventoryError(f"ledger record has invalid {key} text")
    return cast(BackfillSourceRecord, cast(object, fields))


def _read_ledger(project: str) -> list[BackfillSourceRecord]:
    path = _ledger_path(project)
    if not path.exists():
        raise InventoryError(f"no ledger for {project}; run collect first")
    records: list[BackfillSourceRecord] = []
    ids: set[str] = set()
    changed = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = cast(object, json.loads(line))
            if isinstance(value, dict):
                fields = cast(dict[str, object], value)
                claim = fields.get("claim")
                if isinstance(claim, dict):
                    claim_fields = cast(dict[str, object], claim)
                    if "seat" in claim_fields:
                        _ = claim_fields.setdefault("worker", claim_fields["seat"])
                        del claim_fields["seat"]
                        changed = True
            record = _validate_record(cast(object, value), project)
        except (ValueError, TypeError) as error:
            raise InventoryError(f"{path.name}:{number}: {error}") from error
        if record["id"] in ids:
            raise InventoryError(f"duplicate ledger record {record['id']}")
        ids.add(record["id"])
        records.append(record)
    if changed:
        _write_ledger(project, records)
    return records


def _write_ledger(project: str, records: list[BackfillSourceRecord]) -> None:
    path = _ledger_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            for record in records:
                _ = handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _print_counts(project: str, records: list[BackfillSourceRecord], configuration: ProjectSources) -> None:
    print(f"{project}: {len(records)} frozen records")
    for pool in POOLS:
        selected = [record for record in records if record["pool"] == pool]
        proposal = configuration.proposal_counts.get(pool, {})
        print(f"{pool}: {len(selected)} | proposal: " +
              (", ".join(f"{kind}={count}" for kind, count in proposal.items()) or "not measured"))
        counts = Counter(record["kind"] for record in selected)
        for kind in KINDS:
            print(f"  {kind}: {counts[kind]} | proposal: {proposal.get(kind, 'not measured')}")
        if pool == "structured":
            documents = [record for record in selected if record["kind"] in ("phase", "asbuilt")]
            print(f"  documents: {len(documents)} | proposal: {proposal.get('documents', 'not measured')}")
            files = {record["source"].rsplit(":", 1)[0] for record in documents}
            print(f"  files: {len(files)} | proposal: {proposal.get('files', 'not measured')}")
    for repo, ref in sorted({(record["repo"], record["ref"]) for record in records}):
        print(f"ref: {repo} {ref}")


def _run_collect(project: str, sources: SourceTable) -> int:
    path = _ledger_path(project)
    if project not in sources:
        raise InventoryError(f"no source configuration for {project}")
    if path.exists():
        with locked():
            records = _read_ledger(project)
    else:
        records = _collect(project, sources[project])
        with locked():
            if path.exists():
                records = _read_ledger(project)
            else:
                _write_ledger(project, records)
    _print_counts(project, records, sources[project])
    return 0


def _run_next(arguments: argparse.Namespace) -> int:
    project = cast(str, arguments.project)
    pool = cast(Pool, arguments.pool)
    kind = cast(RecordKind | None, arguments.kind)
    worker = cast(str | None, arguments.claim)
    prefixes = cast(list[str], arguments.path)
    limit = cast(int, arguments.limit)
    if limit < 0:
        raise InventoryError("--limit must be nonnegative")
    if worker is not None and not worker.strip():
        raise InventoryError("--claim needs a nonempty worker")
    with locked():
        records = _read_ledger(project)
        selected: list[BackfillSourceRecord] = []
        for record in sorted(records, key=lambda item: (item["date"], item["id"])):
            claim = record["claim"]
            if (record["pool"] != pool or record["outcome"]["state"] != "undisposed"
                    or (kind is not None and record["kind"] != kind)
                    or (claim["state"] == "claimed" and claim["worker"] != worker)):
                continue
            source_path, separator, _line = record["source"].rpartition(":")
            if prefixes and (not separator or not any(source_path.startswith(prefix) for prefix in prefixes)):
                continue
            selected.append(record)
            if limit and len(selected) >= limit:
                break
        if worker is not None and selected:
            for record in selected:
                record["claim"] = {"state": "claimed", "worker": worker}
            _write_ledger(project, records)
    for record in selected:
        print(" | ".join((record["id"], record["date"], record["kind"], record["title"], record["source"])))
    return 0


def _find_record(identifier: str) -> BackfillSourceRecord:
    project = identifier.split(":", 1)[0]
    for record in _read_ledger(project):
        if record["id"] == identifier:
            return record
    raise InventoryError(f"unknown source record {identifier}")


def _run_release(project: str, worker: str) -> int:
    """Return a stopped worker's undisposed records to the pool; repeats are harmless."""
    if not worker.strip():
        raise InventoryError("--worker needs a nonempty worker")
    with locked():
        records = _read_ledger(project)
        released: list[str] = []
        for record in records:
            claim = record["claim"]
            if record["outcome"]["state"] == "undisposed" and claim["state"] == "claimed" and claim["worker"] == worker:
                record["claim"] = {"state": "unclaimed"}
                released.append(record["id"])
        if released:
            _write_ledger(project, records)
    for identifier in released:
        print(identifier)
    return 0


def _run_show(identifier: str) -> int:
    with locked():
        record = _find_record(identifier)
    if record["kind"] in ("phase", "asbuilt", "changelog"):
        path, line = record["source"].rsplit(":", 1)
        text = _git(record["repo"], "show", f"{record['ref']}:{path}")
        if record["kind"] == "phase":
            text = next(block.text for block in _phase_blocks(text) if block.line == int(line))
        elif record["kind"] == "changelog":
            bullet = next(bullet for bullet in _changelog_bullets(text) if bullet.line == int(line))
            text = f"{bullet.heading}\n{bullet.text}"
    else:
        text = _git(record["repo"], "show", "--stat", record["source"], "--")
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    print(text.rstrip())
    return 0


def _note_exists(project: str, title: str) -> None:
    if not title.strip() or title in (".", "..") or "/" in title or "\\" in title:
        raise InventoryError("--note needs a note title in the project's folder")
    folder = (vault_dir() / "feature-log" / project).resolve()
    note = folder / f"{title}.md"
    if note.resolve().parent != folder or not note.is_file():
        raise InventoryError(f"note {title!r} does not exist in {project}")


def _run_mark(arguments: argparse.Namespace) -> int:
    identifiers = cast(list[str], arguments.ids).copy()
    ids_from = cast(str | None, arguments.ids_from)
    if ids_from:
        identifiers.extend(line.strip() for line in Path(ids_from).read_text(encoding="utf-8").splitlines()
                           if line.strip())
    identifiers = list(dict.fromkeys(identifiers))
    if not identifiers:
        raise InventoryError("mark needs at least one ID")
    disposition = cast(Disposition, arguments.disposition)
    note = cast(str | None, arguments.note)
    reason = cast(str | None, arguments.reason)
    if disposition == "represented" or disposition == "combined":
        if note is None or not note.strip() or reason is not None:
            raise InventoryError(f"{disposition} needs --note")
        outcome: ReviewOutcome = {"state": disposition, "note": note}
    else:
        if reason is None or not reason.strip() or note is not None:
            raise InventoryError(f"{disposition} needs --reason")
        outcome = {"state": disposition, "reason": reason}
    worker = cast(str | None, arguments.worker)
    with locked():
        ledgers: dict[str, list[BackfillSourceRecord]] = {}
        selected: list[BackfillSourceRecord] = []
        for identifier in identifiers:
            project = identifier.split(":", 1)[0]
            if project not in ledgers:
                ledgers[project] = _read_ledger(project)
            record = next((item for item in ledgers[project] if item["id"] == identifier), None)
            if record is None:
                raise InventoryError(f"unknown source record {identifier}")
            claim = record["claim"]
            if claim["state"] == "claimed" and claim["worker"] != worker:
                raise InventoryError(f"{identifier} is claimed by {claim['worker']}")
            if note is not None:
                _note_exists(project, note)
            selected.append(record)
        for record in selected:
            record["outcome"] = outcome.copy()
        for project, records in ledgers.items():
            _write_ledger(project, records)
    print(f"marked {len(identifiers)} {disposition}")
    return 0


def _run_status(project: str, pools: tuple[Pool, ...], header: str) -> int:
    with locked():
        records = _read_ledger(project)
    selected = [record for record in records if record["pool"] in pools]
    counts = Counter(record["outcome"]["state"] for record in selected)
    print(f"{header}: {len(selected)} records")
    for state in ("undisposed", *DISPOSITIONS):
        print(f"{state}: {counts[state]}")
    notes: set[str] = set()
    for record in selected:
        outcome = record["outcome"]
        if outcome["state"] == "represented" or outcome["state"] == "combined":
            notes.add(outcome["note"])
    print("notes:")
    for note in sorted(notes):
        print(f"  {note}")
    return 1 if counts["undisposed"] else 0


class InventoryArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # pyright: ignore[reportImplicitOverride]
        raise InventoryError(message)


def _parser() -> InventoryArgumentParser:
    parser = InventoryArgumentParser(prog="inventory")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    _ = collect.add_argument("--project", required=True, choices=tuple(PROJECTS))
    next_parser = commands.add_parser("next")
    _ = next_parser.add_argument("--project", required=True, choices=tuple(PROJECTS))
    _ = next_parser.add_argument("--pool", required=True, choices=POOLS)
    _ = next_parser.add_argument("--kind", choices=KINDS)
    _ = next_parser.add_argument("--path", action="append", nargs="+", default=[])
    _ = next_parser.add_argument("--claim")
    _ = next_parser.add_argument("--limit", type=int, default=25)
    release = commands.add_parser("release")
    _ = release.add_argument("--project", required=True, choices=tuple(PROJECTS))
    _ = release.add_argument("--worker", required=True)
    show = commands.add_parser("show")
    _ = show.add_argument("id")
    mark = commands.add_parser("mark")
    _ = mark.add_argument("ids", nargs="*")
    _ = mark.add_argument("--ids-from")
    _ = mark.add_argument("--worker")
    _ = mark.add_argument("--as", dest="disposition", required=True, choices=DISPOSITIONS)
    destination = mark.add_mutually_exclusive_group(required=True)
    _ = destination.add_argument("--note")
    _ = destination.add_argument("--reason")
    status = commands.add_parser("status")
    _ = status.add_argument("--project", required=True, choices=tuple(PROJECTS))
    _ = status.add_argument("--pool", choices=POOLS)
    return parser


def main(argv: list[str], sources: SourceTable = SOURCES) -> int:
    try:
        arguments = _parser().parse_args(argv)
        command = cast(str, arguments.command)
        if command == "collect":
            return _run_collect(cast(str, arguments.project), sources)
        if command == "next":
            arguments.path = [prefix for group in cast(list[list[str]], arguments.path) for prefix in group]
            return _run_next(arguments)
        if command == "release":
            return _run_release(cast(str, arguments.project), cast(str, arguments.worker))
        if command == "show":
            return _run_show(cast(str, arguments.id))
        if command == "mark":
            return _run_mark(arguments)
        project = cast(str, arguments.project)
        pool = cast(Pool | None, arguments.pool)
        pools = POOLS if pool is None else (pool,)
        header = project if pool is None else f"{project} {pool}"
        return _run_status(project, pools, header)
    except (InventoryError, OSError, StopIteration) as error:
        reason = " ".join(str(error).split()) or "source text no longer matches the frozen record"
        print(f"inventory: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
