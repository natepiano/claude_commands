"""Tests for statusline.sh and statusline.jq: the role, the usage line, its reset times, and their zone."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NotRequired, TypedDict, final, override
from zoneinfo import ZoneInfo

from scripts.accounts import new_account
from scripts.production import session_labels, showrunners, staff
from scripts.shutdown import remote

PROGRAM = Path(__file__).with_name("statusline.jq")
SHIPPED_COLORS = PROGRAM.parents[2] / "config/statusline.conf"
# The settings.json statusLine command, naming this folder in place of ~/.claude's.
COMMAND = f'statusline="{Path(__file__).parent}"; . "$statusline/statusline.sh"'
USER_ZONE = ZoneInfo("America/New_York")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SHELLS = ("/bin/sh", "zsh") if shutil.which("zsh") else ("/bin/sh",)
# Where NixOS's glibc finds zone files; Claude Code's environment carries it.
ZONE_FILES = {"TZDIR": os.environ["TZDIR"]} if "TZDIR" in os.environ else {}


def status_lines(*inputs: object, labels: Path | None = None, account: str = "claude1",
                 colors: Path | str = "", own: str = "",
                 counts: str = "", staff_text: str = "", **marks: str) -> list[str]:
    """Run statusline.jq once over every input and return its output lines.

    `colors` is a file path or temporary config text; empty text omits the argument.
    """
    stdin = "\n".join(json.dumps(each) for each in inputs)
    stored = [] if labels is None else ["--slurpfile", "labels", str(labels)]
    parent = ["--arg", "staff", staff_text, "--arg", "own", own, "--arg", "counts", counts]
    with tempfile.TemporaryDirectory() as directory:
        configured: list[str] = []
        if colors:
            path = colors if isinstance(colors, Path) else Path(directory) / "statusline.conf"
            if isinstance(colors, str):
                _ = path.write_text(colors, encoding="utf-8")
            configured = ["--rawfile", "colors", str(path)]
        result = subprocess.run(
            ["jq", "-r", "--arg", "pwd", "/tmp", "--arg", "account", account,
             *stored, *parent, *configured, "-f", str(PROGRAM)],
            input=stdin, capture_output=True, text=True, check=True,
            env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TZ": USER_ZONE.key, **ZONE_FILES, **marks},
        )
    return result.stdout.splitlines()


OWNER = staff.StaffRow(staff.StaffId("9c5e3298-b8da-5d9d-9a8f-1a023259fefd"), "showrunner", "show", None, current_session=staff.StaffId("9c5e3298-b8da-5d9d-9a8f-1a023259fefd"))
UNIT = staff.StaffRow(staff.StaffId("d9962532-4f02-5126-b86a-08c69f70031c"), "unit", "trunk", OWNER.id, current_session=staff.StaffId("d9962532-4f02-5126-b86a-08c69f70031c"))
WORKER = staff.StaffRow(staff.StaffId("d78dc003-79c1-56d5-a006-8568ea72bd81"), "worker", "impl", UNIT.id, current_session=staff.StaffId("d78dc003-79c1-56d5-a006-8568ea72bd81"))


def staff_text(*rows: staff.StaffRow) -> str:
    """The staff writer's own row serialization, shared by jq and shell tests."""
    return json.dumps({"schema": 1, "rows": [row.to_json() for row in rows]})


class ContextWindow(TypedDict, total=False):
    total_input_tokens: int
    context_window_size: int


class ContextInput(TypedDict):
    context_window: ContextWindow


class ShowrunnerRecord(TypedDict):
    sessionId: str
    name: NotRequired[str]


