"""One isolated, complete agent registry for Python and shell tests."""
from __future__ import annotations

import argparse
import os
import pwd
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import final

FIXTURE = Path(__file__).with_name("test_agents_registry.conf")


@final
@dataclass(frozen=True)
class ExactRows:
    """A section contains only these rows, without inheriting the baseline's rows."""

    rows: Mapping[str, str | None]


RegistryOverrides = Mapping[str, Mapping[str, str | None] | ExactRows]
NO_OVERRIDES: RegistryOverrides = {}


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
    """Write a fixture registry, refusing the user's real registry or overlay."""
    real_config = (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude/config").resolve()
    if path.resolve() in (real_config / "agents.conf", real_config / "agents.local.conf"):
        raise AssertionError(f"test registry must be temporary: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    base_text = path.read_text(encoding="utf-8") if update and path.exists() else base_text
    _ = path.write_text(registry_text(overrides, replace_sections=replace_sections, base_text=base_text,
                                    bare_rows=bare_rows), encoding="utf-8")
    return path


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
