"""Read, atomically edit and publish the shared model registry."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, NamedTuple, cast, final

from scripts.lib import conf_reads, git_index
from scripts.lib.process_failure import failure_detail
from scripts.lib.atomic_file import write_text_atomic

DEFAULT_PATH = Path("~/.claude/config/models.conf").expanduser()
REGISTRY_PATH = "config/models.conf"
OPERATION_ENVIRONMENT = ("MODELS_LAUNCH_CONFIGURATION", "MODELS_REGISTRY_EDIT_BASE")


class RegistryRow(NamedTuple):
    section: str
    key: str
    value: str
    line: str


class LaunchModelConfiguration(NamedTuple):
    """One configuration for a launch and its child processes."""
    path: Path
    text: str
    rows: tuple[RegistryRow, ...]

    def environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        return _environment(environment, OPERATION_ENVIRONMENT[0], self)


class FreshLaunchConfiguration(NamedTuple):
    """The next operation must read a new registry configuration."""


class RegistryEditBase(NamedTuple):
    """The exact text and parsed rows that one editor validates and changes."""
    path: Path
    text: str
    rows: tuple[RegistryRow, ...]

    def environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        return _environment(environment, OPERATION_ENVIRONMENT[1], self)


def _encoded(configuration: LaunchModelConfiguration | RegistryEditBase) -> str:
    return json.dumps([str(configuration.path), configuration.text])


def _environment(environment: Mapping[str, str], variable: str,
                 configuration: LaunchModelConfiguration | RegistryEditBase) -> dict[str, str]:
    return {**environment, variable: _encoded(configuration)}


def _snapshot(environment: Mapping[str, str], variable: str, path: Path, *,
              synchronize: bool = False) -> tuple[Path, str, tuple[RegistryRow, ...]]:
    if encoded := environment.get(variable, ""):
        path, text = _decoded(encoded)
    else:
        if synchronize:
            _sync_catalog(path)
        text = registry_text(path)
    return path, text, tuple(_records(text))


def launch_configuration(environment: Mapping[str, str]) -> LaunchModelConfiguration:
    """Use the parent's operation configuration, or read a new configuration."""
    return LaunchModelConfiguration(*_snapshot(environment, OPERATION_ENVIRONMENT[0], registry_path(), synchronize=True))


def edit_base(environment: Mapping[str, str]) -> RegistryEditBase:
    """Start an editor operation, or continue its parent's editor operation."""
    return RegistryEditBase(*_snapshot(environment, OPERATION_ENVIRONMENT[1], registry_path(), synchronize=True))


def _sync_catalog(path: Path) -> None:
    if os.environ.get("MODELS_CONFIG_TIER_ONLY") == "1":
        return
    home = Path.home()
    script = Path(__file__).resolve().parents[1] / "agents/sync_codex_catalog.sh"
    state = Path(os.environ.get("CODEX_CATALOG_SYNC_STATE_FILE") or home / ".local/state/codex-agent-catalog-sync/last_success")
    sources = (Path(os.environ.get("CODEX_CONFIG_FILE") or home / ".codex/config.toml"),
               Path(os.environ.get("CODEX_MODELS_CACHE_FILE") or home / ".codex/models_cache.json"))
    if not path.is_file() or not os.access(script, os.X_OK):
        return
    if state.is_file() and not any(source.exists() and source.stat().st_mtime > state.stat().st_mtime for source in sources):
        return
    try:
        result = subprocess.run([str(script)], env={**os.environ, "MODELS_CONFIG_FILE": str(path)},
                                stdout=subprocess.DEVNULL, check=False)
        if result.returncode == 0:
            return
    except OSError:
        pass
    print("WARNING: Codex catalog sync failed; using the current model registry.", file=sys.stderr)


def agent_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Remove operation state before an agent or server starts."""
    return {key: value for key, value in environment.items()
            if key not in (*OPERATION_ENVIRONMENT, "STAFF_OPERATION_FD")}


def _decoded(encoded: str) -> tuple[Path, str]:
    match cast(object, json.loads(encoded)):
        case [str() as path, str() as text]:
            return Path(path), text
        case _:
            raise ValueError("invalid model configuration; start a new operation")


class LaunchSettings(NamedTuple):
    kind: Literal["claude", "codex"]
    model: str
    effort: str
    service_tier: str


def claude_model(model: str, environment: Mapping[str, str]) -> str:
    """Resolve a model with the selected launch account's catalog."""
    try:
        result = subprocess.run(
            ["bash", "-c", 'source "$1" && _agents_model_slug claude "$2"', "claude-model",
             str(Path(__file__).resolve().parents[1] / "agents/models_config.sh"), model],
            env={**environment, "MODELS_CONFIG_TIER_ONLY": "1"}, capture_output=True, text=True, check=True)
    except (OSError, subprocess.SubprocessError):
        return model
    return result.stdout.strip()


