#!/usr/bin/env python3
"""
Bevy Migration Checklist Validator

Validates generated migration checklist against official Bevy migration guides
to ensure accuracy and completeness.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import TypedDict, cast


class ChecklistSection(TypedDict):
    """Type for parsed checklist section"""
    title: str
    prs: list[int]
    content: str


class GuideInfo(TypedDict):
    """Type for parsed guide information"""
    title: str
    prs: list[int]
    filename: str


def parse_checklist_sections(checklist_path: Path) -> list[ChecklistSection]:
    """Parse checklist file into sections"""
    content = checklist_path.read_text(encoding='utf-8')
    sections: list[ChecklistSection] = []

    # Split by ## headers
    section_pattern = r'^## (.+?)$'
    matches = list(re.finditer(section_pattern, content, re.MULTILINE))

    for i, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        section_content = content[start:end]

        # Extract PR numbers
        pr_matches: list[str | tuple[str, ...]] = re.findall(r'#(\d+)', section_content[:200])
        prs = [int(pr) for pr in pr_matches if isinstance(pr, str)]

        sections.append(ChecklistSection(
            title=title,
            prs=prs,
            content=section_content
        ))

    return sections


def parse_guide_file(guide_path: Path) -> GuideInfo:
    """Parse a single migration guide file"""
    content = guide_path.read_text(encoding='utf-8')

    # Extract frontmatter
    frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
    if not frontmatter_match:
        return GuideInfo(title='', prs=[], filename=guide_path.name)

    frontmatter = frontmatter_match.group(1)

    # Extract title
    title_match = re.search(r'title:\s*(.+)', frontmatter)
    title = title_match.group(1).strip() if title_match else ''

    # Extract PRs
    pr_match = re.search(r'pull_requests:\s*\[([\d,\s]+)\]', frontmatter)
    prs: list[int] = []
    if pr_match:
        prs = [int(pr.strip()) for pr in pr_match.group(1).split(',')]

    return GuideInfo(
        title=title,
        prs=prs,
        filename=guide_path.name
    )


def find_matching_section(
    guide: GuideInfo,
    sections: list[ChecklistSection]
) -> ChecklistSection | None:
    """Find checklist section matching a guide"""
    guide_title = guide['title']
    guide_prs = guide['prs']

    for section in sections:
        section_title = section['title']
        section_prs = section['prs']

        # Match by title (case insensitive, normalized)
        if normalize_title(guide_title) == normalize_title(section_title):
            return section

        # Match by PR numbers (if PRs present and match)
        if guide_prs and section_prs:
            if set(guide_prs) == set(section_prs):
                return section

    return None


def normalize_title(title: str) -> str:
    """Normalize title for comparison"""
    # Remove backticks, quotes, extra spaces
    normalized = title.lower()
    normalized = re.sub(r'[`"\']', '', normalized)
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip()


def validate_checklist(
    checklist_path: Path,
    guides_dir: Path
) -> tuple[list[str], list[str], list[str]]:
    """
    Validate checklist against guides.

    Returns (errors, warnings, missing_guides)
    """
    errors: list[str] = []
    warnings: list[str] = []
    missing_guides: list[str] = []

    # Parse checklist
    try:
        sections = parse_checklist_sections(checklist_path)
    except Exception as e:
        errors.append(f"Failed to parse checklist: {e}")
        return errors, warnings, missing_guides

    # Parse all guide files
    guide_files = sorted(guides_dir.glob('*.md'))
    if not guide_files:
        errors.append(f"No migration guides found in {guides_dir}")
        return errors, warnings, missing_guides

    guides: list[GuideInfo] = []
    for guide_file in guide_files:
        try:
            guide = parse_guide_file(guide_file)
            if guide['title']:  # Only include guides with titles
                guides.append(guide)
        except Exception as e:
            warnings.append(f"Failed to parse guide {guide_file.name}: {e}")

    print(f"Parsed {len(guides)} migration guides", file=sys.stderr)
    print(f"Parsed {len(sections)} checklist sections", file=sys.stderr)

    # Validation 1: Every guide should have a corresponding section
    for guide in guides:
        guide_title = guide['title']
        guide_filename = guide['filename']

        matching_section = find_matching_section(guide, sections)
        if not matching_section:
            missing_guides.append(
                f"Missing: {guide_title} (PRs: {guide['prs']}) from {guide_filename}"
            )

    # Validation 2: Every section should correspond to a guide
    for section in sections:
        section_title = section['title']

        # Try to find matching guide
        matched = False
        for guide in guides:
            if find_matching_section(guide, [section]) is not None:
                matched = True
                break

        if not matched:
            warnings.append(
                f"Orphan section (no matching guide): {section_title} (PRs: {section['prs']})"
            )

    # Validation 3: All PR numbers should exist in source guides
    all_guide_prs: set[int] = set()
    for guide in guides:
        all_guide_prs.update(guide['prs'])

    for section in sections:
        section_title = section['title']
        section_prs = section['prs']

        for pr in section_prs:
            if pr not in all_guide_prs:
                errors.append(
                    f"Fabricated PR #{pr} in section: {section_title}"
                )

    return errors, warnings, missing_guides


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Validate Bevy migration checklist against official guides'
    )
    _ = parser.add_argument(
        '--checklist',
        type=Path,
        required=True,
        help='Path to generated checklist file'
    )
    _ = parser.add_argument(
        '--guides-dir',
        type=Path,
        required=True,
        help='Path to official migration guides directory'
    )

    args = parser.parse_args()

    checklist_path = cast(Path, args.checklist)
    guides_dir = cast(Path, args.guides_dir)

    # Validate paths
    if not checklist_path.exists():
        print(f"Error: Checklist not found: {checklist_path}", file=sys.stderr)
        sys.exit(1)

    if not guides_dir.exists():
        print(f"Error: Guides directory not found: {guides_dir}", file=sys.stderr)
        sys.exit(1)

    # Run validation
    print("Validating checklist...", file=sys.stderr)
    errors, warnings, missing_guides = validate_checklist(checklist_path, guides_dir)

    # Print results
    print("\n" + "=" * 60)
    print("VALIDATION REPORT")
    print("=" * 60)

    if errors:
        print(f"\n❌ ERRORS ({len(errors)}):")
        for error in errors:
            print(f"  - {error}")

    if missing_guides:
        print(f"\n⚠️  MISSING GUIDES ({len(missing_guides)}):")
        for missing in missing_guides:
            print(f"  - {missing}")

    if warnings:
        print(f"\n⚠️  WARNINGS ({len(warnings)}):")
        for warning in warnings:
            print(f"  - {warning}")

    if not errors and not missing_guides:
        print("\n✅ VALIDATION PASSED")
        print("  - All migration guides present in checklist")
        print("  - All PR numbers verified against source")
        print("  - No fabricated content detected")
    else:
        print("\n❌ VALIDATION FAILED")
        print(f"  - {len(errors)} critical errors")
        print(f"  - {len(missing_guides)} missing guides")
        print(f"  - {len(warnings)} warnings")

    print("=" * 60)

    # Exit with error code if validation failed
    if errors or missing_guides:
        sys.exit(1)


if __name__ == '__main__':
    main()
