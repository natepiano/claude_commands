#!/usr/bin/env python3
"""Turn banned-word hook enforcement on or off.

The three hooks stay registered in settings.json permanently. This flips the
switch they read, so enforcement changes without touching settings.json --
which carries a git clean filter and would need JSON surgery on every flip.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from banned_words_lib import HOOK_CONFIG, hooks_enabled

GOVERNED_HOOKS = (
    ("PostToolUse", "post-tool-use-banned-words.py", "messages the violation"),
    ("PostToolUse", "post-tool-use-banned-words-block.py", "blocks until it is fixed"),
    ("Stop", "stop-assistant-prose-banned-words.py", "scans the emitted turn"),
)
DEFAULT_CONFIG = """# Banned-word hook enforcement — on or off.
#
# Written by /banned_word_hook. Only a literal `off` disables enforcement.

[hooks]
enabled=on
"""


def write_setting(value: str) -> None:
    """Rewrite the `enabled` line, keeping every comment around it."""
    try:
        text = HOOK_CONFIG.read_text(encoding="utf-8")
    except OSError:
        text = DEFAULT_CONFIG

    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#") and stripped.partition("=")[0].strip() == "enabled":
            lines[index] = f"enabled={value}"
            break
    else:
        lines.append(f"enabled={value}")

    HOOK_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    _ = HOOK_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def report() -> None:
    """Print the current state and what it governs."""
    state = "on" if hooks_enabled() else "off"
    verb = "enforcing" if state == "on" else "passing through"
    print(f"Banned-word hooks: {state} ({verb})")
    print(f"  switch: {HOOK_CONFIG}")
    for event, script, role in GOVERNED_HOOKS:
        print(f"  {event:12} {script:36} {role}")


def main() -> int:
    """Main entry point."""
    arguments = sys.argv[1:]
    if not arguments or arguments[0] == "status":
        report()
        return 0

    action = arguments[0].lower()
    if action not in {"on", "off"}:
        print(f"usage: {Path(sys.argv[0]).name} on|off|status", file=sys.stderr)
        return 2

    was = "on" if hooks_enabled() else "off"
    if was == action:
        print(f"Banned-word hooks were already {action}.")
        report()
        return 0

    write_setting(action)
    print(f"Banned-word hooks: {was} -> {action}")
    print("Takes effect on the next hook invocation; no restart needed.")
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
