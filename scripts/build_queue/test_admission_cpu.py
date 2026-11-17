"""Placement passes share host configuration reads."""
from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest import mock

from scripts.build_queue import admission_protocol as wire, placement
from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate
from scripts.lib import conf


class PlacementWorkTests(unittest.TestCase):
    def test_mac_evidence_uses_the_repository_timeout_when_the_live_file_is_missing(self) -> None:
        with PlacementFixture() as fixture:
            fixture.configure(waiting_pass_seconds=1)
            _ = fixture.run_pass()
            live = fixture.root / "live"
            live.mkdir()
            _ = (live / "build_targets.conf").write_text(
                "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
            repository_settings = fixture.root / "repository-mac-test.conf"
            _ = repository_settings.write_text("# range: 1..60\nprobe_timeout_s=15\n")
            request = placement_request(fixture)

            with (mock.patch.dict(os.environ), mock.patch.object(conf, "directory", return_value=live),
                  mock.patch.object(conf, "_repository_path", return_value=repository_settings)):
                del os.environ["CONF_DIR"]
                evidence = publish_candidate(fixture, request)
                fixture.clock.advance(16)
                self.assertEqual(fixture.server.host_evidence(evidence.key), evidence)
                fixture.clock.advance(0.5)
                self.assertIsInstance(fixture.server.host_evidence(evidence.key), placement.StaleHostEvidence)

    def test_waiting_placements_read_host_configuration_once_per_pass(self) -> None:
        with PlacementFixture() as fixture:
            for _ in range(3):
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion("busy"))
                self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)

            configuration = conf.path("build_admission")
            read_bytes = Path.read_bytes
            reads: list[Path] = []
            settings = conf.path("mac_test")
            parse = conf.read
            parses: list[Path] = []

            def read_settings(file: str | Path) -> conf.ConfigRead:
                if conf.path(file) == settings:
                    parses.append(settings)
                return parse(file)

            def read(path: Path) -> bytes:
                if path == configuration:
                    reads.append(path)
                return read_bytes(path)

            with mock.patch.object(Path, "read_bytes", read), mock.patch.object(conf, "read", read_settings):
                _ = fixture.run_pass()
                self.assertEqual(reads, [configuration])
                self.assertEqual(parses, [settings])
                _ = fixture.run_pass()
                self.assertEqual(reads, [configuration, configuration])
                self.assertEqual(parses, [settings, settings])
