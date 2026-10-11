"""Split tool calls into screenshot episodes and store their results."""

from __future__ import annotations

import json
import os
import re
import tempfile
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from heapq import heappop, heappush
from pathlib import Path
from typing import Literal, cast

if __package__:
    from .transcripts import (AttemptCountInferredFromImages, ExactAttemptCountFromOrderedCaptures,
                              ExactOrderedCaptureAttempts, FailedCapture, MeasuredHanaShotDuration, NamedCaptureView,
                              SelectedCaptureTarget, SuccessfulCapture, ToolCall, TranscriptToolEvent,
                              parsed_tool_event_category)
else:
    from transcripts import (  # pyright: ignore[reportImplicitRelativeImport]
        AttemptCountInferredFromImages, ExactAttemptCountFromOrderedCaptures, ExactOrderedCaptureAttempts,
        FailedCapture, MeasuredHanaShotDuration, NamedCaptureView, SelectedCaptureTarget, SuccessfulCapture,
        ToolCall, TranscriptToolEvent, parsed_tool_event_category,
    )

Method = Literal["by hand", "/hana_shot"]
ScreenshotSource = Literal[
    "mcp_brp", "bash_brp", "hana_shot", "grim", "spectacle", "screencapture", "import", "browser",
]
ScreenshotSources = frozenset[ScreenshotSource]
SURVEY_SOURCES = {"grim", "spectacle", "screencapture", "import", "browser"}
DEFAULT_SOURCES = frozenset({"mcp_brp", "bash_brp", "hana_shot", *SURVEY_SOURCES})
type GapCategory = Literal[
    "hana_shot", "retry", "other_brp", "builds_and_app_launches",
    "file_reads_searches_and_edits", "other_tool_calls", "agent_time", "unattributed",
]
GAP_CATEGORIES: tuple[GapCategory, ...] = (
    "hana_shot", "retry", "other_brp", "builds_and_app_launches",
    "file_reads_searches_and_edits", "other_tool_calls", "agent_time", "unattributed",
)


@dataclass(frozen=True)
class AttributedEpisodeGap:
    start: datetime
    end: datetime
    category: GapCategory

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass(frozen=True)
class EpisodeTimeline:
    gaps: tuple[AttributedEpisodeGap, ...]


@dataclass(frozen=True)
class TimelineUnavailable:
    pass


@dataclass(frozen=True)
class ShotIdentity:
    value: str


ToolUseIdentity = tuple[str, str, str]
TranscriptIdentity = tuple[str, str, str]


@dataclass(frozen=True)
class TranscriptToolEventWindow:
    """Start-ordered tool events and overlap bounds for one transcript."""

    events: tuple[TranscriptToolEvent, ...]
    starts: tuple[datetime, ...]
    latest_relevant_ends: tuple[datetime, ...]


ToolEventsByTranscript = dict[TranscriptIdentity, TranscriptToolEventWindow]


@dataclass(frozen=True)
class MostRecentActiveToolEvent:
    """A heap entry whose first item is the event used for timeline attribution."""

    event: TranscriptToolEvent

    def __lt__(self, other: MostRecentActiveToolEvent) -> bool:
        return _event_priority(self.event) > _event_priority(other.event)


@dataclass(frozen=True)
class MatchingEpisodeCall:
    call: ToolCall


@dataclass(frozen=True)
class NoMatchingEpisodeCall:
    pass


EpisodeCallMatch = MatchingEpisodeCall | NoMatchingEpisodeCall


@dataclass(frozen=True)
class OneCitedShot:
    attempts_before_first_kept_shot: int


@dataclass(frozen=True)
class SeveralCitedShots:
    attempts_before_first_kept_shot: int
    cited_attempt_count: int


@dataclass(frozen=True)
class NoneCited:
    pass


@dataclass(frozen=True)
class NoObservablePath:
    pass


@dataclass(frozen=True)
class LegacyEvidenceUnavailable:
    pass


KeptShotEvidence = OneCitedShot | SeveralCitedShots | NoneCited | NoObservablePath | LegacyEvidenceUnavailable


