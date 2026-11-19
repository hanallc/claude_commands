"""The user's question supplies the Waiting reason for each Review type."""
from __future__ import annotations

import unittest
from typing import override

from scripts.production import investigation, update_registration
from scripts.production import test_waiting_why as fixtures
from scripts.review_links import change_page, review_actions, review_items


class WaitingActionTests(unittest.TestCase):
    fixture: fixtures.WaitingWhyTests = fixtures.WaitingWhyTests('runTest')

    @override
    def setUp(self) -> None:
        self.fixture = fixtures.WaitingWhyTests('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_user_question_automatically_stores_its_text_for_each_type(self) -> None:
        owner = self.fixture.fixture.unit.id
        for report_kind in investigation.InvestigationKind:
            record = investigation.file(owner, f'Check the {report_kind}', 'What caused this?',
                                        waiting_why=fixtures.WHY, kind=report_kind)
            _ = investigation.answer(owner, record.id, 'The cause is known.')
        for kind in review_items.REVIEW_TYPES:
            with self.subTest(kind=kind):
                item = next(item for item in review_items.read(self.fixture.root, assign_ids=False) if item.type == kind)
                words = 'Which evidence resolves this ' + kind.value + '?'
                status = change_page.DeliveryStatus(self.fixture.root)
                if kind == review_items.ReviewType.CHANGE:
                    _ = status.ask(self.fixture.fixture.page, 'C1', words)
                elif kind == review_items.ReviewType.QUESTION:
                    from scripts.review_links import change_browser
                    browser = change_page.ReviewBrowser('private', kind='questions')
                    question = next(value for value in change_browser.prepare(browser, status).questions if value.key == item.key)
                    identities = update_registration.QuestionStaff.read()
                    scope = update_registration.question_scope(item.owner, identities.record)
                    _ = change_browser.save_outcome(scope, question, 'question', words, identities)
                elif isinstance(item.source, review_items.InvestigationSource):
                    _ = review_actions.investigation_action(item, 'question', words, status)
                else:
                    _ = review_actions.decision_action(item, 'question', words, status)
                current = next(value for value in review_items.read(self.fixture.root, assign_ids=False) if value.key == item.key)
                expected_state = (
                    review_items.ReviewState.VALIDATING
                    if kind == review_items.ReviewType.CHANGE
                    else review_items.ReviewState.WAITING
                )
                self.assertEqual(current.state, expected_state)
                self.assertEqual(current.waiting_why, words)


if __name__ == '__main__':
    _ = unittest.main()
