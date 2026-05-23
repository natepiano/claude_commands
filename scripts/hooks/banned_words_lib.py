"""Shared banned-words detection."""

import json
import os
import re
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, cast

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None  # type: ignore[assignment]

STYLE_GUIDE = Path.home() / "rust" / "nate_style" / "rust" / "forbidden-words.md"
COUNTER_STATE = Path.home() / ".claude" / "state" / "forbidden-word-counts.json"
COUNTER_LOCK = COUNTER_STATE.with_suffix(".lock")
# Per-line allowance marker. Must be preceded by a comment opener (`#`, `//`,
# or `<!--`) so casual prose mentions of the literal `allow-banned:` — e.g.
# documentation describing the mechanism, or a backticked reference in chat —
# do NOT silently disable the line. This is the only escape hatch; tightening
# it is intentional.
ALLOW_MARKER_RE = re.compile(r"(?:#|//|<!--)\s*allow-banned:", re.IGNORECASE)
INTROSPECTION_TOKENS = (
    "banned_words_lib",
    "forbidden-words.md",
    "forbidden-word-counts.json",
    "analyze_changes.sh",
    "load-rust-style.sh",
    "git diff",
    "git status",
    "git log",
    "git show",
)


def is_introspection_command(command: str) -> bool:
    """True if a Bash command's output is unsafe to scan for banned words.

    Two cases:
    1. Commands that print the banned-words machinery itself (importing the
       lib, catting the style guide) — output naturally contains every stem.
    2. Commands that print diff/status/log content — the output mirrors the
       repo's own files, which were already gated when authored. Re-scanning
       the diff would flag the same content twice and double-bump counters.
    """
    return any(tok in command for tok in INTROSPECTION_TOKENS)


# ── Exception manager: guide-reproduction detection ─────────────────────────
# `is_introspection_command` only covers Bash command strings. It cannot see
# the assistant's own prose (scanned by the Stop hook) or arbitrary content
# written to non-Bash tools. Those paths legitimately reproduce the whole
# banned-word list whenever a message is *about* the machinery — an analysis
# report, a mechanism explanation, or an echo of the loaded style guide. When
# that happens the naive scan bumps every counter at once (the "all 12 at the
# same timestamp" pattern). Two signals mark such reproduction so the hooks
# skip both the block and the counter-bump:
#
#   1. Density — natural prose does not trip a large fraction of the entire
#      list in a single message; a reproduction trips nearly all of it.
#   2. Self-reference — the text names the machinery itself (the counter-state
#      file, the guide, the library module, or the report header).
DENSITY_MIN_STEMS = 6
SELF_REFERENCE_MARKERS = (
    "Counter state:",
    "forbidden-word-counts.json",
    "forbidden-words.md",
    "banned_words_lib",
)


def is_guide_reproduction(text: str, distinct_stem_count: int) -> bool:
    """True when `text` reproduces the banned-word list/machinery rather than
    using the words in prose. See the DENSITY_MIN_STEMS / SELF_REFERENCE_MARKERS
    note above. Callers pass the distinct-stem count they already computed from
    `find_violations`, so this stays a pure decision with no re-scan.
    """
    if distinct_stem_count >= DENSITY_MIN_STEMS:
        return True
    return any(marker in text for marker in SELF_REFERENCE_MARKERS)


class Violation(NamedTuple):
    stem: str
    match: str
    line_no: int
    line: str


@dataclass(frozen=True)
class CounterRecord:
    count: int
    last_triggered_at: str | None


def _read_guide() -> str:
    try:
        return STYLE_GUIDE.read_text()
    except OSError:
        return ""


def _style_guide_counters() -> dict[str, int]:
    guide = _read_guide()
    if not guide:
        return {}
    out: dict[str, int] = {}
    pattern = re.compile(r'^###\s+"([^"]+)".*?\bcounter:\s*(\d+)', re.MULTILINE)
    matches = cast("list[tuple[str, str]]", pattern.findall(guide))
    for stem, raw_count in matches:
        out[stem] = int(raw_count)
    return out


def _initial_timestamp() -> str:
    return _trigger_timestamp()


