"""Test command counts and retained records with scratch homes."""
from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from functools import cached_property
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import cast, final, override
import unittest
from unittest.mock import patch

from scripts.lib.transcript_test_support import compressed_copy
from scripts.usage import command_use as usage
from scripts.usage.context_economy_cache import ParseCheckpoint, TranscriptReadSettings

BEGIN = datetime(2026, 11, 1, tzinfo=timezone.utc)
SETTINGS = TranscriptReadSettings(1, 3600)


def at(day: int) -> datetime:
    return BEGIN + timedelta(days=day)


def command_entry(name: str, day: int, **fields: object) -> dict[str, object]:
    return {"type": "user", "timestamp": at(day).isoformat(),
            "message": {"content": f"<command-name>{name}</command-name>"}, **fields}


@final
class CommandUseTests(unittest.TestCase):
    @cached_property
    def root(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    @property
    def home(self) -> Path:
        return self.root / "home"

    @override
    def setUp(self) -> None:
        self.home.mkdir()
        configuration = self.root / "config"
        configuration.mkdir()
        _ = (configuration / "context_economy.conf").write_text(
            "# whole: 1..\ncommand_use_launcher_window_seconds = 30\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "CONF_DIR": str(configuration),
            "NOTIFY_STATE_DIR": str(self.home / ".local/state/notify")})))

    def transcript(self, name: str, rows: list[dict[str, object]], *, account: str = ".claude") -> Path:
        path = self.home / account / "projects/fixture" / (name + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_transcript_fields_separate_user_and_automated_commands(self) -> None:
        rows = [command_entry("/work", 0, origin={"kind": "human"})]
        for day, evidence in enumerate((
            {"isMeta": True}, {"isSidechain": True}, {"origin": {"kind": "task-notification"}},
            {"source": {"type": "postmaster"}}, {"source": "pane-input"},
            {"entrypoint": "sdk-cli"}, {"sessionKind": "headless"},
            {"sourceToolAssistantUUID": "fixture-assistant"}, {"forwardedIntent": {"source": "launcher"}},
        ), 1):
            rows.append(command_entry("/work", day, **evidence))
        rows.append({"type": "assistant", "timestamp": at(12).isoformat(), "message": {"content": [
            {"type": "text", "text": "<command-name>/ignored</command-name>"},
            {"type": "tool_use", "name": "Skill", "input": {"skill": "unit:work", "args": "fixture"}},
        ]}})
        _ = self.transcript("main", rows)
        _ = self.transcript("other", [command_entry("/work", 10)], account=".claude2")
        histories = usage.read_histories(self.home, SETTINGS)
        self.assertEqual((histories["/work"].user_count, histories["/work"].agent_count), (2, 9))
        self.assertEqual((histories["/work"].first_seen, histories["/work"].last_seen), (at(0), at(10)))
        self.assertEqual((histories["/unit:work"].user_count, histories["/unit:work"].agent_count), (0, 1))
        self.assertNotIn("/ignored", histories)

    def test_compressed_transcript_uses_the_same_cached_summary(self) -> None:
        plain = self.transcript("archive", [command_entry("/archive", 0)])
        compressed = compressed_copy(plain)
        with patch.object(usage, "parse_transcript", wraps=usage.parse_transcript) as parse:
            histories = usage.read_histories(self.home, SETTINGS)
        parse.assert_called_once_with(compressed.resolve(), ParseCheckpoint())
        self.assertEqual(histories["/archive"].user_count, 1)
        with patch.object(usage, "parse_transcript", side_effect=AssertionError("Do not parse an unchanged archive.")):
            self.assertEqual(usage.read_histories(self.home, SETTINGS), histories)

    def test_append_resumes_at_the_saved_offset_without_duplicate_counts(self) -> None:
        path = self.transcript("appended", [command_entry("/work", 0)])
        histories = usage.read_histories(self.home, SETTINGS)
        self.assertEqual(histories["/work"].user_count, 1)
        offset = path.stat().st_size
        tail = json.dumps(command_entry("/work", 1, isMeta=True)).encode() + b"\n"
        with path.open("ab") as stream:
            _ = stream.write(tail[:len(tail) // 2])
        self.assertEqual(usage.read_histories(self.home, SETTINGS), histories)
        with path.open("ab") as stream:
            _ = stream.write(tail[len(tail) // 2:])
        with patch.object(usage, "parse_transcript", wraps=usage.parse_transcript) as parse:
            resumed = usage.read_histories(self.home, SETTINGS)
        self.assertEqual(cast(ParseCheckpoint, parse.call_args.args[1]).offset, offset)
        self.assertEqual((resumed["/work"].user_count, resumed["/work"].agent_count), (1, 1))
        self.assertEqual(resumed["/work"].last_seen, at(1))
        with patch.object(usage, "parse_transcript", side_effect=AssertionError("Do not parse an unchanged file.")):
            self.assertEqual(usage.read_histories(self.home, SETTINGS), resumed)

    def test_deleted_transcript_and_deleted_numbered_home_keep_counts(self) -> None:
        path = self.transcript("deleted", [command_entry("/retained", 0)], account=".claude3")
        histories = usage.read_histories(self.home, SETTINGS)
        path.unlink()
        shutil.rmtree(self.home / ".claude3")
        with patch.object(usage, "parse_transcript", side_effect=AssertionError("Do not parse a deleted file.")):
            self.assertEqual(usage.read_histories(self.home, SETTINGS), histories)
        self.assertEqual(histories["/retained"].user_count, 1)

    def test_trimmed_transcript_keeps_old_counts_and_dates(self) -> None:
        old = command_entry("/work", 0, uuid="old-use")
        kept = command_entry("/work", 1, uuid="kept-use", isMeta=True)
        _ = self.transcript("trimmed", [old, kept])
        self.assertEqual(usage.read_histories(self.home, SETTINGS)["/work"],
                         usage.CommandHistory(1, 1, at(0), at(1)))
        _ = self.transcript("trimmed", [kept])
        self.assertEqual(usage.read_histories(self.home, SETTINGS)["/work"],
                         usage.CommandHistory(1, 1, at(0), at(1)))
        _ = self.transcript("trimmed", [kept, command_entry("/work", 2, uuid="new-use")])
        self.assertEqual(usage.read_histories(self.home, SETTINGS)["/work"],
                         usage.CommandHistory(2, 1, at(0), at(2)))

    def test_reparse_without_a_checkpoint_does_not_count_an_event_twice(self) -> None:
        _ = self.transcript("reparsed", [command_entry("/work", 0, uuid="stable-use")])
        histories = usage.read_histories(self.home, SETTINGS)
        with closing(sqlite3.connect(self.home / ".cache/context_economy/command_use.sqlite3")) as database, database:
            _ = database.execute("DELETE FROM summaries")
        with patch.object(usage, "parse_transcript", wraps=usage.parse_transcript) as parse:
            self.assertEqual(usage.read_histories(self.home, SETTINGS), histories)
        parse.assert_called_once()
        self.assertEqual(histories["/work"], usage.CommandHistory(1, 0, at(0), at(0)))

    def prompt_evidence(self, source: str, day: int) -> dict[str, object]:
        return {"event_id": f"submission-{day}", "session_id": "director-session",
                "prompt_hash": hashlib.sha256(b"/unit:direct docs/plan.md").hexdigest(),
                "timestamp_utc": at(day).isoformat(), "prompt_source": source}

    def director_command(self, identity: str, when: datetime, *, session: str = "director-session") -> dict[str, object]:
        return command_entry("/unit:direct", 0, uuid=identity, sessionId=session, timestamp=when.isoformat(),
                             origin={"kind": "human"}, message={"content":
                                 "<command-name>/unit:direct</command-name>\n"
                                 + "<command-args>docs/plan.md</command-args>"})

    def journal(self, rows: list[dict[str, object]]) -> Path:
        path = self.home / ".local/state/notify/auto-silence-events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def test_launcher_command_uses_the_hook_evidence_for_that_submission(self) -> None:
        rows = [self.director_command(f"use-{day}", at(day)) for day in (0, 1)]
        _ = self.transcript("director-session", rows)
        self.assertEqual(usage.read_histories(self.home, SETTINGS)["/unit:direct"].user_count, 2)
        journal = self.journal([self.prompt_evidence(source, day)
                                for day, source in enumerate(("ScriptPrompt", "UserTypedPrompt"))])
        histories = usage.read_histories(self.home, SETTINGS)
        self.assertEqual(histories["/unit:direct"], usage.CommandHistory(1, 1, at(0), at(1)))
        # A later manual use of the same command stays a user use.
        with patch.object(usage, "parse_transcript", side_effect=AssertionError("Do not reparse for new launcher evidence.")):
            self.assertEqual(usage.read_histories(self.home, SETTINGS), histories)
        journal.unlink()
        self.assertEqual(usage.read_histories(self.home, SETTINGS), histories)

    def test_missing_launcher_entry_does_not_claim_a_later_manual_command(self) -> None:
        manual = self.director_command("manual", at(1))
        _ = self.transcript("director-session", [manual])
        _ = self.journal([self.prompt_evidence("ScriptPrompt", 0)])
        self.assertEqual(usage.read_histories(self.home, SETTINGS)["/unit:direct"],
                         usage.CommandHistory(1, 0, at(1), at(1)))
        launched_at = at(0) + timedelta(seconds=5)
        launcher = self.director_command("launcher", launched_at)
        # The earlier launcher entry arrives after the manual entry in both read orders.
        _ = self.transcript("director-session", [manual, launcher])
        expected = usage.CommandHistory(1, 1, launched_at, at(1))
        self.assertEqual(usage.read_histories(self.home, SETTINGS)["/unit:direct"], expected)
        with closing(sqlite3.connect(self.home / ".cache/context_economy/command_use.sqlite3")) as database, database:
            _ = database.execute("DELETE FROM summaries")
        _ = self.transcript("director-session", [launcher, manual])
        self.assertEqual(usage.read_histories(self.home, SETTINGS)["/unit:direct"], expected)

    def test_launcher_evidence_matches_once_within_the_configured_window(self) -> None:
        configuration = Path(os.environ["CONF_DIR"]) / "context_economy.conf"
        _ = configuration.write_text("# whole: 1..\ncommand_use_launcher_window_seconds = 7\n")
        unmatched = [
            self.director_command("outside", at(0) + timedelta(seconds=8)),
            self.director_command("before", at(0) - timedelta(seconds=8)),
            self.director_command("other-session", at(0) + timedelta(seconds=4), session="other-session"),
        ]
        _ = self.transcript("director-session", unmatched)
        _ = self.journal([self.prompt_evidence("ScriptPrompt", 0)])
        histories = usage.read_histories(self.home, SETTINGS)
        self.assertEqual((histories["/unit:direct"].user_count, histories["/unit:direct"].agent_count), (3, 0))
        _ = self.transcript("director-session", [
            self.director_command("second", at(0) + timedelta(seconds=6)),
            self.director_command("first", at(0) + timedelta(seconds=5)),
            *unmatched,
        ])
        histories = usage.read_histories(self.home, SETTINGS)
        self.assertEqual((histories["/unit:direct"].user_count, histories["/unit:direct"].agent_count), (4, 1))
        with closing(sqlite3.connect(self.home / ".cache/context_economy/command_use_events.sqlite3")) as database:
            actors = cast(list[tuple[str]], database.execute("SELECT DISTINCT actor FROM command_events").fetchall())
        self.assertEqual(actors, [("user",)])

    def test_delayed_launcher_evidence_marks_the_entry_but_not_a_distant_manual_use(self) -> None:
        _ = self.transcript("director-session", [
            self.director_command("launcher", at(0)),
            self.director_command("manual", at(1)),
        ])
        evidence = self.prompt_evidence("ScriptPrompt", 0)
        evidence["timestamp_utc"] = (at(0) + timedelta(seconds=1)).isoformat()
        _ = self.journal([evidence])
        self.assertEqual(usage.read_histories(self.home, SETTINGS)["/unit:direct"],
                         usage.CommandHistory(1, 1, at(0), at(1)))

    def check_nearest_launcher_entry(self, *, metadata: bool, tied: bool) -> None:
        original_observe = usage.CommandHistory.observe
        observed: dict[usage.UseTime, usage.UseActor] = {}

        def observe(history: usage.CommandHistory, actor: usage.UseActor, when: usage.UseTime) -> None:
            observed[when] = actor
            original_observe(history, actor, when)

        manual_at = at(0) - timedelta(seconds=5)
        launched_at = at(0) + timedelta(seconds=5 if tied else 0)
        launcher = self.director_command("launcher", launched_at)
        launcher["isMeta"] = metadata
        _ = self.transcript("director-session", [
            self.director_command("manual", manual_at), launcher,
        ])
        _ = self.journal([self.prompt_evidence("ScriptPrompt", 0)])
        with patch.object(usage.CommandHistory, "observe", observe):
            history = usage.read_histories(self.home, SETTINGS)["/unit:direct"]
        self.assertEqual(history, usage.CommandHistory(1, 1, manual_at, launched_at))
        self.assertEqual(observed, {
            manual_at: usage.UseActor.USER, launched_at: usage.UseActor.AGENT,
        })

    def test_launcher_evidence_uses_the_nearest_entry(self) -> None:
        self.check_nearest_launcher_entry(metadata=False, tied=False)

    def test_launcher_evidence_uses_the_later_entry_on_a_tie(self) -> None:
        self.check_nearest_launcher_entry(metadata=False, tied=True)

    def test_metadata_marked_launcher_does_not_change_the_nearby_manual_entry(self) -> None:
        self.check_nearest_launcher_entry(metadata=True, tied=False)

    def test_metadata_marked_launcher_uses_the_evidence_on_a_tie(self) -> None:
        self.check_nearest_launcher_entry(metadata=True, tied=True)

    def test_durable_events_above_the_budget_keep_checkpoints_and_skip_unchanged_parsing(self) -> None:
        path = self.transcript("large-events", [command_entry("/work", 0, uuid=f"{index}-" + "x" * 1024)
                                                for index in range(2000)])
        histories = usage.read_histories(self.home, SETTINGS)
        self.assertEqual(histories["/work"].user_count, 2000)
        folder = self.home / ".cache/context_economy"
        with closing(sqlite3.connect(folder / "command_use_events.sqlite3")) as database:
            pages = cast(int, database.execute("PRAGMA page_count").fetchone()[0])
            page_size = cast(int, database.execute("PRAGMA page_size").fetchone()[0])
        self.assertGreater(pages * page_size, SETTINGS.cache_max_megabytes * 1024 * 1024)
        with closing(sqlite3.connect(folder / "command_use.sqlite3")) as database:
            checkpoint = cast(tuple[int], database.execute(
                "SELECT offset FROM summaries WHERE path = ?", (str(path.resolve()),)).fetchone())
        self.assertEqual(checkpoint, (path.stat().st_size,))
        with patch.object(usage, "parse_transcript", side_effect=AssertionError("Durable events must not evict checkpoints.")):
            self.assertEqual(usage.read_histories(self.home, SETTINGS), histories)

    def test_event_database_migration_keeps_deleted_history_and_rechecks_available_metadata(self) -> None:
        deleted = self.transcript("deleted", [command_entry("/deleted", 0, uuid="deleted", isMeta=True)])
        _ = self.transcript("manual", [command_entry("/manual", 1, uuid="manual")])
        expected = usage.read_histories(self.home, SETTINGS)
        deleted.unlink()
        folder = self.home / ".cache/context_economy"
        events = folder / "command_use_events.sqlite3"
        with closing(sqlite3.connect(folder / "command_use.sqlite3")) as database, database:
            _ = database.execute("ATTACH DATABASE ? AS events", (str(events),))
            for table in ("command_events", "prompt_evidence"):
                _ = database.execute(f"CREATE TABLE {table} AS SELECT * FROM events.{table}")
            _ = database.execute("UPDATE command_events SET actor = 'agent' WHERE command = '/manual'")
            _ = database.execute("UPDATE summaries SET payload = ?", ('{"command_events": 1}',))
        events.unlink()
        self.assertEqual(usage.read_histories(self.home, SETTINGS), expected)
        with closing(sqlite3.connect(folder / "command_use.sqlite3")) as database:
            tables = cast(list[tuple[str]], database.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('command_events', 'prompt_evidence')").fetchall())
        self.assertEqual(tables, [])

    def test_skill_usage_updates_only_the_last_use_without_inventing_counts(self) -> None:
        _ = self.transcript("main", [command_entry("/work", 1)])
        _ = (self.home / ".claude.json").write_text(json.dumps({
            "skillUsage": {"work": {"lastUsedAt": at(4).timestamp() * 1000, "usageCount": 99},
                           "metadata-only": {"lastUsedAt": at(2).isoformat()}},
            "private": "fixture private value",
        }))
        numbered = self.home / ".claude2"
        numbered.mkdir()
        _ = (numbered / ".claude.json").write_text(json.dumps({
            "skillUsage": {"work": {"lastUsedAt": at(3).timestamp() * 1000}},
        }))
        histories = usage.read_histories(self.home, SETTINGS)
        self.assertEqual(histories["/work"], usage.CommandHistory(1, 0, at(1), at(4)))
        metadata = histories["/metadata-only"]
        self.assertEqual((metadata.user_count, metadata.agent_count, metadata.last_seen), (0, 0, at(2)))
        self.assertIsInstance(metadata.first_seen, usage.UseTimeUnknown)
        output = usage.render_report(["/work", "/metadata-only"], histories)
        self.assertNotIn("fixture private value", output)
        self.assertIn("| /metadata-only | " + at(2).isoformat() + " | 0 | 0 | unknown |", output)

    def test_repository_names_never_used_list_window_and_oldest_first_order(self) -> None:
        checkout = self.root / "checkout"
        for name in ("early", "later", "never", "unit/work"):
            path = checkout / "commands" / (name + ".md")
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text("fixture command\n")
        names = usage.command_names(checkout)
        self.assertEqual(names, ["/early", "/later", "/never", "/unit:work"])
        _ = self.transcript("history", [command_entry("/later", 4), command_entry("/early", 0)])
        histories = usage.read_histories(self.home, SETTINGS)
        output = usage.render_report(names, histories)
        self.assertEqual(output.splitlines()[0], f"Command use: natedev; data window: {at(0).isoformat()} to {at(4).isoformat()}")
        self.assertIn("Never used: /never, /unit:work", output)
        self.assertLess(output.index("| /early |"), output.index("| /later |"))
        self.assertEqual([line for line in output.splitlines() if line.startswith("| /")], [
            f"| /early | {at(0).isoformat()} | 1 | 0 | {at(0).isoformat()} |",
            f"| /later | {at(4).isoformat()} | 1 | 0 | {at(4).isoformat()} |",
        ])

    def test_entrypoints_print_the_report_without_other_config_fields(self) -> None:
        _ = self.transcript("main", [command_entry("/command_use", 0)])
        output = io.StringIO()
        with patch.object(usage, "read_settings", return_value=SETTINGS), redirect_stdout(output):
            self.assertEqual(usage.main(), 0)
        self.assertIn("Command use: natedev", output.getvalue())
        self.assertIn("| /command_use |", output.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
