"""Estimate a step from the recorded build and memory history."""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import fcntl
import json
import math
import os
import re
import sqlite3
from typing import Callable, Literal, cast

from pathlib import Path


from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable
from scripts.build_queue.admission_records import (
    AdmissionPolicy,
    BuildHistoryGeneration,
    DevSizeHeadroom,
    ExpectedPeak,
    FailedPeakFloor,
    FirstProgramPeak,
    FreshTest,
    GIB,
    HeadroomRule,
    InferFromArgv,
    LegacyDevMultiplierHeadroom,
    MeasuredProgramPeak,
    MemoryConfig,
    NoBuildHistoryGeneration,
    NoFloor,
    NoHeadroom,
    ProgramProfile,
    ReleaseHeadroom,
    Repository,
    RepositoryIdentity,
    ShapeFallback,
    ShapeLargest,
    ShapeNeedBasis,
    ShapeP90,
    StepProfile,
    TestScope,
    UnknownRepository,
    read_memory_config,
    test_threads,
)

from scripts.lint.build_shape import (BuildShape, NoBuildShape, CompileCountUnknown, NoCratesCompiled,
    StepEvidence, build_shape, index_step_evidence, recorded_build_shape)
from scripts.lib.test_phase import TEST_STEPS
from scripts.buildlog import parse, record, repository, store
from scripts.buildlog import index as file_marks

# 111 natedev filtered single-test runs with no recompilation in the 14 days
# to 2026-10-29 had anon p90 0.671 GiB and p95 0.784 GiB.
FILTERED_FLOOR = GIB

# On natedev, isolated hana nextest scopes had p90 anon / memory.peak 61%;
# clippy had 65%, so 0.65 covers the measured p90 process-memory share.
ANON_SHARE = 0.65

HISTORY_DAYS = 14

VERIFY_PACKAGE_PREDICATE = r"package\(([A-Za-z0-9_-]+)\)"

VERIFY_NAME_PREDICATE = r"test\([A-Za-z0-9_:]+\)"

VERIFY_NAME_UNION = rf"\(\s*{VERIFY_NAME_PREDICATE}(?:\s*\|\s*{VERIFY_NAME_PREDICATE})+\s*\)"

VERIFY_FILTER_SELECTION = re.compile(
    rf"\s*{VERIFY_PACKAGE_PREDICATE}\s*&\s*(?:{VERIFY_NAME_PREDICATE}|{VERIFY_NAME_UNION})\s*")

VERIFY_PACKAGE_SELECTION = re.compile(rf"\s*{VERIFY_PACKAGE_PREDICATE}\s*")

VERIFY_TEST_NAMES = re.compile(r"\btest\(([A-Za-z0-9_:]+)\)")

TEST_PREDICATE = re.compile(r"\btest\s*\(")

TEST_EXPRESSION_OPTIONS = {"-E", "--filter-expr", "--filterset"}

TEST_OPTION_VALUES = {"-p", "--package", "--manifest-path", "--target", "--target-dir", "--profile",
              "--features", "--test-threads", "-j", "--jobs", "--config-file", "--cargo-profile",
              "--bin", "--example", "--test", "--bench", "--status-level", "--final-status-level",
              "--failure-output", "--success-output", "--color", "--message-format", "--retries",
              "--archive-file", "--workspace-remap", "--partition", "-F", "--exclude", "-P",
              "--run-ignored", "-Z", "--no-tests", "--max-fail", "--tool-config-file", "--test-group"}


def filter_set(names: Iterable[str]) -> tuple[str, ...]:
    """Use one sorted key for each set of test filters."""
    return tuple(sorted(set(names)))


@dataclass(frozen=True)
class PackageTestSelection:
    package: str
    filters: tuple[str, ...]


@dataclass(frozen=True)
class BroadTestSelection:
    pass


def filter_expressions(argv: list[str]) -> list[str]:
    expressions: list[str] = []
    arguments = iter(argv)
    for word in arguments:
        if word in TEST_EXPRESSION_OPTIONS:
            expressions.append(next(arguments, ""))
        elif word.startswith(("--filter-expr=", "--filterset=")):
            expressions.append(word.split("=", 1)[1])
        elif word.startswith("-E") and len(word) > 2:
            expressions.append(word[2:].removeprefix("="))
    return expressions


def _verify_test_selection(argv: list[str], scope: TestScope) -> PackageTestSelection | BroadTestSelection:
    """Recognize the whole positive expression verify emits, never its fragments."""
    expressions = filter_expressions(argv)
    if len(expressions) != 1:
        return BroadTestSelection()
    match = VERIFY_FILTER_SELECTION.fullmatch(expressions[0])
    if match:
        names = cast(list[str], VERIFY_TEST_NAMES.findall(expressions[0]))
        return PackageTestSelection(match.group(1), filter_set(names))
    # Full package runs retain their package metadata, without an exact filter set.
    match = VERIFY_PACKAGE_SELECTION.fullmatch(expressions[0])
    if match and scope == "full":
        return PackageTestSelection(match.group(1), ())
    return BroadTestSelection()


