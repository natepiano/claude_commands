"""Scan transcripts once, then report screenshot episode times from saved data."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import json
import os
import shlex
import statistics
import subprocess
import tempfile
import time
from collections.abc import Iterable
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, cast
from zoneinfo import ZoneInfo

if __package__:
    from .changes import Change, read_changes
    from .episodes import (Episode, LegacyEvidenceUnavailable, NoObservablePath, NoneCited,
                           OneCitedShot, SeveralCitedShots, SURVEY_SOURCES, read_episodes,
                           split_episodes, write_episodes)
    from .transcripts import (AttemptCountInferredFromImages, AvailableTimingSource,
                              CODEX_PREFILTER, PREFILTER, RELATED_WRITE, ExactOrderedCaptureAttempts, PersistentScanCache,
                              UnavailableTimingSource, scan_calls, survey_counts)
else:
    from changes import Change, read_changes  # pyright: ignore[reportImplicitRelativeImport]
    from episodes import (  # pyright: ignore[reportImplicitRelativeImport]
        Episode, LegacyEvidenceUnavailable, NoObservablePath, NoneCited, OneCitedShot,
        SeveralCitedShots, SURVEY_SOURCES, read_episodes, split_episodes, write_episodes,
    )
    from transcripts import (  # pyright: ignore[reportImplicitRelativeImport]
        AttemptCountInferredFromImages, AvailableTimingSource, CODEX_PREFILTER, PREFILTER, RELATED_WRITE,
        ExactOrderedCaptureAttempts,
        PersistentScanCache, UnavailableTimingSource, scan_calls, survey_counts,
    )

PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_DIR = Path.home() / ".local/state/screenshot-analysis"
CLAUDE_ROOT = Path.home() / ".claude/projects"
CODEX_ROOT = Path.home() / ".codex/sessions"
TIMINGS_PATH = Path.home() / ".cache/hana-shot/timings.jsonl"
NO_TIMING_SOURCE = UnavailableTimingSource()
FAILURE_REASONS = (
    "timeout", "already_in_progress", "black_capture", "empty_crop", "no_app",
    "invalid_request", "no_target", "invalid_png", "copy_failed", "brp_error", "shot_failed",
)

# Remote Python receives prior file identities and returns only candidate bytes
# not yet received. Filter literals are filled from transcripts.py below.
MAC_READER = r'''
import base64, itertools, json, pathlib, re, sys, time
prior = json.load(sys.stdin)
deadline = time.monotonic() + __BUDGET_SECONDS__
claude_hints = tuple(term.encode() for term in __CLAUDE_HINTS__)
codex_hints = tuple(term.encode() for term in __CODEX_HINTS__)
related_write = re.compile(__RELATED_WRITE__.encode())
home = pathlib.Path.home()
codex = home / '.codex/sessions'
claude = home / '.claude/projects'
roots = [('codex', codex)]
claude_files = list(itertools.islice(claude.rglob('*.jsonl'), 101)) if claude.is_dir() else []
include_claude = len(claude_files) <= 100 and sum(p.stat().st_size for p in claude_files) <= 100_000_000
print(json.dumps({'kind': 'claude_coverage', 'included': include_claude and bool(claude_files)}))
if include_claude:
    roots.append(('claude', claude))
finished = True

def candidate(path, source):
    hints = codex_hints if source == 'codex' else claude_hints
    with path.open('rb') as stream:
        for line in stream:
            if any(hint in line for hint in hints) or source == 'codex' and related_write.search(line):
                return True
            if time.monotonic() >= deadline:
                return None
    return False

def transfer(path, key, source):
    old = prior.get(key, {})
    stat = path.stat()
    same = old.get('inode') == stat.st_ino and old.get('size') == stat.st_size and old.get('mtime') == stat.st_mtime_ns
    if same and not old.get('matched'):
        matched = False
        start = 0
    elif (old.get('matched') and old.get('inode') == stat.st_ino
          and stat.st_size >= old.get('cursor', 0)):
        matched = True
        start = old.get('cursor', 0)
    else:
        matched = True if source == 'timings' else candidate(path, source)
        if matched is None:
            return False
        start = 0
    print(json.dumps({'kind': 'file', 'key': key, 'size': stat.st_size, 'inode': stat.st_ino,
                      'mtime': stat.st_mtime_ns, 'matched': matched, 'start': start}))
    if matched and (not same or start < stat.st_size):
        with path.open('rb') as stream:
            stream.seek(start)
            while data := stream.read(65536):
                print(json.dumps({'kind': 'chunk', 'key': key, 'data': base64.b64encode(data).decode('ascii')}))
                if time.monotonic() >= deadline:
                    return False
    return True

for source, root in roots:
    if not root.is_dir():
        continue
    for path in sorted(root.rglob('*.jsonl')):
        if time.monotonic() >= deadline:
            finished = False
            break
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        key = source + '/' + rel
        if not transfer(path, key, source):
            finished = False
            break
    if not finished:
        break
path = home / '.cache/hana-shot/timings.jsonl'
if finished and path.is_file():
    if time.monotonic() >= deadline or not transfer(path, 'timings/timings.jsonl', 'timings'):
        finished = False
print(json.dumps({'kind': 'end', 'finished': finished}))
'''


def _mac_reader_source(budget_seconds: int = 80) -> str:
    return (MAC_READER.replace("__BUDGET_SECONDS__", str(budget_seconds))
            .replace("__CLAUDE_HINTS__", repr(PREFILTER))
            .replace("__CODEX_HINTS__", repr(CODEX_PREFILTER))
            .replace("__RELATED_WRITE__", repr(RELATED_WRITE)))


@dataclass(frozen=True)
class MacReadFinished:
    pass


@dataclass(frozen=True)
class MacReadCatchingUp:
    pass


MacReadProgress = MacReadFinished | MacReadCatchingUp


@dataclass(frozen=True)
class MacEvidenceAvailable:
    claude_root: Path
    codex_root: Path
    timings_path: Path
    bytes_read: int
    claude_included: bool
    started_at: datetime
    progress: MacReadProgress


@dataclass(frozen=True)
class MacEvidenceUnavailable:
    reason: str


MacEvidence = MacEvidenceAvailable | MacEvidenceUnavailable


@dataclass
class InProgressMacFileTransfer:
    size: int
    start: int
    inode: int
    modified_ns: int
    matched: bool
    received: int = 0


@dataclass(frozen=True)
class HostCoveredThrough:
    at: datetime


@dataclass(frozen=True)
class HostNeverCovered:
    pass


HostCoverage = HostCoveredThrough | HostNeverCovered


def _mac_evidence(state_dir: Path) -> MacEvidence:
    """Commit complete SSH responses, including a valid budget-limited batch."""
    started_at = _utc_now()
    mirror = state_dir / "mac-source"
    manifest_path = state_dir / "mac_manifest.json"
    prior: dict[str, object] = {}
    for key, value in _read_object(manifest_path).items():
        if isinstance(value, dict):
            record = cast(dict[str, object], value)
            if record.get("matched") is False or (mirror / key).exists():
                prior[key] = record
    command = (f"python3 -c {shlex.quote(_mac_reader_source())}; rc=$?; "
               'printf "rc=%s\\n" "$rc" >&2; exit "$rc"')
    state_dir.mkdir(parents=True, exist_ok=True)
    descriptor, output_name = tempfile.mkstemp(prefix=".mac-read.", dir=state_dir)
    os.close(descriptor)
    output_path = Path(output_name)
    try:
        with output_path.open("wb") as output:
            try:
                result = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "mac", command],
                    input=json.dumps(prior).encode(), stdout=output, stderr=subprocess.PIPE,
                    timeout=120, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                return MacEvidenceUnavailable(str(error))
        if result.returncode != 0 or f"rc={result.returncode}".encode() not in result.stderr:
            return MacEvidenceUnavailable(result.stderr.decode(errors="replace").strip() or "Mac SSH failed")
        announced: dict[str, InProgressMacFileTransfer] = {}
        included = False
        progress: MacReadProgress | None = None
        # Validate the whole transfer before changing the mirror or cursor.
        with output_path.open(encoding="utf-8") as transfer:
            for line in transfer:
                if progress is not None:
                    raise ValueError("Mac transfer has data after its end marker")
                row = cast(object, json.loads(line))
                if not isinstance(row, dict):
                    raise ValueError("invalid Mac transfer line")
                item = cast(dict[str, object], row)
                if item.get("kind") == "claude_coverage":
                    included = item.get("included") is True
                elif item.get("kind") == "file":
                    key = str(item["key"])
                    parts = Path(key).parts
                    if not parts or parts[0] not in ("codex", "claude", "timings") or ".." in parts or key in announced:
                        raise ValueError("invalid Mac source path")
                    size, start = int(str(item["size"])), int(str(item["start"]))
                    matched = item.get("matched")
                    if size < 0 or start < 0 or start > size or not isinstance(matched, bool):
                        raise ValueError("invalid Mac file cursor")
                    announced[key] = InProgressMacFileTransfer(
                        size, start, int(str(item["inode"])), int(str(item["mtime"])), matched,
                    )
                elif item.get("kind") == "chunk":
                    key = str(item["key"])
                    if key not in announced or not announced[key].matched:
                        raise ValueError("Mac chunk without file")
                    announced[key].received += len(base64.b64decode(str(item["data"]), validate=True))
                elif item.get("kind") == "end" and isinstance(item.get("finished"), bool):
                    progress = MacReadFinished() if item["finished"] else MacReadCatchingUp()
                else:
                    raise ValueError("invalid Mac transfer kind")
        if progress is None:
            raise ValueError("Mac transfer lacks an end marker")
        current = {} if isinstance(progress, MacReadFinished) else dict(prior)
        for key, file in announced.items():
            if not file.matched and file.received or (file.matched and file.size > file.start and
                                                      (file.received == 0 or isinstance(progress, MacReadFinished)
                                                       and file.received < file.size - file.start)):
                raise ValueError("incomplete Mac source transfer")
            current[key] = {"size": file.size, "inode": file.inode, "mtime": file.modified_ns,
                            "matched": file.matched, "cursor": file.start + file.received if file.matched else 0}
        bytes_read = sum(file.received for file in announced.values())
        open_files: dict[str, BinaryIO] = {}
        try:
            with output_path.open(encoding="utf-8") as transfer:
                for line in transfer:
                    item = cast(dict[str, object], json.loads(line))
                    if item["kind"] != "chunk":
                        continue
                    key = str(item["key"])
                    target = mirror / key
                    if key not in open_files:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        start = announced[key].start
                        stream = target.open("r+b" if start else "wb")
                        if start:
                            _ = stream.seek(start)
                            _ = stream.truncate()
                        open_files[key] = stream
                    _ = open_files[key].write(base64.b64decode(str(item["data"])))
        finally:
            for stream in open_files.values():
                stream.close()
        for key in prior:
            if key not in current or key in announced and not announced[key].matched:
                _ = (mirror / key).unlink(missing_ok=True)
        _save_object(manifest_path, current)
        return MacEvidenceAvailable(mirror / ("claude" if included else "claude-unavailable"),
                                    mirror / "codex", mirror / "timings/timings.jsonl",
                                    bytes_read, included, started_at, progress)
    except (ValueError, KeyError, OSError, binascii.Error) as error:
        return MacEvidenceUnavailable(str(error))
    finally:
        _ = output_path.unlink(missing_ok=True)


def _pacific(value: datetime) -> str:
    return value.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M %Z (%z)")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _save_object(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    _ = temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _ = temporary.replace(path)


def _window_stamp(value: str) -> datetime:
    if "T" not in value:
        return datetime.combine(datetime.fromisoformat(value).date(), clock_time(), PACIFIC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=PACIFIC)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * percentile
    low = int(index)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low)


def _aligned_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max([len(header), *(len(row[index]) for row in rows)]) for index, header in enumerate(headers)]
    return [
        "  ".join(value.ljust(width) for value, width in zip(row, widths))
        for row in [headers, *rows]
    ]


def _episode_stats(group: list[Episode]) -> list[str]:
    if not group:
        return ["—"] * 7
    duration = [episode.minutes for episode in group]
    shots = [float(episode.screenshot_count) for episode in group]
    other = [float(episode.other_call_count) for episode in group]
    return [
        str(len(group)), f"{sum(duration)/60:.2f}", f"{statistics.median(duration):.1f}",
        f"{_percentile(duration, .75):.1f}", f"{_percentile(duration, .90):.1f}",
        f"{statistics.median(shots):.1f}", f"{statistics.median(other):.1f}",
    ]


def _report_rows(episodes: Iterable[Episode]) -> list[str]:
    groups: dict[tuple[str, str, str], dict[int, list[Episode]]] = defaultdict(lambda: defaultdict(list))
    for episode in episodes:
        groups[(episode.method, episode.agent, episode.project)][episode.split].append(episode)
    metrics = ("n", "total_h", "median_min", "p75_min", "p90_min", "shots_per_ep", "other_calls_per_ep")
    headers = ["method", "agent", "project", *(f"{metric}_{gap}" for metric in metrics for gap in ("5m", "15m"))]
    order = sorted(
        groups,
        key=lambda key: (key[0], key[1], -sum(episode.minutes for episode in groups[key].get(300, [])), key[2]),
    )
    rows: list[list[str]] = []
    for method, agent, project in order:
        five = _episode_stats(groups[(method, agent, project)].get(300, []))
        fifteen = _episode_stats(groups[(method, agent, project)].get(900, []))
        rows.append([method, agent, project, *(value for pair in zip(five, fifteen) for value in pair)])
    return _aligned_table(headers, rows)


def _image_rows(episodes: Iterable[Episode]) -> list[str]:
    groups: dict[tuple[str, str], list[Episode]] = defaultdict(list)
    for episode in episodes:
        if episode.split == 300:
            groups[(episode.method, episode.agent)].append(episode)
    headers = ["method", "agent", "median_min_per_image", "p90_min_per_image", "n_images", "hana_shot_calls"]
    rows: list[list[str]] = []
    for (method, agent), group in sorted(groups.items()):
        per_image = [
            episode.minutes / episode.image_count
            for episode in group if episode.image_count > 0
            for _ in range(episode.image_count)
        ]
        rows.append([
            method, agent,
            f"{statistics.median(per_image):.1f}" if per_image else "—",
            f"{_percentile(per_image, .90):.1f}" if per_image else "—",
            str(len(per_image)), str(sum(episode.hana_shot_call_count for episode in group)),
        ])
    return _aligned_table(headers, rows)


def _kept_rows(episodes: Iterable[Episode]) -> list[str]:
    groups: dict[tuple[int, str, str, str], list[Episode]] = defaultdict(list)
    for episode in episodes:
        groups[(episode.split, episode.method, episode.agent, episode.project)].append(episode)
    headers = ["split_s", "method", "agent", "project", "n_episodes", "exact_ordered_n", "inferred_images_n",
               "cited", "several_cited", "none_cited", "no_observable_path", "legacy_unassessed",
               "exact_first_kept_n", "exact_median_attempts_before", "exact_p90_attempts_before",
               "inferred_first_kept_n", "inferred_median_attempts_before", "inferred_p90_attempts_before"]
    rows: list[list[str]] = []
    for (split, method, agent, project), group in sorted(groups.items()):
        exact = [float(episode.kept_shot.attempts_before_first_kept_shot) for episode in group
                 if isinstance(episode.kept_shot, (OneCitedShot, SeveralCitedShots))
                 and isinstance(episode.attempt_count_evidence, ExactOrderedCaptureAttempts)]
        inferred = [float(episode.kept_shot.attempts_before_first_kept_shot) for episode in group
                    if isinstance(episode.kept_shot, (OneCitedShot, SeveralCitedShots))
                    and isinstance(episode.attempt_count_evidence, AttemptCountInferredFromImages)]
        rows.append([
            str(split), method, agent, project, str(len(group)),
            str(sum(isinstance(episode.attempt_count_evidence, ExactOrderedCaptureAttempts) for episode in group)),
            str(sum(isinstance(episode.attempt_count_evidence, AttemptCountInferredFromImages) for episode in group)),
            str(sum(isinstance(episode.kept_shot, OneCitedShot) for episode in group)),
            str(sum(isinstance(episode.kept_shot, SeveralCitedShots) for episode in group)),
            str(sum(isinstance(episode.kept_shot, NoneCited) for episode in group)),
            str(sum(isinstance(episode.kept_shot, NoObservablePath) for episode in group)),
            str(sum(isinstance(episode.kept_shot, LegacyEvidenceUnavailable) for episode in group)),
            str(len(exact)),
            f"{statistics.median(exact):.1f}" if exact else "—",
            f"{_percentile(exact, .90):.1f}" if exact else "—",
            str(len(inferred)),
            f"{statistics.median(inferred):.1f}" if inferred else "—",
            f"{_percentile(inferred, .90):.1f}" if inferred else "—",
        ])
    return _aligned_table(headers, rows)


def _by_hand_sessions(episodes: Iterable[Episode]) -> list[str]:
    latest: dict[tuple[str, str, str], datetime] = {}
    for episode in episodes:
        if episode.split != 300 or episode.method != "by hand":
            continue
        key = (episode.agent, episode.project, episode.session_id)
        latest[key] = max(latest.get(key, episode.end), episode.end)
    ordered = sorted(latest, key=lambda key: latest[key], reverse=True)
    return [f"By-hand sessions: {len(ordered)}; newest 20 shown (5-min split)", *(
        f"{agent} {project} {session}" for agent, project, session in ordered[:20]
    )]


def _weekly_trend(episodes: Iterable[Episode]) -> list[str]:
    weeks: dict[datetime, dict[str, list[Episode]]] = defaultdict(lambda: defaultdict(list))
    for episode in episodes:
        if episode.split != 300:
            continue
        local = episode.start.astimezone(PACIFIC)
        monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weeks[monday][episode.method].append(episode)
    rows: list[list[str]] = []
    for week, methods in sorted(weeks.items()):
        by_hand, hana = methods["by hand"], methods["/hana_shot"]
        rows.append([_pacific(week), str(len(by_hand)), f"{sum(item.minutes for item in by_hand)/60:.2f}",
                     str(len(hana)), f"{sum(item.minutes for item in hana)/60:.2f}"])
    return ["Weekly agent-hours, 5-min split; weeks start Monday in Pacific time; n counts episodes",
            *_aligned_table(["week_start", "by_hand_n", "by_hand_h", "hana_shot_n", "hana_shot_h"], rows)]


def _window_stats(episodes: list[Episode]) -> str:
    if not episodes:
        return "n=0 median_min=— p90_min=—; too small to judge"
    minutes = [episode.minutes for episode in episodes]
    small = "; too small to judge" if len(episodes) < 20 else ""
    return (f"n={len(episodes)} median_min={statistics.median(minutes):.1f} "
            f"p90_min={_percentile(minutes, .90):.1f}{small}")


def _host_coverage(state_dir: Path) -> dict[str, HostCoveredThrough]:
    raw = _read_object(state_dir / "scan_status.json").get("host_last_success")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, HostCoveredThrough] = {}
    for host, stamp in cast(dict[object, object], raw).items():
        if isinstance(host, str) and isinstance(stamp, str):
            try:
                result[host] = HostCoveredThrough(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
            except ValueError:
                continue
    return result


def _coverage_incomplete(host: str, end: datetime, coverage: dict[str, HostCoveredThrough]) -> bool:
    state: HostCoverage = coverage.get(host, HostNeverCovered())
    return isinstance(state, HostNeverCovered) or state.at < end


def _change_rows(episodes: list[Episode], changes: list[Change], state_dir: Path) -> list[str]:
    products = sorted((change for change in changes if not change.measurement_change),
                      key=lambda change: (change.effective_at, change.repository, change.commit))
    coverage = _host_coverage(state_dir)
    lines = ["By change, product changes only; each host window ends at its next product change",
             "repository commit method split side host window n median_min p90_min coverage"]
    if not episodes:
        return [*lines, "No episodes in the selected window."]
    first = min(episode.start for episode in episodes)
    last = max(episode.end for episode in episodes) + timedelta(microseconds=1)
    for change in products:
        for host in change.host_coverage:
            earlier = [other.effective_at for other in products
                       if other.effective_at < change.effective_at and host in other.host_coverage]
            later = [other.effective_at for other in products
                     if other.effective_at > change.effective_at and host in other.host_coverage]
            before = max(first, max(earlier, default=first))
            after = min(last, min(later, default=last))
            for side, start, end in (("before", before, min(last, change.effective_at)),
                                     ("after", max(first, change.effective_at), after)):
                if end <= start:
                    continue
                label = f"incomplete:{host}" if _coverage_incomplete(host, end, coverage) else "complete"
                window = f"{_pacific(start)} to {_pacific(end)}"
                for method in ("by hand", "/hana_shot"):
                    for split in (300, 900):
                        group = [episode for episode in episodes if episode.method == method
                                 and episode.split == split and (episode.source_host == host or
                                                                 episode.source_host == "local" and host == "natedev")
                                 and start <= episode.start < end]
                        lines.append(f"{change.repository} {change.commit} {method} {split}s {side} host={host} " +
                                     f"[{window}] {_window_stats(group)}; {label}")
    for change in changes:
        if change.measurement_change:
            lines.append(f"Measurement change {change.repository} {change.commit}: {change.summary}; " +
                         "excluded from speed comparisons")
    return lines


def _saved_invocations(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    result: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            raw = cast(object, json.loads(line))
        except ValueError:
            continue
        if isinstance(raw, dict):
            result.append(cast(dict[str, object], raw))
    return result


def _invocation_rows(records: list[dict[str, object]], start: datetime | None,
                     end: datetime | None, start_label: str, end_label: str) -> list[str]:
    selected: list[dict[str, object]] = []
    for record in records:
        try:
            stamp = datetime.fromisoformat(str(record["time"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if (start is None or stamp >= start) and (end is None or stamp < end):
            selected.append(record)
    lines = [f"/hana_shot invocations, {start_label} to {end_label}; n counts records, including failures without transcript results"]
    for kind in ("shot", "views_check", "unknown"):
        group = [record for record in selected if record.get("invocation_kind", "unknown") == kind]
        success = sum(record.get("status") == "success" for record in group)
        failure = sum(record.get("status") == "failure" for record in group)
        legacy = sum(record.get("status") is None for record in group)
        lines.append(f"kind={kind} n={len(group)} success={success} failure={failure} legacy_success={legacy}")
    for reason in FAILURE_REASONS:
        count = 0
        for record in selected:
            raw_attempts = record.get("attempts")
            failed_attempts: list[dict[str, object]] = []
            if isinstance(raw_attempts, list):
                for item in cast(list[object], raw_attempts):
                    if isinstance(item, dict):
                        attempt = cast(dict[str, object], item)
                        if attempt.get("status") == "failure":
                            failed_attempts.append(attempt)
            count += sum(attempt.get("failure_reason") == reason for attempt in failed_attempts)
            if record.get("status") == "failure" and record.get("failure_reason") == reason and not failed_attempts:
                count += 1
        lines.append(f"failure_reason={reason} n={count}")
    phases = ("resolve_ms", "move_ms", "settle_ms", "frame_ms", "capture_ms", "crop_ms", "total_ms")
    groups: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for record in selected:
        if record.get("status") is None:
            successful_attempts: list[object] = [record]
        elif isinstance(record.get("attempts"), list):
            successful_attempts = cast(list[object], record["attempts"])
        else:
            successful_attempts = []
        for item in successful_attempts:
            if not isinstance(item, dict):
                continue
            attempt = cast(dict[str, object], item)
            if attempt.get("status", "success") != "success":
                continue
            groups[(str(record.get("invocation_kind", "unknown")), str(attempt.get("mode", "unknown")),
                    str(attempt.get("crop", "unknown")))].append(attempt)
    lines.append("Successful attempts by kind, mode and crop; each median has its own n; milliseconds")
    for (kind, mode, crop), attempts in sorted(groups.items()):
        values: list[str] = []
        for phase in phases:
            samples = [float(value) for attempt in attempts if isinstance(value := attempt.get(phase), int | float)]
            values.append(f"{phase}={statistics.median(samples):.1f}(n={len(samples)})" if samples
                          else f"{phase}=—(n=0)")
        lines.append(f"kind={kind} mode={mode} crop={crop} n={len(attempts)} " + " ".join(values))
    return lines


def report(state_dir: Path, since: str = "", until: str = "", project: str = "") -> str:
    records = read_episodes(state_dir / "episodes.jsonl")
    start = _window_stamp(since).astimezone(timezone.utc) if since else None
    end = _window_stamp(until).astimezone(timezone.utc) if until else None
    selected = [
        episode for episode in records
        if (start is None or episode.start >= start)
        and (end is None or episode.start < end)
        and (not project or episode.project == project)
    ]
    start_label = _pacific(start) if start else "all available"
    end_label = _pacific(end) if end else "latest saved"
    lines = [f"Window: {start_label} to {end_label} (America/Los_Angeles); n counts episodes; duration in hours and minutes"]
    status = _read_object(state_dir / "scan_status.json")
    last_scan = status.get("last_success")
    if isinstance(last_scan, str):
        lines.append(f"Last successful scan: {_pacific(datetime.fromisoformat(last_scan.replace('Z', '+00:00')))}")
    else:
        lines.append("Last successful scan: never")
    for host, coverage in sorted(_host_coverage(state_dir).items()):
        lines.append(f"{host} evidence covered through {_pacific(coverage.at)}")
    lines.append(f"Mac Claude transcripts: {status.get('mac_claude', 'out')}")
    lines.extend(_report_rows(selected))
    lines.append("Kept-shot evidence by split; attempt medians and p90 separate exact ordered captures from counts inferred from images, each with n")
    lines.extend(_kept_rows(selected))
    lines.append("Minutes per image, 5-min split; n_images counts images")
    lines.extend(_image_rows(selected))
    hana = [episode for episode in selected if episode.split == 300 and episode.method == "/hana_shot"]
    lines.append(f"/hana_shot, 5-min split: {sum(episode.hana_shot_call_count for episode in hana)} calls, {sum(episode.image_count for episode in hana)} images")
    lines.extend(_by_hand_sessions(selected))
    lines.append("By-source counts include episodes with more than one screenshot source.")
    for split in (300, 900):
        subset = [episode for episode in selected if episode.split == split and episode.method == "by hand"]
        for source in ("mcp_brp", "bash_brp", "grim", "spectacle", "screencapture", "import", "browser"):
            matches = [episode for episode in subset if source in episode.source]
            if matches:
                lines.append(f"source split={split} source={source} n={len(matches)} total_h={sum(episode.minutes for episode in matches)/60:.2f}")
        lines.append(f"all by hand split={split} n={len(subset)} total_h={sum(episode.minutes for episode in subset)/60:.2f}")
    lines.extend(_weekly_trend(selected))
    lines.extend(_change_rows(selected, read_changes(state_dir / "changes.json"), state_dir))
    lines.extend(_invocation_rows(_saved_invocations(state_dir / "invocations.jsonl"), start, end,
                                  start_label, end_label))
    return "\n".join(lines)


def survey(state_dir: Path) -> str:
    path = state_dir / "survey.json"
    if not path.exists():
        return "No survey saved; run scan first."
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        return "No survey saved; run scan first."
    counts = cast(dict[str, object], raw)
    lines = ["source calls sessions projects counted_as_by_hand"]
    for source in sorted(counts):
        values = counts[source]
        if not isinstance(values, list):
            continue
        value_list = cast(list[object], values)
        if len(value_list) != 3:
            continue
        calls, sessions, projects = (int(str(value)) for value in value_list)
        counted = source not in SURVEY_SOURCES or calls > 10
        lines.append(f"{source} {calls} {sessions} {projects} {'yes' if counted else 'no'}")
    return "\n".join(lines)


def scan(state_dir: Path, claude_root: Path, codex_root: Path,
         timing_source: AvailableTimingSource | UnavailableTimingSource = NO_TIMING_SOURCE,
         include_mac: bool = False) -> str:
    started = time.monotonic()
    local_read_started = _utc_now()
    previous_status = _read_object(state_dir / "scan_status.json")
    local = scan_calls(claude_root, codex_root, timing_source,
                       PersistentScanCache(state_dir / "scan-cache.pickle"), source_host="natedev")
    calls = list(local)
    invocations = list(local.invocations)
    candidate_count = local.candidate_file_count
    bytes_read = local.bytes_read
    mac_result: MacEvidence = MacEvidenceUnavailable("Mac scan not requested")
    if include_mac:
        mac_result = _mac_evidence(state_dir)
        mirror = state_dir / "mac-source"
        mac_claude_included = (mac_result.claude_included if isinstance(mac_result, MacEvidenceAvailable)
                               else previous_status.get("mac_claude") == "included")
        mac_claude_root = mirror / ("claude" if mac_claude_included else "claude-unavailable")
        mac_codex_root = mirror / "codex"
        mac_timings_path = mirror / "timings/timings.jsonl"
        if mac_claude_root.exists() or mac_codex_root.exists() or mac_timings_path.exists():
            remote_source: AvailableTimingSource | UnavailableTimingSource = (
                AvailableTimingSource(mac_timings_path, "mac") if mac_timings_path.exists()
                else UnavailableTimingSource()
            )
            remote = scan_calls(mac_claude_root, mac_codex_root, remote_source,
                                PersistentScanCache(state_dir / "scan-cache-mac.pickle"), source_host="mac")
            calls.extend(remote)
            invocations.extend(remote.invocations)
            candidate_count += remote.candidate_file_count
            bytes_read += remote.bytes_read
    counts = survey_counts(calls)
    counted = {"mcp_brp", "bash_brp", "hana_shot"}
    counted.update(source for source in SURVEY_SOURCES if counts.get(source, (0, 0, 0))[0] > 10)
    episodes = [*split_episodes(calls, 300, counted), *split_episodes(calls, 900, counted)]
    seed_calls = [
        call for call in calls
        if call.agent == "Claude" and call.source in {"mcp_brp", "hana_shot", "brp"}
    ]
    seed_episodes = split_episodes(seed_calls, 300, {"mcp_brp", "hana_shot"})
    window_start = datetime.fromisoformat("2026-09-09T07:00:00+00:00")
    rollout = datetime.fromisoformat("2026-10-06T16:35:00+00:00")
    baseline = [episode for episode in seed_episodes if episode.method == "by hand" and window_start <= episode.start < rollout]
    full = [episode for episode in episodes if episode.split == 300 and episode.method == "by hand" and window_start <= episode.start < rollout]
    write_episodes(state_dir / "episodes.jsonl", episodes)
    _ = (state_dir / "survey.json").write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _ = read_changes(state_dir / "changes.json")
    with (state_dir / "invocations.jsonl").open("w", encoding="utf-8") as output:
        for invocation in invocations:
            record = {**invocation.raw, "source_host": invocation.source_host,
                      "invocation_kind": invocation.invocation_kind}
            _ = output.write(json.dumps(record, sort_keys=True) + "\n")
    now = _utc_now().isoformat()
    raw_hosts = previous_status.get("host_last_success")
    hosts = cast(dict[str, object], raw_hosts) if isinstance(raw_hosts, dict) else {}
    hosts["natedev"] = local_read_started.isoformat()
    if isinstance(mac_result, MacEvidenceAvailable) and isinstance(mac_result.progress, MacReadFinished):
        hosts["mac"] = mac_result.started_at.isoformat()
    scan_status: dict[str, object] = {
        "last_success": now, "host_last_success": hosts, "mac_claude": (
            ("included" if mac_result.claude_included else "out") if isinstance(mac_result, MacEvidenceAvailable)
            else previous_status.get("mac_claude", "out")
        ),
    }
    if isinstance(previous_status.get("last_hour_utc"), str):
        scan_status["last_hour_utc"] = previous_status["last_hour_utc"]
    _save_object(state_dir / "scan_status.json", scan_status)
    mac_line = (f"Mac: read {mac_result.bytes_read} source bytes; " +
                ("finished" if isinstance(mac_result.progress, MacReadFinished) else "catching up") +
                f"; Claude transcripts {'included' if mac_result.claude_included else 'out'}"
                if isinstance(mac_result, MacEvidenceAvailable)
                else f"Mac: unavailable ({mac_result.reason}); read 0 source bytes; saved evidence retained")
    return "\n".join((
        f"Scanned {len(calls)} calls from {candidate_count} candidate files; read {bytes_read} transcript and timing bytes; saved {len(episodes)} episodes at 300 s and 900 s in {time.monotonic()-started:.1f} s.",
        mac_line,
        f"Claude MCP baseline Sep 9 PDT to 2026-10-06T16:35Z, 300 s: n={len(baseline)} total_h={sum(episode.minutes for episode in baseline)/60:.2f}",
        f"Full by hand Sep 9 PDT to 2026-10-06T16:35Z, 300 s: n={len(full)} total_h={sum(episode.minutes for episode in full)/60:.2f}",
    ))


def scan_hourly(state_dir: Path | None = None, now: datetime | None = None) -> str:
    """Run at most once in each UTC hour, including across concurrent callers."""
    state_dir = state_dir if state_dir is not None else STATE_DIR
    stamp = now if now is not None else datetime.now(timezone.utc)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "hourly.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = _read_object(state_dir / "scan_status.json")
        last_hour = status.get("last_hour_utc")
        if isinstance(last_hour, str):
            try:
                previous = datetime.fromisoformat(last_hour.replace("Z", "+00:00"))
                if previous.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) == (
                    stamp.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                ):
                    return f"Screenshot scan skipped: already ran this UTC hour at {_pacific(previous)}"
            except ValueError:
                pass
        result = scan(state_dir, CLAUDE_ROOT, CODEX_ROOT, AvailableTimingSource(TIMINGS_PATH, "natedev"), True)
        updated = _read_object(state_dir / "scan_status.json")
        updated["last_hour_utc"] = stamp.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0).isoformat()
        _save_object(state_dir / "scan_status.json", updated)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    scan_parser = commands.add_parser("scan")
    _ = scan_parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    _ = scan_parser.add_argument("--claude-root", type=Path, default=CLAUDE_ROOT)
    _ = scan_parser.add_argument("--codex-root", type=Path, default=CODEX_ROOT)
    _ = scan_parser.add_argument("--timings-path", type=Path, default=TIMINGS_PATH)
    _ = scan_parser.add_argument("--with-mac", action="store_true")
    hourly_parser = commands.add_parser("hourly")
    _ = hourly_parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    report_parser = commands.add_parser("report")
    _ = report_parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    _ = report_parser.add_argument("--since", default="")
    _ = report_parser.add_argument("--until", default="")
    _ = report_parser.add_argument("--project", default="")
    survey_parser = commands.add_parser("survey")
    _ = survey_parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    args = parser.parse_args(argv)
    action = cast(str, args.action)
    state_dir = cast(Path, args.state_dir)
    if action == "scan":
        claude_root, codex_root = cast(Path, args.claude_root), cast(Path, args.codex_root)
        print(scan(state_dir, claude_root, codex_root,
                   AvailableTimingSource(cast(Path, args.timings_path), "natedev"),
                   cast(bool, args.with_mac) or (claude_root == CLAUDE_ROOT and codex_root == CODEX_ROOT)))
    elif action == "hourly":
        print(scan_hourly(state_dir))
    elif action == "report":
        print(report(state_dir, cast(str, args.since), cast(str, args.until), cast(str, args.project)))
    else:
        print(survey(state_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
