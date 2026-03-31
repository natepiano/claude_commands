#!/usr/bin/env python3

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys
from dataclasses import dataclass


FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
EMPTY_LINE_RE = re.compile(r"\n{3,}")


@dataclass
class CommandDoc:
    source_path: pathlib.Path
    rel_path: pathlib.PurePosixPath
    source_text: str
    body_text: str
    title: str
    description: str
    skill_name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Codex skills from Claude command markdown files."
    )
    parser.add_argument(
        "--source",
        default="~/".replace("//", "/") + ".claude/commands",
        help="Claude commands directory (default: ~/.claude/commands)",
    )
    parser.add_argument(
        "--dest",
        default="~/".replace("//", "/") + ".codex/skills/generated-from-claude",
        help="Destination root for generated Codex skills",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing generated skill directories",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned output without writing files",
    )
    parser.add_argument(
        "--reference-mode",
        choices=("copy", "symlink"),
        default="copy",
        help="How to place the original Claude command under references/",
    )
    return parser.parse_args()


def expand_path(raw: str) -> pathlib.Path:
    return pathlib.Path(raw).expanduser().resolve()


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")

    return metadata, text[match.end() :]


def infer_title(body: str, rel_path: pathlib.PurePosixPath) -> str:
    match = TITLE_RE.search(body)
    if match:
        return match.group(1).strip()

    stem = rel_path.stem.replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in stem.split())


def infer_summary(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("<") and stripped.endswith(">"):
            continue
        if stripped.startswith("```"):
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            stripped = stripped[2:].strip()
        stripped = re.sub(r"`([^`]+)`", r"\1", stripped)
        stripped = re.sub(r"\*\*([^*]+)\*\*", r"\1", stripped)
        stripped = stripped.rstrip(".")
        if stripped:
            return stripped
    return ""


def normalize_skill_name(rel_path: pathlib.PurePosixPath) -> str:
    base = "-".join(rel_path.with_suffix("").parts).lower()
    base = re.sub(r"[^a-z0-9._-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base)
    return base.strip("-") or "generated-skill"


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def infer_description(metadata: dict[str, str], title: str, summary: str, rel_path: pathlib.PurePosixPath) -> str:
    base = metadata.get("description") or summary or title
    base = collapse_whitespace(base).rstrip(".")
    return (
        f"{base}. Generated from Claude command `{rel_path.as_posix()}`. "
        f"Use when the user names that command or asks for this workflow."
    )


def clean_body(body: str) -> str:
    cleaned = body.lstrip()
    cleaned = EMPTY_LINE_RE.sub("\n\n", cleaned)
    return cleaned.rstrip() + "\n"


def build_skill_markdown(command: CommandDoc) -> str:
    description = command.description.replace('"', '\\"')
    source_path = command.source_path.as_posix()
    rel_path = command.rel_path.as_posix()
    return f"""---
name: {command.skill_name}
description: "{description}"
---

# {command.title}

This skill was generated from the Claude command file `{rel_path}`.

When using this skill:
- Treat any user-supplied extra text as `$ARGUMENTS`.
- If the original workflow requires arguments and none were provided, ask one concise follow-up question.
- Follow the original command instructions in [references/original-claude-command.md](references/original-claude-command.md).
- If that command references other local files such as `@~/.claude/...`, read them only when needed.

Source: `{source_path}`
"""


def build_command_doc(path: pathlib.Path, source_root: pathlib.Path) -> CommandDoc:
    rel_path = pathlib.PurePosixPath(path.relative_to(source_root).as_posix())
    source_text = path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(source_text)
    cleaned_body = clean_body(body)
    title = infer_title(cleaned_body, rel_path)
    summary = infer_summary(cleaned_body)
    description = infer_description(metadata, title, summary, rel_path)
    skill_name = normalize_skill_name(rel_path)
    return CommandDoc(
        source_path=path,
        rel_path=rel_path,
        source_text=source_text,
        body_text=cleaned_body,
        title=title,
        description=description,
        skill_name=skill_name,
    )


def write_reference_file(mode: str, source_path: pathlib.Path, reference_path: pathlib.Path) -> None:
    if mode == "symlink":
        reference_path.symlink_to(source_path)
        return
    shutil.copy2(source_path, reference_path)


def render_plan(commands: list[CommandDoc], dest_root: pathlib.Path) -> str:
    lines = [f"Generate {len(commands)} Codex skills into {dest_root}:"]
    for command in commands:
        lines.append(
            f"- {command.rel_path.as_posix()} -> {dest_root / command.skill_name}"
        )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    source_root = expand_path(args.source)
    dest_root = expand_path(args.dest)

    if not source_root.is_dir():
        print(f"error: source directory does not exist: {source_root}", file=sys.stderr)
        return 1

    command_paths = sorted(source_root.rglob("*.md"))
    commands = [
        build_command_doc(path, source_root)
        for path in command_paths
        if path.is_file()
    ]

    if args.dry_run:
        print(render_plan(commands, dest_root))
        return 0

    dest_root.mkdir(parents=True, exist_ok=True)
    generated = 0

    for command in commands:
        skill_dir = dest_root / command.skill_name
        references_dir = skill_dir / "references"
        skill_file = skill_dir / "SKILL.md"
        reference_file = references_dir / "original-claude-command.md"

        if skill_dir.exists():
            if not args.force:
                print(f"skip: {skill_dir} already exists", file=sys.stderr)
                continue
            shutil.rmtree(skill_dir)

        references_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(build_skill_markdown(command), encoding="utf-8")
        write_reference_file(args.reference_mode, command.source_path, reference_file)
        generated += 1

    print(f"generated {generated} skills in {dest_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
