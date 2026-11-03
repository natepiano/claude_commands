"""HTTP replay tests; no request may leave this process.

Synthetic fixture responses follow https://api.typesafe.ai/openapi.json and
https://docs.typesafe.ai/api/system-one.md; they were not recorded by a live call.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from typing import cast, override
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

from scripts.issue_score import client, questions

FIXTURES = Path(__file__).with_name("fixtures")
MODEL = "jev-1.14.0"
FAKE_KEY = "fixture-only-not-a-secret"


class FixtureHTTPResponse(io.BytesIO):
    headers: Message
    status: int = 200

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = Message()


class ClientTests(unittest.TestCase):
    root: Path = Path()
    ledger: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ledger = self.root / "spend.jsonl"
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": FAKE_KEY})))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch.object(client, "urlopen", side_effect=AssertionError("Live HTTP forbidden")))

    def call(self) -> client.Response:
        return client.system_one(
            {"issue": {"title": "Rendering fix", "body": "Repair a missing image.",
                       "project": "hana", "project_about": "A creative tool."}},
            questions.main_questions(),
            MODEL,
            ledger_path=self.ledger,
        )

    def error(self, status: int, *, retry_after: str = "") -> HTTPError:
        headers = Message()
        if retry_after:
            headers["Retry-After"] = retry_after
        return HTTPError("https://api.typesafe.ai/v1/systemone", status, "fixture error", headers,
                         io.BytesIO((FIXTURES / "http_500.json").read_bytes()))

    def test_missing_key_explains_wrapper_without_making_a_request(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(client, "urlopen") as http:
            with self.assertRaisesRegex(client.TypeSafeError,
                                        "^TYPESAFE_API_KEY not set: run under with-typesafe$"):
                _ = self.call()
        http.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_malformed_key_is_rejected_without_echoing_it_or_attempting_http(self) -> None:
        for separator in ("\n", "\r", "\t", " ", "\x01", "\x7f", "\u0085"):
            key = "fixture" + separator + "secret"
            with self.subTest(separator=repr(separator)), \
                    mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": key}), \
                    mock.patch.object(client, "urlopen", return_value=FixtureHTTPResponse(
                        (FIXTURES / "main_response.json").read_bytes())) as http:
                with self.assertRaises(client.TypeSafeError) as caught:
                    _ = self.call()
                self.assertEqual(str(caught.exception),
                                 "TYPESAFE_API_KEY is malformed: run under with-typesafe")
                self.assertNotIn(key, str(caught.exception))
                self.assertNotIn("Bearer", str(caught.exception))
                http.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_request_header_value_error_cannot_expose_authorization(self) -> None:
        with mock.patch.object(client, "urlopen", side_effect=ValueError("Invalid header Bearer " + FAKE_KEY)):
            with self.assertRaises(client.TypeSafeError) as caught:
                _ = self.call()
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertNotIn("Bearer", str(caught.exception))
        self.assertFalse(self.ledger.exists())

    def test_429_retries_then_decodes_response_and_records_only_success(self) -> None:
        with mock.patch.object(client, "urlopen", side_effect=[
            self.error(429, retry_after="3"), FixtureHTTPResponse((FIXTURES / "main_response.json").read_bytes()),
        ]) as http, mock.patch("time.sleep") as sleep:
            response = self.call()
        self.assertEqual(http.call_count, 2)
        sleep.assert_called_once_with(3.0)
        self.assertEqual(response["model"], MODEL)
        self.assertEqual(response["answers"]["current_harm"], {"type": "noul", "noul": 0.05})
        entries = [cast(dict[str, object], json.loads(line)) for line in self.ledger.read_text().splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["input_tokens"], 2000)
        self.assertAlmostEqual(cast(float, entries[0]["cost_usd"]), 0.000084)
        self.assertNotIn(FAKE_KEY, self.ledger.read_text())

    def test_request_is_gzipped_json_with_bearer_auth_and_no_key_in_payload(self) -> None:
        requests: list[Request] = []

        def replay(request: Request, timeout: float = 60) -> FixtureHTTPResponse:
            _ = timeout
            requests.append(request)
            return FixtureHTTPResponse((FIXTURES / "main_response.json").read_bytes())

        with mock.patch.object(client, "urlopen", side_effect=replay):
            _ = self.call()
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertEqual(request.get_header("Content-encoding"), "gzip")
        payload_bytes = gzip.decompress(cast(bytes, request.data))
        payload = cast(dict[str, object], json.loads(payload_bytes))
        self.assertEqual(payload["model"], MODEL)
        self.assertIn("current_harm", cast(dict[str, object], payload["questions"]))
        self.assertEqual(set(cast(dict[str, object], payload["state"])), {"issue"})
        self.assertNotIn(FAKE_KEY.encode(), payload_bytes)

    def test_500_raises_with_status_and_body_without_retry_or_charge(self) -> None:
        with mock.patch.object(client, "urlopen", side_effect=self.error(500)) as http, \
                mock.patch("time.sleep") as sleep:
            with self.assertRaises(client.TypeSafeError) as caught:
                _ = self.call()
        self.assertIn("500", str(caught.exception))
        self.assertIn("fixture upstream failure", str(caught.exception))
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        http.assert_called_once()
        sleep.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_gzipped_http_error_is_decoded_and_redacted(self) -> None:
        headers = Message()
        headers["Content-Encoding"] = "gzip"
        error = HTTPError("https://api.typesafe.ai/v1/systemone", 500, "fixture", headers,
                          io.BytesIO(gzip.compress(("upstream denied " + FAKE_KEY).encode())))
        with mock.patch.object(client, "urlopen", side_effect=error):
            with self.assertRaises(client.TypeSafeError) as caught:
                _ = self.call()
        self.assertIn("500", str(caught.exception))
        self.assertIn("upstream denied [redacted]", str(caught.exception))
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertFalse(self.ledger.exists())

    def test_corrupt_gzip_http_error_has_a_fixed_readable_message(self) -> None:
        for body in (b"not gzip", gzip.compress(b"truncated")[:12]):
            headers = Message()
            headers["Content-Encoding"] = "gzip"
            error = HTTPError("https://api.typesafe.ai/v1/systemone", 500, "fixture", headers,
                              io.BytesIO(body))
            with self.subTest(body=body), mock.patch.object(client, "urlopen", side_effect=error):
                with self.assertRaises(client.TypeSafeError) as caught:
                    _ = self.call()
                self.assertEqual(str(caught.exception), "TypeSafe HTTP 500: unreadable body")

    def test_retryable_statuses_stop_after_five_attempts(self) -> None:
        for status in (429, 529):
            with self.subTest(status=status), \
                    mock.patch.object(client, "urlopen", side_effect=[self.error(status) for _ in range(5)]) as http, \
                    mock.patch("time.sleep") as sleep:
                with self.assertRaises(client.TypeSafeError):
                    _ = self.call()
                self.assertEqual(http.call_count, 5)
                self.assertEqual(sleep.call_count, 4)
                delays = [cast(float, call.args[0]) for call in sleep.call_args_list]
                self.assertTrue(all(second > first for first, second in zip(delays, delays[1:])))
        self.assertFalse(self.ledger.exists())

    def test_budget_at_cap_refuses_before_http_and_preserves_ledger(self) -> None:
        prior = json.dumps({"input_tokens": 1, "cost_usd": questions.BUDGET_USD}) + "\n"
        _ = self.ledger.write_text(prior)
        with mock.patch.object(client, "urlopen") as http:
            with self.assertRaises(client.BudgetExceeded):
                _ = self.call()
        http.assert_not_called()
        self.assertEqual(self.ledger.read_text(), prior)

    def test_interrupted_pending_write_preserves_prior_reservations_and_allows_rerun(self) -> None:
        pending = self.ledger.with_suffix(".pending.json")
        earlier = {"unfinished-earlier-request": 0.25}
        _ = pending.write_text(json.dumps(earlier), encoding="utf-8")
        with mock.patch.object(os, "replace", side_effect=SystemExit("stopped before replace")), \
                mock.patch.object(client, "urlopen", return_value=FixtureHTTPResponse(
                    (FIXTURES / "main_response.json").read_bytes())) as http:
            with self.assertRaises(SystemExit):
                _ = self.call()
        http.assert_not_called()
        self.assertEqual(json.loads(pending.read_text()), earlier)

        def rerun(request: Request, timeout: float = 60) -> FixtureHTTPResponse:
            _ = request, timeout
            reserved = cast(dict[str, float], json.loads(pending.read_text()))
            self.assertEqual(reserved["unfinished-earlier-request"], 0.25)
            self.assertEqual(len(reserved), 2)
            return FixtureHTTPResponse((FIXTURES / "main_response.json").read_bytes())

        with mock.patch.object(client, "urlopen", side_effect=rerun) as http:
            response = self.call()
        http.assert_called_once()
        self.assertIn("impact", response["answers"])
        self.assertEqual(json.loads(pending.read_text()), earlier)
        self.assertEqual(len(self.ledger.read_text().splitlines()), 1)

    def test_budget_exhausted_during_backoff_refuses_the_retry(self) -> None:
        charged = json.dumps({"input_tokens": 1, "cost_usd": questions.BUDGET_USD}) + "\n"

        def concurrent_charge(delay: float) -> None:
            _ = delay
            _ = self.ledger.write_text(charged)

        with mock.patch.object(client, "urlopen", side_effect=[
            self.error(429), FixtureHTTPResponse((FIXTURES / "main_response.json").read_bytes()),
        ]) as http, mock.patch("time.sleep", side_effect=concurrent_charge) as sleep:
            with self.assertRaises(client.BudgetExceeded):
                _ = self.call()
        http.assert_called_once()
        sleep.assert_called_once()
        self.assertEqual(self.ledger.read_text(), charged)

    def test_error_response_cannot_echo_the_authorization_key(self) -> None:
        error = HTTPError("https://api.typesafe.ai/v1/systemone", 500, "fixture", Message(),
                          io.BytesIO(("Rejected Bearer " + FAKE_KEY).encode()))
        with mock.patch.object(client, "urlopen", side_effect=error):
            with self.assertRaises(client.TypeSafeError) as caught:
                _ = self.call()
        self.assertIn("500", str(caught.exception))
        self.assertNotIn(FAKE_KEY, str(caught.exception))


if __name__ == "__main__":
    _ = unittest.main()
