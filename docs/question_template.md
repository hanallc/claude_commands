# Question template

This file is the one source for the form of a question for the user.
`/showrunner:question`, `/unit:question` and `/showrunner:produce` import it.
`update_registration.py ask --help` names it.

The Questions page shows each part as its own section.
The user reads the question days later, with nothing else in front of them.

## Parts

- `--ask`: the one-line title, at most `question_title_max_chars` characters from `config/production.conf`. Put the rest in `--context` and `--item`.
- `--context`: two sentences at most. Say why you ask now and which work waits.
- `--item`: one fact, step or command. Repeat `--item` for each one.
  - Put a command in backticks, as its own item. The page shows it as a code block.
  - For an image, give the image's own link that `/show_image` prints, not the Images page link.
- `--choice`: one answer in one line. Say what happens when the user picks it. Repeat `--choice` for each answer.
- `--recommend`: the name of one choice and the reason, in one sentence.

A question with choices needs at least two `--choice` and one `--recommend`.
An open question has no `--choice`.
Do not put a list, a step or a command in `--context`. Give it an `--item`.

## Public API questions

Before you ask the user any public API question, including a proposed addition, answer these from the code:

1. Why must it exist? Start with the why in one short sentence: what a user or caller cannot do without it. "It exists already" (in the code, as a private type, on the trunk, in the plan) is a fact to name, never the why. Then name each thing that exists and could do the job. Check at least a model, a public item, the behavior on the trunk and a line of the plan. Say why each one cannot. A signature that takes `World` also says why no narrower Bevy form works (world-arguments-need-a-reason.md).
2. Is it an event? Say who reads the item, and when.
3. What is the code? Show the item beside its neighbours, with one real scenario.

The question to the user shows the why from check 1 for each API item.

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
