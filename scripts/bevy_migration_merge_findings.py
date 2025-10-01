#!/usr/bin/env python3
"""
Bevy Migration Merge Findings - Pass 3

Sequentially merges agent findings into final enhanced checklist.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import cast


def parse_agent_findings(file_path: Path) -> list[str]:
    """Parse agent findings file into guide sections"""
    if not file_path.exists():
        print(f"Warning: Agent file not found: {file_path}", file=sys.stderr)
        return []

    content = file_path.read_text(encoding='utf-8')

    # Split on ## headers (guide sections)
    # Keep the ## in each section
    sections = re.split(r'(^##\s+.+$)', content, flags=re.MULTILINE)

    # Rejoin headers with their content
    guides: list[str] = []
    for i in range(1, len(sections), 2):
        if i + 1 < len(sections):
            section = sections[i] + sections[i + 1]
            guides.append(section.strip())

    return guides


def merge_all_findings(
    agent_findings_dir: Path,
    num_agents: int,
    version: str
) -> str:
    """Merge all agent findings into final checklist"""

    lines: list[str] = []

    # Header
    lines.append(f"# Bevy {version} Migration Checklist")
    lines.append("")
    lines.append("Generated from official Bevy migration guides with semantic enhancement.")
    lines.append("")
    lines.append("**Processing:**")
    lines.append("- Pass 1: Python extraction of structure and content")
    lines.append(f"- Pass 2: {num_agents} parallel agents for semantic review")
    lines.append("- Pass 3: Sequential merge into final checklist")
    lines.append("")

    # Collect all guides from all agents
    all_guides: list[str] = []
    total_guides = 0

    for agent_id in range(1, num_agents + 1):
        agent_file = agent_findings_dir / f"agent-{agent_id:02d}-findings.md"
        guides = parse_agent_findings(agent_file)

        if guides:
            print(f"✓ Agent {agent_id:2d}: {len(guides)} guides", file=sys.stderr)
            all_guides.extend(guides)
            total_guides += len(guides)
        else:
            print(f"⚠ Agent {agent_id:2d}: No guides found", file=sys.stderr)

    lines.append(f"**Statistics:**")
    lines.append(f"- Total migration items: {total_guides}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Add all guide sections
    for guide in all_guides:
        lines.append(guide)
        lines.append("")
        lines.append("---")
        lines.append("")

    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Merge agent findings into final checklist - Pass 3'
    )
    _ = parser.add_argument(
        '--agent-findings-dir',
        type=Path,
        required=True,
        help='Directory containing agent findings'
    )
    _ = parser.add_argument(
        '--output',
        type=Path,
        required=True,
        help='Output final checklist file'
    )
    _ = parser.add_argument(
        '--version',
        type=str,
        required=True,
        help='Bevy version (e.g., 0.17.0)'
    )
    _ = parser.add_argument(
        '--num-agents',
        type=int,
        default=10,
        help='Number of agents (default: 10)'
    )

    args = parser.parse_args()

    agent_findings_dir = cast(Path, args.agent_findings_dir)
    output_path = cast(Path, args.output)
    version = cast(str, args.version)
    num_agents = cast(int, args.num_agents)

    if not agent_findings_dir.exists():
        print(f"Error: Agent findings directory not found: {agent_findings_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Merging findings from {num_agents} agents...", file=sys.stderr)

    # Merge all findings
    final_checklist = merge_all_findings(agent_findings_dir, num_agents, version)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _ = output_path.write_text(final_checklist, encoding='utf-8')

    print(f"\n✓ Final checklist generated: {output_path}", file=sys.stderr)
    print(f"✓ Ready for use in Bevy {version} migration", file=sys.stderr)


if __name__ == '__main__':
    main()
