"""Check the push audit with isolated notification logs."""
from __future__ import annotations

from collections.abc import Sequence
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.hierarchies import push_audit as audit
from scripts.hierarchies import log_records


JsonRecord = dict[str, object]
AUDIT_SCRIPT = Path(__file__).with_name("push_audit.py")
SINCE = "2026-10-13T17:24:00-04:00"
UNTIL = "2026-10-20T17:24:00-04:00"
AT = "2026-10-15T12:00:00Z"
REGISTERED_TEXT = "shutdown.stop_failed"


def detail_section(output: str) -> str:
    return output.split("\n\nTotals per showrunner", 1)[0]


def total_cells(output: str) -> dict[str, list[str]]:
    return {cells[0]: cells[1:] for line in output.splitlines()
            if line.startswith(("showrunner / ", "machine service / ", "session / ", "all senders  "))
            for cells in [re.split(r" {2,}", line)]}


def session_names_in_literals(source: str, sessions: audit.TopLevelSessions) -> set[str]:
    names = {name for session in sessions.entries for name in (session.key, session.display_name)}
    literals = [node.value for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    return {name for name in names if any(re.search(r"\b" + re.escape(name) + r"\b", text)
                                        for text in literals)}


class PushAuditTest(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home: Path = Path(directory.name)
        self.logs: Path = self.home / ".local/state/notify"
        self.logs.mkdir(parents=True)
        self.pushes: Path = self.logs / "pushover.jsonl"
        self.reviews: Path = self.logs / "review.jsonl"
        self.save(self.pushes, [])
        self.save(self.reviews, [])

    def production(self, slug: str, units: Sequence[tuple[str, str]]) -> None:
        instance = self.home / ".local/state/notifier" / f"showrunner-{slug}"
        instance.mkdir(parents=True)
        doc = self.home / f"{slug}.md"
        rows = "".join(f"| {name} | {plan} |\n" for name, plan in units)
        _ = doc.write_text("# Production\n\n## Units\n\n| Unit | Plan |\n|---|---|\n" + rows, encoding="utf-8")
        _ = (instance / "conf").write_text(f"CHECK=bash /fixture/production_check.sh {doc}\n", encoding="utf-8")

    def save(self, path: Path, records: Sequence[JsonRecord]) -> None:
        _ = path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def push(self, title: str, *, source: str = "send.py in hana", priority: str = "0",
             at: str = AT, text_id: str = "", outcome: str = "sent", body: str = "") -> JsonRecord:
        sender = source.removeprefix("send.py in ")
        return {
            "time": at, "source": source, "priority": priority, "title": title,
            "message": f"No action needed.\n{body or title}\n\nSent by: {sender}\nTemplate: {text_id or 'none (free text)'}",
            "outcome": outcome,
        }

    def failed_review(self, title: str, *, source: str = "send.py in hana", priority: str = "0",
                      at: str = AT, text_id: str = "", body: str = "") -> JsonRecord:
        message: JsonRecord = {
            "title": title, "body": body or title, "action": None,
        }
        record: JsonRecord = {
            "time": at, "source": source, "priority": priority,
            "draft": message, "sent": message.copy(), "verdict": "failed",
            "failed_rules": ["context"], "questions": [], "seconds": 0.1,
            "account": "fixture", "model": "fixture",
        }
        if text_id:
            record["text_id"] = text_id
        return record

    def report(self, *, since: str = SINCE, until: str = UNTIL) -> str:
        environment = {
            **os.environ, "HOME": str(self.home),
            "USER_ZONE": "America/New_York",
            "SHOWRUNNER_STATE_DIR": str(self.home / ".local/state/showrunner"),
            "NOTIFIER_STATE_DIR": str(self.home / ".local/state/notifier"),
        }
        result = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT), "report", "--since", since, "--until", until],
            cwd=AUDIT_SCRIPT.parents[2], env=environment, capture_output=True, text=True,
            check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def test_priority_two_send_names_its_exception(self) -> None:
        self.save(self.pushes, [self.push("Emergency fixture", priority="2",
                                        outcome="sent, receipt fixture, sent despite failed review: context")])
        self.save(self.reviews, [self.failed_review("Emergency fixture", priority="2")])
        output = self.report()
        self.assertIn("ApprovedPriorityTwo", detail_section(output))
        self.assertIn("hana", output)
        self.assertIn("Emergency fixture", output)
        self.assertNotIn("Unapproved", detail_section(output))

    def test_registered_text_names_its_exception_and_id(self) -> None:
        self.save(self.pushes, [self.push("Registered fixture", text_id=REGISTERED_TEXT,
                                        outcome="sent, sent despite failed review: context")])
        self.save(self.reviews, [self.failed_review("Registered fixture", text_id=REGISTERED_TEXT)])
        output = self.report()
        self.assertIn("ApprovedRegisteredText", detail_section(output))
        self.assertIn(REGISTERED_TEXT, output)
        self.assertIn("hana", output)

    def test_registered_text_id_can_come_from_review_or_delivered_footer(self) -> None:
        for review_id, footer_id in ((REGISTERED_TEXT, ""), ("", REGISTERED_TEXT)):
            with self.subTest(review_id=review_id, footer_id=footer_id):
                self.save(self.pushes, [self.push("Text identity fixture", text_id=footer_id)])
                self.save(self.reviews, [self.failed_review("Text identity fixture", text_id=review_id)])
                output = self.report()
                self.assertIn("ApprovedRegisteredText", detail_section(output))
                self.assertIn(REGISTERED_TEXT, output)

    def test_unapproved_send_names_real_sending_function_and_explains_missing_keys(self) -> None:
        push = self.push("Unapproved fixture", outcome="sent, sent despite failed review: context")
        self.save(self.pushes, [push])
        self.save(self.reviews, [self.failed_review("Unapproved fixture")])
        output = self.report()
        self.assertIn("Unapproved", detail_section(output))
        self.assertIn("hana", output)
        self.assertEqual(output.count("Neither notification log records a key."), 1)
        self.assertIn("scripts/message/send.py user -> scripts/notify/pushover.py send()", output)
        self.assertNotRegex(output, r"pushover\.py:\d+")

    def test_failed_review_evidence_does_not_require_outcome_annotation(self) -> None:
        self.save(self.pushes, [self.push("Historical fixture", priority="2")])
        self.save(self.reviews, [self.failed_review("Historical fixture", priority="2")])
        output = self.report()
        self.assertIn("Historical fixture", output)
        self.assertIn("ApprovedPriorityTwo", detail_section(output))

    def test_recorded_text_id_keeps_its_exception_after_removal_from_current_catalog(self) -> None:
        removed = "fixture.removed_text"
        self.save(self.pushes, [self.push("Historical text fixture", text_id=removed)])
        self.save(self.reviews, [self.failed_review("Historical text fixture", text_id=removed)])
        output = self.report()
        self.assertIn("ApprovedRegisteredText", detail_section(output))
        self.assertNotIn("Unapproved", detail_section(output))
        self.assertIn(removed, output)
        self.assertNotIn("Current text catalog", output)
        self.assertNotIn("push_catalog.py", output)

    def test_failed_outcome_annotation_is_evidence_without_review_row(self) -> None:
        self.save(self.pushes, [self.push("Outcome evidence fixture", priority="2",
                                        outcome="sent, sent despite failed review: context")])
        output = detail_section(self.report())
        self.assertIn("Outcome evidence fixture", output)
        self.assertIn("ApprovedPriorityTwo", output)

    def test_showrunner_totals_include_all_delivered_pushes_and_name_each_class(self) -> None:
        self.production("alpha", [("unit-a-unit", "active.md"), ("unit-b-unit", "active.md")])
        self.production("beta", [("unit-c-unit", "active.md")])
        pushes = [
            self.push("Priority fixture", source="send.py in unit-a", priority="2"),
            self.push("Text fixture", source="send.py in unit-b-unit", text_id=REGISTERED_TEXT),
            self.push("Unapproved fixture", source="send.py in unit-c"),
            self.push("Passed fixture", source="send.py in unit-c"),
            self.push("Kept fixture", source="send.py in unit-a", outcome="kept: silence"),
        ]
        self.save(self.pushes, pushes)
        self.save(self.reviews, [self.failed_review("Priority fixture", source="send.py in unit-a", priority="2"),
                                 self.failed_review("Text fixture", source="send.py in unit-b-unit", text_id=REGISTERED_TEXT),
                                 self.failed_review("Unapproved fixture", source="send.py in unit-c"),
                                 {**self.failed_review("Passed fixture", source="send.py in unit-c"),
                                  "verdict": "send", "failed_rules": []}])
        output = self.report()
        totals = total_cells(output)
        self.assertEqual(set(totals), {"showrunner / alpha", "showrunner / beta", "all senders"})
        self.assertEqual(totals["showrunner / alpha"][:2],
                         ["ApprovedPriorityTwo 1; ApprovedRegisteredText 1; Unapproved 0", "2"])
        self.assertEqual(totals["showrunner / beta"][:2],
                         ["ApprovedPriorityTwo 0; ApprovedRegisteredText 0; Unapproved 1", "2"])
        self.assertEqual(totals["all senders"][:2],
                         ["after failed check 3; ApprovedPriorityTwo 1; ApprovedRegisteredText 1; Unapproved 1", "4"])
        for cells in totals.values():
            self.assertEqual(cells[2], "2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)")

    def test_failed_verdicts_count_delivered_text_but_rewrites_and_unsent_reviews_do_not(self) -> None:
        self.save(self.pushes, [self.push("Withheld fixture", priority="2"),
                               self.push("Question fixture", priority="2"),
                               self.push("Rewritten fixture", priority="2", body="Corrected body"),
                               self.push("Unsent fixture", priority="2")])
        self.save(self.reviews, [
            {**self.failed_review("Withheld fixture", priority="2"), "verdict": "withhold"},
            {**self.failed_review("Question fixture", priority="2"), "verdict": "ask"},
            {**self.failed_review("Rewritten fixture", priority="2"), "verdict": "rewrite",
             "sent": {"title": "Rewritten fixture", "body": "Corrected body", "action": None}},
            {**self.failed_review("Unsent fixture", priority="2"), "sent": None},
        ])
        output = detail_section(self.report())
        self.assertIn("Withheld fixture", output)
        self.assertIn("Question fixture", output)
        self.assertNotIn("Rewritten fixture", output)
        self.assertNotIn("Unsent fixture", output)

    def test_registered_rewrite_sent_as_draft_counts_as_a_failed_check(self) -> None:
        self.save(self.pushes, [self.push("Original registered fixture", text_id=REGISTERED_TEXT)])
        self.save(self.reviews, [{**self.failed_review("Original registered fixture", text_id=REGISTERED_TEXT),
                                 "verdict": "rewrite"}])
        output = detail_section(self.report())
        self.assertIn("Original registered fixture", output)
        self.assertIn("ApprovedRegisteredText", output)
        self.assertIn("rewrite not applied", output)

    def test_delayed_registered_rewrite_uses_delivery_outcome_as_evidence(self) -> None:
        self.save(self.pushes, [self.push("Delayed registered fixture", text_id=REGISTERED_TEXT,
                                        outcome="sent, registered text sent as rendered, rewrite not applied")])
        self.save(self.reviews, [{**self.failed_review("Delayed registered fixture", text_id=REGISTERED_TEXT,
                                                      at="2026-10-15T10:00:00Z"), "verdict": "rewrite"}])
        output = detail_section(self.report())
        self.assertIn("Delayed registered fixture", output)
        self.assertIn("ApprovedRegisteredText", output)

    def test_release_from_silence_recovers_text_id_from_the_kept_push_review(self) -> None:
        title = "Historical silence fixture"
        payload = "No action needed.\n" + title
        kept = {**self.push(title, at="2026-10-15T10:00:00Z"), "message": payload,
                "outcome": "kept: silence until you are back, registered text sent as rendered, rewrite not applied"}
        delivered = {**self.push(title), "message": payload,
                     "outcome": "sent, kept during silence, registered text sent as rendered, rewrite not applied"}
        self.save(self.pushes, [kept, delivered])
        self.save(self.reviews, [{**self.failed_review(title, at="2026-10-15T10:00:00.5Z", text_id=REGISTERED_TEXT),
                                 "verdict": "rewrite"}])
        output = self.report()
        self.assertIn(REGISTERED_TEXT, detail_section(output))
        self.assertIn("ApprovedRegisteredText", detail_section(output))
        self.assertEqual(total_cells(output)["all senders"][:2],
                         ["after failed check 1; ApprovedPriorityTwo 0; ApprovedRegisteredText 1; Unapproved 0", "1"])

    def test_registered_rewrite_outcome_keeps_exception_when_the_text_id_was_not_saved(self) -> None:
        self.save(self.pushes, [{**self.push("Missing registered identity"), "message": "Historical body",
                                "outcome": "sent, registered text sent as rendered, rewrite not applied"}])
        output = detail_section(self.report())
        self.assertIn("ApprovedRegisteredText / not recorded", output)
        self.assertNotIn("Unapproved", output)

    def test_failed_rules_under_unreviewed_verdict_mark_the_delivered_draft_unapproved(self) -> None:
        self.save(self.pushes, [self.push("Failed rule fixture")])
        self.save(self.reviews, [{**self.failed_review("Failed rule fixture"), "verdict": "unreviewed: timeout"}])
        output = detail_section(self.report())
        self.assertIn("Failed rule fixture", output)
        self.assertIn("Unapproved", output)
        self.assertIn("review.py review()/validate()", output)
        self.assertIn("scripts/notify/pushover.py send() sends original draft", output)
        self.assertNotRegex(output, r"pushover\.py:\d+")

    def test_bare_sources_matching_exact_production_names_have_an_owner(self) -> None:
        self.production("alpha", [("builds-unit", "active.md"), ("model-study-unit", "active.md")])
        self.save(self.pushes, [self.push(name + " fixture", source=name, priority="2",
                                        outcome="sent, sent despite failed review: context")
                                for name in ("alpha", "builds-unit", "model-study-unit")])
        totals = total_cells(self.report())
        self.assertEqual(set(totals), {"showrunner / alpha", "all senders"})
        self.assertEqual(totals["showrunner / alpha"][:2],
                         ["ApprovedPriorityTwo 3; ApprovedRegisteredText 0; Unapproved 0", "3"])

    def test_machine_services_keep_their_labels_when_units_have_the_same_name(self) -> None:
        names = ("shutdown", "quota_alert", "stall-watch", "silence", "messaging")
        self.production("tools", [(name + "-unit", "active.md") for name in names])
        pushes = [self.push(name + " fixture", source=name, priority="2",
                            outcome="sent, sent despite failed review: context") for name in names]
        pushes.append(self.push("Session fixture", source="send.py in shutdown", priority="2",
                                outcome="sent, sent despite failed review: context"))
        self.save(self.pushes, pushes)
        totals = total_cells(self.report())
        for name in names:
            self.assertEqual(totals["machine service / " + name][1], "1")
        self.assertEqual(totals["showrunner / tools"][1], "1")

    def test_top_level_session_has_an_owner_only_when_current_tables_name_it(self) -> None:
        self.save(self.pushes, [self.push(source + " fixture", source=source, priority="2",
                                        outcome="sent, sent despite failed review: context")
                                for source in ("send.py in hana", "hana")])
        output = self.report()
        label = "session / hana (no current production names it)"
        self.assertEqual(total_cells(output)[label][1], "2")
        self.assertNotIn("machine service / hana", output)
        self.assertNotIn("(showrunner not recorded)", output)
        self.production("hana-production", [("hana", "active.md")])
        totals = total_cells(self.report())
        self.assertIn("showrunner / hana-production", totals)
        self.assertEqual(totals["showrunner / hana-production"][1], "2")
        self.assertNotIn(label, totals)

    def test_configured_session_names_are_absent_from_audit_string_literals(self) -> None:
        problems: list[log_records.SourceProblem] = []
        sessions = audit.read_sessions(audit.SESSION_CONFIG, problems)
        self.assertEqual(problems, [])
        self.assertEqual(session_names_in_literals(AUDIT_SCRIPT.read_text(), sessions), set(),
                         "session names must come from the config")

    def test_session_name_guard_finds_words_inside_strings_and_f_strings(self) -> None:
        sessions = audit.TopLevelSessions((audit.TopLevelSession("hana", "Personal work"),))
        cases: list[tuple[str, set[str]]] = [
            ('source = "send.py in hana"', {"hana"}),
            ('source = f"send.py in hana {suffix}"', {"hana"}),
            ('source = f"{prefix} / Personal work / {suffix}"', {"Personal work"}),
            ('source = "hanadocs and prehana and Personal workers"', set()),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(session_names_in_literals(source, sessions), expected)

    def test_loaded_config_names_bare_sessions_and_prints_the_display_name(self) -> None:
        config = self.home / "sessions.toml"
        _ = config.write_text('[personal_work]\ndisplay_name = "Personal work"\n')
        self.save(self.pushes, [self.push("Configured session fixture", source=source, priority="2",
                                        outcome="sent, sent despite failed review: context")
                                for source in ("personal_work", "send.py in personal_work")])
        with patch.object(audit, "SESSION_CONFIG", config):
            sources = audit.load_sources(self.logs)
        attribution = audit.owner(audit.BareSender("personal_work"), sources.production_ownership, sources.sessions)
        self.assertEqual(attribution, audit.SessionWithoutProduction("personal_work"))
        window = log_records.window_bounds(SINCE, UNTIL, log_records.required_time(UNTIL))
        output = audit.render(audit.audit(sources, window))
        self.assertEqual(total_cells(output)["session / Personal work (no current production names it)"][1], "2")
        self.assertIn(" / Personal work / Configured session fixture", detail_section(output))
        self.assertNotIn("personal_work", output)

    def test_sessions_with_one_display_name_keep_separate_totals(self) -> None:
        config = self.home / "sessions.toml"
        _ = config.write_text('[first_session]\ndisplay_name = "Personal work"\n' +
                              '[second_session]\ndisplay_name = "Personal work"\n')
        self.save(self.pushes, [
            self.push("Failed check fixture", source="first_session", priority="2",
                      outcome="sent, sent despite failed review: context"),
            self.push("Passed check fixture", source="second_session"),
        ])
        with patch.object(audit, "SESSION_CONFIG", config):
            sources = audit.load_sources(self.logs)
        window = log_records.window_bounds(SINCE, UNTIL, log_records.required_time(UNTIL))
        report = audit.audit(sources, window)
        self.assertEqual(report.pushes_by_showrunner,
                         {audit.SessionWithoutProduction("first_session"): 1,
                          audit.SessionWithoutProduction("second_session"): 1})
        output = audit.render(report)
        totals = [re.split(r" {2,}", line) for line in output.splitlines()
                  if line.startswith("session / Personal work (no current production names it)  ")]
        self.assertEqual(len(totals), 2)
        self.assertEqual([cells[2] for cells in totals], ["1", "1"])
        self.assertEqual({cells[1] for cells in totals},
                         {"ApprovedPriorityTwo 1; ApprovedRegisteredText 0; Unapproved 0",
                          "ApprovedPriorityTwo 0; ApprovedRegisteredText 0; Unapproved 0"})
        self.assertEqual(total_cells(output)["all senders"][1], "2")

    def test_missing_or_malformed_session_config_reports_its_path_and_keeps_delivery_counts(self) -> None:
        config = self.home / "sessions.toml"
        self.save(self.pushes, [self.push("Config source fixture", priority="2",
                                        outcome="sent, sent despite failed review: context")])
        for kind, contents in [("missing", ""), ("invalid TOML", "["), ("empty config", ""),
                               ("not a table", "session = 1"), ("missing display name", "[session]"),
                               ("nontext display name", "[session]\ndisplay_name = 1")]:
            with self.subTest(kind=kind):
                config.unlink(missing_ok=True)
                if kind != "missing":
                    _ = config.write_text(contents)
                with patch.object(audit, "SESSION_CONFIG", config):
                    sources = audit.load_sources(self.logs)
                window = log_records.window_bounds(SINCE, UNTIL, log_records.required_time(UNTIL))
                output = audit.render(audit.audit(sources, window))
                self.assertIn(str(config), output)
                self.assertIn("Source problems (counts can be incomplete)", output)
                self.assertIn("unreadable session config", output)
                self.assertEqual(total_cells(output)["all senders"][1], "1")
                self.assertIn("after failed check 1;", total_cells(output)["all senders"][0])

    def test_held_agent_text_review_without_a_delivered_push_counts_nowhere(self) -> None:
        review = {**self.failed_review("Held agent text", text_id="nate.adhoc"),
                  "sent": None, "failed_rules": ["context", "action"], "detail": "not sent, failed review"}
        self.save(self.reviews, [review])
        output = self.report()
        self.assertNotIn("Held agent text", detail_section(output))
        self.assertEqual(total_cells(output)["all senders"][:2],
                         ["after failed check 0; ApprovedPriorityTwo 0; ApprovedRegisteredText 0; Unapproved 0", "0"])
        self.assertEqual(set(total_cells(output)), {"all senders"})

    def test_unmatched_session_is_named_without_claiming_a_missing_record(self) -> None:
        self.save(self.pushes, [self.push("Session outside production", source="send.py in independent-session",
                                        priority="2", outcome="sent, sent despite failed review: context")])
        totals = total_cells(self.report())
        self.assertEqual(set(totals), {"session / independent-session (no current production names it)", "all senders"})
        self.assertEqual(totals["session / independent-session (no current production names it)"][1], "1")

    def test_retired_and_ambiguous_unit_aliases_do_not_assign_an_owner(self) -> None:
        self.production("alpha", [("retired-unit", "retired"), ("shared-unit", "active.md")])
        self.production("beta", [("shared-unit", "active.md")])
        self.save(self.pushes, [self.push(name, source="send.py in " + name, priority="2",
                                        outcome="sent, sent despite failed review: context")
                                for name in ("retired", "shared")])
        totals = total_cells(self.report())
        self.assertIn("session / retired (no current production names it)", totals)
        self.assertIn("session / shared (no current production names it)", totals)
        self.assertNotIn("showrunner / alpha", totals)
        self.assertNotIn("showrunner / beta", totals)

    def test_each_review_marks_only_its_nearest_delivered_push(self) -> None:
        self.save(self.pushes, [self.push("Burst fixture", at=at)
                                for at in ("2026-10-15T12:00:00Z", "2026-10-15T12:00:02Z")])
        self.save(self.reviews, [self.failed_review("Burst fixture", at="2026-10-15T12:00:00.2Z")])
        output = self.report()
        self.assertEqual(detail_section(output).count("Burst fixture"), 1)
        self.assertIn("12:00:00+00:00", detail_section(output))
        self.assertNotIn("12:00:02+00:00", detail_section(output))
        self.assertIn("after failed check 1;", total_cells(output)["all senders"][0])
        self.assertEqual(total_cells(output)["all senders"][1], "2")

    def test_each_push_uses_only_its_nearest_review(self) -> None:
        self.save(self.pushes, [self.push("Nearest review fixture")])
        self.save(self.reviews, [self.failed_review("Nearest review fixture", at="2026-10-15T12:00:01Z"),
                                 {**self.failed_review("Nearest review fixture"), "verdict": "send", "failed_rules": []}])
        self.assertNotIn("Nearest review fixture", detail_section(self.report()))

    def test_agent_written_registered_text_keeps_its_exception_and_names_its_origin(self) -> None:
        self.save(self.pushes, [self.push("Agent text fixture", text_id="nate.adhoc",
                                        outcome="sent, sent despite failed review: context")])
        output = detail_section(self.report())
        self.assertIn("ApprovedRegisteredText", output)
        self.assertIn("nate.adhoc (agent-written text)", output)

    def test_priority_two_takes_precedence_over_registered_text(self) -> None:
        self.save(self.pushes, [self.push("Both exceptions fixture", priority="2", text_id=REGISTERED_TEXT,
                                        outcome="sent, sent despite failed review: context")])
        output = detail_section(self.report())
        self.assertIn("ApprovedPriorityTwo", output)
        self.assertNotIn("ApprovedRegisteredText", output)
        self.assertIn("scripts/notify/pushover.py send()", output)
        self.assertNotRegex(output, r"pushover\.py:\d+")

    def test_missing_id_and_owner_have_domain_states_in_the_report(self) -> None:
        self.save(self.pushes, [{**self.push("Missing fields fixture", source="", priority="2",
                                           outcome="sent, sent despite failed review: context"),
                                "message": "Historical body without a delivery footer"}])
        sources = audit.load_sources(self.logs)
        window = log_records.window_bounds(SINCE, UNTIL, log_records.required_time(UNTIL))
        report = audit.audit(sources, window)
        self.assertIsInstance(report.entries[0].text_identity, audit.TextNotRecorded)
        self.assertIsInstance(report.entries[0].showrunner, audit.ShowrunnerNotRecorded)
        self.assertEqual(report.pushes_by_showrunner[report.entries[0].showrunner], 1)
        self.assertIn("not recorded", audit.render(report))

    def test_free_text_and_absent_identity_remain_distinct_in_details(self) -> None:
        self.save(self.pushes, [self.push("Recorded free text", priority="2",
                                        outcome="sent, sent despite failed review: context"),
                               {**self.push("No identity footer", priority="2",
                                            outcome="sent, sent despite failed review: context"),
                                "message": "Historical body without a delivery footer"}])
        sources = audit.load_sources(self.logs)
        window = log_records.window_bounds(SINCE, UNTIL, log_records.required_time(UNTIL))
        report = audit.audit(sources, window)
        self.assertIsInstance(report.entries[0].text_identity, audit.FreeText)
        self.assertIsInstance(report.entries[1].text_identity, audit.TextNotRecorded)
        output = detail_section(audit.render(report))
        self.assertIn("ApprovedPriorityTwo / free text", output)
        self.assertIn("ApprovedPriorityTwo / not recorded", output)

    def test_empty_reviewed_body_matches_only_an_empty_delivered_body(self) -> None:
        self.save(self.pushes, [self.push("Nonempty body fixture"),
                               {**self.push("Empty body fixture"),
                                "message": "No action needed.\n\n\nSent by: hana\nTemplate: none (free text)"}])
        self.save(self.reviews, [{**self.failed_review(title),
                                 "sent": {"title": title, "action": None, "body": ""}}
                                for title in ("Nonempty body fixture", "Empty body fixture")])
        output = detail_section(self.report())
        self.assertNotIn("Nonempty body fixture", output)
        self.assertIn("Empty body fixture", output)

    def test_non_sent_outcomes_do_not_count_as_pushes(self) -> None:
        outcomes = ("kept: silence until tomorrow, sent despite failed review: context",
                    "refused: HTTP 503, sent despite failed review: context",
                    "unreachable: timeout, sent despite failed review: context", "cancel: sent")
        self.save(self.pushes, [self.push(f"Not delivered {index}", outcome=outcome)
                               for index, outcome in enumerate(outcomes)])
        self.save(self.reviews, [self.failed_review(f"Not delivered {index}")
                                for index in range(len(outcomes))])
        output = self.report()
        self.assertNotIn("Not delivered", output)

    def test_replay_dry_and_question_reviews_do_not_mark_pushes_failed(self) -> None:
        ignored: list[JsonRecord] = [{"replay": True}, {"dry": True}, {"sample": True},
                                    {"kind": "question"}, {"kind": "reply"}]
        pushes = [self.push(f"Ignored review {index}") for index in range(len(ignored))]
        reviews = [{**self.failed_review(f"Ignored review {index}"), **fields}
                   for index, fields in enumerate(ignored)]
        self.save(self.pushes, pushes)
        self.save(self.reviews, reviews)
        self.assertNotIn("Ignored review", self.report())

    def test_review_match_requires_sender_title_and_nearby_time(self) -> None:
        self.save(self.pushes, [self.push("Wrong source"), self.push("Wrong title"), self.push("Too late")])
        self.save(self.reviews, [
            self.failed_review("Wrong source", source="other"),
            self.failed_review("A different title"),
            self.failed_review("Too late", at="2026-10-16T12:00:00Z"),
        ])
        output = self.report()
        for title in ("Wrong source", "Wrong title", "Too late"):
            with self.subTest(title=title):
                self.assertNotIn(title, output)

    def test_review_match_checks_body_and_five_second_limit(self) -> None:
        self.save(self.pushes, [self.push(title, priority="2")
                               for title in ("Five seconds", "Six seconds", "Wrong body")])
        self.save(self.reviews, [
            self.failed_review("Five seconds", priority="2", at="2026-10-15T12:00:05Z"),
            self.failed_review("Six seconds", priority="2", at="2026-10-15T12:00:06Z"),
            {**self.failed_review("Wrong body", priority="2"),
             "sent": {"title": "Wrong body", "body": "Different reviewed text", "action": None}},
        ])
        output = detail_section(self.report())
        self.assertIn("Five seconds", output)
        self.assertNotIn("Six seconds", output)
        self.assertNotIn("Wrong body", output)

    def test_long_bodies_with_equal_first_eighty_characters_do_not_match(self) -> None:
        shared = "Shared notification details. " * 8
        self.save(self.pushes, [self.push("Different long body", priority="2", body=shared + "Delivered ending.")])
        self.save(self.reviews, [self.failed_review("Different long body", priority="2",
                                                  body=shared + "Reviewed ending.")])
        self.assertNotIn("Different long body", detail_section(self.report()))

    def test_truncated_body_matches_only_when_all_retained_text_matches(self) -> None:
        body = "".join(f"Line {index}: notification detail for this fixture.\n" for index in range(80))
        footer = "\n\nSent by: hana\nTemplate: none (free text)"
        retained = ("No action needed.\n" + body)[:1024 - len(footer)].rstrip()
        pushes = [
            {**self.push("Truncated match", priority="2"), "message": retained + footer},
            {**self.push("Truncated mismatch", priority="2"), "message": retained[:-1] + "?" + footer},
        ]
        self.save(self.pushes, pushes)
        self.save(self.reviews, [self.failed_review(title, priority="2", body=body)
                                for title in ("Truncated match", "Truncated mismatch")])
        output = detail_section(self.report())
        self.assertIn("Truncated match", output)
        self.assertIn("ApprovedPriorityTwo", output)
        self.assertNotIn("Truncated mismatch", output)

    def test_window_includes_start_and_excludes_end(self) -> None:
        records = (("Before window", "2026-10-13T21:23:59Z"),
                   ("At window start", "2026-10-13T21:24:00Z"),
                   ("At window end", "2026-10-20T21:24:00Z"))
        self.save(self.pushes, [self.push(title, at=at, priority="2") for title, at in records])
        self.save(self.reviews, [self.failed_review(title, at=at, priority="2") for title, at in records])
        output = self.report()
        self.assertIn("At window start", output)
        self.assertNotIn("Before window", output)
        self.assertNotIn("At window end", output)
        self.assertIn("2026-10-13", output)
        self.assertIn("2026-10-20", output)

    def test_date_only_until_includes_the_whole_local_day(self) -> None:
        records = (("Last local minute", "2026-10-21T03:59:00Z"),
                   ("Next local day", "2026-10-21T04:00:00Z"))
        self.save(self.pushes, [self.push(title, at=at, priority="2") for title, at in records])
        self.save(self.reviews, [self.failed_review(title, at=at, priority="2") for title, at in records])
        output = self.report(until="2026-10-20")
        self.assertIn("Last local minute", output)
        self.assertNotIn("Next local day", output)

    def test_missing_log_reports_source_problem(self) -> None:
        for missing in (self.pushes, self.reviews):
            with self.subTest(missing=missing.name):
                missing.unlink()
                output = self.report()
                self.assertIn(missing.name, output)
                self.assertRegex(output.lower(), r"source (?:problems?|unavailable)")
                self.assertTrue(total_cells(output)["all senders"][0].startswith("incomplete; "))
                self.assertFalse(missing.exists())
                self.save(missing, [])

    def test_corrupt_source_reports_problem_and_keeps_valid_rows(self) -> None:
        self.save(self.pushes, [self.push("Valid fixture", priority="2")])
        self.save(self.reviews, [self.failed_review("Valid fixture", priority="2")])
        with self.pushes.open("a", encoding="utf-8") as output:
            _ = output.write("{invalid json\n")
        output = self.report()
        self.assertIn("pushover.jsonl", output)
        self.assertRegex(output.lower(), r"source (?:problems?|unavailable)")
        self.assertIn("Valid fixture", output)

    def test_report_leaves_source_logs_unchanged(self) -> None:
        self.save(self.pushes, [self.push("Read only fixture", priority="2")])
        self.save(self.reviews, [self.failed_review("Read only fixture", priority="2")])
        before = {path: path.read_bytes() for path in (self.pushes, self.reviews)}
        _ = self.report()
        self.assertEqual({path: path.read_bytes() for path in before}, before)


if __name__ == "__main__":
    _ = unittest.main()
