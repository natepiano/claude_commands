"""Scan transcripts once, then report screenshot episode times from saved data."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections.abc import Iterable
from collections import defaultdict
from datetime import datetime, time as clock_time, timezone
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from episodes import Episode, SURVEY_SOURCES, read_episodes, split_episodes, write_episodes  # pyright: ignore[reportImplicitRelativeImport]
from transcripts import scan_calls, survey_counts  # pyright: ignore[reportImplicitRelativeImport]

PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_DIR = Path.home() / ".local/state/screenshot-analysis"
CLAUDE_ROOT = Path.home() / ".claude/projects"
CODEX_ROOT = Path.home() / ".codex/sessions"


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
    start_label = start.astimezone(PACIFIC).isoformat() if start else "all available"
    end_label = end.astimezone(PACIFIC).isoformat() if end else "latest saved"
    lines = [f"Window: {start_label} to {end_label} PDT (America/Los_Angeles); n counts episodes; duration in hours and minutes"]
    lines.extend(_report_rows(selected))
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


def scan(state_dir: Path, claude_root: Path, codex_root: Path) -> str:
    started = time.monotonic()
    calls = scan_calls(claude_root, codex_root)
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
    return "\n".join((
        f"Scanned {len(calls)} calls; saved {len(episodes)} episodes at 300 s and 900 s in {time.monotonic()-started:.1f} s.",
        f"Claude MCP baseline Sep 9 PDT to 2026-10-06T16:35Z, 300 s: n={len(baseline)} total_h={sum(episode.minutes for episode in baseline)/60:.2f}",
        f"Full by hand Sep 9 PDT to 2026-10-06T16:35Z, 300 s: n={len(full)} total_h={sum(episode.minutes for episode in full)/60:.2f}",
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    scan_parser = commands.add_parser("scan")
    _ = scan_parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    _ = scan_parser.add_argument("--claude-root", type=Path, default=CLAUDE_ROOT)
    _ = scan_parser.add_argument("--codex-root", type=Path, default=CODEX_ROOT)
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
        print(scan(state_dir, cast(Path, args.claude_root), cast(Path, args.codex_root)))
    elif action == "report":
        print(report(state_dir, cast(str, args.since), cast(str, args.until), cast(str, args.project)))
    else:
        print(survey(state_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