def claude_arguments(arguments: Sequence[str], environment: Mapping[str, str]) -> list[str]:
    """Resolve model options after the caller selects the launch account."""
    resolved = list(arguments)
    for index, argument in enumerate(arguments):
        if argument == "--":
            break
        if argument == "--model":
            resolved[index + 1] = claude_model(arguments[index + 1], environment)
        elif argument.startswith("--model="):
            resolved[index] = "--model=" + claude_model(argument.partition("=")[2], environment)
    return resolved


def launch_settings(section: str, key: str) -> LaunchSettings:
    """Read launch settings through the existing shell resolver."""
    configuration = LaunchModelConfiguration(*_snapshot(os.environ, OPERATION_ENVIRONMENT[0], registry_path()))
    resolver = 'agents_resolve "$2.$3"'
    producer_arguments: list[str] = []
    if section == "producers":
        value = next((row.value for row in reversed(configuration.rows)
                      if row.section == section and row.key == key), "")
        match value.split(":"):
            case ["claude" | "codex" as family, model, effort] if model and effort:
                producer_arguments = [family, f"{model}:{effort}"]
            case _:
                raise ValueError(f"invalid producer settings for {key}; expected <family>:<model>:<effort>")
        resolver = ('AGENT_FAMILY="$4"; _agents_validate_pair "$2.$3" "$4" "$5" && '
                    '_agents_resolve_service_tier "$2.$3" "$4"')
    result = subprocess.run(
        ["bash", "-c", 'source "$1" && { IFS= read -r -d "" table; _models_table_load "$table"; } && '
         + '{ ' + resolver + '; } && '
         + 'printf "%s\\n%s\\n%s\\n%s\\n" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "$AGENT_SERVICE_TIER"',
         "launch-settings", str(Path(__file__).resolve().parents[1] / "agents/models_config.sh"), section, key,
         *producer_arguments],
        input=configuration_shell(configuration), text=True, capture_output=True, check=False,
        env={**os.environ, "MODELS_CONFIG_TIER_ONLY": "1"})
    values = result.stdout.splitlines()
    kind = values[0] if values else ""
    if result.returncode or len(values) != 4 or kind not in ("claude", "codex") or not values[1]:
        raise ValueError(result.stderr.strip() or f"invalid launch settings for {section}.{key}")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return LaunchSettings(kind, values[1], values[2], values[3])


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
    configured = os.environ.get("MODELS_CONFIG_FILE")
    return Path(configured) if configured else DEFAULT_PATH


def registry_text(registry: Path | None = None) -> str:
    return conf_reads.read_text(registry or registry_path())


def _records(text: str) -> list[RegistryRow]:
    records: list[RegistryRow] = []
    section = ""
    for line in text.splitlines():
        content = line.partition("#")[0].strip()
        if not content:
            continue
        if content.startswith("[") and content.endswith("]"):
            section = content[1:-1]
            records.append(RegistryRow(section, "", "", line))
        else:
            key, separator, value = content.partition("=")
            records.append(RegistryRow(section, key.strip(), value.strip() if separator else "", line))
    return records


def parse_registry(text: str) -> dict[str, dict[str, str]]:
    """Parse sections and rows from registry text, including empty sections."""
    sections: dict[str, dict[str, str]] = {}
    for row in _records(text):
        values = sections.setdefault(row.section, {})
        if row.key:
            values[row.key] = row.value
    return sections


def _shell_rows(rows: tuple[RegistryRow, ...]) -> str:
    return "".join(f"_models_table_{name}=(" + " ".join(shlex.quote(value) for value in values) + ")\n"
                   for name, values in (("sections", [row.section for row in rows]),
                                        ("keys", [row.key for row in rows]), ("values", [row.value for row in rows])))


def configuration_shell(configuration: LaunchModelConfiguration | RegistryEditBase) -> str:
    return (f"MODELS_CONFIG_FILE={shlex.quote(str(configuration.path))}\n"
            + f"MODELS_REGISTRY_BASE={shlex.quote(configuration.text)}\n"
            + f"{OPERATION_ENVIRONMENT[0]}={shlex.quote(_encoded(configuration))}\n"
            + _shell_rows(configuration.rows))


