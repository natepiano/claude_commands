#!/usr/bin/env python3
"""
Combine Bevy migration guides into a single markdown file with subagent TOC

Usage: bevy_migration_combine_guides.py <version> <guides-dir> <output-file>
Example: bevy_migration_combine_guides.py 0.17.1 ~/rust/bevy-0.17.1/release-content/migration-guides ~/.claude/bevy_migration/bevy-0.17.1-guides.md

Exit codes: 0 = success, 1 = error
"""

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast


@dataclass
class GuideInfo:
    """Information about a migration guide"""
    filename: str
    title: str
    content: str
    line_number: int = 0  # Will be set during writing


def extract_title(content: str, filename: str) -> str:
    """Extract title from YAML frontmatter"""
    # Try to extract from frontmatter
    frontmatter_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if frontmatter_match:
        frontmatter = frontmatter_match.group(1)
        title_match = re.search(r'title:\s*(.+)', frontmatter)
        if title_match:
            title = title_match.group(1).strip()
            # Remove surrounding quotes if present
            title = title.strip('"').strip("'")
            return title

    # Fallback to filename
    return filename.replace('.md', '').replace('_', ' ').replace('-', ' ').title()


def calculate_bucket_distribution(total_guides: int, num_buckets: int) -> list[int]:
    """Calculate how many guides per bucket for even distribution"""
    base_size = total_guides // num_buckets
    remainder = total_guides % num_buckets

    # First 'remainder' buckets get base_size + 1, rest get base_size
    distribution = [base_size + 1] * remainder + [base_size] * (num_buckets - remainder)
    return distribution


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Combine Bevy migration guides with subagent TOC'
    )
    _ = parser.add_argument('version', help='Bevy version (e.g., 0.17.1)')
    _ = parser.add_argument('guides_dir', type=Path, help='Path to migration-guides directory')
    _ = parser.add_argument('output_file', type=Path, help='Output file path')

    args = parser.parse_args()

    version = cast(str, args.version)
    guides_dir = cast(Path, args.guides_dir)
    output_file = cast(Path, args.output_file)

    # Validate guides directory
    if not guides_dir.exists():
        print(f"Error: Migration guides directory not found at {guides_dir}", file=sys.stderr)
        sys.exit(1)

    # Find all guide files
    guide_files = sorted(guides_dir.glob('*.md'))
    if not guide_files:
        print(f"Error: No migration guides found in {guides_dir}", file=sys.stderr)
        sys.exit(1)

    total_guides = len(guide_files)
    print(f"Found {total_guides} migration guides", file=sys.stderr)

    # Read all guides and extract titles
    guides: list[GuideInfo] = []
    for guide_file in guide_files:
        content = guide_file.read_text(encoding='utf-8')
        title = extract_title(content, guide_file.name)
        guides.append(GuideInfo(
            filename=guide_file.name,
            title=title,
            content=content
        ))

    # Calculate bucket distribution
    num_buckets = 10
    bucket_sizes = calculate_bucket_distribution(total_guides, num_buckets)

    # Create buckets
    buckets: list[list[GuideInfo]] = []
    guide_index = 0
    for bucket_size in bucket_sizes:
        bucket = guides[guide_index:guide_index + bucket_size]
        buckets.append(bucket)
        guide_index += bucket_size

    # Create output directory
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Build the file content
    lines: list[str] = []

    # Header
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f"# Bevy {version} Migration Guides")
    lines.append("")
    lines.append(f"This file contains all official Bevy {version} migration guides combined into a single document.")
    lines.append("")
    lines.append(f"**Source:** https://github.com/bevyengine/bevy/tree/v{version}/release-content/migration-guides")
    lines.append(f"**Total Guides:** {total_guides}")
    lines.append(f"**Generated:** {timestamp}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("# Table of Contents - Subagent Distribution")
    lines.append("")
    lines.append(f"The {total_guides} migration guides are distributed across {num_buckets} subagents for parallel analysis:")
    lines.append("")

    # First pass: Build content and track structure (without line numbers yet)
    content_lines: list[str] = []
    guide_metadata: list[tuple[int, int, str, int]] = []  # (bucket_idx, guide_num, title, content_size)

    for bucket_idx, bucket in enumerate(buckets, start=1):
        for guide_idx, guide in enumerate(bucket, start=1):
            global_guide_num = sum(bucket_sizes[:bucket_idx-1]) + guide_idx

            # Calculate size of this guide's section
            # Structure: blank + 3 comment lines + blank + content + blank + blank = 5 fixed + content + 2 fixed
            content_size = 7 + len(guide.content.splitlines())
            guide_metadata.append((bucket_idx, global_guide_num, guide.title, content_size))

            # Add guide content
            content_lines.append("")
            content_lines.append("<!-- ================================================================ -->")
            content_lines.append(f"<!-- Migration Guide: {guide.filename} -->")
            content_lines.append("<!-- ================================================================ -->")
            content_lines.append("")
            # Strip trailing newline to avoid double-newline when joining
            content_lines.append(guide.content.rstrip('\n'))
            content_lines.append("")
            content_lines.append("")

    # Build TOC with placeholder line numbers to calculate final TOC size
    toc_lines_temp: list[str] = []
    for bucket_idx in range(1, num_buckets + 1):
        toc_lines_temp.append(f"## Subagent {bucket_idx}")
        toc_lines_temp.append("")
        bucket_guides = [g for g in guide_metadata if g[0] == bucket_idx]
        for _, guide_num, title, _ in bucket_guides:
            # Use placeholder - we'll replace these with real line numbers
            toc_lines_temp.append(f"99999 - migration guide {guide_num} - {title}")
        toc_lines_temp.append("")

    # Now we know the final TOC size, calculate where content actually starts
    # Structure: header + TOC + blank + --- + blank + content_lines[0]
    # So content_lines[0] is at line: (header lines) + (TOC lines) + 3 + 1
    header_size = len(lines)
    toc_size = len(toc_lines_temp)
    content_start_line = header_size + toc_size + 3 + 1  # +3 for blank, separator, blank; +1 to get to first content item

    # Second pass: Calculate actual line numbers
    current_line = content_start_line
    toc_entries: list[tuple[int, int, int, str]] = []  # (bucket_idx, guide_num, line_number, title)

    for bucket_idx, guide_num, title, content_size in guide_metadata:
        # current_line points to blank line, guide separator starts at current_line + 1
        toc_entries.append((bucket_idx, guide_num, current_line + 1, title))
        current_line += content_size

    # Build final TOC with correct line numbers
    toc_lines_final: list[str] = []
    for bucket_idx in range(1, num_buckets + 1):
        toc_lines_final.append(f"## Subagent {bucket_idx}")
        toc_lines_final.append("")
        bucket_entries = [e for e in toc_entries if e[0] == bucket_idx]
        for _, guide_num, line_num, title in bucket_entries:
            toc_lines_final.append(f"{line_num} - migration guide {guide_num} - {title}")
        toc_lines_final.append("")

    # Assemble final content
    lines.extend(toc_lines_final)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.extend(content_lines)

    final_content = '\n'.join(lines)

    # Write output
    _ = output_file.write_text(final_content, encoding='utf-8')

    print(f"✓ Combined migration guides written to: {output_file}", file=sys.stderr)
    print(f"✓ Total guides processed: {total_guides}", file=sys.stderr)
    print(f"✓ Distribution across {num_buckets} subagents: {bucket_sizes}", file=sys.stderr)


if __name__ == '__main__':
    main()
