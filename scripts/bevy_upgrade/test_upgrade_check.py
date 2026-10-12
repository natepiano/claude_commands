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
        return json.dumps(self.bodies[url]).encode()

    def crate(self, name: str, versions: tuple[str, ...] = ("1.0.0",), requirement: str = "^0.19") -> None:
        self.bodies[f"{API}/{name}"] = {"crate": {"repository": f"https://github.com/owner/{name}.git"},
                                       "versions": [{"num": version, "yanked": False} for version in versions]}
        for version in versions:
            self.bodies[f"{API}/{name}/{version}/dependencies"] = {
                "dependencies": [{"crate_id": "bevy_ecs", "kind": "normal", "req": requirement}],
            }


def entry(version: str = "1.0.0") -> upgrade_record.Entry:
    return {"pin": {"source": "registry", "version": version}, "bevy": "0.19.1", "via": [],
            "patched_bevy": False, "answer": None, "note": "", "crates_io": {"status": "unread"}, "error": ""}


def read_fact(checked: datetime = OLD, ready: str = "") -> upgrade_record.CratesIoRead:
    return {"status": "read", "checked": checked.isoformat(), "releases": {"1.0.0": "^0.19"},
            "newest": "1.0.0", "ready": ready, "repository": "old/repository"}


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
            "repository": "owner/adapter", "releases": {"3.0.0": "^0.19", "2.0.0": "^0.20.0", "1.0.0": "^0.19"},
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


if __name__ == "__main__":
    _ = unittest.main()
