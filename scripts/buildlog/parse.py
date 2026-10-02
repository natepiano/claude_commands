"""Facts read from a step's argv and output.

The output is what invoke.sh's run() tee'd: stdout and stderr interleaved and
without a terminal, so cargo drew no progress bar and no color. ANSI is
stripped all the same, and a line keeps only the text after its last carriage
return, for tools that redraw a line in place.

Every count here is of what the log shows. A step that failed before cargo
printed its "Finished" line has no build time, so finished_s is None rather
than 0: None means "not measured", never "zero".
"""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import TypedDict

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

# "1m 05s" or "0.39s"; cargo switches to minutes past 60 seconds.
DURATION = r"(?:(\d+)m\s*)?(\d+(?:\.\d+)?)s"
CARGO_FINISHED = re.compile(r"^\s*Finished\b.*\btarget\(s\) in " + DURATION + r"\s*$")
# mend: "Finished in 0.25s (check: 0.25s, mend: 0.00s)". The check part is
# its cargo build, the counterpart of cargo's own Finished line.
MEND_FINISHED = re.compile(r"^\s*Finished in " + DURATION + r" \(check: " + DURATION + r", mend: " + DURATION + r"\)")
CRATE = re.compile(r"^\s*(?:Compiling|Checking|Documenting) \S+ v\d")
DIAGNOSTIC = re.compile(r"^(warning|error)(?:\[[\w:-]+\])?: ")
NOT_DIAGNOSTIC = re.compile(
    r"generated \d+ warnings?|build failed|could not compile|aborting due to|test run failed"
)
LOCATION = re.compile(r"^\s*--> (\S.*)$")

# "  TRY 2 PASS [   0.003s] (3/5) alpha tests::flaky": an optional retry
# number, the status, the duration ("[>  60.000s]" on SLOW), an optional
# progress counter ("(───)" on a retried attempt) and binary then test.
TEST_LINE = re.compile(
    r"^\s*(?:TRY (\d+) )?([A-Z][A-Z0-9-]*) \[\s*>?\s*(\d+(?:\.\d+)?)s\]\s+"
    + r"(?:\((?:\s*\d+/\d+|[─-]+)\)\s+)?(\S+)\s+(\S.*?)\s*$"
)
SUMMARY = re.compile(r"^\s*Summary \[\s*[\d.]+s\]\s+(\d+)(?:/\d+)? tests? run:(.*)$")
NOT_RESULT = frozenset({"START", "DELAY", "RETRY", "CANCEL", "SKIP", "FLAKY", "SETUP"})
PASSED = frozenset({"PASS", "LEAK", "TMPASS"})
TIMED_OUT = frozenset({"TIMEOUT", "TMT"})
SLOW_TEST_S = 1.0

MEND_APPLIED = re.compile(r"^\s*mend: applied\b(.*)$")
MEND_NONE = re.compile(r"^\s*mend: no\b.*\bfix(?:es)? available")
# "would remove" is --dry-run's wording and frees nothing, so only "removed".
SWEEP_ORPHANS = re.compile(r"\bremoved \d+ orphaned files \((\d+(?:\.\d+)?) (GiB|MiB)\)")
SWEEP_UNITS = re.compile(r"\bremoved \d+ build units and \d+ incremental dirs \((\d+(?:\.\d+)?) (GiB|MiB)\)")
SWEEP_DOC = re.compile(r"\bremoved \S.* \((\d+(?:\.\d+)?) (GiB|MiB)\), rebuilt by the next doc run")
UNIT_BYTES = {"GiB": 1 << 30, "MiB": 1 << 20}


class TestRow(TypedDict):
    binary: str
    test: str
    status: str
    attempts: int
    duration_s: float | None
    slow: bool


class LogFacts(TypedDict):
    finished_s: float | None
    crates_compiled: int | None
    warnings: int | None
    errors: int | None
    tests_run: int | None
    tests_passed: int | None
    tests_failed: int | None
    tests_skipped: int | None
    tests_flaky: int | None
    tests_retried: int | None
    tests: list[TestRow] | None
    mend_fixes: int | None
    mend_check_s: float | None
    mend_s: float | None
    sweep_freed_bytes: int | None


