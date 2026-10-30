"""Keep herdr's Claude detection rules correct for a permission dialog.

herdr's downloaded Claude rules read a pane as idle (live_prompt_box, priority 950) when the text
between the last two `─` rules holds any line that starts with `❯`. While a permission dialog is
open, the last rule is the dialog's top border, so one stray rule higher on the screen makes the
transcript above the dialog look like the input box, and idle outranks the dialog's blocked rule
(priority 850). Seen 2026-10-21: cargo-liner-style-types sat at a Bash permission dialog for
5.5 hours while herdr showed it idle.

The fix is a local override of the rules with two edits: live_prompt_box matches only a box that
opens with its `❯` line, and a new rule, live_permission_prompt, reads a dialog at the bottom of
the screen as blocked above the idle rule. herdr uses an override in place of the downloaded rules
as a whole, so the override is rebuilt from the downloaded rules on each snapshot pass, and a rules
update from upstream gets the two edits again within one pass.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import cast

AGENT = "claude"
PROMPT_BOX_RULE = "live_prompt_box"
PROMPT_BOX_MATCH = "line_regex = ['^\\s*❯']"
PROMPT_BOX_FIX = """# The input box opens with its prompt line. A body that opens with anything else is
# transcript between a stray rule and a dialog's top border, not the box.
regex = ['\\A(?:[ \\t]*\\n)*[ \\t]*❯']"""
PERMISSION_RULE = "live_permission_prompt"
PERMISSION_FIX = """
[[rules]]
id = "live_permission_prompt"
state = "blocked"
priority = 980
region = "after_last_horizontal_rule"
visible_blocker = true
# A permission dialog at the bottom of the screen. Its footer has no "enter to" hint, so
# live_blocked_form misses it, and it must outrank live_prompt_box.
contains = ["do you want to proceed?", "esc to cancel"]
all = [
  { any = [
    { line_regex = ['(?i)^\\s*❯?\\s*1\\.\\s*yes\\b'] },
    { line_regex = ['(?i)^\\s*❯?\\s*2\\.\\s*yes\\b'] },
    { line_regex = ['(?i)^\\s*❯?\\s*2\\.\\s*no\\b'] },
    { line_regex = ['(?i)^\\s*❯?\\s*3\\.\\s*no\\b'] },
  ] },
]
"""


class RulesChanged(Exception):
    """The downloaded rules no longer have the shape the two edits expect."""


def remote_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state) / "herdr" / "agent-detection" / "remote" / f"{AGENT}.toml"


def override_path() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config) / "herdr" / "agent-detection" / f"{AGENT}.toml"


def _rules(text: str) -> list[dict[str, object]]:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise RulesChanged(f"the rules do not parse: {error}") from error
    rules = parsed.get("rules")
    if not isinstance(rules, list):
        raise RulesChanged("the rules have no [[rules]] list")
    return cast(list[dict[str, object]], rules)


def built_rules(remote: str) -> str:
    """The override text: the downloaded rules with the two edits, under its own version."""
    rules = {rule.get("id"): rule for rule in _rules(remote)}
    if PERMISSION_RULE in rules:
        raise RulesChanged(f"the downloaded rules already hold {PERMISSION_RULE}")
    box = rules.get(PROMPT_BOX_RULE)
    if box is None or box.get("line_regex") != ["^\\s*❯"] or "regex" in box:
        raise RulesChanged(f"{PROMPT_BOX_RULE} changed its prompt-line match")
    blocks = remote.split("[[rules]]")
    marked = [index for index, block in enumerate(blocks) if f'id = "{PROMPT_BOX_RULE}"' in block]
    if len(marked) != 1 or blocks[marked[0]].count(PROMPT_BOX_MATCH) != 1:
        raise RulesChanged(f"{PROMPT_BOX_RULE} is not one block with one prompt-line match")
    blocks[marked[0]] = blocks[marked[0]].replace(PROMPT_BOX_MATCH, PROMPT_BOX_FIX)
    lines = "[[rules]]".join(blocks).rstrip("\n").split("\n")
    versions = [index for index, line in enumerate(lines) if line.startswith("version = ")]
    if len(versions) != 1:
        raise RulesChanged("the rules do not hold one version line")
    # herdr takes only a dotted numeric version; the added segment marks the local build.
    lines[versions[0]] = lines[versions[0]].removesuffix('"') + '.1"'
    header = f"# Built from {remote_path()} by ~/.claude/scripts/herdr/detection_rules.py; do not edit."
    built = "\n".join([header, *lines]) + "\n" + PERMISSION_FIX
    ids = [rule.get("id") for rule in _rules(built)]
    if ids.count(PERMISSION_RULE) != 1:
        raise RulesChanged(f"the built rules do not hold {PERMISSION_RULE} once")
    return built


def refresh(running: set[str], reload: Callable[[str], bool]) -> bool:
    """Build the override and reload sessions that have not confirmed its current version.

    Return True when the override was written. The callback returns True only
    for a confirmed reload. Failed sessions retry on the next pass; an
    interruption before the receipt is written retries the whole running set.

    With no downloaded rules there is nothing to build on. A rules change that the edits no longer
    fit keeps the old override, which still holds the fix, and names the change on stderr.
    """
    try:
        remote = remote_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    try:
        built = built_rules(remote)
    except RulesChanged as error:
        print(f"herdr detection rules: kept the old override: {error}", file=sys.stderr)
        return False
    target = override_path()
    written = True
    try:
        if target.read_text(encoding="utf-8") == built:
            written = False
    except FileNotFoundError:
        pass
    # A matching override alone does not prove that every server loaded it.
    # Keep successful reloads for this version so adding one session does not
    # reset detection in sessions already using it.
    receipt = target.with_suffix(".loaded")
    version = sha256(built.encode("utf-8")).hexdigest()
    try:
        loaded = receipt.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        loaded = []
    confirmed = set(loaded[1:]) if not written and loaded[:1] == [version] else set[str]()
    pending = running - confirmed
    if not written and not pending:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # Invalidate a prior receipt before replacing the file or reloading. An
    # interrupted batch must not reuse an older confirmation of the same content.
    receipt.unlink(missing_ok=True)
    if written:
        staged = target.with_name(f".{target.name}.{os.getpid()}")
        _ = staged.write_text(built, encoding="utf-8")
        _ = staged.replace(target)
    for session in sorted(pending):
        if reload(session):
            confirmed.add(session)
    staged_receipt = receipt.with_name(f".{receipt.name}.{os.getpid()}")
    _ = staged_receipt.write_text("\n".join([version, *sorted(confirmed)]) + "\n", encoding="utf-8")
    _ = staged_receipt.replace(receipt)
    return written
