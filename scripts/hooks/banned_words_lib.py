"""Shared banned-words detection.

Source of truth: ~/rust/nate_style/rust/forbidden-words.md
Banned words come from `### "<word>"` headings.
Exemption phrases come from the `exceptions:` frontmatter field.
Per-line override: any line containing `allow-banned:` is skipped.
"""

import re
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


def _stem_pattern(stem: str) -> re.Pattern[str]:
    # Strip trailing silent 'e' so verb forms collapse: shape→shap matches shape/shaping/reshape;
    # carve→carv matches carve/carving/carved. Stems not ending in 'e' (honest/gloss) are unchanged.
    root = stem[:-1] if stem.endswith("e") and len(stem) > 2 else stem
    return re.compile(rf"\b\w*{re.escape(root)}\w*\b", re.IGNORECASE)


def find_violations(text: str) -> list[Violation]:
    stems = load_banned_words()
    exemptions = load_exemptions()
    patterns = [(s, _stem_pattern(s)) for s in stems]

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
