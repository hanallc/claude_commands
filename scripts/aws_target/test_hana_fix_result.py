"""Check the fix result forms and the fixed encoded size bound."""
from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import replace
import json
from typing import ClassVar, cast
import unittest
from unittest import mock

from scripts.aws_target import hana_result as result
from scripts.aws_target.test_hana_result import command_result


MEND = ('lint', 'mend-fix', '--workspace')
CLIPPY_FIX = ('lint', 'clippy-fix', '--workspace')
CLIPPY = ('cargo', 'clippy', '--workspace')


def fix_result[T](factory: Callable[..., T], *, argv: tuple[str, ...] = MEND, status: int = 0,
                  **fields: object) -> T:
    common = replace(command_result(status), argv=argv)
    return factory(**vars(common), **fields)


def wire_row(content: bytes) -> dict[str, object]:
    return result.object_fields(cast(object, json.loads(content)))


class HanaFixResultTests(unittest.TestCase):
    fix_permitted: ClassVar[result.HanaFixPermitted] = result.HanaFixPermitted()
    judgment_only: ClassVar[result.HanaJudgmentOnly] = result.HanaJudgmentOnly()

    def sample_delta(self) -> result.HanaFixDelta:
        return fix_result(result.HanaFixDelta, skipped_checks=['mend'], entries=[
            result.FixDeltaAdd('src/new.sh', b'\xff', '100755'),
            result.FixDeltaReplace('src/main.rs', b'\x00edited'),
            result.FixDeltaRemove('src/obsolete.rs'),
        ])

    def assert_round_trip(self, value: result.HanaGuestResult, policy: result.HanaResultDeltaPolicy) -> None:
        content = result.encode_result(value, delta_policy=policy)
        self.assertEqual(result.decode_result(content, delta_policy=policy), value)
        self.assertEqual(result.decode_result_fields(wire_row(content), delta_policy=policy), value)

    def assert_refused(self, row: dict[str, object], policy: result.HanaResultDeltaPolicy) -> None:
        with self.assertRaises(ValueError):
            _ = result.decode_result(json.dumps(row).encode(), delta_policy=policy)
        with self.assertRaises(ValueError):
            _ = result.decode_result_fields(row, delta_policy=policy)

    def test_judgment_wire_matches_main(self) -> None:
        expected = (b'{"version":1,"key":{"episode":"episode","attempt":"attempt","claim_id":"claim-attempt",'
                    + b'"generation":{"deployment":"deployment","start_token":"start","boot_id":"boot"}},'
                    + b'"source_key":"verify-source","commit":"' + b'a' * 40 + b'","toolchain":"' + b'b' * 64
                    + b'","host":"fixture-hana","argv":["cargo","check","--all-features"],"slot":1,"status":101,'
                    + b'"stdout":"b3V0cHV0AP8=","stderr":"ZGlhZ25vc3RpY/4=","started_at":100.0,"finished_at":130.0,'
                    + b'"execution_seconds":30.0,"memory":{"bytes":12345}}')
        value = command_result(101)
        self.assertEqual(result.encode_result(value), expected)
        self.assertEqual(result.decode_result(expected), value)
        self.assertEqual(result.decode_result_fields(wire_row(expected)), value)
        self.assert_round_trip(value, self.judgment_only)

    def test_version_one_refuses_fix_commands(self) -> None:
        judgment = command_result()
        judgment_row = wire_row(result.encode_result(judgment))
        for argv in (MEND, CLIPPY_FIX):
            with self.subTest(argv=argv):
                value = replace(judgment, argv=argv)
                row = {**judgment_row, 'argv': list(argv)}
                with self.assertRaises(ValueError):
                    _ = result.encode_result(value)
                with self.assertRaises(ValueError):
                    _ = result.encode_result(value, delta_policy=self.judgment_only)
                with self.assertRaises(ValueError):
                    _ = result.decode_result(json.dumps(row).encode())
                with self.assertRaises(ValueError):
                    _ = result.decode_result_fields(row)
                self.assert_refused(row, self.judgment_only)

    def test_all_tagged_forms_round_trip(self) -> None:
        rows: tuple[tuple[result.HanaGuestResult, result.HanaResultDeltaPolicy], ...] = (
            (fix_result(result.HanaFixEmpty, skipped_checks=[]), self.fix_permitted),
            (self.sample_delta(), self.fix_permitted),
            (fix_result(result.HanaFixFailed, status=101, skipped_checks=['clippy'],
                        fix_status=101, reason='compiler failure'), self.fix_permitted),
            (fix_result(result.HanaFixFailed, skipped_checks=[], fix_status=1,
                        reason='delta collection failed'), self.fix_permitted),
            (fix_result(result.HanaLintSkipped, skipped_checks=['mend', 'clippy'],
                        reason='disabled_by_policy'), self.fix_permitted),
            (fix_result(result.HanaLintSkipped, argv=CLIPPY_FIX, skipped_checks=['clippy'],
                        reason='disabled_by_policy'), self.fix_permitted),
            (fix_result(result.HanaLintSkipped, argv=CLIPPY, skipped_checks=['clippy'],
                        reason='disabled_by_policy'), self.judgment_only),
        )
        for value, policy in rows:
            with self.subTest(kind=type(value).__name__, argv=value.argv, status=value.status):
                self.assert_round_trip(value, policy)
        wire = wire_row(result.encode_result(self.sample_delta(), delta_policy=self.fix_permitted))
        self.assertEqual(wire['entries'], [
            {'kind': 'add', 'path': 'src/new.sh', 'data': '/w==', 'mode': '100755'},
            {'kind': 'replace', 'path': 'src/main.rs', 'data': 'AGVkaXRlZA=='},
            {'kind': 'remove', 'path': 'src/obsolete.rs'},
        ])

    def test_no_policy_refuses_each_tagged_form(self) -> None:
        encode_without_policy = cast(Callable[[result.HanaGuestResult], bytes], result.encode_result)
        values: tuple[result.HanaGuestResult, ...] = (
            fix_result(result.HanaFixEmpty, skipped_checks=[]),
            self.sample_delta(),
            fix_result(result.HanaFixFailed, skipped_checks=[], fix_status=1, reason='failed'),
            fix_result(result.HanaLintSkipped, skipped_checks=['mend', 'clippy'], reason='disabled_by_policy'),
        )
        for value in values:
            with self.subTest(kind=type(value).__name__):
                content = result.encode_result(value, delta_policy=self.fix_permitted)
                with self.assertRaises(ValueError):
                    _ = encode_without_policy(value)
                with self.assertRaises(ValueError):
                    _ = result.decode_result(content)
                with self.assertRaises(ValueError):
                    _ = result.decode_result_fields(wire_row(content))
                self.assert_refused(wire_row(content), self.judgment_only)
        with self.assertRaises(ValueError):
            _ = result.encode_result(command_result(), delta_policy=self.fix_permitted)
        self.assert_refused(wire_row(result.encode_result(command_result())), self.fix_permitted)

    def test_malformed_result_fields(self) -> None:
        original = wire_row(result.encode_result(self.sample_delta(), delta_policy=self.fix_permitted))
        rows: tuple[tuple[str, object], ...] = (
            ('version', 1), ('version', True), ('version', 3),
            ('kind', 'HanaUnknown'), ('kind', 2), ('status', 1), ('status', False),
            ('argv', ['cargo', 'check']), ('skipped_checks', 'mend'), ('skipped_checks', ['fmt']),
            ('skipped_checks', ['mend', 'mend']), ('skipped_checks', ['mend', 'clippy']),
            ('stdout', '@@@'), ('stderr', 'é'), ('memory', {'bytes': 5, 'reason': 'both'}),
            ('entries', []), ('entries', {}), ('extra', 'field'),
        )
        for field, bad in rows:
            with self.subTest(field=field, bad=bad):
                self.assert_refused({**original, field: bad}, self.fix_permitted)
        for field in ('kind', 'source_key', 'skipped_checks', 'entries'):
            with self.subTest(missing=field):
                self.assert_refused({name: value for name, value in original.items() if name != field}, self.fix_permitted)
        duplicate = b'{"version":2,' + result.encode_result(self.sample_delta(), delta_policy=self.fix_permitted)[1:]
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            _ = result.decode_result(duplicate, delta_policy=self.fix_permitted)

    def test_both_decoders_refuse_nonfinite_numbers_in_an_attempt_key(self) -> None:
        value = self.sample_delta()
        content = result.encode_result(value, delta_policy=self.fix_permitted)
        row = wire_row(content)
        key = result.object_fields(row['key'])
        for literal in (b'NaN', b'Infinity', b'-Infinity', b'1e400', b'-1e400'):
            with self.subTest(literal=literal):
                raw = content.replace(b'"key":{', b'"key":{"extra":' + literal + b',', 1)
                with self.assertRaisesRegex(ValueError, 'JSON numbers must be finite'):
                    _ = result.decode_result(raw, delta_policy=self.fix_permitted)
                number = cast(object, json.loads(literal))
                with self.assertRaisesRegex(ValueError, 'JSON numbers must be finite'):
                    _ = result.decode_result_fields({**row, 'key': {**key, 'extra': number}},
                                                    delta_policy=self.fix_permitted)
        finite = content.replace(b'"key":{', b'"key":{"extra":1e308,', 1)
        self.assertEqual(result.decode_result(finite, delta_policy=self.fix_permitted), value)
        self.assertEqual(result.decode_result_fields(wire_row(finite), delta_policy=self.fix_permitted), value)

    def test_codec_refuses_invalid_common_command_fields(self) -> None:
        value = self.sample_delta()
        row = wire_row(result.encode_result(value, delta_policy=self.fix_permitted))
        earlier = value.started_at - 1
        invalid_key = replace(value.key, generation=replace(value.key.generation, start_token=''))
        invalid_argv = (*value.argv, 'part\0rest')
        cases: tuple[tuple[str, object, result.HanaFixDelta], ...] = (
            ('key', result.encode_attempt_key(invalid_key), replace(value, key=invalid_key)),
            ('source_key', '', replace(value, source_key='')),
            ('commit', '', replace(value, commit='')),
            ('toolchain', '', replace(value, toolchain='')),
            ('host', '', replace(value, host='')),
            ('argv', list(invalid_argv), replace(value, argv=invalid_argv)),
            ('slot', -1, replace(value, slot=-1)),
            ('status', -1, replace(value, status=-1)),
            ('started_at', -1, replace(value, started_at=-1)),
            ('finished_at', earlier, replace(value, finished_at=earlier)),
            ('execution_seconds', -1, replace(value, execution_seconds=-1)),
            ('memory', {'bytes': -1}, replace(value, memory=result.MeasuredPeak(-1))),
            ('memory', {'reason': ''}, replace(value, memory=result.PeakUnavailable(''))),
        )
        for field, number, changed in cases:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    _ = result.encode_result(changed, delta_policy=self.fix_permitted)
                self.assert_refused({**row, field: number}, self.fix_permitted)

    def test_malformed_entries(self) -> None:
        original = wire_row(result.encode_result(self.sample_delta(), delta_policy=self.fix_permitted))
        normal = {'kind': 'add', 'path': 'added.rs', 'data': 'QQ==', 'mode': '100644'}
        rows: tuple[tuple[str, object], ...] = (
            ('kind', {**normal, 'kind': 'copy'}),
            ('mode', {**normal, 'mode': '120000'}),
            ('base64', {**normal, 'data': 'Q!=='}),
            ('padding', {**normal, 'data': 'QQ'}),
            ('path type', {**normal, 'path': False}),
            ('unsafe path', {**normal, 'path': 'src/.git/config'}),
            ('missing mode', {'kind': 'add', 'path': 'added.rs', 'data': ''}),
            ('replacement mode', {**normal, 'kind': 'replace'}),
            ('removal data', {'kind': 'remove', 'path': 'old.rs', 'data': ''}),
            ('non-object', 'remove'),
        )
        for name, entry in rows:
            with self.subTest(name=name):
                self.assert_refused({**original, 'entries': [entry]}, self.fix_permitted)
        self.assert_refused({**original, 'entries': [normal, {**normal, 'path': 'ADDED.rs'}]}, self.fix_permitted)

    def test_addition_modes_match_on_encode_and_decode(self) -> None:
        value = self.sample_delta()
        modes: tuple[result.FixFileMode, ...] = ('100644', '100755')
        for mode in modes:
            with self.subTest(mode=mode):
                entry = result.FixDeltaAdd('src/added.rs', b'A', mode)
                self.assert_round_trip(replace(value, entries=[entry]), self.fix_permitted)
        invalid_mode = cast(result.FixFileMode, cast(object, '120000'))
        invalid_entry = result.FixDeltaAdd('src/added.rs', b'A', invalid_mode)
        with self.assertRaises(ValueError):
            _ = result.encode_result(replace(value, entries=[invalid_entry]), delta_policy=self.fix_permitted)

    def test_skip_matches_the_requested_checks(self) -> None:
        value = fix_result(result.HanaLintSkipped, skipped_checks=['mend', 'clippy'], reason='disabled_by_policy')
        row = wire_row(result.encode_result(value, delta_policy=self.fix_permitted))
        fix_cases: tuple[dict[str, object], ...] = (
            {'skipped_checks': []}, {'skipped_checks': ['mend']}, {'reason': ''},
            {'argv': list(CLIPPY_FIX)}, {'status': 1},
        )
        for changed in fix_cases:
            with self.subTest(changed=changed):
                self.assert_refused({**row, **changed}, self.fix_permitted)
        judgment = replace(value, argv=CLIPPY, skipped_checks=['clippy'])
        row = wire_row(result.encode_result(judgment, delta_policy=self.judgment_only))
        judgment_cases: tuple[dict[str, object], ...] = (
            {'argv': ['cargo', 'check']}, {'skipped_checks': ['mend']}, {'entries': []},
        )
        for changed in judgment_cases:
            with self.subTest(changed=changed):
                self.assert_refused({**row, **changed}, self.judgment_only)

    def test_failed_fix_never_carries_a_delta(self) -> None:
        value = fix_result(result.HanaFixFailed, status=101, skipped_checks=[], fix_status=101, reason='failed')
        row = wire_row(result.encode_result(value, delta_policy=self.fix_permitted))
        cases: tuple[dict[str, object], ...] = (
            {'entries': []}, {'fix_status': 0}, {'fix_status': True}, {'fix_status': 1}, {'reason': ''},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                self.assert_refused({**row, **changed}, self.fix_permitted)

    def test_fixed_encoded_bound_includes_delta_data_and_json_framing(self) -> None:
        encoded_bound = 32 * 1024 * 1024
        self.assertEqual(result.RESULT_MAX_BYTES, encoded_bound)
        entry = result.FixDeltaReplace('src/main.rs', b'')
        value = replace(self.sample_delta(), entries=[entry])
        row = wire_row(result.encode_result(value, delta_policy=self.fix_permitted))
        entry_fields = result.object_fields(cast(list[object], row['entries'])[0])

        data = b'x' * (encoded_bound * 3 // 4)
        oversized = replace(value, entries=[replace(entry, data=data)])
        with self.assertRaisesRegex(result.HanaFixDeltaLimitExceeded, '32 MiB'):
            _ = result.encode_result(oversized, delta_policy=self.fix_permitted)

        entry_fields['data'] = base64.b64encode(data).decode('ascii')
        with self.assertRaises(result.HanaFixDeltaLimitExceeded):
            _ = result.decode_result_fields(row, delta_policy=self.fix_permitted)
        content = json.dumps(row, separators=(',', ':')).encode()
        with mock.patch.object(json, 'loads', side_effect=AssertionError('parsed an oversized result')):
            with self.assertRaises(result.HanaFixDeltaLimitExceeded):
                _ = result.decode_result(content, delta_policy=self.fix_permitted)


if __name__ == '__main__':
    _ = unittest.main()