def no_facts() -> LogFacts:
    """What a step without a captured log records: nothing measured."""
    return {
        "finished_s": None,
        "crates_compiled": None,
        "warnings": None,
        "errors": None,
        "tests_run": None,
        "tests_passed": None,
        "tests_failed": None,
        "tests_skipped": None,
        "tests_flaky": None,
        "tests_retried": None,
        "tests": None,
        "mend_fixes": None,
        "mend_check_s": None,
        "mend_s": None,
        "sweep_freed_bytes": None,
    }


def command_words(argv: list[str]) -> list[str]:
    """argv without a leading `env` and its options and NAME=VALUE words."""
    if not argv or PurePath(argv[0]).name != "env":
        return argv
    index = 1
    while index < len(argv):
        word = argv[index]
        if word in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
            index += 2
        elif word.startswith("-") or "=" in word:
            index += 1
        else:
            break
    return argv[index:]


def step_name(argv: list[str]) -> str:
    """clippy, mend, doc, fmt, nextest, check or sweep; the program otherwise."""
    if any(PurePath(word).name == "sweep.py" for word in argv):
        return "sweep"
    words = command_words(argv)
    if not words:
        return "unknown"
    program = PurePath(words[0]).name
    if program != "cargo":
        return program
    rest = words[1:]
    if rest and rest[0].startswith("+"):
        rest = rest[1:]
    return rest[0] if rest else "cargo"


def toolchain(argv: list[str]) -> str | None:
    """The `+toolchain` a cargo argv names, as rustc takes it."""
    words = command_words(argv)
    if len(words) > 1 and PurePath(words[0]).name == "cargo" and words[1].startswith("+"):
        return words[1]
    return None


def manifest_path(argv: list[str]) -> str | None:
    for index, word in enumerate(argv):
        if word == "--manifest-path" and index + 1 < len(argv):
            return argv[index + 1]
        if word.startswith("--manifest-path="):
            return word.split("=", 1)[1]
    return None


def clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in ANSI.sub("", text).split("\n"):
        lines.append(raw.rstrip("\r").rsplit("\r", 1)[-1])
    return lines


def seconds(minutes: str | None, secs: str) -> float:
    return int(minutes or 0) * 60 + float(secs)


def build_seconds(lines: list[str]) -> tuple[float | None, float | None, float | None]:
    """Build time, then mend's check and own time; mend's check stands in for cargo's."""
    cargo: list[float] = []
    checks: list[float] = []
    mends: list[float] = []
    for line in lines:
        match = CARGO_FINISHED.match(line)
        if match:
            cargo.append(seconds(match.group(1), match.group(2)))
            continue
        match = MEND_FINISHED.match(line)
        if match:
            checks.append(seconds(match.group(3), match.group(4)))
            mends.append(seconds(match.group(5), match.group(6)))
    mend_check = round(sum(checks), 3) if checks else None
    mend = round(sum(mends), 3) if mends else None
    if checks:
        return mend_check, mend_check, mend
    return (round(sum(cargo), 3) if cargo else None), mend_check, mend


def diagnostics(lines: list[str]) -> tuple[int, int]:
    """Distinct warnings and errors: a diagnostic cargo repeats for each target counts once."""
    seen: set[tuple[str, str, str]] = set()
    for index, line in enumerate(lines):
        match = DIAGNOSTIC.match(line)
        if not match or NOT_DIAGNOSTIC.search(line):
            continue
        location = ""
        if index + 1 < len(lines):
            found = LOCATION.match(lines[index + 1])
            if found:
                location = found.group(1)
        seen.add((match.group(1), line, location))
    warnings = sum(1 for kind, _, _ in seen if kind == "warning")
    return warnings, len(seen) - warnings


class Summary(TypedDict):
    run: int
    passed: int
    failed: int
    skipped: int
    flaky: int


