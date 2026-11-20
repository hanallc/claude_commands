"""Keep linked Hana executions durable and separate from local machine totals."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import gzip
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import cast
import unittest
from unittest import mock

from scripts.build_queue import execution_identity
from scripts.aws_target import hana_result
from scripts.aws_target.test_hana_result import attempt_key, command_result
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_client, admission_protocol as wire, placement
from scripts.buildlog import index, memory, record, report, store
from scripts.buildlog.test_index import use_test_log
from scripts.mac_test.claim_state import RepositoryScope

use_test_log()


class HanaRecordTests(unittest.TestCase):
    def test_publication_replay_and_recorder_restart_append_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {'BUILDLOG_DIR': temporary}):
            value = command_result(101)
            link = hana_result.LogicalStepLink('call', value.key.episode)
            context = record.HanaStepContext('fixture-target', 'check', 'fixture-repository', '/fixture/tree', '/fixture/target')
            accepted = hana_result.AcceptedHanaResult(value)
            with mock.patch.object(record, '_delete_step_sources', side_effect=OSError('interrupted after append')):
                with self.assertRaises(OSError):
                    _ = record.publish_hana_result(accepted, link, context)
            self.assertFalse(record.publish_hana_result(accepted, link, context).added)
            self.assertFalse(record.publish_hana_result(accepted, link, context).added)
            rows = store.host_file(value.host, value.started_at).read_text().splitlines()
            self.assertEqual(len(rows), 1)
            row = cast(dict[str, object], json.loads(rows[0]))
            self.assertEqual(row['source_key'], value.source_key)
            self.assertEqual(row['source_commit'], value.commit)
            self.assertEqual(row['toolchain_identity'], value.toolchain)
            self.assertNotIn('tree_key', row)
            self.assertNotIn('rustc', row)
            self.assertEqual(row['peak_mem_bytes'], 12345)
            self.assertEqual(row['status'], 101)
            self.assertEqual(gzip.decompress((Path(temporary) / str(row['log'])).read_bytes()), value.stdout + value.stderr)
            self.assertEqual(len(list(record.pending_steps_root().glob('*.done'))), 1)

    def test_settled_loss_and_fallback_keep_hosts_and_count_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {'BUILDLOG_DIR': temporary}), \
                closing(sqlite3.connect(':memory:')) as connection:
            index.create_schema(connection)
            key = attempt_key()
            link = hana_result.LogicalStepLink('call', key.episode)
            context = record.HanaStepContext('fixture-target', 'check', 'fixture-repository', '/fixture/tree', '/fixture/target')
            self.assertTrue(record.publish_hana_loss(key, link, context, 'fixture-hana', 100, 120, 'settled loss').added)
            loss = hana_result.object_fields(cast(object, json.loads(store.host_file('fixture-hana', 100).read_text())))
            loss.update({'crates_compiled': 99, 'finished_s': 18, 'peak_mem_bytes': 99 * 2**30,
                         'mem_wait_s': 100, 'slice': 'fallback'})
            index.add_step(connection, loss, 'remote.jsonl')
            local: dict[str, object] = {**loss, 'id': 'local', 'host': 'natedev', 'target': 'natedev',
                'caller': 'verify', 'duration_s': 8, 'status': 0, 'crates_compiled': 2, 'finished_s': 6,
                'argv': ['cargo', 'check'], 'peak_mem_bytes': 2**30, 'mem_wait_s': 0, 'slice': 'builds',
                **hana_result.linked_step_fields(attempt_key('replacement'), link, 'fallback', True)}
            index.add_step(connection, local, 'local.jsonl')
            finals = cast(list[tuple[str]], connection.execute('SELECT host FROM logical_step_results').fetchall())
            self.assertEqual(finals, [('natedev',)])
            identity = record.LocalStepIdentity(('natedev',), ('natedev',))
            day = report.call_time(str(loss['started_at'])).astimezone().date().isoformat()
            with mock.patch.object(record, 'local_step_identity', return_value=identity):
                rows = report.known_crate_steps(connection, day)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].crates_compiled, 2)
                lines = '\n'.join(report.linked_steps_section(connection, day))
                self.assertIn('1 logical results.', lines)
                self.assertIn('fixture-hana', lines)
                self.assertIn('natedev', lines)
                summary = '\n'.join(report.summary_sections(connection, day, ['check']))
                self.assertNotIn('99.0', summary)
            predicates = memory.InstrumentPredicates("host='natedev'", "host='natedev'", "host='natedev'")
            instruments = '\n'.join(memory.instrument_lines(connection, predicates, ()))
            self.assertIn('unsliced steps: none', instruments)
            self.assertEqual(connection.execute('SELECT sum(duration_s) FROM steps').fetchone(), (28.0,))

    def test_old_records_leave_link_columns_null(self) -> None:
        with closing(sqlite3.connect(':memory:')) as connection:
            index.create_schema(connection)
            old: dict[str, object] = {'id': 'old', 'host': 'natedev', 'started_at': store.utc_iso(100),
                'duration_s': 4.5, 'peak_mem_bytes': 123, 'status': 0, 'argv': ['cargo', 'check']}
            index.add_step(connection, old, 'old.jsonl')
            names = ','.join(name for name, _, _ in index.PLACEMENT_COLUMNS)
            self.assertEqual(connection.execute(f'SELECT {names} FROM steps').fetchone(),
                             (None,) * len(index.PLACEMENT_COLUMNS))
            self.assertEqual(connection.execute('SELECT duration_s,peak_mem_bytes FROM steps').fetchone(), (4.5, 123))
            self.assertEqual(connection.execute('SELECT count(*) FROM logical_step_results').fetchone(), (1,))

    def test_fallback_link_fields_do_not_replace_the_execution_host(self) -> None:
        key = attempt_key('replacement')
        with mock.patch.dict(os.environ, {'BUILDLOG_HANA_ATTEMPT': json.dumps(hana_result.encode_attempt_key(key)),
                'BUILDLOG_CALL_ID': 'call', 'BUILDLOG_ATTEMPT_ROLE': 'fallback', 'BUILDLOG_IS_FINAL_RESULT': '1',
                'BUILDLOG_SOURCE_KEY': 'new-live-source'}):
            fields = record.placement_step_fields()
        self.assertEqual(fields['attempt_role'], 'fallback')
        self.assertEqual(fields['source_key'], 'new-live-source')
        self.assertTrue(fields['is_final_result'])
        self.assertNotIn('host', fields)

    def test_local_grant_reads_the_remote_link_from_its_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'assignment.json'
            local = placement.PlacementAssignment('episode', 'local-attempt',
                placement.PlacementDestination('local', 'natedev', 'natedev', RepositoryScope('fixture', 'fixture-repo', 1), 1),
                execution_identity.SourceKeyKnown('edited-live-source'), placement.SubmittingOwner('natedev', 123, 'start'),
                NoOwningSession(), 100.0)
            grant = wire.LocalPlacementGranted(local, wire.Admitted('reservation', 1, 'fixture', 'max', 'wave', {}))
            admission_client.write_assignment(path, grant)
            record.save_hana_fallback_link(path, attempt_key(), hana_result.LogicalStepLink('call', 'episode'))
            with mock.patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': str(path), 'BUILDLOG_CALL_ID': 'call'}):
                _ = os.environ.pop('BUILDLOG_HANA_ATTEMPT', None)
                fields = record.placement_step_fields()
                self.assertEqual(fields['placement_attempt'], 'local-attempt')
                self.assertEqual(fields['source_key'], 'edited-live-source')
                self.assertEqual(fields['step_link'], '["call","episode"]')
                self.assertNotIn('execution_generation', fields)
                self.assertNotIn('host', fields)
                admission_client.write_assignment(path, replace(grant, assignment=replace(local, episode='later-step')))
                self.assertEqual(record.placement_step_fields(), {})


if __name__ == '__main__':
    _ = unittest.main()
