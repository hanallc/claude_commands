# User-facing explanation

Shared by `/adhoc_review` and `/unit:direct`. Both reference this file rather
than carrying their own copy, so the method cannot drift between them. Applies
to any turn that presents a finding, a briefing, or a decision to the user.

For wording, load `/message_style`; its rules live in `~/rust/nate_style/message/`.

`~/.claude/docs/explain_on_demand.md` is the companion: this file governs the
first presentation, that one governs the rebuild after it fails to land.

## The principle

**You do the reconstruction, not the user.**

Every rule below is that sentence applied to something the user would otherwise
have to rebuild themselves:

| Left to them | Prevented by |
| --- | --- |
| What a name means | Describe the behavior before you give the name. |
| How the pieces relate | state the model before the items |
| Whether an issue is real | verify it before naming it; only real choices reach the user |
| Which option is better | exactly one recommendation, with its reason |
| Whether they already agreed | discussion never authorizes; gates survive explanation |

The user's synthesis work is the scarce resource. Spend it only on decisions
that are theirs, and make those answerable without reading anything.

## What the user knows

Only what has been said to them in user-facing conversation this session.

Not the source document, the diff, the reviews, another agent's output, or your
own earlier reasoning. Owning the project, requesting the review, or having
approved a related feature is not knowledge of a concept. A reference the user
has never seen is invisible to them.

**A report that repeats on a timer is read cold.** Progress updates arrive while
the user is doing something else, are read one at a time out of a long scroll,
and are read the morning after. Nothing an earlier update or briefing
established carries into this one: vocabulary does not accumulate across
updates, and a term defined three updates ago is undefined again here. Each one
re-orients before it reports, or it does not land.

## Build order — one rule at three scales

Build from what the reader already holds toward what they do not.

- **Within an item:** start with the user's situation. Use the template below for ad hoc review.
- **Within a walk:** the model everything sits inside → the individual items.
- **Within a report:** the readable summary → the technical reference.

## Naming

- Behavior first, name second. If the name is not needed for the decision, omit
  it.
- Never substitute a label for an explanation.
- Never present enum variants, policy names, state names, or API alternatives
  without saying what each one makes the system do. A list of names is not an
  explanation.
- Mark an undecided name `(name TBD)` rather than letting it sound settled.

## Banned unless defined in the same sentence

- Finding numbers or titles from another reviewer or agent.
- Plan-decision codes (`D2`, `I3`).
- Test, guard, file, or target identifiers used as if the user knows them.
- Tooling terms — `headless` becomes "the automated tests on this machine cannot
  drive a real screen"; `bind group` becomes what the data connection does.
- Internal bookkeeping (`fix pass 1 of 10`) as a headline. State the constraint
  in ordinary words where it matters.
- The design's own nouns — what a structure is built from (`edge`, `node`,
  `marker`, `ancestry`), the project's name for a subsystem, a type or trait
  name. From inside the design these read as ordinary words; from outside they
  read as nothing at all, and the user did not read the source document. Say the
  situation the thing exists for, or define the noun where it appears. Being the
  vocabulary of the work in progress is what makes these easy to miss: they do
  not look like the terms on this list, they look like English.

Test: if you cannot say what it means in behavior terms, you do not understand
it well enough to present it. Read the code until you can.

## Ad hoc review items

Write a title that names the result for the user. Start the item body with the required **The point** block.
Use three sentences or fewer. Name the user's request or what they see on screen in their words.
State the one decision. Omit team names for the work.

### Item template

1. **The point**
2. **How the choices behave.** Describe the current behavior and what each choice changes.
   Explain any relevant triggering event or task. Explain what can go wrong if the system chooses incorrectly.
3. **Concrete scenario.** Include one when timing, lifecycle, ownership, state changes, or multiple actors matter.
4. **Question.** Ask the user to decide in terms of behavior.
5. **Recommendation.** Name the recommended outcome and the goal or failure it addresses. Include meaningful costs or restrictions.
6. **Implementation mapping.** Add the proposed name or a short code example if it makes the decision more precise.
7. **Still pending.** Name nearby decisions that could appear settled. Say where you will handle them if known.
8. **Choices.** Follow the shared Choices rules below. Always include the exact choice `elaborate`.

Labels after **The point** are optional.

### Example

```markdown
### Keep the editor usable when its monitor disconnects

**The point**

The editor can disappear when you unplug its monitor.
Should the editor move to another monitor or stay hidden, or should the application open a replacement?

If the editor stays hidden, you cannot use it. A replacement depends on when the application opens it.
I recommend moving the current editor automatically so you can keep editing.

move automatically (recommended — you can keep editing) / leave hidden / let the application replace it / elaborate
```

### Reader test

1. Read only the title and **The point** as a reader who knows none of the work.
2. State why the item exists.
3. If you need more context to answer, rewrite the item and repeat the test.

## The comprehension gate

Before presenting, a reader who has not seen the source must be able to answer:

1. Why does this decision or finding exist?
2. What would each choice make the system do?
3. What goal or failure mode makes the recommendation preferable?

If any answer depends on an unexplained type, variant, acronym, file, or prior
finding, rebuild from the user's situation.

Add missing context to the body until the gate passes. Make the causal relationships explicit.

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
- Exactly one option marked `(recommended — <one-line reason>)`, connected to the
  user's stated goal or the failure it avoids. Open prompts ("any thoughts?") are
  exempt.
- Always include a way to discuss instead of deciding (`elaborate`, "talk through
  an item first").
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

- Never write the word "plain" or any variant — not in a header, a label, or a
  sentence. Write that way; do not announce that you are.
- Do not echo the user's feedback back to them unless asked. Record it.
- Do not manufacture content to fill a template. Write "none" and say why.
- No preamble, no recap, no closing summary.
