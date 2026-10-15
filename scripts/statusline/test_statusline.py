"""Tests for statusline.sh and statusline.jq: the role, the usage line, its reset times, and their zone."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict, final, override
from zoneinfo import ZoneInfo

PROGRAM = Path(__file__).with_name("statusline.jq")
SHIPPED_COLORS = PROGRAM.parents[2] / "config/statusline.conf"
# The settings.json statusLine command, naming this folder in place of ~/.claude's.
COMMAND = f'statusline="{Path(__file__).parent}"; . "$statusline/statusline.sh"'
USER_ZONE = ZoneInfo("America/New_York")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def status_lines(*inputs: object, labels: Path | None = None, account: str = "claude1",
                 colors: Path | str = "", showrunners: str = "", **marks: str) -> list[str]:
    """Run statusline.jq once over every input and return its output lines.

    `colors` is a file path or temporary config text; empty text omits the argument.
    """
    stdin = "\n".join(json.dumps(each) for each in inputs)
    stored = [] if labels is None else ["--slurpfile", "labels", str(labels)]
    with tempfile.TemporaryDirectory() as directory:
        configured: list[str] = []
        if colors:
            path = colors if isinstance(colors, Path) else Path(directory) / "statusline.conf"
            if isinstance(colors, str):
                _ = path.write_text(colors, encoding="utf-8")
            configured = ["--rawfile", "colors", str(path)]
        result = subprocess.run(
            ["jq", "-r", "--arg", "pwd", "/tmp", "--arg", "account", account,
             "--arg", "showrunners", showrunners, *stored, *configured, "-f", str(PROGRAM)],
            input=stdin, capture_output=True, text=True, check=True,
            env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **marks},
        )
    return result.stdout.splitlines()


class ContextWindow(TypedDict, total=False):
    total_input_tokens: int
    context_window_size: int


class ContextInput(TypedDict):
    context_window: ContextWindow


class ColorTests(unittest.TestCase):
    def test_shipped_role_colors_and_unassigned(self) -> None:
        cases: tuple[tuple[str, str, dict[str, str]], ...] = (
            ("\x1b[95mshowrunner\x1b[0m", "session-id", {}),
            ("\x1b[94munit director\x1b[0m", "", {"SHOWRUNNER_UNIT": "unit"}),
            ("\x1b[96mworker\x1b[0m", "", {"PLAN_DELEGATE_TEAM_ROLE": "impl", "SHOWRUNNER_UNIT": "unit"}),
            ("unassigned", "", {}),
        )
        for expected, showrunners, marks in cases:
            with self.subTest(role=expected):
                line = status_lines({"session_id": "session-id"}, colors=SHIPPED_COLORS,
                                    showrunners=showrunners,
                                    PLAN_DELEGATE_TEAM_ROLE=marks.get("PLAN_DELEGATE_TEAM_ROLE", ""),
                                    SHOWRUNNER_UNIT=marks.get("SHOWRUNNER_UNIT", ""))[0]
                self.assertEqual(line.split(" | ")[0], expected)

    def test_shipped_account_colors_and_unknown_label(self) -> None:
        for account, expected in (
            ("claude1", "\x1b[36mclaude1\x1b[0m"), ("claude2", "\x1b[35mclaude2\x1b[0m"),
            ("claude3", "\x1b[34mclaude3\x1b[0m"), ("claude4", "\x1b[97mclaude4\x1b[0m"),
            ("claude99", "claude99"),
        ):
            with self.subTest(account=account):
                self.assertEqual(status_lines({}, account=account, colors=SHIPPED_COLORS)[0].split(" | ")[1], expected)

    def test_token_thresholds_use_the_unrounded_window_percentage(self) -> None:
        for tokens, code in ((79_000, 92), (79_999, 92), (80_000, 93),
                             (89_000, 93), (89_999, 93), (90_000, 91)):
            with self.subTest(tokens=tokens):
                payload = ContextInput(context_window=ContextWindow(total_input_tokens=tokens, context_window_size=100_000))
                field = status_lines(payload, colors=SHIPPED_COLORS)[0].split(" | ")[3]
                self.assertEqual(field, f"\x1b[{code}m{tokens:,}\x1b[0m")

    def test_tokens_without_a_positive_window_size_are_uncolored(self) -> None:
        for window in (ContextWindow(total_input_tokens=90_000),
                       ContextWindow(total_input_tokens=90_000, context_window_size=0),
                       ContextWindow(total_input_tokens=90_000, context_window_size=-1)):
            with self.subTest(window=window):
                self.assertEqual(status_lines(ContextInput(context_window=window), colors=SHIPPED_COLORS)[0].split(" | ")[3], "90,000")

    def test_each_usage_window_colors_the_integer_left_at_both_thresholds(self) -> None:
        for name, index in (("5-hour", 0), ("weekly", 1)):
            for used, left, code in ((89, 11, 92), (90, 10, 93), (94, 6, 93), (95, 5, 91), (89.1, 10, 93)):
                with self.subTest(window=name, used=used):
                    payload = limits({"used_percentage": used}, {"used_percentage": used})
                    field = status_lines(payload, colors=SHIPPED_COLORS)[1].split(" | ")[index]
                    self.assertEqual(field, f"{name} \x1b[{code}m{left}% left\x1b[0m")

    def test_empty_config_unknown_keys_and_invalid_colors_leave_the_piece_uncolored(self) -> None:
        for config in (Path("/dev/null"), "unknown.setting=bright-red\n",
                       "account.claude1=ultraviolet\n"):
            with self.subTest(config=config):
                self.assertEqual(status_lines({}, colors=config), status_lines({}))

    def test_invalid_threshold_leaves_its_family_uncolored_without_disabling_other_fields(self) -> None:
        payload = ContextInput(context_window=ContextWindow(total_input_tokens=90_000, context_window_size=100_000))
        usage = limits({"used_percentage": 95}, {"used_percentage": 90})
        for family in ("tokens", "left"):
            for threshold in ("warn", "alarm"):
                with self.subTest(family=family, threshold=threshold):
                    config = SHIPPED_COLORS.read_text(encoding="utf-8") + f"\n{family}.{threshold}=not-a-number\n"
                    lines = status_lines({**payload, **usage}, colors=config)
                    self.assertEqual(lines[0].split(" | ")[1], "\x1b[36mclaude1\x1b[0m")
                    if family == "tokens":
                        self.assertEqual(lines[0].split(" | ")[3], "90,000")
                        self.assertEqual(lines[1], "5-hour \x1b[91m5% left\x1b[0m | weekly \x1b[93m10% left\x1b[0m")
                    else:
                        self.assertEqual(lines[1], "5-hour 5% left | weekly 10% left")
                        self.assertEqual(lines[0].split(" | ")[3], "\x1b[91m90,000\x1b[0m")

    def test_colors_wrap_only_the_fields_and_preserve_both_plain_lines(self) -> None:
        reset = int(datetime(2026, 12, 3, 12, tzinfo=timezone.utc).timestamp())
        payload = {**limits({"used_percentage": 90, "resets_at": reset}, {"used_percentage": 95, "resets_at": reset}),
                   "session_id": "sid", "workspace": {"current_dir": "/tmp/project"},
                   "model": {"display_name": "Opus"}, "effort": {"level": "high"},
                   "context_window": ContextWindow(total_input_tokens=80_000, context_window_size=100_000)}
        plain = status_lines(payload, showrunners="sid")
        colored = status_lines(payload, showrunners="sid", colors=SHIPPED_COLORS)
        self.assertEqual([ANSI.sub("", line) for line in colored], plain)
        self.assertEqual(colored, [
            "\x1b[95mshowrunner\x1b[0m | \x1b[36mclaude1\x1b[0m | project | \x1b[93m80,000\x1b[0m | Opus high",
            "5-hour \x1b[93m10% left\x1b[0m until 07:00 EST | weekly \x1b[91m5% left\x1b[0m until Thu 07:00 EST",
        ])


def limits(five_hour: dict[str, float | None], seven_day: dict[str, float | None]) -> dict[str, object]:
    return {"rate_limits": {"five_hour": five_hour, "seven_day": seven_day}}


def user_time(epoch: int, fmt: str) -> str:
    return datetime.fromtimestamp(epoch, USER_ZONE).strftime(fmt)


class UsageLineTests(unittest.TestCase):
    def test_reset_times_in_each_season(self) -> None:
        edt = int(datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc).timestamp())
        est = int(datetime(2026, 12, 3, 12, 0, tzinfo=timezone.utc).timestamp())
        lines = status_lines(limits({"used_percentage": 38, "resets_at": edt}, {"used_percentage": 60, "resets_at": est}))
        self.assertEqual(lines[1], "5-hour 62% left until 14:00 EDT | weekly 40% left until Thu 07:00 EST")

    def test_missing_reset_shows_percent_alone(self) -> None:
        lines = status_lines(limits({"used_percentage": 38}, {"used_percentage": 60, "resets_at": None}))
        self.assertEqual(lines[1], "5-hour 62% left | weekly 40% left")

    def test_missing_rate_limits_gives_one_line(self) -> None:
        self.assertEqual(len(status_lines({})), 1)

    def test_daylight_saving_edges_match_zoneinfo(self) -> None:
        """Each second either side of every transition, 2026 to 2040, reads as zoneinfo does."""
        epochs: list[int] = []
        hour = datetime(2026, 1, 1, tzinfo=timezone.utc)
        while hour.year < 2041:
            following = hour + timedelta(hours=1)
            if hour.astimezone(USER_ZONE).utcoffset() != following.astimezone(USER_ZONE).utcoffset():
                edge = int(following.timestamp())
                epochs.extend([edge - 1, edge])
            hour = following
        self.assertEqual(len(epochs), 15 * 2 * 2)
        inputs = [limits({"used_percentage": 0, "resets_at": t}, {"used_percentage": 0, "resets_at": t}) for t in epochs]
        lines = status_lines(*inputs)[1::2]
        expected = [
            f"5-hour 100% left until {user_time(t, '%H:%M %Z')} | weekly 100% left until {user_time(t, '%a %H:%M %Z')}"
            for t in epochs
        ]
        self.assertEqual(lines, expected)



@final
class StoredReadingTests(unittest.TestCase):
    """The weekly and 5-hour figures agent_notes stored for the session's label."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.directory = Path()
        self.now = 0
        self.reset = 0
        self.live: dict[str, object] = {}

    @override
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = int(time.time())
        self.reset = self.now + 3 * 86400
        self.live = limits({"used_percentage": 10, "resets_at": self.now + 3600},
                           {"used_percentage": 97, "resets_at": self.now + 86400})

    def labels(self, readings: dict[str, object]) -> Path:
        path = self.directory / "by-label.json"
        _ = path.write_text(json.dumps(readings), encoding="utf-8")
        return path

    def weekly(self, left: float, at: int, resets_at: int | None) -> dict[str, object]:
        return {"weekly": {"left": left, "resets_at": resets_at, "at": at}}

    def test_a_fresh_reading_for_the_label_replaces_the_response_figure(self) -> None:
        path = self.labels({"claude1": self.weekly(2.0, self.now - 120, self.reset)})
        self.assertEqual(
            status_lines(self.live, labels=path)[1].split(" | ")[1],
            f"weekly 2% left until {user_time(self.reset, '%a %H:%M %Z')}",
        )

    def test_legacy_and_canonical_account_caches_find_the_canonical_reading(self) -> None:
        path = self.labels({"claude2": self.weekly(42, self.now - 60, self.reset)})
        for label in ("claude 2", "claude2"):
            with self.subTest(label=label):
                self.assertIn("weekly 42% left", status_lines(self.live, labels=path, account=label)[1])

    def test_a_reading_older_than_ten_minutes_gives_the_response_figure(self) -> None:
        path = self.labels({"claude1": self.weekly(2.0, self.now - 601, self.reset)})
        self.assertTrue(status_lines(self.live, labels=path)[1].endswith("| weekly 3% left until "
                        + user_time(self.now + 86400, "%a %H:%M %Z")))

    def test_a_reading_whose_window_has_reset_gives_the_response_figure(self) -> None:
        path = self.labels({"claude1": self.weekly(2.0, self.now - 60, self.now - 1)})
        self.assertIn("weekly 3% left", status_lines(self.live, labels=path)[1])

    def test_another_label_s_reading_is_not_used(self) -> None:
        path = self.labels({"claude2": self.weekly(50.0, self.now - 60, self.reset)})
        self.assertIn("weekly 3% left", status_lines(self.live, labels=path)[1])

    def test_a_missing_file_given_as_dev_null_gives_the_response_figure(self) -> None:
        self.assertIn("weekly 3% left", status_lines(self.live, labels=Path("/dev/null"))[1])

    def test_a_none_open_five_hour_reading_shows_all_left_with_no_reset(self) -> None:
        path = self.labels({"claude1": {"five_hour": {"left": 100.0, "resets_at": None, "at": self.now - 60}}})
        self.assertTrue(status_lines(self.live, labels=path)[1].startswith("5-hour 100% left | weekly"))

    def test_an_address_as_the_account_never_reaches_the_line(self) -> None:
        line = status_lines(self.live, account="someone@example.invalid")[0]
        self.assertNotIn("@", line)

    def test_a_numbered_claude_label_reaches_the_line_without_its_space(self) -> None:
        line = status_lines(self.live, account="claude 3")[0]
        self.assertIn("| claude3 |", line)
        self.assertNotIn("claude 3", line)


