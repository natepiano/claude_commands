"""One isolated, complete agent registry for Python and shell tests."""
from __future__ import annotations

import argparse
import os
import pwd
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

FIXTURE = Path(__file__).with_name("test_models_registry.conf")


@final
@dataclass(frozen=True)
class ExactRows:
    """A section contains only these rows, without inheriting the baseline's rows."""

    rows: Mapping[str, str | None]


RegistryOverrides = Mapping[str, Mapping[str, str | None] | ExactRows]
NO_OVERRIDES: RegistryOverrides = {}


@final
class RegistryReadProbe:
    """Count registry opens in every Python child of a fixture launch."""

    def __init__(self, root: Path, registry: Path) -> None:
        self.directory = root / "registry-read-probe"
        self.directory.mkdir(exist_ok=True)
        self.log = self.directory / "reads"
        self.registry = registry.resolve()
        self.local_reads = 0
        _ = (self.directory / "sitecustomize.py").write_text('''import os, sys
target = os.environ.get("TEST_REGISTRY_PATH", "")
log = os.environ.get("TEST_REGISTRY_READS", "")
def registry_open(event, arguments):
    if event != "open" or not target or not log:
        return
    path, mode, flags = arguments
    if not isinstance(path, (str, bytes)) or os.fsdecode(path) != target:
        return
    if (isinstance(mode, str) and "r" in mode) or (mode is None and flags & os.O_ACCMODE == os.O_RDONLY):
        descriptor = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, (str(os.getpid()) + " " + sys.argv[0] + "\\n").encode())
        finally:
            os.close(descriptor)
sys.addaudithook(registry_open)
''')
        self.clear()

    def environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        return {**environment, "TEST_REGISTRY_PATH": str(self.registry), "TEST_REGISTRY_READS": str(self.log),
                "PYTHONPATH": str(self.directory) + os.pathsep + environment.get("PYTHONPATH", "")}

    @contextmanager
    def active(self, environment: Mapping[str, str]) -> Generator[None]:
        from scripts.lib import models_registry

        self.local_reads = 0
        with patch.dict(os.environ, self.environment(environment)), \
                patch.object(models_registry, "registry_text", wraps=models_registry.registry_text) as reads:
            try:
                yield
            finally:
                self.local_reads = reads.call_count

    def total(self) -> int:
        return self.local_reads + len(self.reads())

    def reads(self) -> list[str]:
        return self.log.read_text().splitlines()

    def clear(self) -> None:
        self.local_reads = 0
        _ = self.log.write_text("")


def registry_text(overrides: RegistryOverrides = NO_OVERRIDES, *,
                  replace_sections: Sequence[str] = (), base_text: str = "",
                  bare_rows: Sequence[str] = ()) -> str:
    """Merge row overrides; ExactRows replaces one section, including assignments."""
    sections: dict[str, dict[str, str | None]] = {}
    section = ""
    for line in (base_text or FIXTURE.read_text(encoding="utf-8")).splitlines():
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            _ = sections.setdefault(section, {})
        elif "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            sections.setdefault(section, {})[key.strip()] = value
        elif line.strip() and not line.lstrip().startswith("#"):
            sections.setdefault(section, {})[line.strip()] = None
    replaced = set(replace_sections)
    replaced.update(section for section, rows in overrides.items() if isinstance(rows, ExactRows))
    for section in replaced:
        sections[section] = {}
    for section, rows in overrides.items():
        target = sections.setdefault(section, {})
        for key, value in (rows.rows if isinstance(rows, ExactRows) else rows).items():
            if value is None:
                _ = target.pop(key, None)
            else:
                target[key] = value
    for address in bare_rows:
        section, key = address.rsplit("/", 1)
        sections.setdefault(section, {})[key] = None
    return "\n\n".join(f"[{section}]\n" + "\n".join(key if value is None else f"{key}={value}"
                                                   for key, value in rows.items())
                          for section, rows in sections.items() if rows) + "\n"