@dataclass(frozen=True)
class Episode:
    split: int
    agent: str
    project: str
    method: Method
    source: ScreenshotSources
    start: datetime
    end: datetime
    screenshot_count: int
    image_count: int
    hana_shot_call_count: int
    other_call_count: int
    transcript_path: str
    session_id: str
    kept_shot: KeptShotEvidence
    attempt_count_evidence: ExactAttemptCountFromOrderedCaptures | AttemptCountInferredFromImages = AttemptCountInferredFromImages()
    source_host: str = "local"
    timeline: EpisodeTimeline | TimelineUnavailable = TimelineUnavailable()

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def _transcript_identity(event: TranscriptToolEvent) -> TranscriptIdentity:
    return event.source_host, event.transcript_path, event.session_id


def _latest_relevant_end(event: TranscriptToolEvent) -> datetime:
    if event.unfinished:
        return datetime.max.replace(tzinfo=event.start.tzinfo)
    return event.end


def _index_tool_events(tool_events: Iterable[TranscriptToolEvent]) -> ToolEventsByTranscript:
    grouped: dict[TranscriptIdentity, list[TranscriptToolEvent]] = {}
    for event in tool_events:
        grouped.setdefault(_transcript_identity(event), []).append(event)
    index: ToolEventsByTranscript = {}
    for identity, grouped_events in grouped.items():
        grouped_events.sort(key=lambda event: (event.start, event.end, event.name, event.tool_use_id))
        starts: list[datetime] = []
        latest_relevant_ends: list[datetime] = []
        latest_end: datetime | None = None
        for event in grouped_events:
            starts.append(event.start)
            event_end = _latest_relevant_end(event)
            latest_end = event_end if latest_end is None else max(latest_end, event_end)
            latest_relevant_ends.append(latest_end)
        index[identity] = TranscriptToolEventWindow(
            tuple(grouped_events), tuple(starts), tuple(latest_relevant_ends),
        )
    return index


def split_episodes(
    calls: Iterable[ToolCall], gap_seconds: int,
    counted_sources: set[str] | frozenset[str] = DEFAULT_SOURCES,
    tool_events: Iterable[TranscriptToolEvent] = (),
) -> list[Episode]:
    """Group calls within one transcript using the previous result time."""
    by_transcript: dict[tuple[str, str], list[ToolCall]] = {}
    for call in calls:
        if call.kind == "shot" and call.source not in counted_sources:
            continue
        if call.kind == "other" and call.source == "browser" and "browser" not in counted_sources:
            continue
        by_transcript.setdefault((call.transcript_path, call.session_id), []).append(call)

    events_by_transcript = _index_tool_events(tool_events)
    result: list[Episode] = []
    for transcript_calls in by_transcript.values():
        transcript_calls.sort(key=lambda call: (call.start, call.end))
        current: list[ToolCall] = []
        last_result: datetime | None = None
        for call in transcript_calls:
            if current and last_result is not None and (call.start - last_result).total_seconds() > gap_seconds:
                episode = _episode(current, gap_seconds, events_by_transcript)
                if episode is not None:
                    result.append(episode)
                current = []
            current.append(call)
            last_result = max(last_result, call.end) if last_result is not None else call.end
        if current:
            episode = _episode(current, gap_seconds, events_by_transcript)
            if episode is not None:
                result.append(episode)
    return sorted(result, key=lambda episode: (episode.start, episode.transcript_path))


