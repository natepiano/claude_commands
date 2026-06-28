#!/usr/bin/env python3
"""Add a Rust project to the clean-fix allowlists."""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import tomllib

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

DEFAULT_CONF = Path.home() / ".claude" / "scripts" / "clean-fix" / "clean-fix.conf"
DEFAULT_RUST_DIR = Path.home() / "rust"
MARKER = "#CLEAN_FIX_SKIP#"
SECTION_RE = re.compile(r"^\[(?P<name>.+)\]\s*$")

ProjectKind = Literal["standalone", "workspace", "workspace_member"]
SectionStatus = Literal["added", "active", "skipped", "conflict"]


@dataclass(frozen=True)
class Project:
    entry: str
    key: str
    kind: ProjectKind
    target: Path
    workspace_root: Path | None


@dataclass(frozen=True)
class SectionResult:
    section: str
    status: SectionStatus
    detail: str


class CliArgs(argparse.Namespace):
    project: str = ""
    conf: Path = DEFAULT_CONF
    rust_dir: Path = DEFAULT_RUST_DIR
    dry_run: bool = False


def read_toml(path: Path) -> Mapping[str, object]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return cast(Mapping[str, object], data)


def table(value: object) -> Mapping[str, object]:
    if isinstance(value, dict):
        return cast(Mapping[str, object], value)
    return {}


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    return [item for item in items if isinstance(item, str)]


def cargo_toml(path: Path) -> Path:
    return path if path.name == "Cargo.toml" else path / "Cargo.toml"