def _rows(text: str) -> dict[tuple[str, str], RegistryRow]:
    return {(row.section, row.key): row for row in _records(text)
            if row.key and "=" in row.line.partition("#")[0]}


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
        raise OSError(f"git {' '.join(arguments)}: {failure_detail(done)}")
    return done.stdout.strip()


def _publish_repository(path: Path) -> SharedRegistry | UnpublishedRegistry:
    if os.environ.get("MODELS_CONFIG_PUBLISH") == "0" or path.resolve() != DEFAULT_PATH.resolve():
        return UnpublishedRegistry()
    repo = Path(_git(DEFAULT_PATH.parent, "rev-parse", "--show-toplevel"))
    if path.resolve() != (repo.resolve() / REGISTRY_PATH):
        return UnpublishedRegistry()
    return SharedRegistry(repo)


def _fetch(repo: Path, branch: str) -> str:
    """Read the branch tip from its own remote ref; every fetch in the checkout writes FETCH_HEAD."""
    tracking = f"refs/remotes/origin/{branch}"
    _ = _git(repo, "fetch", "origin", f"+refs/heads/{branch}:{tracking}")
    return _git(repo, "rev-parse", "--verify", f"{tracking}^{{commit}}")


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
    with tempfile.TemporaryDirectory(prefix="models-ff-index-") as directory:
        trial_index = Path(directory) / "index"
        git_index.private_copy(index, trial_index)
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
    with tempfile.TemporaryDirectory(prefix="models-index-") as directory:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _ = _git(repo, "read-tree", parent, environment=environment)
        blob = _git(repo, "hash-object", "-w", "--stdin", input_text=contents)
        _ = _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{REGISTRY_PATH}",
                 environment=environment)
        tree = _git(repo, "write-tree", environment=environment)
        commit = _git(repo, "commit-tree", tree, "-p", parent, "-m", f"config(models): {' '.join(description.splitlines())}")
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
                   additive_catalog: bool = False) -> str:
    """Return the published text after atomic replay under the writer lock.

    Publishing is disabled by MODELS_CONFIG_PUBLISH=0 and for every other path.
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
    lock_path = (Path(_git(target.repository, "rev-parse", "--absolute-git-dir")) / "models-config.publish.lock"
                 if isinstance(target, SharedRegistry) else path.with_name(path.name + ".lock"))
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if isinstance(target, SharedRegistry):
            _ = _publish(target.repository, path, changes, description, additive_catalog)
        elif changes:
            latest = registry_text(path)
            result = _replay(latest, changes, additive_catalog)
            if _values(latest) != _values(result):
                # Preserve formatting when no concurrent edit intervenes.
                write_text_atomic(path, after if latest == before and not additive_catalog else result)
        return registry_text(path)


@final
class Arguments(argparse.Namespace):
    command: str = ""
    registry: Path
    before: Path = Path("/dev/stdin")
    after: Path = Path()
    description: str = "update registry"
    additive_catalog: bool = False
    launch: bool = False
    edit: bool = False

    def __init__(self) -> None:
        super().__init__()
        self.registry = registry_path()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("store", "catalog", "shell"))
    _ = parser.add_argument("--launch", action="store_true")
    _ = parser.add_argument("--edit", action="store_true")
    _ = parser.add_argument("--registry", type=Path)
    _ = parser.add_argument("--before", type=Path, default=Path("/dev/stdin"))
    _ = parser.add_argument("--after", type=Path, default=Path())
    _ = parser.add_argument("--description", default="update registry")
    _ = parser.add_argument("--additive-catalog", action="store_true")
    arguments = parser.parse_args(namespace=Arguments())
    try:
        if arguments.command == "shell":
            if arguments.launch:
                configuration = LaunchModelConfiguration(*_snapshot(
                    os.environ, OPERATION_ENVIRONMENT[0], arguments.registry, synchronize=True))
            elif arguments.edit:
                configuration = RegistryEditBase(*_snapshot(
                    os.environ, OPERATION_ENVIRONMENT[1], arguments.registry, synchronize=True))
            else:
                configuration = RegistryEditBase(*_snapshot({}, "", arguments.registry))
            print(configuration_shell(configuration), end="")
            return 0
        conf_reads.record("models")
        before = arguments.before.read_text(encoding="utf-8")
        after = arguments.after.read_text(encoding="utf-8")
        if arguments.command == "catalog":
            print(catalog_text(before, after), end="")
        else:
            text = store_registry(arguments.registry, before, after, arguments.description,
                                  additive_catalog=arguments.additive_catalog)
            print(configuration_shell(RegistryEditBase(arguments.registry, text, tuple(_records(text)))), end="")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