def _episode(calls: list[ToolCall], gap_seconds: int,
             events_by_transcript: ToolEventsByTranscript) -> Episode | None:
    shots = [call for call in calls if call.kind == "shot"]
    if not shots:
        return None
    first = calls[0]
    method: Method = "/hana_shot" if any(call.source == "hana_shot" for call in shots) else "by hand"
    sources = cast(ScreenshotSources, frozenset(call.source for call in shots))
    cited_shots = [call for call in shots if call.cited_image_paths]
    exact = False
    if cited_shots:
        first_kept = cited_shots[0]
        before = sum(len(_capture_paths(call)) for call in shots[:shots.index(first_kept)])
        kept_indices = _cited_attempt_indices(first_kept)
        before += min(kept_indices) if kept_indices else 0
        cited_attempt_count = sum(len(_cited_attempt_indices(call)) for call in cited_shots)
        evidence: KeptShotEvidence = (OneCitedShot(before) if cited_attempt_count == 1
                                     else SeveralCitedShots(before, cited_attempt_count))
        exact = all(isinstance(call.attempt_evidence, ExactOrderedCaptureAttempts)
                    for call in shots[:shots.index(first_kept) + 1])
    elif any(call.image_paths for call in shots):
        evidence = NoneCited()
    else:
        evidence = NoObservablePath()
    end = max(call.end for call in shots)
    timeline = _episode_timeline(first.start, end, calls, events_by_transcript)
    return Episode(
        split=gap_seconds, agent=first.agent, project=first.project, method=method,
        source=sources, start=first.start, end=end,
        screenshot_count=len(shots), image_count=sum(call.image_count for call in shots),
        hana_shot_call_count=sum(call.source == "hana_shot" for call in shots),
        other_call_count=sum(call.kind == "other" for call in calls),
        transcript_path=first.transcript_path, session_id=first.session_id,
        kept_shot=evidence,
        attempt_count_evidence=(ExactAttemptCountFromOrderedCaptures() if exact else AttemptCountInferredFromImages()),
        source_host=first.source_host, timeline=timeline,
    )


def _tool_use_identity(transcript_path: str, session_id: str, tool_use_id: str,
                       start: datetime, end: datetime) -> ToolUseIdentity:
    stable_id = tool_use_id or f"{start.isoformat()}/{end.isoformat()}"
    return transcript_path, session_id, stable_id


def _call_key(call: ToolCall) -> ToolUseIdentity:
    return _tool_use_identity(call.transcript_path, call.session_id, call.tool_use_id, call.start, call.end)


def _event_key(event: TranscriptToolEvent) -> ToolUseIdentity:
    return _tool_use_identity(
        event.transcript_path, event.session_id, event.tool_use_id, event.start, event.end,
    )


def _command_shot_identities(command: str) -> set[ShotIdentity]:
    identities: set[ShotIdentity] = set()
    for option in ("target", "view"):
        match = re.search(rf"(?:^|\s)--{option}(?:=|\s+)(['\"]?)([^\s'\"]+)\1", command)
        if match is not None:
            identities.add(ShotIdentity(f"{option}:{match.group(2)}"))
    return identities


def _attempt_shot_identities(call: ToolCall, attempt: SuccessfulCapture | FailedCapture) -> set[ShotIdentity]:
    identities = _command_shot_identities(call.command)
    if isinstance(attempt.view, NamedCaptureView):
        identities.add(ShotIdentity("view:" + attempt.view.name))
    if isinstance(attempt.target, SelectedCaptureTarget):
        identities.add(ShotIdentity("target:" + attempt.target.selector))
    return identities


def _retry_call_keys(calls: list[ToolCall]) -> set[ToolUseIdentity]:
    needs_retry: set[ShotIdentity] = set()
    retries: set[ToolUseIdentity] = set()
    for call in sorted((item for item in calls if item.kind == "shot"), key=lambda item: (item.start, item.end)):
        prior_needs_retry = needs_retry.copy()
        if isinstance(call.attempt_evidence, ExactOrderedCaptureAttempts):
            for attempt in call.attempt_evidence.captures:
                identities = _attempt_shot_identities(call, attempt)
                if prior_needs_retry & identities:
                    retries.add(_call_key(call))
                cited_success = (isinstance(attempt, SuccessfulCapture)
                                 and bool(set(attempt.image_paths) & set(call.cited_image_paths)))
                if cited_success:
                    needs_retry.difference_update(identities)
                else:
                    needs_retry.update(identities)
        else:
            identities = _command_shot_identities(call.command)
            if needs_retry & identities:
                retries.add(_call_key(call))
            if call.cited_image_paths:
                needs_retry.difference_update(identities)
            else:
                needs_retry.update(identities)
    return retries


