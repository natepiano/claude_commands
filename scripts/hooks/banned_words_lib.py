"""Shared banned-words detection."""

import json
import os
import re
import shutil
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
# Snapshot of the counter file as it was *before* the most recent write. Each
# write copies the live file here first, so `revert_to_backup()` undoes exactly
# the last bump — used when a hit is deemed not to count (e.g. quoting a stem).
COUNTER_BACKUP = COUNTER_STATE.with_name(COUNTER_STATE.name + ".bak")
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


# Tools that only read — they never author content this turn. The PostToolUse
# hook has no matcher, so it runs on every tool and scans `tool_response.output`;
# for these that output is just file/search content the agent is inspecting, so
# scanning it only produces false positives (a Read of a file that legitimately
# uses a banned term, a Grep whose pattern is the term itself).
READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "NotebookRead", "LS"})


def is_read_only_tool(tool_name: str) -> bool:
    return tool_name in READ_ONLY_TOOLS


# Read-only shell programs: the command string is a search pattern or path and
# the output mirrors existing files — neither is content the agent authored this
# turn. Matched as whole words so `cat` does not fire inside `duplicate`/`truncate`.
READ_ONLY_PROGRAMS = (
    "grep", "rg", "ag", "cat", "bat", "head", "tail", "less", "more",
    "ls", "find", "fd", "wc", "tree", "diff", "stat", "file",
)
_READ_ONLY_PROG_RE = re.compile(r"\b(" + "|".join(READ_ONLY_PROGRAMS) + r")\b")
# A redirection to a real file (not /dev/null, not an fd dup like `2>&1` /
# `2>/dev/null`) means the command writes — disqualifies the read-only fast path.
_WRITE_REDIRECT_RE = re.compile(r"(?<![0-9&])>>?\s*(?!/dev/null\b)(?!&)\S")
# Programs that mutate even without a `>` redirection.
_WRITE_PROGRAM_RE = re.compile(r"\b(tee|dd)\b|\bsed\s+-i|\bgit\s+(commit|add|tag)\b")


def is_read_only_command(command: str) -> bool:
    """True when a Bash command only reads/searches and writes nothing.

    Used to skip the banned-word scan for inspection commands run through Bash
    (a `grep`/`rg`/`cat` over the repo). A command qualifies only if it invokes a
    read-only program AND carries no file-writing redirection or mutating program,
    so `echo ... > f`, `git commit -m`, and `sed -i` are still scanned.
    """
    if not command:
        return False
    if _WRITE_REDIRECT_RE.search(command) or _WRITE_PROGRAM_RE.search(command):
        return False
    return bool(_READ_ONLY_PROG_RE.search(command))


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
    # Back up the current on-disk state before overwriting it, so a bump the
    # user later decides should not count can be undone with `--revert`. The
    # backup stays exactly one write behind the live file. A failed copy must
    # not block the counter write, so a missing backup is tolerated.
    if COUNTER_STATE.exists():
        try:
            _ = shutil.copy2(COUNTER_STATE, COUNTER_BACKUP)
        except OSError:
            pass
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


def revert_to_backup() -> bool:
    """Restore the counter file from the last backup (the snapshot taken before
    the most recent write). Returns True if a backup existed and was restored,
    False if there was nothing to restore. Takes the counter lock so it cannot
    race a concurrent bump. The backup is left in place, so a second revert is a
    no-op rather than restoring a staler state.
    """
    with _counter_file_lock():
        if not COUNTER_BACKUP.exists():
            return False
        _ = shutil.copy2(COUNTER_BACKUP, COUNTER_STATE)
        return True


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
    # Literal case-insensitive match. Interior whitespace becomes [\s-]+ so
    # spacing and hyphenation variants don't dodge the match. Word boundaries
    # are added only on edges that are word characters; punctuation edges
    # anchor themselves.
    parts = re.split(r"\s+", phrase.strip())
    body = r"[\s-]+".join(re.escape(p) for p in parts)
    left = r"\b" if phrase[:1].isalnum() or phrase[:1] == "_" else ""
    right = r"\b" if phrase[-1:].isalnum() or phrase[-1:] == "_" else ""
    return re.compile(rf"{left}{body}{right}", re.IGNORECASE)


