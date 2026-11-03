#!/usr/bin/env python3
"""
Split a consolidated Bevy migration guide into individual section files.

Fetches the migration guide from bevy-website and splits it by ### headers
into individual .md files in the target directory.

Usage:
    bevy_migration_split_guide.py --version 0.18.0 --output-dir ~/rust/bevy-0.18.0/release-content/migration-guides
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, cast

COMPLETION = ".split-complete.json"


class Section(NamedTuple):
    """A migration guide section."""
    title: str
    filename: str
    content: str


def sanitize_filename(title: str) -> str:
    """Convert a section title to a valid filename."""
    filename = title.strip()
    filename = re.sub(r'\s+', '_', filename)
    filename = re.sub(r'[^\w\-_]', '', filename)
    filename = filename[:80]
    if not filename:
        filename = "untitled"
    return f"{filename}.md"


def fetch_guide_from_website(version: str) -> str:
    """Fetch the migration guide from bevy-website."""
    parts = version.split('.')
    if len(parts) < 2:
        raise ValueError(f"Invalid version format: {version}")

    major_minor = f"{parts[0]}.{parts[1]}"
    prev_parts = parts.copy()
    prev_parts[1] = str(int(parts[1]) - 1)
    prev_major_minor = f"{prev_parts[0]}.{prev_parts[1]}"

    guide_path = f"content/learn/migration-guides/{prev_major_minor}-to-{major_minor}.md"
    url = f"https://raw.githubusercontent.com/bevyengine/bevy-website/main/{guide_path}"

    result = subprocess.run(
        ["curl", "-fsSL", url],
        capture_output=True,
        text=True,
        check=False
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to fetch migration guide from {url}\nError: {result.stderr}"
        )

    return result.stdout


def parse_sections(content: str) -> list[Section]:
    """Parse the consolidated guide into individual sections."""
    lines = content.split('\n')
    sections: list[Section] = []
    current_title: str | None = None
    current_lines: list[str] = []
    in_frontmatter = False
    frontmatter_count = 0

    for line in lines:
        if line.strip() == '+++':
            frontmatter_count += 1
            in_frontmatter = frontmatter_count == 1
            continue

        if in_frontmatter:
            continue

        if frontmatter_count == 2 and not in_frontmatter:
            in_frontmatter = False

        if line.startswith('### '):
            if current_title is not None:
                section_content = '\n'.join(current_lines).strip()
                if section_content:
                    sections.append(Section(
                        title=current_title,
                        filename=sanitize_filename(current_title),
                        content=section_content
                    ))

            title_match = re.match(r'###\s+(.+?)(?:\s*\{\{.*\}\})?\s*$', line)
            if title_match:
                current_title = title_match.group(1).strip()
            else:
                current_title = line[4:].strip()

            current_lines = [line]

        elif current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        section_content = '\n'.join(current_lines).strip()
        if section_content:
            sections.append(Section(
                title=current_title,
                filename=sanitize_filename(current_title),
                content=section_content
            ))

    return sections


class GuidePublication:
    """A complete staged set replaces the live directory; the previous set survives until then."""

    def __init__(self, live: Path) -> None:
        self.live: Path = live
        self.staging: Path = live.with_name(f".{live.name}.staging")
        self.previous: Path = live.with_name(f".{live.name}.previous")

    def recover(self) -> None:
        if not self.live.exists() and self.previous.exists():
            if (self.staging / COMPLETION).is_file():
                os.replace(self.staging, self.live)
            else:
                os.replace(self.previous, self.live)
        if (self.live / COMPLETION).is_file():
            # The marker certifies publication, not immutable content: preserve later user edits.
            for obsolete in (self.staging, self.previous):
                if obsolete.exists():
                    shutil.rmtree(obsolete)


def write_sections(sections: list[Section], output_dir: Path) -> int:
    """Build a complete directory before either publication rename."""
    publication = GuidePublication(output_dir)
    publication.recover()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if publication.staging.exists():
        shutil.rmtree(publication.staging)
    publication.staging.mkdir()
    manifest: dict[str, str] = {}
    for section in sections:
        filename = section.filename
        counter = 1
        while filename in manifest:
            filename = f"{section.filename.rsplit('.', 1)[0]}_{counter}.md"
            counter += 1
        filepath = publication.staging / filename
        _ = filepath.write_text(section.content)
        manifest[filename] = hashlib.sha256(filepath.read_bytes()).hexdigest()
    _ = (publication.staging / COMPLETION).write_text(json.dumps(manifest, sort_keys=True) + "\n")
    if output_dir.exists():
        os.replace(output_dir, publication.previous)
    os.replace(publication.staging, output_dir)
    if publication.previous.exists():
        shutil.rmtree(publication.previous)
    return len(manifest)


def guides_complete(output_dir: Path) -> bool:
    """A published set, or a legacy set with no interrupted swap, belongs to its user."""
    publication = GuidePublication(output_dir)
    publication.recover()
    if not output_dir.is_dir():
        return False
    if (output_dir / COMPLETION).is_file():
        return True
    return (not publication.staging.exists() and not publication.previous.exists()
            and any(output_dir.glob("*.md")))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split consolidated Bevy migration guide into individual files"
    )
    _ = parser.add_argument("--check-dir", type=Path)
    _ = parser.add_argument(
        "--version",
        help="Bevy version (e.g., 0.18.0)"
    )
    _ = parser.add_argument(
        "--output-dir",
        help="Output directory for individual guide files"
    )

    args = parser.parse_args()

    check_dir = cast(object, getattr(args, "check_dir"))
    if isinstance(check_dir, Path):
        sys.exit(0 if guides_complete(check_dir) else 1)
    if not args.version or not args.output_dir:  # pyright: ignore[reportAny]
        parser.error("--version and --output-dir are required")

    version: str = str(getattr(args, "version"))  # pyright: ignore[reportAny]
    output_dir_arg: str = str(getattr(args, "output_dir"))  # pyright: ignore[reportAny]
    output_dir = Path(output_dir_arg).expanduser()

    print(f"Fetching migration guide for Bevy {version} from bevy-website...", file=sys.stderr)
    content = fetch_guide_from_website(version)

    print("Parsing sections...", file=sys.stderr)
    sections = parse_sections(content)

    if not sections:
        print("Error: No sections found in migration guide", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(sections)} sections", file=sys.stderr)

    written = write_sections(sections, output_dir)
    print(f"Wrote {written} migration guide files to {output_dir}", file=sys.stderr)

    print(output_dir)


if __name__ == "__main__":
    main()