def _trigger_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _coerce_int(value: object) -> int | None:
    """Best-effort int from untrusted JSON. `bool` is rejected (it would parse
    as 0/1 and silently corrupt a count); `int`/`str`/`float` are accepted."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _normalize_counter_records(raw: object) -> dict[str, CounterRecord]:
    if not isinstance(raw, dict):
        return {}
    raw_dict = cast("dict[str, object]", raw)

    words = raw_dict.get("words")
    source = cast("dict[str, object]", words) if isinstance(words, dict) else raw_dict

    out: dict[str, CounterRecord] = {}
    for key, value in source.items():
        if isinstance(value, dict):
            value_dict = cast("dict[str, object]", value)
            count_value = value_dict.get("count")
            timestamp_value: object = value_dict.get("last_triggered_at")
        else:
            count_value = value
            timestamp_value = _initial_timestamp()

        count = _coerce_int(count_value)
        if count is None or count < 0:
            continue
        timestamp = timestamp_value if isinstance(timestamp_value, str) else None
        out[key] = CounterRecord(count=count, last_triggered_at=timestamp)
    return out


def load_counter_records() -> dict[str, CounterRecord]:
    try:
        raw: object = json.loads(COUNTER_STATE.read_text())  # pyright: ignore[reportAny]
    except (OSError, json.JSONDecodeError):
        return {}
    return _normalize_counter_records(raw)


def load_counters() -> dict[str, int]:
    return {stem: record.count for stem, record in load_counter_records().items()}


@contextmanager
def _counter_file_lock():
    COUNTER_STATE.parent.mkdir(parents=True, exist_ok=True)
    with COUNTER_LOCK.open("a+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_counter_records(records: dict[str, CounterRecord]) -> None:
    COUNTER_STATE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "words": {
            stem: {
                "count": record.count,
                "last_triggered_at": record.last_triggered_at,
            }
            for stem, record in sorted(records.items())
        },
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp_path = COUNTER_STATE.with_name(f".{COUNTER_STATE.name}.{os.getpid()}.tmp")
    _ = tmp_path.write_text(text)
    _ = tmp_path.replace(COUNTER_STATE)


def format_counter_totals(records: dict[str, CounterRecord]) -> str:
    if not records:
        return "(none)"
    return ", ".join(
        f"{stem}={record.count} (last {record.last_triggered_at or 'never'})"
        for stem, record in records.items()
    )


def counter_analysis_rows() -> list[tuple[str, int, str]]:
    records = load_counter_records()
    rows: list[tuple[str, int, str]] = []
    for stem in load_banned_words():
        record = records.get(stem, CounterRecord(count=0, last_triggered_at=None))
        rows.append((stem, record.count, record.last_triggered_at or "never"))
    return rows


def load_banned_words() -> list[str]:
    return re.findall(r'^###\s+"([^"]+)"', _read_guide(), re.MULTILINE)


def load_exemptions() -> list[str]:
    m = re.search(r"^exceptions:\s*(.+)$", _read_guide(), re.MULTILINE)
    if not m:
        return []
    raw = m.group(1).strip()
    parts = re.split(r"[,;]", raw) if ("," in raw or ";" in raw) else [raw]
    return [p.strip() for p in parts if p.strip()]


def load_per_stem_exemptions() -> dict[str, list[str]]:
    """Parse per-section `except:` lines from the canonical guide.

    Each `### "<stem>"` section may declare its own exemptions on a line
    starting with `except:` followed by a comma-separated list of substrings.
    Matches that fall inside any of those substrings are skipped for that
    stem only — useful when a word is banned as a metaphor but legitimate
    as domain vocabulary.
    """
    guide = _read_guide()
    if not guide:
        return {}
    out: dict[str, list[str]] = {}
    section_pat = re.compile(
        r'^###\s+"([^"]+)".*?\n(.*?)(?=^###\s+"|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    except_pat = re.compile(r"^except(?:ions?)?:\s*(.+?)\s*$", re.MULTILINE)
    for m in section_pat.finditer(guide):
        stem = m.group(1)
        body = m.group(2)
        em = except_pat.search(body)
        if not em:
            continue
        raw = em.group(1).strip()
        parts = re.split(r"[,;]", raw) if ("," in raw or ";" in raw) else [raw]
        items = [p.strip() for p in parts if p.strip()]
        if items:
            out[stem] = items
    return out


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


def _is_phrase(stem: str) -> bool:
    return bool(re.search(r"\s", stem))


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    # Literal case-insensitive match. Interior whitespace becomes \s+ so extra
    # spacing or line wraps don't dodge the match. Word boundaries are added
    # only on edges that are word characters; punctuation edges anchor themselves.
    parts = re.split(r"\s+", phrase.strip())
    body = r"\s+".join(re.escape(p) for p in parts)
    left = r"\b" if phrase[:1].isalnum() or phrase[:1] == "_" else ""
    right = r"\b" if phrase[-1:].isalnum() or phrase[-1:] == "_" else ""
    return re.compile(rf"{left}{body}{right}", re.IGNORECASE)


def _stem_pattern(stem: str, override: str | None = None) -> re.Pattern[str]:
    # If the canonical guide supplied an explicit regex for this stem, use it.
    # Otherwise, strip a trailing silent 'e' so verb forms collapse to a single
    # root pattern (the stem itself works for stems that don't end in 'e').
    if override:
        return re.compile(override, re.IGNORECASE)
    root = stem[:-1] if stem.endswith("e") and len(stem) > 2 else stem
    return re.compile(rf"\b\w*{re.escape(root)}\w*\b", re.IGNORECASE)


def bump_counters(stems: Iterable[str]) -> dict[str, CounterRecord]:
    """Increment local hit counters and return new totals for bumped stems."""
    unique = list(dict.fromkeys(stems))
    if not unique:
        return {}
    with _counter_file_lock():
        records = load_counter_records()
        legacy = _style_guide_counters()
        for stem, count in legacy.items():
            _ = records.setdefault(
                stem,
                CounterRecord(count=count, last_triggered_at=_initial_timestamp()),
            )

        bumped: dict[str, CounterRecord] = {}
        timestamp = _trigger_timestamp()
        for stem in unique:
            current = records.get(stem, CounterRecord(count=0, last_triggered_at=None))
            updated = CounterRecord(
                count=current.count + 1,
                last_triggered_at=timestamp,
            )
            records[stem] = updated
            bumped[stem] = updated

        try:
            _write_counter_records(records)
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
    per_stem_exemptions = load_per_stem_exemptions()
    overrides = load_overrides()
    patterns = [
        (s, _phrase_pattern(s) if _is_phrase(s) else _stem_pattern(s, overrides.get(s)))
        for s in stems
    ]

    out: list[Violation] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER_RE.search(line):
            continue
        global_exempt_spans: list[tuple[int, int]] = []
        for ex in exemptions:
            for em in re.finditer(re.escape(ex), line, re.IGNORECASE):
                global_exempt_spans.append((em.start(), em.end()))

        per_stem_spans: dict[str, list[tuple[int, int]]] = {}
        for stem, ex_list in per_stem_exemptions.items():
            spans: list[tuple[int, int]] = []
            for ex in ex_list:
                for em in re.finditer(re.escape(ex), line, re.IGNORECASE):
                    spans.append((em.start(), em.end()))
            if spans:
                per_stem_spans[stem] = spans

        for stem, pat in patterns:
            stem_spans = per_stem_spans.get(stem, [])
            for m in pat.finditer(line):
                if any(m.start() >= s and m.end() <= e for s, e in global_exempt_spans):
                    continue
                if any(m.start() >= s and m.end() <= e for s, e in stem_spans):
                    continue
                out.append(Violation(stem, m.group(0), line_no, line.strip()))
    return out


def _main() -> int:
    """CLI entry point for ad-hoc scans (e.g. /clippy style review, nightly).

    Usage:
        python3 banned_words_lib.py --analysis     # print local counter state
        python3 banned_words_lib.py [path ...]      # scan each file
        python3 banned_words_lib.py                 # scan stdin

    Output: one line per violation as `path:lineno: stem: <line>`.
    Exit 1 if any violations found, 0 if clean. Errors go to stderr (exit 2).

    Safe to call from inside Claude Code: the script path contains
    `banned_words_lib`, so `is_introspection_command()` exempts the command
    from the PostToolUse hook — output is not re-scanned and counters are
    not bumped.
    """
    import sys

    paths: list[str] = sys.argv[1:]
    if paths == ["--analysis"] or paths == ["--counters"]:
        rows = counter_analysis_rows()
        width = max([len("word"), *(len(stem) for stem, _, _ in rows)])
        print(f"Counter state: {COUNTER_STATE}")
        print(f"{'word'.ljust(width)}  count  last_triggered_at")
        print(f"{'-' * width}  -----  -----------------")
        for stem, count, timestamp in rows:
            print(f"{stem.ljust(width)}  {str(count).rjust(5)}  {timestamp}")
        return 0

    sources: list[tuple[str, str]] = []
    if paths:
        for p in paths:
            try:
                sources.append((p, Path(p).read_text()))
            except OSError as exc:
                print(f"{p}: error: {exc}", file=sys.stderr)
                return 2
    else:
        sources.append(("<stdin>", sys.stdin.read()))

    found = 0
    for label, text in sources:
        for v in find_violations(text):
            print(f"{label}:{v.line_no}: {v.stem}: {v.line}")
            found += 1
    return 1 if found else 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
