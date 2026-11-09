"""Small typed HTTP boundary; credentials never leave request headers."""
from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import gzip
from http.client import HTTPResponse
import json
import math
import os
from pathlib import Path
import time
from tempfile import NamedTemporaryFile
from typing import Literal, TypedDict, cast
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
import zlib

from scripts.lib import conf
from scripts.lib.conf import UseConfiguredValue

from scripts.issue_score import questions as policy
from scripts.issue_score.questions import Question

def endpoint(scoring_config: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> str:
    scoring_config = conf.read("issue_score") if isinstance(scoring_config, UseConfiguredValue) else scoring_config
    return scoring_config.text("endpoint")


def spend_ledger(scoring_config: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Path:
    scoring_config = conf.read("issue_score") if isinstance(scoring_config, UseConfiguredValue) else scoring_config
    return Path(scoring_config.text("spend_ledger")).expanduser()


def max_attempts(scoring_config: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> int:
    scoring_config = conf.read("issue_score") if isinstance(scoring_config, UseConfiguredValue) else scoring_config
    return scoring_config.whole("max_attempts")


def request_timeout(scoring_config: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> float:
    scoring_config = conf.read("issue_score") if isinstance(scoring_config, UseConfiguredValue) else scoring_config
    return scoring_config.number("request_timeout")


def retry_backoff_base(scoring_config: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> float:
    scoring_config = conf.read("issue_score") if isinstance(scoring_config, UseConfiguredValue) else scoring_config
    return scoring_config.number("retry_backoff_base")


class TypeSafeError(RuntimeError):
    """The service did not provide a usable answer."""


class BudgetExceeded(TypeSafeError):
    """The authorized spend cannot accommodate another request."""


class NoulAnswer(TypedDict):
    type: Literal["noul"]
    noul: float


class ChoiceAnswer(TypedDict):
    type: Literal["choice"]
    choice: str
    confidence: float
    probabilities: dict[str, float]


class ScoreAnswer(TypedDict):
    type: Literal["score"]
    score: float
    confidence: float
    legend: dict[str, str]
    probabilities: dict[str, float]


Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


class Usage(TypedDict):
    input_tokens: int
    output_tokens: int


class Response(TypedDict):
    model: str
    answers: dict[str, Answer]
    usage: Usage


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeSafeError("Expected a JSON object")
    if any(not isinstance(key, str) for key in cast(dict[object, object], value)):
        raise TypeSafeError("Expected a JSON object")
    return cast(dict[str, object], value)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeSafeError("Expected a finite number")
    return float(value)


def _probability(value: object) -> float:
    number = _number(value)
    if not 0 <= number <= 1:
        raise TypeSafeError("Probability outside [0, 1]")
    return number


def _probabilities(value: object) -> dict[str, float]:
    result = {key: _probability(probability) for key, probability in _object(value).items()}
    if not result or abs(sum(result.values()) - 1) > 0.02:
        raise TypeSafeError("Invalid probability distribution")
    return result


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeSafeError("Expected a non-empty string")
    return value


def _usage(value: object) -> Usage:
    counts = _object(value)
    for name in ("input_tokens", "output_tokens"):
        count = counts.get(name)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise TypeSafeError("Invalid token usage")
    return {"input_tokens": cast(int, counts["input_tokens"]), "output_tokens": cast(int, counts["output_tokens"])}


def parse_response(value: object, questions: Mapping[str, Question]) -> Response:
    """Validate untrusted JSON before it reaches decision code."""
    raw = _object(value)
    answers = _object(raw.get("answers"))
    if answers.keys() != questions.keys():
        raise TypeSafeError("Response question names do not match request")
    parsed: dict[str, Answer] = {}
    for name, question in questions.items():
        answer = _object(answers[name])
        if answer.get("type") != question["type"]:
            raise TypeSafeError(f"Response type mismatch for {name}")
        if question["type"] == "noul":
            parsed[name] = {"type": "noul", "noul": _probability(answer.get("noul"))}
            continue
        probabilities = _probabilities(answer.get("probabilities"))
        confidence = _probability(answer.get("confidence"))
        if question["type"] == "choice":
            choice = _text(answer.get("choice"))
            if probabilities.keys() != question["criteria"].keys() or choice not in probabilities:
                raise TypeSafeError(f"Response choices do not match {name}")
            parsed[name] = {"type": "choice", "choice": choice, "confidence": confidence, "probabilities": probabilities}
        else:
            expected = {str(i) for i in range(len(question["criteria"]))}
            if probabilities.keys() != expected:
                raise TypeSafeError(f"Response score levels do not match {name}")
            legend = {key: _text(text) for key, text in _object(answer.get("legend")).items()}
            if legend.keys() != expected:
                raise TypeSafeError(f"Response legend does not match {name}")
            mean = _number(answer.get("score"))
            if not 0 <= mean <= len(expected) - 1:
                raise TypeSafeError(f"Response score outside rubric for {name}")
            parsed[name] = {"type": "score", "score": mean, "confidence": confidence, "legend": legend, "probabilities": probabilities}
    return {"model": _text(raw.get("model")), "answers": parsed, "usage": _usage(raw.get("usage"))}


@contextmanager
def _ledger_lock(path: Path) -> Generator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _spent(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0.0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = _object(cast(object, json.loads(line)))
            cost = _number(entry["cost_usd"])
            if cost < 0:
                raise TypeSafeError("Negative spend ledger cost")
            total += cost
    except (ValueError, KeyError) as error:
        raise TypeSafeError("Cannot read spend ledger") from error
    return total


def _pending(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    try:
        entries = _object(cast(object, json.loads(path.read_text(encoding="utf-8"))))
        return {key: _number(value) for key, value in entries.items()}
    except ValueError as error:
        raise TypeSafeError("Cannot read pending spend reservations") from error


def _write_pending(path: Path, pending: Mapping[str, float]) -> None:
    """A stopped replacement leaves the last complete reservation set intact."""
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                            prefix=path.name + ".", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            _ = handle.write(json.dumps(pending))
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _reserve(path: Path, maximum_cost: float, scoring_config: conf.ConfigRead) -> Generator[str]:
    """Reserve a byte-based upper bound across threads and CLI processes.

    An interrupted process leaves its reservation in place, failing closed until
    the unfinished request can be reconciled with the service's billing.
    """
    reservation = uuid4().hex
    pending_path = path.with_suffix(".pending.json")
    with _ledger_lock(path):
        pending = _pending(pending_path)
        spent = _spent(path)
        budget = policy.budget_usd(scoring_config)
        if spent >= budget or spent + sum(pending.values()) + maximum_cost > budget:
            raise BudgetExceeded(f"TypeSafe spend would exceed the ${budget:.2f} budget")
        pending[reservation] = maximum_cost
        _write_pending(pending_path, pending)
    try:
        yield reservation
    finally:
        with _ledger_lock(path):
            pending = _pending(pending_path)
            _ = pending.pop(reservation, 0.0)
            _write_pending(pending_path, pending)


def _record_usage(path: Path, model: str, usage: Usage, scoring_config: conf.ConfigRead) -> None:
    cost = usage["input_tokens"] * policy.input_usd_per_million(scoring_config) / 1_000_000
    with _ledger_lock(path):
        with path.open("a", encoding="utf-8") as handle:
            _ = handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "model": model, "input_tokens": usage["input_tokens"], "cost_usd": cost}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _retry_delay(header: str, attempt: int, scoring_config: conf.ConfigRead) -> float:
    if header:
        try:
            value = float(header)
            if math.isfinite(value):
                return max(0.0, value)
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
                return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return float(retry_backoff_base(scoring_config) ** attempt)


def system_one(state: Mapping[str, object], questions: Mapping[str, Question], model: str | UseConfiguredValue = UseConfiguredValue.CURRENT,
               *, ledger_path: Path | UseConfiguredValue = UseConfiguredValue.CURRENT, scoring_config: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Response:
    scoring_config = conf.read("issue_score") if isinstance(scoring_config, UseConfiguredValue) else scoring_config
    model = policy.model(scoring_config) if isinstance(model, UseConfiguredValue) else model
    ledger_path = spend_ledger(scoring_config) if isinstance(ledger_path, UseConfiguredValue) else ledger_path
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if not key:
        raise TypeSafeError("TYPESAFE_API_KEY not set: run under with-typesafe")
    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in key):
        raise TypeSafeError("TYPESAFE_API_KEY is malformed: run under with-typesafe")
    payload = {"model": model, "state": state, "questions": questions}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # Each question sees the state independently. UTF-8 bytes conservatively
    # bound text tokens; include framing headroom before reserving spend.
    token_bound = (len(encoded) + 4096) * max(1, len(questions))
    maximum_cost = token_bound * policy.input_usd_per_million(scoring_config) / 1_000_000
    request = Request(endpoint(scoring_config), data=gzip.compress(encoded), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Content-Encoding": "gzip", "Accept-Encoding": "gzip",
    })
    with _reserve(ledger_path, maximum_cost, scoring_config):
        for attempt in range(max_attempts(scoring_config)):
            with _ledger_lock(ledger_path):
                if _spent(ledger_path) >= policy.budget_usd(scoring_config):
                    raise BudgetExceeded(f"TypeSafe spend reached the ${policy.budget_usd(scoring_config):.2f} budget")
            try:
                with cast(HTTPResponse, urlopen(request, timeout=request_timeout(scoring_config))) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        body = gzip.decompress(body)
                    status = response.status
                    if not 200 <= status < 300:
                        raise TypeSafeError(f"TypeSafe HTTP {status}: {body.decode('utf-8', errors='replace').replace(key, '[redacted]')}")
            except HTTPError as error:
                error_body = error.read()
                if error.headers.get("Content-Encoding", "").lower() == "gzip":
                    try:
                        error_body = gzip.decompress(error_body)
                    except (OSError, EOFError, zlib.error):
                        error_body = b"unreadable body"
                body_text = error_body.decode("utf-8", errors="replace").replace(key, "[redacted]")
                if error.code in (429, 529) and attempt + 1 < max_attempts(scoring_config):
                    time.sleep(_retry_delay(error.headers.get("Retry-After", ""), attempt, scoring_config))
                    continue
                raise TypeSafeError(f"TypeSafe HTTP {error.code}: {body_text}") from None
            except (URLError, OSError, TimeoutError, ValueError):
                raise TypeSafeError("TypeSafe request failed before a usable response") from None
            try:
                raw = _object(cast(object, json.loads(body)))
                usage = _usage(raw.get("usage"))
                answered_model = _text(raw.get("model"))
                _record_usage(ledger_path, answered_model, usage, scoring_config)
                return parse_response(raw, questions)
            except (ValueError, KeyError, UnicodeError):
                raise TypeSafeError("TypeSafe returned malformed JSON") from None
    raise TypeSafeError("TypeSafe exhausted retry attempts")
