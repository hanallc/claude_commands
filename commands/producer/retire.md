---
description: Retire a producer.
---

Run only on the user’s direct instruction. Verify that `NAME` identifies a producer. The program refuses to retire morty.

1. Run `agents retire NAME [--to PRODUCER]` with the supplied arguments.
2. Show the output. Success prints `NAME: retired`.

The program requires no open questions. If `handoff.md` is absent, it requests the file and asks you to repeat the command.
Open List items move to morty by default. Use `--to PRODUCER` to move them to another live producer.

Use `agents resume NAME --retired` to restore the producer after the user confirms.
See [agents.md](../agents.md#rename-or-retire-a-producer) in `~/.claude/commands`.