def _test_scope(argv: list[str], step: str) -> TestScope:
    words = argv[argv.index(step) + 1:]
    if step == "nextest" and words and words[0] in ("run", "list"):
        words = words[1:]
    arguments = iter(words)
    for word in arguments:
        option = word.split("=", 1)[0]
        if option in ("--skip", "--exact"):
            return "filtered"
        if option in TEST_EXPRESSION_OPTIONS or word.startswith("-E"):
            expression = (word[2:].removeprefix("=") if word.startswith("-E") and len(word) > 2 else
                          word.split("=", 1)[1] if "=" in word else next(arguments, ""))
            if TEST_PREDICATE.search(expression):
                return "filtered"
        elif word in TEST_OPTION_VALUES:
            _ = next(arguments, "")
        elif not word.startswith("-"):
            return "filtered"
    return "full"


def test_selection(argv: Sequence[str]) -> HistorySelection:
    """Read the package, scope and canonical filters with one command parse."""
    words = list(argv)
    step = parse.step_name(words)
    scope = _test_scope(words, step) if step in TEST_STEPS else "full"
    selection = _verify_test_selection(words, scope)
    package = selection.package if isinstance(selection, PackageTestSelection) else ""
    filters = selection.filters if isinstance(selection, PackageTestSelection) else ()
    if step in ("check", "build"):
        packages: list[str] = []
        arguments = iter(words)
        for word in arguments:
            if word in ("-p", "--package"):
                packages.append(next(arguments, ""))
            elif word.startswith("--package="):
                packages.append(word.split("=", 1)[1])
            elif word.startswith("-p") and len(word) > 2:
                packages.append(word[2:])
        selected = set(packages)
        package = selected.pop() if len(selected) == 1 else ""
    return HistorySelection(package, scope, filters)


def verify_test_selection(argv: list[str]) -> PackageTestSelection | BroadTestSelection:
    """Recognize the whole positive expression that verify emits."""
    return _verify_test_selection(argv, test_selection(argv).scope)



def test_filters(argv: list[str]) -> tuple[str, ...]:
    """Return the canonical filters of a verify test selection."""
    return test_selection(argv).filters


def test_package(argv: list[str]) -> str:
    """Return one test or compile package, or an empty string."""
    return test_selection(argv).package


def test_scope(argv: list[str]) -> TestScope:
    """Keep test selection separate from package, target and execution options."""
    return test_selection(argv).scope


@dataclass(frozen=True)
class Completed:
    pass


@dataclass(frozen=True)
class Failed:
    pass


@dataclass(frozen=True)
class Killed:
    pass


@dataclass(frozen=True)
class OwnerGone:
    pass


@dataclass(frozen=True)
class OutcomeNotRecorded:
    pass

FinalOutcome = Completed | Failed | Killed | OwnerGone

HistoryOutcome = FinalOutcome | OutcomeNotRecorded

NO_BUILD_SHAPE = NoBuildShape()

NO_STEP_EVIDENCE = StepEvidence(NoBuildShape(), CompileCountUnknown())


def anonymous_history_rows(path: Path) -> Iterator[dict[str, object]]:
    """Read complete anonymous-history rows under a shared lock."""
    return store.locked_rows(path)


def program_peak_estimate(program: str, host: str, now: datetime, root: Path,
                          first_peak_bytes: int) -> MeasuredProgramPeak | FirstProgramPeak:
    """Read one program's peak history, without using cargo estimates."""
    if program == "merge-gate":
        return FirstProgramPeak(first_peak_bytes)
    cutoff = now - timedelta(days=HISTORY_DAYS)
    peaks: list[int] = []
    for row in anonymous_history_rows(root / "admission/anon_peaks.jsonl"):
        try:
            if row.get("host") != host or row.get("step") != program:
                continue
            peak, ended = row.get("anon_peak_bytes"), row.get("ended_at")
            if not isinstance(peak, int) or peak <= 0 or not isinstance(ended, str):
                continue
            if cutoff <= datetime.fromisoformat(ended.replace("Z", "+00:00")) <= now:
                peaks.append(peak)
        except (ValueError, TypeError, AttributeError):
            continue
    return MeasuredProgramPeak(percentile(peaks), len(peaks)) if peaks else FirstProgramPeak(first_peak_bytes)


def memory_config() -> MemoryConfig:
    return read_memory_config()


