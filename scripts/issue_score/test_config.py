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


SHIPPED_CONFIG = Path(__file__).resolve().parents[2] / "config/issue_score.conf"
SCORING_FIELDS = ("backlog_goal", "backlog_alignment", "backlog_impact", "category", "area")


def scoring_fixture(folder: Path) -> str:
    return f"""\
# whole: 1..
workers=8
# text: model
model=jev-1.14.0
# range: 0..
input_usd_per_million=0.042
# range: 0..
budget_usd=20
# range: 0..1
threshold.backlog_goal=0.40
# range: 0..1
threshold.backlog_alignment=0.40
# range: 0..1
threshold.backlog_impact=0.40
# range: 0..1
threshold.category=0.90
# range: 0..1
threshold.area=0.3
# range: 0..1
evidence_present=0.7
# range: 0..1
evidence_absent=0.3
# range: 0..1
split_probability=0.25
# whole: 1..
body_character_limit=24000
# text: endpoint
endpoint=https://api.typesafe.ai/v1/systemone
# whole: 1..
max_attempts=5
# range: 5e-324..
request_timeout=120
# range: 5e-324..
retry_backoff_base=2
# text: private path
spend_ledger={folder / "spend.jsonl"}
# text: private path
state_dir={folder / "state"}
# text: private path
vault_root={folder / "vault"}
# text: private path
style_rules_root={folder / "style"}
# whole: 0..
title_only_character_limit=200
# whole: 1..
disagreement_count=20
# whole: 1..
reliability_bucket_count=5
# whole: 1..
blind_sample_count=7
""" + "".join(f"# range: 0..1\nerror_target.{field}=0.1\n" for field in SCORING_FIELDS)


class ConfiguredTestCase(unittest.TestCase):
    """Read private settings with explicit values for the scoring tests."""

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.configuration()

    def configuration(self, **changes: str) -> Path:
        """Create private settings and select them for the rest of this test."""
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "issue_score.conf"
        _ = write_config_fixture(path, defaults=scoring_fixture(folder))
        if changes:
            self.change_configuration(path, **changes)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(folder)})))
        return path

    def change_configuration(self, path: Path, **changes: str) -> None:
        """Change the private settings through the shared configuration writer."""
        _ = write_config_fixture(path, "\n".join(f"{key}={value}" for key, value in changes.items()), defaults="")


@contextmanager
def patch_thresholds(**policies: questions.ThresholdOutcome) -> Generator[None]:
    """Select a historical policy without changing the configured policy kinds."""
    values = {**questions.thresholds(), **policies}
    with mock.patch.object(questions, "thresholds", return_value=values):
        yield


class ConfigurationTests(ConfiguredTestCase):
    def test_each_fixed_key_reads_without_recovery_and_has_a_valid_limit(self) -> None:
        path = conf.path("issue_score")
        scoring_config = conf.read(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        settings = conf.read(path).values()
        self.assertEqual(set(settings), conf.shipped_keys(SHIPPED_CONFIG))
        for key, value in settings.items():
            with self.subTest(key=key):
                index, _ = conf.effective_line(path, key)
                self.assertGreater(index, 0)
                limit = lines[index - 1]
                self.assertTrue(limit.startswith(("# whole:", "# range:", "# text:")), limit)
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

    def test_readers_use_values_from_the_private_file(self) -> None:
        _ = self.configuration(workers="3", model="private-model", budget_usd="12.5")
        self.assertEqual(score_issues.workers(), 3)
        self.assertEqual(questions.model(), "private-model")
        self.assertEqual(questions.budget_usd(), 12.5)

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
        for field in SCORING_FIELDS:
            readers[f"threshold.{field}"] = (
                lambda field=field: questions.thresholds()[field], "0.51",
                questions.MinimumGap(.51) if field == "area" else questions.Supported(.51),
            )
            readers[f"error_target.{field}"] = (lambda field=field: questions.error_targets()[field], "0.2", .2)
        self.assertEqual(set(readers), conf.shipped_keys(SHIPPED_CONFIG))
        for key, (read, replacement, expected) in readers.items():
            with self.subTest(key=key):
                initial = "private-initial" if isinstance(expected, (str, Path)) else "1"
                path = self.configuration(**{key: initial})
                before = read()
                self.change_configuration(path, **{key: replacement})
                self.assertEqual(read(), expected)
                self.assertNotEqual(read(), before)
