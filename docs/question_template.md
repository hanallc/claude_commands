# Question template

This file is the one source for the form of a question for the user.
`/showrunner:question`, `/unit:question` and `/showrunner:produce` import it.
`update_registration.py ask --help` names it.

The Questions page shows each part as its own section.
The user reads the question days later, with nothing else in front of them.

## Parts

- `--ask`: the question in one line. The lists use it as the title.
- `--context`: two sentences at most. Say why you ask now and which work waits.
- `--item`: one fact, step or command. Repeat `--item` for each one.
  - Put a command in backticks, as its own item. The page shows it as a code block.
  - For an image, give the image's own link that `/show_image` prints, not the Images page link.
- `--choice`: one answer in one line. Say what happens when the user picks it. Repeat `--choice` for each answer.
- `--recommend`: the name of one choice and the reason, in one sentence.

A question with choices needs at least two `--choice` and one `--recommend`.
An open question has no `--choice`.
Do not put a list, a step or a command in `--context`. Give it an `--item`.

## Example

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py ask --unit <unit staff id> \
  --ask "alpha: delete the old trace folder on natedev?" \
  --context "The alpha traces fill 40 GB on natedev. The next trace run waits for the space." \
  --item "The folder holds the traces of phases 1 to 4; their reports are in the plan." \
  --item "The delete cannot be undone." \
  --item '`rm -r ~/scratch/alpha-traces`' \
  --choice "Delete it: the next trace run starts at once." \
  --choice "Keep it: alpha moves the next trace run to the Mac." \
  --recommend "Delete it, because the plan keeps every fact the traces gave."
```
