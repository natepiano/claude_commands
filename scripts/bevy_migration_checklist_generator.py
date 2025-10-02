#!/usr/bin/env python3
"""
Bevy Migration Checklist Generator - Pass 1

Parses Bevy migration guide markdown files and generates a comprehensive
migration checklist with search patterns and code examples.

Outputs both JSON (for Pass 2 semantic review) and basic markdown.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast


@dataclass
class CodeBlock:
    """Represents a code example with optional label"""
    label: str  # e.g., "Old", "New", "Before", "After", or ""
    code: str


@dataclass
class MigrationGuide:
    """Represents a single migration guide"""
    index: int  # Position in the list for ordering
    title: str
    pull_requests: list[int] = field(default_factory=list)
    description_paragraphs: list[str] = field(default_factory=list)
    bullet_points: list[str] = field(default_factory=list)
    code_blocks: list[dict[str, str]] = field(default_factory=list)
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

        for index, guide_file in enumerate(guide_files):
            guide = self._parse_guide_file(guide_file, index)
            if guide:
                self.guides.append(guide)

        print(f"Parsed {len(self.guides)} migration guides", file=sys.stderr)

    def _parse_guide_file(self, file_path: Path, index: int) -> MigrationGuide | None:
        """Parse a single migration guide file with enhanced extraction"""
        try:
            content = file_path.read_text(encoding='utf-8')
        except Exception as e:
            print(f"Error reading {file_path}: {e}", file=sys.stderr)
            return None

        guide = MigrationGuide(index=index, title="")

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

        # Extract all description paragraphs (stop at first code block or bullet list)
        body_lines = body.strip().split('\n')
        current_paragraph: list[str] = []

        for line in body_lines:
            stripped = line.strip()

            # Stop at code blocks or bullet lists
            if stripped.startswith('```') or stripped.startswith('- ') or stripped.startswith('* '):
                if current_paragraph:
                    guide.description_paragraphs.append(' '.join(current_paragraph).strip())
                break

            # Empty line ends a paragraph
            if not stripped:
                if current_paragraph:
                    guide.description_paragraphs.append(' '.join(current_paragraph).strip())
                    current_paragraph = []
            else:
                current_paragraph.append(stripped)

        # Don't forget last paragraph if file ends
        if current_paragraph:
            guide.description_paragraphs.append(' '.join(current_paragraph).strip())

        # Extract all bullet points (preserve structure)
        bullet_matches: list[str | tuple[str, ...]] = re.findall(r'^\s*[-*]\s+(.+)$', body, re.MULTILINE)
        guide.bullet_points = [text for text in bullet_matches if isinstance(text, str)]

        # Extract all code blocks with labels
        guide.code_blocks = self._extract_code_blocks(body)

        # Extract search patterns from code and description
        all_text = ' '.join(guide.description_paragraphs)
        guide.search_patterns = self._extract_patterns_enhanced(guide.code_blocks, all_text)

        # Generate checklist items
        guide.checklist_items = self._generate_checklist_items(guide)

        return guide

    def _extract_code_blocks(self, body: str) -> list[dict[str, str]]:
        """Extract code blocks with context labels"""
        blocks: list[dict[str, str]] = []

        # Find all code blocks
        code_pattern = r'```(?:rust)?\s*\n(.*?)\n```'
        matches = list(re.finditer(code_pattern, body, re.DOTALL))

        for match in matches:
            code = match.group(1).strip()
            start_pos = match.start()

            # Look at preceding text (up to 100 chars) for context
            before_text = body[max(0, start_pos - 100):start_pos].lower()

            # Determine label
            label = ""
            if '// old' in code.lower()[:50] or 'old' in before_text[-50:]:
                label = "Old"
            elif '// new' in code.lower()[:50] or 'new' in before_text[-50:]:
                label = "New"
            elif '// before' in code.lower()[:50] or 'before' in before_text[-50:]:
                label = "Before"
            elif '// after' in code.lower()[:50] or 'after' in before_text[-50:]:
                label = "After"

            blocks.append({"label": label, "code": code})

        return blocks

    def _extract_patterns_enhanced(self, code_blocks: list[dict[str, str]], description: str) -> list[str]:
        """Extract searchable patterns from code and description"""
        patterns: list[str] = []

        # Extract from backticks in description
        backtick_patterns = re.findall(r'`([^`]+)`', description)
        patterns.extend(backtick_patterns)

        # Extract from all code blocks
        for block in code_blocks:
            code = block['code']

            # Find type names (PascalCase or snake_case with ::)
            types = re.findall(r'\b([A-Z][a-zA-Z0-9]*(?:<[^>]+>)?)\b', code)
            patterns.extend(types)

            # Find function/method calls
            functions = re.findall(r'\b([a-z_][a-z0-9_]*)\s*\(', code)
            patterns.extend(functions)

            # Find module paths
            modules = re.findall(r'\b([a-z_][a-z0-9_]*::[a-z_:][a-z0-9_:]*)', code)
            patterns.extend(modules)

            # Find derive attributes
            derives: list[str | tuple[str, ...]] = re.findall(r'#\[derive\(([^)]+)\)\]', code)
            for derive_match in derives:
                if isinstance(derive_match, str):
                    derive_items = [d.strip() for d in derive_match.split(',')]
                    patterns.extend(derive_items)

            # Find attributes
            attributes = re.findall(r'#\[([a-z_]+)(?:\([^)]*\))?\]', code)
            patterns.extend(attributes)

        # Remove duplicates and common words
        stopwords = {'fn', 'let', 'mut', 'if', 'else', 'for', 'while', 'loop', 'match', 'return',
                    'self', 'Self', 'use', 'pub', 'mod', 'impl', 'trait', 'struct', 'enum',
                    'const', 'static', 'crate', 'super', 'where', 'async', 'await', 'move',
                    'ref', 'in', 'as', 'break', 'continue', 'type', 'unsafe', 'extern'}
        unique_patterns = list(set(p for p in patterns if p and p not in stopwords and len(p) > 1))

        return sorted(unique_patterns)[:30]  # Limit to top 30 patterns

    def _generate_checklist_items(self, guide: MigrationGuide) -> list[str]:
        """Generate checklist items from guide content"""
        items: list[str] = []

        # Use bullet points if available
        if guide.bullet_points:
            items.extend(guide.bullet_points[:10])  # Limit to first 10

        # Look for rename patterns in description
        all_desc = ' '.join(guide.description_paragraphs)
        if 'rename' in all_desc.lower():
            rename_matches: list[tuple[str, str] | str] = re.findall(r'`([^`]+)`.*?(?:to|→|renamed to).*?`([^`]+)`', all_desc)
            for match_item in rename_matches:
                if isinstance(match_item, tuple) and len(match_item) == 2:
                    old_name = match_item[0]
                    new_name = match_item[1]
                    items.append(f"Rename `{old_name}` to `{new_name}`")

        # If no items yet, use title
        if not items and guide.title:
            items.append(guide.title)

        return items

    def generate_markdown(self) -> str:
        """Generate the basic markdown checklist"""
        lines: list[str] = []

        lines.append(f"# Bevy {self.version} Migration Checklist - Pass 1")
        lines.append("")
        lines.append("Generated from official Bevy migration guides (basic extraction).")
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

            # Description
            for para in guide.description_paragraphs:
                lines.append(para)
                lines.append("")

            # Bullet points
            if guide.bullet_points:
                for bullet in guide.bullet_points:
                    lines.append(f"- {bullet}")
                lines.append("")

            # Checklist items
            if guide.checklist_items:
                lines.append("**Checklist:**")
                for item in guide.checklist_items:
                    lines.append(f"- [ ] {item}")
                lines.append("")

            # Search patterns
            if guide.search_patterns:
                patterns_str = '`, `'.join(guide.search_patterns)
                lines.append(f"**Search Patterns:** `{patterns_str}`")
                lines.append("")

            # Code examples
            for block in guide.code_blocks:
                if block['label']:
                    lines.append(f"**{block['label']}:**")
                lines.append("```rust")
                lines.append(block['code'])
                lines.append("```")
                lines.append("")

            lines.append("---")
            lines.append("")

        return '\n'.join(lines)

    def generate_json(self) -> str:
        """Generate JSON data for Pass 2"""
        data = {
            "version": self.version,
            "total_guides": len(self.guides),
            "guides": [
                {
                    "index": g.index,
                    "title": g.title,
                    "pull_requests": g.pull_requests,
                    "description_paragraphs": g.description_paragraphs,
                    "bullet_points": g.bullet_points,
                    "code_blocks": g.code_blocks,
                    "search_patterns": g.search_patterns,
                    "checklist_items": g.checklist_items
                }
                for g in self.guides
            ]
        }

        return json.dumps(data, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Generate Bevy migration checklist from migration guides - Pass 1'
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
        '--work-dir',
        type=Path,
        required=True,
        help='Working directory for transient files'
    )

    args = parser.parse_args()

    version = cast(str, args.version)
    guides_dir = cast(Path, args.guides_dir)
    work_dir = cast(Path, args.work_dir)

    # Create work directory
    work_dir.mkdir(parents=True, exist_ok=True)

    output_path = work_dir / f"bevy-{version}-checklist-pass1.md"
    json_output_path = work_dir / f"bevy-{version}-checklist-pass1.json"

    # Generate checklist
    generator = ChecklistGenerator(guides_dir, version)
    generator.parse_guides()

    # Write markdown
    markdown = generator.generate_markdown()
    _ = output_path.write_text(markdown, encoding='utf-8')

    # Write JSON (always needed for Pass 2)
    json_data = generator.generate_json()
    _ = json_output_path.write_text(json_data, encoding='utf-8')

    print(f"✓ Basic checklist generated: {output_path}", file=sys.stderr)
    print(f"✓ JSON data generated: {json_output_path}", file=sys.stderr)
    print(f"✓ Found {len(generator.guides)} migration items", file=sys.stderr)


if __name__ == '__main__':
    main()
