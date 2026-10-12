"""Check request order, immutable release caching and budget-limited progress."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, final
from urllib.parse import urlsplit

import pins
import upgrade_check
import upgrade_record
import upstream

if TYPE_CHECKING:
    from typing import override
else:
    def override(method: object) -> object:
        return method

API = "https://crates.io/api/v1/crates"
NOW = datetime(2026, 10, 12, 19, 8, 41, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=1)
NO_STEP = timedelta()
BEVY_NAMES = frozenset({"bevy", "bevy_ecs", "bevy_app"})


@final
class StepClock:
    def __init__(self, start: datetime = NOW, step: timedelta = NO_STEP) -> None:
        self.next = start
        self.step = step

    def __call__(self) -> datetime:
        result = self.next
        self.next += self.step
        return result


@final
class CheckFetch:
    def __init__(self) -> None:
        self.bodies: dict[str, object] = {}
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if url not in self.bodies:
            raise upstream.FetchFailure(f"{urlsplit(url).hostname}: no fixture")
        body = self.bodies[url]
        if isinstance(body, upstream.FetchFailure):
            raise body
        if body is None:
            raise upstream.NotFound(f"{urlsplit(url).hostname}: HTTP 404")
        return body.encode() if isinstance(body, str) else json.dumps(body).encode()

    def crate(self, name: str, versions: tuple[str, ...] = ("1.0.0",), requirement: str = "^0.19") -> None:
        self.bodies[f"{API}/{name}"] = {"crate": {"repository": None},
                                       "versions": [{"num": version, "yanked": False} for version in versions]}
        for version in versions:
            self.bodies[f"{API}/{name}/{version}/dependencies"] = {
                "dependencies": [{"crate_id": "bevy_ecs", "kind": "normal", "req": requirement}],
            }


def entry(version: str = "1.0.0") -> upgrade_record.Entry:
    return {"pin": {"source": "registry", "version": version}, "bevy": "0.19.1", "via": [],
            "patched_bevy": False, "answer": None, "note": "", "crates_io": {"status": "unread"}, "error": "",
            "branch": {"status": "unread"}, "items": []}


def read_fact(checked: datetime = OLD, ready: str = "") -> upgrade_record.CratesIoRead:
    return {"status": "read", "checked": checked.isoformat(), "releases": {"1.0.0": "^0.19"},
            "newest": "1.0.0", "ready": ready, "repository": ""}


@final
class CheckTests(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.current: upgrade_record.Record = {
            "slug": "demo", "production": "", "checkout": "", "target": "0.20",
            "started": OLD.isoformat(), "closed": None, "bevy": ["0.19.1"],
            "pins_read": OLD.isoformat(), "dependencies": {}, "target_bevy": {"status": "unread"},
            "target_error": "", "checked": None,
        }
        self.fetch = CheckFetch()

    @override
    def setUp(self) -> None:
        self.fetch.bodies[f"{API}/bevy/versions"] = {"versions": []}

    def run_pass(self, clock: StepClock | None = None) -> upgrade_check.CheckPass:
        return upgrade_check.run_check(self.current, pins.Pins(("0.19.1",), BEVY_NAMES, ()),
                                       self.fetch, clock or StepClock())

    def released_target(self) -> None:
        self.current["target_bevy"] = {"status": "released", "version": "0.20.0",
                                       "date": "2026-10-12", "since": "2026-10-01"}

    def test_target_is_looked_up_once_per_pass_until_released(self) -> None:
        result = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/bevy/versions"])
        self.assertEqual(result.record["target_bevy"], {"status": "unpublished"})
        self.fetch.bodies[f"{API}/bevy/versions"] = {"versions": [
            {"num": "0.20.0-rc.1", "created_at": "2026-10-01T00:00:00Z"},
        ]}
        _ = self.run_pass()
        self.assertEqual(self.current["target_bevy"], {"status": "pre-release", "since": "2026-10-01"})
        self.fetch.bodies[f"{API}/bevy/versions"] = {"versions": [
            {"num": "0.20.0", "created_at": "2026-10-12T00:00:00Z"},
        ]}
        _ = self.run_pass()
        _ = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/bevy/versions"] * 3)
        self.assertEqual(result.asked, 0)
        self.assertEqual(result.failed, 0)

    def test_skip_rules_make_no_entry_requests_or_changes(self) -> None:
        self.released_target()
        done, patched, path, ready = entry(), entry(), entry(), entry()
        done["bevy"] = "0.20.3"
        patched["patched_bevy"] = True
        path["pin"] = {"source": "path", "path": "vendor/adapter", "version": "1.0.0"}
        ready["crates_io"] = read_fact(ready="1.0.0")
        self.current["dependencies"] = {"done": done, "patched": patched, "path": path, "ready": ready}
        for value in self.current["dependencies"].values():
            value["error"] = "earlier failure"
        before = copy.deepcopy(self.current["dependencies"])
        result = self.run_pass()
        self.assertEqual(self.fetch.urls, [])
        self.assertEqual(result.record["dependencies"], before)
        self.assertEqual((result.asked, result.failed), (0, 0))
        self.assertEqual(result.record["checked"], NOW.isoformat())

    def test_walk_includes_pin_and_selects_newest_matching_target(self) -> None:
        self.current["dependencies"] = {"adapter": entry("1.0.0")}
        self.fetch.crate("adapter", ("3.0.0", "2.0.0", "1.0.0", "0.9.0"))
        self.fetch.bodies[f"{API}/adapter/2.0.0/dependencies"] = {
            "dependencies": [{"crate_id": "bevy_app", "kind": "normal", "req": "^0.20.0"}],
        }
        self.current["dependencies"]["adapter"]["error"] = "old failure"
        result = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/bevy/versions", f"{API}/adapter",
                                          f"{API}/adapter/3.0.0/dependencies", f"{API}/adapter/2.0.0/dependencies",
                                          f"{API}/adapter/1.0.0/dependencies"])
        self.assertEqual(result.record["dependencies"]["adapter"]["crates_io"], {
            "status": "read", "checked": NOW.isoformat(), "newest": "3.0.0", "ready": "2.0.0",
            "repository": "", "releases": {"3.0.0": "^0.19", "2.0.0": "^0.20.0", "1.0.0": "^0.19"},
        })
        self.assertEqual(result.record["dependencies"]["adapter"]["error"], "")
        self.assertEqual((result.asked, result.failed), (1, 0))

    def test_missing_pin_walks_only_six_newest_releases(self) -> None:
        self.released_target()
        self.current["dependencies"] = {"adapter": entry("0.1.0")}
        versions = tuple(f"{number}.0.0" for number in range(9, 1, -1))
        self.fetch.crate("adapter", versions)
        _ = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter"]
                         + [f"{API}/adapter/{version}/dependencies" for version in versions[:6]])

    def test_found_pin_is_walked_even_beyond_six_versions(self) -> None:
        self.released_target()
        versions = tuple(f"{number}.0.0" for number in range(9, 0, -1))
        self.current["dependencies"] = {"adapter": entry("2.0.0")}
        self.fetch.crate("adapter", versions)
        _ = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter"]
                         + [f"{API}/adapter/{version}/dependencies" for version in versions[:-1]])

    def test_ready_is_newest_matching_release_and_unreadable_requirements_do_not_match(self) -> None:
        self.released_target()
        self.current["dependencies"] = {"adapter": entry()}
        self.fetch.crate("adapter", ("4.0.0", "3.0.0", "2.0.0", "1.0.0"), requirement="^0.20")
        self.fetch.bodies[f"{API}/adapter/4.0.0/dependencies"] = {"dependencies": []}
        self.fetch.bodies[f"{API}/adapter/1.0.0/dependencies"] = {
            "dependencies": [{"crate_id": "bevy_ecs", "kind": "normal", "req": ">=0.20, <0.21"}],
        }
        result = self.run_pass()
        facts = result.record["dependencies"]["adapter"]["crates_io"]
        assert facts["status"] == "read"
        self.assertEqual(facts["newest"], "4.0.0")
        self.assertEqual(facts["ready"], "3.0.0")
        self.assertEqual(facts["releases"]["4.0.0"], "")
        self.assertEqual(facts["releases"]["1.0.0"], ">=0.20, <0.21")
        self.assertEqual(self.fetch.urls, [f"{API}/adapter"] + [
            f"{API}/adapter/{version}/dependencies" for version in ("4.0.0", "3.0.0", "2.0.0", "1.0.0")])

    def test_cached_requirements_are_not_fetched_again_and_old_releases_survive(self) -> None:
        self.released_target()
        current = entry()
        current["crates_io"] = read_fact()
        current["crates_io"]["releases"]["0.5.0"] = "^0.18"
        self.current["dependencies"] = {"adapter": current}
        self.fetch.crate("adapter", ("2.0.0", "1.0.0"))
        _ = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{API}/adapter/2.0.0/dependencies"])
        facts = current["crates_io"]
        assert facts["status"] == "read"
        self.assertEqual(facts["releases"], {"0.5.0": "^0.18", "1.0.0": "^0.19", "2.0.0": "^0.19"})
        self.fetch.urls.clear()
        _ = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter"])

    def test_failure_rolls_back_all_facts_but_other_entries_update(self) -> None:
        self.released_target()
        failing = entry()
        failing["crates_io"] = read_fact()
        failing["answer"] = {"answer": "approved", "at": "2026-10-01", "words": "use this"}
        failing["note"] = "keep this"
        before = copy.deepcopy(failing)
        self.current["dependencies"] = {"a-failing": failing, "z-good": entry()}
        self.fetch.crate("a-failing", ("3.0.0", "2.0.0", "1.0.0"))
        del self.fetch.bodies[f"{API}/a-failing/2.0.0/dependencies"]
        self.fetch.crate("z-good")
        result = self.run_pass()
        before["error"] = "crates.io: no fixture"
        self.assertEqual(result.record["dependencies"]["a-failing"], before)
        self.assertEqual(result.record["dependencies"]["z-good"]["crates_io"]["status"], "read")
        self.assertEqual((result.asked, result.failed), (2, 1))
        self.assertEqual(result.record["checked"], NOW.isoformat())

    def test_all_yanked_versions_fail_without_replacing_stored_facts(self) -> None:
        self.released_target()
        current = entry()
        current["crates_io"] = read_fact()
        before = copy.deepcopy(current)
        self.current["dependencies"] = {"adapter": current}
        self.fetch.bodies[f"{API}/adapter"] = {"crate": {"repository": None},
                                               "versions": [{"num": "1.0.0", "yanked": True}]}
        result = self.run_pass()
        before["error"] = "crates.io: no version that is not yanked"
        self.assertEqual(current, before)
        self.assertEqual((result.asked, result.failed), (1, 1))
        self.assertEqual(self.fetch.urls, [f"{API}/adapter"])

    def test_failed_target_preserves_status_and_entries_still_run(self) -> None:
        self.current["target_bevy"] = {"status": "pre-release", "since": "2026-10-01"}
        del self.fetch.bodies[f"{API}/bevy/versions"]
        self.current["dependencies"] = {"adapter": entry()}
        self.fetch.crate("adapter")
        result = self.run_pass()
        self.assertEqual(result.record["target_bevy"], {"status": "pre-release", "since": "2026-10-01"})
        self.assertEqual(result.record["target_error"], "crates.io: no fixture")
        self.assertEqual(result.record["dependencies"]["adapter"]["crates_io"]["status"], "read")
        self.assertEqual((result.asked, result.failed), (1, 0))
        self.assertEqual(self.fetch.urls[0], f"{API}/bevy/versions")
        self.fetch.bodies[f"{API}/bevy/versions"] = {"versions": []}
        _ = self.run_pass()
        self.assertEqual(self.current["target_error"], "")

    def test_entries_order_unread_then_oldest_check_then_name(self) -> None:
        self.released_target()
        self.current["dependencies"] = {name: entry() for name in ("z-unread", "a-unread", "z-old", "a-old", "new")}
        for name in ("z-old", "a-old", "new"):
            self.current["dependencies"][name]["crates_io"] = read_fact(NOW if name == "new" else OLD)
        for name in self.current["dependencies"]:
            self.fetch.crate(name)
        _ = self.run_pass()
        info_urls = [url for url in self.fetch.urls if not url.endswith("/dependencies")]
        self.assertEqual(info_urls, [f"{API}/{name}" for name in ("a-unread", "z-unread", "a-old", "z-old", "new")])

    def test_budget_stops_before_request_rolls_back_current_entry_and_resumes_unread_first(self) -> None:
        self.assertEqual(upgrade_check.CHECK_BUDGET, timedelta(seconds=180))
        self.released_target()
        self.current["checked"] = OLD.isoformat()
        self.current["dependencies"] = {name: entry() for name in ("a", "b", "c")}
        self.current["dependencies"]["b"]["error"] = "keep this error"
        before = copy.deepcopy(self.current["dependencies"])
        for name in ("a", "b", "c"):
            self.fetch.crate(name)
        result = self.run_pass(StepClock(step=timedelta(seconds=50)))
        self.assertEqual(self.fetch.urls, [f"{API}/a", f"{API}/a/1.0.0/dependencies", f"{API}/b"])
        self.assertEqual(result.record["dependencies"]["a"]["crates_io"]["status"], "read")
        self.assertEqual(result.record["dependencies"]["b"], before["b"])
        self.assertEqual(result.record["dependencies"]["c"], before["c"])
        self.assertEqual(result.record["checked"], OLD.isoformat())
        self.assertEqual(result.asked, 2)
        self.assertEqual(result.failed, 0)
        self.fetch.urls.clear()
        second = self.run_pass(StepClock(start=NOW + timedelta(minutes=5)))
        self.assertEqual(self.fetch.urls, [f"{API}/b", f"{API}/b/1.0.0/dependencies",
                                          f"{API}/c", f"{API}/c/1.0.0/dependencies", f"{API}/a"])
        self.assertEqual(second.record["checked"], (NOW + timedelta(minutes=5)).isoformat())

    def test_spent_budget_before_target_preserves_entire_record(self) -> None:
        self.current["dependencies"] = {"adapter": entry()}
        self.current["target_error"] = "keep target error"
        before = copy.deepcopy(self.current)
        result = self.run_pass(StepClock(step=timedelta(seconds=181)))
        self.assertEqual(self.fetch.urls, [])
        self.assertEqual(result.record, before)
        self.assertEqual((result.asked, result.failed), (0, 0))

    def github_fixtures(self, name: str = "adapter", rev: str = "abcdef0123456789",
                        since: str = "2026-10-01") -> tuple[str, str, str]:
        base = f"https://api.github.com/repos/owner/{name}"
        manifest = f"https://raw.githubusercontent.com/owner/{name}/{rev}/Cargo.toml"
        items = f"{base}/issues?state=all&since={since}T00:00:00Z&sort=updated&direction=desc&per_page=100"
        self.fetch.bodies[base] = {"default_branch": "main"}
        self.fetch.bodies[f"{base}/commits/main"] = {
            "sha": rev, "commit": {"committer": {"date": "2026-10-11T00:00:00Z"}}}
        self.fetch.bodies[manifest] = '[dependencies]\nbevy = "^0.20"\n'
        self.fetch.bodies[items] = []
        return base, manifest, items

    def github_crate(self, name: str = "adapter") -> None:
        self.fetch.crate(name)
        self.fetch.bodies[f"{API}/{name}"] = {
            "crate": {"repository": f"https://github.com/owner/{name}.git"},
            "versions": [{"num": "1.0.0", "yanked": False}]}

    def test_old_releases_read_github_then_cache_branch_manifest_and_unchanged_pull(self) -> None:
        self.released_target()
        current = entry()
        self.current["dependencies"] = {"adapter": current}
        self.github_crate()
        base, manifest, items = self.github_fixtures()
        self.fetch.bodies[items] = [{"number": 7, "title": "Port v0.20.0", "state": "open",
            "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-10-11T00:00:00Z",
            "pull_request": {}}]
        self.fetch.bodies[f"{base}/pulls/7"] = {"state": "closed", "merged": True,
                                              "head": {"ref": "port", "sha": "1234567890abcdef"}}
        result = self.run_pass()
        self.assertEqual((result.asked, result.failed), (1, 0))
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{API}/adapter/1.0.0/dependencies",
            base, f"{base}/commits/main", manifest, items, f"{base}/pulls/7"])
        self.assertEqual(current["branch"], {"status": "read", "repository": "owner/adapter", "name": "main", "rev": "abcdef0123456789",
            "committed": "2026-10-11", "requirement": "^0.20"})
        self.assertEqual(current["items"][0]["state"], "merged")
        self.assertEqual(current["items"][0]["head"], "port @ 1234567")
        before = copy.deepcopy(current["items"])
        self.fetch.urls.clear()
        _ = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{base}/commits/main", items])
        self.assertEqual(current["items"], before)

    def test_changed_head_and_updated_or_new_pulls_are_read_again(self) -> None:
        self.released_target()
        current = entry()
        current["crates_io"] = read_fact()
        current["branch"] = {"status": "read", "repository": "owner/adapter", "name": "main", "rev": "old",
                            "committed": "2026-10-01", "requirement": "^0.19"}
        current["items"] = [{"number": 7, "kind": "pr", "state": "open", "title": "Port 0.20",
                             "updated": "old", "head": "old @ 1234567"}]
        self.current["dependencies"] = {"adapter": current}
        self.github_crate()
        base, manifest, items = self.github_fixtures(rev="newrevision")
        self.fetch.bodies[items] = [
            {"number": n, "title": "Port 0.20", "state": "open", "created_at": "2026-10-02T00:00:00Z",
             "updated_at": "new", "pull_request": {}} for n in (8, 7)]
        for n in (8, 7):
            self.fetch.bodies[f"{base}/pulls/{n}"] = {"state": "open", "merged": False,
                                                   "head": {"ref": "port", "sha": "123456789abcdef"}}
        _ = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{base}/commits/main", manifest,
                                          items, f"{base}/pulls/8", f"{base}/pulls/7"])
        branch = current["branch"]
        assert branch["status"] == "read"
        self.assertEqual(branch["rev"], "newrevision")
        self.assertEqual({value["number"] for value in current["items"]}, {7, 8})

    def test_unlisted_git_crate_uses_pin_repository_and_checks_crates_io_each_pass(self) -> None:
        self.released_target()
        current = entry()
        current["pin"] = {"source": "git", "version": "1.0.0",
                          "url": "https://github.com/owner/adapter.git", "ref": "branch=port", "rev": "pinned"}
        self.current["dependencies"] = {"adapter": current}
        self.fetch.bodies[f"{API}/adapter"] = None
        current["error"] = "earlier crates.io failure"
        base, manifest, items = self.github_fixtures()
        result = self.run_pass()
        self.assertEqual(current["crates_io"], {"status": "unlisted", "checked": NOW.isoformat(),
                                               "repository": "owner/adapter"})
        self.assertEqual(current["error"], "")
        self.assertEqual((result.asked, result.failed), (1, 0))
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", base, f"{base}/commits/main", manifest, items])
        self.fetch.urls.clear()
        _ = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{base}/commits/main", items])
        self.fetch.urls.clear()
        self.github_crate()
        self.fetch.bodies[f"{API}/adapter/1.0.0/dependencies"] = {
            "dependencies": [{"crate_id": "bevy_ecs", "kind": "normal", "req": "^0.20"}]}
        before_branch, before_items = copy.deepcopy(current["branch"]), copy.deepcopy(current["items"])
        _ = self.run_pass()
        self.assertEqual(current["branch"], before_branch)
        self.assertEqual(current["items"], before_items)
        self.assertEqual(current["crates_io"]["status"], "read")
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{API}/adapter/1.0.0/dependencies"])

    def test_unlisted_registry_crate_has_no_repository_or_failure(self) -> None:
        self.released_target()
        self.current["dependencies"] = {"adapter": entry()}
        self.fetch.bodies[f"{API}/adapter"] = None
        result = self.run_pass()
        self.assertEqual(result.record["dependencies"]["adapter"]["crates_io"],
                         {"status": "unlisted", "checked": NOW.isoformat(), "repository": ""})
        self.assertEqual(result.record["dependencies"]["adapter"]["error"], "")
        self.assertEqual((result.asked, result.failed), (1, 0))
        self.assertEqual(self.fetch.urls, [f"{API}/adapter"])

    def test_not_found_dependencies_still_fail_and_keep_whole_entry(self) -> None:
        self.released_target()
        current = entry()
        current["crates_io"] = read_fact()
        before = copy.deepcopy(current)
        self.current["dependencies"] = {"adapter": current}
        self.fetch.crate("adapter", ("2.0.0", "1.0.0"))
        self.fetch.bodies[f"{API}/adapter/2.0.0/dependencies"] = None
        result = self.run_pass()
        before["error"] = "crates.io: HTTP 404"
        self.assertEqual(current, before)
        self.assertEqual((result.asked, result.failed), (1, 1))

    def test_github_failures_roll_back_crates_io_branch_and_items_and_continue(self) -> None:
        for failing_step in ("repository", "head", "manifest", "items", "pull"):
            with self.subTest(step=failing_step):
                self.fetch = CheckFetch()
                self.released_target()
                current = entry()
                current["crates_io"] = read_fact()
                current["note"] = "keep this"
                self.current["dependencies"] = {"adapter": current, "good": entry()}
                self.github_crate()
                self.fetch.crate("good")
                base, manifest, items = self.github_fixtures()
                self.fetch.bodies[items] = [{"number": 7, "title": "Port 0.20", "state": "open",
                    "created_at": "2026-10-02T00:00:00Z", "updated_at": "new", "pull_request": {}}]
                self.fetch.bodies[f"{base}/pulls/7"] = {"state": "open", "merged": False,
                    "head": {"ref": "port", "sha": "abcdef0123456"}}
                url = {"repository": base, "head": f"{base}/commits/main", "manifest": manifest,
                       "items": items, "pull": f"{base}/pulls/7"}[failing_step]
                self.fetch.bodies[url] = upstream.FetchFailure(f"{urlsplit(url).hostname}: HTTP 500")
                before = copy.deepcopy(current)
                result = self.run_pass()
                before["error"] = f"{urlsplit(url).hostname}: HTTP 500"
                self.assertEqual(current, before)
                self.assertEqual(self.current["dependencies"]["good"]["crates_io"]["status"], "read")
                self.assertEqual((result.asked, result.failed), (2, 1))

    def test_done_patched_path_and_ready_keep_stored_github_facts_without_requests(self) -> None:
        self.released_target()
        values = {name: entry() for name in ("done", "patched", "path", "ready")}
        for value in values.values():
            facts = read_fact()
            facts["repository"] = "owner/adapter"
            value["crates_io"] = facts
            value["branch"] = {"status": "read", "repository": "owner/adapter", "name": "main", "rev": "old",
                               "committed": "2026-10-01", "requirement": "^0.20"}
            value["items"] = [{"number": 7, "kind": "pr", "state": "open", "title": "Port",
                              "updated": "old", "head": "port @ 1234567"}]
        values["done"]["bevy"] = "0.20.0"
        values["patched"]["patched_bevy"] = True
        values["path"]["pin"] = {"source": "path", "version": "1.0.0", "path": "vendor/adapter"}
        ready_facts = read_fact(ready="1.0.0")
        ready_facts["repository"] = "owner/adapter"
        values["ready"]["crates_io"] = ready_facts
        self.current["dependencies"] = values
        before = copy.deepcopy(values)
        _ = self.run_pass()
        self.assertEqual(self.fetch.urls, [])
        self.assertEqual(values, before)

    def test_no_repository_preserves_prior_branch_and_items_without_github_request(self) -> None:
        self.released_target()
        current = entry()
        current["branch"] = {"status": "read", "repository": "owner/adapter", "name": "old", "rev": "old",
                            "committed": "2026-10-01", "requirement": "^0.19"}
        current["items"] = [{"number": 1, "kind": "issue", "state": "open",
                             "title": "Port", "updated": "old", "head": ""}]
        before = copy.deepcopy(current)
        self.current["dependencies"] = {"adapter": current}
        self.fetch.crate("adapter")
        _ = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{API}/adapter/1.0.0/dependencies"])
        self.assertEqual(current["branch"], before["branch"])
        self.assertEqual(current["items"], before["items"])

    def test_item_since_uses_target_first_date_or_watch_start_date(self) -> None:
        targets: tuple[upgrade_record.TargetBevy, ...] = (
            {"status": "pre-release", "since": "2026-10-01"},
            {"status": "released", "version": "0.20.0", "date": "2026-10-12", "since": "2026-10-01"},
            {"status": "unpublished"}, {"status": "unread"},
        )
        for target in targets:
            with self.subTest(target=target):
                self.fetch = CheckFetch()
                self.current["target_bevy"] = target
                self.fetch.bodies[f"{API}/bevy/versions"] = None
                self.current["dependencies"] = {"adapter": entry()}
                self.github_crate()
                since = target["since"] if (target["status"] == "released" or target["status"] == "pre-release") else OLD.date().isoformat()
                _, _, items = self.github_fixtures(since=since)
                _ = self.run_pass()
                self.assertIn(items, self.fetch.urls)

    def test_budget_in_github_leaves_whole_entry_and_complete_check_time_unchanged(self) -> None:
        self.released_target()
        self.current["checked"] = OLD.isoformat()
        current = entry()
        current["error"] = "keep this"
        self.current["dependencies"] = {"adapter": current}
        self.github_crate()
        base, _, _ = self.github_fixtures()
        before = copy.deepcopy(current)
        result = self.run_pass(StepClock(step=timedelta(seconds=50)))
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{API}/adapter/1.0.0/dependencies", base])
        self.assertEqual(current, before)
        self.assertEqual(result.record["checked"], OLD.isoformat())
        self.assertEqual((result.asked, result.failed), (1, 0))

    def test_repository_change_reads_new_branch_manifest_and_matching_pull_without_old_items(self) -> None:
        self.released_target()
        current = entry()
        current["crates_io"] = read_fact()
        current["branch"] = {"status": "read", "repository": "previous/adapter", "name": "master",
                            "rev": "abcdef0123456789", "committed": "2026-10-01", "requirement": "^0.19"}
        current["items"] = [
            {"number": 7, "kind": "pr", "state": "closed", "title": "Old port", "updated": "same",
             "head": "old @ 1234567"},
            {"number": 99, "kind": "issue", "state": "open", "title": "Old issue", "updated": "same", "head": ""},
        ]
        self.current["dependencies"] = {"adapter": current}
        self.github_crate()
        base, manifest, items = self.github_fixtures()
        self.fetch.bodies[items] = [{"number": 7, "title": "Port 0.20", "state": "open",
                                    "created_at": "2026-10-02T00:00:00Z", "updated_at": "same", "pull_request": {}}]
        self.fetch.bodies[f"{base}/pulls/7"] = {"state": "open", "merged": False,
                                              "head": {"ref": "new-port", "sha": "76543210abcdef"}}
        result = self.run_pass()
        self.assertEqual((result.asked, result.failed), (1, 0))
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", base, f"{base}/commits/main",
                                          manifest, items, f"{base}/pulls/7"])
        self.assertEqual(current["branch"], {"status": "read", "repository": "owner/adapter", "name": "main",
            "rev": "abcdef0123456789", "committed": "2026-10-11", "requirement": "^0.20"})
        self.assertEqual(current["items"], [{"number": 7, "kind": "pr", "state": "open", "title": "Port 0.20",
                                           "updated": "same", "head": "new-port @ 7654321"}])
        self.assertEqual(current["error"], "")

    def test_missing_stored_branch_refreshes_default_name_and_keeps_same_revision_manifest_cached(self) -> None:
        self.released_target()
        current = entry()
        current["crates_io"] = read_fact()
        current["branch"] = {"status": "read", "repository": "owner/adapter", "name": "master",
                            "rev": "abcdef0123456789", "committed": "2026-10-01", "requirement": "^0.19"}
        current["error"] = "previous failure"
        self.current["dependencies"] = {"adapter": current}
        self.github_crate()
        base, _, items = self.github_fixtures()
        self.fetch.bodies[f"{base}/commits/master"] = None
        result = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{base}/commits/master", base,
                                          f"{base}/commits/main", items])
        self.assertEqual(current["branch"], {"status": "read", "repository": "owner/adapter", "name": "main",
            "rev": "abcdef0123456789", "committed": "2026-10-11", "requirement": "^0.19"})
        self.assertEqual(current["error"], "")
        self.assertEqual((result.asked, result.failed), (1, 0))

    def test_missing_stored_and_refreshed_branch_keeps_all_facts_and_records_failure(self) -> None:
        self.released_target()
        current = entry()
        current["crates_io"] = read_fact()
        current["branch"] = {"status": "read", "repository": "owner/adapter", "name": "master",
                            "rev": "oldrev", "committed": "2026-10-01", "requirement": "^0.19"}
        current["items"] = [{"number": 7, "kind": "pr", "state": "open", "title": "Port 0.20",
                             "updated": "old", "head": "port @ 1234567"}]
        self.current["dependencies"] = {"adapter": current}
        self.github_crate()
        base, _, _ = self.github_fixtures()
        self.fetch.bodies[f"{base}/commits/master"] = None
        self.fetch.bodies[f"{base}/commits/main"] = None
        before = copy.deepcopy(current)
        result = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{base}/commits/master", base, f"{base}/commits/main"])
        before["error"] = "api.github.com: HTTP 404"
        self.assertEqual(current, before)
        self.assertEqual((result.asked, result.failed), (1, 1))

    def test_issues_response_keeps_stored_pull_absent_from_this_answer(self) -> None:
        self.released_target()
        current = entry()
        current["crates_io"] = read_fact()
        current["branch"] = {"status": "read", "repository": "owner/adapter", "name": "main",
                            "rev": "abcdef0123456789", "committed": "2026-10-01", "requirement": "^0.19"}
        stored: upstream.Item = {"number": 7, "kind": "pr", "state": "open", "title": "Port 0.20",
                                 "updated": "old", "head": "port @ 1234567"}
        expected_stored = copy.deepcopy(stored)
        current["items"] = [stored]
        self.current["dependencies"] = {"adapter": current}
        self.github_crate()
        base, _, items = self.github_fixtures()
        self.fetch.bodies[items] = [{"number": 9, "title": "Track 0.20", "state": "open",
                                    "created_at": "2026-10-02T00:00:00Z", "updated_at": "new"}]
        result = self.run_pass()
        self.assertEqual(self.fetch.urls, [f"{API}/adapter", f"{base}/commits/main", items])
        self.assertEqual(sorted(current["items"], key=lambda item: item["number"]), [expected_stored,
            {"number": 9, "kind": "issue", "state": "open", "title": "Track 0.20", "updated": "new", "head": ""}])
        self.assertEqual((result.asked, result.failed), (1, 0))


if __name__ == "__main__":
    _ = unittest.main()
