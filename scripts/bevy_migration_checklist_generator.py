#!/usr/bin/env python3
"""
Bevy Migration Checklist Generator

Parses Bevy migration guide markdown files and generates a comprehensive
migration checklist with search patterns and code examples.
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast


@dataclass
class MigrationGuide:
    """Represents a single migration guide"""
    title: str
    pull_requests: list[int] = field(default_factory=list)
    description: str = ""
    old_code: str = ""
    new_code: str = ""
    checklist_items: list[str] = field(default_factory=list)
    search_patterns: list[str] = field(default_factory=list)


class ChecklistGenerator:
    """Generates migration checklist from Bevy migration guides"""

    def __init__(self, guides_dir: Path, version: str) -> None:
        self.guides_dir: Path = guides_dir
        self.version: str = version
        self.guides: list[MigrationGuide] = []

    def parse_guides(self) -> None:
        """Parse all migration guide files"""
        if not self.guides_dir.exists():
            print(f"Error: Guides directory not found: {self.guides_dir}", file=sys.stderr)
            sys.exit(1)

        guide_files = sorted(self.guides_dir.glob('*.md'))
        if not guide_files:
            print(f"Error: No migration guides found in {self.guides_dir}", file=sys.stderr)
            sys.exit(1)

        print(f"Parsing {len(guide_files)} migration guides...", file=sys.stderr)

        for guide_file in guide_files:
            guide = self._parse_guide_file(guide_file)
            if guide:
                self.guides.append(guide)

        print(f"Parsed {len(self.guides)} migration guides", file=sys.stderr)

    def _parse_guide_file(self, file_path: Path) -> MigrationGuide | None:
        """Parse a single migration guide file"""
        try:
            content = file_path.read_text(encoding='utf-8')
        except Exception as e:
            print(f"Error reading {file_path}: {e}", file=sys.stderr)
            return None

        guide = MigrationGuide(title="")

        # Parse frontmatter
        frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
        if frontmatter_match:
            frontmatter = frontmatter_match.group(1)
            body = frontmatter_match.group(2)

            # Extract title
            title_match = re.search(r'title:\s*(.+)', frontmatter)
            if title_match:
                guide.title = title_match.group(1).strip()

            # Extract pull requests
            pr_match = re.search(r'pull_requests:\s*\[([\d,\s]+)\]', frontmatter)
            if pr_match:
                prs = [int(pr.strip()) for pr in pr_match.group(1).split(',')]
                guide.pull_requests = prs
        else:
            body = content
            # Use filename as title if no frontmatter
            guide.title = file_path.stem.replace('_', ' ').replace('-', ' ').title()

        # Extract description (first paragraph after frontmatter)
        desc_match = re.search(r'^(.+?)\n\n', body.strip())
        if desc_match:
            guide.description = desc_match.group(1).strip()

        # Extract code examples (Old/New pattern)
        code_blocks = re.findall(r'```(?:rust)?\s*\n(.*?)\n```', body, re.DOTALL)

        # Look for Old/New or Before/After patterns
        for block in code_blocks:
            # Check if this block is preceded by "Old" or "New"
            before_block = body[:body.find(f'```rust\n{block}') if f'```rust\n{block}' in body else body.find(f'```\n{block}')]
            if 'old' in before_block[-50:].lower() or 'before' in before_block[-50:].lower():
                guide.old_code = block.strip()
            elif 'new' in before_block[-50:].lower() or 'after' in before_block[-50:].lower():
                guide.new_code = block.strip()

        # Extract search patterns from old code and description
        guide.search_patterns = self._extract_patterns(guide.old_code, guide.description)

        # Generate checklist items from description and code
        guide.checklist_items = self._generate_checklist_items(guide)

        return guide

    def _extract_patterns(self, old_code: str, description: str) -> list[str]:
        """Extract searchable patterns from code and description"""
        patterns: list[str] = []

        # Extract from backticks in description
        backtick_patterns = re.findall(r'`([^`]+)`', description)
        patterns.extend(backtick_patterns)

        # Extract from old code if available
        if old_code:
            # Find type names (PascalCase)
            types = re.findall(r'\b([A-Z][a-zA-Z]+(?:<[^>]+>)?)\b', old_code)
            patterns.extend(types)

            # Find function calls
            functions = re.findall(r'\b([a-z_]+)\s*\(', old_code)
            patterns.extend(functions)

            # Find module paths
            modules = re.findall(r'\b([a-z_]+::[a-z_:]+)', old_code)
            patterns.extend(modules)

        # Remove duplicates and common words
        stopwords = {'fn', 'let', 'mut', 'if', 'else', 'for', 'while', 'loop', 'match', 'return',
                    'self', 'Self', 'use', 'pub', 'mod', 'impl', 'trait', 'struct', 'enum'}
        patterns = list(set(p for p in patterns if p and p not in stopwords and len(p) > 2))

        return sorted(patterns)[:20]  # Limit to top 20 patterns

    def _generate_checklist_items(self, guide: MigrationGuide) -> list[str]:
        """Generate checklist items from guide content"""
        items: list[str] = []

        # Look for bullet points in description
        bullet_matches = re.findall(r'^\s*[-*]\s+(.+)$', guide.description, re.MULTILINE)
        if bullet_matches:
            items.extend(bullet_matches)

        # If no explicit items, create from title
        if not items and guide.title:
            items.append(guide.title)

        # Look for rename patterns
        if 'rename' in guide.description.lower():
            rename_matches = re.findall(r'`([^`]+)`.*?(?:to|→).*?`([^`]+)`', guide.description)
            for old, new in rename_matches:
                items.append(f"Rename `{old}` to `{new}`")

        return items

    def generate_checklist(self) -> str:
        """Generate the complete checklist markdown"""
        lines: list[str] = []

        lines.append(f"# Bevy {self.version} Migration Checklist")
        lines.append("")
        lines.append(f"Generated from official Bevy migration guides.")
        lines.append(f"Total migration items: {len(self.guides)}")
        lines.append("")
        lines.append("---")
        lines.append("")

        for guide in self.guides:
            lines.append(f"## {guide.title}")
            lines.append("")

            if guide.pull_requests:
                pr_links = ', '.join(f"#{pr}" for pr in guide.pull_requests)
                lines.append(f"**Pull Requests:** {pr_links}")
                lines.append("")

            if guide.description:
                lines.append(guide.description)
                lines.append("")

            # Checklist items
            if guide.checklist_items:
                for item in guide.checklist_items:
                    lines.append(f"- [ ] {item}")
                lines.append("")

            # Search patterns
            if guide.search_patterns:
                patterns_str = '`, `'.join(guide.search_patterns)
                lines.append(f"**Search Patterns:** `{patterns_str}`")
                lines.append("")

            # Code examples
            if guide.old_code and guide.new_code:
                lines.append("**Official Example:**")
                lines.append("")
                lines.append("```rust")
                lines.append("// Old")
                lines.append(guide.old_code)
                lines.append("```")
                lines.append("")
                lines.append("```rust")
                lines.append("// New")
                lines.append(guide.new_code)
                lines.append("```")
                lines.append("")

            lines.append("---")
            lines.append("")

        return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Generate Bevy migration checklist from migration guides'
    )
    _ = parser.add_argument(
        '--version',
        type=str,
        required=True,
        help='Bevy version (e.g., 0.17.0)'
    )
    _ = parser.add_argument(
        '--guides-dir',
        type=Path,
        required=True,
        help='Path to migration-guides directory'
    )
    _ = parser.add_argument(
        '--output',
        type=Path,
        required=True,
        help='Output checklist file path'
    )

    args = parser.parse_args()

    version = cast(str, args.version)
    guides_dir = cast(Path, args.guides_dir)
    output_path = cast(Path, args.output)

    # Generate checklist
    generator = ChecklistGenerator(guides_dir, version)
    generator.parse_guides()
    checklist = generator.generate_checklist()

    # Write to file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _ = output_path.write_text(checklist, encoding='utf-8')

    print(f"✓ Checklist generated: {output_path}", file=sys.stderr)
    print(f"✓ Found {len(generator.guides)} migration items", file=sys.stderr)


if __name__ == '__main__':
    main()
