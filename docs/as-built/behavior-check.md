# Behavior check before landing

The check finds existing tests that a commit alters or removes. It adds no marks to tests.

Run `scripts/lib/py scripts/production/behavior_check.py find --repo CHECKOUT --base BASE --commit COMMIT` to see the test changes. The command prints the key and each test's old and new text.

The finder reads Python test functions and Rust functions with test attributes. It skips new tests and tests that move with the same text.
It includes the attributes of enclosing Rust modules in the test text.

The key hashes the origin identity, the changed files, the old and new test text, and the commit messages in the range.
Each changed file contributes its path, its old and new Git modes, and its old and new Git object IDs.
A change to a commit message requires a new review, even when the patch is the same.

`land_main.py` and direct checkpoint pushes read the default branch from origin.
They check default-branch pushes against the fetched tip.
The PR script checks the range from the merge base to the PR head.
It reads the head and base again before merge and supplies the checked head to GitHub.

A clear record names the saved reviewer reply.
The gate checks that the reply has a finding of no user decision for each changed test.
For a Yes record, the gate reads the answer in the original question.

The showrunner state root stores the records in `behavior_checks/`.
Each key has one state. The states are clear, waiting, invalid answer, Yes, and No.

Run `behavior_check.py run` with the same range before a land. Use the repository's `scripts/lib/py` launcher. Add `--production OWNER` to select the owner of a Review question.

1. If the range has no changed test, the command exits 0.
2. If the range has changed tests, the command starts an independent read-only review. Run the same command after the reviewer replies.
3. If the reviewer finds no decision, the command saves a clear record.
4. If the reviewer finds a decision, the command drafts one question for Review.
5. Run `behavior_check.py status --key KEY` to save the answer and its time. The command also accepts the original range.

The question title names the behavior and the date of the decision.
The point quotes the user's words, gives the reason for the change, and says what the change does.
The reviewer reads the commit messages in the range for the reason.
If the messages give no reason, the reviewer uses "The change gives no reason."
Each additional finding gives one fact line. The recommendation and choices give the result of each answer.

A retry starts a new review if the prior reviewer process ended before it saved a valid reply.
Run the check again if the saved reply has no behavior name or reason field.
The collector reads the first word of the answer. It ignores letter case and punctuation at the end of that word.
If the Console review holds the question, the command reports the hold. The land stays refused.
The command then prints "Nate was not asked. Give the reason for the change in the commit message, then run the check again."
If the first word is neither Yes nor No, `behavior_check.py run` quotes the answer and the land stays refused.
A missing review, an incomplete review, a question without an answer, and a No answer stop the land. Review errors also stop the land.
