"""Read synthetic lockfiles and patches without cargo or upstream lookups."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, final

import pins

if TYPE_CHECKING:
    from typing import override
else:
    def override(method: object) -> object:
        """The checker marker is an identity decorator on Python 3.11."""
        return method


REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
REVISION = "1234567890abcdef1234567890abcdef12345678"


def package(name: str, version: str, dependencies: tuple[str, ...] = (),
            source: str = REGISTRY) -> str:
    """An empty source writes a workspace or path package."""
    lines = ["[[package]]", f"name = {json.dumps(name)}", f"version = {json.dumps(version)}"]
    if source:
        lines.append(f"source = {json.dumps(source)}")
    if dependencies:
        lines.append(f"dependencies = {json.dumps(dependencies)}")
    return "\n".join(lines) + "\n"


@final
class PinsTests(unittest.TestCase):
    checkout: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="bevy-pins-")
        self.addCleanup(folder.cleanup)
        self.checkout = Path(folder.name)
        self.write_lock(package("bevy_ecs", "0.19.1"))
        self.write_manifest()
        _ = (self.checkout / "demo-production.md").write_text(
            "# Test production\n\n- **Merge branch:** demo\n"
            + f"- **Showrunner checkout:** {self.checkout}\n"
            + "- **Log:** log.md\n- **User zone:** America/New_York\n\n## Units\n",
            encoding="utf-8",
        )

    def write_lock(self, text: str) -> None:
        _ = (self.checkout / "Cargo.lock").write_text("version = 4\n" + text, encoding="utf-8")

    def write_manifest(self, patches: str = "") -> None:
        _ = (self.checkout / "Cargo.toml").write_text("[workspace]\n" + patches, encoding="utf-8")

    def test_registry_git_and_transitive_dependents(self) -> None:
        git_source = f"git+https://github.com/example/adapter?branch=main#{REVISION}"
        self.write_lock("\n".join([
            package("bevy_ecs", "0.19.1"),
            package("bevy", "0.19.1", ("bevy_ecs",)),
            package("renderer", "1.2.3", ("bevy",)),
            package("git-adapter", "2.0.0", ("bevy_ecs 0.19.1",), git_source),
            package("leaf", "3.0.0", (f"bevy_ecs 0.19.1 ({REGISTRY})",)),
            package("z-parent", "1.0.0", ("leaf",)),
            package("a-parent", "1.0.0", ("leaf 3.0.0",)),
            package("app", "0.1.0", ("renderer", "git-adapter"), ""),
            package("workspace-helper", "0.1.0", ("bevy_ecs",), ""),
        ]))
        result = pins.read_pins(self.checkout)
        self.assertEqual(result.bevy, ("0.19.1",))
        self.assertEqual(result.bevy_names, frozenset({"bevy", "bevy_ecs"}))
        self.assertEqual(result.dependents, (
            pins.Dependent("git-adapter", {
                "source": "git", "version": "2.0.0", "url": "https://github.com/example/adapter",
                "ref": "branch=main", "rev": REVISION,
            }, "0.19.1", (), False),
            pins.Dependent("leaf", {"source": "registry", "version": "3.0.0"},
                           "0.19.1", ("a-parent", "z-parent"), False),
            pins.Dependent("renderer", {"source": "registry", "version": "1.2.3"},
                           "0.19.1", (), False),
        ))

    def test_git_lock_sources_preserve_the_query_and_full_revision(self) -> None:
        for query in ("branch=next", "rev=abc1234", "tag=v1", ""):
            with self.subTest(query=query):
                suffix = f"?{query}" if query else ""
                self.write_lock(package("bevy_ecs", "0.19.1") + package(
                    "adapter", "1.0.0", ("bevy_ecs",),
                    f"git+https://github.com/example/adapter.git{suffix}#{REVISION}",
                ))
                self.assertEqual(pins.read_pins(self.checkout).dependents[0].pin, {
                    "source": "git", "version": "1.0.0", "url": "https://github.com/example/adapter.git",
                    "ref": query, "rev": REVISION,
                })

    def test_path_patches_track_a_dependent_and_a_bevy_crate(self) -> None:
        self.write_manifest('[patch.crates-io]\nadapter = { path = "../vendor/adapter" }\n'
                            + 'bevy_render = { path = "vendor/bevy/render" }\n')
        self.write_lock("\n".join([
            package("bevy_ecs", "0.19.1"),
            package("bevy_render", "0.19.1", ("bevy_ecs",), ""),
            package("adapter", "2.3.4", ("bevy_render",), ""),
            package("app", "0.1.0", ("adapter", "bevy_render"), ""),
        ]))
        result = pins.read_pins(self.checkout)
        self.assertEqual(result.bevy_names, frozenset({"bevy_ecs", "bevy_render"}))
        self.assertEqual(result.dependents, (
            pins.Dependent("adapter", {"source": "path", "version": "2.3.4", "path": "../vendor/adapter"},
                           "0.19.1", (), False),
            pins.Dependent("bevy_render", {
                "source": "path", "version": "0.19.1", "path": "vendor/bevy/render",
            }, "0.19.1", (), True),
        ))

    def test_git_patch_entries_use_the_resolved_lock_revision(self) -> None:
        self.write_manifest('[patch."https://github.com/example/bevy"]\n'
                            + 'bevy_render = { git = "https://github.com/example/bevy", branch = "port" }\n'
                            + '[patch.crates-io]\n'
                            + 'adapter = { git = "https://github.com/example/adapter", tag = "v2" }\n')
        self.write_lock("\n".join([
            package("bevy_ecs", "0.19.1"),
            package("bevy_render", "0.19.1", ("bevy_ecs",),
                    f"git+https://github.com/example/bevy?branch=port#{REVISION}"),
            package("adapter", "2.0.0", ("bevy_render",),
                    f"git+https://github.com/example/adapter?tag=v2#{REVISION}"),
            package("app", "0.1.0", ("adapter", "bevy_render"), ""),
        ]))
        result = pins.read_pins(self.checkout)
        self.assertEqual(result.dependents, (
            pins.Dependent("adapter", {
                "source": "git", "version": "2.0.0", "url": "https://github.com/example/adapter",
                "ref": "tag=v2", "rev": REVISION,
            }, "0.19.1", (), False),
            pins.Dependent("bevy_render", {
                "source": "git", "version": "0.19.1", "url": "https://github.com/example/bevy",
                "ref": "branch=port", "rev": REVISION,
            }, "0.19.1", (), True),
        ))

    def test_multiple_versions_choose_the_oldest_resolved_bevy(self) -> None:
        self.write_lock("\n".join([
            package("bevy_ecs", "0.10.0"),
            package("bevy_ecs", "0.9.2"),
            package("bevy_app", "0.10.0"),
            package("adapter", "9.0.0", ("bevy_ecs 0.10.0",)),
            package("adapter", "10.0.0", (f"bevy_ecs 0.9.2 ({REGISTRY})",)),
            package("mixed", "1.0.0", ("bevy_app", "bevy_ecs 0.9.2")),
            package("app", "0.1.0", ("adapter 9.0.0", "adapter 10.0.0", "mixed"), ""),
        ]))
        result = pins.read_pins(self.checkout)
        self.assertEqual(result.bevy, ("0.9.2", "0.10.0"))
        self.assertEqual(result.dependents, (
            pins.Dependent("adapter", {"source": "registry", "version": "10.0.0"}, "0.9.2", (), False),
            pins.Dependent("mixed", {"source": "registry", "version": "1.0.0"}, "0.9.2", (), False),
        ))

    def test_path_patch_keeps_the_registry_pin_and_marks_only_replaced_bevy(self) -> None:
        self.write_manifest('[patch.crates-io]\nadapter = { path = "vendor/adapter" }\n'
                            + 'bevy_render = { path = "vendor/bevy_render" }\n')
        adapters = [
            package("adapter", "1.0.0", ("bevy_ecs 0.19.1",)),
            package("adapter", "2.0.0", ("bevy_ecs 0.20.0",), ""),
        ]
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.write_lock("\n".join([
                    package("bevy_ecs", "0.19.1"),
                    package("bevy_ecs", "0.20.0"),
                    package("bevy_render", "0.19.1", ("bevy_ecs 0.19.1",)),
                    package("bevy_render", "0.20.0", ("bevy_ecs 0.20.0",), ""),
                    *(reversed(adapters) if reverse else adapters),
                    package("app", "0.1.0", ("adapter 1.0.0", "adapter 2.0.0",
                                               "bevy_render 0.19.1", "bevy_render 0.20.0"), ""),
                ]))
                self.assertEqual(pins.read_pins(self.checkout).dependents, (
                    pins.Dependent("adapter", {"source": "registry", "version": "1.0.0"},
                                   "0.19.1", (), False),
                    pins.Dependent("bevy_render", {
                        "source": "path", "version": "0.20.0", "path": "vendor/bevy_render",
                    }, "0.20.0", (), True),
                ))

    def test_git_patch_does_not_reject_kept_or_discarded_registry_versions(self) -> None:
        self.write_manifest('[patch.crates-io]\n'
                            + 'adapter = { git = "https://github.com/example/adapter", branch = "port" }\n'
                            + 'bevy_render = { git = "https://github.com/example/bevy", branch = "port" }\n')
        git_source = f"git+https://github.com/example/adapter?branch=port#{REVISION}"
        for git_bevy, registry_bevy in (("0.19.1", "0.20.0"), ("0.20.0", "0.19.1")):
            adapters = [
                package("adapter", "1.0.0", (f"bevy_ecs {registry_bevy}",)),
                package("adapter", "2.0.0", (f"bevy_ecs {git_bevy}",), git_source),
            ]
            for reverse in (False, True):
                with self.subTest(git_bevy=git_bevy, reverse=reverse):
                    self.write_lock("\n".join([
                        package("bevy_ecs", "0.19.1"),
                        package("bevy_ecs", "0.20.0"),
                        *(reversed(adapters) if reverse else adapters),
                        package("bevy_render", "0.19.1", ("bevy_ecs 0.19.1",),
                                f"git+https://github.com/example/bevy?branch=port#{REVISION}"),
                        package("bevy_render", "0.20.0", ("bevy_ecs 0.20.0",)),
                        package("app", "0.1.0", ("adapter 1.0.0", "adapter 2.0.0",
                                                   "bevy_render 0.19.1", "bevy_render 0.20.0"), ""),
                    ]))
                    expected_pin: pins.Pin = {"source": "registry", "version": "1.0.0"}
                    if git_bevy == "0.19.1":
                        expected_pin = {"source": "git", "version": "2.0.0",
                                        "url": "https://github.com/example/adapter",
                                        "ref": "branch=port", "rev": REVISION}
                    self.assertEqual(pins.read_pins(self.checkout).dependents, (
                        pins.Dependent("adapter", expected_pin, "0.19.1", (), False),
                        pins.Dependent("bevy_render", {
                            "source": "git", "version": "0.19.1",
                            "url": "https://github.com/example/bevy",
                            "ref": "branch=port", "rev": REVISION,
                        }, "0.19.1", (), True),
                    ))

    def test_source_qualified_dependencies_resolve_the_named_package(self) -> None:
        git_source = f"git+https://github.com/example/bevy#{REVISION}"
        self.write_lock("\n".join([
            package("bevy_ecs", "0.19.1"),
            package("bevy_app", "0.19.1"),
            package("bevy_app", "0.19.1", source=git_source),
            package("official-user", "1.0.0", (f"bevy_app 0.19.1 ({REGISTRY})",)),
            package("fork-user", "1.0.0", (f"bevy_app 0.19.1 ({git_source})",)),
        ]))
        self.assertEqual([item.name for item in pins.read_pins(self.checkout).dependents], ["official-user"])

    def test_bevy_name_version_and_source_all_constrain_membership(self) -> None:
        self.write_lock("\n".join([
            package("bevy_ecs", "0.19.1"),
            package("bevy_app", "99.0.0"),
            package("bevyish", "0.19.1"),
            package("bevy_render", "0.19.1", source=f"git+https://github.com/example/bevy#{REVISION}"),
            package("bevy_local", "0.19.1", source=""),
            package("wrong-version", "1.0.0", ("bevy_app",)),
            package("wrong-name", "1.0.0", ("bevyish",)),
            package("unpatched-git", "1.0.0", ("bevy_render",)),
            package("unpatched-path", "1.0.0", ("bevy_local",)),
            package("valid", "1.0.0", ("bevy_ecs",)),
        ]))
        result = pins.read_pins(self.checkout)
        self.assertEqual(result.bevy_names, frozenset({"bevy_ecs"}))
        self.assertEqual([item.name for item in result.dependents], ["valid"])

    def test_an_own_package_parent_overrides_transitive_via(self) -> None:
        self.write_lock("\n".join([
            package("bevy_ecs", "0.19.1"),
            package("adapter", "1.0.0", ("bevy_ecs",)),
            package("wrapper", "1.0.0", ("adapter",)),
            package("app", "0.1.0", ("adapter",), ""),
        ]))
        self.assertEqual(pins.read_pins(self.checkout).dependents[0].via, ())

    def test_no_bevy_ecs_has_the_specified_refusal(self) -> None:
        self.write_lock(package("bevy", "0.19.1"))
        with self.assertRaises(pins.PinsFailure) as caught:
            _ = pins.read_pins(self.checkout)
        self.assertEqual(str(caught.exception),
                         f"{self.checkout / 'Cargo.lock'}: no bevy_ecs package, so this is not a Bevy project")

    def test_missing_files_name_the_failed_path(self) -> None:
        for filename in ("Cargo.lock", "Cargo.toml"):
            with self.subTest(filename=filename):
                path = self.checkout / filename
                original = path.read_text(encoding="utf-8")
                path.unlink()
                with self.assertRaises(pins.PinsFailure) as caught:
                    _ = pins.read_pins(self.checkout)
                self.assertTrue(str(caught.exception).startswith(f"{path}: "))
                _ = path.write_text(original, encoding="utf-8")

    def test_unparseable_files_name_the_failed_path(self) -> None:
        for filename in ("Cargo.lock", "Cargo.toml"):
            with self.subTest(filename=filename):
                path = self.checkout / filename
                original = path.read_text(encoding="utf-8")
                _ = path.write_text("[invalid\n", encoding="utf-8")
                with self.assertRaises(pins.PinsFailure) as caught:
                    _ = pins.read_pins(self.checkout)
                self.assertTrue(str(caught.exception).startswith(f"{path}: "))
                _ = path.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    _ = unittest.main()
