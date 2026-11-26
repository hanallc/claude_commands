"""Test copy buttons in the Review browser."""

import json
import shutil
import unittest

from scripts.review_links import change_browser, change_page, page_frame
from scripts.review_links.browser_test_support import firefox_probe_results


class QuestionCodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for this browser test")
    def test_adjacent_code_blocks_stay_in_one_numbered_item(self) -> None:
        first = 'printf "<tag> & value"'
        second = "Choices: 3. This is code.\n\nRecommended: Keep this line."
        first_fence = f"```sh\r\n{first}\r\n```"
        second_fence = "```text\r\n" + second.replace("\n", "\r\n") + "\r\n```"
        adjacent = first_fence + "\r\n" + second_fence
        numbered = "1. Read the title.\r\n2. Run both:\r\n\r\n"
        inline, choices = change_browser.text_sections(numbered + adjacent + "\r\n\r\nRead the result.")
        spaced, spaced_choices = change_browser.text_sections(numbered + first_fence + "\r\n\r\n" + second_fence)
        self.assertEqual(choices, "")
        self.assertEqual(spaced_choices, "")
        self.assertTrue(inline.endswith("<p>Read the result.</p>"))

        cases = (
            (change_browser.item_list(["Run both:", adjacent]), 1),
            (change_browser.item_list(["Run both:\r\n" + adjacent]), 1),
            (change_browser.item_list(["Run both:", first_fence, second_fence]), 1),
            (inline, 2),
            (spaced, 2),
            (change_page.review_html(adjacent), 0),
            (change_browser.question_paragraphs(adjacent), 0),
        )
        sections = "".join(f'<section data-items="{items}">{body}</section>' for body, items in cases)
        probe = r'''
const failures=[],copied=[];
Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{copied.push(text);}}});
for(const [index,section] of [...document.querySelectorAll('section')].entries()){
 const items=section.querySelectorAll('ol > li');
 if(items.length!==Number(section.dataset.items)) failures.push('Unexpected item count for case '+index);
 const buttons=section.querySelectorAll('[data-copy-code]');
 if(items.length && [...buttons].some(button=>button.closest('li')!==items[items.length-1]))
  failures.push('Copy button outside the command item for case '+index);
 const start=copied.length;
 for(const button of buttons) button.click();
 if(JSON.stringify(copied.slice(start))!==JSON.stringify(EXPECTED_CODES))
  failures.push('Unexpected clipboard text for case '+index+': '+JSON.stringify(copied.slice(start)));
}
void fetch('/results',{method:'POST',body:JSON.stringify(failures)});
'''.replace("EXPECTED_CODES", json.dumps([first, second]))
        page = f"<!doctype html><meta charset=utf-8>{sections}<script>{page_frame.COPY_SCRIPT}\n{probe}</script>"
        self.assertEqual(firefox_probe_results(page.encode()), [[]])
