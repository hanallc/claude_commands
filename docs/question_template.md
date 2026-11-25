# Question template

Use the [Console text standard](console_text_standard.md) to write each part.

## Parts

- `--ask` — Title
- `--context` — The point
- `--item` — Fact line
- `--choice` — Choice
- `--recommend` — Recommendation

## Public API questions

Before you ask the user any public API question, including a proposed addition, answer these from the code:

1. Why must it exist? Start with the why in one short sentence: what a user or caller cannot do without it. "It exists already" (in the code, as a private type, on the trunk, in the plan) is a fact to name, never the why. Then name each thing that exists and could do the job. Check at least a model, a public item, the behavior on the trunk and a line of the plan. Say why each one cannot. A signature that takes `World` also says why no narrower Bevy form works (world-arguments-need-a-reason.md).
2. Is it an event? Say who reads the item, and when.
3. What is the code? Show the item beside its neighbours, with one real scenario.

The question to the user shows the why from check 1 for each API item.
