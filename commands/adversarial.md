---
description: Review a topic with opposing agents.
---

`$ARGUMENTS` — optional clarifications: the exact question, the sides, the metric, what evidence counts, whether experiments are allowed. Empty means the topic under discussion now.

## 1. Frame

Write `<SCRATCH>/adversarial/<slug>/frame.md` (the session scratchpad, or `/tmp` when there is none):

- **Question:** one claim a side can win or lose, e.g. "running clippy inside mend's compile saves agent time on natedev".
- **Mode:** *argue* (advocates weigh evidence) or *prove* (each side must show it with a test, a measurement or a counterexample). Default argue; prove when the claim is checkable.
- **Metric:** what a verdict is stated in, fixed now so the verdicts compare: net minutes per day, pass/fail, which bug, the cost in tokens. In argue mode with no direct metric at hand, name one or more proxies (what each stands in for and how it can mislead), and have both sides argue in them.
- **Facts:** the raw material, with absolute paths: files, commits, data sources and how to query them, prior measurements and their method. Facts first; an existing conclusion goes last, as one claim to test, never as "the analysis to attack". Both sides get the same facts.
- **Stake:** if this session made the claim under review, say so. That is why a third agent judges.

## 2. Launch both sides

When enrolled, assign two read-only workers named `for` and `against` through `/showrunner:worker`.
Keep orders and replies in the existing run directory. Send experiments that write to a unit.
Before enrollment, start both named helpers with background `Agent` calls in one message.
Give each worker the same skeleton with its own side and evidence list. Keep the other report private.

- **Side:** "Show that <claim>." / "Show that <claim> does not hold, or costs more than it gives." Then: "Another agent argues the opposite side. If, after real effort, the evidence does not support your side, say so plainly."
- **Frame:** `frame.md` verbatim.
- **Look for, among others:** a list fitted to that side: confounds, baselines, noise, comparability, the cost side, the other machine.
- **Rules.** Use read-only commands. Do not edit files, use `git stash` or `cd`, or access credentials. Report the exit codes.
  Run an experiment only when `$ARGUMENTS` permits it. State its cost.
  Use `~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py run --repo <source> --commit HEAD -- <experiment>`.
- **Return,** in short plain words: (1) the verdict in the metric, with a range; (2) each point, with the exact command, query or `file:line` that shows it; (3) the strongest point against its own side that it could not knock down; (4) when the metric is a proxy, a better proxy if it found one, and why.

## 3. Rebuttal

Give each worker the other report in one rebuttal round. Use a new named assignment when enrolled.
Ask which points it accepts, which points it refutes, what evidence it has, and its revised verdict.

## 4. Judge

Assign `judge` through the same review route. Supply `frame.md`, both reports, and both rebuttals.
Ask for the verdict in the metric, the evidence for each side, the uncertainty, and one test to resolve it.

## 5. Report

End the staff workers after reading their replies. Before enrollment, stop the three built-in helpers. Give the user the judge's verdict first, then the two or three points that decided it, then what stays open and the test that would settle it. Keep the reports in the scratch folder and name its path.
