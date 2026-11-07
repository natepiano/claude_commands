"""GitHub Actions timings for the build log: one line per completed run attempt.

For each repo in REPOS, every completed run attempt not yet in <root>/ci/ is
written to ci/<YYYY-MM of its creation>.jsonl with its jobs and their steps,
so queue time, job time and step time sit beside the local builds. Only
natedev polls; sync carries ci/ to the Mac.

The GitHub token sits behind gpg-agent, which is cold after a reboot and a
week after each unlock. github-warm-status answers without prompting; when it
says cold this skips quietly and exits 0, and the next poll catches up. Never
`gh auth login`: a cold agent is not a lost login.

Paging: the first full pass reads every page (515 runs on 2026-10-02). Once a
pass has completed, a poll stops at the first page that is entirely known and
older than STOP_AGE_S. At most REQUEST_CAP requests for jobs and attempts per
poll; what is left over waits for the next one.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import store

REPOS = ("natepiano/hana",)
PER_PAGE = 100
REQUEST_CAP = 1000
STOP_AGE_S = 3 * 24 * 3600
GH_TIMEOUT_S = 60
STATE_NAME = "ci_state.json"


class GhRun(TypedDict):
    id: int
    run_attempt: int
    name: str | None
    event: str
    head_branch: str | None
    head_sha: str
    status: str
    conclusion: str | None
    created_at: str
    run_started_at: str | None
    updated_at: str
    html_url: str


class GhRuns(TypedDict):
    total_count: int
    workflow_runs: list[GhRun]


class GhStep(TypedDict):
    number: int
    name: str
    status: str
    conclusion: str | None
    started_at: str | None
    completed_at: str | None


class GhJob(TypedDict):
    id: int
    name: str
    status: str
    conclusion: str | None
    created_at: str | None
    started_at: str | None
    completed_at: str | None
    runner_name: str | None
    labels: list[str]
    steps: NotRequired[list[GhStep]]


class GhJobs(TypedDict):
    total_count: int
    jobs: list[GhJob]


class RepoState(TypedDict):
    backfill_complete: bool


@dataclass(frozen=True)
class CompletedPoll:
    polled_at: str


@dataclass(frozen=True)
class CappedPoll:
    polled_at: str


@dataclass(frozen=True)
class NeverPolled:
    pass


PollState = CompletedPoll | CappedPoll | NeverPolled


class GhError(Exception):
    pass


def warm() -> bool:
    try:
        result = subprocess.run(["github-warm-status"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def gh_get(path: str, fields: dict[str, str]) -> object:
    args = ["gh", "api", "-X", "GET", path]
    for name, value in fields.items():
        args += ["-f", f"{name}={value}"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=GH_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        raise GhError(f"gh api {path}: {error}") from error
    if result.returncode != 0:
        raise GhError(f"gh api {path}: {(result.stderr.strip().splitlines() or ['no output'])[-1]}")
    return cast(object, json.loads(result.stdout))


def known_attempts() -> set[tuple[int, int]]:
    known: set[tuple[int, int]] = set()
    for path in (store.root() / store.CI_DIR).glob("*.jsonl"):
        with path.open("rb") as handle:
            for raw in handle:
                try:
                    line = cast(dict[str, object], json.loads(raw))
                except ValueError:
                    continue
                run_id, attempt = line.get("run_id"), line.get("attempt")
                if isinstance(run_id, int) and isinstance(attempt, int):
                    known.add((run_id, attempt))
    return known


def state_path() -> Path:
    return store.root() / STATE_NAME


def read_state() -> dict[str, RepoState]:
    try:
        return cast(dict[str, RepoState], json.loads(state_path().read_text()))
    except (OSError, ValueError):
        return {}


def write_state(state: dict[str, RepoState]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    _ = partial.write_text(json.dumps(state, indent=2) + "\n")
    _ = partial.replace(path)


def polled_path() -> Path:
    return store.root() / store.CI_DIR / store.CI_POLLED_NAME


def _read_polled() -> dict[str, object]:
    try:
        data = cast(object, json.loads(polled_path().read_text()))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, object], data) if isinstance(data, dict) else {}


def read_poll_state(repo: str) -> PollState:
    record = _read_polled().get(repo)
    if not isinstance(record, dict):
        return NeverPolled()
    fields = cast(dict[str, object], record)
    polled_at = fields.get("polled_at")
    complete = fields.get("complete")
    if not isinstance(polled_at, str) or not isinstance(complete, bool):
        return NeverPolled()
    try:
        at = datetime.fromisoformat(polled_at.replace("Z", "+00:00"))
    except ValueError:
        return NeverPolled()
    if at.tzinfo is None:
        return NeverPolled()
    return CompletedPoll(polled_at) if complete else CappedPoll(polled_at)


def write_polled(repo: str, complete: bool) -> None:
    records = _read_polled()
    records[repo] = {"polled_at": store.utc_iso(time.time()), "complete": complete}
    path = polled_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    _ = partial.write_text(json.dumps(records) + "\n")
    _ = partial.replace(path)


def epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


class Poll:
    """One poll of one repo, counting its requests against REQUEST_CAP."""

    def __init__(self, repo: str, known: set[tuple[int, int]]) -> None:
        self.repo: str = repo
        self.known: set[tuple[int, int]] = known
        self.requests: int = 0
        self.written: int = 0
        self.jobs: int = 0

    def capped(self) -> bool:
        return self.requests >= REQUEST_CAP

    def jobs_of(self, run_id: int, attempt: int) -> list[GhJob]:
        jobs: list[GhJob] = []
        page = 1
        while True:
            self.requests += 1
            data = cast(
                GhJobs,
                gh_get(
                    f"repos/{self.repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
                    {"per_page": str(PER_PAGE), "page": str(page)},
                ),
            )
            jobs += data["jobs"]
            if len(data["jobs"]) < PER_PAGE or len(jobs) >= data["total_count"]:
                return jobs
            page += 1

    def write(self, run: GhRun, attempt: int) -> None:
        if attempt != run["run_attempt"]:
            # The listing describes the latest attempt; an earlier one has its own times.
            self.requests += 1
            run = cast(GhRun, gh_get(f"repos/{self.repo}/actions/runs/{run['id']}/attempts/{attempt}", {}))
        jobs = self.jobs_of(run["id"], attempt)
        record: dict[str, object] = {
            "kind": "ci_run",
            "v": 1,
            "repo": self.repo,
            "run_id": run["id"],
            "attempt": attempt,
            "workflow": run["name"],
            "event": run["event"],
            "branch": run["head_branch"],
            "sha": run["head_sha"],
            "status": run["status"],
            "conclusion": run["conclusion"],
            "created_at": run["created_at"],
            "started_at": run["run_started_at"],
            "updated_at": run["updated_at"],
            "url": run["html_url"],
            "jobs": [
                {
                    "job_id": job["id"],
                    "name": job["name"],
                    "status": job["status"],
                    "conclusion": job["conclusion"],
                    "created_at": job["created_at"],
                    "started_at": job["started_at"],
                    "completed_at": job["completed_at"],
                    "runner": job["runner_name"],
                    "labels": job["labels"],
                    "steps": [
                        {
                            "number": step["number"],
                            "name": step["name"],
                            "status": step["status"],
                            "conclusion": step["conclusion"],
                            "started_at": step["started_at"],
                            "completed_at": step["completed_at"],
                        }
                        for step in job.get("steps", [])
                    ],
                }
                for job in jobs
            ],
        }
        created = epoch(run["created_at"])
        store.append_line(store.root() / store.CI_DIR / f"{store.month(created)}.jsonl", record)
        self.known.add((run["id"], attempt))
        self.written += 1
        self.jobs += len(jobs)

    def page(self, runs: list[GhRun]) -> bool:
        """Write the page's new attempts; True when every attempt on it was already known."""
        all_known = True
        for run in runs:
            for attempt in range(1, run["run_attempt"] + 1):
                if (run["id"], attempt) in self.known:
                    continue
                all_known = False
                if attempt == run["run_attempt"] and run["status"] != "completed":
                    continue
                if self.capped():
                    return False
                self.write(run, attempt)
        return all_known

    def run(self, backfilled: bool) -> bool:
        """Poll every page it needs; True when it reached the end uncapped."""
        page = 1
        while True:
            self.requests += 1
            data = cast(
                GhRuns,
                gh_get(f"repos/{self.repo}/actions/runs", {"per_page": str(PER_PAGE), "page": str(page)}),
            )
            runs = data["workflow_runs"]
            if not runs:
                return not self.capped()
            all_known = self.page(runs)
            if self.capped():
                return False
            oldest = min(epoch(run["created_at"]) for run in runs)
            if backfilled and all_known and oldest < time.time() - STOP_AGE_S:
                return True
            if len(runs) < PER_PAGE:
                return True
            page += 1


def ci() -> int:
    if not warm():
        print("buildlog ci: GitHub credentials are cold (github-warm-status); skipped until github-warmup")
        return 0
    known = known_attempts()
    state = read_state()
    status = 0
    for repo in REPOS:
        poll = Poll(repo, known)
        backfilled = state.get(repo, {"backfill_complete": False})["backfill_complete"]
        try:
            complete = poll.run(backfilled)
        except GhError as error:
            print(f"buildlog ci: {repo}: {error}")
            status = 1
            complete = False
        else:
            write_polled(repo, complete)
        if complete and not backfilled:
            state[repo] = {"backfill_complete": True}
            write_state(state)
        left = " (request cap reached; the rest next poll)" if poll.capped() else ""
        print(
            f"buildlog ci: {repo}: {poll.written} run attempts, {poll.jobs} jobs added"
            + f" in {poll.requests} requests{left}"
        )
    return status