class ColorTests(unittest.TestCase):
    def test_direct_jq_colors_the_entire_staff_assignment(self) -> None:
        line = status_lines({"session_id": UNIT.id}, colors=SHIPPED_COLORS, staff_text=staff_text(OWNER, UNIT))[0]
        self.assertEqual(line.split(" | ")[0], "\x1b[33mtrunk unit for show\x1b[0m")

    def test_direct_jq_with_invalid_or_non_object_staff_text_keeps_both_lines(self) -> None:
        payload = {**limits({"used_percentage": 38}, {"used_percentage": 60}),
                   "workspace": {"current_dir": "/tmp/project"}, "model": {"display_name": "Opus"}}
        for record in ('{"schema":1,"rows":', "42", "[]"):
            with self.subTest(record=record):
                self.assertEqual(status_lines(payload, colors=SHIPPED_COLORS, staff_text=record, STAFF_ID=UNIT.id), [
                    "unassigned | /tmp/project",
                    "\x1b[38;5;137mclaude1\x1b[0m | Opus | weekly \x1b[92m40% left\x1b[0m",
                ])

    def test_the_user_s_examples_render_the_role_with_its_path_then_the_account_model_tokens_and_weekly(self) -> None:
        home = os.environ["HOME"]
        payload = {**limits({"used_percentage": 20}, {"used_percentage": 9}), "session_id": OWNER.id,
                   "model": {"display_name": "Opus 5.5"}, "effort": {"level": "high"},
                   "context_window": ContextWindow(total_input_tokens=103_438, context_window_size=250_000)}
        own = json.dumps({"sessionId": "sid", "name": "natedev"})
        for cwd, path in (("/etc/nixos/", "/etc/nixos"), (f"{home}/rust/hana_catalyst", "~/rust/hana_catalyst"),
                          (home, "~"), (f"{home}x/elsewhere", f"{home}x/elsewhere"), ("/", "/")):
            with self.subTest(cwd=cwd):
                self.assertEqual(status_lines({**payload, "workspace": {"current_dir": cwd}}, own=own,
                                             staff_text=staff_text(staff.StaffRow(OWNER.id, "showrunner", "natedev", None, current_session=OWNER.id)),
                                             STAFF_ID=OWNER.id), [
                    f"natedev showrunner | {path}", "claude1 | Opus 5.5 high | 103,438 | weekly 91% left"])
        unit = json.dumps({"sessionId": "sid", "name": "cables"})
        lines = status_lines({**payload, "session_id": UNIT.id, "workspace": {"current_dir": f"{home}/rust/hana_catalyst_cables"}},
                             own=unit, staff_text=staff_text(
                                 staff.StaffRow(OWNER.id, "showrunner", "hana", None, current_session=OWNER.id),
                                 staff.StaffRow(UNIT.id, "unit", "cables", OWNER.id, current_session=UNIT.id)), STAFF_ID=UNIT.id)
        self.assertEqual(lines[0], "cables unit for hana | ~/rust/hana_catalyst_cables")

    def test_shipped_role_colors_and_plain_worker_and_unassigned(self) -> None:
        cases = (
            ("\x1b[38;5;141mshow showrunner\x1b[0m", OWNER.id),
            ("\x1b[33mtrunk unit for show\x1b[0m", UNIT.id),
            ("worker", WORKER.id),
            ("unassigned", ""),
        )
        for expected, identity in cases:
            with self.subTest(role=expected):
                line = status_lines({"session_id": identity}, colors=SHIPPED_COLORS,
                                    staff_text=staff_text(OWNER, UNIT, WORKER), STAFF_ID=identity)[0]
                self.assertEqual(line.split(" | ")[0], expected)

    def test_shipped_account_colors_and_unknown_label(self) -> None:
        for account, expected in (
            ("claude1", "\x1b[38;5;137mclaude1\x1b[0m"), ("claude2", "\x1b[35mclaude2\x1b[0m"),
            ("claude3", "\x1b[34mclaude3\x1b[0m"), ("claude4", "\x1b[38;5;208mclaude4\x1b[0m"),
            ("claude99", "claude99"),
        ):
            with self.subTest(account=account):
                self.assertEqual(status_lines({}, account=account, colors=SHIPPED_COLORS)[1].split(" | ")[0], expected)

    def test_unassigned_stays_uncolored_without_its_role_color(self) -> None:
        line = status_lines({}, colors="role.showrunner=purple\n")[0]
        self.assertEqual(line.split(" | ")[0], "unassigned")

    def test_named_extended_colors_render_their_256_color_codes(self) -> None:
        for color, code in (("orange", 208), ("purple", 141), ("pink", 211), ("teal", 30), ("brown", 137),
                            ("mauve", 139)):
            with self.subTest(color=color):
                field = status_lines({}, colors=f"account.claude1={color}\n")[1].split(" | ")[0]
                self.assertEqual(field, f"\x1b[38;5;{code}mclaude1\x1b[0m")

    def test_shipped_roles_and_accounts_use_hues_and_accounts_have_distinct_colors(self) -> None:
        accounts: list[str] = []
        for line in SHIPPED_COLORS.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.split("#", 1)[0].partition("=")
            if not separator or not key.strip().startswith(("account.", "role.")):
                continue
            color = value.strip()
            with self.subTest(key=key):
                self.assertNotIn(color, ("white", "bright-white", "black", "bright-black"))
            if key.strip().startswith("account."):
                accounts.append(color)
        self.assertEqual(len(set(accounts)), len(accounts))

    def test_every_new_account_palette_color_is_renderable_by_the_status_line(self) -> None:
        for color in new_account.STATUSLINE_PALETTE:
            with self.subTest(color=color):
                field = status_lines({}, colors=f"account.claude1={color}\n")[1].split(" | ")[0]
                self.assertRegex(field, r"^\x1b\[[0-9;]+mclaude1\x1b\[0m$")

    def test_token_thresholds_use_the_unrounded_window_percentage(self) -> None:
        for tokens, code in ((79_000, 92), (79_999, 92), (80_000, 93),
                             (89_000, 93), (89_999, 93), (90_000, 91)):
            with self.subTest(tokens=tokens):
                payload = ContextInput(context_window=ContextWindow(total_input_tokens=tokens, context_window_size=100_000))
                field = status_lines(payload, colors=SHIPPED_COLORS)[1].split(" | ")[1]
                self.assertEqual(field, f"\x1b[{code}m{tokens:,}\x1b[0m")

    def test_tokens_without_a_positive_window_size_are_uncolored(self) -> None:
        for window in (ContextWindow(total_input_tokens=90_000),
                       ContextWindow(total_input_tokens=90_000, context_window_size=0),
                       ContextWindow(total_input_tokens=90_000, context_window_size=-1)):
            with self.subTest(window=window):
                self.assertEqual(status_lines(ContextInput(context_window=window), colors=SHIPPED_COLORS)[1].split(" | ")[1], "90,000")

    def test_each_usage_window_colors_the_integer_left_at_both_thresholds(self) -> None:
        for name, index in (("5-hour", 1), ("weekly", -1)):
            for used, left, code in ((89, 11, 92), (90, 10, 93), (94, 6, 93), (95, 5, 91), (89.1, 10, 93)):
                # The 5-hour window shows only when under 10% of it is left.
                if name == "5-hour" and left >= 10:
                    continue
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
                    account = "\x1b[38;5;137mclaude1\x1b[0m"
                    if family == "tokens":
                        self.assertEqual(lines[1], f"{account} | 90,000 | 5-hour \x1b[91m5% left\x1b[0m | weekly \x1b[93m10% left\x1b[0m")
                    else:
                        self.assertEqual(lines[1], f"{account} | \x1b[91m90,000\x1b[0m | 5-hour 5% left | weekly 10% left")

    def test_colors_wrap_only_the_fields_and_preserve_both_plain_lines(self) -> None:
        reset = int(datetime(2026, 12, 3, 12, tzinfo=timezone.utc).timestamp())
        payload = {**limits({"used_percentage": 91, "resets_at": reset}, {"used_percentage": 95, "resets_at": reset}),
                   "session_id": OWNER.id, "workspace": {"current_dir": "/tmp/project"},
                   "model": {"display_name": "Opus"}, "effort": {"level": "high"},
                   "context_window": ContextWindow(total_input_tokens=80_000, context_window_size=100_000)}
        plain = status_lines(payload, staff_text=staff_text(OWNER), STAFF_ID=OWNER.id)
        colored = status_lines(payload, staff_text=staff_text(OWNER), STAFF_ID=OWNER.id, colors=SHIPPED_COLORS)
        self.assertEqual([ANSI.sub("", line) for line in colored], plain)
        self.assertEqual(colored, [
            "\x1b[38;5;141mshow showrunner\x1b[0m | /tmp/project",
            "\x1b[38;5;137mclaude1\x1b[0m | Opus high | \x1b[93m80,000\x1b[0m | 5-hour \x1b[93m9% left\x1b[0m until 07:00 EST | weekly \x1b[91m5% left\x1b[0m until Thu 07:00 EST",
        ])

    def test_the_counts_row_colors_each_part_by_its_type_and_keeps_the_number_bold(self) -> None:
        row = "sid For you: 1 question, 2 changes, 130 efficiency, 2 proposals"
        line = status_lines({"session_id": "sid"}, colors=SHIPPED_COLORS, counts=row)[-1]
        self.assertEqual(line, "For you: \x1b[91m\x1b[1m1\x1b[22m question\x1b[0m, "
                         + "\x1b[96m\x1b[1m2\x1b[22m changes\x1b[0m, "
                         + "\x1b[92m\x1b[1m130\x1b[22m efficiency\x1b[0m, "
                         + "\x1b[38;5;141m\x1b[1m2\x1b[22m proposals\x1b[0m")

    def test_a_counts_type_without_a_color_key_keeps_only_its_bold_number(self) -> None:
        row = "sid For you: 2 questions, 1 change, 1 proposal"
        line = status_lines({"session_id": "sid"}, colors="counts.questions=bright-red\n", counts=row)[-1]
        self.assertEqual(line, "For you: \x1b[91m\x1b[1m2\x1b[22m questions\x1b[0m, "
                         + "\x1b[1m1\x1b[22m change, \x1b[1m1\x1b[22m proposal")