def _tool_category(event: TranscriptToolEvent, call_match: EpisodeCallMatch,
                   retry_keys: set[ToolUseIdentity]) -> GapCategory:
    if isinstance(call_match, MatchingEpisodeCall):
        call = call_match.call
        if _call_key(call) in retry_keys:
            return "retry"
        if call.source == "hana_shot":
            return "hana_shot"
        if call.source in {"mcp_brp", "bash_brp"}:
            return "other_brp"
    return event.category


def _matching_tool_events(start: datetime, end: datetime, calls: list[ToolCall],
                          events_by_transcript: ToolEventsByTranscript) -> list[TranscriptToolEvent]:
    first = calls[0]
    identity = first.source_host, first.transcript_path, first.session_id
    window = events_by_transcript.get(identity)
    if window is None:
        matching: list[TranscriptToolEvent] = []
    else:
        first_relevant = bisect_right(window.latest_relevant_ends, start)
        after_window = bisect_left(window.starts, end)
        matching = [
            event for event in window.events[first_relevant:after_window]
            if (event.unfinished or event.end > start) and event.start < end
        ]
    known = {_event_key(event) for event in matching}
    matching.extend(TranscriptToolEvent(
        call.start, call.end, call.session_id, call.transcript_path, call.agent,
        call.source, parsed_tool_event_category(call.source, call.command), False,
        call.source_host, tool_use_id=call.tool_use_id,
    ) for call in calls if _call_key(call) not in known and call.end > start and call.start < end)
    return matching


def _event_priority(event: TranscriptToolEvent) -> tuple[datetime, datetime, str]:
    return event.start, event.end, event.name


def _unfinished_event_ranges(
    events: list[TranscriptToolEvent], episode_end: datetime,
) -> list[tuple[datetime, datetime]]:
    starts = sorted(event.start for event in events)
    unfinished_ends: dict[ToolUseIdentity, datetime] = {}
    for event in events:
        if not event.unfinished:
            continue
        same_start_begin = bisect_left(starts, event.start)
        after_same_start = bisect_right(starts, event.start)
        if after_same_start - same_start_begin > 1:
            open_end = event.start
        elif after_same_start < len(starts):
            open_end = starts[after_same_start]
        else:
            open_end = episode_end
        unfinished_ends[_event_key(event)] = open_end
    return sorted(
        (event.start, unfinished_ends[_event_key(event)])
        for event in events if event.unfinished
    )


