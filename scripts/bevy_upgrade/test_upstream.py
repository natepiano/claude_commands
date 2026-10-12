"""Read canned crates.io responses through the public fetch boundary."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from email.message import Message
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
from typing import TYPE_CHECKING, final
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

import upstream

if TYPE_CHECKING:
    from typing import override
else:
    def override(method: object) -> object:
        return method

API = "https://crates.io/api/v1/crates"


@final
class CannedFetch:
    def __init__(self, bodies: dict[str, object]) -> None:
        self.bodies = bodies
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


@final
class UpstreamTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="bevy-upstream-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_crate_info_filters_yanked_and_preserves_api_order(self) -> None:
        url = f"{API}/adapter"
        fetch = CannedFetch({url: {"crate": {"repository": "https://github.com/owner/project.git/tree/main"},
                                  "versions": [{"num": "3.0.0", "yanked": True},
                                               {"num": "2.0.0", "yanked": False},
                                               {"num": "1.0.0-rc.1", "yanked": False}]}})
        self.assertEqual(upstream.crate_info(fetch, "adapter"),
                         upstream.CrateInfo(("2.0.0", "1.0.0-rc.1"), "owner/project"))
        self.assertEqual(fetch.urls, [url])

    def test_repository_only_accepts_github_owner_and_name(self) -> None:
        repositories: tuple[tuple[object, str], ...] = (
            ("https://github.com/owner/project.git", "owner/project"),
            ("https://github.com/owner/project/", "owner/project"),
            ("https://github.com/owner/project/issues/4", "owner/project"),
            ("https://gitlab.com/owner/project", ""), (None, ""), ("", ""),
        )
        for repository, expected in repositories:
            with self.subTest(repository=repository):
                url = f"{API}/adapter"
                fetch = CannedFetch({url: {"crate": {"repository": repository}, "versions": []}})
                self.assertEqual(upstream.crate_info(fetch, "adapter").repository, expected)
                self.assertEqual(fetch.urls, [url])

    def test_requirement_prefers_non_dev_bevy_then_sorted_known_bevy_crate(self) -> None:
        url = f"{API}/adapter/1.0.0/dependencies"
        names = frozenset({"bevy", "bevy_app", "bevy_ecs"})
        dependencies: list[dict[str, object]] = [
            {"crate_id": "bevy_ecs", "kind": "normal", "req": "^0.19"},
            {"crate_id": "bevy", "kind": "dev", "req": "^0.99"},
            {"crate_id": "bevy_egui", "kind": "normal", "req": "^0.20"},
            {"crate_id": "bevy_app", "kind": "build", "req": "~0.20.1"},
        ]
        fetch = CannedFetch({url: {"dependencies": dependencies}})
        self.assertEqual(upstream.release_requirement(fetch, "adapter", "1.0.0", names), "~0.20.1")
        dependencies.append({"crate_id": "bevy", "kind": "normal", "req": "=0.21.0"})
        self.assertEqual(upstream.release_requirement(fetch, "adapter", "1.0.0", names), "=0.21.0")
        self.assertEqual(fetch.urls, [url, url])

    def test_requirement_prefers_non_dev_bevy_outside_known_names(self) -> None:
        url = f"{API}/adapter/1.0.0/dependencies"
        fetch = CannedFetch({url: {"dependencies": [
            {"crate_id": "bevy", "kind": "normal", "req": "^0.20"},
            {"crate_id": "bevy_ecs", "kind": "normal", "req": "^0.19"},
        ]}})
        self.assertEqual(upstream.release_requirement(fetch, "adapter", "1.0.0", frozenset({"bevy_ecs"})), "^0.20")
        self.assertEqual(fetch.urls, [url])

    def test_requirement_ignores_dev_bevy_and_unknown_bevy_egui(self) -> None:
        url = f"{API}/adapter/1.0.0/dependencies"
        fetch = CannedFetch({url: {"dependencies": [
            {"crate_id": "bevy", "kind": "dev", "req": "^0.20"},
            {"crate_id": "bevy_egui", "kind": "normal", "req": "^0.20"},
            {"crate_id": "bevy_ecs", "kind": "normal", "req": "^0.19"},
        ]}})
        self.assertEqual(upstream.release_requirement(fetch, "adapter", "1.0.0", frozenset({"bevy_ecs"})), "^0.19")
        self.assertEqual(fetch.urls, [url])

    def test_unknown_and_dev_only_dependencies_have_no_bevy_requirement(self) -> None:
        url = f"{API}/adapter/1.0.0/dependencies"
        fetch = CannedFetch({url: {"dependencies": [
            {"crate_id": "bevy_egui", "kind": "normal", "req": "^0.20"},
            {"crate_id": "bevy_ecs", "kind": "dev", "req": "^0.20"},
        ]}})
        self.assertEqual(upstream.release_requirement(fetch, "adapter", "1.0.0", frozenset({"bevy_ecs"})), "")
        self.assertEqual(fetch.urls, [url])

    def test_target_results_use_first_target_date_and_newest_stable(self) -> None:
        url = f"{API}/bevy/versions"
        versions: list[dict[str, str]] = [
            {"num": "0.200.0", "created_at": "2026-10-12T10:00:00Z"},
            {"num": "0.19.9", "created_at": "2026-10-11T10:00:00Z"},
        ]
        fetch = CannedFetch({url: {"versions": versions}})
        self.assertEqual(upstream.target_release(fetch, "0.20"), {"status": "unpublished"})
        versions.extend([
            {"num": "0.20.0-rc.2", "created_at": "2026-10-10T10:00:00Z"},
            {"num": "0.20.0-rc.1", "created_at": "2026-10-01T10:00:00Z"},
        ])
        self.assertEqual(upstream.target_release(fetch, "0.20"), {"status": "pre-release", "since": "2026-10-01"})
        versions[:0] = [
            {"num": "0.20.2", "created_at": "2026-10-12T10:00:00Z"},
            {"num": "0.20.1", "created_at": "2026-10-11T10:00:00Z"},
            {"num": "0.20.0", "created_at": "2026-10-10T10:00:00Z"},
        ]
        self.assertEqual(upstream.target_release(fetch, "0.20"), {
            "status": "released", "version": "0.20.2", "date": "2026-10-12", "since": "2026-10-01",
        })
        self.assertEqual(fetch.urls, [url, url, url])

    def test_fixture_fetch_serves_json_bodies_and_refuses_missing_urls(self) -> None:
        url = f"{API}/adapter"
        body = {"crate": {"repository": None}, "versions": [{"num": "1.0.0", "yanked": False}]}
        path = self.root / "fixtures.json"
        _ = path.write_text(json.dumps({url: body}), encoding="utf-8")
        fetch = upstream.fixture_fetch(path)
        self.assertEqual(upstream.crate_info(fetch, "adapter"), upstream.CrateInfo(("1.0.0",), ""))
        with self.assertRaises(upstream.FetchFailure) as caught:
            _ = fetch("https://example.invalid/missing")
        self.assertEqual(str(caught.exception), "example.invalid: no fixture")

    def test_fetch_failures_propagate_from_every_lookup(self) -> None:
        fetch = CannedFetch({})
        for lookup in ("info", "requirement", "target"):
            with self.subTest(lookup=lookup), self.assertRaises(upstream.FetchFailure):
                if lookup == "info":
                    _ = upstream.crate_info(fetch, "adapter")
                elif lookup == "requirement":
                    _ = upstream.release_requirement(fetch, "adapter", "1.0.0", frozenset({"bevy_ecs"}))
                else:
                    _ = upstream.target_release(fetch, "0.20")

    def test_http_fetch_sets_user_agent_timeout_and_returns_bytes(self) -> None:
        requests: list[tuple[Request, int]] = []
        def urlopen(request: Request, *, timeout: int) -> io.BytesIO:
            requests.append((request, timeout))
            return io.BytesIO(b'{"ok":true}')
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(upstream.http_fetch(f"{API}/adapter"), b'{"ok":true}')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0].full_url, f"{API}/adapter")
        self.assertEqual(requests[0][0].get_header("User-agent"), "bevy-upgrade (natepiano)")
        self.assertEqual(requests[0][1], 30)

    def test_truncated_response_read_is_a_host_qualified_fetch_failure(self) -> None:
        response = io.BytesIO(b"")
        with mock.patch.object(response, "read", side_effect=IncompleteRead(b"partial")), \
                mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(upstream.FetchFailure) as caught:
                _ = upstream.http_fetch(f"{API}/adapter")
        self.assertTrue(str(caught.exception).startswith("crates.io: "))

    def test_malformed_http_status_is_a_host_qualified_fetch_failure(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=BadStatusLine("bad")):
            with self.assertRaises(upstream.FetchFailure) as caught:
                _ = upstream.http_fetch(f"{API}/adapter")
        self.assertTrue(str(caught.exception).startswith("crates.io: "))

    def test_http_errors_are_host_qualified_fetch_failures(self) -> None:
        url = f"{API}/adapter"
        for error in (HTTPError(url, 503, "unavailable", Message(), None), URLError("offline")):
            with self.subTest(error=error), mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(upstream.FetchFailure) as caught:
                    _ = upstream.http_fetch(url)
                self.assertTrue(str(caught.exception).startswith("crates.io: "))
                if isinstance(error, HTTPError):
                    self.assertEqual(str(caught.exception), "crates.io: HTTP 503")

    def test_http_not_found_is_distinct_from_other_http_failures(self) -> None:
        url = f"{API}/missing"
        for code in (404, 500):
            with self.subTest(code=code), mock.patch("urllib.request.urlopen",
                    side_effect=HTTPError(url, code, "failure", Message(), None)):
                with self.assertRaises(upstream.FetchFailure) as caught:
                    _ = upstream.http_fetch(url)
                self.assertEqual(str(caught.exception), f"crates.io: HTTP {code}")
                self.assertEqual(isinstance(caught.exception, upstream.NotFound), code == 404)

    def test_null_fixture_means_not_found_and_missing_fixture_is_failure(self) -> None:
        url = f"{API}/missing"
        path = self.root / "fixtures.json"
        _ = path.write_text(json.dumps({url: None}), encoding="utf-8")
        fetch = upstream.fixture_fetch(path)
        with self.assertRaises(upstream.NotFound) as caught:
            _ = fetch(url)
        self.assertEqual(str(caught.exception), "crates.io: HTTP 404")
        with self.assertRaises(upstream.FetchFailure) as missing:
            _ = fetch(f"{API}/unknown")
        self.assertNotIsInstance(missing.exception, upstream.NotFound)

    def test_public_repository_parser_removes_suffix_and_trailing_path(self) -> None:
        for url, expected in (
            ("https://github.com/owner/project.git/tree/main", "owner/project"),
            ("https://github.com/owner/project/", "owner/project"),
            ("https://gitlab.com/owner/project", ""), ("https://github.com/owner", ""), ("", ""),
        ):
            with self.subTest(url=url):
                self.assertEqual(upstream.github_repository(url), expected)

    def test_default_branch_and_commit_head_use_repository_urls(self) -> None:
        base = "https://api.github.com/repos/owner/project"
        fetch = CannedFetch({base: {"default_branch": "main"},
                            f"{base}/commits/main": {"sha": "abcdef0123456789",
                                "commit": {"committer": {"date": "2026-10-11T12:34:56Z"}}}})
        self.assertEqual(upstream.default_branch(fetch, "owner/project"), "main")
        self.assertEqual(upstream.branch_head(fetch, "owner/project", "main"),
                         upstream.BranchHead("abcdef0123456789", "2026-10-11"))
        self.assertEqual(fetch.urls, [base, f"{base}/commits/main"])

    def test_manifest_requirement_prefers_bevy_and_resolves_workspace_inheritance(self) -> None:
        url = "https://raw.githubusercontent.com/owner/project/abcdef/Cargo.toml"
        manifests = (
            ('[dependencies]\nbevy = "^0.20"\nbevy_ecs = "^0.19"\n', "^0.20"),
            ('[dependencies]\nbevy = { workspace = true }\n'
             + '[workspace.dependencies]\nbevy = { version = "~0.20.1" }\n', "~0.20.1"),
            ('[workspace.dependencies]\nbevy = "=0.20.0"\n', "=0.20.0"),
            ('[dependencies]\nbevy_ecs = { version = "^0.19" }\nbevy_app = "^0.20"\n', "^0.20"),
            ('[dependencies]\nbevy = { path = "../bevy" }\n'
             + 'bevy_ecs = { git = "https://example.invalid/bevy" }\nbevy_egui = "^0.20"\n', ""),
        )
        for manifest, expected in manifests:
            with self.subTest(manifest=manifest):
                fetch = CannedFetch({url: manifest})
                self.assertEqual(upstream.manifest_requirement(fetch, "owner/project", "abcdef",
                                 frozenset({"bevy_ecs", "bevy_app"})), expected)
                self.assertEqual(fetch.urls, [url])

    def test_invalid_manifest_is_a_host_qualified_fetch_failure(self) -> None:
        url = "https://raw.githubusercontent.com/owner/project/abcdef/Cargo.toml"
        fetch = CannedFetch({url: "[broken"})
        with self.assertRaises(upstream.FetchFailure) as caught:
            _ = upstream.manifest_requirement(fetch, "owner/project", "abcdef", frozenset({"bevy_ecs"}))
        self.assertTrue(str(caught.exception).startswith("raw.githubusercontent.com: "))

    def test_missing_manifest_names_no_requirement_but_server_failure_is_preserved(self) -> None:
        url = "https://raw.githubusercontent.com/owner/project/abcdef/Cargo.toml"
        fetch = CannedFetch({url: None})
        self.assertEqual(upstream.manifest_requirement(fetch, "owner/project", "abcdef",
                         frozenset({"bevy_ecs"})), "")
        self.assertEqual(fetch.urls, [url])
        fetch = CannedFetch({url: upstream.FetchFailure("raw.githubusercontent.com: HTTP 500")})
        with self.assertRaises(upstream.FetchFailure) as caught:
            _ = upstream.manifest_requirement(fetch, "owner/project", "abcdef", frozenset({"bevy_ecs"}))
        self.assertNotIsInstance(caught.exception, upstream.NotFound)
        self.assertEqual(str(caught.exception), "raw.githubusercontent.com: HTTP 500")
        self.assertEqual(fetch.urls, [url])

    def test_port_items_filter_title_substrings_and_creation_date(self) -> None:
        url = ("https://api.github.com/repos/owner/project/issues?state=all&since=2026-10-01T00:00:00Z"
               "&sort=updated&direction=desc&per_page=100")
        def item(number: int, title: str, created: str, pr: bool = False) -> dict[str, object]:
            result: dict[str, object] = {"number": number, "title": title, "state": "open",
                "created_at": created, "updated_at": "2026-10-12T00:00:00Z"}
            if pr:
                result["pull_request"] = {"url": "unused"}
            return result
        fetch = CannedFetch({url: [
            item(7, "Migrate to Bevy v0.20.0", "2026-10-01T00:00:00Z", True),
            item(6, "Port to 0.20", "2026-09-30T23:59:59Z"),
            item(5, "Unrelated work", "2026-10-12T00:00:00Z"),
            item(4, "Track 0.20 upgrade", "2026-10-02T00:00:00Z"),
        ]})
        self.assertEqual(upstream.port_items(fetch, "owner/project", "0.20", "2026-10-01"), [
            {"number": 7, "kind": "pr", "state": "open", "title": "Migrate to Bevy v0.20.0",
             "updated": "2026-10-12T00:00:00Z", "head": ""},
            {"number": 4, "kind": "issue", "state": "open", "title": "Track 0.20 upgrade",
             "updated": "2026-10-12T00:00:00Z", "head": ""},
        ])
        self.assertEqual(fetch.urls, [url])

    def test_pull_state_reports_merged_closed_and_open_with_short_head(self) -> None:
        url = "https://api.github.com/repos/owner/project/pulls/7"
        for state, merged, expected in (("closed", True, "merged"), ("closed", False, "closed"),
                                         ("open", False, "open")):
            with self.subTest(state=state, merged=merged):
                fetch = CannedFetch({url: {"state": state, "merged": merged,
                                          "head": {"ref": "port", "sha": "abcdef0123456789"}}})
                self.assertEqual(upstream.pull_state(fetch, "owner/project", 7), (expected, "port @ abcdef0"))
                self.assertEqual(fetch.urls, [url])

    def test_github_missing_or_malformed_bodies_use_fetch_failure_boundary(self) -> None:
        base = "https://api.github.com/repos/owner/project"
        cases = (
            (base, "branch"), (f"{base}/commits/main", "head"),
            (f"{base}/pulls/7", "pull"),
            (f"{base}/issues?state=all&since=2026-10-01T00:00:00Z&sort=updated&direction=desc&per_page=100", "items"),
        )
        bodies: tuple[object, ...] = ({}, "invalid JSON", None)
        for url, lookup in cases:
            for body in bodies:
                with self.subTest(lookup=lookup, body=body):
                    fetch = CannedFetch({url: body})
                    with self.assertRaises(upstream.FetchFailure):
                        if lookup == "branch":
                            _ = upstream.default_branch(fetch, "owner/project")
                        elif lookup == "head":
                            _ = upstream.branch_head(fetch, "owner/project", "main")
                        elif lookup == "pull":
                            _ = upstream.pull_state(fetch, "owner/project", 7)
                        else:
                            _ = upstream.port_items(fetch, "owner/project", "0.20", "2026-10-01")


if __name__ == "__main__":
    _ = unittest.main()