def limits(five_hour: dict[str, float | None], seven_day: dict[str, float | None]) -> dict[str, object]:
    return {"rate_limits": {"five_hour": five_hour, "seven_day": seven_day}}


def user_time(epoch: int, fmt: str) -> str:
    return datetime.fromtimestamp(epoch, USER_ZONE).strftime(fmt)


class UsageLineTests(unittest.TestCase):
    def test_reset_times_in_each_season(self) -> None:
        edt = int(datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc).timestamp())
        est = int(datetime(2026, 12, 3, 12, 0, tzinfo=timezone.utc).timestamp())
        lines = status_lines(limits({"used_percentage": 95, "resets_at": edt}, {"used_percentage": 60, "resets_at": est}))
        self.assertEqual(lines[1], "claude1 | 5-hour 5% left until 14:00 EDT | weekly 40% left until Thu 07:00 EST")

    def test_missing_reset_shows_percent_alone(self) -> None:
        lines = status_lines(limits({"used_percentage": 95}, {"used_percentage": 60, "resets_at": None}))
        self.assertEqual(lines[1], "claude1 | 5-hour 5% left | weekly 40% left")

    def test_the_5_hour_window_shows_only_under_10_percent_left(self) -> None:
        for used, line in ((89.1, "claude1 | weekly 40% left"), (90, "claude1 | weekly 40% left"),
                           (91, "claude1 | 5-hour 9% left | weekly 40% left")):
            with self.subTest(used=used):
                self.assertEqual(status_lines(limits({"used_percentage": used}, {"used_percentage": 60}))[1], line)

    def test_missing_rate_limits_leave_the_account_alone_on_the_second_line(self) -> None:
        self.assertEqual(status_lines({})[1:], ["claude1"])
        self.assertEqual(len(status_lines({}, account="")), 1)

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
        inputs = [limits({"used_percentage": 95, "resets_at": t}, {"used_percentage": 0, "resets_at": t}) for t in epochs]
        lines = status_lines(*inputs)[1::2]
        expected = [
            f"claude1 | 5-hour 5% left until {user_time(t, '%H:%M %Z')} | weekly 100% left until {user_time(t, '%a %H:%M %Z')}"
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
            status_lines(self.live, labels=path)[1].split(" | ")[-1],
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

    def test_a_five_hour_reading_with_no_reset_shows_its_figure_alone(self) -> None:
        path = self.labels({"claude1": {"five_hour": {"left": 5.0, "resets_at": None, "at": self.now - 60}}})
        self.assertTrue(status_lines(self.live, labels=path)[1].startswith("claude1 | 5-hour 5% left | weekly"))

    def test_an_address_as_the_account_never_reaches_the_line(self) -> None:
        self.assertNotIn("@", "\n".join(status_lines(self.live, account="someone@example.invalid")))

    def test_a_numbered_claude_label_reaches_the_line_without_its_space(self) -> None:
        line = status_lines(self.live, account="claude 3")[1]
        self.assertTrue(line.startswith("claude3 | "), line)
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

    def test_home_account_label_disappears_after_cutover_even_with_a_stale_cache(self) -> None:
        config = self.home / ".claude"
        config.mkdir()
        _ = (config / "account-label").write_text("claude2\n", encoding="utf-8")
        home_json = self.home / ".claude.json"
        _ = home_json.write_text(json.dumps({"oauthAccount": {"emailAddress": "legacy@example.invalid"}}))
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.assertEqual(ANSI.sub("", self.command_lines({}, shell=shell)[1]), "claude2")
        for content in ("{}", "not JSON", "[]", "42"):
            _ = home_json.write_text(content)
            for shell in SHELLS:
                with self.subTest(home_identity=content, shell=shell):
                    self.assertNotIn("claude2", ANSI.sub("", "\n".join(self.command_lines({}, shell=shell))))
        home_json.unlink()
        home_json.mkdir()
        for shell in SHELLS:
            with self.subTest(home_identity="directory", shell=shell):
                self.assertNotIn("claude2", ANSI.sub("", "\n".join(self.command_lines({}, shell=shell))))

    def first_line(self, session_id: str = "aaaa-1111", **marks: str) -> str:
        return ANSI.sub("", self.command_lines({"session_id": session_id}, **marks)[0])

    def command_lines(self, payload: object, *, command: str = COMMAND, shell: str = "/bin/sh", **marks: str) -> list[str]:
        """Run the statusLine command as Claude Code does, with only `marks` beside a bare environment."""
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), **marks}
        result = subprocess.run(
            [shell, "-c", command], input=json.dumps(payload), capture_output=True, text=True,
            check=True, env=env, cwd=self.home,
        )
        return result.stdout.splitlines()

    def test_reset_times_take_the_zone_from_the_setting_and_its_label(self) -> None:
        reset = int(datetime(2026, 12, 3, 12, tzinfo=timezone.utc).timestamp())
        payload = limits({"used_percentage": 95, "resets_at": reset}, {"used_percentage": 60})
        conf = self.home / "user_zone.conf"
        _ = conf.write_text("# the user's zone\nzone=America/Curacao\n", encoding="utf-8")
        for shell in SHELLS:
            with self.subTest(shell=shell):
                line = self.command_lines(payload, shell=shell, USER_ZONE_CONF=str(conf))[1]
                self.assertIn("5-hour \x1b[91m5% left\x1b[0m until 08:00 AST", line)
                pinned = self.command_lines(payload, shell=shell, USER_ZONE_CONF=str(conf),
                                            USER_ZONE="Europe/Paris")[1]
                self.assertIn("until 13:00 CET", pinned)

    def showrunner_record(self, pid: int, name: str | None, *, session_id: str = "parent-id") -> None:
        directory = self.home / ".claude/sessions"
        directory.mkdir(parents=True, exist_ok=True)
        record = ShowrunnerRecord(sessionId=session_id)
        if name is not None:
            record["name"] = name
        path = directory / f"{pid}.json"
        _ = path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")

    def write_staff(self, *rows: staff.StaffRow) -> Path:
        path = self.home / ".local/state/staff/staff.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(staff_text(*rows), encoding="utf-8")
        return path

    def test_staff_assignment_survives_a_restart_without_any_environment_labels(self) -> None:
        _ = self.write_staff(OWNER, UNIT)
        self.showrunner_record(os.getpid(), "trunk", session_id="own-id")
        for shell in SHELLS:
            with self.subTest(shell=shell):
                line = self.command_lines({"session_id": UNIT.id}, shell=shell)[0]
                self.assertEqual(line.split(" | ")[0], "\x1b[33mtrunk unit for show\x1b[0m")
                self.assertEqual(self.command_lines({"session_id": "other-id"}, shell=shell)[0].split(" | ")[0],
                                 "unassigned")

    def test_current_staff_parent_and_name_are_read_each_refresh(self) -> None:
        for name in ("initial", "renamed"):
            _ = self.write_staff(staff.StaffRow(OWNER.id, "showrunner", name, None, current_session=OWNER.id), UNIT)
            for shell in SHELLS:
                with self.subTest(name=name, shell=shell):
                    self.assertEqual(ANSI.sub("", self.command_lines({"session_id": UNIT.id}, shell=shell)[0]),
                                     f"trunk unit for {name} | ~")

    def test_staff_identity_wins_over_stale_environment_and_the_registry_name(self) -> None:
        _ = self.write_staff(OWNER, UNIT, WORKER)
        self.showrunner_record(os.getpid(), "show", session_id="own-id")
        for shell in SHELLS:
            with self.subTest(shell=shell):
                line = self.command_lines({"session_id": WORKER.id}, shell=shell, STAFF_ID=OWNER.id,
                                          SHOWRUNNER_UNIT="wrong", UNIT_TEAM_ROLE="director")[0]
                self.assertEqual(line, "worker | ~")
                self.assertEqual(self.command_lines({"session_id": OWNER.id}, shell=shell, STAFF_ID=WORKER.id,
                                                    SHOWRUNNER_UNIT="wrong")[0].split(" | ")[0],
                                 "\x1b[38;5;141mshow showrunner\x1b[0m")

    def test_unknown_staff_identity_never_adopts_a_same_named_row(self) -> None:
        _ = self.write_staff(OWNER, UNIT)
        self.showrunner_record(os.getpid(), "trunk", session_id="own-id")
        self.assertTrue(self.command_lines({"session_id": "own-id"}, STAFF_ID="s99999999999")[0]
                        .startswith("unassigned | "))

    def test_missing_damaged_and_wrong_schema_staff_render_without_environment_fallback(self) -> None:
        for text in ("", "{", "[]", '{"schema":3,"rows":[]}', '{"schema":1,"rows":[42]}',
                     json.dumps({"schema": 2, "rows": [{**UNIT.to_json(), "worktree": "bad"}]}),
                     json.dumps({"schema": 2, "rows": [{**OWNER.to_json(), "doc": {"state": "live"}}]})):
            path = self.write_staff()
            _ = path.write_text(text, encoding="utf-8")
            for shell in SHELLS:
                with self.subTest(text=text, shell=shell):
                    lines = self.command_lines({"model": {"display_name": "Opus"}}, shell=shell,
                                               SHOWRUNNER_UNIT="stale", UNIT_TEAM_ROLE="impl")
                    self.assertEqual(lines, ["unassigned | ~", "Opus"])
        path.unlink()
        self.assertEqual(self.first_line(SHOWRUNNER_UNIT="stale"), "unassigned | ~")

    def test_missing_parent_keeps_the_unit_role_without_inventing_a_parent_name(self) -> None:
        _ = self.write_staff(UNIT)
        self.assertEqual(ANSI.sub("", self.command_lines({"session_id": UNIT.id})[0]), "trunk unit | ~")

    def test_staff_file_override_is_the_same_as_the_python_reader(self) -> None:
        path = self.write_staff(OWNER, UNIT)
        moved = self.home / "moved-staff.json"
        _ = path.rename(moved)
        self.assertEqual(ANSI.sub("", self.command_lines({"session_id": UNIT.id}, STAFF_FILE=str(moved))[0]),
                         "trunk unit for show | ~")

    def test_jq_and_queue_reader_agree_on_identity_unique_names_and_ambiguity(self) -> None:
        other = staff.StaffRow(staff.StaffId("9ac86c12-2241-576a-ba84-431dc42bb778"), "showrunner", "other", None, current_session=staff.StaffId("9ac86c12-2241-576a-ba84-431dc42bb778"))
        duplicate = staff.StaffRow(staff.StaffId("589d5100-ff94-52e9-a854-d37bc466fdd3"), "unit", "trunk", other.id, current_session=staff.StaffId("589d5100-ff94-52e9-a854-d37bc466fdd3"))
        rows = (OWNER, UNIT, WORKER)
        for members in (rows, (*rows, other, duplicate)):
            _ = self.write_staff(*members)
            reader = session_labels.StaffLabels(staff.StaffSnapshot(members), {}, {})
            for identity, name in (("", "trunk"), ("", "trunk-unit"), ("", "trunk_unit"),
                                   ("", "show"), ("", "missing"), ("", "bad name"),
                                   (UNIT.id, "show"), (OWNER.id, "trunk"), (WORKER.id, "trunk"),
                                   ("s99999999999", "trunk")):
                with self.subTest(identity=identity, name=name, duplicate=len(members) == 5):
                    environment = {"STAFF_ID": identity} if identity else {}
                    resolved = reader.environment(environment, identity)
                    row = reader.record.by_id(staff.StaffId(resolved.get("CLAUDE_CODE_SESSION_ID", "")))
                    self.showrunner_record(os.getpid(), name, session_id="own-id")
                    role = ANSI.sub("", self.command_lines({"session_id": identity}, **environment)[0]).split(" | ")[0]
                    if row is None:
                        self.assertEqual(role, "unassigned")
                    elif row.kind == "worker":
                        self.assertEqual(role, "worker")
                    elif row.kind == "showrunner":
                        self.assertEqual(role, f"{row.name} showrunner")
                    else:
                        parent = reader.record.by_id(row.parent) if row.parent is not None else None
                        self.assertIsNotNone(parent)
                        assert parent is not None
                        self.assertEqual(role, f"{row.name} unit for {parent.name}")

    def test_python_and_jq_share_timer_folder_and_name_precedence(self) -> None:
        machine = remote.local_machine()
        doc = self.home / "runner's production.md"
        runner = replace(OWNER, name="hana", doc=staff.LiveDoc(machine, doc))
        folder = str(self.home / "geometry material")
        unit = replace(UNIT, name="geometry-material", worktree=staff.PresentWorktree(machine, Path(folder)))
        other = staff.StaffRow(staff.StaffId("589d5100-ff94-52e9-a854-d37bc466fdd3"), "showrunner", "other", None, current_session=staff.StaffId("589d5100-ff94-52e9-a854-d37bc466fdd3"))
        duplicate = replace(unit, id=staff.StaffId("9ac86c12-2241-576a-ba84-431dc42bb778"), parent=other.id)
        source = showrunners.ShowrunnerSource(showrunners.Timer("different-slug", "session:runner-id", doc, "UTC"), "")
        sources = {source.timer.slug: source}
        instance = self.home / ".local/state/notifier/showrunner-different-slug"
        instance.mkdir(parents=True)
        _ = doc.write_text("# Production\n")
        _ = (instance / "conf").write_text(
            f"TARGET=session:runner-id\nCHECK=zsh /scripts/production_check.sh {shlex.quote(str(doc))}\n")
        cases: list[tuple[str, tuple[staff.StaffRow, ...], dict[str, str], str, str, str, str]] = [
            ("folder gives no identity", (runner, unit, other), {}, "own-id", "other", folder, ""),
            ("timer gives no identity", (runner, unit, other), {}, "runner-id", "other", folder, ""),
            ("native unit before folder and name", (runner, unit, other), {}, unit.id, "other", "/elsewhere", unit.id),
            ("native worker ignores inherited identity", (runner, unit, other, WORKER), {"STAFF_ID": runner.id}, WORKER.id, "hana", folder, WORKER.id),
            ("runtime before timer", (runner, unit, other), {"STAFF_ID": other.id}, runner.id, "", folder, runner.id),
            ("unknown identity", (runner, unit, other), {"STAFF_ID": "s99999999999"}, "runner-id", "hana", folder, ""),
            ("duplicate folders", (runner, unit, other, duplicate), {}, "own-id", "organon", folder, ""),
            ("duplicate falls to name", (runner, unit, other, duplicate), {}, "own-id", "other", folder, ""),
            ("remote folder ignored", (runner, replace(unit, worktree=staff.PresentWorktree("elsewhere", Path(folder))), other),
             {}, "own-id", "other", folder, ""),
            ("removed folder ignored", (runner, replace(unit, worktree=staff.RemovedWorktree(machine, Path(folder))), other),
             {}, "own-id", "organon", folder, ""),
            ("unfilled folder ignored", (runner, replace(unit, worktree=staff.WorktreeUnfilled()), other),
             {}, "own-id", "organon", folder, ""),
            ("descendant is not equal", (runner, unit, other), {}, "own-id", "organon", folder + "/child", ""),
            ("prefix is not equal", (runner, unit, other), {}, "own-id", "organon", folder + "-other", ""),
            ("empty cwd", (runner, unit, other), {}, "own-id", "organon", "", ""),
            ("name fallback", (runner, unit, other), {}, "own-id", "geometry-material_unit", "/elsewhere", ""),
            ("retained name fallback", (runner, unit, other), {"SHOWRUNNER_UNIT_ID": "geometry-material-unit"},
             "own-id", "organon", "/elsewhere", ""),
        ]
        for label, rows, environment, session_id, name, cwd, expected in cases:
            with self.subTest(case=label):
                path = self.write_staff(*rows)
                # Exercise the schema emitted by staff's current writer.
                _ = path.write_text(json.dumps({"schema": 2, "rows": [row.to_json() for row in rows]}))
                reader = session_labels.StaffLabels(staff.StaffSnapshot(rows), sources, {source.timer.slug: runner})
                resolved = reader.environment(environment, session_id)
                row = reader.record.by_id(staff.StaffId(resolved.get("CLAUDE_CODE_SESSION_ID", "")))
                self.assertEqual(row.id if row is not None else "", expected)
                self.showrunner_record(os.getpid(), name, session_id=session_id)
                role = ("worker" if row is not None and row.kind == "worker"
                        else f"{row.name} unit for hana" if row is not None and row.kind == "unit"
                        else f"{row.name} showrunner" if row is not None else "unassigned")
                for shell in SHELLS:
                    rendered = self.command_lines({"session_id": session_id, "cwd": cwd}, shell=shell, **environment)
                    self.assertEqual(ANSI.sub("", rendered[0]).split(" | ")[0], role)

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_zsh_settings_command_renders_both_lines_without_any_notifier_directory(self) -> None:
        self.assertFalse((self.home / ".local/state/notifier").exists())
        payload = {**limits({"used_percentage": 38}, {"used_percentage": 60}),
                   "workspace": {"current_dir": "/tmp/project"},
                   "model": {"display_name": "Opus"}}

        lines = self.command_lines(payload, shell="zsh")

        self.assertEqual(lines, [
            "unassigned | /tmp/project",
            "Opus | weekly \x1b[92m40% left\x1b[0m",
        ])
        self.assertFalse((self.home / ".local/state/notifier").exists())

    def test_settings_command_finds_shipped_colors_relative_to_the_script_folder(self) -> None:
        config = self.home / "account"
        config.mkdir()
        _ = (config / "account-label").write_text("claude4\n", encoding="utf-8")
        payload = {**limits({"used_percentage": 95}, {"used_percentage": 89}),
                   "workspace": {"current_dir": "/tmp/project"},
                   "model": {"display_name": "Opus"},
                   "context_window": ContextWindow(total_input_tokens=90_000, context_window_size=100_000)}
        _ = self.write_staff(OWNER, UNIT)
        self.assertEqual(self.command_lines({**payload, "session_id": UNIT.id}, CLAUDE_CONFIG_DIR=str(config)), [
            "\x1b[33mtrunk unit for show\x1b[0m | /tmp/project",
            "\x1b[38;5;208mclaude4\x1b[0m | Opus | \x1b[91m90,000\x1b[0m | 5-hour \x1b[91m5% left\x1b[0m | weekly \x1b[92m11% left\x1b[0m",
        ])

    def test_settings_command_with_a_missing_config_keeps_plain_text(self) -> None:
        scripts = self.home / "scripts/statusline"
        scripts.mkdir(parents=True)
        for name in ("statusline.sh", "statusline.jq"):
            _ = (scripts / name).write_text((PROGRAM.parent / name).read_text(encoding="utf-8"), encoding="utf-8")
        command = f'statusline="{scripts}"; . "$statusline/statusline.sh"'
        payload = {**limits({"used_percentage": 95}, {"used_percentage": 90}),
                   "context_window": ContextWindow(total_input_tokens=90_000, context_window_size=100_000)}
        _ = self.write_staff(OWNER, UNIT, WORKER)
        self.assertEqual(self.command_lines({**payload, "session_id": WORKER.id}, command=command), [
            "worker | ~",
            "90,000 | 5-hour 5% left | weekly 10% left",
        ])

    def role(self, session_id: str = "aaaa-1111", **marks: str) -> str:
        return self.first_line(session_id, **marks).split(" | ")[0]

    def test_a_session_with_no_mark_is_unassigned(self) -> None:
        self.assertEqual(self.role(), "unassigned")

    def test_notifier_timers_and_inherited_environment_do_not_assign_a_session(self) -> None:
        self.timer("showrunner-show", "session:aaaa-1111")
        self.assertEqual(self.role(SHOWRUNNER_UNIT="show", UNIT_TEAM_ROLE="impl"), "unassigned")

    def test_the_account_label_follows_the_role(self) -> None:
        config = self.home / "config"
        config.mkdir()
        _ = (config / "account-label").write_text("claude4\n", encoding="utf-8")
        lines = [ANSI.sub("", line) for line in self.command_lines({"session_id": "aaaa-1111"},
                                                                    CLAUDE_CONFIG_DIR=str(config))]
        self.assertEqual(lines[0].split(" | ")[0], "unassigned")
        self.assertEqual(lines[1], "claude4")



