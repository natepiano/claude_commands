#!/usr/bin/env python3
"""Report assertion and test changes; refuse unruled removals and weaker checks."""
from __future__ import annotations

import argparse
import ast
from bisect import bisect_right
from collections import Counter, defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
import re
import subprocess
import sys
from typing import Literal, NamedTuple, NoReturn, cast, override

Side = Literal["old", "new"]
Kind = Literal["removed", "weaker", "changed", "test removed", "test turned off"]
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
FUNCTION = re.compile(r"\b(?:fn|def)\s+([A-Za-z_]\w*)\s*[(<]")
ASSERTION = re.compile(
    r"\b(?P<macro>assert|assert_eq|assert_ne|debug_assert|debug_assert_eq|debug_assert_ne|"
    + r"assert_matches|prop_assert|prop_assert_eq|prop_assert_ne)!\s*\("
    + r"|\b(?P<bare>assert)\s+|\bself\.(?P<method>assert\w+|fail)\s*\("
    + r"|\b(?P<raises>pytest\.raises)\s*\(")
EXACT_CALLS = frozenset((
    "assert_eq", "assert_ne", "debug_assert_eq", "debug_assert_ne", "prop_assert_eq",
    "assertEqual", "assertNotEqual", "assertIs", "assertListEqual", "assertDictEqual",
    "assertTupleEqual", "assertSetEqual", "assertCountEqual", "assertMultiLineEqual", "assertSequenceEqual"))
COMPARISON = re.compile(r"==|!=|<=|>=|(?<![<])<(?![<])|(?<![>])>(?![>])|\bis(?:\s+not)?\b")
STATEMENT_TOKEN = re.compile(r'''"""|\'\'\'|["']|//|/\*|\*/|\#|[()[\]{}]''')
PYTHON_LITERAL = re.compile(
    r"'''(?:\\.|'(?!'')|[^'\\])*'''"
    + r'|"""(?:\\.|"(?!"")|[^"\\])*"""'
    + r'''|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|\#[^\n]*''', re.DOTALL)
