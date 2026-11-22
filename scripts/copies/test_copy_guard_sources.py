"""Check copy policy in scans of the repository's own scripts."""
from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import ClassVar
import unittest

from scripts.copies import copy_guard
from scripts.lib import conf


class CopyGuardTests(unittest.TestCase):
    transitional: ClassVar[frozenset[str]] = frozenset({"mac_test/offload.py", "production/mac_run.sh", "direct/verify.sh"})

    @classmethod
    def repository_exemptions(cls) -> frozenset[str]:
        config = Path(__file__).resolve().parents[2] / "config/copies.conf"
        return frozenset(key.removeprefix("allow.") for key in conf.shipped_keys(config) if key.startswith("allow."))

    @classmethod
    @cache
    def repository_findings(cls) -> tuple[copy_guard.TreeCreation, ...]:
        """Share the strict scan of the unchanged repository between both policy tests."""
        scripts = Path(__file__).resolve().parents[1]
        allow_list = dict.fromkeys(cls.repository_exemptions() - cls.transitional, "Original checkout")
        return tuple(copy_guard.scan_scripts(scripts, allow_list=allow_list))

    def test_repository_scripts_use_the_single_checkout_service(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        exemptions = self.repository_exemptions()
        findings = [finding for finding in self.repository_findings()
                    if finding.path.relative_to(scripts).as_posix() not in exemptions]
        self.assertEqual(findings, [], "\n".join(str(finding) for finding in findings))

    def test_repository_build_copy_makers_have_no_transitional_exemptions(self) -> None:
        transitional = self.transitional
        findings = list(self.repository_findings())
        self.assertEqual(findings, [], "\n".join(str(finding) for finding in findings))
        self.assertFalse(self.repository_exemptions() & transitional)


if __name__ == "__main__":
    _ = unittest.main()
