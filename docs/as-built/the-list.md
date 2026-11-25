# The List

The List tracks work that needs more than one action.
Each item has a number, owner, producer, and state.

Bug, Enhancement, Investigation, Change, and Validation use one report workflow.
The report type names the work in the List details.
The details keep the user's words and the source evidence.
The item shows when it entered its current state.

The producer starts work in Working.
After testing, the producer sets Repaired and records one line on how to check the result.
Needs me shows those instructions with **Works** and **Not fixed**.
**Works** sets Delivered.
**Not fixed** sets Working and sends the user's words to the owner and producer.

Review files a Change when the user sends feedback or returns the change to a team.
**Ask showrunner to check** files a Validation with the showrunner as owner.
The native record keeps the reply and the source log.
Standalone change review links file the same reports.
Review files the List report before it saves the verdict.
If List filing fails, the user can submit the verdict again.
The source key identifies one List report across retries.
If the verdict save fails, the next attempt updates the report with the new verdict words.
A failed validation shows the validator's words under **Validation result** in the List details.

`investigation.py file` files an Investigation on the List.
The command prints the native ID for the answer command.
An interrupted command with the same words resumes the filing until it prints the ID.

Run `the_list.py move-review-waits` from the Morty session to move open Waiting and Validating Review records.
The command retains the native record and adds `moved to List item <n>` to its log.
A repeat run uses the saved List number for an interrupted move.
The command checks the current state while it holds the source lock.
Native replies appear with the List report and stay out of Review counts and `/next`.