RUST_LITERAL = re.compile(
    r'''(?:br(?<!\wbr)|r(?<!\wr))(?P<hashes>\#*)"(?s:.*?)"(?P=hashes)'''
    + r'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])'|//[^\n]*|/\*''', re.DOTALL)
BLOCK_COMMENT = re.compile(r"/\*|\*/")
FINDING_TOKEN = re.compile(r"assert|pytest\.raises|self\.fail|ignore|skip|expectedFailure|xfail")
WEAKER = re.compile(
    r"\b(?:assert|debug_assert|prop_assert)!\s*\(|\bassert\s+"
    + r"|\bself\.(?:assertTrue|assertFalse|assertLess\w*|assertGreater\w*|assertIn|"
    + r"assertAlmostEqual|assertRegex)\s*\(")
TURNED_OFF = re.compile(
    r"#\[ignore\b|@unittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b"
    + r"|@pytest\.mark\.(?:skip|skipif|xfail)\b|\bself\.skipTest\s*\(|\bpytest\.skip\s*\(")
TEST_MARK = re.compile(r"#\[(?:test|tokio::test|rstest)\b")
PYTHON_TEST = re.compile(r"\bdef\s+(test_\w*)\s*\(")
RULING = re.compile(r"(?:^|(?:-[a-zA-Z]*m|--message)[= \t]*[\"']?)Assertion-Change:"
                    + r"[ \t]*(.+?)::([^\s]+)[ \t]+[—-][ \t]*(\S[^\n]*)$", re.MULTILINE)


class DiffLine(NamedTuple):
    path: str
    side: Side
    number: int
    text: str
    hunk: int

    @property
    def key(self) -> str:
        return normalized(self.text)


@dataclass(frozen=True)
class Finding:
    source: DiffLine
    kind: Kind
    old: str
    new: str
    test: str = "?"
    removed_assertions: int = 0


@dataclass(frozen=True)
class Assertion:
    source: DiffLine
    name: str
    operands: tuple[str, ...]
    exact: bool
    weaker: bool
    hunks: tuple[int, ...]
    subject_text: str


@dataclass(frozen=True)
class NoReplacement:
    """No added assertion meets the pairing rule."""


class GuardError(Exception):
    """The guard cannot judge this diff."""


class GuardParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise GuardError(message)


class GuardArguments(argparse.Namespace):
    repo: str = "."
    head: str = ""
    staged: bool = False
    def __init__(self) -> None:
        super().__init__()
        self.bases: list[str] = []
        self.rulings: list[str] = []


class AssertionSpan(NamedTuple):
    first: int
    last: int
    text: str


class TouchedAssertion(NamedTuple):
    source: DiffLine
    hunks: tuple[int, ...]


def normalized(text: str) -> str:
    return "".join(text.split())


def code_lines(lines: list[str], rust: bool) -> list[str]:
    """Hide strings and comments, with the same line and column positions."""
    token_pattern = RUST_LITERAL if rust else PYTHON_LITERAL
    text = "\n".join(lines)
    parts: list[str] = []
    cursor = 0
    scan = 0
    while match := token_pattern.search(text, scan):
        end = match.end()
        if rust and match[0] == "/*":
            depth = 1
            end = len(text)
            for delimiter in BLOCK_COMMENT.finditer(text, match.end()):
                depth += 1 if delimiter[0] == "/*" else -1
                if not depth:
                    end = delimiter.end()
                    break
        if FINDING_TOKEN.search(text, match.start(), end):
            parts.append(text[cursor:match.start()])
            parts.append("\n".join(" " * len(row) for row in text[match.start():end].split("\n")))
            cursor = end
        scan = end
    if not parts:
        return lines
    parts.append(text[cursor:])
    return "".join(parts).split("\n")


def diff_path(text: str) -> str:
    if text.startswith('"'):
        value = cast(object, ast.literal_eval(text))
        if not isinstance(value, str):
            raise GuardError("invalid diff path")
        text = value
    return text[2:] if text.startswith(("a/", "b/")) else text


def parse_diff(text: str) -> list[DiffLine]:
    lines: list[DiffLine] = []
    old_path = new_path = ""
    old_number = new_number = hunk = 0
    in_hunk = False
    for row in text.splitlines():
        if in_hunk and row.startswith(("-", "+")):
            content = row[1:]
            if row[0] == "-":
                if old_path.endswith((".rs", ".py")):
                    lines.append(DiffLine(old_path, "old", old_number, content, hunk))
                old_number += 1
            else:
                if new_path.endswith((".rs", ".py")):
                    lines.append(DiffLine(new_path, "new", new_number, content, hunk))
                new_number += 1
        elif row.startswith("diff --git "):
            in_hunk = False
        elif not in_hunk and row.startswith("--- "):
            old_path = diff_path(row[4:])
        elif not in_hunk and row.startswith("+++ "):
            new_path = diff_path(row[4:])
        elif row.startswith("@@ "):
            match = HUNK.match(row)
            if match is None:
                raise GuardError("invalid diff hunk")
            old_number, new_number = int(match[1]), int(match[2])
            hunk += 1
            in_hunk = True
        elif in_hunk and row.startswith(" "):
            old_number += 1
            new_number += 1
    return lines


def argument_spans(text: str) -> list[tuple[int, int]]:
    """Split call arguments without splitting strings or nested expressions."""
    spans: list[tuple[int, int]] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                spans.append((start, index))
                return spans
            depth -= 1
        elif char == "," and depth == 0:
            spans.append((start, index))
            start = index + 1
    spans.append((start, len(text)))
    return spans


def comparison_operands(text: str) -> tuple[tuple[str, ...], bool]:
    """Read a top-level comparison, including one followed by another condition."""
    text = text.strip()
    openings: list[int] = []
    closes: dict[int, int] = {}
    comparisons: list[tuple[int, int, int, str]] = []
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "([{":
            openings.append(index)
        elif char in ")]}":
            if openings:
                closes[openings.pop()] = index
        elif char in "=!<>" or char == "i":
            comparison = COMPARISON.match(text, index)
            if comparison:
                comparisons.append((len(openings), index, comparison.end(), comparison[0]))
    # Remove only parentheses that enclose the whole condition, in one pass.
    start, end, depth = 0, len(text), 0
    while start < end and text[start] == "(" and closes.get(start) == end - 1:
        start, end, depth = start + 1, end - 1, depth + 1
        while start < end and text[start].isspace():
            start += 1
        while start < end and text[end - 1].isspace():
            end -= 1
    for level, index, right, operator in comparisons:
        if level == depth:
            return ((normalized(text[start:index]), normalized(text[right:end])),
                    operator in ("==", "!=", "is"))
    return (normalized(text[start:end]),), False


def parse_assertion(line: DiffLine, hunks: tuple[int, ...]) -> Assertion:
    match = ASSERTION.search(code_lines([line.text], line.path.endswith(".rs"))[0])
    if match is None:
        raise GuardError("cannot read assertion")
    name = match.group("macro") or match.group("method") or match.group("bare") or match.group("raises")
    arguments = line.text[match.end():]
    operands = tuple(arguments[start:end].strip() for start, end in argument_spans(arguments)
                     if arguments[start:end].strip())
    exact = name in EXACT_CALLS
    if name in ("assert", "debug_assert", "prop_assert") and operands:
        operands, exact = comparison_operands(operands[0])
    else:
        operands = tuple(normalized(operand) for operand in operands)
    subject_text = normalized(arguments) if match.group("bare") else line.key
    return Assertion(line, name, operands, exact, not exact and is_weaker(line.text), hunks, subject_text)


def assertion_spans(lines: list[str], code_rows: list[str], rust: bool,
                    changes: list[DiffLine]) -> Iterable[AssertionSpan]:
    """Scan each assertion once and stop its statement after at most 40 lines."""
    index = 0
    cursor = 0
    while index < len(lines):
        while cursor < len(changes) and changes[cursor].number <= index:
            cursor += 1
        if cursor == len(changes):
            break
        # A statement more than 39 lines above the next change cannot hold it.
        index = max(index, changes[cursor].number - 40)
        if not is_assertion(code_rows[index]):
            index += 1
            continue
        first = index
        depth = 0
        quote = ""
        block = 0
        parts: list[str] = []
        for index in range(first, min(first + 40, len(lines))):
            row = lines[index]
            code: list[str] = []
            start = 0
            for match in STATEMENT_TOKEN.finditer(row):
                token = match[0]
                offset = match.start()
                if block:
                    if token == "/*":
                        block += 1
                    elif token == "*/":
                        block -= 1
                        start = match.end()
                    continue
                if quote:
                    if token == quote:
                        before = offset - 1
                        while before >= 0 and row[before] == "\\":
                            before -= 1
                        if (offset - before) % 2:
                            quote = ""
                elif (rust and token == "//") or (not rust and token == "#"):
                    code.append(row[start:offset])
                    start = len(row)
                    break
                elif rust and token == "/*":
                    code.append(row[start:offset])
                    block = 1
                elif token[0] in "\"'":
                    quote = token
                elif token in "([{":
                    depth += 1
                elif token in ")]}":
                    depth -= 1
            if not block:
                code.append(row[start:])
            part = "".join(code).strip()
            parts.append(part)
            if depth <= 0 and not quote and not block and not part.endswith("\\"):
                break
        text = " ".join(parts)
        if is_assertion(text):
            yield AssertionSpan(first + 1, index + 1, text)
        index += 1


def touched_assertions(lines: list[DiffLine], sources: dict[tuple[str, Side], list[str]],
                       code: dict[tuple[str, Side], list[str]]) -> list[TouchedAssertion]:
    groups: dict[tuple[str, Side], list[DiffLine]] = defaultdict(list)
    for line in lines:
        groups[line.path, line.side].append(line)
    assertions: list[TouchedAssertion] = []
    for (path, side), changes in groups.items():
        cursor = 0
        for span in assertion_spans(sources[path, side], code[path, side], path.endswith(".rs"), changes):
            while cursor < len(changes) and changes[cursor].number < span.first:
                cursor += 1
            hunks: dict[int, None] = {}
            while cursor < len(changes) and changes[cursor].number <= span.last:
                hunks[changes[cursor].hunk] = None
                cursor += 1
            if hunks:
                source = DiffLine(path, side, span.first, span.text, next(iter(hunks)))
                assertions.append(TouchedAssertion(source, tuple(hunks)))
            if cursor == len(changes):
                break
    return assertions


def weaker_subjects(additions: list[Assertion], removals: list[Assertion]) -> dict[tuple[int, str], deque[Assertion]]:
    """Find all old subjects in new checks with one shared prefix scan."""
    subjects = {assertion.operands[0] for assertion in removals if assertion.exact and assertion.operands}
    branches: list[dict[str, int]] = [{}]
    fallback = [0]
    endings: list[list[str]] = [[]]
    for subject in subjects:
        node = 0
        for char in subject:
            if char not in branches[node]:
                branches[node][char] = len(branches)
                branches.append({})
                fallback.append(0)
                endings.append([])
            node = branches[node][char]
        endings[node].append(subject)
    queue = deque(branches[0].values())
    while queue:
        node = queue.popleft()
        for char, child in branches[node].items():
            queue.append(child)
            target = fallback[node]
            while target and char not in branches[target]:
                target = fallback[target]
            fallback[child] = branches[target].get(char, 0)
            endings[child].extend(endings[fallback[child]])
    matches: dict[tuple[int, str], deque[Assertion]] = defaultdict(deque)
    for assertion in additions:
        if not assertion.weaker:
            continue
        text = assertion.subject_text
        node = 0
        found: set[str] = set()
        for index, char in enumerate(text):
            while node and char not in branches[node]:
                node = fallback[node]
            node = branches[node].get(char, 0)
            for subject in endings[node]:
                before = index - len(subject)
                after = index + 1
                if ((before < 0 or not (text[before].isalnum() or text[before] == "_"))
                        and (after == len(text) or not (text[after].isalnum() or text[after] == "_"))):
                    found.add(subject)
        for subject in found:
            for hunk in assertion.hunks:
                matches[hunk, subject].append(assertion)
    return matches


def take_replacement(queues: Iterable[deque[Assertion]], paired: set[DiffLine]) -> Assertion | NoReplacement:
    replacement: Assertion | NoReplacement = NoReplacement()
    for queue in queues:
        while queue and queue[0].source in paired:
            _ = queue.popleft()
        if queue and (isinstance(replacement, NoReplacement)
                      or queue[0].source.number < replacement.source.number):
            replacement = queue[0]
    if isinstance(replacement, Assertion):
        paired.add(replacement.source)
    return replacement


def is_weaker(text: str) -> bool:
    return bool(re.search(r"<=|>=|(?<![=!])<|>|\.\.|\bcontains\s*\(|\bmatches!|"
                          + r"assert(?:Less|Greater|In|AlmostEqual|Regex)", text)
                or (WEAKER.search(text) and not re.search(r"==|!=|\bis\b", text)))


def is_assertion(text: str) -> bool:
    return (("assert" in text or "pytest.raises" in text or "self.fail" in text)
            and ASSERTION.search(text) is not None)


def test_lines(lines: list[DiffLine]) -> list[tuple[DiffLine, str]]:
    tests: list[tuple[DiffLine, str]] = []
    markers: dict[tuple[str, Side], int] = {}
    for line in lines:
        if "#[" not in line.text and "def" not in line.text and "fn" not in line.text:
            continue
        key = line.path, line.side
        if "#[" in line.text and TEST_MARK.search(line.text):
            markers[key] = line.number
        match = PYTHON_TEST.search(line.text) if "def" in line.text else None
        if match:
            tests.append((line, match[1]))
        elif "fn" in line.text and line.path.endswith(".rs") and 0 < line.number - markers.get(key, -4) <= 3:
            function = FUNCTION.search(line.text)
            if function:
                tests.append((line, function[1]))
    return tests


def unmatched_lines(lines: list[DiffLine]) -> list[DiffLine]:
    added = Counter(line.key for line in lines if line.side == "new")
    removed = Counter(line.key for line in lines if line.side == "old")
    unmatched: list[DiffLine] = []
    for line in lines:
        opposite = removed if line.side == "new" else added
        if opposite[line.key]:
            opposite[line.key] -= 1
        else:
            unmatched.append(line)
    return unmatched


def find_changes(lines: list[DiffLine], touched: list[TouchedAssertion],
                 code: dict[tuple[str, Side], list[str]]) -> list[Finding]:
    statement_lines = unmatched_lines([assertion.source for assertion in touched])
    remaining = set(statement_lines)
    assertions = [parse_assertion(assertion.source, assertion.hunks)
                  for assertion in touched if assertion.source in remaining]
    additions = [assertion for assertion in assertions if assertion.source.side == "new"]
    unmatched = [assertion for assertion in assertions if assertion.source.side == "old"]
    weaker = weaker_subjects(additions, unmatched)
    changed: dict[tuple[int, str, str], deque[Assertion]] = defaultdict(deque)
    exact_changes: dict[tuple[int, str, str], deque[Assertion]] = defaultdict(deque)
    stronger: dict[int, deque[Assertion]] = defaultdict(deque)
    marker_keys = {line.key for line in lines if line.side == "new"
                   and ("#[" in line.text or "@" in line.text or "skip" in line.text)
                   and TURNED_OFF.search(code[line.path, line.side][line.number - 1])}
    marker_lines = unmatched_lines([line for line in lines if line.key in marker_keys]) if marker_keys else []
    findings = [Finding(line, "test turned off", "(none)", line.text.strip())
                for line in marker_lines if line.side == "new"]
    for assertion in additions:
        for hunk in assertion.hunks:
            for operand in set(assertion.operands):
                changed[hunk, assertion.name, operand].append(assertion)
                if not assertion.weaker:
                    exact_changes[hunk, assertion.name, operand].append(assertion)
            if assertion.exact:
                stronger[hunk].append(assertion)
    paired: set[DiffLine] = set()
    replacements: dict[DiffLine, Finding] = {}
    # Check all shared subjects before changed values can use the same additions.
    for assertion in unmatched:
        line = assertion.source
        if assertion.exact and assertion.operands:
            candidate = take_replacement((weaker[hunk, assertion.operands[0]] for hunk in assertion.hunks), paired)
            if isinstance(candidate, Assertion):
                replacements[line] = Finding(line, "weaker", line.text.strip(), candidate.source.text.strip())
    for assertion in unmatched:
        line = assertion.source
        if line in replacements:
            continue
        index = exact_changes if assertion.exact else changed
        candidate = take_replacement((index[hunk, assertion.name, operand] for hunk in assertion.hunks
                                      for operand in set(assertion.operands)), paired)
        if isinstance(candidate, Assertion):
            replacements[line] = Finding(line, "changed", line.text.strip(), candidate.source.text.strip())
    for assertion in unmatched:
        line = assertion.source
        if line in replacements:
            findings.append(replacements[line])
        elif not assertion.exact:
            # A stronger exact check can take the place of a bare check.
            candidate = take_replacement((stronger[hunk] for hunk in assertion.hunks), paired)
            if isinstance(candidate, NoReplacement):
                findings.append(Finding(line, "removed", line.text.strip(), "(none)"))
        else:
            findings.append(Finding(line, "removed", line.text.strip(), "(none)"))
    tests = test_lines(lines)
    new_tests = {name for line, name in tests if line.side == "new"}
    for line, name in tests:
        if line.side == "old" and name not in new_tests:
            findings.append(Finding(line, "test removed", line.text.strip(), "(none)"))
    return findings


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *arguments], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        raise GuardError(result.stderr.strip() or f"git {arguments[0]} failed")
    return result.stdout


