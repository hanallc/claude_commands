import unittest

from scripts.lib.display_text import display_field


class DisplayFieldTests(unittest.TestCase):
    def test_control_characters_go_and_whitespace_becomes_one_space(self) -> None:
        self.assertEqual(display_field("stderr: \x1b[31mrefused\x1b[0m\n\tline two\x00"), "stderr: [31mrefused[0m line two")


if __name__ == "__main__":
    _ = unittest.main()
