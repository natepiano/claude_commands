"""Read, atomically edit and publish the shared agent registry."""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple, final

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import write_text_atomic

DEFAULT_PATH = Path("~/.claude/config/agents.conf").expanduser()
REGISTRY_PATH = "config/agents.conf"


class RegistryRow(NamedTuple):
    section: str
    key: str
    value: str
    line: str


class RowChange(NamedTuple):
    section: str
    key: str
    replacement: str  # An empty string deletes the row.


class SharedRegistry(NamedTuple):
    repository: Path


class UnpublishedRegistry(NamedTuple):
    pass


class AlreadyContained(NamedTuple):
    pass


class FastForward(NamedTuple):
    target: str
    registry_entry: str


def registry_path() -> Path:
    configured = os.environ.get("AGENTS_CONFIG_FILE")
    return Path(configured) if configured else DEFAULT_PATH


def registry_text(registry: Path | None = None) -> str:
    return (registry or registry_path()).read_text(encoding="utf-8")


def _rows(text: str) -> dict[tuple[str, str], RegistryRow]:
    rows: dict[tuple[str, str], RegistryRow] = {}
    section = ""
    for line in text.splitlines():
        content = line.partition("#")[0].strip()
        if content.startswith("[") and content.endswith("]"):
            section = content[1:-1]
        elif "=" in content:
            key, value = (part.strip() for part in content.split("=", 1))
            rows[section, key] = RegistryRow(section, key, value, line)
    return rows


def _changes(before: str, after: str) -> list[RowChange]:
    previous, wanted = _rows(before), _rows(after)
    return [RowChange(section, key, wanted[section, key].line if (section, key) in wanted else "")
            for section, key in dict.fromkeys([*previous, *wanted])
            if previous.get((section, key), RegistryRow(section, key, "", "")).value
            != wanted.get((section, key), RegistryRow(section, key, "", "")).value
            or ((section, key) in previous) != ((section, key) in wanted)]


def _values(text: str) -> dict[tuple[str, str], str]:
    return {address: row.value for address, row in _rows(text).items()}


def catalog_text(before: str, after: str) -> str:
    changes = [change for change in _changes(before, after)
               if change.section in ("codex.agents", "codex.families")]
    return _replay(before, changes, True)


def _version(model: str) -> tuple[int, ...]:
    match = re.match(r"gpt-([0-9]+(?:\.[0-9]+)*)-", model)
    return tuple(int(part) for part in match[1].split(".")) if match else ()


def _additive_line(change: RowChange, current: dict[tuple[str, str], RegistryRow]) -> str:
    """Union efforts and retain the newest family target across machine catalogs."""
    prior = current.get((change.section, change.key))
    if not change.replacement:
        return prior.line if prior else ""
    value = change.replacement.partition("#")[0].split("=", 1)[1].strip()
    if prior and change.section == "codex.agents":
        efforts = dict.fromkeys(item.strip() for item in f"{prior.value},{value}".split(",") if item.strip())
        value = ",".join(efforts)
    elif prior and change.section == "codex.families" and _version(prior.value) >= _version(value):
        return prior.line
    return f"{change.key}={value}"


def _replay(text: str, changes: list[RowChange], additive_catalog: bool) -> str:
    current = _rows(text)
    replacements = {(change.section, change.key):
                    _additive_line(change, current) if additive_catalog and change.section in (
                        "codex.agents", "codex.families") else change.replacement
                    for change in changes}
    lines: list[str] = []
    section = ""

    def append_new_rows() -> None:
        for address in list(replacements):
            if address[0] == section:
                line = replacements.pop(address)
                if line:
                    lines.append(line)

    for line in text.splitlines():
        content = line.partition("#")[0].strip()
        if content.startswith("[") and content.endswith("]"):
            append_new_rows()
            section = content[1:-1]
        elif "=" in content:
            address = section, content.split("=", 1)[0].strip()
            if address in replacements:
                replacement = replacements.pop(address)
                if replacement:
                    lines.append(replacement)
                continue
        lines.append(line)
    append_new_rows()
    for address, line in replacements.items():
        new_section = address[0]
        if not line:
            continue
        if section != new_section:
            lines.extend(["", f"[{new_section}]"])
            section = new_section
        lines.append(line)
    return "\n".join(lines) + "\n"


