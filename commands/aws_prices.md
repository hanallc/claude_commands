---
description: Show AWS Spot prices and quotas.
---

Run `python3 ~/.claude/scripts/aws_target/deploy.py spot-table` and show its lines unchanged.

The command reads only the local hourly snapshot and cost ledger. It makes no AWS call.
Two tables show the lowest zone Spot price in USD/h for each type and region: standard types, then GPU types.
The snapshot age of each region follows the prices.
Two quota tables follow: the Standard quota with the standard box counts, and the G and VT quota with the GPU box counts.
Under the quotas, each standard type has one block with one line for each region.
A line shows the Spot price, the median merge gate time in minutes, the run count, and the estimated USD per merge gate.
It also shows the paid USD/h, the paid USD per merge gate from the AWS bill for that region, and the paid run count.
Gate placement uses the same estimate.
A region without its own runs uses the type's time from other regions, and the runs cell says `elsewhere`.
The next line names the cheapest merge gate.
Then two tables show the refused requests and the launched boxes of each type and region.
The last table shows billed hours / box hours with the gap, and `over` above `billed_hours_gap_max_ratio`.
`buildlog hourly` refreshes the snapshot with `aws_target.hourly`.
Empty cells say `no snapshot`, `read failed`, or `not sold here`.
Each failed region shows its saved AWS error and the failure time.
Cached values remain visible with a failure label or an old age.
A failed hourly snapshot sends one user notice for each failure streak.
Permission errors request `~/.claude/scripts/lib/py ~/.claude/scripts/aws_target/deploy.py up --approve-resources`.
A successful read in every region ends the failure streak.
