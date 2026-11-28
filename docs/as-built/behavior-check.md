# Behavior check before landing

The check finds existing tests that a commit alters or removes. It adds no marks to tests.

Run `scripts/lib/py scripts/production/behavior_check.py find --repo CHECKOUT --base BASE --commit COMMIT` to see the test changes. The command prints the key and each test's old and new text.
The `find`, `run`, `check`, and `status` commands compare the result of `git merge-base BASE COMMIT` with COMMIT.

The finder reads Python test functions and Rust functions with test attributes. It skips new tests and tests that move with the same text.
It includes the attributes of enclosing Rust modules in the test text.

The key hashes the origin identity, the changed files, and the old and new test text.
Each changed file contributes its path, its old and new Git modes, and its old and new Git object IDs.

`land_main.py` and direct checkpoint pushes read the default branch from origin.
They check default-branch pushes against the fetched tip.
The legacy direct-main gate checks from the merge base to the checkpoint before it merges.
Promotion checks behavior once for a new default-branch tip before it moves live main or pushes the commit.
The PR script checks the range from the merge base to the PR head.
It reads the head and base again before merge and supplies the checked head to GitHub.

A clear record names the saved reviewer reply.
The reviewer filters transcript files before parsing them.
The prompt gives the checker's absolute code root and paths to the transcript reader and question record code.
The reviewer starts isolated Python from the checker's code root before it imports the checker modules.
It passes the imports to Python with `-c` so the shell needs no temporary file.
The reviewer runs each command in one tool call without `write_stdin`.
For a `requested` finding, the reviewer quotes one source message.
If several messages contain relevant words, the reviewer quotes the newest message that sets the behavior.
The gate checks the finding for each changed test.
Source verification applies only to `requested` findings.
The check accepts typed Claude turns, sent List Tell feedback, and saved List or Question answers from Nate.
The check accepts a List question from Nate when its List page receipt matches a saved send receipt in state `sent`.
The check requires a signed Images answer with a matching, completed server delivery to all recipients.
For a List entry, the server record must match the List item, keeper, delivery key, and words.
The approval line counts as approval. The feedback counts as separate words from Nate.
The Images store keeps standalone answers in `.review-link-image-decisions/*.json` under the configured review links directory.
The check reads `answer` and `feedback` as Nate's words when the archived decision matches the server delivery.
The reviewer uses `image_text` for context.
It matches consecutive full sentences or lines and treats each run of whitespace as one space.
The check reports the date from the source. A `requested` finding needs a question if the source cannot prove the author or quote.
Status fails if it cannot read or parse the saved proof for a clear record.
For a Yes record, the gate reads the answer in the original question.

The reviewer gives a verdict for each `specified` finding.
Use `against` only when the change removes or changes behavior Nate explicitly asked for.
A refinement of a term Nate did not define, such as "history", does not warrant `against`.
The verdicts `against` and `unclear` need a question. The verdicts `follows` and `different` clear the finding.

The showrunner state root stores the records in `behavior_checks/`.
Each key has one state. The states are clear, waiting, invalid answer, Yes, and No.

Run `behavior_check.py run` with the same range before a land. Use the repository's `scripts/lib/py` launcher. Add `--production OWNER` to select the owner of a Review question.

1. If the range has no changed test, the command exits 0.
2. If the range has changed tests, the command starts an independent read-only review. Run the same command after the reviewer replies.
3. The command saves a clear record when no test needs a question. It lists each verified request with its test, quote, date, and source.
4. The command prints the evidence for findings that need a question. It includes requests with unverified quotes.
5. Send the finding to the unit named in `Ask UNIT (phase PHASE)`.
6. Ask the author to write the question and run the same command with `--question FILE`.
7. Run `behavior_check.py status --key KEY` to save the answer and its time. The command also accepts the original range.

`status` recovers a filed question if `run` stopped before it saved the local record.

The command traces each finding to the commit that last changed the test in the range.
If that commit is a phase merge, the command names its unit and phase.
For other commits, the command looks for a first-parent phase merge that brought in the commit.
If there is no such merge, it prints the commit hash and subject.
If the lookup fails, the command prints the range tip's hash and subject.
If the command cannot read the subject, it prints `Ask the author of COMMIT`.

Use the question template to write `ask`, `context`, `items`, `choices`, and `recommend` in the JSON file.
Include each finding's ISO date in the title or point.
Paraphrase Nate's words when needed. The review record and refusal output keep the exact quote.
Start one choice with `Yes (the change goes in` and the other with `No (the change stays out`.
Describe the result of each choice after its required start.
The command files one question per key with `blocks` urgency.
Once the command files the question, later runs track the answer without the file.

A retry starts a new review if the prior reviewer process ended before it saved a valid reply.
The reviewer can report a blocked part as `[{"blocker":"what stopped the review"}]`.
The blocker row may include a `source` field.
The command prints the part number and blocker text. A retry starts a new review for that part.
The collector reads the first word of the answer. It ignores letter case and punctuation at the end of that word.
If the Console review holds the question, the command prints the review's questions and failed rules.
Revise the JSON file and run the command again with `--question FILE`.
If the first word is neither Yes nor No, `behavior_check.py run` quotes the answer and the land stays refused.
A missing review, an incomplete review, a question without an answer, and a No answer stop the land. Review errors also stop the land.