def _git(repo: Path, *arguments: str, input_text: str | None = None,
         environment: dict[str, str] | None = None) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), *arguments], input=input_text, text=True,
                              capture_output=True, env=environment, timeout=60, check=False)
    except subprocess.TimeoutExpired as error:
        raise OSError(f"git {' '.join(arguments)} timed out") from error
    if done.returncode:
        raise OSError(f"git {' '.join(arguments)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout.strip()


def _publish_repository(path: Path) -> SharedRegistry | UnpublishedRegistry:
    if os.environ.get("AGENTS_CONFIG_PUBLISH") == "0" or path.resolve() != DEFAULT_PATH.resolve():
        return UnpublishedRegistry()
    repo = Path(_git(DEFAULT_PATH.parent, "rev-parse", "--show-toplevel"))
    if path.resolve() != (repo.resolve() / REGISTRY_PATH):
        return UnpublishedRegistry()
    return SharedRegistry(repo)


def _fetch(repo: Path, branch: str) -> str:
    _ = _git(repo, "fetch", "origin", branch)
    return _git(repo, "rev-parse", "FETCH_HEAD")


def _preflight_fast_forward(repo: Path, target: str, *, replace_registry: bool,
                            base: str = "HEAD") -> AlreadyContained | FastForward:
    """Refuse unfinished merges; preserve the publishing parent before checking a move."""
    registry_entry = ""
    merge_head = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD"))
    if merge_head.exists():
        raise OSError("an unfinished merge must be completed first")
    if _git(repo, "ls-files", "--unmerged"):
        raise OSError("unmerged index entries must be resolved first")
    parent = _git(repo, "rev-parse", base)
    ancestor = _git(repo, "merge-base", parent, target)
    if ancestor == target:
        return AlreadyContained()
    if ancestor != parent:
        raise OSError("the target diverges from the publishing parent; cannot fast-forward")
    index = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    with tempfile.TemporaryDirectory(prefix="agents-ff-index-") as directory:
        trial_index = Path(directory) / "index"
        _ = shutil.copyfile(index, trial_index)
        environment = {**os.environ, "GIT_INDEX_FILE": str(trial_index)}
        if replace_registry:
            registry_entry = _git(repo, "ls-tree", target, "--", REGISTRY_PATH) + "\n"
            _ = _git(repo, "update-index", "--index-info", input_text=registry_entry, environment=environment)
        _ = _git(repo, "read-tree", "--dry-run", "-m", "-u", "HEAD", target, environment=environment)
    return FastForward(target, registry_entry)


def _apply_fast_forward(repo: Path, path: Path, plan: AlreadyContained | FastForward) -> None:
    if isinstance(plan, AlreadyContained):
        return
    # The trial index lets an accepted registry edit catch up without exempting
    # any other path from Git's tracked, untracked, or file/directory checks.
    try:
        if plan.registry_entry:
            contents = _git(repo, "show", f"{plan.target}:{REGISTRY_PATH}") + "\n"
            if registry_text(path) != contents:
                write_text_atomic(path, contents)
            _ = _git(repo, "update-index", "--index-info", input_text=plan.registry_entry)
        _ = _git(repo, "merge", "--ff-only", plan.target)
    except OSError as error:
        raise OSError(f"registry fast-forward failed after its check; local files may have changed: {error}") from error


def _fast_forward(repo: Path, path: Path, target: str, *, registry_already_shared: bool = False) -> None:
    """Preflight before any live writes, then apply the checked transition.

    A contained target is a no-op only when there is no unfinished merge.
    Only a refused check guarantees no live registry or index writes. A race or
    filesystem failure after that check can still interrupt the fast-forward.
    """
    try:
        plan = _preflight_fast_forward(repo, target, replace_registry=registry_already_shared)
    except OSError as error:
        raise OSError(f"registry edit refused before writing: fast-forward check failed: {error}") from error
    _apply_fast_forward(repo, path, plan)


def _reconcile_shared(repo: Path, path: Path, plan: AlreadyContained | FastForward) -> bool:
    try:
        _apply_fast_forward(repo, path, plan)
    except OSError as error:
        raise OSError(f"{path} is shared, but local reconciliation failed: {error}") from error
    return True


def _registry_commit(repo: Path, parent: str, contents: str, description: str) -> str:
    """Commit just the registry through an isolated index; never touch the user's index."""
    with tempfile.TemporaryDirectory(prefix="agents-index-") as directory:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _ = _git(repo, "read-tree", parent, environment=environment)
        blob = _git(repo, "hash-object", "-w", "--stdin", input_text=contents)
        _ = _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{REGISTRY_PATH}",
                 environment=environment)
        tree = _git(repo, "write-tree", environment=environment)
        commit = _git(repo, "commit-tree", tree, "-p", parent, "-m", f"config(agents): {' '.join(description.splitlines())}")
        return commit


