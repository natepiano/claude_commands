"""Mark generated prose with its source."""

SCRIPT_ENVELOPE_START = '<system-reminder source="script">'
SCRIPT_ENVELOPE_END = "</system-reminder>"


def script_prompt(text: str) -> str:
    """Mark generated prose without changing slash commands or an existing envelope."""
    if not text.strip() or text.lstrip().startswith(("/", SCRIPT_ENVELOPE_START)):
        return text
    separator = "\n" if "\n" in text else " "
    return f"{SCRIPT_ENVELOPE_START}{separator}{text}{separator}{SCRIPT_ENVELOPE_END}"