def write_registry(path: Path, overrides: RegistryOverrides = NO_OVERRIDES, *,
                   replace_sections: Sequence[str] = (), update: bool = False, base_text: str = "",
                   bare_rows: Sequence[str] = ()) -> Path:
    """Write a fixture registry, refusing the user's real models.conf."""
    real_config = (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude/config").resolve()
    if path.resolve() == real_config / "models.conf":
        raise AssertionError(f"test registry must be temporary: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    base_text = path.read_text(encoding="utf-8") if update and path.exists() else base_text
    _ = path.write_text(registry_text(overrides, replace_sections=replace_sections, base_text=base_text,
                                    bare_rows=bare_rows), encoding="utf-8")
    return path


class LaunchSettingsTests(unittest.TestCase):
    root: Path = Path()
    path: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "models.conf"
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "MODELS_CONFIG_FILE": str(self.path),
            "MODELS_CONFIG_PUBLISH": "0",
            "MODELS_CONFIG_TIER_ONLY": "1",
            "CODEX_PACER_TIER_FILE": str(self.root / "absent-tier"),
        })))

    def shell_settings(self, section: str, key: str) -> tuple[str, ...]:
        from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

        result = subprocess.run([
            "bash", "-c", 'source "$1"; agents_resolve "$2.$3" || exit; ' +
            'printf "%s\\n" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "$AGENT_SERVICE_TIER"',
            "settings-test", str(Path(__file__).resolve().parents[1] / "agents/models_config.sh"), section, key,
        ], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        return tuple(result.stdout.splitlines())

    def test_launch_settings_match_shell_role_resolution_and_tier_precedence(self) -> None:
        from scripts.lib import models_registry

        for family in ("codex", "claude"):
            for section, keys in (("direct", ("impl", "test", "fix", "review")),
                                  ("design_roles", ("designer", "reviewer"))):
                for key in keys:
                    for tier in ("", "fast", "flex", "default", "pace"):
                        with self.subTest(family=family, section=section, key=key, tier=tier):
                            _ = write_registry(self.path, {
                                "assignments": {section: family},
                                f"{section}.{family}": {key: "gpt-test:medium" if family == "codex" else "sonnet:max"},
                                f"{section}.options": {"codex_service_tier": "flex",
                                                         f"codex_service_tier.{key}": tier or None},
                            })
                            settings = models_registry.launch_settings(section, key)
                            self.assertEqual((settings.kind, settings.model, settings.effort, settings.service_tier),
                                             self.shell_settings(section, key))

    def test_registry_changes_affect_the_next_launch_without_changing_caller_context(self) -> None:
        from scripts.lib import models_registry

        with patch.dict(os.environ, {"AGENTS_CALLER_FAMILY": "claude", "AGENTS_CALLER_ASSIGNMENT": "caller"}):
            _ = write_registry(self.path, {"assignments": {"direct": "codex"}})
            first = models_registry.launch_settings("direct", "impl")
            _ = write_registry(self.path, {"assignments": {"direct": "caller"},
                                          "direct.claude": {"impl": "sonnet:max"}})
            second = models_registry.launch_settings("direct", "impl")
            self.assertNotEqual((first.kind, first.model), (second.kind, second.model))
            self.assertEqual((second.kind, second.model, second.effort, second.service_tier),
                             self.shell_settings("direct", "impl"))
            self.assertEqual(os.environ["AGENTS_CALLER_FAMILY"], "claude")
            self.assertEqual(os.environ["AGENTS_CALLER_ASSIGNMENT"], "caller")

            configuration = models_registry.launch_configuration(os.environ)
            _ = write_registry(self.path, {"assignments": {"direct": "codex"}})
            for inherited, expected in ((False, first), (True, second)):
                environment = configuration.environment(os.environ) if inherited else dict(os.environ)
                with self.subTest(inherited=inherited), patch.dict(os.environ, environment):
                    self.assertEqual(models_registry.launch_settings("direct", "impl"), expected)

    def test_launch_settings_starts_no_python_registry_process(self) -> None:
        from scripts.lib import models_registry

        _ = write_registry(self.path)
        text = self.path.read_text()
        log = self.root / "python-processes"
        probe = self.root / "process-probe"
        probe.mkdir()
        _ = (probe / "sitecustomize.py").write_text(
            "import sys\n"
            + f"with open({str(log)!r}, 'a') as output: output.write(sys.argv[0] + '\\n')\n")
        for comment_length in (0, 70_000):
            with self.subTest(comment_length=comment_length):
                _ = self.path.write_text("#" + "x" * comment_length + "\n" + text)
                _ = log.write_text("")
                with patch.dict(os.environ, {"PYTHONPATH": str(probe) + os.pathsep + os.environ.get("PYTHONPATH", "")}):
                    settings = models_registry.launch_settings("direct", "impl")
                self.assertEqual(tuple(settings), self.shell_settings("direct", "impl"))
                self.assertEqual(log.read_text(), "")

    def test_invalid_launch_rows_fail_before_a_launcher_can_use_them(self) -> None:
        from scripts.lib import models_registry

        cases: tuple[RegistryOverrides, ...] = (
            {"direct.codex": {"impl": "missing-agent:high"}},
            {"direct.codex": {"impl": "gpt-test:invalid-effort"}},
            {"direct.codex": {"impl": None}},
            {"assignments": {"direct": "unknown-family"}},
            {"direct.options": {"codex_service_tier": "unknown-tier"}},
        )
        for rows in cases:
            with self.subTest(rows=rows):
                _ = write_registry(self.path, rows)
                with self.assertRaises(ValueError):
                    _ = models_registry.launch_settings("direct", "impl")


@final
class Arguments(argparse.Namespace):
    path: Path = Path()
    rows: list[str] = []
    replace_section: list[str] = []
    bare_row: list[str] = []
    update: bool = False
    replace_provided_sections: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("path", type=Path)
    _ = parser.add_argument("rows", nargs="*", help="section/key=value; omit =value to delete")
    _ = parser.add_argument("--replace-section", action="append", default=[])
    _ = parser.add_argument("--bare-row", action="append", default=[], help="section/key without a value")
    _ = parser.add_argument("--update", action="store_true")
    _ = parser.add_argument("--replace-provided-sections", action="store_true",
                            help="replace the supplied sections for closed catalog/sweep scenarios")
    args = Arguments()
    _ = parser.parse_intermixed_args(namespace=args)
    rows: dict[str, dict[str, str | None]] = {}
    for argument in args.rows:
        address, separator, value = argument.partition("=")
        section, key = address.rsplit("/", 1)
        rows.setdefault(section, {})[key] = value if separator else None
    if args.replace_provided_sections:
        args.replace_section.extend(rows)
    _ = write_registry(args.path, rows, replace_sections=args.replace_section, update=args.update,
                       bare_rows=args.bare_row)


if __name__ == "__main__":
    main()
