"""Make a bounded check pass, committing only complete upstream facts."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import upstream
from pins import Pins
from upgrade_record import BranchRead, BranchUnread, CratesIoRead, CratesIoUnlisted, Entry, Record, on_target, target_fit

Clock = Callable[[], datetime]
CHECK_BUDGET = timedelta(seconds=180)


class CheckPass(NamedTuple):
    record: Record
    asked: int
    failed: int


class CheckBudgetSpent(Exception):
    """No further request fits this pass; partial entry facts are discarded."""


def _priority(item: tuple[str, Entry]) -> tuple[int, str, str]:
    name, entry = item
    facts = entry["crates_io"]
    return (0, "", name) if facts["status"] == "unread" else (1, facts["checked"], name)


def _skip(entry: Entry, target: str) -> bool:
    facts = entry["crates_io"]
    return (on_target(entry["bevy"], target) or entry["patched_bevy"]
            or entry["pin"]["source"] == "path" or (facts["status"] == "read" and bool(facts["ready"])))


def _crate_facts(fetch: upstream.Fetch, name: str, entry: Entry, target: str,
                 bevy_names: frozenset[str], checked: str) -> CratesIoRead | CratesIoUnlisted:
    facts = entry["crates_io"]
    releases = dict(facts["releases"]) if facts["status"] == "read" else {}
    try:
        info = upstream.crate_info(fetch, name)
    except upstream.NotFound:
        pin = entry["pin"]
        repository = upstream.github_repository(pin["url"]) if pin["source"] == "git" else ""
        return CratesIoUnlisted(status="unlisted", checked=checked, repository=repository)
    if not info.versions:
        raise upstream.FetchFailure("crates.io: no version that is not yanked")
    pin = entry["pin"]["version"]
    count = info.versions.index(pin) + 1 if pin in info.versions else 6
    versions = info.versions[:count]
    ready = ""
    for version in versions:
        if version not in releases:
            releases[version] = upstream.release_requirement(fetch, name, version, bevy_names)
        if not ready and target_fit(releases[version], target) == "on target":
            ready = version
    return CratesIoRead(status="read", checked=checked, releases=releases,
                        newest=info.versions[0], ready=ready, repository=info.repository)


def _github_facts(fetch: upstream.Fetch, name: str, entry: Entry, record: Record, bevy_names: frozenset[str]) -> None:
    facts = entry["crates_io"]
    if facts["status"] == "unread" or not facts["repository"]:
        return
    if facts["status"] == "read" and facts["ready"]:
        return
    repository = facts["repository"]
    branch = entry["branch"]
    previous = ({item["number"]: item for item in entry["items"]}
                if branch["status"] == "read" and branch["repository"] == repository else {})
    if branch["status"] == "read" and branch["repository"] != repository:
        branch = BranchUnread(status="unread")
    branch_name = branch["name"] if branch["status"] == "read" else upstream.default_branch(fetch, repository)
    try:
        head = upstream.branch_head(fetch, repository, branch_name)
    except upstream.NotFound:
        if branch["status"] == "unread":
            raise
        branch_name = upstream.default_branch(fetch, repository)
        head = upstream.branch_head(fetch, repository, branch_name)
    requirement = (branch["requirement"] if branch["status"] == "read" and branch["rev"] == head.rev
                   and branch["requirement"]
                   else upstream.manifest_requirement(fetch, repository, head.rev, name, bevy_names))
    target = record["target_bevy"]
    since = (target["since"] if target["status"] == "pre-release" or target["status"] == "released"
             else record["started"][:10])
    items = upstream.port_items(fetch, repository, record["target"], since)
    for item in items:
        if item["kind"] != "pr":
            continue
        old = previous.get(item["number"])
        if old is not None and old["kind"] == "pr" and old["updated"] == item["updated"]:
            item["state"], item["head"] = old["state"], old["head"]
        else:
            item["state"], item["head"] = upstream.pull_state(fetch, repository, item["number"])
    returned = {item["number"] for item in items}
    for number, old in previous.items():
        if number in returned:
            continue
        item = old.copy()
        if item["kind"] == "pr" and item["state"] == "open":
            item["state"], item["head"] = upstream.pull_state(fetch, repository, number)
        items.append(item)
    entry["branch"] = BranchRead(status="read", repository=repository, name=branch_name, rev=head.rev,
                                  committed=head.committed, requirement=requirement)
    entry["items"] = items


def run_check(record: Record, pins: Pins, fetch: upstream.Fetch, clock: Clock) -> CheckPass:
    now = clock()
    checked = now.astimezone(timezone.utc).isoformat()
    asked = failed = 0
    entry_asked = False

    def bounded_fetch(url: str) -> bytes:
        if clock() - now >= CHECK_BUDGET:
            raise CheckBudgetSpent
        return fetch(url)

    def entry_fetch(url: str) -> bytes:
        nonlocal entry_asked
        if clock() - now >= CHECK_BUDGET:
            raise CheckBudgetSpent
        entry_asked = True
        return fetch(url)

    try:
        if record["target_bevy"]["status"] != "released":
            try:
                target = upstream.target_release(bounded_fetch, record["target"])
            except upstream.FetchFailure as error:
                record["target_error"] = str(error)
            else:
                record["target_bevy"] = target
                record["target_error"] = ""
        for name, entry in sorted(record["dependencies"].items(), key=_priority):
            if _skip(entry, record["target"]):
                continue
            entry_asked = False
            try:
                staged: Entry = entry.copy()
                staged["crates_io"] = _crate_facts(entry_fetch, name, entry, record["target"], pins.bevy_names, checked)
                _github_facts(entry_fetch, name, staged, record, pins.bevy_names)
            except upstream.FetchFailure as error:
                entry["error"] = str(error)
                failed += 1
            else:
                entry.update(staged)
                entry["error"] = ""
            finally:
                if entry_asked:
                    asked += 1
        record["checked"] = checked
    except CheckBudgetSpent:
        pass
    return CheckPass(record, asked, failed)
