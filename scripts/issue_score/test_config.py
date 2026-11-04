"""Use private configuration for the issue-score tests."""
from __future__ import annotations

import io
import inspect
import os
from collections.abc import Callable, Generator
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib import conf
from scripts.lib.conf import UseConfiguredValue
from scripts.issue_score import apply_labels, blind, client, decide, evaluate, questions, records, score_issues, vault


CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class ConfiguredTestCase(unittest.TestCase):
    """Read the worktree configuration instead of the installed configuration."""

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(CONFIG_DIR)})))

    def configuration(self, **changes: str) -> Path:
        """Create private settings and select them for the rest of this test."""
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "issue_score.conf"
        self.change_configuration(path, **changes)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(folder)})))
        return path

    def change_configuration(self, path: Path, **changes: str) -> None:
        """Change the private settings through the shared configuration writer."""
        values = {**(conf.read(path).values() if path.exists() else {}), **changes}
        _ = write_config_fixture(path, "\n".join(f"{key}={value}" for key, value in values.items()))
        lines = path.read_text(encoding="utf-8").splitlines()
        for key, value in values.items():
            index, _ = conf.effective_line(path, key)
            if index > 0 and lines[index - 1].startswith("# one of:"):
                lines[index - 1] = f"# one of: {value}"
        _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@contextmanager
def patch_thresholds(**policies: questions.ThresholdOutcome) -> Generator[None]:
    """Select a historical policy without changing the configured policy kinds."""
    values = {**questions.thresholds(), **policies}
    with mock.patch.object(questions, "thresholds", return_value=values):
        yield


class ConfigurationTests(ConfiguredTestCase):
    def test_each_shipped_key_reads_without_recovery_and_has_a_valid_limit(self) -> None:
        scoring_config = conf.read('issue_score')
        path = CONFIG_DIR / "issue_score.conf"
        lines = path.read_text(encoding="utf-8").splitlines()
        settings = conf.read(path).values()
        self.assertTrue(settings)
        for key, value in settings.items():
            with self.subTest(key=key):
                index, _ = conf.effective_line(path, key)
                self.assertGreater(index, 0)
                limit = lines[index - 1]
                self.assertTrue(limit.startswith(("# whole:", "# range:", "# one of:")), limit)
                warnings = io.StringIO()
                with redirect_stderr(warnings):
                    conf.check(path, key, value)
                    if limit.startswith("# whole:"):
                        self.assertEqual(scoring_config.whole(key), int(value))
                    elif limit.startswith("# range:"):
                        self.assertEqual(scoring_config.number(key), float(value))
                    else:
                        self.assertEqual(scoring_config.text(key), value)
                self.assertEqual(warnings.getvalue(), "")

    def test_shipped_values_match_the_values_before_the_config_move(self) -> None:
        self.assertEqual(conf.read(CONFIG_DIR / "issue_score.conf").values(), {
            "workers": "8", "model": "jev-1.14.0", "budget_usd": "20.0",
            "input_usd_per_million": "0.042",
            "threshold.backlog_goal": "0.40", "threshold.backlog_alignment": "0.40",
            "threshold.backlog_impact": "0.40", "threshold.category": "0.90", "threshold.area": "0.3",
            "error_target.backlog_goal": "0.1", "error_target.category": "0.1", "error_target.area": "0.1",
            "error_target.backlog_impact": "0.1", "error_target.backlog_alignment": "0.1",
            "evidence_present": "0.7", "evidence_absent": "0.3", "split_probability": "0.25",
            "body_character_limit": "24000", "endpoint": "https://api.typesafe.ai/v1/systemone",
            "max_attempts": "5", "request_timeout": "120", "retry_backoff_base": "2.0",
            "spend_ledger": "~/.local/state/issue-score/spend.jsonl", "state_dir": "~/.local/state/issue-score",
            "vault_root": "~/rust/hanadocs", "style_rules_root": "~/rust/nate_style",
            "blind_sample_count": "20", "title_only_character_limit": "200", "disagreement_count": "20", "reliability_bucket_count": "5",
        })

    def test_domain_defaults_name_the_choice_to_read_current_configuration(self) -> None:
        functions: tuple[Callable[..., object], ...] = (client.system_one, decide.is_sure, score_issues.load_cache,
                     score_issues.score, score_issues.propose, score_issues.experiment, score_issues.reassess,
                     vault.load_area_definitions, vault.load_project_names, vault.load_open_issues,
                     vault.load_scoring_issues, vault.scoring_issue, vault.load_rated_issues,
                     evaluate.compare, blind.blind, apply_labels.apply_labels)
        for function in functions:
            parameters = inspect.signature(function).parameters.values()
            defaults = [cast(object, parameter.default) for parameter in parameters
                        if cast(object, parameter.default) is not inspect.Parameter.empty]
            with self.subTest(api=function.__name__):
                self.assertNotIn(None, defaults)
                self.assertIn(UseConfiguredValue.CURRENT, defaults)

    def test_readers_observe_file_edits_without_a_module_reload(self) -> None:
        readers: dict[str, tuple[Callable[[], object], str, object]] = {
            "reliability_bucket_count": (evaluate.reliability_bucket_count, "3", 3),
            "workers": (score_issues.workers, "2", 2),
            "model": (questions.model, "fixture-model", "fixture-model"),
            "budget_usd": (questions.budget_usd, "3.5", 3.5),
            "input_usd_per_million": (questions.input_usd_per_million, "0.21", .21),
            "evidence_present": (questions.evidence_present, "0.8", .8),
            "evidence_absent": (questions.evidence_absent, "0.2", .2),
            "split_probability": (questions.split_probability, "0.4", .4),
            "body_character_limit": (questions.body_character_limit, "80", 80),
            "endpoint": (client.endpoint, "https://fixture.invalid/systemone", "https://fixture.invalid/systemone"),
            "max_attempts": (client.max_attempts, "3", 3),
            "request_timeout": (client.request_timeout, "7.5", 7.5),
            "retry_backoff_base": (client.retry_backoff_base, "3.0", 3.),
            "spend_ledger": (client.spend_ledger, "~/private-spend.jsonl", Path("~/private-spend.jsonl").expanduser()),
            "state_dir": (records.state_dir, "~/private-state", Path("~/private-state").expanduser()),
            "vault_root": (vault.vault_root, "~/private-vault", Path("~/private-vault").expanduser()),
            "style_rules_root": (lambda: vault.style_rules_dirs()[0], "~/private-rules", Path("~/private-rules").expanduser()),
            "blind_sample_count": (blind.sample_count, "7", 7),
            "title_only_character_limit": (evaluate.title_only_character_limit, "40", 40),
            "disagreement_count": (evaluate.disagreement_count, "3", 3),
        }
        for field in ("backlog_goal", "backlog_alignment", "backlog_impact", "category", "area"):
            readers[f"threshold.{field}"] = (
                lambda field=field: questions.thresholds()[field], "0.51",
                questions.MinimumGap(.51) if field == "area" else questions.Supported(.51),
            )
            readers[f"error_target.{field}"] = (lambda field=field: questions.error_targets()[field], "0.2", .2)
        self.assertEqual(set(readers), set(conf.read(CONFIG_DIR / "issue_score.conf").values()))
        for key, (read, replacement, expected) in readers.items():
            with self.subTest(key=key):
                path = self.configuration()
                before = read()
                self.change_configuration(path, **{key: replacement})
                self.assertEqual(read(), expected)
                self.assertNotEqual(read(), before)