def read_sources(repo: Path, base: str, head: str, staged: bool,
                 lines: list[DiffLine]) -> dict[tuple[str, Side], list[str]]:
    """Read each changed side once; get all revision contents in one batch."""
    sources: dict[tuple[str, Side], list[str]] = {}
    requests: list[tuple[str, Side, str]] = []
    paths: dict[tuple[str, Side], None] = dict.fromkeys((line.path, line.side) for line in lines)
    for path, side in paths:
        if side == "new" and not staged and not head:
            sources[path, side] = (repo / path).read_text(encoding="utf-8").splitlines()
        else:
            revision = base if side == "old" else "" if staged else head
            if "\n" in path or "\r" in path:
                raise GuardError("cannot read a path with a line break")
            requests.append((path, side, f"{revision}:{path}"))
    if requests:
        result = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                                input="".join(f"{revision}\n" for _, _, revision in requests).encode("utf-8"),
                                capture_output=True, check=False)
        if result.returncode:
            raise GuardError(result.stderr.decode("utf-8", errors="replace").strip() or "git cat-file failed")
        offset = 0
        for path, side, revision in requests:
            end = result.stdout.find(b"\n", offset)
            fields = result.stdout[offset:end].split() if end >= 0 else []
            if len(fields) != 3 or fields[1] != b"blob":
                raise GuardError(f"cannot read {revision}")
            size = int(fields[2])
            offset = end + 1
            content = result.stdout[offset:offset + size]
            offset += size
            if len(content) != size or result.stdout[offset:offset + 1] != b"\n":
                raise GuardError("incomplete git cat-file output")
            sources[path, side] = content.decode("utf-8", errors="replace").splitlines()
            offset += 1
    return sources


