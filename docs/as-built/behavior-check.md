# Behavior check before landing

The check finds existing tests that a commit alters or removes. It adds no marks to tests.

Run `scripts/lib/py scripts/production/behavior_check.py find --repo CHECKOUT --base BASE --commit COMMIT` to see the test changes. The command prints the key and each test's old and new text.

The finder reads Python test functions and Rust functions with test attributes. It skips new tests and tests that move with the same text.

The key hashes the origin identity, the whole patch, and the old and new test text.
A merge with the same patch in the same repository uses the same key.

`land_main.py` checks default-branch pushes against the fetched tip under the push lock.
The PR script checks the range from the merge base to the PR head.
It reads the head and base again before merge and supplies the checked head to GitHub.

A clear record names the saved reviewer reply.
The gate checks that the reply has a finding of no user decision for each changed test.
For a Yes record, the gate reads the answer in the original question.

The showrunner state root stores the records in `behavior_checks/`. Each record has one key and one state. The states are clear, waiting, Yes, and No.

Run `behavior_check.py run` with the same range before a land. Use the repository's `scripts/lib/py` launcher. Add `--production OWNER` to select the owner of a Review question.

1. If the range has no changed test, the command exits 0.
2. If the range has changed tests, the command starts an independent read-only review. Run the same command after the reviewer replies.
3. If the reviewer finds no decision, the command saves a clear record.
4. If the reviewer finds a decision, the command files one question on Review. The question gives the user's words, the change, and the Yes and No choices.
5. Run `behavior_check.py status --key KEY` to save the answer and its time. The command also accepts the original range.

A missing review, an incomplete review, a question without an answer, and a No answer stop the land. Review errors also stop the land.
