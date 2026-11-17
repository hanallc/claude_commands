"""Check exact result identities before any publication or cache decision."""
from __future__ import annotations

from dataclasses import replace
import json
from typing import cast
import unittest
from unittest import mock

from scripts.aws_target import hana_result as result
from scripts.aws_target.hana_generation import HanaHostGeneration


def attempt_key(attempt: str = 'attempt') -> result.HanaAttemptKey:
    return result.HanaAttemptKey('episode', attempt, 'claim-' + attempt,
                                 HanaHostGeneration('deployment', 'start', 'boot'))


def command_result(status: int = 0) -> result.HanaCommandResult:
    return result.HanaCommandResult(attempt_key(), 'verify-source', 'a' * 40, 'b' * 64, 'fixture-hana',
        ('cargo', 'check', '--all-features'), 1, status, b'output\x00\xff', b'diagnostic\xfe',
        100.0, 130.0, 30.0, result.MeasuredPeak(12345))


def expectation(value: result.HanaCommandResult) -> result.HanaResultExpectation:
    return result.HanaResultExpectation(value.key, value.source_key, value.commit, value.toolchain,
                                        value.host, value.argv, value.slot)


class HanaResultTests(unittest.TestCase):
    def test_round_trip_preserves_both_outputs_and_unknown_memory(self) -> None:
        for memory in (result.MeasuredPeak(12345), result.PeakUnavailable('no cgroup evidence')):
            with self.subTest(memory=memory):
                value = replace(command_result(101), memory=memory)
                self.assertEqual(result.decode_result(result.encode_result(value)), value)
                self.assertEqual(result.decode_attempt_key(result.encode_attempt_key(value.key)), value.key)

    def test_valid_compiler_failure_is_terminal(self) -> None:
        value = command_result(101)
        decision = result.validate_result(value, expectation(value), value.source_key)
        self.assertEqual(decision, result.AcceptedHanaResult(value))

    def test_edits_during_execution_or_transfer_reject_result(self) -> None:
        value = command_result()
        for key in ('edited-during-execution', 'edited-during-transfer', ''):
            with self.subTest(key=key):
                self.assertIsInstance(result.validate_result(value, expectation(value), key), result.RejectedHanaResult)

    def test_all_assignment_and_source_fields_are_checked(self) -> None:
        value = command_result()
        replacements: tuple[result.HanaCommandResult, ...] = (
            replace(value, key=replace(value.key, episode='other')),
            replace(value, key=replace(value.key, attempt='replacement')),
            replace(value, key=replace(value.key, claim_id='other')),
            *(replace(value, key=replace(value.key, generation=other)) for other in (
                HanaHostGeneration('other', 'start', 'boot'), HanaHostGeneration('deployment', 'other', 'boot'),
                HanaHostGeneration('deployment', 'start', 'other'))),
            replace(value, slot=2), replace(value, source_key='other'), replace(value, commit='c' * 40),
            replace(value, toolchain='d' * 64), replace(value, argv=('cargo', 'check', '--no-default-features')),
            replace(value, host='other-host'))
        for changed in replacements:
            with self.subTest(changed=changed):
                self.assertIsInstance(result.validate_result(changed, expectation(value), value.source_key),
                                      result.RejectedHanaResult)

    def test_fenced_late_result_is_rejected(self) -> None:
        value = command_result()
        self.assertIsInstance(result.validate_result(value, expectation(value), value.source_key, fenced=True),
                              result.RejectedHanaResult)

    def test_bad_wire_fields_never_decode_as_a_result(self) -> None:
        value = command_result()
        for name, bad in (('version', True), ('status', True), ('slot', -1), ('argv', ['cargo', 1]),
                          ('stdout', '@@@'), ('started_at', float('nan')), ('finished_at', 1),
                          ('execution_seconds', -1), ('memory', {'bytes': True})):
            with self.subTest(name=name, bad=bad):
                row = cast(dict[str, object], json.loads(result.encode_result(value)))
                row[name] = bad
                with self.assertRaises(ValueError):
                    _ = result.decode_result(json.dumps(row).encode())

    def test_result_transport_has_a_byte_limit(self) -> None:
        with mock.patch.object(result, 'RESULT_MAX_BYTES', 64):
            with self.assertRaises(ValueError):
                _ = result.encode_result(command_result())
            with self.assertRaises(ValueError):
                _ = result.decode_result(b' ' * 65)

    def test_link_identity_separates_calls_and_episodes(self) -> None:
        key = attempt_key()
        fields = result.linked_step_fields(key, result.LogicalStepLink('call', key.episode), 'remote', False)
        other = result.linked_step_fields(key, result.LogicalStepLink('other-call', key.episode), 'fallback', True)
        self.assertNotEqual(fields['step_link'], other['step_link'])
        self.assertFalse(fields['is_final_result'])
        self.assertEqual(other['attempt_role'], 'fallback')
        self.assertNotEqual(result.attempt_record_id(key), result.attempt_record_id(attempt_key('replacement')))
        with self.assertRaises(ValueError):
            _ = result.linked_step_fields(key, result.LogicalStepLink('call', 'other-episode'), 'remote', True)


if __name__ == '__main__':
    _ = unittest.main()