def default_base(repo: Path, explicit_base: str = "") -> str:
    """Use the branch's upstream or its nearest unambiguous remote ancestor."""
    if explicit_base:
        return git(repo, "rev-parse", "--verify", f"{explicit_base}^{{commit}}").strip()
    try:
        return git(repo, "merge-base", "HEAD", "@{upstream}").strip()
    except GuardError:
        pass
    default_remote = ""
    try:
        default_remote = git(repo, "symbolic-ref", "refs/remotes/origin/HEAD").strip()
    except GuardError:
        pass
    nearest: dict[str, int] = {}
    for reference in git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/").splitlines():
        if reference.endswith("/HEAD") or reference == default_remote:
            continue
        try:
            base = git(repo, "merge-base", "HEAD", reference).strip()
            nearest[base] = int(git(repo, "rev-list", "--count", f"{base}..HEAD").strip())
        except GuardError:
            continue
    if nearest:
        distance = min(nearest.values())
        bases = [base for base, count in nearest.items() if count == distance]
        if len(bases) == 1:
            return bases[0]
    raise GuardError("cannot determine branch base; set VERIFY_ASSERTION_BASE (or --base)")


def commit_bases(repo: Path) -> tuple[str, ...]:
    """The parents of the commit being made, including all pending merge heads."""
    bases = [git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip()]
    merge_head = repo / git(repo, "rev-parse", "--git-path", "MERGE_HEAD").strip()
    try:
        merging = merge_head.read_text(encoding="ascii").splitlines()
    except FileNotFoundError:
        merging = []
    for revision in merging:
        bases.append(git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip())
    return tuple(dict.fromkeys(bases))