class OutstandingItem(TypedDict):
    since: str
    text: str
    after: NotRequired[str]


@final
class CountsRowTests(unittest.TestCase):
    """The third line: the `For you: …` counts row of a session a production targets, kept in a cache."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home = Path()
        self.ledger = Path()
        self.cache = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        doc = self.home / "docs/plans/show-production.md"
        doc.parent.mkdir(parents=True)
        self.ledger = self.home / "ledger.md"
        _ = doc.write_text("# show\n\n> **Status: PRODUCTION — running**\n"
                           + f"- **Improvement queue:** {self.ledger}\n", encoding="utf-8")
        instance = self.home / ".local/state/notifier/showrunner-show"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(f"TARGET=session:{OWNER.id}\nCHECK=zsh /x/production_check.sh {doc}\n",
                                           encoding="utf-8")
        self.cache = self.home / ".local/state/showrunner/counts/show"
        path = self.home / ".local/state/staff/staff.json"
        path.parent.mkdir(parents=True)
        _ = path.write_text(staff_text(OWNER), encoding="utf-8")


    def questions(self, *items: OutstandingItem) -> None:
        path = self.home / ".local/state/showrunner/outstanding/show.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(list(items)), encoding="utf-8")

    def lines(self, session_id: str = OWNER.id, shell: str = "/bin/sh") -> list[str]:
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "STAFF_ID": OWNER.id}
        result = subprocess.run([shell, "-c", COMMAND], input=json.dumps({"session_id": session_id}),
                                capture_output=True, text=True, check=True, env=env, cwd=self.home)
        return [ANSI.sub("", line) for line in result.stdout.splitlines()]

    def test_the_session_a_production_targets_shows_its_counts_under_the_other_rows(self) -> None:
        _ = self.ledger.write_text("- 2026-10-16 Idea (idea.md) — proposed\n", encoding="utf-8")
        self.questions({"since": "2026-10-16T19:00", "text": "Vault promotion"})
        for shell in SHELLS:
            with self.subTest(shell=shell):
                lines = self.lines(shell=shell)
                self.assertTrue(lines[0].startswith("show showrunner | "), lines)
                self.assertEqual(lines[-1], "For you: 1 question, 1 proposal")
                self.assertNotIn("project", lines[-1])
                self.assertNotIn("For you: 1 question, 1 proposal", self.lines("bbbb-2222", shell=shell))

    def test_each_count_in_the_row_is_bold_inside_its_shipped_color(self) -> None:
        _ = self.ledger.write_text("- 2026-10-16 Idea (idea.md) — proposed\n", encoding="utf-8")
        self.questions(*(OutstandingItem(since="2026-10-16T19:00", text=f"Q{n}") for n in range(12)))
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "STAFF_ID": OWNER.id}
        result = subprocess.run(["/bin/sh", "-c", COMMAND], input=json.dumps({"session_id": OWNER.id}),
                                capture_output=True, text=True, check=True, env=env, cwd=self.home)
        self.assertEqual(result.stdout.splitlines()[-1],
                         "For you: \x1b[91m\x1b[1m12\x1b[22m questions\x1b[0m, "
                         + "\x1b[38;5;141m\x1b[1m1\x1b[22m proposal\x1b[0m")

    def test_the_row_counts_efficiency_and_refreshes_when_the_ledger_changes(self) -> None:
        ledger = self.home / "efficiency.md"
        doc = self.home / "docs/plans/show-production.md"
        _ = doc.write_text(doc.read_text(encoding="utf-8") + f"- **Efficiency ledger:** {ledger}\n",
                           encoding="utf-8")
        _ = self.ledger.write_text("- 2026-10-19 Faster tests (tests.md) — proposed\n", encoding="utf-8")
        self.questions({"since": "2026-10-19T04:00", "text": "Review the study"})
        for shell in SHELLS:
            with self.subTest(shell=shell):
                _ = ledger.write_text("- 2026-10-19 config: First — proposed\n"
                                      + "- 2026-10-19 messages: Second — deferred\n"
                                      + "- 2026-10-19 rust: Done — accepted, applied\n", encoding="utf-8")
                self.assertEqual(self.lines(shell=shell)[-1],
                                 "For you: 1 question, 2 efficiency, 1 proposal")
                _ = ledger.write_text("- 2026-10-19 config: First — accepted, applied\n"
                                      + "- 2026-10-19 messages: Second — deferred\n", encoding="utf-8")
                self.assertEqual(self.lines(shell=shell)[-1],
                                 "For you: 1 question, 1 efficiency, 1 proposal")

    def test_nothing_counted_shows_no_row(self) -> None:
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(self.cache.read_text(encoding="utf-8").splitlines()[:2], ["", ""])

    def test_the_cache_is_rebuilt_only_when_a_file_it_was_read_from_changes(self) -> None:
        self.questions({"since": "2026-10-16T19:00", "text": "A"})
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.assertEqual(self.lines(shell=shell)[-1], "For you: 1 question")
                built = self.cache.stat().st_mtime_ns
                self.assertEqual(self.lines(shell=shell)[-1], "For you: 1 question")
                self.assertEqual(self.cache.stat().st_mtime_ns, built)
                self.questions({"since": "2026-10-16T19:00", "text": "A"}, {"since": "2026-10-16T19:00", "text": "B"})
                self.assertEqual(self.lines(shell=shell)[-1], "For you: 2 questions")
                # The ledger did not exist, so its folder stands in until it appears.
                _ = self.ledger.write_text("- 2026-10-16 Idea (idea.md) — proposed\n", encoding="utf-8")
                self.assertEqual(self.lines(shell=shell)[-1], "For you: 2 questions, 1 proposal")
                self.ledger.unlink()
                self.questions({"since": "2026-10-16T19:00", "text": "A"})

    def test_a_deferred_question_joins_the_row_when_its_time_comes(self) -> None:
        later = datetime.now(USER_ZONE) + timedelta(hours=1)
        outstanding = self.home / ".local/state/showrunner/outstanding/show.json"
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.questions({"since": "2026-10-16T19:00", "text": "A", "after": later.strftime("%Y-%m-%dT%H:%M")})
                self.assertEqual(len(self.lines(shell=shell)), 1)
                row, due, *read_from = self.cache.read_text(encoding="utf-8").splitlines()
                self.assertEqual((row, int(due)), ("", int(later.replace(second=0, microsecond=0).timestamp())))
                # The hour has passed: the item's time and the cache's due second move back, each file kept older.
                built = self.cache.stat().st_mtime_ns
                self.questions({"since": "2026-10-16T19:00", "text": "A", "after": "2026-10-16T19:00"})
                os.utime(outstanding, ns=(built - 10**9, built - 10**9))
                _ = self.cache.write_text("\n".join(["", str(int(time.time()) - 1), *read_from]) + "\n",
                                          encoding="utf-8")
                os.utime(self.cache, ns=(built, built))
                self.assertEqual(self.lines(shell=shell)[-1], "For you: 1 question")

if __name__ == "__main__":
    _ = unittest.main()
