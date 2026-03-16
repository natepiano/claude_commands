#!/usr/bin/env python3
"""
Split a consolidated Bevy migration guide into individual section files.

Fetches the migration guide from bevy-website and splits it by ### headers
into individual .md files in the target directory.

Usage:
    bevy_migration_split_guide.py --version 0.18.0 --output-dir ~/rust/bevy-0.18.0/release-content/migration-guides
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


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


def write_sections(sections: list[Section], output_dir: Path) -> int:
    """Write sections to individual files."""
    output_dir.mkdir(parents=True, exist_ok=True)

    for existing in output_dir.glob("*.md"):
        existing.unlink()

    used_filenames: set[str] = set()
    written = 0

    for section in sections:
        filename = section.filename
        counter = 1
        while filename in used_filenames:
            base = section.filename.rsplit('.', 1)[0]
            filename = f"{base}_{counter}.md"
            counter += 1

        used_filenames.add(filename)

        filepath = output_dir / filename
        _ = filepath.write_text(section.content)
        written += 1

    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split consolidated Bevy migration guide into individual files"
    )
    _ = parser.add_argument(
        "--version",
        required=True,
        help="Bevy version (e.g., 0.18.0)"
    )
    _ = parser.add_argument(
        "--output-dir",
        required=True,
        help="Output directory for individual guide files"
    )

    args = parser.parse_args()

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
