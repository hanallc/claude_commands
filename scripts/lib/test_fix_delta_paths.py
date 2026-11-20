"""Check the path rules for returned fix deltas."""
from __future__ import annotations

import unittest

from scripts.lib.fix_delta_paths import FixDeltaPath, validate_fix_delta_paths


class FixDeltaPathTests(unittest.TestCase):
    def test_keeps_valid_paths_and_their_order(self) -> None:
        source = ('src/café.rs', 'src/with space.rs', '.cargo/other.toml', 'other/src.rs')
        self.assertEqual(validate_fix_delta_paths(source), tuple(
            FixDeltaPath(path, tuple(path.split('/'))) for path in source))
        self.assertEqual(validate_fix_delta_paths(()), ())

    def test_refuses_invalid_path_forms(self) -> None:
        paths = (
            ('empty path', ''),
            ('absolute path', '/src/main.rs'),
            ('drive path', 'C:/src/main.rs'),
            ('backslash', 'src\\main.rs'),
            ('colon', 'src/main:rs'),
            ('parent component', 'src/../main.rs'),
            ('dot component', 'src/./main.rs'),
            ('empty component', 'src//main.rs'),
            ('trailing slash', 'src/'),
            ('trailing dot', 'src./main.rs'),
            ('trailing space', 'src/main.rs '),
            ('null character', 'src/nu\x00ll.rs'),
            ('control character', 'src/new\nline.rs'),
            ('delete character', 'src/\x7f.rs'),
            ('C1 control character', 'src/\x85.rs'),
            ('decomposed unicode', 'src/cafe\u0301.rs'),
            ('invalid UTF-8 scalar', 'src/\ud800.rs'),
        )
        for name, path in paths:
            with self.subTest(name=name), self.assertRaises(ValueError):
                _ = validate_fix_delta_paths((path,))
        with self.assertRaises(ValueError):
            _ = validate_fix_delta_paths('src/main.rs')

    def test_refuses_repository_metadata_at_any_depth(self) -> None:
        components = ('.git', '.gitmodules', '.gitignore', '.gitattributes', '.hg', '.hgignore',
                      '.hgtags', '.svn', '.bzr', '.claude', '.codex', '.direnv')
        for component in components:
            for path in (component, f'src/{component.upper()}/file'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    _ = validate_fix_delta_paths((path,))
        for path in ('.cargo/config', '.cargo/config.toml', 'src/.CARGO/CONFIG', 'src/.CARGO/CONFIG.TOML'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                _ = validate_fix_delta_paths((path,))

    def test_refuses_destination_and_directory_aliases(self) -> None:
        pairs = (
            ('duplicate', 'src/main.rs', 'src/main.rs'),
            ('case alias', 'src/main.rs', 'src/MaIn.rs'),
            ('case-fold alias', 'src/Straße.rs', 'src/STRASSE.rs'),
            ('file then descendant', 'src/data', 'src/data/file.rs'),
            ('descendant then file', 'src/data/file.rs', 'src/DATA'),
            ('alternate directory spelling', 'src/Data/a.rs', 'src/data/b.rs'),
        )
        for name, first, second in pairs:
            with self.subTest(name=name), self.assertRaises(ValueError):
                _ = validate_fix_delta_paths((first, second))


if __name__ == '__main__':
    _ = unittest.main()