@final
class RoleTests(unittest.TestCase):
    """The first field, as statusline.sh and statusline.jq read it from the session."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def timer(self, instance: str, target: str) -> None:
        """A notifier instance whose conf opens with TARGET, as notifier.sh writes it."""
        directory = self.home / ".local/state/notifier" / instance
        directory.mkdir(parents=True)
        _ = (directory / "conf").write_text(f"TARGET={target}\nEVERY=600\n", encoding="utf-8")

    def first_line(self, session_id: str = "aaaa-1111", **marks: str) -> str:
        return ANSI.sub("", self.command_lines({"session_id": session_id}, **marks)[0])

    def command_lines(self, payload: object, *, command: str = COMMAND, **marks: str) -> list[str]:
        """Run the statusLine command as Claude Code does, with only `marks` beside a bare environment."""
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), **marks}
        result = subprocess.run(
            ["/bin/sh", "-c", command], input=json.dumps(payload), capture_output=True, text=True,
            check=True, env=env, cwd=self.home,
        )
        return result.stdout.splitlines()

    def test_settings_command_finds_shipped_colors_relative_to_the_script_folder(self) -> None:
        config = self.home / "account"
        config.mkdir()
        _ = (config / "account-label").write_text("claude4\n", encoding="utf-8")
        payload = {**limits({"used_percentage": 95}, {"used_percentage": 89}),
                   "workspace": {"current_dir": "/tmp/project"},
                   "model": {"display_name": "Opus"},
                   "context_window": ContextWindow(total_input_tokens=90_000, context_window_size=100_000)}
        self.assertEqual(self.command_lines(payload, CLAUDE_CONFIG_DIR=str(config), SHOWRUNNER_UNIT="unit"), [
            "\x1b[94munit director\x1b[0m | \x1b[97mclaude4\x1b[0m | project | \x1b[91m90,000\x1b[0m | Opus",
            "5-hour \x1b[91m5% left\x1b[0m | weekly \x1b[92m11% left\x1b[0m",
        ])

    def test_settings_command_with_a_missing_config_keeps_plain_text(self) -> None:
        scripts = self.home / "scripts/statusline"
        scripts.mkdir(parents=True)
        for name in ("statusline.sh", "statusline.jq"):
            _ = (scripts / name).write_text((PROGRAM.parent / name).read_text(encoding="utf-8"), encoding="utf-8")
        command = f'statusline="{scripts}"; . "$statusline/statusline.sh"'
        payload = {**limits({"used_percentage": 95}, {"used_percentage": 90}),
                   "context_window": ContextWindow(total_input_tokens=90_000, context_window_size=100_000)}
        self.assertEqual(self.command_lines(payload, command=command, PLAN_DELEGATE_TEAM_ROLE="impl"), [
            f"worker | {self.home.name} | 90,000 | ",
            "5-hour 5% left | weekly 10% left",
        ])

    def role(self, session_id: str = "aaaa-1111", **marks: str) -> str:
        return self.first_line(session_id, **marks).split(" | ")[0]

    def test_a_session_with_no_mark_is_unassigned(self) -> None:
        self.assertEqual(self.role(), "unassigned")

    def test_a_session_a_showrunner_timer_targets_is_the_showrunner(self) -> None:
        self.timer("showrunner-other", "session:bbbb-2222")
        self.timer("showrunner-show", "session:aaaa-1111")
        self.assertEqual(self.role(), "showrunner")

    def test_a_unit_promoted_to_showrunner_reads_as_the_showrunner(self) -> None:
        self.timer("showrunner-show", "session:aaaa-1111")
        self.assertEqual(self.role(SHOWRUNNER_UNIT="show"), "showrunner")

    def test_another_session_s_timer_does_not_make_a_showrunner(self) -> None:
        self.timer("showrunner-show", "session:bbbb-2222")
        self.assertEqual(self.role(), "unassigned")

    def test_a_timer_that_is_not_a_showrunner_s_does_not_make_one(self) -> None:
        self.timer("delegate-aaaa-1111", "session:aaaa-1111")
        self.assertEqual(self.role(), "unassigned")

    def test_an_empty_target_does_not_match_an_empty_session_id(self) -> None:
        self.timer("showrunner-show", "session:")
        self.assertEqual(self.role(session_id=""), "unassigned")

    def test_the_notifier_dir_variable_names_where_timers_are(self) -> None:
        self.timer("showrunner-show", "session:aaaa-1111")
        moved = self.home / "moved"
        _ = (self.home / ".local/state/notifier").rename(moved)
        self.assertEqual(self.role(NOTIFIER_STATE_DIR=str(moved)), "showrunner")

    def test_a_unit_mark_is_a_unit_director(self) -> None:
        self.assertEqual(self.role(SHOWRUNNER_UNIT="show", SHOWRUNNER_UNIT_ID="trunk-unit"), "unit director")

    def test_an_empty_unit_mark_is_unassigned(self) -> None:
        self.assertEqual(self.role(SHOWRUNNER_UNIT=""), "unassigned")

    def test_a_worker_under_a_daemon_a_unit_started_is_a_worker(self) -> None:
        self.assertEqual(self.role(PLAN_DELEGATE_TEAM_ROLE="impl", SHOWRUNNER_UNIT="show"), "worker")

    def test_the_empty_team_role_agent_bg_hands_a_friend_is_unassigned(self) -> None:
        self.assertEqual(self.role(PLAN_DELEGATE_TEAM_ROLE=""), "unassigned")

    def test_the_account_label_follows_the_role(self) -> None:
        config = self.home / "config"
        config.mkdir()
        _ = (config / "account-label").write_text("claude4\n", encoding="utf-8")
        self.assertTrue(self.first_line(CLAUDE_CONFIG_DIR=str(config)).startswith("unassigned | claude4 | "))


if __name__ == "__main__":
    _ = unittest.main()
