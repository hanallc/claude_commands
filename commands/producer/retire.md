---
description: Retire a producer.
argument-hint: 'NAME [--to PRODUCER]'
---

Run only on the user’s direct instruction. Verify that `NAME` identifies a producer. The program refuses to retire morty.

1. Run `agents retire NAME [--to PRODUCER]` with the supplied arguments.
2. Show the output. Success prints `NAME: retired`.

The program refuses while the producer has an open question. If the producer's `handoff.md` is absent, the program asks the producer for it, exits 0 and does not retire. Run the command again after the file exists.
Open List items move to morty by default. Use `--to PRODUCER` to move them to another live producer.

Use `agents resume NAME --retired` to restore the producer after the user confirms.
See [agents.md](../agents.md#rename-or-retire-a-producer) in `~/.claude/commands`.
