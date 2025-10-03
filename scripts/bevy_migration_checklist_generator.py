#!/usr/bin/env python3
"""
Bevy Migration Checklist Generator

Parses Bevy migration guide markdown files and generates a comprehensive
migration checklist with actionable items, search patterns, and code examples.

This is a single-pass deterministic generator that produces accurate output
without LLM enhancement.
"""

import argparse
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
    code_blocks: list[CodeBlock] = field(default_factory=list)
    checklist_items: list[str] = field(default_factory=list)
    search_patterns: list[str] = field(default_factory=list)
    change_type: str = ""  # rename, remove, move, add, etc.


class ChecklistGenerator:
    """Generates migration checklist from Bevy migration guides"""

    def __init__(self, guides_dir: Path, version: str, output_path: Path) -> None:
        self.guides_dir: Path = guides_dir
        self.version: str = version
        self.output_path: Path = output_path
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

        print(f"✓ Parsed {len(self.guides)} migration guides", file=sys.stderr)

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

        # Identify change type
        guide.change_type = self._identify_change_type(guide)

        # Extract search patterns from code and description (prioritized)
        guide.search_patterns = self._extract_patterns_prioritized(guide)

        # Generate enhanced checklist items
        guide.checklist_items = self._generate_actionable_checklist_items(guide)

        return guide

    def _extract_code_blocks(self, body: str) -> list[CodeBlock]:
        """Extract code blocks with context labels"""
        blocks: list[CodeBlock] = []

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

            blocks.append(CodeBlock(label=label, code=code))

        return blocks

    def _identify_change_type(self, guide: MigrationGuide) -> str:
        """Identify the type of change from title and description"""
        title_lower = guide.title.lower()
        desc = ' '.join(guide.description_paragraphs).lower()

        if 'renamed' in title_lower or 'rename' in title_lower:
            return 'rename'
        elif 'removed' in title_lower or 'remove' in title_lower or 'deprecat' in title_lower:
            return 'remove'
        elif 'moved to' in desc or 'moved to' in title_lower:
            return 'move'
        elif 'split' in title_lower:
            return 'split'
        elif 'add' in title_lower:
            return 'add'
        elif 'replac' in title_lower:
            return 'replace'
        else:
            return 'change'

    def _extract_patterns_prioritized(self, guide: MigrationGuide) -> list[str]:
        """Extract searchable patterns with priority ranking"""
        patterns: set[str] = set()
        priority_patterns: set[str] = set()

        # Priority 1: Extract from "Old" or "Before" code blocks
        for block in guide.code_blocks:
            if block.label in ['Old', 'Before']:
                priority_patterns.update(self._extract_identifiers_from_code(block.code))

        # Priority 2: Backtick-quoted identifiers in description
        desc = ' '.join(guide.description_paragraphs)
        backtick_matches: list[str | tuple[str, ...]] = re.findall(r'`([^`]+)`', desc)
        for match in backtick_matches:
            if isinstance(match, str):
                # Filter out full sentences and very long patterns
                if len(match) < 50 and '.' not in match[:10]:
                    patterns.add(match)

        # Priority 3: All code blocks (lower priority)
        for block in guide.code_blocks:
            if block.label not in ['Old', 'Before']:
                patterns.update(self._extract_identifiers_from_code(block.code))

        # Combine with priority first
        all_patterns = list(priority_patterns) + [p for p in patterns if p not in priority_patterns]

        # Remove stopwords and common terms
        stopwords = {'fn', 'let', 'mut', 'if', 'else', 'for', 'while', 'loop', 'match', 'return',
                    'self', 'Self', 'use', 'pub', 'mod', 'impl', 'trait', 'struct', 'enum',
                    'const', 'static', 'crate', 'super', 'where', 'async', 'await', 'move',
                    'ref', 'in', 'as', 'break', 'continue', 'type', 'unsafe', 'extern', 'true',
                    'false', 'None', 'Some', 'Ok', 'Err', 'Box', 'Vec', 'Option', 'Result'}

        filtered = [p for p in all_patterns if p and p not in stopwords and len(p) > 1]

        return filtered[:30]  # Limit to top 30 patterns

    def _extract_identifiers_from_code(self, code: str) -> set[str]:
        """Extract identifiers (types, functions, traits) from code"""
        identifiers: set[str] = set()

        # Find type names (PascalCase)
        types = re.findall(r'\b([A-Z][a-zA-Z0-9]*(?:<[^>]+>)?)\b', code)
        identifiers.update(types)

        # Find function/method calls
        functions = re.findall(r'\b([a-z_][a-z0-9_]*)\s*\(', code)
        identifiers.update(functions)

        # Find module paths
        modules = re.findall(r'\b([a-z_][a-z0-9_]*::[a-z_:][a-z0-9_:]*)', code)
        identifiers.update(modules)

        # Find derive attributes
        derives: list[str | tuple[str, ...]] = re.findall(r'#\[derive\(([^)]+)\)\]', code)
        for derive_match in derives:
            if isinstance(derive_match, str):
                derive_items = [d.strip() for d in derive_match.split(',')]
                identifiers.update(derive_items)

        # Find attributes
        attributes = re.findall(r'#\[([a-z_]+)(?:\([^)]*\))?\]', code)
        identifiers.update(attributes)

        return identifiers

    def _generate_actionable_checklist_items(self, guide: MigrationGuide) -> list[str]:
        """Generate specific, actionable checklist items"""
        items: list[str] = []

        # Extract from code changes (before/after pairs)
        code_items = self._extract_code_changes(guide.code_blocks)
        items.extend(code_items)

        # Use bullet points if available and specific
        if guide.bullet_points:
            for bullet in guide.bullet_points[:10]:
                # Only add if it contains specific identifiers (backticks or PascalCase)
                if '`' in bullet or re.search(r'\b[A-Z][a-zA-Z0-9]+\b', bullet):
                    items.append(bullet)

        # Extract rename patterns from description
        desc = ' '.join(guide.description_paragraphs)
        rename_items = self._extract_rename_actions(desc)
        items.extend(rename_items)

        # Add requirements markers based on keywords
        marked_items: list[str] = []
        for item in items:
            item_lower = item.lower()
            if any(word in item_lower for word in ['must', 'required', 'breaking', 'removed', 'no longer']):
                if not item.startswith('**'):
                    marked_items.append(f"**REQUIRED:** {item}")
                else:
                    marked_items.append(item)
            else:
                marked_items.append(item)

        # If no specific items, use title as fallback
        if not marked_items and guide.title:
            marked_items.append(guide.title)

        # Remove duplicates while preserving order
        seen: set[str] = set()
        unique_items: list[str] = []
        for item in marked_items:
            if item not in seen:
                seen.add(item)
                unique_items.append(item)

        return unique_items

    def _extract_code_changes(self, code_blocks: list[CodeBlock]) -> list[str]:
        """Extract specific changes from before/after code pairs"""
        changes: list[str] = []

        # Find old/new or before/after pairs
        for i, block in enumerate(code_blocks):
            if block.label in ['Old', 'Before']:
                # Look for corresponding new/after block
                next_block = code_blocks[i + 1] if i + 1 < len(code_blocks) else None
                if next_block and next_block.label in ['New', 'After']:
                    changes.extend(self._compare_code_blocks(block.code, next_block.code))

        return changes

    def _compare_code_blocks(self, old_code: str, new_code: str) -> list[str]:
        """Compare two code blocks to find specific changes"""
        changes: list[str] = []

        # Extract identifiers from both
        old_ids = self._extract_identifiers_from_code(old_code)
        new_ids = self._extract_identifiers_from_code(new_code)

        # Find renames (present in both with similar context)
        # Simple heuristic: if one identifier disappeared and one appeared, it might be a rename
        removed = old_ids - new_ids
        added = new_ids - old_ids

        # If we have equal counts of removed and added, suggest renames
        if len(removed) <= 3 and len(added) <= 3 and len(removed) > 0:
            for old_id in sorted(removed):
                for new_id in sorted(added):
                    # Check if they're similar types (both PascalCase or both snake_case)
                    if (old_id[0].isupper() == new_id[0].isupper()):
                        changes.append(f"Replace `{old_id}` with `{new_id}`")
                        break

        # Find removed APIs
        if len(removed) > 0 and len(added) == 0:
            for old_id in sorted(removed)[:5]:
                changes.append(f"Remove usage of `{old_id}`")

        # Find added APIs
        if len(added) > 0 and len(removed) == 0:
            for new_id in sorted(added)[:5]:
                changes.append(f"Use new `{new_id}` API")

        return changes

    def _extract_rename_actions(self, description: str) -> list[str]:
        """Extract rename actions from description text"""
        actions: list[str] = []

        # Pattern: `old` renamed to `new`
        rename_pattern = r'`([^`]+)`\s+(?:has been\s+)?(?:renamed|changed)\s+to\s+`([^`]+)`'
        rename_matches: list[tuple[str, str] | str] = re.findall(rename_pattern, description)
        for match in rename_matches:
            if isinstance(match, tuple) and len(match) == 2:
                old_name = match[0]
                new_name = match[1]
                actions.append(f"Rename `{old_name}` to `{new_name}`")

        # Pattern: `old` → `new` or `old` -> `new`
        arrow_pattern = r'`([^`]+)`\s*(?:→|->)\s*`([^`]+)`'
        arrow_matches: list[tuple[str, str] | str] = re.findall(arrow_pattern, description)
        for match in arrow_matches:
            if isinstance(match, tuple) and len(match) == 2:
                old_name = match[0]
                new_name = match[1]
                actions.append(f"Replace `{old_name}` with `{new_name}`")

        return actions

    def _enhance_description_with_context(self, guide: MigrationGuide) -> str:
        """Add contextual guidance based on change type"""
        base = '\n\n'.join(guide.description_paragraphs)

        # Add actionable context based on change type
        if guide.change_type == 'rename':
            context = "\n\n*Migration approach: Use search-and-replace to update all occurrences in your codebase.*"
        elif guide.change_type == 'remove':
            context = "\n\n*Migration approach: Remove all usage of deprecated APIs and replace with recommended alternatives.*"
        elif guide.change_type == 'move':
            context = "\n\n*Migration approach: Update import statements to use the new module path.*"
        elif guide.change_type == 'split':
            context = "\n\n*Migration approach: Review usage of the split type and update to use the appropriate variant.*"
        else:
            context = ""

        return base + context if context else base

    def generate_final_checklist(self) -> str:
        """Generate the final markdown checklist"""
        lines: list[str] = []

        lines.append(f"# Bevy {self.version} Migration Checklist")
        lines.append("")
        lines.append("Generated from official Bevy migration guides.")
        lines.append(f"Total migration items: {len(self.guides)}")
        lines.append("")
        lines.append("Each section includes:")
        lines.append("- PR numbers linking to the actual changes")
        lines.append("- Description of what changed and why")
        lines.append("- Actionable checklist items")
        lines.append("- Search patterns to find affected code")
        lines.append("- Official code examples showing before/after")
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

            # Enhanced description with context
            enhanced_desc = self._enhance_description_with_context(guide)
            if enhanced_desc:
                lines.append(enhanced_desc)
                lines.append("")

            # Checklist items
            if guide.checklist_items:
                lines.append("**Migration Checklist:**")
                for item in guide.checklist_items:
                    lines.append(f"- [ ] {item}")
                lines.append("")

            # Search patterns
            if guide.search_patterns:
                patterns_str = '`, `'.join(guide.search_patterns[:15])  # Limit display
                lines.append(f"**Search for:** `{patterns_str}`")
                lines.append("")

            # Code examples
            if guide.code_blocks:
                has_labeled = any(block.label for block in guide.code_blocks)
                if has_labeled:
                    lines.append("**Code Examples:**")
                    lines.append("")

                for block in guide.code_blocks:
                    if block.label:
                        lines.append(f"*{block.label}:*")
                    lines.append("```rust")
                    lines.append(block.code)
                    lines.append("```")
                    lines.append("")

            lines.append("---")
            lines.append("")

        return '\n'.join(lines)

    def write_output(self) -> None:
        """Write the final checklist to output file"""
        checklist = self.generate_final_checklist()

        # Ensure output directory exists
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write output
        _ = self.output_path.write_text(checklist, encoding='utf-8')

        print(f"✓ Final checklist generated: {self.output_path}", file=sys.stderr)
        print(f"✓ Total sections: {len(self.guides)}", file=sys.stderr)


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
        help='Output path for final checklist'
    )

    args = parser.parse_args()

    version = cast(str, args.version)
    guides_dir = cast(Path, args.guides_dir)
    output_path = cast(Path, args.output)

    # Generate checklist
    generator = ChecklistGenerator(guides_dir, version, output_path)
    generator.parse_guides()
    generator.write_output()


if __name__ == '__main__':
    main()
