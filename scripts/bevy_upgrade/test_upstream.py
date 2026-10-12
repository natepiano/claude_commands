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
        return json.dumps(self.bodies[url]).encode()


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


if __name__ == "__main__":
    _ = unittest.main()
