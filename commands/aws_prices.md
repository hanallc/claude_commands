---
description: Show AWS Spot prices and quotas.
---

Run `python3 ~/.claude/scripts/aws_target/deploy.py spot-table` and show its lines unchanged.

The command reads only the local hourly snapshot. It makes no AWS call.
The table shows the lowest zone Spot price in USD/h for each type and region, and the snapshot age.
Under the table, each region shows its Standard and G and VT Spot quotas in vCPUs and the box count of each type that fits.
Under the quotas, each type has one block with one line for each region.
A line shows the Spot price, the median merge gate time and run count, and the estimated USD per merge gate.
It also shows the paid USD/h and paid USD per merge gate from the AWS bill for that region, and refused/launched.
Gate placement uses the same estimate.
A region without its own runs uses the type's time from other regions, and the cell says so.
The last line names the cheapest merge gate.
`buildlog hourly` refreshes the snapshot with `aws_target.py snapshot`.
Empty cells say `no snapshot`, `read failed`, or `no offer`.
Each failed region shows its saved AWS error and the failure time.
Cached values remain visible with a failure label or an old age.
A failed hourly snapshot sends one user notice for each failure streak.
Permission errors request `~/.claude/scripts/lib/py ~/.claude/scripts/aws_target/deploy.py up --approve-resources`.
A successful read in every region ends the failure streak.
