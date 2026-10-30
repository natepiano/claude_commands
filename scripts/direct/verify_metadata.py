"""Answer a verify run's requests from one Cargo metadata document."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import sys
from typing import TypedDict, cast


CargoTarget = TypedDict("CargoTarget", {
    "name": str,
    "kind": list[str],
    "test": bool,
    "src_path": str,
    "required-features": list[str],
})


class CargoPackage(TypedDict):
    name: str
    id: str
    manifest_path: str
    features: dict[str, list[str]]
    targets: list[CargoTarget]


class CargoMetadata(TypedDict):
    packages: list[CargoPackage]
    workspace_members: list[str]
    workspace_root: str
    target_directory: str


def require_package(meta: CargoMetadata, name: str) -> CargoPackage:
    for package in meta["packages"]:
        if package["name"] == name:
            return package
    print("verify.sh: package " + name + " not found in workspace", file=sys.stderr)
    raise SystemExit(2)


def example_features(meta: CargoMetadata, package_name: str, example_name: str) -> str:
    for package in meta["packages"]:
        if package["name"] != package_name:
            continue
        for target in package["targets"]:
            if target["name"] == example_name and "example" in target["kind"]:
                return ",".join(target.get("required-features", []))
        print(
            "verify.sh: example " + example_name + " not found in package " + package_name,
            file=sys.stderr,
        )
        sys.exit(2)
    print("verify.sh: package " + package_name + " not found in workspace", file=sys.stderr)
    sys.exit(2)


def build_targets(meta: CargoMetadata, package_name: str, kind: str, name: str) -> str:
    package = require_package(meta, package_name)
    lines: list[str] = []
    wanted = [target for target in package["targets"] if (kind or "bin") in target["kind"] and name in ("", target["name"])]
    if not wanted:
        what = f"{kind} {name}" if name else "binary"
        advice = "" if name else "; name an example with --example <name>"
        print(f"verify.sh: package {package_name} has no {what}{advice}", file=sys.stderr)
        sys.exit(2)
    for target in wanted:
        lines.append("select --" + (kind or "bin"))
        lines.append("select " + target["name"])
        if kind == "example" and target.get("required-features"):
            lines.append("features " + ",".join(target["required-features"]))
        lines.append(("binary examples/" if kind == "example" else "binary ") + target["name"])
    lines.append("target " + meta["target_directory"])
    return "\n".join(lines)


def test_targets(meta: CargoMetadata, package_name: str, feature_flags: str) -> str:
    package = require_package(meta, package_name)
    features = package["features"]
    enabled: set[str] = set()
    pending = features.get("default", []) + [
        name[len(package_name) + 1 :] for name in feature_flags.split(",") if name.startswith(package_name + "/")
    ]
    while pending:
        name = pending.pop()
        if name in features and name not in enabled:
            enabled.add(name)
            pending += features[name]
    flags = {"bin": "--bin", "test": "--test", "bench": "--bench"}
    shared = {
        (target["kind"][0], target["name"])
        for other in meta["packages"]
        if other["name"] != package_name
        for target in other["targets"]
    }
    has_lib = any(target["kind"][0] in {"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"} for target in package["targets"])
    selection = ["--lib"] if has_lib else []
    for target in package["targets"]:
        kind = target["kind"][0]
        if kind not in flags or (kind != "bin" and not target["test"]):
            continue
        if not set(target.get("required-features", [])) <= enabled or (kind, target["name"]) in shared:
            selection = (["--lib"] if has_lib else []) + ["--bins", "--tests"]
            break
        selection += [flags[kind], target["name"]]
    return "\n".join(selection or ["--bins", "--tests"])


def untested_examples(meta: CargoMetadata, package_name: str) -> str:
    members = set(meta["workspace_members"])
    packages = [package for package in meta["packages"] if package["id"] in members]
    if package_name != "--workspace":
        packages = [package for package in packages if package["name"] == package_name]
        if not packages:
            print("verify.sh: package " + package_name + " not found in workspace", file=sys.stderr)
            sys.exit(2)

    test_marker = re.compile(r"^\s*(#\[(?:cfg\(test\)|(?:\w+::)*test)\])")
    offenders: list[tuple[str, str]] = []
    for package in packages:
        for target in package["targets"]:
            if "example" not in target["kind"]:
                continue
            example_name = target["name"]
            owner_package = package["name"]
            manifest_path = package["manifest_path"]
            source = Path(target["src_path"])
            if source.name == "main.rs" and source.parent.parent.name == "examples":
                sources = sorted(source.parent.rglob("*.rs"))
            else:
                sources = [source]
            for path in sources:
                for line_number, line in enumerate(path.read_text().splitlines(), 1):
                    if match := test_marker.match(line):
                        offenders.append((
                            f"example {example_name} holds a test: {path}:{line_number} has {match.group(1)}, and examples carry no tests.",
                            f"move it into {owner_package}\x27s src/ or tests/, or delete it.",
                        ))
                        break
                else:
                    continue
                break
            if target.get("test", False):
                offenders.append((
                    f"example {example_name} sets test = true in {manifest_path}, and examples carry no tests.",
                    "remove test = true from its [[example]] entry.",
                ))

    for index, (reason, fix) in enumerate(offenders):
        if index:
            print(file=sys.stderr)
        print(reason, file=sys.stderr)
        print(fix, file=sys.stderr)
    if offenders:
        sys.exit(2)
    return ""


def answer(meta: CargoMetadata, request: str) -> str:
    name, *arguments = request.split(":")
    if name == "member" and len(arguments) == 1:
        _ = require_package(meta, arguments[0])
        return ""
    if name == "example-features" and len(arguments) == 2:
        return example_features(meta, arguments[0], arguments[1])
    if name == "test-targets" and len(arguments) == 2:
        return test_targets(meta, arguments[0], arguments[1])
    if name == "build-targets" and len(arguments) == 3:
        return build_targets(meta, arguments[0], arguments[1], arguments[2])
    if name == "untested-examples" and len(arguments) == 1:
        return untested_examples(meta, arguments[0])
    if name == "workspace-root" and not arguments:
        return meta["workspace_root"]
    if name == "target-directory" and not arguments:
        return meta["target_directory"]
    print("verify.sh: invalid metadata request " + request, file=sys.stderr)
    raise SystemExit(2)


def main(requests: list[str]) -> int:
    meta = cast(CargoMetadata, json.load(sys.stdin))
    for request in requests:
        value = answer(meta, request)
        name = request.split(":", 1)[0].upper().replace("-", "_")
        print(name + "=" + shlex.quote(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
