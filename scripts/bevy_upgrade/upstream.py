"""Fetch crates.io facts through one injectable HTTP boundary."""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from typing import Literal, NamedTuple, TypeVar, TypedDict, cast
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request

Fetch = Callable[[str], bytes]
LookupResult = TypeVar("LookupResult")


class CrateVersion(TypedDict):
    num: str
    yanked: bool


class CrateMetadata(TypedDict):
    repository: str | None  # None means crates.io names no repository.


class CrateResponse(TypedDict):
    versions: list[CrateVersion]
    crate: CrateMetadata


class ReleaseDependency(TypedDict):
    crate_id: str
    req: str
    kind: str | None  # None is crates.io's normal dependency kind.


class DependenciesResponse(TypedDict):
    dependencies: list[ReleaseDependency]


class TargetVersion(TypedDict):
    num: str
    created_at: str


class TargetVersionsResponse(TypedDict):
    versions: list[TargetVersion]


class FetchFailure(Exception):
    """A lookup failed; its text names the host and reason."""


class CrateInfo(NamedTuple):
    versions: tuple[str, ...]
    repository: str  # Empty when the crate names no GitHub repository.


class TargetUnpublished(TypedDict):
    status: Literal["unpublished"]


class TargetPrerelease(TypedDict):
    status: Literal["pre-release"]
    since: str


class TargetReleased(TypedDict):
    status: Literal["released"]
    version: str
    date: str
    since: str


def http_fetch(url: str) -> bytes:
    host = urlsplit(url).hostname or url
    try:
        request = Request(url, headers={"User-Agent": "bevy-upgrade (natepiano)"})
        with cast(HTTPResponse, urllib.request.urlopen(request, timeout=30)) as response:
            return response.read()
    except HTTPError as error:
        raise FetchFailure(f"{host}: HTTP {error.code}") from error
    except (HTTPException, OSError, ValueError) as error:
        raise FetchFailure(f"{host}: {error}") from error


def fixture_fetch(path: Path) -> Fetch:
    bodies: dict[str, object] = {}
    failure = ""
    try:
        bodies = _object(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    except (OSError, ValueError) as error:
        failure = f"fixture {path}: {error}"

    def fetch(url: str) -> bytes:
        host = urlsplit(url).hostname or url
        if failure:
            raise FetchFailure(f"{host}: {failure}")
        if url not in bodies:
            raise FetchFailure(f"{host}: no fixture")
        body = bodies[url]
        return body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")

    return fetch


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("expected an array")
    return cast(list[object], value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def _lookup(fetch: Fetch, url: str, read: Callable[[dict[str, object]], LookupResult]) -> LookupResult:
    try:
        return read(_object(cast(object, json.loads(fetch(url)))))
    except (ValueError, KeyError, TypeError) as error:
        raise FetchFailure(f"{urlsplit(url).hostname}: invalid response: {error}") from error


def _repository(value: object) -> str:
    if value is None or value == "":
        return ""
    url = urlsplit(_text(value))
    parts = url.path.strip("/").split("/")
    if url.hostname != "github.com" or len(parts) < 2 or not all(parts[:2]):
        return ""
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def _crate_response(data: dict[str, object]) -> CrateResponse:
    versions: list[CrateVersion] = []
    for raw in _array(data["versions"]):
        version = _object(raw)
        yanked = version["yanked"]
        if not isinstance(yanked, bool):
            raise ValueError("yanked must be a boolean")
        versions.append(CrateVersion(num=_text(version["num"]), yanked=yanked))
    repository = _object(data["crate"]).get("repository")
    if repository is not None:
        repository = _text(repository)
    return CrateResponse(versions=versions, crate=CrateMetadata(repository=repository))


def crate_info(fetch: Fetch, crate: str) -> CrateInfo:
    def read(data: dict[str, object]) -> CrateInfo:
        response = _crate_response(data)
        return CrateInfo(tuple(version["num"] for version in response["versions"] if not version["yanked"]),
                         _repository(response["crate"]["repository"]))

    return _lookup(fetch, f"https://crates.io/api/v1/crates/{quote(crate, safe='')}", read)


def _dependencies_response(data: dict[str, object]) -> DependenciesResponse:
    dependencies: list[ReleaseDependency] = []
    for raw in _array(data["dependencies"]):
        dependency = _object(raw)
        kind = dependency.get("kind")
        if kind is not None:
            kind = _text(kind)
        dependencies.append(ReleaseDependency(crate_id=_text(dependency["crate_id"]),
                                              req=_text(dependency["req"]), kind=kind))
    return DependenciesResponse(dependencies=dependencies)


def release_requirement(fetch: Fetch, crate: str, version: str, bevy_names: frozenset[str]) -> str:
    def read(data: dict[str, object]) -> str:
        requirements: dict[str, str] = {}
        for dependency in _dependencies_response(data)["dependencies"]:
            name = dependency["crate_id"]
            if (name == "bevy" or name in bevy_names) and dependency["kind"] != "dev":
                _ = requirements.setdefault(name, dependency["req"])
        if "bevy" in requirements:
            return requirements["bevy"]
        return requirements[sorted(requirements)[0]] if requirements else ""

    url = f"https://crates.io/api/v1/crates/{quote(crate, safe='')}/{quote(version, safe='')}/dependencies"
    return _lookup(fetch, url, read)


def _target_versions_response(data: dict[str, object]) -> TargetVersionsResponse:
    versions: list[TargetVersion] = []
    for raw in _array(data["versions"]):
        version = _object(raw)
        versions.append(TargetVersion(num=_text(version["num"]), created_at=_text(version["created_at"])))
    return TargetVersionsResponse(versions=versions)


def target_release(fetch: Fetch, target: str) -> TargetUnpublished | TargetPrerelease | TargetReleased:
    def read(data: dict[str, object]) -> TargetUnpublished | TargetPrerelease | TargetReleased:
        matching: list[tuple[str, str]] = []
        for version in _target_versions_response(data)["versions"]:
            number = version["num"]
            if number.startswith(f"{target}."):
                matching.append((number, version["created_at"][:10]))
        if not matching:
            return TargetUnpublished(status="unpublished")
        since = min(date for _, date in matching)
        stable = [(number, date) for number, date in matching if "-" not in number.split("+", 1)[0]]
        if not stable:
            return TargetPrerelease(status="pre-release", since=since)
        number, date = max(stable, key=lambda item: tuple(int(part) for part in item[0].split("+", 1)[0].split(".")))
        return TargetReleased(status="released", version=number, date=date, since=since)

    return _lookup(fetch, "https://crates.io/api/v1/crates/bevy/versions", read)
