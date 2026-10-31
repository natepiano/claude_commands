"""Staff-backed reporting trees, legacy edge writes and their command-line behavior."""
from __future__ import annotations

from pathlib import Path
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import showrunner_parent
import showrunner_tree
from scripts.production import showrunners
import test_showrunners
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff


class ShowrunnerTreeTests(test_showrunners.ShowrunnerFixture):
    def test_attach_retries_routing_notices_after_the_parent_was_saved(self) -> None:
        self.showrunner("top")
        self.showrunner("mid")
        with mock.patch.dict(os.environ, self.environment()), \
                mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", self.state), \
                mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier), \
                mock.patch.object(showrunners, "registered_showrunners", return_value=[]), \
                mock.patch.object(showrunners, "tell_others", side_effect=RuntimeError("before notices")), \
                redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            _ = showrunners.main(["attach", "mid", "top"])
        self.assertEqual((self.state / "mid/parent").read_text(), "top\n")
        self.assertEqual(self.successful("attach", "mid", "top").splitlines(),
                         ["mid now reports to top", "told mid", "told top"])
        notices = self.messages()
        self.assertEqual(len(notices), 2)
        self.assertIn("send it each item for the user", notices[0][1])
        self.assertIn("never a unit", notices[1][1])

    def test_tree_uses_one_supplied_snapshot_and_ignores_stale_parent_files(self) -> None:
        for slug in ("old", "new", "child", "leaf"):
            self.showrunner(slug, running=False)
        self.recorded_parent("child", "old")
        self.recorded_parent("leaf", "child")
        record = staff.snapshot()
        child = record.by_path("child")
        parent = record.by_path("new")
        assert child is not None and parent is not None
        _ = staff.move(child.id, parent.id)
        write_text_atomic(self.state / "child" / "parent", "old\n")
        record = staff.snapshot()
        expected = {"child": "new", "leaf": "child"}
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("tree reread staff")):
            self.assertEqual(showrunner_tree.edges(record=record), expected)
            self.assertEqual(showrunner_tree.parent_edges(record=record), expected)
            self.assertEqual(showrunner_tree.parent_of("child", record=record), "new")
            self.assertEqual(showrunner_tree.children_of("old", record=record), [])
            self.assertEqual(showrunner_tree.children_of("new", record=record), ["child"])
            self.assertEqual(showrunner_tree.stored_children("new", record=record), ["child"])
            self.assertEqual(showrunner_tree.descendants_of("new", record=record), ["child", "leaf"])
            self.assertEqual(showrunner_tree.root_of("leaf", record=record), "new")
            self.assertEqual(showrunner_tree.user_facing("new", below=True, record=record), ["new", "child", "leaf"])
            self.assertEqual(showrunner_tree.edges_to_release("child", record=record),
                             showrunner_tree.WrapRelease(showrunner_tree.ReportedTo("new"), ("leaf",), ()))

    def test_tree_keeps_recorded_parent_without_a_local_timer(self) -> None:
        self.showrunner("child", running=False)
        self.recorded_parent("child", "remote")
        self.assertEqual(showrunner_tree.parent_of("child"), "remote")
        self.assertEqual(showrunner_tree.children_of("remote"), ["child"])
        self.assertEqual(showrunner_tree.root_of("child"), "remote")

    def test_attach_refuses_a_running_showrunner_not_named_for_its_production(self) -> None:
        self.showrunner("top")
        self.showrunner("mid", name="mid-name")
        stderr = self.refused("attach", "mid", "top")
        self.assertIn("production mid has showrunner mid-name", stderr)
        self.assertFalse((self.state / "mid/parent").exists())

    def test_attach_stores_the_parent_and_every_lookup_reads_it(self) -> None:
        self.showrunner("top")
        self.showrunner("mid")
        self.showrunner("leaf", running=False)
        attached = self.successful("attach", "mid", "top")
        self.assertEqual(attached.splitlines(), ["mid now reports to top", "told mid", "told top"])
        self.assertEqual((self.state / "mid" / "parent").read_text(), "top\n")
        deeper = self.successful("attach", "leaf", "mid")
        self.assertEqual(deeper.splitlines(),
                         ["leaf now reports to mid", "leaf is not running: tell it when it starts", "told mid"])
        self.assertEqual(self.successful("parent", "leaf"), "mid\n")
        self.assertEqual(self.successful("parent", "top"), "")
        self.assertEqual(self.successful("children", "top"), "mid\n")
        self.assertEqual(self.successful("children", "leaf"), "")
        self.assertEqual([self.successful("root", slug) for slug in ("top", "mid", "leaf")], ["top\n"] * 3)
        self.assertEqual(self.successful("tree").splitlines(),
                         ["top\ttop", "  mid\tmid", "    leaf\t<not running>"])
        sockets = [socket for socket, _ in self.messages()]
        self.assertEqual(sockets, [f"uds:{self.root / f'{slug}-id.sock'}" for slug in ("mid", "top", "mid")])
        to_child, to_parent, to_leafs_parent = (text for _, text in self.messages())
        self.assertIn("mid now reports to top. The root is top: send it each item for the user.", to_child)
        self.assertIn("never a unit: it has no Units row", to_parent)
        self.assertIn("leaf now reports to mid. It is a child showrunner", to_leafs_parent)

    def test_tree_commands_accept_former_slugs_and_answer_with_current_slugs(self) -> None:
        self.showrunner("top", running=False, formerly=("old-top",))
        self.showrunner("leaf", running=False, formerly=("old-leaf",))
        self.recorded_parent("leaf", "top")
        for arguments, expected in ((("parent", "old-leaf"), "top\n"),
                                    (("children", "old-top"), "leaf\n"),
                                    (("root", "old-leaf"), "top\n")):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.successful(*arguments), expected)

    def test_attach_and_detach_former_slugs_use_the_current_state_directory(self) -> None:
        self.showrunner("top", running=False, formerly=("old-top",))
        self.showrunner("leaf", running=False, formerly=("old-leaf",))
        with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier),
              mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", self.state)):
            self.recorded_parent("leaf", "top")
            showrunner_parent.attach("old-leaf", "old-top")
            self.assertEqual((self.state / "leaf" / "parent").read_text(), "top\n")
            self.assertFalse((self.state / "old-leaf").exists())
            self.assertEqual(showrunner_parent.detach("old-leaf"), "top")
            self.assertFalse((self.state / "leaf" / "parent").exists())

    def test_attach_with_no_parent_named_takes_the_callers_production_and_sends_the_caller_nothing(self) -> None:
        self.showrunner("top")
        self.showrunner("mid")
        attached = self.successful("attach", "mid", caller="top-id")
        self.assertEqual(attached.splitlines(), ["mid now reports to top", "told mid"])
        self.assertEqual([socket for socket, _ in self.messages()], [f"uds:{self.root / 'mid-id.sock'}"])
        self.assertIn("name the parent: this session runs no production", self.refused("attach", "top"))

    def test_attach_refuses_and_names_the_cause_and_stores_nothing(self) -> None:
        self.showrunner("top")
        self.showrunner("mid")
        self.showrunner("leaf")
        self.assertIn("gone has no update timer", self.refused("attach", "mid", "gone"))
        self.assertIn("mid cannot report to itself", self.refused("attach", "mid", "mid"))
        self.assertFalse(self.state.exists())
        _ = self.successful("attach", "mid", "top")
        _ = self.successful("attach", "leaf", "mid")
        # A child two levels down is told the top of the whole tree, not its own parent.
        self.assertIn("leaf now reports to mid. The root is top:", self.messages()[2][1])
        self.assertIn("mid already reports to top; detach it first", self.refused("attach", "mid", "leaf"))
        self.assertIn("leaf already reports to top, directly or through others", self.refused("attach", "top", "leaf", code=2))
        self.assertEqual(sorted(path.parent.name for path in self.state.glob("*/parent")), ["leaf", "mid"])
        self.assertEqual(len(self.messages()), 4)

    def test_detach_removes_the_edge_tells_both_and_accepts_a_child_with_none(self) -> None:
        self.showrunner("top")
        self.showrunner("mid")
        _ = self.successful("attach", "mid", "top")
        detached = self.successful("detach", "mid")
        self.assertEqual(detached.splitlines(), ["mid no longer reports to top", "told mid", "told top"])
        self.assertFalse((self.state / "mid" / "parent").exists())
        self.assertEqual(self.successful("root", "mid"), "mid\n")
        self.assertIn("mid is a root again", self.messages()[-1][1])
        self.assertIn("mid reports to no one", self.successful("detach", "mid"))

    def test_a_message_that_could_not_be_sent_is_said_and_the_edge_stands(self) -> None:
        self.showrunner("top")
        self.showrunner("mid")
        failing = {**self.environment(), "TEST_SEND_EXIT": "3"}
        result = subprocess.run([sys.executable, str(test_showrunners.SCRIPT), "attach", "mid", "top"], env=failing,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["mid now reports to top", "could not tell mid: send.py exited 3",
                                                      "could not tell top: send.py exited 3"])
        self.assertEqual(self.successful("parent", "mid"), "top\n")

    def test_an_edge_to_a_production_with_no_timer_counts_for_nothing(self) -> None:
        self.showrunner("top")
        self.showrunner("mid")
        self.stored_parent("mid", "wrapped")
        self.assertEqual(self.successful("parent", "mid"), "")
        self.assertEqual(self.successful("root", "mid"), "mid\n")
        self.assertEqual(self.successful("tree").splitlines(), ["mid\tmid", "top\ttop"])
        # The dead edge does not hold the child: it attaches again without a detach.
        _ = self.successful("attach", "mid", "top")
        self.assertEqual(self.successful("parent", "mid"), "top\n")

    def test_a_cycle_written_by_hand_makes_each_of_its_members_a_root(self) -> None:
        for slug in ("one", "two", "under"):
            self.showrunner(slug, running=False)
        self.stored_parent("one", "two")
        self.stored_parent("two", "one")
        self.stored_parent("under", "one")
        self.recorded_parent("under", "one")
        self.assertEqual([self.successful("root", slug) for slug in ("one", "two", "under")],
                         ["one\n", "two\n", "one\n"])
        self.assertEqual(self.successful("tree").splitlines(),
                         ["one\t<not running>", "  under\t<not running>", "two\t<not running>"])

    def test_a_lookup_refuses_a_production_with_no_timer(self) -> None:
        self.showrunner("top")
        for lookup in ("children", "root"):
            self.assertIn("gone has no update timer", self.refused(lookup, "gone"))

    def test_a_showrunner_faces_its_own_production_and_with_below_every_level_under_it(self) -> None:
        self.recorded_parent("mid", "top")
        self.recorded_parent("other", "top")
        self.recorded_parent("leaf", "mid")
        known = {"top", "mid", "other", "leaf", "alone"}
        with (mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", self.state),
              mock.patch.object(showrunners, "timer_slugs", return_value=known)):
            self.assertEqual([showrunner_tree.root_of(slug) for slug in ("top", "mid", "leaf", "alone")],
                             ["top", "top", "top", "alone"])
            self.assertEqual(showrunner_tree.descendants_of("mid"), ["leaf"])
            for slug in ("top", "mid", "leaf", "alone"):
                self.assertEqual(showrunner_tree.user_facing(slug), [slug])
            self.assertEqual(showrunner_tree.user_facing("top", below=True), ["top", "mid", "other", "leaf"])
            self.assertEqual(showrunner_tree.user_facing("mid", below=True), ["mid", "leaf"])
            self.assertEqual(showrunner_tree.user_facing("leaf", below=True), ["leaf"])

    def test_what_all_must_hear_once_goes_to_the_highest_running_showrunner_of_each_tree(self) -> None:
        def runner(slug: str, *, running: bool) -> showrunners.Showrunner:
            return showrunners.Showrunner(session=slug if running else "", socket=f"/{slug}.sock" if running else "",
                                          slug=slug, zone="UTC", doc="/d")

        self.recorded_parent("mid", "top")
        self.recorded_parent("leaf", "mid")
        self.recorded_parent("orphan", "down")
        with mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", self.state):
            every = [runner(slug, running=True) for slug in ("top", "mid", "leaf", "alone")]
            self.assertEqual([one["slug"] for one in showrunner_tree.highest_running(every)], ["top", "alone"])
            # With the root down, the showrunner nearest it hears, and nothing below that one does.
            below = [runner("top", running=False), runner("mid", running=True), runner("leaf", running=True),
                     runner("down", running=False), runner("orphan", running=True)]
            self.assertEqual([one["slug"] for one in showrunner_tree.highest_running(below)], ["mid", "orphan"])
            self.assertEqual(showrunner_tree.highest_running([]), [])


if __name__ == "__main__":
    _ = unittest.main()
