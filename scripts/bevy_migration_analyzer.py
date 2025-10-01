#!/usr/bin/env python3
"""
Bevy Migration Gap Analysis Tool

Analyzes a Bevy codebase against a migration checklist to identify:
- Which checklist items are actually used in the codebase
- Which items are already covered in the migration plan
- Which items need to be added to the migration plan
- Which items are not applicable (not used in codebase)
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast


@dataclass
class ChecklistItem:
    """Represents a single item from the migration checklist"""
    section: str
    description: str
    patterns: list[str] = field(default_factory=list)
    line_number: int = 0


@dataclass
class CodeMatch:
    """Represents a code match with context"""
    file_path: str
    context_before: str
    matching_line: str
    context_after: str
    function_signature: str = ""  # Extracted function/struct signature for context


@dataclass
class SearchResult:
    """Results from searching for a pattern in the codebase"""
    pattern: str
    files: list[str] = field(default_factory=list)
    count: int = 0
    code_matches: list[CodeMatch] = field(default_factory=list)  # Code snippets with context


@dataclass
class MigrationGuide:
    """Represents an example from Bevy migration guides"""
    title: str
    before_code: str
    after_code: str
    description: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """Analysis result for a checklist item"""
    item: ChecklistItem
    search_results: list[SearchResult]
    total_occurrences: int
    is_in_plan: bool
    category: str  # CRITICAL, HIGH, MEDIUM, LOW, NOT_FOUND
    update_size: str  # Minor, Medium, Major, NOT_FOUND
    migration_guide: MigrationGuide | None = None  # Associated migration guide example


class MigrationAnalyzer:
    """Analyzes codebase against migration checklist"""

    # Categorization thresholds
    OCCURRENCE_THRESHOLDS: dict[str, tuple[int, float]] = {
        'NOT_FOUND': (0, 0),
        'Minor': (1, 5),
        'Medium': (6, 20),
        'Major': (21, float('inf'))
    }

    # Keywords that indicate different impact levels
    CRITICAL_KEYWORDS: list[str] = ['CRITICAL', 'will break compilation', 'MAJOR CHANGE']
    HIGH_KEYWORDS: list[str] = ['will break runtime', 'breaking change']
    MEDIUM_KEYWORDS: list[str] = ['may cause issues', 'deprecated']

    def __init__(self, codebase_path: Path, checklist_path: Path, plan_path: Path,
                 guides_path: Path | None = None) -> None:
        self.codebase_path: Path = codebase_path
        self.checklist_path: Path = checklist_path
        self.plan_path: Path = plan_path
        self.plan_content: str = self._read_file(plan_path) if plan_path.exists() else ""
        self.migration_guides: list[MigrationGuide] = []

        # Load migration guides if path provided
        if guides_path and guides_path.exists():
            self.migration_guides = self._parse_migration_guides(guides_path)

    def _read_file(self, path: Path) -> str:
        """Read file contents"""
        try:
            return path.read_text(encoding='utf-8')
        except Exception as e:
            print(f"Error reading {path}: {e}", file=sys.stderr)
            return ""

    def parse_checklist(self) -> list[ChecklistItem]:
        """Parse the migration checklist to extract items and search patterns"""
        content = self._read_file(self.checklist_path)
        items: list[ChecklistItem] = []
        current_section = "Unknown"

        lines = content.split('\n')
        for i, line in enumerate(lines):
            # Track section headers
            if line.startswith('## '):
                current_section = line.replace('##', '').strip()
                continue

            # Look for checklist items
            if line.strip().startswith('- [ ]'):
                description = line.replace('- [ ]', '').strip()

                # Extract patterns from the description
                patterns = self._extract_patterns(description)

                items.append(ChecklistItem(
                    section=current_section,
                    description=description,
                    patterns=patterns,
                    line_number=i + 1
                ))

        return items

    def _extract_patterns(self, text: str) -> list[str]:
        """Extract searchable patterns from item description"""
        patterns: list[str] = []

        # Look for backtick-quoted identifiers
        backtick_patterns = re.findall(r'`([^`]+)`', text)
        patterns.extend(backtick_patterns)

        # Look for specific Bevy types/traits
        bevy_patterns = re.findall(r'\b(bevy_[a-z_]+(?:::[a-z_]+)*)\b', text)
        patterns.extend(bevy_patterns)

        # Look for common API patterns
        api_patterns = [
            r'\b(\w+::\w+)\b',  # Module::Item
            r'\b([A-Z][a-zA-Z]+(?:<[^>]+>)?)\b'  # PascalCase types
        ]

        for pattern in api_patterns:
            matches = re.findall(pattern, text)
            patterns.extend(matches)

        # Remove duplicates and filter out common words
        stopwords = {'Handle', 'System', 'World', 'Entity', 'Component', 'Resource',
                    'Update', 'Query', 'Event', 'Plugin', 'App', 'Commands'}
        patterns = list(set(p for p in patterns if p and p not in stopwords))

        return patterns

    def search_pattern(self, pattern: str) -> SearchResult:
        """Search for a pattern in the codebase using ripgrep with context"""
        result = SearchResult(pattern=pattern)

        try:
            # Use ripgrep for fast searching with context
            cmd = [
                'rg',
                '--context', '5',  # Get 5 lines before and after
                '--type', 'rust',
                '--ignore-case',
                pattern,
                str(self.codebase_path)
            ]

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if proc.returncode == 0:
                # Parse ripgrep output to extract matches with context
                code_matches = self._parse_rg_output(proc.stdout)
                result.code_matches = code_matches
                result.files = list(set(match.file_path for match in code_matches))
                result.count = len(result.files)

        except subprocess.TimeoutExpired:
            print(f"Search timeout for pattern: {pattern}", file=sys.stderr)
        except FileNotFoundError:
            # Fallback to grep if ripgrep not available
            result = self._search_with_grep(pattern)
        except Exception as e:
            print(f"Search error for pattern '{pattern}': {e}", file=sys.stderr)

        return result

    def _parse_rg_output(self, output: str) -> list[CodeMatch]:
        """Parse ripgrep output with context to extract code matches"""
        matches: list[CodeMatch] = []
        lines = output.split('\n')

        current_file = ""
        context_before: list[str] = []
        matching_line = ""
        context_after: list[str] = []
        in_context_after = False

        for line in lines:
            if not line.strip():
                # Empty line indicates end of match block
                if matching_line and current_file:
                    function_sig = self._extract_function_signature('\n'.join(context_before + [matching_line]))
                    matches.append(CodeMatch(
                        file_path=current_file,
                        context_before='\n'.join(context_before),
                        matching_line=matching_line,
                        context_after='\n'.join(context_after),
                        function_signature=function_sig
                    ))
                # Reset for next match
                context_before = []
                matching_line = ""
                context_after = []
                in_context_after = False
                continue

            # Check if this is a file path line
            if line and not line.startswith('-') and not line.startswith(':'):
                # Might be a file path
                if ':' in line:
                    current_file = line.split(':')[0]
                    continue

            # Context before match (lines starting with -)
            if line.startswith('-'):
                if not matching_line:
                    context_before.append(line[1:])  # Remove the '-' prefix
                elif in_context_after:
                    context_after.append(line[1:])

            # Matching line (starts with :)
            elif line.startswith(':'):
                matching_line = line[1:]  # Remove the ':' prefix
                in_context_after = True

        # Handle last match if exists
        if matching_line and current_file:
            function_sig = self._extract_function_signature('\n'.join(context_before + [matching_line]))
            matches.append(CodeMatch(
                file_path=current_file,
                context_before='\n'.join(context_before),
                matching_line=matching_line,
                context_after='\n'.join(context_after),
                function_signature=function_sig
            ))

        return matches

    def _extract_function_signature(self, code: str) -> str:
        """Extract function or struct signature from code context"""
        lines = code.split('\n')
        for line in reversed(lines):
            # Look for function definitions
            if 'fn ' in line or 'pub fn' in line:
                # Extract just the signature line
                sig = line.strip()
                return sig
            # Look for struct definitions
            if 'struct ' in line or 'pub struct' in line:
                sig = line.strip()
                return sig
            # Look for impl blocks
            if 'impl ' in line:
                sig = line.strip()
                return sig
        return ""

    def _is_relevant_match(self, item: ChecklistItem, code_match: CodeMatch) -> bool:
        """Check if a code match is semantically relevant (reduce false positives)"""
        pattern = item.description.lower()
        code_context = (code_match.context_before + code_match.matching_line +
                       code_match.context_after).lower()

        # For entity Index patterns
        if 'entity' in pattern and 'index' in pattern:
            # Only relevant if code contains Entity type or entity-related imports
            if 'entity' in code_context or 'bevy_ecs' in code_context:
                return True
            return False

        # For event attribute patterns
        if 'event' in pattern and 'traversal' in pattern:
            # Only relevant if in attribute context
            if '#[' in code_context and 'event' in code_context:
                return True
            return False

        # For Trigger/Observer patterns
        if 'trigger' in pattern or 'observer' in pattern:
            # Check for bevy_ecs imports or Trigger type usage
            if 'trigger' in code_context or 'observer' in code_context:
                return True
            return False

        # Default: accept the match
        return True

    def _filter_relevant_matches(self, item: ChecklistItem, search_result: SearchResult) -> SearchResult:
        """Filter search results to only include semantically relevant matches"""
        filtered_matches = [
            match for match in search_result.code_matches
            if self._is_relevant_match(item, match)
        ]

        return SearchResult(
            pattern=search_result.pattern,
            files=list(set(match.file_path for match in filtered_matches)),
            count=len(filtered_matches),
            code_matches=filtered_matches
        )

    def _parse_migration_guides(self, guides_path: Path) -> list[MigrationGuide]:
        """Parse Bevy migration guides from markdown files"""
        guides: list[MigrationGuide] = []

        # Look for markdown files in the guides directory
        if guides_path.is_file():
            files = [guides_path]
        else:
            files = list(guides_path.glob('*.md'))

        for file in files:
            content = self._read_file(file)
            if not content:
                continue

            # Extract title from first heading or filename
            title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
            title = title_match.group(1) if title_match else file.stem

            # Find code blocks with before/after patterns
            # Look for patterns like "Before:" followed by code block, then "After:" and code block
            before_after_pattern = r'(?:Before|Old).*?```(?:rust)?\s*\n(.*?)\n```.*?(?:After|New).*?```(?:rust)?\s*\n(.*?)\n```'
            matches = re.findall(before_after_pattern, content, re.DOTALL | re.IGNORECASE)

            for before_code, after_code in matches:
                # Extract description (text before the before block)
                desc_pattern = r'([^\n]+)\s*(?:Before|Old)'
                desc_match = re.search(desc_pattern, content)
                description = desc_match.group(1).strip() if desc_match else title

                # Extract keywords from before code
                keywords = re.findall(r'\b[A-Z][a-zA-Z]+\b', before_code)

                guides.append(MigrationGuide(
                    title=title,
                    before_code=before_code.strip(),
                    after_code=after_code.strip(),
                    description=description,
                    keywords=list(set(keywords))
                ))

        return guides

    def _find_matching_guide(self, item: ChecklistItem) -> MigrationGuide | None:
        """Find a migration guide that matches this checklist item"""
        if not self.migration_guides:
            return None

        # Try to match by patterns
        for guide in self.migration_guides:
            for pattern in item.patterns:
                if pattern in guide.keywords or pattern in guide.before_code:
                    return guide

        # Try to match by description
        for guide in self.migration_guides:
            # Check if key phrases from item appear in guide
            for pattern in item.patterns:
                if pattern.lower() in guide.description.lower():
                    return guide

        return None

    def _search_with_grep(self, pattern: str) -> SearchResult:
        """Fallback to grep if ripgrep not available"""
        result = SearchResult(pattern=pattern)

        try:
            cmd = [
                'grep',
                '-r',
                '-l',
                '--include=*.rs',
                pattern,
                str(self.codebase_path)
            ]

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if proc.returncode == 0:
                files = [line.strip() for line in proc.stdout.split('\n') if line.strip()]
                result.files = files
                result.count = len(files)

        except Exception as e:
            print(f"Grep search error for pattern '{pattern}': {e}", file=sys.stderr)

        return result

    def is_covered_in_plan(self, item: ChecklistItem) -> bool:
        """Check if item is already mentioned in the migration plan"""
        if not self.plan_content:
            return False

        # Check if any patterns from the item appear in the plan
        for pattern in item.patterns:
            if pattern in self.plan_content:
                return True

        # Check if key phrases from description appear
        key_phrases = self._extract_key_phrases(item.description)
        for phrase in key_phrases:
            if phrase.lower() in self.plan_content.lower():
                return True

        return False

    def _extract_key_phrases(self, text: str) -> list[str]:
        """Extract key identifying phrases from text"""
        phrases: list[str] = []

        # Remove common prefixes
        text = re.sub(r'^(Replace|Update|Rename|Move|Add|Remove|Handle|Fix)\s+', '', text)

        # Extract first significant phrase (up to 50 chars)
        if len(text) > 50:
            phrases.append(text[:50])
        else:
            phrases.append(text)

        return phrases

    def categorize_by_count(self, count: int) -> str:
        """Categorize update size based on occurrence count"""
        for category, (min_val, max_val) in self.OCCURRENCE_THRESHOLDS.items():
            if min_val <= count <= max_val:
                return category
        return 'Major'

    def determine_impact(self, item: ChecklistItem) -> str:
        """Determine impact level based on description keywords"""
        text = item.description.upper()

        if any(kw.upper() in text for kw in self.CRITICAL_KEYWORDS):
            return 'CRITICAL'
        elif any(kw.upper() in text for kw in self.HIGH_KEYWORDS):
            return 'HIGH'
        elif any(kw.upper() in text for kw in self.MEDIUM_KEYWORDS):
            return 'MEDIUM'
        else:
            return 'LOW'

    def analyze_item(self, item: ChecklistItem) -> AnalysisResult:
        """Analyze a single checklist item"""
        # Search for all patterns
        search_results: list[SearchResult] = []
        total_count = 0

        for pattern in item.patterns:
            result = self.search_pattern(pattern)
            # Apply smart filtering to reduce false positives
            filtered_result = self._filter_relevant_matches(item, result)
            if filtered_result.count > 0:
                search_results.append(filtered_result)
                total_count += filtered_result.count

        # Find matching migration guide
        migration_guide = self._find_matching_guide(item)

        # Determine categorization
        is_covered = self.is_covered_in_plan(item)
        category = self.determine_impact(item)
        update_size = self.categorize_by_count(total_count)

        return AnalysisResult(
            item=item,
            search_results=search_results,
            total_occurrences=total_count,
            is_in_plan=is_covered,
            category=category,
            update_size=update_size,
            migration_guide=migration_guide
        )

    def generate_report(self, results: list[AnalysisResult]) -> str:
        """Generate the gap analysis report"""
        report_lines: list[str] = []

        report_lines.append("# Bevy Migration Gap Analysis Report")
        report_lines.append("")
        report_lines.append(f"**Checklist**: {self.checklist_path.name}")
        report_lines.append(f"**Migration Plan**: {self.plan_path.name}")
        report_lines.append(f"**Codebase**: {self.codebase_path}")
        report_lines.append("")

        # Separate results into categories
        to_add = [r for r in results if r.total_occurrences > 0 and not r.is_in_plan]
        already_covered = [r for r in results if r.is_in_plan]
        not_applicable = [r for r in results if r.total_occurrences == 0]

        # Summary statistics
        report_lines.append("## Summary Statistics")
        report_lines.append(f"- Total checklist items: {len(results)}")
        report_lines.append(f"- Items found in codebase: {len([r for r in results if r.total_occurrences > 0])}")
        report_lines.append(f"- Already in migration plan: {len(already_covered)}")
        report_lines.append(f"- Need to add: {len(to_add)}")
        report_lines.append(f"- Not applicable: {len(not_applicable)}")
        report_lines.append("")

        # Items to ADD to migration plan
        report_lines.append("## Items to ADD to Migration Plan")
        report_lines.append("")
        report_lines.append("These items were found in your codebase but are not yet covered in the migration plan.")
        report_lines.append("")

        # Group by category
        critical = [r for r in to_add if r.category == 'CRITICAL']
        high = [r for r in to_add if r.category == 'HIGH']
        medium = [r for r in to_add if r.category == 'MEDIUM']
        low = [r for r in to_add if r.category == 'LOW']

        if critical:
            report_lines.append("## Critical Priority Changes (Must Add)")
            report_lines.append("")
            for result in critical:
                self._format_result(result, report_lines)

        if high:
            report_lines.append("## High Priority Changes (Should Add)")
            report_lines.append("")
            for result in high:
                self._format_result(result, report_lines)

        if medium:
            report_lines.append("## Medium Priority Changes")
            report_lines.append("")
            for result in medium:
                self._format_result(result, report_lines)

        if low:
            report_lines.append("## Low Priority Changes")
            report_lines.append("")
            for result in low:
                self._format_result(result, report_lines)

        # Items already covered
        if already_covered:
            report_lines.append("## Items Already Covered")
            report_lines.append("")
            for result in already_covered:
                report_lines.append(f"- [x] {result.item.description}")
                report_lines.append(f"  - Found in: {result.total_occurrences} location(s)")
                report_lines.append(f"  - Status: COVERED in migration plan")
            report_lines.append("")

        # Items not applicable
        if not_applicable:
            report_lines.append("## Items Not Applicable")
            report_lines.append("")
            for result in not_applicable:
                report_lines.append(f"- {result.item.description}")
                report_lines.append(f"  - Status: NOT FOUND in codebase")
            report_lines.append("")

        return '\n'.join(report_lines)

    def _format_result(self, result: AnalysisResult, lines: list[str]) -> None:
        """Format a single result for the report with actionable examples"""
        lines.append(f"### {result.item.section}: {result.item.description}")
        lines.append("")
        lines.append(f"**Occurrences:** {result.total_occurrences} locations across {len(set(f for sr in result.search_results for f in sr.files))} files")
        lines.append(f"**Priority:** {result.category}")
        lines.append(f"**Update Size:** {result.update_size}")
        lines.append("")

        # Show official migration guide if available
        if result.migration_guide:
            lines.append("#### Official Bevy Migration Guide")
            lines.append("")
            lines.append("**Before (Bevy 0.16):**")
            lines.append("```rust")
            lines.append(result.migration_guide.before_code)
            lines.append("```")
            lines.append("")
            lines.append("**After (Bevy 0.17):**")
            lines.append("```rust")
            lines.append(result.migration_guide.after_code)
            lines.append("```")
            lines.append("")

        # Show example from codebase
        if result.search_results and result.search_results[0].code_matches:
            best_match = result.search_results[0].code_matches[0]

            lines.append("#### Example from Your Codebase")
            lines.append("")
            lines.append(f"**File:** `{best_match.file_path}`")

            if best_match.function_signature:
                lines.append(f"**Context:** `{best_match.function_signature}`")

            lines.append("")
            lines.append("**Current Code:**")
            lines.append("```rust")
            if best_match.context_before:
                lines.append(best_match.context_before)
            lines.append(best_match.matching_line)
            if best_match.context_after:
                lines.append(best_match.context_after)
            lines.append("```")
            lines.append("")

        # Show pattern to find all occurrences
        if result.item.patterns:
            lines.append("#### Pattern to Find All Occurrences")
            lines.append("")
            main_pattern = result.item.patterns[0]
            lines.append(f"```bash")
            lines.append(f'rg "{main_pattern}" --type rust')
            lines.append("```")
            lines.append("")

        # List all affected locations
        lines.append("#### All Affected Locations")
        lines.append("")

        # Group by file
        file_matches: dict[str, list[CodeMatch]] = {}
        for sr in result.search_results:
            for match in sr.code_matches:
                if match.file_path not in file_matches:
                    file_matches[match.file_path] = []
                file_matches[match.file_path].append(match)

        for file_path in sorted(file_matches.keys()):
            matches = file_matches[file_path]
            lines.append(f"- `{file_path}` ({len(matches)} occurrence(s))")
            for match in matches[:3]:  # Show first 3 matches per file
                if match.function_signature:
                    lines.append(f"  - {match.function_signature}")
            if len(matches) > 3:
                lines.append(f"  - ... and {len(matches) - 3} more")

        lines.append("")
        lines.append("---")
        lines.append("")

    def run(self) -> str:
        """Run the complete analysis"""
        print(f"Parsing checklist: {self.checklist_path}", file=sys.stderr)
        items = self.parse_checklist()
        print(f"Found {len(items)} checklist items", file=sys.stderr)

        print(f"Analyzing codebase: {self.codebase_path}", file=sys.stderr)
        results: list[AnalysisResult] = []
        for i, item in enumerate(items, 1):
            print(f"Analyzing item {i}/{len(items)}: {item.section[:30]}...", file=sys.stderr)
            result = self.analyze_item(item)
            results.append(result)

        print("Generating report...", file=sys.stderr)
        report = self.generate_report(results)

        return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Analyze Bevy codebase against migration checklist'
    )
    _ = parser.add_argument(
        '--checklist',
        type=Path,
        required=True,
        help='Path to migration checklist markdown file'
    )
    _ = parser.add_argument(
        '--plan',
        type=Path,
        required=True,
        help='Path to current migration plan markdown file'
    )
    _ = parser.add_argument(
        '--codebase',
        type=Path,
        default=Path.cwd(),
        help='Path to codebase root (default: current directory)'
    )
    _ = parser.add_argument(
        '--guides',
        type=Path,
        help='Path to Bevy migration guides directory (optional)'
    )
    _ = parser.add_argument(
        '--output',
        type=Path,
        help='Output file path (default: stdout)'
    )

    args = parser.parse_args()

    # Extract typed arguments
    checklist_path = cast(Path, args.checklist)
    plan_path = cast(Path, args.plan)
    codebase_path = cast(Path, args.codebase)
    guides_path = cast(Path | None, args.guides)
    output_path = cast(Path | None, args.output)

    # Validate inputs
    if not checklist_path.exists():
        print(f"Error: Checklist file not found: {checklist_path}", file=sys.stderr)
        sys.exit(1)

    if not codebase_path.exists():
        print(f"Error: Codebase path not found: {codebase_path}", file=sys.stderr)
        sys.exit(1)

    # Run analysis
    analyzer = MigrationAnalyzer(codebase_path, checklist_path, plan_path, guides_path)
    report = analyzer.run()

    # Output report
    if output_path:
        _ = output_path.write_text(report, encoding='utf-8')
        print(f"Report written to: {output_path}", file=sys.stderr)
    else:
        print(report)


if __name__ == '__main__':
    main()
