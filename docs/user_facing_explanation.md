# User-facing explanation

Use the [Console text standard](console_text_standard.md) for the text of each item.

## Ad hoc review parts

1. Title
2. The point
3. How the choices behave
4. Concrete scenario
5. Question
6. Recommendation
7. Implementation mapping
8. Still pending
9. Choices

## Reader test and comprehension gate

Use rule 10 of the Console text standard before you show each item.
For a prepared batch, give a helper only the titles and the point of each item.
Ask the helper what each choice does and why the recommendation helps.
Rewrite each item that the helper cannot explain.

## Which decisions reach the user

Before presenting anything as a decision, test all four:

1. **Is it real?** Checked against the actual tree or code, not against a
   document's claim about them. A stale status file is not evidence.
2. **Does it have at least two buildable answers?** Test each option you would
   offer: can it be built as described? A list with one viable option is not a
   decision — make the correction yourself and report it in one line.
3. **Is it theirs?** If the source material already answers it, if it is
   mechanical, or if it affects only later work, then do it or defer it and say
   so in one line.
4. **Would the finished thing differ?** Compare the two options at the level of
   what ships: behavior, contract, whether a piece of scope exists. If the
   deliverable is identical either way and only the route there changes — how
   the work is split, ordered, numbered, or which unit owns a task — it is not
   a decision. Decide it, state the call in one line, keep going. Two buildable
   answers make it a choice; only a difference in the result makes it *theirs*.

Manufactured blockers stall authorized work and bury the real findings among
fake ones.

## Choices

- One line of ordinary message text. **Never** a survey, AskUserQuestion, option
  chips, or any questionnaire UI — not for the items, not for any step of the
  workflow. Keep the technical content, drop the widget.
- Mark the recommendation as `(recommended — <one-line reason>)`.
- Include the discussion choices `elaborate / simplify`.
- One decision per turn. Split an item that contains independent questions.

## When it does not land

- Confusion means the explanation failed, not that the user needs more of the
  same. Do not re-emit the summary with softer words.
- **Repair downward once, then upward.** First signal: rebuild from the concrete
  triggering situation, introducing no new labels. Second signal about the same
  item: stop adding detail and go up to the model. Finer-grained detail produces
  a chain of individually-correct explanations that never converges.
- **Model-level questions get model-level answers.** "What are examples of X?",
  "how is it used?", "what's the difference between X and Y?", "is it per-X or
  per-Y?" ask for the model, not for more detail about the current item.
  Answering a model-level question with item detail is the most common way this
  fails, and it fails silently — each answer is accurate, so nothing looks wrong
  until the user has asked four times.
- **If the user restates the model themselves and asks you to confirm it, the
  orientation failed.** The synthesis landed on them. Confirm, adopt their
  wording verbatim for the rest of the session, and reframe every remaining item
  inside it.
- For a full rebuild, follow `~/.claude/docs/explain_on_demand.md`. Explaining
  never authorizes anything: afterwards restate the pending question and wait.

## Tics

- Do not echo the user's feedback back to them unless asked. Record it.
- Do not manufacture content to fill a template. Write "none" and say why.
- No preamble, no recap, no closing summary.