def _stem_pattern(stem: str, override: str | None = None) -> re.Pattern[str]:
    # If the canonical guide supplied an explicit regex for this entry, use it.
    # Otherwise, strip a trailing silent 'e' so verb forms collapse to a single
    # root pattern (the stem itself works for stems that don't end in 'e').
    if override:
        return re.compile(override, re.IGNORECASE)
    root = stem[:-1] if stem.endswith("e") and len(stem) > 2 else stem
    return re.compile(rf"\b\w*{re.escape(root)}\w*\b", re.IGNORECASE)


def _entry_pattern(stem: str, override: str | None = None) -> re.Pattern[str]:
    if override:
        return _stem_pattern(stem, override)
    if _is_phrase(stem):
        return _phrase_pattern(stem)
    return _stem_pattern(stem)


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
    patterns = [(s, _entry_pattern(s, overrides.get(s))) for s in stems]

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


def _scan_diff(diff_text: str) -> int:
    """Scan a unified diff on stdin, reporting only ADDED lines at their real
    file path and new-file line number. Exit 1 if any violation, else 0.

    A new (untracked) file rendered via `git diff --no-index /dev/null <file>`
    appears as an all-additions hunk, so untracked files are scanned in full;
    tracked files are scanned on their added lines only. Per-stem `except:`
    exemptions and `allow-banned:` markers apply per line, same as a file scan.
    """
    found = 0
    path: str | None = None
    new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            # `--- /dev/null` / `+++ /dev/null` marks the absent side; strip the
            # a/ or b/ prefix git prepends to real paths.
            if target == "/dev/null":
                path = None
            elif target[:2] in ("a/", "b/"):
                path = target[2:]
            else:
                path = target
            continue
        if raw.startswith("--- ") or raw.startswith("diff ") or raw.startswith("index "):
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_line = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+"):
            for v in find_violations(raw[1:]):
                label = path or "<unknown>"
                print(f"{label}:{new_line}: {v.stem}: {v.line}")
                found += 1
            new_line += 1
        elif raw.startswith("-"):
            # Removed line: present only on the old side, so it does not advance
            # the new-file counter and is never scanned.
            continue
        else:
            # Context line (leading space) or inter-file blank: advances the
            # new-file counter without being scanned.
            new_line += 1
    return 1 if found else 0


def _main() -> int:
    """CLI entry point for ad-hoc scans (e.g. /clippy style review, clean-fix).

    Usage:
        python3 banned_words_lib.py --analysis     # print local counter state
        python3 banned_words_lib.py --diff          # scan a unified diff on stdin
        python3 banned_words_lib.py [path ...]      # scan each file
        python3 banned_words_lib.py                 # scan stdin

    Output: one line per violation as `path:lineno: stem: <line>`. In `--diff`
    mode `path:lineno` is the real source location of the added line.
    Exit 1 if any violations found, 0 if clean. Errors go to stderr (exit 2).

    Safe to call from inside Claude Code: the script path contains
    `banned_words_lib`, so `is_introspection_command()` exempts the command
    from the PostToolUse hook — output is not re-scanned and counters are
    not bumped.
    """
    import sys

    paths: list[str] = sys.argv[1:]
    if paths and paths[0] in ("--revert", "--restore", "--undo"):
        if revert_to_backup():
            print(f"Reverted {COUNTER_STATE} from backup {COUNTER_BACKUP}")
            print(f"Restored totals: {format_counter_totals(load_counter_records())}")
            return 0
        print(f"No backup found at {COUNTER_BACKUP}", file=sys.stderr)
        return 2

    if paths and paths[0] == "--diff":
        return _scan_diff(sys.stdin.read())

    if paths and paths[0] in ("--analysis", "--counters"):
        rest = [a.lower() for a in paths[1:]]
        by_last_triggered = any(
            a in ("last-triggered", "last_triggered", "last", "triggered")
            for a in rest
        )
        rows = counter_analysis_rows()
        if by_last_triggered:
            # newest first; never-triggered (no timestamp) sorts last.
            # Stable sort: order by timestamp desc, then push "never" to the end.
            rows.sort(key=lambda r: r[2], reverse=True)
            rows.sort(key=lambda r: r[2] == "never")
        else:
            rows.sort(key=lambda r: r[1], reverse=True)
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