def relative_posix(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def contains_workspace_manifest(path: Path) -> bool:
    manifest = cargo_toml(path)
    if not manifest.exists():
        return False
    workspace = read_toml(manifest).get("workspace")
    return isinstance(workspace, dict)


def workspace_package_root(target: Path) -> Path | None:
    package = table(read_toml(cargo_toml(target)).get("package"))
    workspace = package.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return None
    root = (target / workspace).resolve()
    if contains_workspace_manifest(root):
        return root
    return None


def pattern_matches_member(pattern: str, member: str) -> bool:
    normalized = pattern.strip().strip("/")
    if not normalized:
        return False
    return member == normalized or fnmatch.fnmatchcase(member, normalized)


def workspace_includes_member(workspace_root: Path, member: Path) -> bool:
    workspace = table(read_toml(cargo_toml(workspace_root)).get("workspace"))
    member_rel = relative_posix(member, workspace_root)
    excludes = string_list(workspace.get("exclude"))
    if any(pattern_matches_member(pattern, member_rel) for pattern in excludes):
        return False
    members = string_list(workspace.get("members"))
    return any(pattern_matches_member(pattern, member_rel) for pattern in members)


def containing_workspace(target: Path, rust_dir: Path) -> Path | None:
    explicit = workspace_package_root(target)
    if explicit is not None:
        return explicit

    for parent in target.parents:
        if parent == target or parent == rust_dir.parent:
            break
        if not contains_workspace_manifest(parent):
            continue
        if workspace_includes_member(parent, target):
            return parent
    return None


def bare_project_name(raw_project: str) -> bool:
    path = Path(raw_project)
    return not path.is_absolute() and path.name == raw_project and raw_project != "Cargo.toml"


def workspace_member_candidates(name: str, rust_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    for root in sorted(path for path in rust_dir.iterdir() if path.is_dir()):
        if root.name.endswith("_style_fix") or not cargo_toml(root).is_file():
            continue
        workspace = table(read_toml(cargo_toml(root)).get("workspace"))
        if not workspace:
            continue
        for pattern in string_list(workspace.get("members")):
            for candidate in root.glob(pattern):
                resolved = candidate.resolve()
                if (
                    resolved.name == name
                    and resolved.is_dir()
                    and cargo_toml(resolved).is_file()
                    and workspace_includes_member(root, resolved)
                    and resolved not in seen
                ):
                    candidates.append(resolved)
                    seen.add(resolved)
    return candidates


def resolve_target(raw_project: str, rust_dir: Path) -> Path:
    if bare_project_name(raw_project):
        direct = rust_dir / raw_project
        if direct.exists():
            return direct.resolve()
        candidates = workspace_member_candidates(raw_project, rust_dir)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            rendered = ", ".join(relative_posix(path, rust_dir) for path in candidates)
            raise ValueError(f"ambiguous project name {raw_project}: {rendered}")

    raw_path = Path(raw_project).expanduser()
    target = raw_path if raw_path.is_absolute() else rust_dir / raw_path
    if target.name == "Cargo.toml":
        target = target.parent
    return target.resolve()


def resolve_project(raw_project: str, rust_dir: Path) -> Project:
    rust_dir = rust_dir.resolve()
    target = resolve_target(raw_project, rust_dir)

    if not target.is_dir():
        raise ValueError(f"target is not a directory: {target}")
    manifest = cargo_toml(target)
    if not manifest.is_file():
        raise ValueError(f"target has no Cargo.toml: {target}")
    try:
        target_rel = relative_posix(target, rust_dir)
    except ValueError as exc:
        raise ValueError(f"target must be under {rust_dir}: {target}") from exc

    if contains_workspace_manifest(target):
        return Project(
            entry=target_rel,
            key=target.name,
            kind="workspace",
            target=target,
            workspace_root=target,
        )

    workspace_root = containing_workspace(target, rust_dir)
    if workspace_root is None:
        return Project(
            entry=target_rel,
            key=target.name,
            kind="standalone",
            target=target,
            workspace_root=None,
        )

    workspace_rel = relative_posix(workspace_root, rust_dir)
    member_rel = relative_posix(target, workspace_root)
    return Project(
        entry=f"{workspace_rel}/{member_rel}",
        key=target.name,
        kind="workspace_member",
        target=target,
        workspace_root=workspace_root,
    )


def section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    start = -1
    for index, raw in enumerate(lines):
        if raw.strip() == f"[{section}]":
            start = index
            break
    if start < 0:
        raise ValueError(f"section [{section}] not found")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    return start, end


def uncommented_body(line: str) -> str:
    body = line.strip()
    if body.startswith(MARKER):
        body = body[len(MARKER):].strip()
    return body.split("#", 1)[0].strip()


def is_tagged(line: str) -> bool:
    return line.strip().startswith(MARKER)


def project_key(entry: str) -> str:
    return entry.rsplit("/", 1)[-1] if "/" in entry else entry


def last_content_index(lines: list[str], section: str) -> int:
    start, end = section_bounds(lines, section)
    last = start
    for index in range(start + 1, end):
        if lines[index].strip():
            last = index
    return last


def add_to_section(
    lines: list[str],
    section: str,
    project: Project,
    *,
    unique_key: bool,
) -> tuple[list[str], SectionResult]:
    out = list(lines)
    start, end = section_bounds(out, section)
    for index in range(start + 1, end):
        body = uncommented_body(out[index])
        if not body:
            continue
        if body == project.entry:
            if is_tagged(out[index]):
                return out, SectionResult(
                    section=section,
                    status="skipped",
                    detail=f"{project.entry} is present but skipped",
                )
            return out, SectionResult(
                section=section,
                status="active",
                detail=f"{project.entry} is already active",
            )
        if unique_key and project_key(body) == project.key:
            return out, SectionResult(
                section=section,
                status="conflict",
                detail=f"{project.key} already maps to {body}",
            )

    out.insert(last_content_index(out, section) + 1, project.entry)
    return out, SectionResult(
        section=section,
        status="added",
        detail=f"added {project.entry}",
    )


def add_project(lines: list[str], project: Project) -> tuple[list[str], list[SectionResult]]:
    out, build = add_to_section(lines, "build", project, unique_key=False)
    out, style = add_to_section(out, "projects", project, unique_key=True)
    return out, [build, style]


def print_result(project: Project, results: list[SectionResult], changed: bool) -> None:
    if project.kind == "workspace_member" and project.workspace_root is not None:
        workspace = project.workspace_root.name
        print(f"Project: {project.key} ({project.kind}, workspace {workspace})")
    else:
        print(f"Project: {project.key} ({project.kind})")
    print(f"Entry: {project.entry}")
    for result in results:
        print(f"[{result.section}] {result.status}: {result.detail}")
    if not changed:
        print("No changes.")


def parse_args(argv: list[str]) -> CliArgs:
    parser = argparse.ArgumentParser(description="Add a project to clean-fix.")
    _ = parser.add_argument("project", help="project name/path under ~/rust, or absolute path")
    _ = parser.add_argument("--conf", type=Path, default=DEFAULT_CONF)
    _ = parser.add_argument("--rust-dir", type=Path, default=DEFAULT_RUST_DIR)
    _ = parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv, namespace=CliArgs())


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        project = resolve_project(args.project, args.rust_dir)
        lines = args.conf.read_text().splitlines()
        out, results = add_project(lines, project)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if any(result.status in {"skipped", "conflict"} for result in results):
        print_result(project, results, changed=False)
        return 1

    changed = out != lines
    if changed and not args.dry_run:
        _ = args.conf.write_text("\n".join(out) + "\n")
    print_result(project, results, changed=changed)
    if args.dry_run and changed:
        print("Dry run; no file written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