def summary_counts(rest: str) -> Summary:
    def count(pattern: str) -> int:
        return sum(int(found.group(1)) for found in re.finditer(pattern, rest))

    return {
        "run": 0,
        "passed": count(r"(\d+) passed"),
        "failed": count(r"(\d+) failed") + count(r"(\d+) timed out") + count(r"(\d+) exec failed"),
        "skipped": count(r"(\d+) skipped"),
        "flaky": count(r"(\d+) flaky"),
    }


def result_status(word: str) -> str:
    if word in PASSED:
        return "passed"
    if word in TIMED_OUT:
        return "timed_out"
    return "failed"


def test_results(lines: list[str]) -> tuple[list[TestRow], list[Summary]]:
    """Per-test rows worth keeping, and the Summary counts.

    Only lines before the Summary count: nextest reprints the failed and flaky
    ones after it. A row is kept for a test that failed, was retried, ran past
    SLOW_TEST_S or was reported SLOW; the counts cover every test.
    """
    tests: dict[tuple[str, str], TestRow] = {}
    summaries: list[Summary] = []
    for line in lines:
        found = SUMMARY.match(line)
        if found:
            counts = summary_counts(found.group(2))
            counts["run"] = int(found.group(1))
            summaries.append(counts)
            break
        match = TEST_LINE.match(line)
        if not match:
            continue
        attempt_text, word, duration, binary, test = match.groups()
        if word in NOT_RESULT:
            continue
        row = tests.setdefault(
            (binary, test),
            {"binary": binary, "test": test, "status": "passed", "attempts": 0, "duration_s": None, "slow": False},
        )
        if word == "SLOW":
            row["slow"] = True
            continue
        attempt = int(attempt_text) if attempt_text else 1
        if attempt >= row["attempts"]:
            row["attempts"] = attempt
            row["status"] = result_status(word)
            row["duration_s"] = float(duration)
    rows: list[TestRow] = []
    for row in tests.values():
        if row["attempts"] == 0:
            row["attempts"] = 1
        if row["status"] == "passed" and row["attempts"] > 1:
            row["status"] = "flaky"
        if row["status"] != "passed" or row["attempts"] > 1 or row["slow"] or (row["duration_s"] or 0) > SLOW_TEST_S:
            rows.append(row)
    return rows, summaries


def mend_fixes(lines: list[str]) -> int | None:
    total = 0
    seen = False
    for line in lines:
        applied = MEND_APPLIED.match(line)
        if applied:
            seen = True
            total += sum(int(number.group(0)) for number in re.finditer(r"\d+", applied.group(1)))
        elif MEND_NONE.match(line):
            seen = True
    return total if seen else None


def sweep_freed(lines: list[str]) -> int:
    freed = 0
    for line in lines:
        for pattern in (SWEEP_ORPHANS, SWEEP_UNITS, SWEEP_DOC):
            match = pattern.search(line)
            if match:
                freed += int(float(match.group(1)) * UNIT_BYTES[match.group(2)])
    return freed


def log_facts(step: str, text: str) -> LogFacts:
    lines = clean_lines(text)
    facts = no_facts()
    finished, mend_check, mend = build_seconds(lines)
    facts["finished_s"] = finished
    facts["mend_check_s"] = mend_check
    facts["mend_s"] = mend
    facts["crates_compiled"] = sum(1 for line in lines if CRATE.match(line))
    facts["warnings"], facts["errors"] = diagnostics(lines)
    rows, summaries = test_results(lines)
    if summaries or step == "nextest":
        facts["tests"] = rows
        facts["tests_retried"] = sum(1 for row in rows if row["attempts"] > 1)
    if summaries:
        facts["tests_run"] = sum(summary["run"] for summary in summaries)
        facts["tests_passed"] = sum(summary["passed"] for summary in summaries)
        facts["tests_failed"] = sum(summary["failed"] for summary in summaries)
        facts["tests_skipped"] = sum(summary["skipped"] for summary in summaries)
        facts["tests_flaky"] = sum(summary["flaky"] for summary in summaries)
    facts["mend_fixes"] = mend_fixes(lines)
    if step == "sweep":
        facts["sweep_freed_bytes"] = sweep_freed(lines)
    return facts