def name_tests(findings: list[Finding], sources: dict[tuple[str, Side], list[str]]) -> list[Finding]:
    groups: dict[tuple[str, Side], list[Finding]] = defaultdict(list)
    for finding in findings:
        groups[finding.source.path, finding.source.side].append(finding)
    named: list[Finding] = []
    for (path, side), group in groups.items():
        functions = [(number, match[1]) for number, text in enumerate(sources[path, side], 1)
                     if "fn" in text or "def" in text
                     if (match := FUNCTION.search(text))]
        positions = [number for number, _ in functions]
        for finding in group:
            index = bisect_right(positions, finding.source.number) - 1
            named.append(replace(finding, test=functions[index][1] if index >= 0 else "?"))
    removed_tests = {(finding.source.path, finding.test) for finding in named if finding.kind == "test removed"}
    counts = Counter((finding.source.path, finding.test) for finding in named if finding.kind == "removed")
    combined = [replace(finding, removed_assertions=counts[finding.source.path, finding.test])
                if finding.kind == "test removed" else finding
                for finding in named if finding.kind != "removed"
                or (finding.source.path, finding.test) not in removed_tests]
    return sorted(combined, key=lambda finding: (finding.source.path, finding.source.number))


def read_rulings(sources: list[str], messages: str) -> dict[tuple[str, str], str]:
    texts = [messages]
    for source in sources:
        if source == "-":
            texts.append(sys.stdin.read())
        else:
            path = Path(source).expanduser()
            paths = sorted(path.rglob("*.md")) if path.is_dir() else [path]
            texts.extend(item.read_text(encoding="utf-8") for item in paths)
    return {(match[1], match[2]): why
            for text in texts for match in RULING.finditer(text)
            if (why := match[3].strip().rstrip("\"'").strip())}


