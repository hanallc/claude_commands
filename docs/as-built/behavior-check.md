# Behavior check before landing

The check finds existing tests that a commit alters or removes. It adds no marks to tests.

Run `scripts/lib/py scripts/production/behavior_check.py find --repo CHECKOUT --base BASE --commit COMMIT` to see the test changes. The command prints the key and each test's old and new text.

The finder reads Python test functions and Rust functions with test attributes. It skips new tests and tests that move with the same text.

The key hashes the test IDs and their old and new text. A merge with the same test changes uses the same key.

`land_main.py` fetches the destination under the push lock. Before the push it checks the fetched tip against the commit. Only a clear or Yes record permits the push when the range changes an existing test.

The showrunner state root stores the records in `behavior_checks/`. Each record has one key and one state. The states are clear, waiting, Yes, and No.
