"""Fetch crates.io facts through one injectable HTTP boundary."""
from __future__ import annotations

import json
import tomllib
import urllib.request
from collections.abc import Callable
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from typing import Literal, NamedTuple, TypeVar, TypedDict, cast
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request

import crates_io_pacer

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


class NotFound(FetchFailure):
    """The requested host answered HTTP 404."""


class BranchHead(NamedTuple):
    rev: str
    committed: str


class Item(TypedDict):
    number: int
    kind: Literal["pr", "issue"]
    state: Literal["open", "closed", "merged"]
    title: str
    updated: str
    head: str  # Empty for an issue, or before a PR's details are read.


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
        if host == "crates.io":
            crates_io_pacer.CRATES_IO_PACER.wait()
        with cast(HTTPResponse, urllib.request.urlopen(request, timeout=30)) as response:
            return response.read()
    except HTTPError as error:
        if error.code == 404:
            raise NotFound(f"{host}: HTTP 404") from error
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
        if body is None:
            raise NotFound(f"{host}: HTTP 404")
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


def github_repository(url: str) -> str:
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname != "github.com" or len(parts) < 2 or not all(parts[:2]):
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
                         github_repository(response["crate"]["repository"] or ""))

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


def default_branch(fetch: Fetch, repository: str) -> str:
    return _lookup(fetch, f"https://api.github.com/repos/{repository}",
                   lambda data: _text(data["default_branch"]))


def branch_head(fetch: Fetch, repository: str, branch: str) -> BranchHead:
    def read(data: dict[str, object]) -> BranchHead:
        committer = _object(_object(data["commit"])["committer"])
        return BranchHead(_text(data["sha"]), _text(committer["date"])[:10])
    return _lookup(fetch, f"https://api.github.com/repos/{repository}/commits/{quote(branch, safe='')}", read)


def _manifest_version(value: object) -> str:
    if isinstance(value, str):
        return value
    dependency = _object(value)
    if dependency.get("workspace") is True:
        return ""  # Inheritance is resolved by the caller's dependency name.
    version = dependency.get("version", "")
    return _text(version)


def member_manifest_paths(fetch: Fetch, repository: str, rev: str) -> list[str]:
    def read(data: dict[str, object]) -> list[str]:
        paths: list[str] = []
        for raw in _array(data["tree"]):
            item = _object(raw)
            path, kind = _text(item["path"]), _text(item["type"])
            if kind == "blob" and path.endswith("/Cargo.toml"):
                paths.append(path)
        return sorted(paths)

    url = f"https://api.github.com/repos/{repository}/git/trees/{quote(rev, safe='')}?recursive=1"
    return _lookup(fetch, url, read)


def _read_manifest(fetch: Fetch, repository: str, rev: str, path: str) -> dict[str, object]:
    url = f"https://raw.githubusercontent.com/{repository}/{quote(rev, safe='')}/{quote(path, safe='/')}"
    try:
        return cast(dict[str, object], tomllib.loads(fetch(url).decode("utf-8")))
    except (ValueError, KeyError, TypeError) as error:
        raise FetchFailure(f"raw.githubusercontent.com: invalid response: {error}") from error


def _manifest_requirement(dependencies: dict[str, object], workspace: dict[str, object],
                          bevy_names: frozenset[str]) -> str:
    requirements: dict[str, str] = {}
    for name in sorted(set(workspace) | set(dependencies)):
        if name != "bevy" and name not in bevy_names:
            continue
        value = dependencies.get(name, workspace.get(name))
        if not isinstance(value, str) and _object(value).get("workspace") is True:
            value = workspace.get(name, {})
        requirement = _manifest_version(value)
        if requirement:
            requirements[name] = requirement
    return requirements.get("bevy", "") or (requirements[sorted(requirements)[0]] if requirements else "")


def manifest_requirement(fetch: Fetch, repository: str, rev: str, crate: str,
                         bevy_names: frozenset[str]) -> str:
    try:
        document = _read_manifest(fetch, repository, rev, "Cargo.toml")
    except NotFound:
        return ""
    try:
        workspace = _object(_object(document.get("workspace", {})).get("dependencies", {}))
        dependencies = _object(document.get("dependencies", {}))
        requirement = _manifest_requirement(dependencies, workspace, bevy_names)
        if requirement or "workspace" not in document:
            return requirement
        paths = member_manifest_paths(fetch, repository, rev)
        paths.sort(key=lambda path: (Path(path).parent.name.replace("-", "_") != crate.replace("-", "_"), path))
        for path in paths[:5]:
            try:
                member = _read_manifest(fetch, repository, rev, path)
            except NotFound:
                continue
            package = _object(member.get("package", {}))
            dependencies = _object(member.get("dependencies", {}))
            if _text(package.get("name", "")) == crate:
                return _manifest_requirement(dependencies, workspace, bevy_names)
        return ""
    except (ValueError, KeyError, TypeError) as error:
        raise FetchFailure(f"raw.githubusercontent.com: invalid response: {error}") from error


def _item_state(value: object) -> Literal["open", "closed", "merged"]:
    if value not in ("open", "closed", "merged"):
        raise ValueError("item state must be open, closed or merged")
    return value


def port_items(fetch: Fetch, repository: str, target: str, since: str) -> list[Item]:
    url = (f"https://api.github.com/repos/{repository}/issues?state=all&since={since}T00:00:00Z"
           "&sort=updated&direction=desc&per_page=100")
    try:
        items: list[Item] = []
        for raw in _array(cast(object, json.loads(fetch(url)))):
            data = _object(raw)
            title = _text(data["title"])
            if target not in title or _text(data["created_at"])[:10] < since:
                continue
            number = data["number"]
            if not isinstance(number, int) or isinstance(number, bool):
                raise ValueError("item number must be an integer")
            items.append(Item(number=number, kind="pr" if "pull_request" in data else "issue",
                              state=_item_state(data["state"]), title=title,
                              updated=_text(data["updated_at"]), head=""))
        return items
    except (ValueError, KeyError, TypeError) as error:
        raise FetchFailure(f"api.github.com: invalid response: {error}") from error


def pull_state(fetch: Fetch, repository: str, number: int) -> tuple[Literal["open", "closed", "merged"], str]:
    def read(data: dict[str, object]) -> tuple[Literal["open", "closed", "merged"], str]:
        merged = data["merged"]
        if not isinstance(merged, bool):
            raise ValueError("merged must be a boolean")
        head = _object(data["head"])
        return ("merged" if merged else _item_state(data["state"]),
                f"{_text(head['ref'])} @ {_text(head['sha'])[:7]}")
    return _lookup(fetch, f"https://api.github.com/repos/{repository}/pulls/{number}", read)