def findings_against_base(repo: Path, base: str, head: str, staged: bool) -> list[Finding]:
    """Judge a single parent against the selected tree, index or revision."""
    diff_args = ["diff", "--no-color", "--no-ext-diff", "-U0", "--find-renames"]
    if staged:
        diff_args.append("--cached")
    diff_args.append(base)
    if head:
        diff_args.append(head)
    diff_args.extend(("--", "*.rs", "*.py"))
    diff = git(repo, *diff_args)
    lines = parse_diff(diff)
    sources = read_sources(repo, base, head, staged, lines)
    code = {key: code_lines(rows, key[0].endswith(".rs")) for key, rows in sources.items()}
    assertions = touched_assertions(lines, sources, code)
    return name_tests(find_changes(lines, assertions, code), sources)


def finding_identity(finding: Finding) -> tuple[str, str, Kind, str, str]:
    """Match the same change across parents even when source line numbers differ."""
    return (finding.source.path, finding.test, finding.kind, normalized(finding.old), normalized(finding.new))


def run(arguments: list[str]) -> int:
    parser = GuardParser(description=__doc__)
    _ = parser.add_argument("--repo", default=".")
    _ = parser.add_argument("--base", dest="bases", action="append", default=[])
    new_side = parser.add_mutually_exclusive_group()
    _ = new_side.add_argument("--head", default="")
    _ = new_side.add_argument("--staged", action="store_true")
    _ = parser.add_argument("--rulings", action="append", default=[])
    args = GuardArguments()
    _ = parser.parse_args(arguments, namespace=args)
    repo = Path(args.repo).expanduser().resolve()
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    if args.bases:
        bases = [default_base(repo, base) for base in args.bases]
    else:
        bases = [default_base(repo)]
        bases.extend(git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[2:])
    bases = list(dict.fromkeys(bases))
    findings = findings_against_base(repo, bases[0], args.head, args.staged)
    common = Counter(finding_identity(finding) for finding in findings)
    for base in bases[1:]:
        common &= Counter(finding_identity(finding)
                          for finding in findings_against_base(repo, base, args.head, args.staged))
    shared: list[Finding] = []
    for finding in findings:
        identity = finding_identity(finding)
        if common[identity]:
            shared.append(finding)
            common[identity] -= 1
    messages = "\n".join(git(repo, "log", "--format=%B", f"{base}..{args.head or 'HEAD'}", "--")
                         for base in bases)
    rulings = read_rulings(args.rulings, messages)
    blocking = 0
    for finding in shared:
        why = rulings.get((finding.source.path, finding.test), "")
        prefix = "note: " if finding.kind == "changed" else ""
        suffix = f" — ruled: {why}" if why else ""
        count = f" ({finding.removed_assertions} assertions)" if finding.removed_assertions else ""
        print(f"{prefix}{finding.source.path}:{finding.source.number} {finding.test} "
              + f"{finding.kind}{count}: {finding.old} -> {finding.new}{suffix}")
        if finding.kind != "changed" and not why:
            blocking += 1
    if blocking:
        print(f'assertion guard: {blocking} blocking; add "Assertion-Change: <path>::<test> — <why>" '
              + "to the work order or the commit message, or restore the assertion")
    elif not shared:
        print("assertion guard: no removed or weaker assertions")
    return 1 if blocking else 0


def main() -> int:
    try:
        return run(sys.argv[1:])
    except (GuardError, OSError, UnicodeError, ValueError, SyntaxError) as error:
        print("assertion guard: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
