#!/usr/bin/env python3
"""
Bevy Migration Semantic Review Orchestrator - Pass 2

Divides migration guides into 10 batches and generates prompts for parallel
agent review to enhance actionability.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import cast


JsonDict = dict[str, str | int | list[str] | list[int] | list[dict[str, str]]]


def divide_guides(guides: list[JsonDict], num_agents: int = 10) -> list[list[JsonDict]]:
    """Divide guides into balanced batches for agents"""
    total = len(guides)
    base_size = total // num_agents
    remainder = total % num_agents

    batches: list[list[JsonDict]] = []
    start = 0

    for i in range(num_agents):
        # First 'remainder' agents get an extra guide
        size = base_size + (1 if i < remainder else 0)
        end = start + size
        batches.append(guides[start:end])
        start = end

    return batches


def generate_agent_prompt(
    batch: list[JsonDict],
    output_file: Path,
    version: str
) -> str:
    """Generate prompt for a single agent"""

    prompt = f"""You are performing a semantic review of Bevy {version} migration guides to make them more actionable.

**Your Task:**
Review {len(batch)} migration guides and enhance them for maximum actionability.

**Enhancement Criteria:**
1. **Break down complex changes** into specific, granular checklist items
2. **Extract concrete search patterns** (types, methods, fields, attributes, derive macros)
3. **Identify required vs optional changes** ("must" vs "may need to")
4. **Clarify ambiguous descriptions** with concrete actions
5. **Preserve all code examples** with labels
6. **Add helpful context** from prose descriptions

**Output Format:**
Write enhanced guide sections to: `{output_file}`

Each section should follow this format:

```markdown
## {{Title}}

**Pull Requests:** {{PRs}}

**Description:**
{{Enhanced description with clear actionable explanation}}

**Checklist:**
- [ ] Specific action 1 (mention exact types/methods)
- [ ] Specific action 2 (note if optional/required)
- [ ] Specific action 3 (provide clear guidance)

**Search Patterns:** `pattern1`, `pattern2`, `pattern3`

**Examples:**
{{Preserve code blocks with Old/New labels}}

---
```

**Guidelines:**
- Be specific: Instead of "Update API calls", say "Replace `Trigger<T>` with `On<T>` in observer closures"
- Include search guidance: "Search for `Trigger<` and `trigger.target()`"
- Note optionality: "May need to add `entity: Entity` field if using EntityEvent"
- Keep code examples: Show before/after comparisons
- Preserve all information from original guide

**Your Migration Guides:**

```json
{json.dumps(batch, indent=2)}
```

Now write the enhanced sections to `{output_file}` in the exact format shown above.
"""

    return prompt


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Orchestrate semantic review of Bevy migration guides - Pass 2'
    )
    _ = parser.add_argument(
        '--json-input',
        type=Path,
        required=True,
        help='Input JSON file from Pass 1'
    )
    _ = parser.add_argument(
        '--output-dir',
        type=Path,
        required=True,
        help='Output directory for agent findings'
    )
    _ = parser.add_argument(
        '--num-agents',
        type=int,
        default=10,
        help='Number of parallel agents (default: 10)'
    )

    args = parser.parse_args()

    json_input = cast(Path, args.json_input)
    output_dir = cast(Path, args.output_dir)
    num_agents = cast(int, args.num_agents)

    # Load Pass 1 data
    if not json_input.exists():
        print(f"Error: Input JSON not found: {json_input}", file=sys.stderr)
        sys.exit(1)

    json_content = json_input.read_text(encoding='utf-8')
    data = cast(dict[str, str | int | list[JsonDict]], json.loads(json_content))
    version = cast(str, data['version'])
    guides = cast(list[JsonDict], data['guides'])

    print(f"Loaded {len(guides)} migration guides for Bevy {version}", file=sys.stderr)

    # Divide into batches
    batches = divide_guides(guides, num_agents)

    print(f"Divided into {num_agents} batches:", file=sys.stderr)
    for i, batch in enumerate(batches, 1):
        print(f"  Agent {i:2d}: {len(batch):2d} guides (indices {batch[0]['index']}-{batch[-1]['index']})", file=sys.stderr)

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate agent prompts and write to files for review
    prompt_dir = output_dir / 'prompts'
    prompt_dir.mkdir(exist_ok=True)

    for agent_id, batch in enumerate(batches, 1):
        output_file = output_dir / f"agent-{agent_id:02d}-findings.md"
        prompt = generate_agent_prompt(batch, output_file, version)

        # Save prompt for reference
        prompt_file = prompt_dir / f"agent-{agent_id:02d}-prompt.txt"
        _ = prompt_file.write_text(prompt, encoding='utf-8')

    print(f"\n✓ Generated {num_agents} agent prompts in {prompt_dir}", file=sys.stderr)
    print(f"✓ Output files will be: {output_dir}/agent-01-findings.md ... agent-{num_agents:02d}-findings.md", file=sys.stderr)
    print(f"\nNext: Launch agents in parallel using Task tool", file=sys.stderr)


if __name__ == '__main__':
    main()
