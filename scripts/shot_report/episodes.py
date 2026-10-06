"""Split tool calls into screenshot episodes and store their results."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from transcripts import ToolCall  # pyright: ignore[reportImplicitRelativeImport]

Method = Literal["by hand", "/hana_shot"]
ScreenshotSource = Literal[
    "mcp_brp", "bash_brp", "hana_shot", "grim", "spectacle", "screencapture", "import", "browser",
]
ScreenshotSources = frozenset[ScreenshotSource]
SURVEY_SOURCES = {"grim", "spectacle", "screencapture", "import", "browser"}
DEFAULT_SOURCES = frozenset({"mcp_brp", "bash_brp", "hana_shot", *SURVEY_SOURCES})


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

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def split_episodes(
    calls: Iterable[ToolCall], gap_seconds: int,
    counted_sources: set[str] | frozenset[str] = DEFAULT_SOURCES,
) -> list[Episode]:
    """Group calls within one transcript using the previous result time."""
    by_transcript: dict[tuple[str, str], list[ToolCall]] = {}
    for call in calls:
        if call.kind == "shot" and call.source not in counted_sources:
            continue
        if call.kind == "other" and call.source == "browser" and "browser" not in counted_sources:
            continue
        by_transcript.setdefault((call.transcript_path, call.session_id), []).append(call)

    result: list[Episode] = []
    for transcript_calls in by_transcript.values():
        transcript_calls.sort(key=lambda call: (call.start, call.end))
        current: list[ToolCall] = []
        last_result: datetime | None = None
        for call in transcript_calls:
            if current and last_result is not None and (call.start - last_result).total_seconds() > gap_seconds:
                episode = _episode(current, gap_seconds)
                if episode is not None:
                    result.append(episode)
                current = []
            current.append(call)
            last_result = max(last_result, call.end) if last_result is not None else call.end
        if current:
            episode = _episode(current, gap_seconds)
            if episode is not None:
                result.append(episode)
    return sorted(result, key=lambda episode: (episode.start, episode.transcript_path))


def _episode(calls: list[ToolCall], gap_seconds: int) -> Episode | None:
    shots = [call for call in calls if call.kind == "shot"]
    if not shots:
        return None
    first = calls[0]
    method: Method = "/hana_shot" if any(call.source == "hana_shot" for call in shots) else "by hand"
    sources = cast(ScreenshotSources, frozenset(call.source for call in shots))
    return Episode(
        split=gap_seconds, agent=first.agent, project=first.project, method=method,
        source=sources, start=first.start, end=max(call.end for call in shots),
        screenshot_count=len(shots), image_count=sum(call.image_count for call in shots),
        hana_shot_call_count=sum(call.source == "hana_shot" for call in shots),
        other_call_count=sum(call.kind == "other" for call in calls),
        transcript_path=first.transcript_path, session_id=first.session_id,
    )


def write_episodes(path: Path, episodes: Iterable[Episode]) -> None:
    """Replace the JSONL file after writing every episode to a sibling file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            for episode in episodes:
                record = asdict(episode)
                record["source"] = sorted(episode.source)
                record["start"] = episode.start.isoformat()
                record["end"] = episode.end.isoformat()
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
            raw = cast(object, json.loads(line))
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
            screenshot_count = int(str(record["screenshot_count"]))
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
            ))
    return episodes
