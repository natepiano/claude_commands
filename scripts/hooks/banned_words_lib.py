"""Shared banned-words detection.

Source of truth: ~/rust/nate_style/rust/forbidden-words.md
Banned words come from `### "<word>"` headings.
Exemption phrases come from the `exceptions:` frontmatter field.
Per-line override: any line containing `allow-banned:` is skipped.
"""

import re
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

STYLE_GUIDE = Path.home() / "rust" / "nate_style" / "rust" / "forbidden-words.md"
ALLOW_MARKER = "allow-banned:"


class Violation(NamedTuple):
    stem: str
    match: str
    line_no: int
    line: str


def _read_guide() -> str:
    try:
        return STYLE_GUIDE.read_text()
    except OSError:
        return ""


def load_banned_words() -> list[str]:
    return re.findall(r'^###\s+"([^"]+)"', _read_guide(), re.MULTILINE)


def load_exemptions() -> list[str]:
    m = re.search(r"^exceptions:\s*(.+)$", _read_guide(), re.MULTILINE)
    if not m:
        return []
    raw = m.group(1).strip()
    parts = re.split(r"[,;]", raw) if ("," in raw or ";" in raw) else [raw]
    return [p.strip() for p in parts if p.strip()]


def load_overrides() -> dict[str, str]:
    """Parse optional `regex: <pattern>` lines from the canonical guide.

    Each banned-word section may include a single `regex:` line under its
    `### "<stem>"` heading. When present, that pattern replaces the default
    silent-e algorithm for that stem (used when the default root would be
    too short and collide with unrelated common words).
    """
    guide = _read_guide()
    if not guide:
        return {}
    out: dict[str, str] = {}
    section_pat = re.compile(
        r'^###\s+"([^"]+)".*?\n(.*?)(?=^###\s+"|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    regex_pat = re.compile(r"^regex:\s*(.+?)\s*$", re.MULTILINE)
    for m in section_pat.finditer(guide):
        stem = m.group(1)
        body = m.group(2)
        rm = regex_pat.search(body)
        if rm:
            out[stem] = rm.group(1)
    return out


def _stem_pattern(stem: str, override: str | None = None) -> re.Pattern[str]:
    # If the canonical guide supplied an explicit regex for this stem, use it.
    # Otherwise, strip a trailing silent 'e' so verb forms collapse to a single
    # root pattern (the stem itself works for stems that don't end in 'e').
    if override:
        return re.compile(override, re.IGNORECASE)
    root = stem[:-1] if stem.endswith("e") and len(stem) > 2 else stem
    return re.compile(rf"\b\w*{re.escape(root)}\w*\b", re.IGNORECASE)


def bump_counters(stems: Iterable[str]) -> dict[str, int]:
    """Increment the `counter: N` value in each `### "<stem>" — counter: N` heading.

    Returns a mapping of stem → new counter value for the stems that were bumped.
    Stems with no counter heading are skipped silently.
    """
    unique = list(dict.fromkeys(stems))
    if not unique:
        return {}
    try:
        guide = STYLE_GUIDE.read_text()
    except OSError:
        return {}

    bumped: dict[str, int] = {}
    new_text = guide
    for stem in unique:
        pattern = re.compile(
            rf'(^###\s+"{re.escape(stem)}"\s+\S+\s+counter:\s*)(\d+)',
            re.MULTILINE,
        )

        def _inc(m: re.Match[str], stem: str = stem) -> str:
            n = int(m.group(2)) + 1
            bumped[stem] = n
            return f"{m.group(1)}{n}"

        new_text = pattern.sub(_inc, new_text, count=1)

    if bumped:
        try:
            _ = STYLE_GUIDE.write_text(new_text)
        except OSError:
            return {}
    return bumped


def get_stem_guidance(stem: str) -> str:
    """Return the per-stem body from the style guide (between this heading and the next ###).

    Used by the messaging hook so the agent gets the substitutes and rule prose
    inline without needing to open the style guide on every violation.
    """
    guide = _read_guide()
    if not guide:
        return ""
    pattern = re.compile(
        rf'^###\s+"{re.escape(stem)}".*?\n(.*?)(?=^###\s+"|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(guide)
    if not m:
        return ""
    return m.group(1).strip()


def find_violations(text: str) -> list[Violation]:
    stems = load_banned_words()
    exemptions = load_exemptions()
    overrides = load_overrides()
    patterns = [(s, _stem_pattern(s, overrides.get(s))) for s in stems]

    out: list[Violation] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        exempt_spans: list[tuple[int, int]] = []
        for ex in exemptions:
            for em in re.finditer(re.escape(ex), line, re.IGNORECASE):
                exempt_spans.append((em.start(), em.end()))

        for stem, pat in patterns:
            for m in pat.finditer(line):
                if any(m.start() >= s and m.end() <= e for s, e in exempt_spans):
                    continue
                out.append(Violation(stem, m.group(0), line_no, line.strip()))
    return out