def _publish(repo: Path, path: Path, changes: list[RowChange], description: str,
             additive_catalog: bool) -> bool:
    branch = _git(repo, "symbolic-ref", "--short", "HEAD")
    base = _git(repo, "rev-parse", "HEAD")
    local = registry_text(path)
    # A previous invocation may have written the file and stopped before push.
    # Keep *all* unshared rows, including edits preceding this operation.
    pending = _changes(_git(repo, "show", f"{base}:{REGISTRY_PATH}") + "\n", local)
    try:
        remote = _fetch(repo, branch)
    except OSError as error:
        raise OSError(f"registry edit refused before writing: fetch failed: {error}") from error
    remote_text = _git(repo, "show", f"{remote}:{REGISTRY_PATH}") + "\n"
    # Recover a push accepted before its acknowledgement/local fast-forward.
    registry_already_shared = bool(pending and _values(_replay(remote_text, pending, False)) == _values(remote_text))
    _fast_forward(repo, path, remote, registry_already_shared=registry_already_shared)
    base = _git(repo, "rev-parse", "HEAD")
    local = registry_text(path)
    contents = _replay(local, changes, additive_catalog)
    pending = _changes(_git(repo, "show", f"{base}:{REGISTRY_PATH}") + "\n", contents)
    if not pending:
        return False
    if contents != local:
        write_text_atomic(path, contents)
    last_error = ""
    for attempt in range(3):
        commit = _registry_commit(repo, base, contents, description)
        try:
            _ = _git(repo, "push", "origin", f"{commit}:refs/heads/{branch}")
        except OSError as error:
            last_error = str(error)
            try:
                remote = _fetch(repo, branch)
                remote_text = _git(repo, "show", f"{remote}:{REGISTRY_PATH}") + "\n"
                replayed = _replay(remote_text, pending, additive_catalog)
                ancestor = _git(repo, "merge-base", commit, remote)
                shared = (ancestor == commit
                          or ancestor == base and _values(replayed) == _values(remote_text))
            except OSError as retry_error:
                last_error = str(retry_error)
                break
            try:
                plan = _preflight_fast_forward(repo, remote, replace_registry=True, base=base)
            except OSError as retry_error:
                if shared:
                    raise OSError(f"{path} is shared, but local reconciliation failed: {retry_error}") from retry_error
                last_error = f"retry fast-forward check failed: {retry_error}"
                break
            if shared:
                return _reconcile_shared(repo, path, plan)
            if attempt == 2:
                break
            # A contained remote cannot replace local history or its registry.
            # A forward target passed the same preflight as the initial fetch.
            if isinstance(plan, FastForward):
                base, contents = remote, replayed
                if registry_text(path) != contents:
                    write_text_atomic(path, contents)
            continue
        try:
            plan = _preflight_fast_forward(repo, commit, replace_registry=True)
        except OSError as error:
            raise OSError(f"{path} is shared, but local reconciliation failed: {error}") from error
        return _reconcile_shared(repo, path, plan)
    raise OSError(f"{path} is edited but not shared; publish failed after at most 3 tries: {last_error}")


def store_registry(registry: Path, before: str, after: str, description: str, *,
                   additive_catalog: bool = False) -> bool:
    """Replay this edit atomically; on the real registry fetch, ff, commit only it, and push.

    Publishing is disabled by AGENTS_CONFIG_PUBLISH=0 and for every other path.
    Rejected pushes replay every pending local row on the fetched tip, at most
    three tries. An identical command recovers unfinished publication first.
    The registry commit is parented on local HEAD, so publishing an ahead
    checkout also pushes its pre-existing local commits.
    No other path is staged, committed, stashed, reset or rebased.
    """
    path = registry.resolve()
    changes = _changes(before, after)
    if additive_catalog:
        changes = [change for change in changes if change.section in ("codex.agents", "codex.families")]
    target = _publish_repository(path)
    if isinstance(target, SharedRegistry):
        repo = target.repository
        lock_path = Path(_git(repo, "rev-parse", "--absolute-git-dir")) / "agents-config.publish.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _publish(repo, path, changes, description, additive_catalog)
    if not changes:
        return False
    latest = registry_text(path)
    result = _replay(latest, changes, additive_catalog)
    if _values(latest) == _values(result):
        return False
    # Preserve the writer's exact formatting when no concurrent edit intervened.
    write_text_atomic(path, after if latest == before and not additive_catalog else result)
    return True


@final
class Arguments(argparse.Namespace):
    command: str = ""
    registry: Path = DEFAULT_PATH
    before: Path = Path()
    after: Path = Path()
    description: str = "update registry"
    additive_catalog: bool = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("store", "catalog"))
    _ = parser.add_argument("--registry", type=Path, default=registry_path())
    _ = parser.add_argument("--before", type=Path, required=True)
    _ = parser.add_argument("--after", type=Path, required=True)
    _ = parser.add_argument("--description", default="update registry")
    _ = parser.add_argument("--additive-catalog", action="store_true")
    arguments = parser.parse_args(namespace=Arguments())
    try:
        before = arguments.before.read_text(encoding="utf-8")
        after = arguments.after.read_text(encoding="utf-8")
        if arguments.command == "catalog":
            print(catalog_text(before, after), end="")
        else:
            _ = store_registry(arguments.registry, before, after, arguments.description,
                               additive_catalog=arguments.additive_catalog)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