def _episode_timeline(start: datetime, end: datetime, calls: list[ToolCall],
                      events_by_transcript: ToolEventsByTranscript) -> EpisodeTimeline:
    events = _matching_tool_events(start, end, calls, events_by_transcript)
    if end <= start:
        return EpisodeTimeline(())
    calls_by_key = {_call_key(call): call for call in calls}
    retry_keys = _retry_call_keys(calls)
    boundaries = {start, end}
    for event in events:
        boundaries.add(max(start, event.start))
        boundaries.add(min(end, max(start, event.end)))
        call = calls_by_key.get(_event_key(event))
        if call is not None and isinstance(call.hana_shot_duration, MeasuredHanaShotDuration):
            measured_start = event.end - timedelta(milliseconds=call.hana_shot_duration.milliseconds)
            boundaries.add(min(end, max(start, measured_start)))
    ordered = sorted(boundaries)
    events_by_start = sorted(events, key=lambda event: (event.start, event.end, event.name))
    finished_by_start = [event for event in events_by_start if not event.unfinished]
    finished_ends = sorted(event.end for event in finished_by_start)
    unfinished_ranges = _unfinished_event_ranges(events, end)
    active_finished: list[MostRecentActiveToolEvent] = []
    active_unfinished_ends: list[datetime] = []
    finished_start_index = 0
    finished_end_index = 0
    all_start_index = 0
    unfinished_start_index = 0
    gaps: list[AttributedEpisodeGap] = []
    for interval_start, interval_end in zip(ordered, ordered[1:]):
        if interval_end <= interval_start:
            continue
        while (finished_start_index < len(finished_by_start)
               and finished_by_start[finished_start_index].start < interval_end):
            heappush(active_finished, MostRecentActiveToolEvent(finished_by_start[finished_start_index]))
            finished_start_index += 1
        while active_finished and active_finished[0].event.end <= interval_start:
            _ = heappop(active_finished)
        while (unfinished_start_index < len(unfinished_ranges)
               and unfinished_ranges[unfinished_start_index][0] < interval_end):
            heappush(active_unfinished_ends, unfinished_ranges[unfinished_start_index][1])
            unfinished_start_index += 1
        while active_unfinished_ends and active_unfinished_ends[0] <= interval_start:
            _ = heappop(active_unfinished_ends)
        while finished_end_index < len(finished_ends) and finished_ends[finished_end_index] <= interval_start:
            finished_end_index += 1
        while all_start_index < len(events_by_start) and events_by_start[all_start_index].start < interval_end:
            all_start_index += 1
        if active_unfinished_ends:
            category: GapCategory = "unattributed"
        elif active_finished:
            event = active_finished[0].event
            call = calls_by_key.get(_event_key(event))
            call_match: EpisodeCallMatch = (MatchingEpisodeCall(call)
                                            if call is not None else NoMatchingEpisodeCall())
            category = _tool_category(event, call_match, retry_keys)
            if call is not None and isinstance(call.hana_shot_duration, MeasuredHanaShotDuration):
                measured_start = event.end - timedelta(milliseconds=call.hana_shot_duration.milliseconds)
                if interval_end <= measured_start:
                    category = "unattributed"
        else:
            has_result_before = finished_end_index > 0
            has_call_after = all_start_index < len(events_by_start)
            category = "agent_time" if has_result_before and has_call_after else "unattributed"
        if gaps and gaps[-1].category == category and gaps[-1].end == interval_start:
            gaps[-1] = AttributedEpisodeGap(gaps[-1].start, interval_end, category)
        else:
            gaps.append(AttributedEpisodeGap(interval_start, interval_end, category))
    return EpisodeTimeline(tuple(gaps))


def _capture_paths(call: ToolCall) -> tuple[tuple[str, ...], ...]:
    if isinstance(call.attempt_evidence, ExactOrderedCaptureAttempts):
        return tuple(attempt.image_paths for attempt in call.attempt_evidence.captures)
    paths = tuple((path,) for path in call.image_paths)
    if paths:
        return paths + tuple(() for _ in range(max(0, call.image_count - len(paths))))
    return tuple(() for _ in range(max(1, call.image_count)))


def _cited_attempt_indices(call: ToolCall) -> set[int]:
    attempts = _capture_paths(call)
    return {max((index for index, paths in enumerate(attempts) if path in paths), default=0)
            for path in call.cited_image_paths}


