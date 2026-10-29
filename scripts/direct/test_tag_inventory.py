"""Fixture checks for the resolved /unit:direct tag contract."""

from pathlib import Path
import tempfile
import unittest
from typing import override

from scripts.direct.tag_inventory import contract_table_paths, inventory


class TagInventoryTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp/claude")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text, encoding="utf-8")

    def route(self, tag: str, path: str) -> str:
        return ("<TagReferenceContract>\n| Contract | File | Moment |\n| --- | --- | --- |\n"
                f"| <{tag}/> | `{path}` | now |\n</TagReferenceContract>\n")

    def test_resolves_nested_routes_imports_and_whole_step_bodies(self) -> None:
        self.write("commands/unit/direct.md", self.route("Step", "docs/direct/step.md") +
                   "<Core>\nApply <Step/> and <ThreeGods/>.\n</Core>\n@~/.claude/docs/decision_criteria.md\n")
        self.write("docs/decision_criteria.md", "<ThreeGods>\nServe these.\n</ThreeGods>\n")
        self.write("docs/direct/step.md", "Defines `<Step/>` in full.\n"
                   + "| Contract | File | Moment |\n| --- | --- | --- |\n"
                   + "| <Finish/> | `docs/direct/end.md` | end |\n")
        self.write("docs/direct/end.md", "<Finish>\nFinish once.\n</Finish>\n")
        result = inventory(self.root)
        self.assertEqual(result.issues, ())
        self.assertEqual(result.tags, ("Core", "Finish", "Step", "TagReferenceContract", "ThreeGods"))
        self.assertIn("docs/direct/end.md", result.files)
        self.assertEqual(contract_table_paths((self.root / "commands/unit/direct.md").read_text()),
                         ["docs/direct/step.md"])

    def test_missing_body_is_named(self) -> None:
        self.write("commands/unit/direct.md", "<Core>\nApply <Absent/>.\n</Core>\n")
        self.assertEqual(inventory(self.root).issues, ("MissingBody: <Absent/>",))

    def test_two_bodies_are_named(self) -> None:
        self.write("commands/unit/direct.md", self.route("Step", "docs/direct/step.md") +
                   "<Shared>\nFirst body.\n</Shared>\n")
        self.write("docs/direct/step.md", "<Step>\nUse <Shared/>.\n</Step>\n<Shared>\nSecond body.\n</Shared>\n")
        self.assertTrue(any(issue.startswith("TwoBodies: <Shared/>") for issue in inventory(self.root).issues))

    def test_nested_table_file_joins_the_same_contract_namespace(self) -> None:
        self.write("commands/unit/direct.md", self.route("Step", "docs/direct/step.md") +
                   "<Shared>\nFirst body.\n</Shared>\n")
        self.write("docs/direct/step.md", "<Step>\nApply <Shared/>.\n</Step>\n"
                   + "| Contract | File | Moment |\n| --- | --- | --- |\n"
                   + "| <Shared/> | `docs/direct/nested.md` | now |\n")
        self.write("docs/direct/nested.md", "<Shared>\nSecond body.\n</Shared>\n")
        self.assertTrue(any(issue.startswith("TwoBodies: <Shared/>") for issue in inventory(self.root).issues))

    def test_named_step_dependencies_cannot_hide_a_duplicate_body(self) -> None:
        self.write("commands/unit/direct.md", self.route("Step", "docs/direct/step.md"))
        self.write("docs/direct/step.md", "<Step>\nRead `docs/direct/helper.md`.\n</Step>\n")
        self.write("docs/direct/helper.md", "<Step>\nSecond body.\n</Step>\n")
        self.assertIn("TwoBodies: <Step/> in docs/direct/step.md, docs/direct/helper.md",
                      inventory(self.root).issues)

    def test_named_step_dependencies_join_the_contract_scope_recursively(self) -> None:
        self.write("commands/unit/direct.md", self.route("Step", "docs/direct/step.md"))
        self.write("docs/direct/step.md", "<Step>\nRead `docs/direct/helper.md`.\n</Step>\n")
        self.write("docs/direct/helper.md", "Read `docs/direct/nested.md`.\n")
        self.write("docs/direct/nested.md", "<Step>\nSecond body.\n</Step>\n")
        self.assertTrue(any(issue.startswith("TwoBodies: <Step/>") for issue in inventory(self.root).issues))

    def test_table_and_stub_must_name_existing_body_files(self) -> None:
        for target in ("gone", "empty", "wrong"):
            with self.subTest(target=target):
                self.write("commands/unit/direct.md", self.route("Step", "docs/direct/step.md") +
                           "<Step>\nRead `docs/direct/step.md` in full.\n</Step>\n")
                path = self.root / "docs/direct/step.md"
                if target == "empty":
                    self.write("docs/direct/step.md", "No tagged body here.\n")
                elif target == "wrong":
                    self.write("docs/direct/step.md", "<Other>\nThe wrong contract.\n</Other>\n")
                elif path.exists():
                    path.unlink()
                issues = inventory(self.root).issues
                self.assertIn("MissingBody: <Step/>", issues)
                self.assertTrue(any(issue.startswith("StalePath: <Step/>") for issue in issues))

    def test_read_stub_without_table_cannot_hide_a_stale_path(self) -> None:
        self.write("commands/unit/direct.md", "<Step>\nRead `docs/direct/gone.md` in full.\n</Step>\n")
        self.assertTrue(any(issue.startswith("StalePath: <Step/>") for issue in inventory(self.root).issues))

    def test_repeated_table_rows_do_not_hide_a_body_or_a_missing_file(self) -> None:
        self.write("commands/unit/direct.md", self.route("Step", "docs/direct/one.md").replace(
            "</TagReferenceContract>", "| <Step/> | `docs/direct/two.md` | now |\n</TagReferenceContract>"))
        self.write("docs/direct/two.md", "<Step>\nSecond.\n</Step>\n")
        self.assertTrue(any("StalePath: <Step/>" in issue and "one.md" in issue
                            for issue in inventory(self.root).issues))
        self.write("docs/direct/one.md", "<Step>\nFirst.\n</Step>\n")
        self.assertTrue(any(issue.startswith("TwoBodies: <Step/>") for issue in inventory(self.root).issues))

    def test_table_cannot_hide_a_different_stale_stub(self) -> None:
        self.write("commands/unit/direct.md", self.route("Step", "docs/direct/step.md") +
                   "<Step>\nDefined in `docs/direct/gone.md`; read it.\n</Step>\n")
        self.write("docs/direct/step.md", "<Step>\nThe body.\n</Step>\n")
        self.assertTrue(any(issue.startswith("StalePath: <Step/>") for issue in inventory(self.root).issues))

    def test_forwarding_cycle_is_named(self) -> None:
        self.write("commands/unit/direct.md", self.route("Step", "docs/direct/step.md"))
        self.write("docs/direct/step.md", "<Step>\nDefined in `docs/direct/other.md`.\n</Step>\n")
        self.write("docs/direct/other.md", "<Step>\nDefined in `docs/direct/step.md`.\n</Step>\n")
        self.assertTrue(any(issue.startswith("ForwardingCycle: <Step/>") for issue in inventory(self.root).issues))

    def test_examples_plans_and_other_commands_guild_are_not_definitions(self) -> None:
        self.write("commands/unit/direct.md", self.route("Step", "docs/direct/step.md") +
                   "<Guild>\nThis command.\n</Guild>\n<Core>\nApply <External/>.\n"
                   + "Read `commands/other/run.md` and `docs/plans/example.md`.\n</Core>\n")
        self.write("docs/direct/step.md", "<Step>\nUse <Guild/> and read `commands/other/run.md`.\n</Step>\n"
                   + "```xml\n<Guild>\nFenced example.\n</Guild>\n```\n"
                   + "> <Guild>\n> Quoted example.\n> </Guild>\n")
        self.write("commands/other/run.md", "<External>\nThis step.\n</External>\n<Guild>\nOther command.\n</Guild>\n")
        self.write("docs/plans/example.md", "<Guild>\nPlanned contract.\n</Guild>\n")
        self.assertEqual(inventory(self.root).issues, ())


if __name__ == "__main__":
    _ = unittest.main()