def percentile(values: list[int]) -> int:
    values.sort()
    return values[(90 * len(values) + 99) // 100 - 1]


def expected_peak(repository: Repository, step: str, host: str, now: datetime, root: Path,
                  test_threads: int | None = None, *, scope: TestScope = "full",
                  filters: tuple[str, ...] = (), package: str = "",
                  fresh: bool = False,
                  shape: BuildShape | NoBuildShape = NO_BUILD_SHAPE,
                  first_peak_bytes: int) -> ExpectedPeak:
    """Estimate a compile promise from HISTORY_DAYS of earlier peaks.

    Full test requests with a package recover missing selection fields from
    indexed arguments. Other requests keep the recorded scope rule.
    The gate pools test widths. `test_threads` selects one width for reports.
    """
    if step == "build":
        return build_expected_peak(repository, shape, host, now, root, first_peak_bytes)
    if isinstance(repository, UnknownRepository):
        return ExpectedPeak(first_peak_bytes, "fallback", 0)
    filters = filter_set(filters)
    if fresh and step == "nextest" and package:
        compile_peak = expected_peak(repository, step, host, now, root, test_threads,
                                     scope=scope, filters=filters, package=package,
                                     first_peak_bytes=first_peak_bytes)
        return fresh_test_peak(repository, step, host, now, root, test_threads,
                               scope, filters, package, compile_peak)
    if scope == "filtered" and package:
        return filtered_peak(repository, step, host, now, root, test_threads,
                             filters, package)
    cutoff = store.utc_iso((now - timedelta(days=HISTORY_DAYS)).timestamp())
    before = store.utc_iso(now.timestamp())
    rows: list[dict[str, object]] = []
    for item, evidence in compiling_history(root, host, step, now):
        if package and step in TEST_STEPS:
            selection = history_selection(item, evidence)
            if selection.package != package or selection.scope != scope:
                continue
        elif item.get("test_scope", "full") != scope:
            continue
        if (test_threads is None or item.get("test_threads") == test_threads) and (
                item.get("repo_identity") or item.get("repo") not in GENERIC_REPO_NAMES):
            rows.append(item)
    measured = [int(cast(int, item["anon_peak_bytes"]))
                for item in repository_history(rows, repository)]
    pooled = [int(cast(int, item["anon_peak_bytes"])) for item in rows]
    if len(measured) >= 5:
        return ExpectedPeak(percentile(measured), "measured", len(measured))
    peaks: list[int] = []
    try:
        with closing(sqlite3.connect(file_marks.read_only_uri(root), uri=True)) as connection:
            if repository.legacy_name:
                columns = {row[1] for row in cast(list[tuple[object, ...]],
                           connection.execute("PRAGMA table_info(steps)").fetchall())}
                argv_column = "argv" if "argv" in columns else "NULL"
                compile_clause = " AND crates_compiled > 0" if "crates_compiled" in columns else ""
                indexed = cast(list[tuple[int, str | None]], connection.execute(
                    f"SELECT peak_mem_bytes, {argv_column} FROM steps WHERE host=? AND repo=? AND step=? "
                    + "AND started_at>=? AND started_at<? AND peak_mem_bytes>0" + compile_clause,
                    (host, repository.legacy_name, step, cutoff, before),
                ).fetchall())
                for value, raw_argv in indexed:
                    arguments = history_arguments(raw_argv)
                    selection = test_selection(arguments)
                    if (selection.scope == scope
                            and (step not in TEST_STEPS or not package or selection.package == package)):
                        peaks.append(value)
    except (OSError, sqlite3.Error):
        pass
    if len(peaks) >= 5:
        return ExpectedPeak(int(percentile(peaks) * ANON_SHARE), "buildlog", len(peaks))
    if measured:
        peak = max(measured)
        if len(pooled) >= 5:
            peak = min(peak, percentile(pooled))
        return ExpectedPeak(peak, "measured", len(measured))
    if peaks:
        peak = int(max(peaks) * ANON_SHARE)
        if len(pooled) >= 5:
            peak = min(peak, percentile(pooled))
        return ExpectedPeak(peak, "buildlog", len(peaks))
    if len(pooled) >= 5:
        return ExpectedPeak(percentile(pooled), "pooled", len(pooled))
    return ExpectedPeak(first_peak_bytes, "fallback", 0)


def repository_history(rows: Sequence[dict[str, object]],
                       repository: RepositoryIdentity) -> list[dict[str, object]]:
    """Use legacy rows only while the keyed history has fewer than five rows."""
    keyed = [row for row in rows if row.get("repo_identity") == repository.key]
    if len(keyed) >= 5:
        return keyed
    return [row for row in rows if history_matches_repository(row, repository)]


@dataclass(frozen=True)
class HistorySelection:
    package: str
    scope: TestScope
    filters: tuple[str, ...]


def history_selection(item: Mapping[str, object], evidence: StepEvidence) -> HistorySelection:
    """Keep recorded fields and recover missing fields from test arguments."""
    recovered = (test_selection(evidence.arguments)
                 if evidence.arguments and any(key not in item for key in ("package", "test_scope", "filters"))
                 else HistorySelection("", "full", ()))
    package = item.get("package", recovered.package)
    scope = item.get("test_scope", recovered.scope)
    names = item.get("filters", recovered.filters)
    filters = (filter_set(cast(Sequence[str], names))
               if isinstance(names, (list, tuple))
               and all(isinstance(name, str) for name in cast(Sequence[object], names)) else ())
    return HistorySelection(package if isinstance(package, str) else "",
                            "filtered" if scope == "filtered" else "full", filters)


def compiling_history(root: Path, host: str, step: str,
                      now: datetime, *, include_current_end: bool = False) -> list[tuple[dict[str, object], StepEvidence]]:
    """Exclude only peaks whose known attempts compiled no crates."""
    cutoff = now - timedelta(days=HISTORY_DAYS)
    evidence = index_step_evidence(file_marks.index_path_in(root), host, step,
                                   datetime.min.replace(tzinfo=now.tzinfo), now)
    peaks: list[tuple[dict[str, object], StepEvidence]] = []
    try:
        for item in anonymous_history_rows(root / "admission/anon_peaks.jsonl"):
            if item.get("host") != host or item.get("step") != step:
                continue
            joined = evidence.get((str(item.get("call_id", "")), step),
                                  NO_STEP_EVIDENCE)
            if isinstance(joined.compilation, NoCratesCompiled):
                continue
            try:
                ended_at = datetime.fromisoformat(str(item.get("ended_at", "")))
                peak = item.get("anon_peak_bytes")
                if (cutoff <= ended_at and (ended_at <= now if include_current_end else ended_at < now)
                        and isinstance(peak, int)
                        and not isinstance(peak, bool) and peak > 0):
                    peaks.append((item, joined))
            except (ValueError, TypeError):
                continue
    except OSError:
        pass
    return peaks


@dataclass(frozen=True)
class BuildPeak:
    shape: BuildShape
    peak: int
    ended_at: datetime
    need: int
    outcome: HistoryOutcome


def history_outcome(raw: object) -> HistoryOutcome:
    match raw:
        case "completed":
            return Completed()
        case "failed":
            return Failed()
        case "killed":
            return Killed()
        case "owner_gone":
            return OwnerGone()
        case _:
            return OutcomeNotRecorded()


def history_matches_repository(row: Mapping[str, object], repository: Repository) -> bool:
    return isinstance(repository, RepositoryIdentity) and (
        row.get("repo_identity") == repository.key
        or (not row.get("repo_identity") and bool(repository.legacy_name)
            and row.get("repo") == repository.legacy_name))


def build_peaks(repository: RepositoryIdentity, host: str, now: datetime, root: Path) -> list[BuildPeak]:
    """Keep compile peaks and resolve each shape from its recorded evidence."""
    history = compiling_history(root, host, "build", now, include_current_end=True)
    shaped: dict[BuildShape, list[dict[str, object]]] = {}
    for item, evidence in history:
        shape = recorded_build_shape(item.get("shape"))
        if isinstance(shape, NoBuildShape) and "shape" not in item:
            shape = evidence.shape
        if isinstance(shape, BuildShape):
            shaped.setdefault(shape, []).append(item)
    peaks: list[BuildPeak] = []
    for shape, rows in shaped.items():
        for item in repository_history(rows, repository):
            need = item.get("need")
            peaks.append(BuildPeak(shape, cast(int, item["anon_peak_bytes"]),
                                   datetime.fromisoformat(str(item["ended_at"])),
                                   need if isinstance(need, int) and need > 0 else 0,
                                   history_outcome(item.get("outcome"))))
    return peaks


def shape_headroom_bytes(base: int, rule: HeadroomRule) -> int:
    """Apply the recorded headroom rule to the same-shape base."""
    if isinstance(rule, DevSizeHeadroom):
        return math.ceil(base * rule.factor + rule.margin_bytes)
    if isinstance(rule, (ReleaseHeadroom, LegacyDevMultiplierHeadroom)):
        return math.ceil(base * rule.factor)
    return base


def build_expected_peak(repository: Repository, shape: BuildShape | NoBuildShape,
                        host: str, now: datetime, root: Path, first_peak_bytes: int) -> ExpectedPeak:
    rows = build_peaks(repository, host, now, root) if isinstance(repository, RepositoryIdentity) else []
    same = [row for row in rows if row.shape == shape]
    floor: FailedPeakFloor | NoFloor = NoFloor()
    failures = [row for row in same if not isinstance(row.outcome, OutcomeNotRecorded)
                and (isinstance(row.outcome, (Killed, OwnerGone)) or (row.need > 0 and row.peak > row.need))]
    if failures:
        newest = max(failures, key=lambda row: row.ended_at)
        floor = FailedPeakFloor(newest.peak, store.utc_iso(newest.ended_at.timestamp()))
    count = len(same)
    if count >= 10:
        base = percentile([row.peak for row in same])
        basis: ShapeNeedBasis = ShapeP90(count)
    elif same:
        base = max(row.peak for row in same)
        basis = ShapeLargest(count)
    else:
        base = max([first_peak_bytes, *(row.peak for row in rows
                               if isinstance(shape, BuildShape) and row.shape.profile == shape.profile)])
        basis = ShapeFallback()
    if isinstance(basis, (ShapeP90, ShapeLargest)):
        if isinstance(floor, FailedPeakFloor):
            base = max(base, floor.peak)
        factor: HeadroomRule = NoHeadroom()
        if isinstance(shape, BuildShape):
            config = memory_config()
            if shape.profile == "release":
                factor = ReleaseHeadroom(config.release_headroom)
            elif shape.profile == "dev":
                factor = DevSizeHeadroom(config.dev_size_factor, config.dev_size_margin_bytes)
        uncapped = shape_headroom_bytes(base, factor)
        basis = replace(basis, floor=floor, factor=factor)
    else:
        uncapped = base
    return ExpectedPeak(uncapped, "shape", count, basis)


def history_arguments(raw: object) -> list[str]:
    """Decode indexed or canonical argv; absent arguments prove no selection."""
    if isinstance(raw, list):
        return [word for word in cast(list[object], raw) if isinstance(word, str)]
    if not isinstance(raw, str):
        return []
    try:
        decoded = cast(object, json.loads(raw))
        if isinstance(decoded, list):
            return [word for word in cast(list[object], decoded) if isinstance(word, str)]
    except (ValueError, TypeError):
        pass
    return []


def filtered_peak(repository: RepositoryIdentity, step: str, host: str, now: datetime,
                  root: Path, width: int | None, filters: tuple[str, ...],
                  package: str) -> ExpectedPeak:
    """Use the larger exact-filter and package anonymous p90.

    Recorded selection fields take priority. The shared resolver reads indexed
    test arguments only when a field is missing. Indexed test peaks, then
    check and build peaks, cover missing anonymous history.
    """
    cutoff = store.utc_iso((now - timedelta(days=HISTORY_DAYS)).timestamp())
    before = store.utc_iso(now.timestamp())
    measured: list[int] = []
    exact: list[int] = []
    compile_peaks: list[int] = []
    compile_index: list[int] = []
    histories = {name: compiling_history(root, host, name, now)
                 for name in dict.fromkeys((step, "check", "build"))}
    for sample_step, history in histories.items():
        selected = repository_history([entry[0] for entry in history], repository)
        selected_ids = {id(item) for item in selected}
        for item, evidence in history:
            if id(item) not in selected_ids:
                continue
            peak = cast(int, item["anon_peak_bytes"])
            selection = history_selection(item, evidence) if sample_step in TEST_STEPS else HistorySelection(
                cast(str, item.get("package", "")), "full", ())
            sample_package = selection.package
            if (sample_step in ("check", "build")
                    and (sample_package == package
                         or (not sample_package and package == repository.name))):
                compile_peaks.append(peak)
            if (sample_step != step or selection.scope != "filtered"
                    or sample_package != package
                    or (width is not None and item.get("test_threads") != width)):
                continue
            measured.append(peak)
            if filters and selection.filters == filters:
                exact.append(peak)
    if measured:
        package_peak = percentile(measured)
        if exact and (exact_peak := percentile(exact)) >= package_peak:
            return ExpectedPeak(exact_peak, "measured", len(exact))
        return ExpectedPeak(package_peak, "measured", len(measured))
    indexed: list[int] = []
    indexed_exact: list[int] = []
    if repository.legacy_name:
        try:
            connection = sqlite3.connect(file_marks.read_only_uri(root), uri=True)
            try:
                columns = {str(row[1]) for row in cast(list[tuple[object, ...]],
                           connection.execute("PRAGMA table_info(steps)").fetchall())}
                argv_column = "argv" if "argv" in columns else "NULL"
                compile_clause = " AND crates_compiled > 0" if "crates_compiled" in columns else ""
                rows = cast(list[tuple[int, str | None, str]], connection.execute(
                    f"SELECT peak_mem_bytes, {argv_column}, step FROM steps "
                    + "WHERE host = ? AND repo = ? AND step IN (?, 'check', 'build') "
                    + "AND started_at >= ? AND started_at < ? AND peak_mem_bytes > 0" + compile_clause,
                    (host, repository.legacy_name, step, cutoff, before),
                ).fetchall())
                for value, raw_argv, sample_step in rows:
                    arguments = history_arguments(raw_argv)
                    selection = test_selection(arguments)
                    sample_package = selection.package
                    if sample_step in ("check", "build"):
                        if (sample_package == package
                                or (not sample_package and package == repository.name)):
                            compile_index.append(int(value))
                    elif (sample_package == package and selection.scope == "filtered"
                          and (width is None or test_threads(arguments) == width)):
                        indexed.append(int(value))
                        if filters and selection.filters == filters:
                            indexed_exact.append(int(value))
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            pass
    for values in (indexed_exact, indexed):
        if values:
            return ExpectedPeak(int(percentile(values) * ANON_SHARE), "buildlog", len(values))
    if compile_peaks:
        return ExpectedPeak(percentile(compile_peaks), "measured", len(compile_peaks))
    if compile_index:
        return ExpectedPeak(int(percentile(compile_index) * ANON_SHARE),
                            "buildlog", len(compile_index))
    return ExpectedPeak(FILTERED_FLOOR, "fallback", 0)


def current_worktree_steps(root: Path, host: str, worktree: str,
                          now: datetime) -> list[dict[str, object]]:
    """Use a validated indexed prefix plus every newly appended recorder line.

    The index is only an acceleration: missing, replaced or incompatible data
    makes us read the canonical file instead. Partial or corrupt tails refuse
    freshness rather than hiding a more recent build.
    This reader holds one lock for os.fstat and file_marks.marks and rejects partial tails that whole_rows skips.
    """
    prefixes: dict[str, tuple[int, str]] = {}
    indexed: dict[str, list[dict[str, object]]] = {}
    try:
        with closing(sqlite3.connect(file_marks.read_only_uri(root), uri=True)) as connection:
            _ = connection.execute("BEGIN")
            prefixes = {path: (offset, marks) for path, offset, marks in cast(
                list[tuple[str, int, str]], connection.execute("SELECT path, offset, marks FROM files").fetchall())}
            columns = ("src", "started_at", "ended_at", "step", "argv", "test_build_key",
                       "status", "tests_run", "sweep_freed_bytes")
            cutoff = store.utc_iso((now - timedelta(days=1)).timestamp())
            result = connection.execute(
                "SELECT " + ", ".join(columns) + " FROM steps WHERE host = ? AND worktree = ? "
                + "AND (ended_at >= ? OR started_at >= ?)", (host, worktree, cutoff, cutoff))
            for values in cast(list[tuple[object, ...]], result.fetchall()):
                row: dict[str, object] = dict(zip(columns, values))
                indexed.setdefault(str(row["src"]), []).append(row)
    except sqlite3.Error:
        prefixes = {}
    rows: list[dict[str, object]] = []
    months = {now.strftime("%Y-%m"), (now - timedelta(days=1)).strftime("%Y-%m")}
    for month in sorted(months):
        path = root / host / f"{month}.jsonl"
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            continue
        with handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            source = path.relative_to(root).as_posix()
            offset, marks = prefixes.get(source, (0, ""))
            if (offset <= os.fstat(handle.fileno()).st_size
                    and marks == file_marks.marks(handle, offset)):
                rows.extend(indexed.get(source, []))
            else:
                offset = 0
            _ = handle.seek(offset)
            for line in handle:
                if not line.endswith(b"\n"):
                    raise ValueError("incomplete build-log tail")
                raw = cast(object, json.loads(line))
                if not isinstance(raw, dict):
                    raise ValueError("invalid build-log record")
                row = cast(dict[str, object], raw)
                if (row.get("kind") == "step" and row.get("host") == host
                        and row.get("worktree") == worktree):
                    rows.append(row)
    return rows


def is_fresh_test(root: Path, host: str, worktree: str, package: str,
                  scope: TestScope, tree_key: str, now: datetime,
                  output_roots: CargoOutputRoots | CargoOutputUnavailable) -> bool:
    """Only the last completed matching test build can prove the targets fresh.

    The key covers Cargo build inputs, not nextest's run-only selection. Read
    the recorder's current JSONL, since the derived index can lag a finished step.
    """
    if (not tree_key or not package or not worktree
            or isinstance(output_roots, CargoOutputUnavailable)):
        return False
    before = store.utc_iso(now.timestamp())
    try:
        rows = current_worktree_steps(root, host, worktree, now)
        rows.sort(key=lambda row: (str(row.get("ended_at") or ""),
                                   str(row.get("started_at") or "")), reverse=True)
        for row in rows:
            ended = str(row.get("ended_at") or "")
            if row.get("step") != "nextest" or not ended or ended >= before:
                continue
            arguments = history_arguments(row.get("argv"))
            if test_package(arguments) != package or test_scope(arguments) != scope:
                continue
            tests_run = row.get("tests_run")
            if row.get("test_build_key") != tree_key or not (
                    row.get("status") == 0 or isinstance(tests_run, int) and tests_run > 0):
                return False
            finished = epoch(ended)
            # Sweep deletion records are retained for one day. Older builds
            # cannot prove that their target survived an intervening sweep.
            if now.timestamp() - finished >= 24 * 60 * 60:
                return False
            targets = {output_roots.target_directory, output_roots.build_directory}
            if any(not target.is_dir() for target in targets):
                return False
            from scripts.lint import tmp_sweep

            state = tmp_sweep.state_dir()
            try:
                deleted = cast(object, json.loads((state / tmp_sweep.TARGET_DELETES_NAME).read_text()))
            except FileNotFoundError:
                deleted = {}
            if not isinstance(deleted, dict):
                return False
            for path, timestamp in cast(dict[str, object], deleted).items():
                if Path(path).resolve() in targets:
                    if (not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp)
                            or timestamp >= finished):
                        return False
            for sweep in rows:
                started = str(sweep.get("started_at") or "")
                freed = sweep.get("sweep_freed_bytes")
                if (sweep.get("step") == "sweep" and ended <= started < before
                        and (not isinstance(freed, (int, float)) or freed > 0)):
                    return False
            return True
    except (OSError, ValueError, TypeError, sqlite3.Error):
        pass
    return False