def write_episodes(path: Path, episodes: Iterable[Episode]) -> None:
    """Replace the JSONL file after writing every episode to a sibling file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            for episode in episodes:
                record: dict[str, object] = {
                    "split": episode.split,
                    "agent": episode.agent,
                    "project": episode.project,
                    "method": episode.method,
                    "source": sorted(episode.source),
                    "start": episode.start.isoformat(),
                    "end": episode.end.isoformat(),
                    "screenshot_count": episode.screenshot_count,
                    "image_count": episode.image_count,
                    "hana_shot_call_count": episode.hana_shot_call_count,
                    "other_call_count": episode.other_call_count,
                    "transcript_path": episode.transcript_path,
                    "session_id": episode.session_id,
                    "source_host": episode.source_host,
                    "kept_shot_state": _evidence_name(episode.kept_shot),
                    "attempt_count_evidence_state": type(episode.attempt_count_evidence).__name__,
                }
                if isinstance(episode.timeline, EpisodeTimeline):
                    record["timeline"] = [
                        {"start": gap.start.isoformat(), "end": gap.end.isoformat(), "category": gap.category}
                        for gap in episode.timeline.gaps
                    ]
                if isinstance(episode.kept_shot, (OneCitedShot, SeveralCitedShots)):
                    record["attempts_before_first_kept_shot"] = episode.kept_shot.attempts_before_first_kept_shot
                if isinstance(episode.kept_shot, SeveralCitedShots):
                    record["cited_attempt_count"] = episode.kept_shot.cited_attempt_count
                _ = target.write(json.dumps(record, sort_keys=True) + "\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_episodes(path: Path) -> list[Episode]:
    if not path.exists():
        return []
    episodes: list[Episode] = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                raw = cast(object, json.loads(line))
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            record = cast(dict[str, object], raw)
            start = record.get("start")
            end = record.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                continue
            method = record.get("method")
            if method not in ("by hand", "/hana_shot"):
                continue
            raw_sources = record.get("source", [])
            if isinstance(raw_sources, str):
                source_names: list[object] = cast(list[object], raw_sources.split(","))
            elif isinstance(raw_sources, list):
                source_names = cast(list[object], raw_sources)
            else:
                continue
            if not all(isinstance(name, str) and name in DEFAULT_SOURCES for name in source_names):
                continue
            sources = cast(ScreenshotSources, frozenset(source_names))
            try:
                screenshot_count = int(str(record["screenshot_count"]))
                evidence = _read_evidence(record)
                episodes.append(Episode(
                    split=int(str(record["split"])), agent=str(record["agent"]),
                    project=str(record["project"]), method=method,
                    source=sources,
                    start=datetime.fromisoformat(start.replace("Z", "+00:00")),
                    end=datetime.fromisoformat(end.replace("Z", "+00:00")),
                    screenshot_count=screenshot_count,
                    image_count=int(str(record.get("image_count", screenshot_count))),
                    hana_shot_call_count=int(str(record.get(
                        "hana_shot_call_count", screenshot_count if "hana_shot" in sources else 0,
                    ))),
                    other_call_count=int(str(record["other_call_count"])),
                    transcript_path=str(record["transcript_path"]),
                    session_id=str(record["session_id"]),
                    kept_shot=evidence,
                    attempt_count_evidence=(ExactAttemptCountFromOrderedCaptures()
                                            if record.get("attempt_count_evidence_state") in
                                            ("ExactAttemptCountFromOrderedCaptures", "ExactOrderedCaptureAttempts")
                                            else AttemptCountInferredFromImages()),
                    source_host=str(record.get("source_host", "local")),
                    timeline=_read_timeline(record),
                ))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
    return episodes


def _evidence_name(evidence: KeptShotEvidence) -> str:
    if isinstance(evidence, OneCitedShot):
        return "cited"
    if isinstance(evidence, SeveralCitedShots):
        return "several_cited"
    if isinstance(evidence, NoneCited):
        return "none_cited"
    if isinstance(evidence, NoObservablePath):
        return "no_observable_path"
    return "legacy_unassessed"


def _read_evidence(record: dict[str, object]) -> KeptShotEvidence:
    state = record.get("kept_shot_state")
    if state == "cited":
        return OneCitedShot(int(str(record["attempts_before_first_kept_shot"])))
    if state == "several_cited":
        return SeveralCitedShots(int(str(record["attempts_before_first_kept_shot"])),
                                 int(str(record["cited_attempt_count"])))
    if state == "none_cited":
        return NoneCited()
    if state == "no_observable_path":
        return NoObservablePath()
    return LegacyEvidenceUnavailable()


def _read_timeline(record: dict[str, object]) -> EpisodeTimeline | TimelineUnavailable:
    raw = record.get("timeline")
    if not isinstance(raw, list):
        return TimelineUnavailable()
    gaps: list[AttributedEpisodeGap] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            return TimelineUnavailable()
        gap = cast(dict[str, object], item)
        category = gap.get("category")
        start, end = gap.get("start"), gap.get("end")
        if category not in GAP_CATEGORIES or not isinstance(start, str) or not isinstance(end, str):
            return TimelineUnavailable()
        gaps.append(AttributedEpisodeGap(
            datetime.fromisoformat(start.replace("Z", "+00:00")),
            datetime.fromisoformat(end.replace("Z", "+00:00")),
            category,
        ))
    return EpisodeTimeline(tuple(gaps))