def fresh_test_peak(repository: RepositoryIdentity, step: str, host: str, now: datetime,
                    root: Path, width: int | None, scope: TestScope,
                    filters: tuple[str, ...], package: str,
                    compile_peak: ExpectedPeak) -> ExpectedPeak:
    """Use only completed zero-compile measurements of this package and scope."""
    cutoff = store.utc_iso((now - timedelta(days=HISTORY_DAYS)).timestamp())
    before = store.utc_iso(now.timestamp())
    try:
        with closing(sqlite3.connect(file_marks.read_only_uri(root), uri=True)) as connection:
            rows = cast(list[tuple[str | None, str, int | None, str]], connection.execute(
                "SELECT call_id, argv, peak_mem_bytes, repo FROM steps WHERE host = ? "
                + "AND step = ? AND started_at >= ? AND started_at < ? AND ended_at < ? "
                + "AND crates_compiled = 0 AND (status = 0 OR tests_run > 0)",
                (host, step, cutoff, before, before),
            ).fetchall())
    except (OSError, sqlite3.Error):
        return compile_peak
    calls: dict[str, bool] = {}
    indexed: list[int] = []
    indexed_exact: list[int] = []
    for call_id, raw_argv, peak, sample_repo in rows:
        arguments = history_arguments(raw_argv)
        if (test_package(arguments) != package or test_scope(arguments) != scope
                or (width is not None and test_threads(arguments) != width)):
            continue
        exact = bool(filters) and test_filters(arguments) == filters
        if call_id:
            calls[call_id] = exact
        if (repository.legacy_name and sample_repo == repository.legacy_name
                and isinstance(peak, int) and peak > 0):
            indexed.append(peak)
            if exact:
                indexed_exact.append(peak)
    measured: list[int] = []
    measured_exact: list[int] = []
    try:
        for item in anonymous_history_rows(root / "admission/anon_peaks.jsonl"):
            try:
                call_id = item.get("call_id")
                own = (item.get("repo_identity") == repository.key
                       or (not item.get("repo_identity") and repository.legacy_name
                           and item.get("repo") == repository.legacy_name))
                if (not own or item.get("host") != host or item.get("step") != step
                        or not cutoff <= str(item.get("ended_at", "")) < before
                        or not isinstance(call_id, str) or call_id not in calls
                        or item.get("test_scope", scope) != scope
                        or item.get("package", package) != package
                        or (width is not None and item.get("test_threads") != width)):
                    continue
                peak = item.get("anon_peak_bytes")
                if isinstance(peak, int) and peak > 0:
                    measured.append(peak)
                    if calls[call_id]:
                        measured_exact.append(peak)
            except (ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    for values, source in ((measured_exact, "measured"), (indexed_exact, "buildlog"),
                           (measured, "measured"), (indexed, "buildlog")):
        if values:
            peak = percentile(values)
            if source == "buildlog":
                peak = int(peak * ANON_SHARE)
            return ExpectedPeak(max(FILTERED_FLOOR, peak),
                                cast(Literal["measured", "buildlog"], source), len(values))
    return compile_peak

GENERIC_REPO_NAMES = frozenset({"", "repo", "c", "clone", "scratch", "tmp", "repository"})


def git_identity(argv: list[str], cwd: str) -> tuple[Repository, str]:
    if not argv:
        return UnknownRepository(), ""
    directory = record.git_directory(argv, cwd)
    facts = record.git_facts(directory)
    worktree = Path(facts["worktree"] or cwd).name
    key = repository.repository_key(directory)
    if key == store.UNKNOWN_IDENTITY:
        return UnknownRepository(), worktree
    checkout_name = Path(facts["repo_path"] or cwd).name
    if key.startswith("git-root:"):
        return RepositoryIdentity(key, checkout_name), worktree
    name = key.rsplit("/", 1)[-1]
    legacy = name if checkout_name == name and name not in GENERIC_REPO_NAMES else ""
    return RepositoryIdentity(key, name, legacy), worktree


@dataclass(frozen=True)
class ReusableEstimate:
    pass


@dataclass(frozen=True)
class StepEstimate:
    profile: StepProfile | ProgramProfile
    cache: FreshTest | ReusableEstimate


def no_build_history_generation() -> NoBuildHistoryGeneration:
    return NoBuildHistoryGeneration()


@dataclass(frozen=True)
class RepositoryLocation:
    repository: Repository
    worktree: str


@dataclass(frozen=True)
class ReadRepositoryAtUse:
    pass


READ_REPOSITORY_AT_USE = ReadRepositoryAtUse()


def step_estimate(argv: list[str], cwd: str, profile: InferFromArgv | StepProfile | ProgramProfile,
                  now: datetime | Callable[[], datetime], root: Path, policy: AdmissionPolicy, *,
                  first_peak_bytes: int,
                  build_history_generation: Callable[[], BuildHistoryGeneration | NoBuildHistoryGeneration] = no_build_history_generation,
                  location: RepositoryLocation | ReadRepositoryAtUse = READ_REPOSITORY_AT_USE
                  ) -> StepEstimate:
    """Resolve one estimate before admission connects."""
    estimate_time = now() if callable(now) else now
    fresh = False
    package, filters = test_package(argv), test_filters(argv)
    if isinstance(profile, ProgramProfile):
        sizing = profile.sizing
        if not sizing.estimated_at or estimate_time.timestamp() - sizing.estimated_at >= policy.timing.estimate_refresh_seconds:
            profile = replace(profile, sizing=replace(sizing,
                estimate=program_peak_estimate(profile.program, store.host_name(), estimate_time, root,
                                               sizing.first_peak_bytes),
                estimated_at=estimate_time.timestamp()))
    elif isinstance(profile, StepProfile) and profile.estimated_at and estimate_time.timestamp() - profile.estimated_at >= policy.timing.estimate_refresh_seconds:
        profile = InferFromArgv()
    if isinstance(profile, InferFromArgv):
        repository, worktree = (git_identity(argv, cwd) if isinstance(location, ReadRepositoryAtUse)
                                else (location.repository, location.worktree))
        repo = repository.name if isinstance(repository, RepositoryIdentity) else ""
        scope = test_scope(argv)
        step = parse.step_name(argv) if argv else "build"
        host = store.host_name()
        worktree_path = ""
        if step == "nextest" and package and os.environ.get("BUILDLOG_TEST_BUILD_KEY"):
            directory = record.git_directory(argv, cwd)
            worktree_path = record.git_facts(directory)["worktree"] or ""
        target_dir = os.environ.get("BUILDLOG_TEST_TARGET_DIR", "")
        build_dir = os.environ.get("BUILDLOG_TEST_BUILD_DIR", "")
        output_roots = (CargoOutputRoots(Path(worktree_path).resolve(), Path(target_dir).resolve(),
                                        Path(build_dir).resolve())
                        if Path(target_dir).is_absolute() and Path(build_dir).is_absolute()
                        else CargoOutputUnavailable("verify did not save both output roots"))
        fresh = (step == "nextest" and is_fresh_test(
            root, host, worktree_path, package, scope,
            os.environ.get("BUILDLOG_TEST_BUILD_KEY", ""), estimate_time, output_roots))
        generation = build_history_generation() if step == "build" else NoBuildHistoryGeneration()
        if step == "build" and callable(now):
            estimate_time = now()
        peak = expected_peak(repository, step, host, estimate_time, root, scope=scope,
                             package=package, filters=filters, fresh=fresh,
                             shape=build_shape(argv) if step == "build" else NoBuildShape(),
                             first_peak_bytes=first_peak_bytes)
        profile = StepProfile(repo, step, worktree, peak, repository, scope, estimate_time.timestamp(),
                              generation)
    elif isinstance(profile, StepProfile) and not profile.estimated_at:
        profile = replace(profile, estimated_at=estimate_time.timestamp())
    return StepEstimate(profile, FreshTest() if fresh else ReusableEstimate())


def epoch(instant: str) -> float:
    try:
        return datetime.fromisoformat(instant.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
